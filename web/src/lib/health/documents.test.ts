import { describe, expect, it } from 'vitest';
import {
  attachOptions,
  attachPoolNotice,
  attachedRecordsWarning,
  fetchAllPages,
  documentName,
  entityTypeLabel,
  formatBytes,
  mimeLabel,
  otherRecordsWarning,
  sourceLabel,
  truncationNotice,
  type AttachPool,
  type TruncationFlags,
} from './documents';
import type { Diagnosis, DocumentLink, Encounter, Immunization } from '$lib/api';

describe('formatBytes', () => {
  it('renders bytes below 1 KB verbatim', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('steps up through binary units', () => {
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(184320)).toBe('180 KB');
    expect(formatBytes(1024 * 1024)).toBe('1 MB');
    expect(formatBytes(25 * 1024 * 1024)).toBe('25 MB');
    expect(formatBytes(1024 ** 3)).toBe('1 GB');
  });

  it('keeps one decimal only below 10 of a unit', () => {
    expect(formatBytes(1024 * 1024 * 1.5)).toBe('1.5 MB');
    expect(formatBytes(1024 * 1024 * 12.34)).toBe('12 MB');
  });

  it('does not pretend to know a nonsensical size', () => {
    expect(formatBytes(-1)).toBe('—');
    expect(formatBytes(Number.NaN)).toBe('—');
  });
});

describe('otherRecordsWarning', () => {
  it('says nothing when the document is attached only here', () => {
    expect(otherRecordsWarning(0)).toBe('');
    expect(otherRecordsWarning(-1)).toBe('');
  });

  it('is singular for one other record', () => {
    expect(otherRecordsWarning(1)).toBe(
      'This document is also attached to 1 other record. Deleting removes it everywhere.',
    );
  });

  it('is plural for several', () => {
    expect(otherRecordsWarning(2)).toBe(
      'This document is also attached to 2 other records. Deleting removes it everywhere.',
    );
  });
});

describe('mimeLabel', () => {
  it('shortens the types a user actually sees', () => {
    expect(mimeLabel('application/pdf')).toBe('PDF');
    expect(mimeLabel('image/jpeg')).toBe('JPEG');
    expect(mimeLabel('text/plain')).toBe('Text');
  });

  it('falls through unchanged for anything else', () => {
    expect(mimeLabel('application/zip')).toBe('application/zip');
  });
});

describe('sourceLabel', () => {
  it('names every source the store writes', () => {
    expect(sourceLabel('manual')).toBe('Uploaded');
    expect(sourceLabel('import')).toBe('Imported');
    expect(sourceLabel('lab_panel')).toBe('Lab panel');
  });

  it('falls through unchanged for anything else', () => {
    // Shown as written rather than hidden: a source this table does not know
    // about is still a fact about the document, and the row is the only place
    // it appears now that the Type column is gone.
    expect(sourceLabel('garmin')).toBe('garmin');
  });
});

describe('documentName', () => {
  const base = {
    id: 1,
    filename: 'after-visit-summary.pdf',
    mime: 'application/pdf',
    byte_size: 10,
    source: 'import' as const,
    notes: null,
    created_at: '',
    url: '',
  };

  it('prefers what the user uploaded over the sanitized name', () => {
    expect(documentName({ ...base, original_filename: 'After Visit Summary.pdf' })).toBe(
      'After Visit Summary.pdf',
    );
  });

  it('falls back to the stored name', () => {
    expect(documentName({ ...base, original_filename: null })).toBe('after-visit-summary.pdf');
  });
});

describe('attachedRecordsWarning', () => {
  it('names an unattached document rather than saying nothing', () => {
    // The counterpart to otherRecordsWarning's silence at 0. From the
    // Documents table this is the one document nothing else surfaces, so the
    // confirmation says so instead of leaving the count implicit.
    expect(attachedRecordsWarning(0)).toBe('It is not attached to any record.');
  });

  it('warns at one, where otherRecordsWarning deliberately does not', () => {
    expect(attachedRecordsWarning(1)).toBe('It is attached to 1 record, which will lose it.');
    expect(otherRecordsWarning(0)).toBe('');
  });

  it('pluralizes above one', () => {
    expect(attachedRecordsWarning(3)).toBe('It is attached to 3 records, which will lose it.');
  });
});

describe('fetchAllPages', () => {
  it('stops on a short page and returns everything', async () => {
    const pages = [[1, 2, 3], [4]];
    const seen: Array<[number, number]> = [];
    const out = await fetchAllPages<number>(
      (offset, limit) => {
        seen.push([offset, limit]);
        return Promise.resolve(pages.shift() ?? []);
      },
      { pageSize: 3, maxPages: 5 },
    );
    expect(out).toEqual({ items: [1, 2, 3, 4], truncated: false });
    // The offset is what has been collected, not the page index.
    expect(seen).toEqual([
      [0, 3],
      [3, 3],
    ]);
  });

  it('is one request when the first page is short', async () => {
    let calls = 0;
    const out = await fetchAllPages<number>(
      () => {
        calls += 1;
        return Promise.resolve([1]);
      },
      { pageSize: 3, maxPages: 5 },
    );
    expect(calls).toBe(1);
    expect(out.truncated).toBe(false);
  });

  it('stops on an empty page even at exactly the page size', async () => {
    // A total that is an exact multiple of the page size: the last full page
    // is followed by an empty one, which must end the walk rather than count
    // as "still going" and burn the whole ceiling.
    const pages = [[1, 2], [3, 4], []];
    const out = await fetchAllPages<number>(() => Promise.resolve(pages.shift() ?? []), {
      pageSize: 2,
      maxPages: 5,
    });
    expect(out).toEqual({ items: [1, 2, 3, 4], truncated: false });
  });

  it('reports truncation rather than looping forever', async () => {
    // A server that keeps answering with a full page. Without the ceiling this
    // never returns; without the flag the caller states a total that is a lie.
    let calls = 0;
    const out = await fetchAllPages<number>(
      () => {
        calls += 1;
        return Promise.resolve([1, 2]);
      },
      { pageSize: 2, maxPages: 3 },
    );
    expect(calls).toBe(3);
    expect(out.items).toHaveLength(6);
    expect(out.truncated).toBe(true);
  });
});

describe('attachOptions', () => {
  const iso = (s: string) => s;

  const pool: AttachPool = {
    encounters: [
      { id: 1, encounter_date: '2026-06-29', encounter_type: 'visit' } as Encounter,
      { id: 2, encounter_date: '2026-07-04', encounter_type: 'follow-up' } as Encounter,
    ],
    diagnoses: [
      { id: 1, name: 'Anaemia', icd10: 'D64.9' } as Diagnosis,
      { id: 5, name: 'Asthma', icd10: null } as Diagnosis,
    ],
    immunizations: [{ id: 1, name: 'Tetanus', date_given: '2026-02-02' } as Immunization],
  };

  function link(entity_type: DocumentLink['entity_type'], entity_id: number): DocumentLink {
    return { entity_type, entity_id, label: 'x' };
  }

  it('offers every record of the type when nothing is attached', () => {
    expect(attachOptions('encounter', pool, [], iso).map((o) => o.value)).toEqual(['1', '2']);
  });

  it('drops a record already attached, so the picker offers no no-op', () => {
    expect(
      attachOptions('encounter', pool, [link('encounter', 1)], iso).map((o) => o.value),
    ).toEqual(['2']);
  });

  it('scopes the filter by type, not by bare id', () => {
    // The trap document_links sets: encounter 1 and diagnosis 1 are different
    // records sharing a number. Filtering on the id alone would hide Anaemia
    // from the picker because a visit was attached, with nothing on screen to
    // explain the missing row.
    const options = attachOptions('diagnosis', pool, [link('encounter', 1)], iso);
    expect(options.map((o) => o.value)).toEqual(['1', '5']);
    expect(options[0].label).toBe('Anaemia (D64.9)');
  });

  it('labels immunizations by date and name', () => {
    expect(attachOptions('immunization', pool, [], iso)).toEqual([
      { value: '1', label: '2026-02-02 · Tetanus' },
    ]);
  });
});

describe('entityTypeLabel', () => {
  it('renders the user-facing word, not the table name', () => {
    expect(entityTypeLabel('encounter')).toBe('Visit');
    expect(entityTypeLabel('diagnosis')).toBe('Condition');
    expect(entityTypeLabel('immunization')).toBe('Vaccination');
  });

  it('passes an unknown type through rather than blanking the chip', () => {
    // A link type the server grows before this file learns about it must
    // still render as something the user can act on.
    expect(entityTypeLabel('panel')).toBe('panel');
  });
});

const whole: TruncationFlags = {
  documents: false,
  encounters: false,
  diagnoses: false,
  immunizations: false,
};

describe('truncationNotice', () => {
  it('says nothing when every list was walked to the end', () => {
    expect(truncationNotice(whole)).toBe('');
  });

  it('keeps the documents sentence about the documents themselves', () => {
    expect(truncationNotice({ ...whole, documents: true })).toBe(
      'There are more documents than this page loads. The oldest are not shown.',
    );
  });

  it('names a cut pool, and says what it costs', () => {
    // A cut pool does not shorten the table — it shortens the attach picker,
    // which is the half the user cannot see.
    expect(truncationNotice({ ...whole, encounters: true })).toBe(
      'There are more visits than this page loads, so the attach picker does not offer every record.',
    );
  });

  it('lists several cut pools in the order the picker offers them', () => {
    expect(truncationNotice({ ...whole, immunizations: true, encounters: true })).toBe(
      'There are more visits and vaccinations than this page loads, so the attach picker does not offer every record.',
    );
    expect(
      truncationNotice({
        documents: false,
        encounters: true,
        diagnoses: true,
        immunizations: true,
      }),
    ).toBe(
      'There are more visits, conditions and vaccinations than this page loads, so the attach picker does not offer every record.',
    );
  });

  it('carries both sentences when the documents and a pool were both cut', () => {
    expect(truncationNotice({ ...whole, documents: true, diagnoses: true })).toBe(
      'There are more documents than this page loads. The oldest are not shown. ' +
        'There are more conditions than this page loads, so the attach picker does not offer every record.',
    );
  });
});

describe('attachPoolNotice', () => {
  it('says nothing for a pool that was read whole', () => {
    expect(attachPoolNotice('encounter', whole)).toBe('');
    // Another pool's cut says nothing about this one — the picker shows one
    // type at a time, and a blanket notice on all three would be false for two.
    expect(attachPoolNotice('encounter', { ...whole, diagnoses: true })).toBe('');
    // A cut documents list says nothing about any pool at all.
    expect(attachPoolNotice('encounter', { ...whole, documents: true })).toBe('');
  });

  it('names the type the picker is showing, not the pool key', () => {
    expect(attachPoolNotice('encounter', { ...whole, encounters: true })).toBe(
      'There are more visits than this page loads, so this list is not complete.',
    );
    expect(attachPoolNotice('diagnosis', { ...whole, diagnoses: true })).toBe(
      'There are more conditions than this page loads, so this list is not complete.',
    );
    expect(attachPoolNotice('immunization', { ...whole, immunizations: true })).toBe(
      'There are more vaccinations than this page loads, so this list is not complete.',
    );
  });
});
