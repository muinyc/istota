<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import {
    getImmunizationCoverage,
    getImmunizationExplainer,
    listImmunizations,
    listImmunizationRefs,
    type CoverageEntry,
    type Immunization,
    type ImmunizationExplainer,
    type ImmunizationRef,
    type ImmunizationStatus,
  } from '$lib/api';
  import { Badge, Button, KebabMenu } from '$lib/components/ui';
  import { immunizationStatusLabel, immunizationStatusVariant } from '$lib/health/status';
  import { formatDate as formatIsoDate } from '$lib/dateFormat';

  let name = $derived(page.url.searchParams.get('name') || '');
  let loading = $state(true);
  let error = $state('');
  let ref: ImmunizationRef | null = $state(null);
  let entry: CoverageEntry | null = $state(null);
  let history: Immunization[] = $state([]);
  let explainer: ImmunizationExplainer | null = $state(null);
  let explainerLoading = $state(false);

  let loadToken = 0;

  async function load() {
    if (!name) return;
    const token = ++loadToken;
    loading = true;
    error = '';
    explainer = null;
    try {
      const [refResp, cov, hist] = await Promise.all([
        listImmunizationRefs(),
        getImmunizationCoverage(),
        listImmunizations({ name, limit: 200 }),
      ]);
      if (token !== loadToken) return;
      ref = refResp.refs.find((r) => r.name === name) ?? null;
      entry = cov.coverage.find((c) => c.name === name) ?? null;
      history = hist.immunizations;
    } catch (e) {
      if (token !== loadToken) return;
      error = e instanceof Error ? e.message : 'Failed to load';
    } finally {
      if (token === loadToken) loading = false;
    }

    if (entry && token === loadToken) {
      explainerLoading = true;
      try {
        const next = await getImmunizationExplainer(name);
        if (token === loadToken) explainer = next;
      } catch {
        // Leave whatever was last successfully loaded in place.
      } finally {
        if (token === loadToken) explainerLoading = false;
      }
    }
  }

  const formatDate = (iso: string | null) => formatIsoDate(iso, { empty: '—' });

  $effect(() => {
    if (name) load();
  });
</script>

{#if !loading && !error}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <h1>{ref?.display_name || name}</h1>
    <Button href="{base}/health/immunizations">Back</Button>
  </div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if !ref}
  <div class="empty">
    Unknown vaccine "{name}". It may not be in the canonical reference list.
  </div>
{:else}
  <section class="card coverage-card">
    {#if entry}
      <div class="status-row">
        <Badge variant={immunizationStatusVariant(entry.status)}
          >{immunizationStatusLabel(entry.status)}</Badge
        >
        <span class="caption">{ref.category} · {ref.schedule}</span>
      </div>
      <dl class="grid-stats">
        <div>
          <dt>Last given</dt>
          <dd>{formatDate(entry.last_given)}</dd>
        </div>
        <div>
          <dt>Doses recorded</dt>
          <dd>{entry.dose_count}</dd>
        </div>
        <div>
          <dt>Next due</dt>
          <dd>{formatDate(entry.next_due)}</dd>
        </div>
        {#if entry.days_until_due !== null}
          <div>
            <dt>{entry.days_until_due < 0 ? 'Days overdue' : 'Days until due'}</dt>
            <dd>{Math.abs(entry.days_until_due)}</dd>
          </div>
        {/if}
      </dl>
    {/if}
    {#if ref.description}
      <p class="description">{ref.description}</p>
    {/if}
    {#if ref.typical_age_range}
      <p class="caption">Typical age range: {ref.typical_age_range}</p>
    {/if}
  </section>

  {#if explainerLoading}
    <section class="card explainer placeholder">
      <h2 class="micro-label">About this vaccine</h2>
      <p class="muted">Loading…</p>
    </section>
  {:else if explainer && explainer.summary}
    <details class="card explainer">
      <summary>
        <span class="label">About this vaccine</span>
        <span class="chev" aria-hidden="true">›</span>
      </summary>
      <div class="content">
        <p class="summary">{explainer.summary}</p>
        {#if explainer.why_it_matters.length > 0}
          <h3>Why it matters</h3>
          <ul>
            {#each explainer.why_it_matters as item (item)}
              <li>{item}</li>
            {/each}
          </ul>
        {/if}
        {#if explainer.disclaimer}
          <p class="disclaimer">{explainer.disclaimer}</p>
        {/if}
      </div>
    </details>
  {/if}

  <section class="history">
    <h2 class="micro-label">Dose history</h2>
    {#if history.length === 0}
      <div class="empty small">No doses recorded yet.</div>
    {:else}
      <div class="table-scroll">
        <table class="grid">
          <thead>
            <tr>
              <th>Date</th>
              <th>Product</th>
              <th>Dose label</th>
              <th>Facility</th>
              <th>Notes</th>
              <th class="row-actions"></th>
            </tr>
          </thead>
          <tbody>
            {#each history as i (i.id)}
              <tr>
                <td>{formatDate(i.date_given)}</td>
                <td>{i.product_name || '—'}</td>
                <td>{i.dose_label || '—'}</td>
                <td>{i.facility || '—'}</td>
                <td class="notes">{i.notes || '—'}</td>
                <td class="row-actions">
                  <KebabMenu
                    ariaLabel="Immunization actions"
                    items={[
                      { label: 'Edit', href: `${base}/health/immunizations/detail?id=${i.id}` },
                    ]}
                  />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
{/if}

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-4);
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }

  .card {
    /* The layout owns .card's box and reads its padding from this hook. A
		   plain `padding` here is dead: `.health-frame :global(.card)` scopes to
		   (0,3,0) and beats a page-local `.card` at (0,2,0), so the declaration
		   never applied and every card sat at the layout default. */
    --card-padding: 0.85rem 1rem;
    margin-bottom: var(--space-4);
  }
  .coverage-card .status-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-2);
  }
  .grid-stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(140px, 100%), 1fr));
    gap: var(--space-3) var(--space-4);
    margin: 0;
  }
  dt {
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.15rem;
  }
  dd {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }
  .description {
    margin: var(--space-3) 0 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    line-height: 1.55;
    max-width: 75ch;
  }

  .explainer h2 {
    margin: 0 0 var(--space-2);
  }
  details.explainer {
    /* Zero the panel's own padding — via the hook, for the same specificity
		   reason — so the summary and the content can each carry the full card
		   padding themselves. Without this the two stacked and the panel was
		   inset roughly twice as far as the coverage card above it. */
    --card-padding: 0;
  }
  details.explainer > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    /* Matches .card's padding so the collapsed panel is the same box as the
		   coverage card above it. The padding lives on the summary and the
		   content rather than on the <details>, or the click target would stop
		   short of the panel's edges. */
    padding: var(--space-3) var(--space-4);
    user-select: none;
  }
  details.explainer > summary::-webkit-details-marker {
    display: none;
  }
  details.explainer > summary .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
    font-weight: 500;
  }
  details.explainer > summary .chev {
    color: var(--text-dim);
    font-size: 1rem;
    line-height: 1;
    transition: transform 0.15s ease;
  }
  details.explainer[open] > summary .chev {
    transform: rotate(90deg);
  }
  details.explainer:hover > summary .label,
  details.explainer:hover > summary .chev {
    color: var(--text-muted);
  }
  details.explainer > .content {
    /* No top padding: the summary's own bottom padding already separates the
		   label from the body, so adding more would double it once open. */
    padding: 0 var(--space-4) var(--space-3);
  }
  .explainer h3 {
    margin: var(--space-3) 0 var(--space-2);
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .explainer .summary {
    margin: 0;
    font-size: var(--text-base);
    color: var(--text-secondary);
    line-height: 1.55;
    max-width: 75ch;
  }
  .explainer ul {
    margin: 0;
    padding-left: 1.1rem;
    font-size: var(--text-base);
    color: var(--text-secondary);
    line-height: 1.55;
  }
  .explainer li {
    margin: 0.2rem 0;
    /* Same measure as .summary above, applied to the item rather than the
		   list: the <ul>'s marker indent would otherwise eat into the width and
		   wrap each bullet a little earlier than the paragraph. */
    max-width: 75ch;
  }
  .explainer .disclaimer {
    margin: var(--space-3) 0 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-style: italic;
  }
  .explainer.placeholder {
    opacity: 0.7;
  }

  .history h2 {
    margin: 0 0 var(--space-2);
  }
  td.notes {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
  }
  td.row-actions,
  th.row-actions {
    text-align: right;
    white-space: nowrap;
  }

  /* Light theme overrides — dark rules above untouched. */
</style>
