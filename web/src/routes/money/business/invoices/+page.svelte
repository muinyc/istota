<script lang="ts">
  import {
    getInvoices,
    getInvoiceDetails,
    markInvoicePaid,
    markInvoicePending,
    invoicePdfUrl,
    type InvoiceRow,
    type InvoiceDetailItem,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { KebabMenu, type KebabItem } from '$lib/components/ui';
  import { formatDate } from '$lib/dateFormat';
  import { formatDecimal as formatAmount } from '$lib/format';

  let invoices: InvoiceRow[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let invoiceCount = $state(0);
  let outstandingCount = $state(0);
  let sortAsc = $state(false);
  // Invoice number currently running an action (disables its menu items).
  let busyInvoice = $state('');

  async function handleMarkPaid(inv: InvoiceRow) {
    busyInvoice = inv.invoice_number;
    try {
      await markInvoicePaid(inv.invoice_number);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark invoice paid';
    } finally {
      busyInvoice = '';
    }
  }

  async function handleMarkPending(inv: InvoiceRow) {
    busyInvoice = inv.invoice_number;
    try {
      await markInvoicePending(inv.invoice_number);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark invoice pending';
    } finally {
      busyInvoice = '';
    }
  }

  function handleDownloadPdf(inv: InvoiceRow) {
    window.open(invoicePdfUrl(inv.invoice_number), '_blank');
  }

  function menuItems(inv: InvoiceRow): KebabItem[] {
    const busy = busyInvoice === inv.invoice_number;
    const items: KebabItem[] = [];
    if (inv.status === 'paid') {
      items.push({ label: 'Mark pending', onSelect: () => handleMarkPending(inv), disabled: busy });
    } else {
      items.push({ label: 'Mark paid', onSelect: () => handleMarkPaid(inv), disabled: busy });
    }
    items.push({ label: 'Download PDF', onSelect: () => handleDownloadPdf(inv) });
    return items;
  }

  let expandedKeys = $state(new Set<string>());
  let detailsCache = $state(new Map<string, InvoiceDetailItem[]>());
  let detailsLoading = $state(new Set<string>());

  async function toggleExpand(inv: InvoiceRow) {
    const key = inv.invoice_number;
    if (expandedKeys.has(key)) {
      const next = new Set(expandedKeys);
      next.delete(key);
      expandedKeys = next;
      return;
    }

    const nextExpanded = new Set(expandedKeys);
    nextExpanded.add(key);
    expandedKeys = nextExpanded;

    if (!detailsCache.has(key)) {
      const nextLoading = new Set(detailsLoading);
      nextLoading.add(key);
      detailsLoading = nextLoading;
      try {
        const resp = await getInvoiceDetails(key);
        const nextCache = new Map(detailsCache);
        nextCache.set(key, resp.items);
        detailsCache = nextCache;
      } catch {
        // stay expanded but empty
      } finally {
        const nl = new Set(detailsLoading);
        nl.delete(key);
        detailsLoading = nl;
      }
    }
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const resp = await getInvoices({ show_all: true });
      invoices = resp.invoices;
      invoiceCount = resp.invoice_count;
      outstandingCount = resp.outstanding_count;
      expandedKeys = new Set();
      detailsCache = new Map();
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load invoices';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    load();
  });

  let sorted = $derived.by(() => {
    const copy = [...invoices];
    copy.sort((a, b) => {
      const cmp = a.date.localeCompare(b.date);
      return sortAsc ? cmp : -cmp;
    });
    return copy;
  });

  function toggleSort() {
    sortAsc = !sortAsc;
  }

  function displayStatus(status: string): string {
    if (status === 'outstanding') return 'posted';
    return status;
  }

  function formatQty(value: number): string {
    if (value === Math.floor(value)) return String(value);
    return value.toFixed(2);
  }
</script>

<div class="invoices-content">
  <!-- Held back behind both whole-pane states, so the pane shows nothing but
       the centered message. The bar used to stay put (with only its count
       dropped) to stop the table shifting up and back down on a reload; that
       shift cannot happen now, because the table is not on screen while
       loading — bar and table arrive together. On error the count is also a
       lie: it reports 0 invoices of a list that failed to load. -->
  {#if !loading && !error}
    <div class="money-toolbar control-row">
      <span class="money-result-count"
        >{invoiceCount} invoices ({outstandingCount} outstanding)</span
      >
    </div>
  {/if}

  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else if invoices.length === 0}
    <div class="money-table-empty">No invoices found.</div>
  {:else}
    <div class="money-table">
      <div class="money-table-header">
        <span class="inv-number">Invoice</span>
        <span class="inv-client">Client</span>
        <button
          class="inv-date money-sortable"
          onclick={toggleSort}
          type="button"
          title="Sort by date"
        >
          Date <span class="money-sort-arrow">{sortAsc ? '\u25B2' : '\u25BC'}</span>
        </button>
        <span class="inv-status money-status">Status</span>
        <span class="inv-amount money-amount">Amount</span>
        <span class="money-kebab-spacer"></span>
      </div>
      {#each sorted as inv (inv.invoice_number)}
        {@const key = inv.invoice_number}
        {@const isExpanded = expandedKeys.has(key)}
        <div
          class="money-table-row"
          class:expanded={isExpanded}
          role="button"
          tabindex="0"
          aria-expanded={isExpanded}
          onclick={() => toggleExpand(inv)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              toggleExpand(inv);
            }
          }}
        >
          <span
            class="inv-number"
            class:status-paid={inv.status === 'paid'}
            class:status-posted={inv.status === 'outstanding'}
            class:status-draft={inv.status === 'draft'}>{inv.invoice_number}</span
          >
          <span class="inv-client">{inv.client}</span>
          <span class="inv-date">{formatDate(inv.date)}</span>
          <span
            class="inv-status money-status"
            class:status-paid={inv.status === 'paid'}
            class:status-posted={inv.status === 'outstanding'}
            class:status-draft={inv.status === 'draft'}>{displayStatus(inv.status)}</span
          >
          <span class="inv-amount money-amount">${formatAmount(inv.total)}</span>
          <KebabMenu items={menuItems(inv)} />
        </div>
        {#if isExpanded}
          <div class="inv-details">
            {#if detailsLoading.has(key)}
              <div class="detail-row"><span class="detail-desc">Loading...</span></div>
            {:else if detailsCache.has(key)}
              {#each detailsCache.get(key) ?? [] as item}
                <div class="detail-row">
                  <span class="detail-desc">
                    {item.description}
                    {#if item.detail}
                      <span class="detail-note">{item.detail}</span>
                    {/if}
                  </span>
                  <span class="detail-qty"
                    >{formatQty(item.quantity)} &times; ${formatAmount(item.rate)}</span
                  >
                  {#if item.discount > 0}
                    <span class="detail-discount">-${formatAmount(item.discount)}</span>
                  {/if}
                  <span class="detail-amount">${formatAmount(item.amount)}</span>
                </div>
              {/each}
            {/if}
          </div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  .invoices-content {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  /* Columns only — the toolbar/list/header/row/chip shell is shared, in
     routes/money/+layout.svelte. */

  .inv-number {
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
    font-size: var(--text-xs);
    color: var(--text-muted);
    flex-shrink: 0;
    min-width: 6.5rem;
    box-sizing: border-box;
  }

  .inv-client {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-primary);
    font-weight: 500;
  }

  /* Fixed slot: the client column absorbs the slack, so a variable-width date
     (or status) would slide the whole right side of the row per row. */
  .inv-date {
    color: var(--text-dim);
    white-space: nowrap;
    flex-shrink: 0;
    font-size: var(--text-xs);
    min-width: 6.5rem;
  }

  /* Snug around the longest label ("posted") in caps: a slot much wider than
     the text turns the chip into a mostly-empty capsule. */
  .inv-status {
    min-width: 4rem;
  }

  .inv-amount {
    min-width: 5.5rem;
  }

  .inv-details {
    padding: 0.15rem var(--space-3) var(--space-2) var(--space-8);
    background: var(--surface-card);
    border-radius: 0 0 0.25rem 0.25rem;
    margin-top: -0.15rem;
  }

  .detail-row {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    padding: 0.15rem 0;
    font-size: var(--text-xs);
  }

  .detail-desc {
    flex: 1;
    min-width: 0;
    color: var(--text-secondary);
  }

  .detail-note {
    color: var(--text-dim);
    margin-left: var(--space-1);
  }

  .detail-qty {
    color: var(--text-dim);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .detail-discount {
    color: var(--money-expense);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .detail-amount {
    margin-left: auto;
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    color: var(--text-secondary);
    min-width: 4rem;
  }

  @media (max-width: 640px) {
    .inv-date {
      display: none;
    }
    /* The shared shell hides the status chip at this width, so the invoice
       number carries the status colors instead — same tokens as the chip. */
    .inv-number {
      min-width: 5.25rem;
      padding: 0.1rem var(--space-2);
      border-radius: var(--radius-pill);
      text-align: center;
    }
    .inv-number.status-posted {
      color: var(--status-warn-fg);
      background: var(--status-warn-bg);
    }
    .inv-number.status-paid {
      color: var(--status-success-fg);
      background: var(--status-success-bg);
    }
    .inv-number.status-draft {
      color: var(--text-muted);
      background: var(--surface-badge);
    }
    .inv-amount {
      min-width: 4rem;
    }
    .inv-details {
      padding-left: var(--space-3);
    }
    .detail-qty {
      display: none;
    }
  }
</style>
