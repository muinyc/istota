/**
 * The Documents view (ISSUE-423).
 *
 * Named without the `+` prefix for the reason `dashboard.svelte.test.ts` is:
 * SvelteKit reserves that prefix under `src/routes/` and `svelte-kit sync`
 * refuses to build a manifest when it finds a name it does not recognize.
 *
 * ---
 *
 * Two of the four assertions here are about something the rendered output
 * cannot show you.
 *
 * **`getDocument` is never called.** Associations come off the list payload,
 * which is the whole point of widening `GET /documents` — a page that fetched
 * a document per row would render exactly the same table, and only the call
 * record tells the two apart. The server-side half of the same property is
 * `tests/test_health_routes.py::TestDocumentRoutes::test_list_link_labels_cost_no_query_per_row`.
 *
 * **Detach is asserted on the pair it sent, not on it having sent something.**
 * `document_links` is polymorphic, so a chip carries `(entity_type, entity_id)`
 * and the id alone names three different records. A detach that read the id
 * off the chip and the type off anywhere else would unlink an unrelated
 * record, and every visible outcome — the chip going, the table reloading —
 * would look identical.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, within } from '@testing-library/svelte';

// Real helpers, small paging. The page names its page size at the call site
// precisely so this override is possible: reaching the truncation ceiling at
// the production size means rendering twenty thousand table rows, which runs
// jsdom out of memory rather than proving anything.
vi.mock('$lib/health/documents', async () => {
  const actual =
    await vi.importActual<typeof import('$lib/health/documents')>('$lib/health/documents');
  // 3, not 2: the base fixture below holds two documents, and at a page size
  // of 2 that is a *full* page, so every unrelated test would page a second
  // time and its call-count assertions would be about paging instead of about
  // what they are named for.
  const small = { documents: 3, encounters: 3, diagnoses: 3, immunizations: 3 };
  return { ...actual, PAGE_SIZES: small, MAX_PAGES: 3 };
});

vi.mock('$lib/api', async () => {
  const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
  return {
    ...actual,
    listDocuments: vi.fn(),
    listEncounters: vi.fn(),
    listDiagnoses: vi.fn(),
    listImmunizations: vi.fn(),
    getDocument: vi.fn(),
    linkDocument: vi.fn(),
    unlinkDocument: vi.fn(),
    deleteDocument: vi.fn(),
  };
});

import {
  deleteDocument,
  getDocument,
  linkDocument,
  listDiagnoses,
  listDocuments,
  listEncounters,
  listImmunizations,
  unlinkDocument,
  type DocumentLink,
  type HealthDocument,
} from '$lib/api';
import Page from './+page.svelte';

afterEach(cleanup);

function doc(id: number, name: string, links: DocumentLink[]): HealthDocument {
  return {
    id,
    filename: name,
    original_filename: null,
    mime: 'application/pdf',
    byte_size: 2048,
    source: 'manual',
    notes: null,
    created_at: '2026-06-29T10:00:00+00:00',
    url: `/istota/api/health/documents/${id}/file`,
    links,
  };
}

// Two links of different types on one document, sharing neither id nor type,
// so an implementation that read the id off the chip and hardcoded the type
// sends a different pair than the one the chip names.
const attached = doc(1, 'discharge.pdf', [
  { entity_type: 'diagnosis', entity_id: 4, label: 'Anaemia' },
  { entity_type: 'encounter', entity_id: 7, label: '2026-06-29 — visit' },
]);
const loose = doc(2, 'scan.pdf', []);

beforeEach(() => {
  // No `clearMocks` in vitest.config, so call counts would otherwise carry
  // across tests and every "called once" assertion would drift upward.
  vi.clearAllMocks();
  vi.mocked(listDocuments).mockResolvedValue({ documents: [attached, loose] });
  vi.mocked(listDocuments).mockImplementation(async (_entity, page) =>
    // A short page, so the paging walk stops after one request. A mock that
    // ignored `offset` and always answered in full would page forever.
    (page?.offset ?? 0) > 0 ? { documents: [] } : { documents: [attached, loose] },
  );
  vi.mocked(listEncounters).mockResolvedValue({
    encounters: [{ id: 7, encounter_date: '2026-06-29', encounter_type: 'visit' }],
  } as Awaited<ReturnType<typeof listEncounters>>);
  vi.mocked(listDiagnoses).mockResolvedValue({ diagnoses: [] } as Awaited<
    ReturnType<typeof listDiagnoses>
  >);
  vi.mocked(listImmunizations).mockResolvedValue({ immunizations: [] } as Awaited<
    ReturnType<typeof listImmunizations>
  >);
  vi.mocked(unlinkDocument).mockResolvedValue({ status: 'ok', removed: true });
  vi.mocked(linkDocument).mockResolvedValue({ status: 'ok', created: true });
  vi.mocked(deleteDocument).mockResolvedValue({ status: 'ok' });
  vi.mocked(getDocument).mockRejectedValue(new Error('the view must not fetch per row'));
});

describe('the Documents view', () => {
  it('lists every document, attached or not', async () => {
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());
    // The unattached one is the case that appeared on no other page at all.
    expect(screen.getByText('scan.pdf')).toBeTruthy();
    expect(screen.getByText('2026-06-29 — visit')).toBeTruthy();
    expect(screen.getByText('Visit')).toBeTruthy();
  });

  it('says where a document came from, under its name rather than in a column', async () => {
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());
    // Both fixtures are `manual`, so this is one label per row and the count
    // is what says it is on the row rather than in a header somewhere.
    expect(screen.getAllByText('Uploaded')).toHaveLength(2);
    // The column it used to share with the MIME chip is gone with the chip.
    expect(screen.queryByText('PDF')).toBeNull();
  });

  it('reads associations off the list payload rather than a request per row', async () => {
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());
    expect(vi.mocked(getDocument)).not.toHaveBeenCalled();
    expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(1);
  });

  it('detaches the pair on the chip, not the bare id', async () => {
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());

    vi.mocked(listDocuments).mockResolvedValue({ documents: [doc(1, 'discharge.pdf', []), loose] });
    screen.getByLabelText('Detach from Anaemia').click();

    await waitFor(() => expect(vi.mocked(unlinkDocument)).toHaveBeenCalled());
    expect(vi.mocked(unlinkDocument)).toHaveBeenCalledWith(1, { type: 'diagnosis', id: 4 });
    // The table is re-read rather than patched, so what is on screen is what
    // the server says — including any link the request changed as a side
    // effect of a dedup.
    await waitFor(() => expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(2));
  });

  it('filters to the documents nothing points at', async () => {
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());

    screen.getByText('1 unattached').click();

    await waitFor(() => expect(screen.queryByText('discharge.pdf')).toBeNull());
    expect(screen.getByText('scan.pdf')).toBeTruthy();
  });

  it('reports a failed detach without taking the table away', async () => {
    vi.mocked(unlinkDocument).mockRejectedValue(new Error('nope'));
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());

    screen.getByLabelText('Detach from Anaemia').click();

    await waitFor(() => expect(screen.getByText('nope')).toBeTruthy());
    // A load failure replaces the pane; an action failure must not, because
    // everything on screen is still correct.
    expect(screen.getByText('discharge.pdf')).toBeTruthy();
    const banner = screen.getByText('nope');
    expect(banner.className).toContain('banner');
  });

  it('replaces the pane when the load itself fails', async () => {
    vi.mocked(listDocuments).mockRejectedValue(new Error('offline'));
    render(Page);
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy());
    expect(screen.queryByText('discharge.pdf')).toBeNull();
    const msg = screen.getByText('offline');
    expect(msg.className).toContain('center-msg');
  });

  it('walks past the first page rather than showing only it', async () => {
    // `GET /documents` defaults to 200 and caps at 1000, and orders
    // newest-first — so a single-request page silently drops the OLDEST
    // documents, which are the ones the 24h orphan sweep is about to take and
    // the ones this view exists to surface.
    const first = [
      doc(101, 'newer.pdf', []),
      doc(102, 'newish.pdf', []),
      doc(103, 'older.pdf', []),
    ];
    vi.mocked(listDocuments).mockImplementation(async (_entity, page) =>
      (page?.offset ?? 0) === 0
        ? { documents: first }
        : { documents: [doc(9001, 'oldest.pdf', [])] },
    );

    render(Page);
    await waitFor(() => expect(screen.getByText('oldest.pdf')).toBeTruthy());
    expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(2);
    // The offset is what has been collected so far, so the second page starts
    // where the first ended rather than at a page index.
    expect(vi.mocked(listDocuments)).toHaveBeenLastCalledWith(undefined, { limit: 3, offset: 3 });
    expect(screen.getByText('4 documents on file')).toBeTruthy();
  });

  it('refuses to state a total it could not reach', async () => {
    // A server that keeps answering with a full page. The walk stops at its
    // ceiling; the header must then describe what is loaded rather than claim
    // a count of the store, and the page must say so out loud.
    vi.mocked(listDocuments).mockImplementation(async (_entity, page) => {
      const at = page?.offset ?? 0;
      return {
        documents: [
          doc(1000 + at, `a-${at}.pdf`, []),
          doc(2000 + at, `b-${at}.pdf`, []),
          doc(3000 + at, `c-${at}.pdf`, []),
        ],
      };
    });

    render(Page);
    await waitFor(() => expect(screen.getByText(/showing the .* most recent/)).toBeTruthy());
    expect(screen.getByText(/There are more documents than this page loads/)).toBeTruthy();
    expect(screen.queryByText(/documents on file/)).toBeNull();
    expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(3);
  });

  it('keeps the table on screen while a reload runs', async () => {
    // The row-level busy state is the whole point of the guard, and it is only
    // observable if the reload does not swap the table for the whole-pane
    // "Loading…". A `waitFor` would never catch this, so the reload is held
    // open deliberately.
    let releaseReload = () => {};
    render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());

    vi.mocked(listDocuments).mockImplementation(
      () => new Promise((resolve) => (releaseReload = () => resolve({ documents: [loose] }))),
    );
    screen.getByLabelText('Detach from Anaemia').click();

    // Wait for the reload to have *started* — not merely for the unlink to
    // have been called. `mutate` awaits the unlink before calling `load`, so
    // releasing on the unlink alone resolves a promise that does not exist
    // yet and the reload then hangs for the rest of the test.
    await waitFor(() => expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(2));
    // Mid-reload: still the table, not the pane message.
    expect(screen.queryByText('Loading…')).toBeNull();
    expect(screen.getByText('discharge.pdf')).toBeTruthy();

    releaseReload();
    await waitFor(() => expect(screen.queryByText('discharge.pdf')).toBeNull());
  });

  it('names the number of records a delete would strip it from', async () => {
    const { container } = render(Page);
    await waitFor(() => expect(screen.getByText('discharge.pdf')).toBeTruthy());
    // The dialog is driven by `deleteTarget`, reached through the kebab. What
    // matters here is that the row offers the action at all; the wording is
    // pinned in `lib/health/documents.test.ts`.
    const row = screen.getByText('discharge.pdf').closest('tr') as HTMLElement;
    expect(within(row).getByLabelText('Document actions')).toBeTruthy();
    expect(container.querySelector('.attach')).toBeTruthy();
  });
});
