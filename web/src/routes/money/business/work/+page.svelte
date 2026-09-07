<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    getWorkEntries,
    createWorkEntry,
    updateWorkEntry,
    deleteWorkEntry,
    getClients,
    getBusinessSettings,
    ApiError,
    type WorkEntryRow,
    type WorkTotals,
    type WorkStatusFilter,
    type ClientRow,
    type ServiceRow,
  } from '$lib/money/api';
  import type { WorkEntrySavePayload } from '$lib/money/workEntryPayload';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import {
    Button,
    ConfirmDialog,
    KebabMenu,
    NoticeBanner,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import WorkEntryForm from '$lib/components/money/WorkEntryForm.svelte';
  import { formatDate } from '$lib/dateFormat';
  import { formatDecimal as formatAmount } from '$lib/format';

  let entries: WorkEntryRow[] = $state([]);
  let totals: WorkTotals = $state({
    uninvoiced_count: 0,
    uninvoiced_amount: 0,
    invoiced_count: 0,
    paid_count: 0,
  });
  let clients: ClientRow[] = $state([]);
  let services: ServiceRow[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let conflict = $state('');

  // Uninvoiced first: those are the entries you can still change, and the
  // ones that determine the next invoice.
  let statusFilter: WorkStatusFilter = $state('uninvoiced');
  let clientFilter = $state('');
  let periodFilter = $state('');
  let sortAsc = $state(false);

  // uid of the entry currently running an action (disables its menu items).
  let busyUid = $state('');

  let formOpen = $state(false);
  let editing: WorkEntryRow | null = $state(null);
  let formError = $state('');
  let saving = $state(false);

  let confirmOpen = $state(false);
  let pendingDelete: WorkEntryRow | null = $state(null);

  const statusOptions: SelectOption[] = [
    { value: 'uninvoiced', label: 'Uninvoiced' },
    { value: 'invoiced', label: 'Invoiced' },
    { value: 'paid', label: 'Paid' },
    { value: 'all', label: 'All' },
  ];

  const clientFilterOptions = $derived.by<SelectOption[]>(() => [
    { value: '', label: 'All clients' },
    ...clients.map((c) => ({ value: c.key, label: c.name || c.key })),
  ]);

  // Years accumulate across loads and never shrink: deriving them from the
  // current (already filtered) rows would delete the option you just picked.
  let knownYears: string[] = $state([]);

  // "All" rather than "All time", matching the year filter on transactions,
  // accounts and reports — and worth ~26px, which is most of what this toolbar
  // was over budget by on a phone.
  const periodOptions = $derived.by<SelectOption[]>(() => [
    { value: '', label: 'All' },
    ...knownYears.map((y) => ({ value: y, label: y })),
  ]);

  function rememberYears(rows: WorkEntryRow[]) {
    const years = new Set(knownYears);
    for (const row of rows) years.add(row.date.slice(0, 4));
    knownYears = [...years].sort().reverse();
  }

  // Four filters change independently and each fires a load; without a
  // sequence guard a slow earlier response lands after a newer one and shows
  // rows for a filter you already left.
  let loadSeq = 0;

  async function load() {
    const seq = ++loadSeq;
    loading = true;
    error = '';
    try {
      const resp = await getWorkEntries({
        client: clientFilter || undefined,
        period: periodFilter || undefined,
        status: statusFilter,
      });
      if (seq !== loadSeq) return;
      entries = resp.entries;
      totals = resp.totals;
      rememberYears(resp.entries);
    } catch (e) {
      if (seq !== loadSeq) return;
      error = e instanceof Error ? e.message : 'Failed to load work entries';
    } finally {
      if (seq === loadSeq) loading = false;
    }
  }

  async function loadConfig() {
    knownYears = [];
    // Best-effort: the list is still usable without client/service names.
    try {
      const [clientResp, settingsResp] = await Promise.all([getClients(), getBusinessSettings()]);
      clients = clientResp.clients;
      services = settingsResp.services;
    } catch {
      clients = [];
      services = [];
    }
  }

  // Client/service names only change with the ledger, so they load separately
  // from the list, which also reloads on every filter change.
  $effect(() => {
    $selectedLedger;
    loadConfig();
  });

  $effect(() => {
    $selectedLedger;
    statusFilter;
    clientFilter;
    periodFilter;
    load();
  });

  function openAdd() {
    editing = null;
    formError = '';
    formOpen = true;
  }

  function openEdit(entry: WorkEntryRow) {
    editing = entry;
    formError = '';
    formOpen = true;
  }

  function closeForm() {
    formOpen = false;
    editing = null;
    formError = '';
  }

  /** A 409 means the agent (or another tab) got there first — offer a reload. */
  function isConflict(e: unknown): boolean {
    return e instanceof ApiError && e.status === 409;
  }

  async function handleSave(data: WorkEntrySavePayload) {
    saving = true;
    formError = '';
    const target = editing;
    try {
      if (target) {
        await updateWorkEntry(target.uid, { ...data, etag: target.etag });
      } else {
        await createWorkEntry(data);
      }
      closeForm();
      await load();
    } catch (e) {
      if (isConflict(e)) {
        closeForm();
        conflict = 'This entry was changed elsewhere — your edit was not saved.';
      } else {
        formError = e instanceof Error ? e.message : 'Failed to save work entry';
      }
    } finally {
      saving = false;
    }
  }

  function askDelete(entry: WorkEntryRow) {
    pendingDelete = entry;
    confirmOpen = true;
  }

  async function handleDelete() {
    const entry = pendingDelete;
    confirmOpen = false;
    pendingDelete = null;
    if (!entry) return;

    busyUid = entry.uid;
    try {
      await deleteWorkEntry(entry.uid, { etag: entry.etag });
      await load();
    } catch (e) {
      if (isConflict(e)) {
        conflict = 'This entry was changed elsewhere — it was not deleted.';
      } else {
        error = e instanceof Error ? e.message : 'Failed to delete work entry';
      }
    } finally {
      busyUid = '';
    }
  }

  function viewInvoice() {
    goto(`${base}/money/business/invoices`);
  }

  async function reloadFromConflict() {
    conflict = '';
    await load();
  }

  function menuItems(entry: WorkEntryRow): KebabItem[] {
    const busy = busyUid === entry.uid;
    // `editable` is the server's decision; invoice/uid only pick the reason.
    if (!entry.editable) {
      if (entry.invoice) {
        return [
          { label: 'Invoiced — void the invoice to edit', onSelect: () => {}, disabled: true },
          { label: `View invoice ${entry.invoice}`, onSelect: viewInvoice },
        ];
      }
      return [
        {
          label: 'No stable id — run `money work backfill-ids`',
          onSelect: () => {},
          disabled: true,
        },
      ];
    }
    return [
      { label: 'Edit', onSelect: () => openEdit(entry), disabled: busy },
      { label: 'Delete', onSelect: () => askDelete(entry), danger: true, disabled: busy },
    ];
  }

  const sorted = $derived.by(() => {
    const copy = [...entries];
    copy.sort((a, b) => {
      const cmp = a.date.localeCompare(b.date);
      return sortAsc ? cmp : -cmp;
    });
    return copy;
  });

  function toggleSort() {
    sortAsc = !sortAsc;
  }

  function statusOf(entry: WorkEntryRow): 'draft' | 'posted' | 'paid' {
    if (entry.paid_date) return 'paid';
    if (entry.invoice) return 'posted';
    return 'draft';
  }

  function statusLabel(entry: WorkEntryRow): string {
    if (entry.paid_date) return 'paid';
    if (entry.invoice) return 'invoiced';
    return 'uninvoiced';
  }

  function formatQty(entry: WorkEntryRow): string {
    if (entry.qty != null) {
      const unit = entry.service_type === 'days' ? 'd' : entry.service_type === 'hours' ? 'h' : '';
      return `${entry.qty}${unit}`;
    }
    if (entry.amount != null) return `$${formatAmount(entry.amount)}`;
    return '—';
  }

  function warningText(entry: WorkEntryRow): string {
    if (entry.warnings.includes('unknown_service')) {
      return `Service "${entry.service}" is not configured — this entry will be skipped at invoice time.`;
    }
    if (entry.warnings.includes('unknown_client')) {
      return `Client "${entry.client}" is not configured.`;
    }
    if (entry.warnings.includes('no_uid')) {
      return 'No stable id — not editable here until the backfill runs.';
    }
    return '';
  }
</script>

<div class="work-content">
  {#if conflict}
    <div class="money-notice-bar">
      <NoticeBanner title={conflict} variant="warn" />
      <Button variant="ghost" onclick={reloadFromConflict}>Reload</Button>
    </div>
  {/if}

  <!-- Held back behind both whole-pane states, so the pane shows nothing but the
       centered message rather than centering it in the space left below. On
       error the count is also a lie: it reports 0 of something that failed to
       load. -->
  {#if !loading && !error}
    <div class="money-toolbar control-row">
      <span class="money-result-count">
        {totals.uninvoiced_count} uninvoiced &middot; ${formatAmount(totals.uninvoiced_amount)}
      </span>
      <!-- Every width here is pinned, because the row does not wrap: a trigger
           sized to its own selection re-flows the whole row each time you use
           it, and on a phone that meant Add entry dropping to a line of its
           own. Status and period take the longest label they can ever hold
           ("Uninvoiced", a four-digit year). Client cannot be sized that way —
           its labels are user data of no bounded length — so it takes a width
           that reads most names rather than one that fits them all; raise it if
           yours are longer, at the cost of the widths below.

           Below ~400px the row runs out of room and the labels give ground
           instead of it breaking. Which one gives way is not the order you
           might expect: flex-shrink takes pixels in proportion to width, so
           Client loses the most, but Status is the first to ellipsis because
           it is the one carrying a label that nearly fills it. That is the
           right way round anyway — a half-read client name still says which
           client, where a clipped "Invoiced" and "Uninvoiced" are the same
           word. -->
      <div class="filters">
        <Select
          value={statusFilter}
          options={statusOptions}
          ariaLabel="Status filter"
          widthChars={9}
          onValueChange={(v) => (statusFilter = v as WorkStatusFilter)}
        />
        <Select
          bind:value={clientFilter}
          options={clientFilterOptions}
          ariaLabel="Client filter"
          widthChars={10}
        />
        <Select
          bind:value={periodFilter}
          options={periodOptions}
          ariaLabel="Period filter"
          widthChars={4}
        />
        <Button variant="primary" onclick={openAdd}>Add entry</Button>
      </div>
    </div>
  {/if}

  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else if entries.length === 0}
    <div class="money-table-empty">No work entries found.</div>
  {:else}
    <div class="money-table">
      <div class="money-table-header">
        <span class="work-index">#</span>
        <button
          class="work-date money-sortable"
          onclick={toggleSort}
          type="button"
          title="Sort by date"
        >
          Date <span class="money-sort-arrow">{sortAsc ? '▲' : '▼'}</span>
        </button>
        <span class="work-client">Client</span>
        <span class="work-service">Service</span>
        <span class="work-qty">Qty</span>
        <span class="work-status money-status">Status</span>
        <span class="work-amount money-amount">Amount</span>
        <span class="money-kebab-spacer"></span>
      </div>
      {#each sorted as entry (entry.uid || `${entry.date}-${entry.index}`)}
        {@const warning = warningText(entry)}
        <div class="money-table-row">
          <span class="work-index" title="Display index — shifts as entries are added">
            {entry.index ?? ''}
          </span>
          <span class="work-date">{formatDate(entry.date)}</span>
          <span class="work-client">{entry.client_name}</span>
          <span class="work-service">
            {entry.service_name}
            {#if entry.description}
              <span class="work-desc">{entry.description}</span>
            {/if}
          </span>
          <span class="work-qty">{formatQty(entry)}</span>
          <span
            class="work-status money-status"
            class:status-paid={statusOf(entry) === 'paid'}
            class:status-posted={statusOf(entry) === 'posted'}
            class:status-draft={statusOf(entry) === 'draft'}>{statusLabel(entry)}</span
          >
          <span
            class="work-amount money-amount"
            title={entry.invoice
              ? 'Computed at the current rate — the invoice is the record of what was billed.'
              : undefined}
          >
            {entry.computed_amount != null ? `$${formatAmount(entry.computed_amount)}` : '—'}
          </span>
          <KebabMenu items={menuItems(entry)} />
        </div>
        {#if warning}
          <div class="work-warning">{warning}</div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

{#if formOpen}
  <WorkEntryForm
    entry={editing}
    {clients}
    {services}
    onSave={handleSave}
    onCancel={closeForm}
    error={formError}
    {saving}
  />
{/if}

<ConfirmDialog
  bind:open={confirmOpen}
  title="Delete work entry"
  message={pendingDelete
    ? `Are you sure you want to delete the ${pendingDelete.service_name} entry for ${pendingDelete.client_name} on ${formatDate(pendingDelete.date)}? This cannot be undone.`
    : ''}
  confirmLabel="Delete"
  onConfirm={handleDelete}
  onCancel={() => (pendingDelete = null)}
/>

<style>
  .work-content {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  /* One line, always. Wrapping put Add entry on a row by itself on a phone,
     which read as a second toolbar; the filters truncate instead (they are
     pinned to a `ch` width and carry an ellipsis). min-width lets this shrink
     below its own content as a toolbar item, or the truncation never gets a
     chance to happen. */
  .filters {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: nowrap;
    min-width: 0;
  }

  /* The one thing that does not give ground: a button truncated to "Add e…" is
     not a button, and it is the row's only action. */
  .filters :global(.btn) {
    flex-shrink: 0;
  }

  /* Columns only — the toolbar/list/header/row/chip shell is shared, in
     routes/money/+layout.svelte. */

  /* Left-aligned so the first character sits on the same edge as the invoice
     number one tab over — right-aligning it in the slot floated the whole
     table ~1.3rem inward. The fixed slot still keeps Date from shifting
     between one- and two-digit indices. */
  .work-index {
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
    font-size: var(--text-xs);
    color: var(--text-dim);
    flex-shrink: 0;
    min-width: 1.75rem;
  }

  .work-date {
    color: var(--text-dim);
    white-space: nowrap;
    flex-shrink: 0;
    font-size: var(--text-xs);
    min-width: 6.5rem;
  }

  .work-client {
    flex: 0 0 8rem;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-primary);
    font-weight: 500;
  }

  .work-service {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-secondary);
  }

  .work-desc {
    color: var(--text-dim);
    margin-left: var(--space-2);
    font-size: var(--text-xs);
  }

  .work-qty {
    color: var(--text-dim);
    white-space: nowrap;
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    min-width: 3rem;
    text-align: right;
  }

  /* Fits the longest label ("uninvoiced") in caps — if the chip outgrows the
     slot it starts shifting the Amount column per row again. */
  .work-status {
    min-width: 5rem;
  }

  .work-amount {
    min-width: 5.5rem;
  }

  /* Hangs under the row's first text column (index + gap). */
  .work-warning {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    padding: 0 var(--space-3) var(--space-2) 3.25rem;
  }

  @media (max-width: 640px) {
    .work-index,
    .work-qty {
      display: none;
    }
    .work-date {
      min-width: 4.5rem;
    }
    .work-client {
      flex: 0 0 5rem;
    }
    .work-amount {
      min-width: 4rem;
    }
    .work-warning {
      padding-left: var(--space-3);
    }
  }
</style>
