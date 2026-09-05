import { describe, expect, it } from 'vitest';
import {
  attachOptions,
  attachedRecordsWarning,
  fetchAllPages,
  documentName,
  entityTypeLabel,
  formatBytes,
  mimeLabel,
  otherRecordsWarning,
  type AttachPool,
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
