<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getBriefingArchive,
    deleteBriefingArchiveItem,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    briefingFilterName,
    briefingArchiveCount,
    briefingArchiveError,
    briefingsRefreshNonce,
  } from '$lib/stores/briefings';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Chip,
    Select,
    KebabMenu,
    ConfirmDialog,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from 'lucide-svelte';
  import { formatDateTime } from '$lib/dateFormat';

  let { children } = $props();

  const PAGE = 20;

  let items = $state<BriefingArchiveItem[]>([]);
  let total = $state(0);
  let names = $state<string[]>([]);
  let offset = $state(0);
  let sidebarOpen = $state(false);
  let loadingMore = $state(false);

  // The archived briefing pending a delete confirmation (null = no dialog).
  let deleteTarget = $state<BriefingArchiveItem | null>(null);
  let deleteError = $state('');

  // Briefing-name filter, auto-populated from the archive's distinct names.
  let nameOptions = $derived([
    { value: '', label: 'All' },
    ...names.map((n) => ({ value: n, label: n })),
  ]);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/briefings/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/briefings`);
    else goto(`${base}/briefings/settings`);
  }

  async function load(reset = true) {
    loadingMore = !reset;
    try {
      const params: Record<string, string> = {
        limit: String(PAGE),
        offset: String(offset),
      };
      if ($briefingFilterName) params.briefing_name = $briefingFilterName;
      const resp = await getBriefingArchive(params);
      items = reset ? resp.items : [...items, ...resp.items];
      total = resp.total;
      names = resp.briefing_names;
      briefingArchiveCount.set(items.length);
      briefingArchiveError.set(null);
      // Seed a selection so the reader has something to show.
      if (reset) {
        const stillPresent = items.some((i) => i.id === $selectedBriefingId);
        if (!stillPresent) selectedBriefingId.set(items[0]?.id ?? null);
      }
    } catch {
      // Published rather than swallowed. This used to read "the reader page
      // surfaces its own load errors", which is false for the only case that
      // reaches here: the reader fetches the *selected* briefing, a failed list
      // fetch leaves nothing selected, and its effect returns before its catch.
      // So the count below — zero items, indistinguishable from an empty
      // archive — was the only thing the reader had to go on, and it rendered
      // "No briefings yet" at a user who was offline with briefings configured.
      //
      // A fixed string rather than the thrown message, matching feeds, location
      // and money: offline throws `TypeError: Failed to fetch`, and putting that
      // on the pane is worse than saying plainly what did not happen.
      briefingArchiveCount.set(items.length);
      briefingArchiveError.set('Failed to load briefings');
    } finally {
      loadingMore = false;
    }
  }

  function pickName(name: string) {
    briefingFilterName.set(name);
    offset = 0;
    selectedBriefingId.set(null);
    void load();
  }

  function pickItem(id: number) {
    selectedBriefingId.set(id);
    sidebarOpen = false;
    if (onSettings) goto(`${base}/briefings`);
  }

  function loadMore() {
    offset += PAGE;
    void load(false);
  }

  async function performDelete() {
    const target = deleteTarget;
    if (!target) return;
    deleteTarget = null;
    deleteError = '';
    try {
      await deleteBriefingArchiveItem(target.id);
      const idx = items.findIndex((i) => i.id === target.id);
      const wasSelected = $selectedBriefingId === target.id;
      // Optimistic local removal — preserves any already-loaded older pages
      // instead of refetching just the current offset window.
      items = items.filter((i) => i.id !== target.id);
      total = Math.max(0, total - 1);
      briefingArchiveCount.set(items.length);
      if (wasSelected) {
        // Move the reader to a neighbour so it isn't stranded on a dead id.
        const next = items[idx] ?? items[idx - 1] ?? null;
        selectedBriefingId.set(next ? next.id : null);
      }
    } catch (e) {
      deleteError = e instanceof Error ? e.message : 'Failed to delete briefing';
      // Reconcile from the server so the list reflects reality.
      offset = 0;
      void load();
    }
  }

  const fmtDate = (iso: string) => formatDateTime(iso, { dateStyle: 'medium', timeStyle: 'short' });

  // Refresh the archive when the settings page reports a schedule change.
  let lastNonce = 0;
  $effect(() => {
    const n = $briefingsRefreshNonce;
    if (n !== lastNonce) {
      lastNonce = n;
      offset = 0;
      void load();
    }
  });

  onMount(() => load());
</script>

<!-- insetBottom only on the settings sub-route. The reader is a card-colored
     surface, so letting the shell hold the bottom inset stops that fill one
     home-indicator's height short of the screen edge and shows a band of the
     shell background under it; the reader pads itself instead (same split the
     chat composer makes). Settings is an ordinary scrolling form with no
     full-bleed surface, so the shell inset is right there. -->
<AppShell insetBottom={onSettings}>
  {#snippet header()}
    <ShellHeader
      title="Briefings"
      onTitleClick={onSettings ? undefined : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open archive"
    >
      {#snippet leading()}
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Archive"
            count={total}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
      {/snippet}
      {#snippet nav()}
        {#if !onSettings && names.length > 1}
          <Select
            value={$briefingFilterName}
            options={nameOptions}
            onValueChange={(v) => pickName(v)}
            ariaLabel="Filter by briefing"
          />
        {/if}
      {/snippet}
      {#snippet tools()}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Briefing settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#if !onSettings}
      <Sidebar
        title="Archive"
        count={total}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        {#if deleteError}
          <p class="sidebar-error">{deleteError}</p>
        {/if}
        {#if items.length === 0 && $briefingArchiveError}
          <p class="sidebar-error">{$briefingArchiveError}</p>
        {:else if items.length === 0}
          <p class="sidebar-empty">No briefings yet.</p>
        {:else}
          {#each items as item (item.id)}
            <div class="list-row archive-row" class:active={item.id === $selectedBriefingId}>
              <button class="archive-btn" type="button" onclick={() => pickItem(item.id)}>
                <span class="archive-subject">{item.subject || item.briefing_name}</span>
                <span class="archive-date">{fmtDate(item.generated_at)}</span>
              </button>
              <KebabMenu
                ariaLabel="Briefing actions"
                items={[{ label: 'Delete', danger: true, onSelect: () => (deleteTarget = item) }]}
              />
            </div>
          {/each}
          {#if items.length < total}
            <button class="load-more" type="button" onclick={loadMore} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : 'Load older'}
            </button>
          {/if}
        {/if}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

{#if deleteTarget}
  <ConfirmDialog
    open={true}
    title="Delete briefing"
    message={`Permanently remove the archived briefing "${deleteTarget.subject || deleteTarget.briefing_name}" from ${fmtDate(deleteTarget.generated_at)}? This cannot be undone.`}
    confirmLabel="Delete"
    onConfirm={performDelete}
    onCancel={() => (deleteTarget = null)}
  />
{/if}

<style>
  /* Row = clickable title button (flex:1) + a kebab sibling. A KebabMenu is
     itself a <button>, so it can't be nested inside .archive-btn; the row's
     layout and hover come from `.sidebar .list-row` in lib/styles/sidebar.css,
     shared with the chat sidebar. */
  .archive-btn {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1;
    min-width: 0;
    text-align: left;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .archive-row.active .archive-btn {
    color: var(--text-primary);
  }

  .archive-subject {
    font-size: var(--text-sm);
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .archive-date {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .load-more {
    width: 100%;
    margin-top: var(--space-2);
    padding: var(--space-2);
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
  }

  .load-more:hover:not(:disabled) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .sidebar-empty {
    padding: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .sidebar-error {
    padding: var(--space-2) var(--space-2);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
