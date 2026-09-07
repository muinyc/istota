<script lang="ts">
  import { base } from '$app/paths';
  import { renderMarkdown } from '$lib/markdown';
  import { getBriefingArchiveItem, type BriefingArchiveItem } from '$lib/api';
  import {
    selectedBriefingId,
    briefingArchiveCount,
    briefingArchiveError,
  } from '$lib/stores/briefings';
  import { formatDateTime } from '$lib/dateFormat';

  let current = $state<BriefingArchiveItem | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let loadedId = $state<number | null>(null);

  const fmtDate = (iso: string) => formatDateTime(iso, { dateStyle: 'medium', timeStyle: 'short' });

  // Fetch the full briefing (with body) whenever the selection changes.
  $effect(() => {
    const id = $selectedBriefingId;
    if (id == null) {
      current = null;
      loadedId = null;
      return;
    }
    if (id === loadedId) return;
    loadedId = id;
    loading = true;
    error = null;
    getBriefingArchiveItem(id)
      .then((item) => {
        // Guard against an out-of-order response after a fast re-select.
        if ($selectedBriefingId === id) current = item;
      })
      .catch((e) => {
        error = e instanceof Error ? e.message : 'Failed to load briefing';
      })
      .finally(() => {
        loading = false;
      });
  });
</script>

<svelte:head>
  <title>Briefings</title>
</svelte:head>

<div class="reader">
  {#if error}
    <p class="center-msg error">{error}</p>
  {:else if current}
    <article class="briefing">
      <header class="briefing-head">
        <h1>{current.subject || current.briefing_name}</h1>
        <p class="meta">
          {fmtDate(current.generated_at)}
          {#if current.delivered_to?.length}
            · delivered to {current.delivered_to.join(', ')}
          {/if}
        </p>
      </header>
      <!-- eslint-disable-next-line svelte/no-at-html-tags -->
      <!-- `markdown` is the shared rendered-prose block in app.css (same class
           chat's message body uses). The local rules below layer on top of it —
           Svelte's scoping class outranks the global ones — and only cover where
           a reading surface genuinely differs from a chat bubble (flush lists,
           larger headings). -->
      <div class="body markdown">{@html renderMarkdown(current.body_md ?? '')}</div>
    </article>
  {:else if loading || $briefingArchiveCount === null}
    <p class="center-msg">Loading…</p>
  {:else if $briefingArchiveError}
    <!-- Ahead of the empty state and behind everything else: a briefing already
         on screen stays readable through a failed refresh, but an archive that
         could not be fetched must not read as an archive with nothing in it. -->
    <p class="center-msg error">{$briefingArchiveError}</p>
  {:else}
    <div class="empty-state">
      <h1>No briefings yet</h1>
      <p class="muted">
        Once a scheduled briefing runs it will appear here. Set up the schedule and content blocks
        in <a href="{base}/briefings/settings">settings</a>.
      </p>
    </div>
  {/if}
</div>

<style>
  .reader {
    /* flex-basis: auto (not 0) so the box grows with its content — otherwise
		   the box is pinned to the scroll viewport height and long briefings
		   overflow *past* padding-bottom, losing the bottom gap at scroll-end.
		   flex-grow keeps short content (and the empty state) filling the area. */
    flex: 1 0 auto;
    /* A column so the whole-pane states (`.center-msg`, the only child in the
		   loading and error branches) can center themselves in the pane with
		   `flex: 1`. The article and the empty state keep their natural height at
		   the top of the column. */
    display: flex;
    flex-direction: column;
    padding: var(--space-6) var(--space-8);
    /* The shell hands this route the bottom safe area (insetBottom={onSettings}),
		   so the fill runs to the screen edge and the text clears the home
		   indicator — rather than the fill stopping short and leaving a band of the
		   shell background below the card. Inert where the inset is 0. */
    padding-bottom: max(1.5rem, var(--safe-bottom));
    /* Reading surface, shared with the chat transcript: card-colored in dark,
	     pure white in light. */
    background: var(--surface-reading);
  }

  /* Phone: the desktop 2rem inline gutter costs a fifth of a narrow screen's
	   width, and it left the briefing text inset well past every other landmark
	   on the page. Drop to the 0.75rem the app bar and ShellHeader use, so the
	   body copy lines up with the titles above it. */
  @media (max-width: 768px) {
    .reader {
      padding: var(--space-4) var(--space-3);
      padding-bottom: max(1rem, var(--safe-bottom));
    }
  }

  .briefing {
    max-width: 46rem;
  }

  /* Match the chat message body: same font size (configurable later) and
	   line height so the reader reads at one scale with the rest of the app. */
  .body {
    font-size: var(--text-base);
    line-height: 1.5;
  }

  /* Bullet/numbered lists sit flush with paragraphs — no browser default
	   indent; the marker aligns with the left edge of the surrounding text. */
  .body :global(ul),
  .body :global(ol) {
    margin: 0 0 var(--space-4);
    padding-left: 0;
    list-style-position: inside;
  }

  .body :global(li) {
    margin: var(--space-1) 0;
  }

  .briefing-head h1 {
    margin: 0 0 var(--space-1);
    font-size: 1.25rem;
  }

  .meta {
    margin: 0 0 1.25rem;
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .body :global(h1),
  .body :global(h2) {
    font-size: 1.05rem;
    margin-top: var(--space-6);
  }

  .body :global(table) {
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .body :global(th),
  .body :global(td) {
    border: 1px solid var(--border-subtle);
    padding: var(--space-1) var(--space-2);
    text-align: left;
  }

  /* Sized from the type scale, like the briefing body next door: the h1 was
	   1.1rem and the paragraph inherited the 1rem body default, so the state a
	   user meets before their first briefing rendered a step larger than
	   everything around it. Weight is what still marks the heading. */
  .empty-state {
    max-width: 32rem;
    font-size: var(--text-base);
  }

  .empty-state h1 {
    font-size: var(--text-base);
    margin: 0 0 var(--space-2);
  }
</style>
