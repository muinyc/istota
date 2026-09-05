<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { AppShell, ShellHeader, HeaderNav, Chip } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from 'lucide-svelte';

  let { children } = $props();

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  function isExactActive(path: string): boolean {
    const current = page.url.pathname;
    return current === `${base}${path}` || current === `${base}${path}/`;
  }

  const navItems = $derived([
    {
      href: `${base}/health`,
      label: 'Stats',
      active: isExactActive('/health') || isActive('/health/stats'),
    },
    { href: `${base}/health/history`, label: 'History', active: isActive('/health/history') },
    {
      href: `${base}/health/immunizations`,
      label: 'Immunizations',
      active: isActive('/health/immunizations'),
    },
    { href: `${base}/health/bloodwork`, label: 'Bloodwork', active: isActive('/health/bloodwork') },
    {
      href: `${base}/health/documents`,
      label: 'Documents',
      active: isActive('/health/documents'),
    },
  ]);

  const onSettings = $derived(page.url.pathname.startsWith(`${base}/health/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/health`);
    else goto(`${base}/health/settings`);
  }
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="Health">
      {#snippet nav()}
        <HeaderNav items={navItems} ariaLabel="Health section" />
      {/snippet}
      {#snippet tools()}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Health settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  <div class="content-frame health-frame">
    {@render children()}
  </div>
</AppShell>

<style>
  /* The column geometry — the cap, the centring and the growing flex column —
	   comes from `.content-frame` (app.css), which health shares with money's
	   portfolio. What is left here is health's own inset and the module shell
	   below it. The class stays on a real element in this file rather than
	   moving into a component, because the `:global()` rules under it are
	   pruned the moment Svelte can no longer see their subject in the markup. */
  .health-frame {
    padding: var(--space-4);
  }

  /* Shared card surface for every health page — the module's counterpart to
	   the global .card-grid layout primitive (app.css). Scoped to .health-frame
	   (not global) because `.card` means other things elsewhere in the app;
	   this mirrors how settings.css scopes `.settings .card`. Pages set their
	   own padding/layout on `.card`; this owns surface + border + radius. Add
	   `class="card interactive"` for a clickable card (cursor + hover border). */
  /* Record-table shell, mirroring .money-table* in routes/money/+layout.svelte.
     Six pages had their own copy of this — table.grid three times byte for
     byte, plus grid-tbl, .biomarker-table and .review-table — differing only
     in cell padding and header weight. `--dense` is that difference kept as an
     explicit choice: the review and biomarker editors are input-bearing
     tables, where tighter cells are deliberate rather than drift. */
  .health-frame :global(.table-scroll) {
    width: 100%;
    overflow-x: auto;
    /* Momentum scrolling on iOS. Exactly one of the four copies had it, so a
       table that scrolled sideways felt native on one page and not the rest. */
    -webkit-overflow-scrolling: touch;
  }

  .health-frame :global(table.grid) {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .health-frame :global(table.grid th),
  .health-frame :global(table.grid td) {
    text-align: left;
    padding: var(--space-2) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    vertical-align: middle;
  }

  .health-frame :global(table.grid th) {
    color: var(--text-dim);
    font-weight: 500;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .health-frame :global(table.grid--dense th),
  .health-frame :global(table.grid--dense td) {
    padding: var(--space-1) var(--space-2);
  }

  /* The .msg boxes this replaced each carried their own margin-bottom —
     0.75rem on six pages, 0.5rem on one. Restored once for the module rather
     than as twelve page rules re-forking the primitive; the majority value
     wins, so the bloodwork toolbar's notice gains 0.25rem. */
  .health-frame :global(.banner) {
    margin-bottom: var(--space-3);
  }

  .health-frame :global(.card) {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    /* design-lint-allow: --card-padding is the documented hook a consuming page
       sets to override this padding without raising specificity; it is defined
       by the caller, so it is deliberately absent from the token roster. */
    padding: var(--card-padding, 0.75rem 0.9rem);
    box-sizing: border-box;
    min-width: 0;
    text-decoration: none;
  }
  .health-frame :global(.card.interactive) {
    cursor: pointer;
    color: var(--text-primary);
    transition: border-color var(--transition-fast);
  }
  .health-frame :global(.card.interactive:hover) {
    border-color: var(--border-hover);
  }

  @media (max-width: 768px) {
    .health-frame {
      /* Match ShellHeader's mobile padding so the page heading lines
			   up with the subnav title above it. */
      padding: var(--space-2) var(--space-3);
    }
  }
</style>
