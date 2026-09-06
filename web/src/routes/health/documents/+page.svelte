<script lang="ts">
  /**
   * Every uploaded health document, and what it is attached to (ISSUE-423).
   *
   * The other three document surfaces hang off an entity — the encounter page,
   * the immunization detail page, the conditions modal — so finding a document
   * meant already knowing which visit or condition it had been filed under,
   * and a document whose links were never made, or were removed, appeared on
   * no page at all while still sitting on disk until the orphan sweep took it.
   * This is the view that has no entity: the table is the whole store.
   *
   * Associations come off the list payload rather than a request per row —
   * `GET /documents` carries `links` for exactly this table, and
   * `tests/test_health_routes.py::...::test_list_link_labels_cost_no_query_per_row`
   * is what holds that. A chip's label is the server's spelling, so this page
   * and `GET /documents/{id}` cannot disagree about what a document is on.
   */
  import { onMount } from 'svelte';
  // A plain `Set` in `$state` is not deeply reactive in Svelte 5 — mutating it
  // would not re-render the rows reading `busy.has(...)`.
  import { SvelteSet } from 'svelte/reactivity';
  import {
    deleteDocument,
    linkDocument,
    listDiagnoses,
    listDocuments,
    listEncounters,
    listImmunizations,
    unlinkDocument,
    type Diagnosis,
    type DocumentEntity,
    type DocumentLink,
    type Encounter,
    type HealthDocument,
    type Immunization,
  } from '$lib/api';
  import {
    Button,
    Chip,
    ConfirmDialog,
    Field,
    KebabMenu,
    Modal,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import {
    attachOptions,
    attachPoolNotice,
    attachedRecordsWarning,
    documentName,
    entityTypeLabel,
    fetchAllPages,
    formatBytes,
    MAX_PAGES,
    PAGE_SIZES,
    sourceLabel,
    truncationNotice,
    type TruncationFlags,
  } from '$lib/health/documents';

  const entityTypes: SelectOption[] = [
    { value: 'encounter', label: 'Visit' },
    { value: 'diagnosis', label: 'Condition' },
    { value: 'immunization', label: 'Vaccination' },
  ];

  // `loading` is the first load only. A reload after an edit sets `refreshing`
  // instead, which dims the table rather than replacing it with the whole-pane
  // message — swapping a populated table for "Loading…" on every detach makes
  // the row-level busy state below unreachable and the page flash.
  let loading = $state(true);
  let refreshing = $state(false);
  // Two error slots, because they are two states. A load failure replaces the
  // pane; an action failure sits above a table that is still correct.
  let loadError = $state('');
  let actionError = $state('');
  let documents = $state<HealthDocument[]>([]);
  // Which of the four walks ran out of pages before it ran out of records, one
  // flag each. On `documents` it means the header must not state a total and
  // the unattached count is a count of what is loaded rather than of what
  // exists. On the three pools it means the attach picker is short — a
  // different consequence on a different surface, which is why this is not one
  // boolean (ISSUE-441).
  let truncated = $state<TruncationFlags>({
    documents: false,
    encounters: false,
    diagnoses: false,
    immunizations: false,
  });
  let encounters = $state<Encounter[]>([]);
  let diagnoses = $state<Diagnosis[]>([]);
  let immunizations = $state<Immunization[]>([]);

  let unattachedOnly = $state(false);

  // A set, not a scalar: two mutations on different rows are both reachable
  // (each row's buttons are disabled only by its own id), and one scalar means
  // whichever finishes first clears the other's guard.
  let busy = $state(new SvelteSet<number>());
  // Both dialogs address a document by **id**, never by holding the object.
  // `load()` replaces `documents` wholesale, so a held reference keeps the
  // pre-reload `links` — which would let the attach picker offer a record that
  // is already attached, and the delete confirmation name a record count that
  // no longer holds. That count is the one number the user is acting on.
  let attachForId = $state<number | null>(null);
  let attachType = $state<DocumentEntity>('encounter');
  let attachId = $state('');
  let attachError = $state('');
  let deleteTargetId = $state<number | null>(null);

  const pool = $derived({ encounters, diagnoses, immunizations });

  const byId = $derived(new Map(documents.map((d) => [d.id, d])));
  const attachFor = $derived(attachForId === null ? null : (byId.get(attachForId) ?? null));
  const deleteTarget = $derived(
    deleteTargetId === null ? null : (byId.get(deleteTargetId) ?? null),
  );

  const rows = $derived(
    unattachedOnly ? documents.filter((d) => links(d).length === 0) : documents,
  );

  const unattachedCount = $derived(documents.filter((d) => links(d).length === 0).length);

  const attachChoices = $derived(
    attachFor ? attachOptions(attachType, pool, links(attachFor), formatDate) : [],
  );

  // Two surfaces, because the banner is behind the modal and cannot warn
  // somebody who is already picking from a short list.
  const pageNotice = $derived(truncationNotice(truncated));
  const poolNotice = $derived(attachPoolNotice(attachType, truncated));

  /**
   * A document's links, defaulting to none.
   *
   * `HealthDocument.links` is optional on the type because the upload
   * acknowledgement omits it, but every row here comes from a listing, which
   * always carries it — including as an explicit `[]` for the unattached
   * documents this page exists to surface.
   */
  function links(doc: HealthDocument): DocumentLink[] {
    return doc.links ?? [];
  }

  /**
   * Read everything the page shows.
   *
   * `quiet` is what a reload after an edit passes: it keeps the table mounted
   * and merely dims it, where the initial load owns the whole pane. Without
   * that split every detach blanks the page, and the row-level busy state
   * below is never on screen long enough to be seen.
   *
   * Every list is walked to the end rather than taking the server's default
   * page. This page states a total and derives an orphan count from what it
   * holds, and `list_documents` orders newest-first — so a silent cut drops
   * the *oldest* documents, which are exactly the ones the 24h orphan sweep is
   * about to delete and the ones this view exists to surface.
   */
  async function load({ quiet = false } = {}) {
    if (quiet) refreshing = true;
    else loading = true;
    loadError = '';
    // Named at the call site rather than left to the helper's defaults, so the
    // page's paging policy is visible here and a test can shrink it without
    // rendering twenty thousand rows to reach the ceiling. One size per list,
    // because each endpoint caps `limit` at its own value and asking for more
    // is a 422 that fails this whole `Promise.all`.
    const paged = (list: keyof typeof PAGE_SIZES) => ({
      pageSize: PAGE_SIZES[list],
      maxPages: MAX_PAGES,
    });
    try {
      const [docs, enc, dx, imm] = await Promise.all([
        fetchAllPages(async (offset, limit) => {
          const out = await listDocuments(undefined, { limit, offset });
          return out.documents;
        }, paged('documents')),
        fetchAllPages(async (offset, limit) => {
          const out = await listEncounters({ limit, offset });
          return out.encounters;
        }, paged('encounters')),
        fetchAllPages(async (offset, limit) => {
          const out = await listDiagnoses({ status: 'all', limit, offset });
          return out.diagnoses;
        }, paged('diagnoses')),
        fetchAllPages(async (offset, limit) => {
          const out = await listImmunizations({ limit, offset });
          return out.immunizations;
        }, paged('immunizations')),
      ]);
      documents = docs.items;
      encounters = enc.items;
      diagnoses = dx.items;
      immunizations = imm.items;
      truncated = {
        documents: docs.truncated,
        encounters: enc.truncated,
        diagnoses: dx.truncated,
        immunizations: imm.truncated,
      };
      // The filter's chip is only rendered while something is unattached, so
      // attaching the last loose document would otherwise leave the filter on
      // with no control to turn it off and an empty table behind it — reached
      // by the exact workflow this view is for.
      if (unattachedCount === 0) unattachedOnly = false;
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load documents';
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  /** Run a mutation on one row, then re-read. Owns the per-row busy guard. */
  async function mutate(id: number, action: () => Promise<void>, failure: string) {
    actionError = '';
    busy.add(id);
    try {
      await action();
      await load({ quiet: true });
    } catch (e) {
      actionError = e instanceof Error ? e.message : failure;
    } finally {
      busy.delete(id);
    }
  }

  function detach(doc: HealthDocument, link: DocumentLink) {
    return mutate(
      doc.id,
      () => unlinkDocument(doc.id, { type: link.entity_type, id: link.entity_id }).then(() => {}),
      'Failed to detach',
    );
  }

  function openAttach(doc: HealthDocument) {
    attachForId = doc.id;
    attachType = 'encounter';
    attachId = '';
    attachError = '';
  }

  function pickType(v: string) {
    attachType = v as DocumentEntity;
    // The chosen record belongs to the old type; keeping it would attach the
    // document to whatever shares that id in the new one.
    attachId = '';
  }

  async function attach() {
    const doc = attachFor;
    const id = Number(attachId);
    if (!doc || !Number.isFinite(id) || id <= 0) return;
    const target = { type: attachType, id };
    attachError = '';
    busy.add(doc.id);
    try {
      const out = await linkDocument(doc.id, target);
      attachForId = null;
      await load({ quiet: true });
      // `link_document` is INSERT OR IGNORE, so an already-attached record is
      // a success with `created: false`. The picker filters those out, so
      // seeing one means the pool was stale — say so rather than reporting a
      // change that did not happen.
      if (!out.created) {
        actionError = 'That document was already attached to this record.';
      }
    } catch (e) {
      attachError = e instanceof Error ? e.message : 'Failed to attach';
    } finally {
      busy.delete(doc.id);
    }
  }

  async function confirmDelete() {
    const doc = deleteTarget;
    deleteTargetId = null;
    if (!doc) return;
    await mutate(doc.id, () => deleteDocument(doc.id).then(() => {}), 'Failed to delete');
  }

  function menu(doc: HealthDocument): KebabItem[] {
    return [
      { label: 'Open', href: doc.url },
      { label: 'Attach to a record', onSelect: () => openAttach(doc) },
      { label: 'Delete', danger: true, onSelect: () => (deleteTargetId = doc.id) },
    ];
  }

  function formatDate(iso: string): string {
    if (!iso) return '';
    // Two shapes reach this. `documents.created_at` is a full ISO timestamp,
    // an entity's date is a bare `YYYY-MM-DD`, and the column's schema DEFAULT
    // is SQLite's `datetime('now')` — space-separated, no `T`. Test for a time
    // part rather than for the `T`, or that third shape parses as an Invalid
    // Date and renders raw in a column where every other row reads "29 Jun".
    const hasTime = /\d{2}:\d{2}/.test(iso);
    const d = new Date(hasTime ? iso.replace(' ', 'T') : `${iso}T00:00:00`);
    if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  onMount(load);
</script>

{#if !loading && !loadError}
  <!-- Held back while loading so the pane centres its message rather than
       centring it in the space left under this header. -->
  <div class="header">
    <div>
      <h1>Documents</h1>
      <p class="sub">
        <!-- Never "N on file" once the walk stopped early: the count would be
             a claim about the store rather than about what is loaded. -->
        {#if truncated.documents}
          showing the {documents.length} most recent
        {:else}
          {documents.length}
          {documents.length === 1 ? 'document' : 'documents'} on file
        {/if}
      </p>
    </div>
    {#if unattachedCount > 0 || unattachedOnly}
      <!-- Rendered while the filter is on even at zero, so turning it off is
           always reachable. `aria-pressed` because `checked` only draws the
           state; see the Chip component's own note. -->
      <Chip
        checked={unattachedOnly}
        aria-pressed={unattachedOnly}
        onclick={() => (unattachedOnly = !unattachedOnly)}
      >
        {unattachedCount} unattached
      </Chip>
    {/if}
  </div>
{/if}

<!-- Held back on a load failure alongside the header above it: that branch
     replaces the pane, so there is no table and no picker for this to be
     about, and the flags are whatever the last successful load left. -->
{#if pageNotice && !loadError}
  <div class="banner warn">{pageNotice}</div>
{/if}

{#if actionError}
  <!-- An attach, detach or delete that failed. Separate from `loadError`: the
       table is still on screen and still correct, so this sits above it rather
       than replacing it with a whole-pane failure. -->
  <div class="banner error">{actionError}</div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if loadError}
  <div class="center-msg error">{loadError}</div>
{:else if documents.length === 0}
  <div class="empty">
    Nothing uploaded yet. Documents attach to a visit, a condition or a vaccination from those
    pages, and every one of them shows up here.
  </div>
{:else if rows.length === 0}
  <div class="empty">Every document is attached to a record.</div>
{:else}
  <div class="table-scroll" class:refreshing>
    <table class="grid">
      <thead>
        <tr>
          <th>Document</th>
          <th class="num">Size</th>
          <th>Added</th>
          <th>Attached to</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as doc (doc.id)}
          <tr class:busy={busy.has(doc.id)}>
            <td>
              <a class="name" href={doc.url} title={documentName(doc)}>{documentName(doc)}</a>
              <p class="source">{sourceLabel(doc.source)}</p>
              {#if doc.notes}<p class="notes">{doc.notes}</p>{/if}
            </td>
            <td class="num">{formatBytes(doc.byte_size)}</td>
            <td class="nowrap">{formatDate(doc.created_at)}</td>
            <td>
              <div class="links">
                {#each links(doc) as link (`${link.entity_type}:${link.entity_id}`)}
                  <span class="link-chip">
                    <span class="link-kind">{entityTypeLabel(link.entity_type)}</span>
                    <span class="link-label">{link.label}</span>
                    <button
                      type="button"
                      class="detach"
                      disabled={busy.has(doc.id)}
                      onclick={() => detach(doc, link)}
                      aria-label="Detach from {link.label}">×</button
                    >
                  </span>
                {/each}
                <button
                  type="button"
                  class="attach"
                  disabled={busy.has(doc.id)}
                  onclick={() => openAttach(doc)}
                >
                  + Attach
                </button>
              </div>
            </td>
            <td class="actions">
              <KebabMenu items={menu(doc)} ariaLabel="Document actions" />
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if attachFor}
  <Modal
    open={true}
    title="Attach {documentName(attachFor)}"
    description="Pick the record this document is evidence for. To move it, attach the new record and detach the old one."
    width="30rem"
    onOpenChange={(open) => {
      if (!open) attachForId = null;
    }}
  >
    <div class="attach-form">
      <!-- `labelled={false}` on both: a Select's trigger is a <button>, so
           inside a <label> it becomes that label's implicit control and
           clicking the caption opens the dropdown. See web/AGENTS.md. -->
      <Field label="Record type" labelled={false}>
        <Select
          value={attachType}
          options={entityTypes}
          onValueChange={pickType}
          ariaLabel="Record type"
          fullWidth
        />
      </Field>
      <!-- `warning` rather than `hint`: a hover popover is discoverable, not
           seen, and a short pool is the reason the record you are looking for
           is not in the list. -->
      <Field label="Record" warning={poolNotice} labelled={false}>
        <Select
          value={attachId}
          options={attachChoices}
          onValueChange={(v) => (attachId = v)}
          placeholder={attachChoices.length ? 'Choose a record…' : 'Nothing left to attach to'}
          disabled={attachChoices.length === 0}
          ariaLabel="Record"
          fullWidth
        />
      </Field>
      {#if attachError}
        <div class="banner error">{attachError}</div>
      {/if}
    </div>
    {#snippet footer()}
      <Button onclick={() => (attachForId = null)}>Cancel</Button>
      <Button
        variant="primary"
        onclick={attach}
        disabled={!attachId || (attachFor !== null && busy.has(attachFor.id))}
      >
        Attach
      </Button>
    {/snippet}
  </Modal>
{/if}

<ConfirmDialog
  open={deleteTarget !== null}
  title="Delete document"
  message={deleteTarget
    ? `Are you sure you want to delete "${documentName(deleteTarget)}"? ` +
      `This cannot be undone. ${attachedRecordsWarning(links(deleteTarget).length)}`
    : ''}
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={confirmDelete}
  onCancel={() => (deleteTargetId = null)}
/>

<style>
  .header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  h1 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: 600;
  }

  .sub {
    margin: var(--space-1) 0 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .name {
    color: var(--accent-blue);
    text-decoration: none;
    font-weight: 500;
    /* A scanner-generated filename must wrap rather than widen the column. */
    overflow-wrap: anywhere;
  }

  .name:hover {
    text-decoration: underline;
  }

  .source {
    margin: var(--space-1) 0 0;
    font-size: var(--text-2xs);
    color: var(--text-dim);
    line-height: 1.4;
  }

  .notes {
    margin: var(--space-1) 0 0;
    font-size: var(--text-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .nowrap {
    white-space: nowrap;
  }

  .actions {
    width: 1%;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
  }

  .link-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    padding: var(--space-1) var(--space-1) var(--space-1) var(--space-2);
    max-width: 22rem;
  }

  .link-kind {
    color: var(--text-dim);
    font-size: var(--text-2xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .link-label {
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .detach,
  .attach {
    font: inherit;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-dim);
    padding: 0 var(--space-1);
    border-radius: var(--radius-pill);
  }

  .detach:hover:not(:disabled) {
    color: var(--status-danger-fg);
  }

  .attach {
    font-size: var(--text-xs);
    border: 1px dashed var(--border-default);
    padding: var(--space-1) var(--space-2);
  }

  .attach:hover:not(:disabled) {
    border-color: var(--accent-blue);
    color: var(--text-muted);
  }

  .detach:disabled,
  .attach:disabled {
    cursor: default;
    opacity: 0.5;
  }

  .busy {
    opacity: 0.6;
  }

  /* A reload after an edit dims the table rather than replacing it, so the
     rows stay in place and readable while the new ones arrive. */
  .refreshing {
    opacity: 0.7;
  }

  .attach-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
</style>
