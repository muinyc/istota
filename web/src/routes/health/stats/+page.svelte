<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { theme } from '$lib/stores/theme';
  import { chartChrome } from '$lib/chartTheme';
  import {
    Chart,
    LineController,
    LineElement,
    PointElement,
    CategoryScale,
    LinearScale,
    Tooltip,
    Filler,
  } from 'chart.js';
  import {
    createHealthStat,
    deleteHealthStat,
    getHealthSettings,
    healthStatsSeries,
    listHealthStats,
    type HealthSettings,
    type HealthStat,
  } from '$lib/api';
  import {
    LOG_UNIT_CHOICES,
    METRIC_LABELS,
    METRIC_UNITS,
    formatStat,
    metricLabel,
    toCanonical,
  } from '$lib/health/units';
  import { Button, Field, Input, Modal, Select } from '$lib/components/ui';
  import { formatDate } from '$lib/dateFormat';

  Chart.register(
    LineController,
    LineElement,
    PointElement,
    CategoryScale,
    LinearScale,
    Tooltip,
    Filler,
  );

  const METRIC_KEYS = Object.keys(METRIC_LABELS);
  const metricOptions = METRIC_KEYS.map((m) => ({ value: m, label: METRIC_LABELS[m] }));

  type Range = '30d' | '90d' | '1y' | 'all';

  let range = $state<Range>('90d');
  let loading = $state(true);
  let error = $state('');
  let settings: HealthSettings | null = $state(null);

  // metric -> series points
  let seriesByMetric: Record<string, { measured_at: string; value: number; unit: string }[]> =
    $state({});
  // metric -> latest HealthStat (used for the headline value)
  let latestByMetric: Record<string, HealthStat> = $state({});

  // Per-card chart instances + canvas refs so we can rebuild on resize.
  const charts: Record<string, Chart | undefined> = {};
  const canvases: Record<string, HTMLCanvasElement | undefined> = $state({});

  // Modal state for manual entry.
  let modalOpen = $state(false);
  let formMetric = $state('weight');
  // `bind:value` on a number input writes back a number, or null once the
  // field is cleared — never the empty string this starts as (ISSUE-358).
  let formValue = $state<string | number | null>('');
  let formUnit = $state('');
  let formDate = $state('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');

  function defaultUnitFor(metric: string): string {
    const choices = LOG_UNIT_CHOICES[metric];
    if (!choices) return METRIC_UNITS[metric] || '';
    const display = settings?.display_units;
    if (metric === 'weight' && display?.weight === 'lb') return 'lb';
    if (metric === 'body_temp' && display?.temp === 'F') return '°F';
    return choices[0];
  }

  // Reset formUnit to a sensible default whenever the chosen metric changes.
  $effect(() => {
    formMetric;
    untrack(() => {
      formUnit = defaultUnitFor(formMetric);
    });
  });

  function rangeSince(r: Range): string | undefined {
    if (r === 'all') return undefined;
    const days = { '30d': 30, '90d': 90, '1y': 365 }[r];
    return new Date(Date.now() - days * 86400 * 1000).toISOString();
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const since = rangeSince(range);
      const [latest, ss] = await Promise.all([
        listHealthStats({ limit: 1000 }).then((r) => r.stats),
        settings ? Promise.resolve({ settings }) : getHealthSettings(),
      ]);
      settings = ss.settings;
      // Determine which metrics have any data.
      const seen = new Set(latest.map((s) => s.metric));
      const series: Record<string, { measured_at: string; value: number; unit: string }[]> = {};
      const latestMap: Record<string, HealthStat> = {};
      for (const s of latest) {
        const prev = latestMap[s.metric];
        if (!prev || s.measured_at > prev.measured_at) latestMap[s.metric] = s;
      }
      latestByMetric = latestMap;
      // Fetch series for each present metric in parallel.
      const results = await Promise.all(
        [...seen].map(async (m) => {
          const resp = await healthStatsSeries(m, since ? { since } : {});
          return [m, resp.points] as const;
        }),
      );
      for (const [m, points] of results) {
        series[m] = points;
      }
      seriesByMetric = series;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load stats';
    } finally {
      loading = false;
    }
  }

  function renderChart(metric: string) {
    const canvas = canvases[metric];
    if (!canvas) return;
    const display = settings?.display_units ?? {
      weight: 'kg' as const,
      height: 'cm' as const,
      temp: 'C' as const,
    };
    const points = seriesByMetric[metric] || [];
    const labels: string[] = [];
    const values: number[] = [];
    for (const p of points) {
      labels.push(formatDate(p.measured_at, { month: 'short', day: 'numeric' }));
      values.push(formatStat(metric, p.value, p.unit, display).value);
    }
    if (charts[metric]) {
      charts[metric]!.destroy();
    }
    const chrome = chartChrome();
    charts[metric] = new Chart(canvas, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            data: values,
            /* design-lint-allow-begin: data viz — Chart.js takes a config
               object and never reads the cascade, so it cannot resolve var().
               Chart chrome is themed by $lib/chartTheme; this is a series. */
            borderColor: 'rgb(122, 163, 216)',
            backgroundColor: 'rgba(122, 163, 216, 0.15)',
            /* design-lint-allow-end */
            borderWidth: 1.5,
            tension: 0.25,
            fill: true,
            pointRadius: 0,
            pointHoverRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
        scales: {
          x: { display: false },
          y: {
            display: true,
            grid: { color: chrome.grid },
            ticks: {
              font: { size: 9 },
              maxTicksLimit: 4,
              color: chrome.tick,
            },
          },
        },
      },
    });
  }

  function renderBpChart() {
    const canvas = canvases['blood_pressure'];
    if (!canvas) return;
    const sys = seriesByMetric.blood_pressure_systolic || [];
    const dia = seriesByMetric.blood_pressure_diastolic || [];
    // Align points by date for a paired chart.
    const dates = new Set([...sys.map((p) => p.measured_at), ...dia.map((p) => p.measured_at)]);
    const sorted = [...dates].sort();
    const sysMap = new Map(sys.map((p) => [p.measured_at, p.value]));
    const diaMap = new Map(dia.map((p) => [p.measured_at, p.value]));
    const labels = sorted.map((d) => formatDate(d, { month: 'short', day: 'numeric' }));
    const sysValues = sorted.map((d) => sysMap.get(d) ?? null);
    const diaValues = sorted.map((d) => diaMap.get(d) ?? null);
    if (charts.blood_pressure) charts.blood_pressure!.destroy();
    const chrome = chartChrome();
    charts.blood_pressure = new Chart(canvas, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Systolic',
            data: sysValues,
            // design-lint-allow: data-viz — Chart.js series color, read from its
            // own config rather than CSS. Paired with Diastolic below.
            borderColor: '#f08c8c',
            backgroundColor: 'transparent',
            borderWidth: 1.5,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 3,
            spanGaps: true,
          },
          {
            label: 'Diastolic',
            data: diaValues,
            // design-lint-allow: data-viz — Chart.js series color.
            borderColor: '#7aa3d8',
            backgroundColor: 'transparent',
            borderWidth: 1.5,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 3,
            spanGaps: true,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
        scales: {
          x: { display: false },
          y: {
            display: true,
            grid: { color: chrome.grid },
            ticks: { font: { size: 9 }, maxTicksLimit: 4, color: chrome.tick },
          },
        },
      },
    });
  }

  $effect(() => {
    range;
    untrack(load);
  });

  $effect(() => {
    seriesByMetric;
    // Chart.js holds its colors as plain config, so a theme flip needs a
    // rebuild — reading $theme here makes this effect depend on it.
    $theme;
    untrack(() => {
      // Render after the DOM updates with the new card list.
      queueMicrotask(() => {
        for (const m of Object.keys(seriesByMetric)) {
          if (m === 'blood_pressure_systolic' || m === 'blood_pressure_diastolic') continue;
          renderChart(m);
        }
        if (canvases['blood_pressure']) renderBpChart();
      });
    });
  });

  async function submitEntry(e: Event) {
    e.preventDefault();
    // Not `formValue.trim()`: the binding above has made this a number by the
    // time anyone can press Save, so a string guard threw here — outside the
    // try, so the modal reported nothing at all. NaN is unreachable through
    // the input itself, which sanitizes a bad entry to '', but the field is
    // also written by `openEntry` and by the reset below.
    const entered = Number(formValue);
    if (formValue === null || formValue === '' || Number.isNaN(entered)) return;
    saving = true;
    formError = '';
    try {
      const canonical = toCanonical(formMetric, entered, formUnit);
      await createHealthStat({
        metric: formMetric,
        value: canonical.value,
        unit: canonical.unit,
        measured_at: formDate || undefined,
        notes: formNotes.trim() || undefined,
      });
      modalOpen = false;
      formValue = '';
      formNotes = '';
      formDate = '';
      formUnit = defaultUnitFor(formMetric);
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  function openEntry(metric?: string) {
    if (metric) formMetric = metric;
    formUnit = defaultUnitFor(formMetric);
    modalOpen = true;
  }

  function formatLatestValue(metric: string): { value: number; unit: string } | null {
    const stat = latestByMetric[metric];
    if (!stat || !settings) return null;
    return formatStat(metric, stat.value, stat.unit, settings.display_units);
  }

  function bpHeadline(): string | null {
    const s = latestByMetric.blood_pressure_systolic;
    const d = latestByMetric.blood_pressure_diastolic;
    if (!s && !d) return null;
    return `${s ? Math.round(s.value) : '—'}/${d ? Math.round(d.value) : '—'}`;
  }

  function bpLatestDate(): string | null {
    const s = latestByMetric.blood_pressure_systolic;
    const d = latestByMetric.blood_pressure_diastolic;
    const iso =
      s?.measured_at && d?.measured_at
        ? s.measured_at > d.measured_at
          ? s.measured_at
          : d.measured_at
        : s?.measured_at || d?.measured_at;
    if (!iso) return null;
    return formatDate(iso);
  }

  function metricsToShow(): string[] {
    // Every metric with at least one data point, excluding the BP halves
    // (they merge into the combined card).
    return Object.keys(seriesByMetric)
      .filter((m) => m !== 'blood_pressure_systolic' && m !== 'blood_pressure_diastolic')
      .sort((a, b) => (METRIC_LABELS[a] || a).localeCompare(METRIC_LABELS[b] || b));
  }

  function hasBp(): boolean {
    return Boolean(
      seriesByMetric.blood_pressure_systolic?.length ||
      seriesByMetric.blood_pressure_diastolic?.length,
    );
  }

  function bmi(): number | null {
    const w = latestByMetric.weight;
    if (!w || !settings?.height_cm) return null;
    const h = settings.height_cm / 100;
    return Math.round((w.value / (h * h)) * 10) / 10;
  }

  onMount(load);
</script>

{#if !loading && !error}
  <!-- Held back behind both whole-pane states, so the pane shows nothing but
       the centered message rather than centering it in the space left under
       this header. -->
  <div class="bar">
    <div class="ranges">
      {#each ['30d', '90d', '1y', 'all'] as r}
        <button class:active={range === r} onclick={() => (range = r as Range)} type="button"
          >{r}</button
        >
      {/each}
    </div>
    <button class="log-btn" onclick={() => openEntry()} type="button">+ Log measurement</button>
  </div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if metricsToShow().length === 0 && !hasBp()}
  <div class="empty">
    No measurements yet.
    <button class="link" onclick={() => openEntry()} type="button"
      >Log your first measurement</button
    >.
  </div>
{:else}
  <div class="grid card-grid" style="--card-min: 380px;">
    {#each metricsToShow() as metric (metric)}
      {@const v = formatLatestValue(metric)}
      <button class="card interactive" onclick={() => openEntry(metric)} type="button">
        <header>
          <span class="label">{metricLabel(metric)}</span>
          <span class="count">{(seriesByMetric[metric] || []).length} pts</span>
        </header>
        <div class="value">
          {#if v}
            {v.value}<span class="unit">{v.unit}</span>
          {:else}
            —
          {/if}
        </div>
        <div class="chart">
          <canvas bind:this={canvases[metric]}></canvas>
        </div>
        {#if metric === 'weight' && bmi() != null}
          <div class="caption">BMI {bmi()}</div>
        {:else if latestByMetric[metric]}
          <div class="caption">
            {formatDate(latestByMetric[metric].measured_at)}
          </div>
        {/if}
      </button>
    {/each}
    {#if hasBp()}
      <button
        class="card interactive"
        onclick={() => openEntry('blood_pressure_systolic')}
        type="button"
      >
        <header>
          <span class="label">Blood Pressure</span>
          <span class="count">
            {Math.max(
              (seriesByMetric.blood_pressure_systolic || []).length,
              (seriesByMetric.blood_pressure_diastolic || []).length,
            )} pts
          </span>
        </header>
        <div class="value">
          {bpHeadline() || '—'}<span class="unit">mmHg</span>
        </div>
        <div class="chart">
          <canvas bind:this={canvases['blood_pressure']}></canvas>
        </div>
        {#if bpLatestDate()}<div class="caption">{bpLatestDate()}</div>{/if}
      </button>
    {/if}
  </div>
{/if}

<Modal bind:open={modalOpen} title="Log measurement" width="26rem">
  <form onsubmit={submitEntry}>
    <!-- labelled={false}: the row holds a number input and a unit select, so
         one implicit label would claim the first and leave the other unnamed. -->
    <Field label="Metric" labelled={false}>
      <Select
        value={formMetric}
        options={metricOptions}
        onValueChange={(v) => (formMetric = v)}
        ariaLabel="Metric"
        fullWidth
      />
    </Field>
    <Field label="Value" labelled={false}>
      <div class="value-row">
        <Input type="number" step="any" bind:value={formValue} required aria-label="Value" />
        {#if LOG_UNIT_CHOICES[formMetric]}
          <select class="unit-select" bind:value={formUnit} aria-label="Unit">
            {#each LOG_UNIT_CHOICES[formMetric] as u}
              <option value={u}>{u}</option>
            {/each}
          </select>
        {:else}
          <span class="unit-static">{METRIC_UNITS[formMetric]}</span>
        {/if}
      </div>
    </Field>
    <Field label="When">
      <Input type="datetime-local" bind:value={formDate} />
    </Field>
    <Field label="Notes">
      <Input bind:value={formNotes} placeholder="optional" />
    </Field>
    {#if formError}<div class="banner error">{formError}</div>{/if}
    <div class="modal-actions">
      <Button onclick={() => (modalOpen = false)} disabled={saving}>Cancel</Button>
      <Button variant="primary" type="submit" loading={saving}>Save</Button>
    </div>
  </form>
</Modal>

<style>
  .bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-4);
  }
  .ranges {
    display: flex;
    gap: var(--space-1);
  }
  .ranges button {
    background: none;
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-pill);
    cursor: pointer;
  }
  .ranges button.active {
    color: var(--text-primary);
    border-color: var(--text-primary);
  }
  .log-btn {
    padding: var(--space-2) var(--space-3);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
  }
  .log-btn:hover {
    background: var(--surface-raised);
  }
  .link {
    background: none;
    border: none;
    color: var(--text-primary);
    font: inherit;
    cursor: pointer;
    text-decoration: underline;
    padding: 0;
  }
  .card {
    font: inherit;
    text-align: left;
    display: flex;
    flex-direction: column;
    min-height: 170px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .label {
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .count {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .value {
    font-size: 1.6rem;
    font-weight: 500;
    margin-top: var(--space-1);
    line-height: 1.1;
  }
  .unit {
    font-size: var(--text-sm);
    color: var(--text-muted);
    margin-left: var(--space-1);
  }
  .chart {
    flex: 1;
    min-height: 70px;
    margin-top: var(--space-2);
  }
  /* Typography is the global .caption; the spacing stays at the call site. */
  .caption {
    margin-top: var(--space-1);
  }

  .value-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  /* :global because the value input is an <Input> now — Svelte prunes a rule
     whose subject sits inside a component. */
  .value-row :global(input) {
    flex: 1;
    min-width: 0;
  }
  /* The element selector takes this to (0,2,1), which beats Field's
     `.field :global(select) { width: 100% }` deterministically rather than by
     stylesheet order — without it the fixed-width unit picker takes the whole
     row and collapses the value input to near-zero. */
  .value-row select.unit-select {
    width: auto;
    min-width: 4.5rem;
    flex: 0 0 auto;
  }
  .unit-static {
    font-size: var(--text-sm);
    color: var(--text-muted);
    padding: var(--space-1) 0.2rem;
  }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  /* Light theme overrides — dark rules above untouched. */
</style>
