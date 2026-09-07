<script lang="ts">
  import {
    ApiError,
    getTaxEstimate,
    recalculateTaxEstimate,
    type TaxEstimateResponse,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { base } from '$app/paths';
  import { StatTile } from '$lib/components/ui';
  import RateProvenanceLine from '$lib/components/money/RateProvenanceLine.svelte';
  import TaxDisclaimer from '$lib/components/money/TaxDisclaimer.svelte';
  import { formatDecimal } from '$lib/format';

  let data = $state<TaxEstimateResponse | null>(null);
  let loading = $state(true);
  let error = $state('');

  // Editable inputs
  let method = $state('annualized');
  let w2Income = $state(0);
  let w2FedWithholding = $state(0);
  let w2StateWithholding = $state(0);
  let fedEstimatedPaid = $state(0);
  let stateEstimatedPaid = $state(0);
  let w2Months = $state(12);

  let debounceTimer: ReturnType<typeof setTimeout> | undefined;

  async function loadInitial() {
    loading = true;
    error = '';
    try {
      const resp = await getTaxEstimate({ ledger: $selectedLedger || undefined });
      data = resp;
      // Populate editable fields from response
      method = resp.method;
      w2Income = resp.w2_income;
      // The withholding fields are year-to-date inputs, but the response
      // carries them annualized, so they have to be un-projected to get back
      // what the user typed. The backend projects from `annualization_months`
      // (3/5/8/12) to `w2_months` capped at 12 — *not* from `quarter * 3`,
      // which only coincides in Q1 and inflated the field every other quarter.
      // It also never projects below the YTD amount, so a factor under 1 means
      // the response already is the YTD figure.
      const annualizeFactor = Math.max(1, Math.min(resp.w2_months, 12) / resp.annualization_months);
      w2FedWithholding = resp.federal_withholding / annualizeFactor;
      w2StateWithholding = resp.state_withholding / annualizeFactor;
      fedEstimatedPaid = resp.federal_estimated_paid;
      stateEstimatedPaid = resp.state_estimated_paid;
      w2Months = resp.w2_months;
    } catch (e) {
      // A missing tax config is a 404, which is the state the empty message
      // below describes rather than a failure to report — without this the
      // page showed a bare "API error: 404" and its own empty state was
      // unreachable markup.
      if (e instanceof ApiError && e.status === 404) {
        data = null;
        error = '';
      } else if (e instanceof Error) error = e.message;
      else error = 'Failed to load tax estimate';
    } finally {
      loading = false;
    }
  }

  function scheduleRecalc() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recalc, 400);
  }

  async function recalc() {
    if (!data) return;
    error = '';
    try {
      const resp = await recalculateTaxEstimate(
        {
          method,
          w2_income: w2Income,
          w2_federal_withholding: w2FedWithholding,
          w2_state_withholding: w2StateWithholding,
          federal_estimated_paid: fedEstimatedPaid,
          state_estimated_paid: stateEstimatedPaid,
          w2_months: w2Months,
        },
        { ledger: $selectedLedger || undefined },
      );
      data = resp;
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Recalculation failed';
    }
  }

  $effect(() => {
    $selectedLedger;
    loadInitial();
  });

  const fmt = formatDecimal;

  function fmtDollar(n: number): string {
    return '$' + fmt(n);
  }

  function fmtPct(n: number): string {
    return (n * 100).toFixed(1) + '%';
  }

  // Gated on availability as well as on the backend zeroing it: a total that
  // silently exceeds the sum of its visible parts is the worst thing this page
  // could show.
  let totalQuarterly = $derived(
    data
      ? data.federal_quarterly_amount + (data.state_available ? data.state_quarterly_amount : 0)
      : 0,
  );

  let seTaxableBase = $derived(
    data ? data.se_income_annualized * (data.se_taxable_fraction || 0.9235) : 0,
  );
  let seEffectiveRate = $derived(
    data && data.se_income_annualized > 0
      ? (data.federal_total_liability +
          data.state_total_liability -
          data.federal_withholding -
          data.state_withholding) /
          data.se_income_annualized
      : 0,
  );

  let totalGrossIncome = $derived(data ? data.se_income_annualized + data.w2_income_annualized : 0);
  let effectiveTaxRate = $derived(
    data && totalGrossIncome > 0
      ? (data.federal_total_liability + data.state_total_liability) / totalGrossIncome
      : 0,
  );

  // Whether to render the state column, cards and rows at all. Not a zero: a
  // zero is a computed result, and a user in Texas — or one who has not picked
  // a state — should not be looking at a state tax row.
  let showState = $derived(!!data && data.state_available);

  // The state is selected but produced no figures. Distinct from "no state",
  // which shows nothing, because this one is actionable.
  let stateNotice = $derived.by(() => {
    if (!data || !data.state || data.state_available) return '';
    if (data.state_unavailable_reason === 'no_income_tax') {
      return `${data.state_name} levies no individual income tax, so this estimate is federal only.`;
    }
    if (data.state_unavailable_reason === 'no_brackets') {
      return `No tax brackets for ${data.state_name || data.state} for ${data.tax_year}. This estimate is federal only until you enter them in settings.`;
    }
    return `${data.state} is not a state we recognise, so this estimate is federal only.`;
  });

  let stateColumnLabel = $derived(data && data.state ? `State (${data.state})` : 'State');

  // Colorado and Iowa apply their rate to federal *taxable* income, so both
  // "AGI" as a row label and "QBI does not apply" are false for them — the QBI
  // deduction is already inside their base. The settings card branches on this;
  // the breakdown did not.
  let stateStartsFromTaxable = $derived(data?.state_starts_from === 'federal_taxable_income');
  let stateBasisLabel = $derived(
    data?.state_starts_from === 'federal_taxable_income'
      ? 'federal taxable income'
      : data?.state_starts_from === 'gross_compensation'
        ? 'gross compensation'
        : '',
  );

  // The schedule in force, described rather than assumed. California's
  // 30/40/0/30 used to be stated here as though it were everyone's.
  let installmentDescription = $derived.by(() => {
    const schedule = data?.state_installment_schedule ?? [];
    if (schedule.length !== 4) return '';
    const equal = schedule.every((v, i) => Math.abs(v - (i + 1) * 0.25) < 0.0001);
    if (equal)
      return `${data?.state_name || 'State'} installments are 25% each quarter, like federal.`;
    const perQuarter = schedule.map((v, i) => Math.round((v - (i ? schedule[i - 1] : 0)) * 100));
    return `${data?.state_name || 'State'} uses a ${perQuarter.join('/')} schedule (Apr/Jun/Sep/Jan) rather than equal quarters.`;
  });
</script>

<div class="tax-content">
  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error && !data}
    <div class="center-msg error">{error}</div>
  {:else if !data}
    <div class="empty">
      No tax configuration found. Set your filing status, year and state in
      <a href="{base}/money/settings/taxes">money settings</a>.
    </div>
  {:else}
    {#if error}
      <div class="error-msg">{error}</div>
    {/if}

    {#if stateNotice}
      <div class="banner info state-notice">
        {stateNotice}
        {#if data.state_unavailable_reason === 'no_brackets'}
          <a href="{base}/money/settings/taxes">Add them</a>
        {/if}
      </div>
    {/if}

    <div class="tax-layout">
      <!-- Inputs -->
      <section class="input-section">
        <h2>Inputs</h2>
        <div class="input-card">
          <div class="input-group">
            <label class="input-label" for="w2-income">W-2 income YTD</label>
            <div class="input-field">
              <span class="input-prefix">$</span>
              <input id="w2-income" type="number" bind:value={w2Income} oninput={scheduleRecalc} />
            </div>
          </div>

          <div class="input-group">
            <label class="input-label" for="w2-fed">Federal withholding YTD</label>
            <div class="input-field">
              <span class="input-prefix">$</span>
              <input
                id="w2-fed"
                type="number"
                bind:value={w2FedWithholding}
                oninput={scheduleRecalc}
              />
            </div>
          </div>

          <div class="input-group">
            <label class="input-label" for="w2-state">State withholding YTD</label>
            <div class="input-field">
              <span class="input-prefix">$</span>
              <input
                id="w2-state"
                type="number"
                bind:value={w2StateWithholding}
                oninput={scheduleRecalc}
              />
            </div>
          </div>

          <div class="input-group">
            <label class="input-label" for="w2-months">W-2 employment months</label>
            <div class="input-field">
              <input
                id="w2-months"
                type="number"
                min="1"
                max="12"
                bind:value={w2Months}
                oninput={scheduleRecalc}
              />
              <span class="input-suffix">of 12</span>
            </div>
          </div>

          <div class="input-group">
            <label class="input-label" for="fed-est">Federal estimated paid</label>
            <div class="input-field">
              <span class="input-prefix">$</span>
              <input
                id="fed-est"
                type="number"
                bind:value={fedEstimatedPaid}
                oninput={scheduleRecalc}
              />
            </div>
          </div>

          <div class="input-group">
            <label class="input-label" for="state-est">State estimated paid</label>
            <div class="input-field">
              <span class="input-prefix">$</span>
              <input
                id="state-est"
                type="number"
                bind:value={stateEstimatedPaid}
                oninput={scheduleRecalc}
              />
            </div>
          </div>
        </div>

        <div class="input-meta">
          <span>Q{data.quarter} {data.tax_year}</span>
          <span>{data.filing_status.toUpperCase()}</span>
          <span
            >{data.quarters_remaining} quarter{data.quarters_remaining !== 1 ? 's' : ''} remaining</span
          >
        </div>
      </section>

      <!-- Results -->
      <section class="results-section">
        <h2>Estimate</h2>

        <div class="summary-cards">
          <StatTile
            label="Federal due"
            surface
            borderColor="var(--border-default)"
            valueSize="var(--text-base)"
          >
            {fmtDollar(data.federal_quarterly_amount)}
          </StatTile>
          {#if showState}
            <StatTile
              label="{data.state_name || 'State'} due"
              surface
              borderColor="var(--border-default)"
              valueSize="var(--text-base)"
            >
              {fmtDollar(data.state_quarterly_amount)}
            </StatTile>
          {/if}
          <!-- The total is ranked above its parts by a stronger outline, which
               is why the border is a per-tile colour rather than baked in. -->
          <StatTile
            label="Total due this quarter"
            surface
            borderColor="var(--text-dim)"
            valueSize="var(--text-base)"
          >
            {fmtDollar(totalQuarterly)}
          </StatTile>
        </div>

        <div class="breakdown-table">
          <div class="breakdown-header">
            <span class="breakdown-label"></span>
            <span class="breakdown-val">Federal</span>
            {#if showState}<span class="breakdown-val">{stateColumnLabel}</span>{/if}
          </div>

          <div class="breakdown-group-label">Income</div>
          <div class="breakdown-row">
            <span class="breakdown-label">SE income (YTD)</span>
            <span class="breakdown-val">{fmtDollar(data.se_income_ytd)}</span>
            {#if showState}<span class="breakdown-val"></span>{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">SE income (annualized)</span>
            <span class="breakdown-val">{fmtDollar(data.se_income_annualized)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.se_income_annualized)}</span
              >{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">W-2 income (annualized)</span>
            <span class="breakdown-val">{fmtDollar(data.w2_income_annualized)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.w2_income_annualized)}</span
              >{/if}
          </div>
          <details class="info-panel">
            <summary>How income is annualized</summary>
            <p>
              This uses the IRS annualized-income method (Form 2210). For the Q{data.quarter}
              payment, SE income is pulled from the ledger through the first
              {data.annualization_months} months of the year and annualized &times;{(
                12 / data.annualization_months
              ).toFixed(2)} to project a full year.
              {#if data.w2_months < 12}
                W-2 income is projected from those {data.annualization_months} months to
                {data.w2_months} months of employment (the job ending early), and never falls below the
                amount already earned.
              {:else}
                W-2 income is projected from the same {data.annualization_months} months to a full year.
              {/if}
              {#if method === 'annualized'}
                The period grows each quarter (3, 5, 8, then 12 months), so the projection tightens
                toward your actual annual income.
              {/if}
            </p>
          </details>

          <div class="breakdown-group-label">Self-employment tax</div>
          <div class="breakdown-row">
            <span class="breakdown-label">SE tax</span>
            <span class="breakdown-val">{fmtDollar(data.se_tax)}</span>
            {#if showState}<span class="breakdown-val dim">n/a</span>{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">Half SE deduction</span>
            <span class="breakdown-val">{fmtDollar(data.half_se_deduction)}</span>
            {#if showState}<span class="breakdown-val dim">n/a</span>{/if}
          </div>
          {#if data.additional_medicare_tax > 0}
            <div class="breakdown-row">
              <span class="breakdown-label">Additional Medicare tax (0.9%)</span>
              <span class="breakdown-val">{fmtDollar(data.additional_medicare_tax)}</span>
              {#if showState}<span class="breakdown-val dim">n/a</span>{/if}
            </div>
          {/if}
          <details class="info-panel">
            <summary>How SE tax works</summary>
            <p>
              SE tax is 15.3% (12.4% Social Security + 2.9% Medicare) on {fmtPct(
                data.se_taxable_fraction,
              )} of net SE income. The taxable base is {fmtDollar(seTaxableBase)}.
              {#if data.ss_wage_base > 0 && seTaxableBase > data.ss_wage_base}
                Social Security applies only up to the {data.tax_year} wage base ({fmtDollar(
                  data.ss_wage_base,
                )}); income above that pays only the 2.9% Medicare rate.
              {/if}
              SE tax is computed on the SE person's income alone; the spouse's W-2 wages do not affect
              the SS cap. Half of SE tax ({fmtDollar(data.half_se_deduction)}) is an above-the-line
              deduction that reduces AGI.
              {#if data.additional_medicare_tax > 0}
                An additional 0.9% Medicare tax applies to combined earned income (W-2 + SE) above
                the filing-status threshold.
              {/if}
            </p>
          </details>

          <div class="breakdown-group-label">Tax calculation</div>
          <div class="breakdown-row">
            <span class="breakdown-label"
              >AGI{#if showState && stateBasisLabel}<span class="basis-note">
                  · state basis: {stateBasisLabel}</span
                >{/if}</span
            >
            <span class="breakdown-val">{fmtDollar(data.federal_agi)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_agi)}</span>{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">Standard deduction</span>
            <span class="breakdown-val">{fmtDollar(data.federal_standard_deduction)}</span>
            {#if showState}<span class="breakdown-val"
                >{fmtDollar(data.state_standard_deduction)}</span
              >{/if}
          </div>
          {#if showState && data.state_personal_exemption > 0}
            <!-- Illinois, Indiana and Michigan carry an exemption instead of a
                 standard deduction. Without this row the gap between AGI and
                 taxable income has nothing explaining it. -->
            <div class="breakdown-row">
              <span class="breakdown-label">Personal exemption</span>
              <span class="breakdown-val dim">n/a</span>
              <span class="breakdown-val">{fmtDollar(data.state_personal_exemption)}</span>
            </div>
          {/if}
          {#if data.qbi_deduction > 0}
            <div class="breakdown-row">
              <span class="breakdown-label">QBI deduction</span>
              <span class="breakdown-val">{fmtDollar(data.qbi_deduction)}</span>
              {#if showState}
                <!-- Already inside the base for a federal-taxable-income state,
                     rather than excluded from it. -->
                <span class="breakdown-val dim">{stateStartsFromTaxable ? 'included' : 'n/a'}</span>
              {/if}
            </div>
          {/if}
          <div class="breakdown-row">
            <span class="breakdown-label">Taxable income</span>
            <span class="breakdown-val">{fmtDollar(data.federal_taxable_income)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_taxable_income)}</span
              >{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">Income tax</span>
            <span class="breakdown-val">{fmtDollar(data.federal_tax)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_tax)}</span>{/if}
          </div>
          <div class="breakdown-row highlight">
            <span class="breakdown-label">Total liability</span>
            <span class="breakdown-val">{fmtDollar(data.federal_total_liability)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_total_liability)}</span
              >{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">Effective tax rate</span>
            <span class="breakdown-val combined">{fmtPct(effectiveTaxRate)}</span>
          </div>
          <details class="info-panel">
            <summary>How tax liability is computed</summary>
            <p>
              AGI = annualized SE income + annualized W-2 income - half SE deduction. Federal
              taxable income = AGI - standard deduction ({fmtDollar(
                data.federal_standard_deduction,
              )})
              {#if data.qbi_deduction > 0}
                - QBI deduction ({fmtDollar(data.qbi_deduction)}, which is 20% of qualified business
                income under Section 199A)
              {/if}. Federal income tax is computed using the progressive {data.tax_year}
              {data.filing_status.toUpperCase()} brackets. Federal total liability includes both income
              tax and SE tax.
              {#if showState}
                {#if stateStartsFromTaxable}
                  {data.state_name} applies its rate to federal taxable income, so the federal standard
                  deduction and the QBI deduction are both already inside its base and it has no deduction
                  of its own.
                {:else if data.state_starts_from === 'gross_compensation'}
                  {data.state_name} taxes gross wage and self-employment income under its own rules: no
                  standard deduction, no personal exemption, and no above-the-line deductions.
                {:else if data.state_personal_exemption > 0}
                  {data.state_name} uses its own rate and a personal exemption of {fmtDollar(
                    data.state_personal_exemption,
                  )} rather than a standard deduction; the QBI deduction does not reduce its taxable income.
                {:else}
                  {data.state_name} uses its own brackets and a standard deduction of {fmtDollar(
                    data.state_standard_deduction,
                  )}; the QBI deduction does not reduce its taxable income.
                {/if}
              {/if}
            </p>
          </details>

          <div class="breakdown-group-label">Credits and payments</div>
          <div class="breakdown-row">
            <span class="breakdown-label">Withholding (annualized)</span>
            <span class="breakdown-val">{fmtDollar(data.federal_withholding)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_withholding)}</span
              >{/if}
          </div>
          <div class="breakdown-row">
            <span class="breakdown-label">Estimated payments made</span>
            <span class="breakdown-val">{fmtDollar(data.federal_estimated_paid)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_estimated_paid)}</span
              >{/if}
          </div>
          <div class="breakdown-row highlight">
            <span class="breakdown-label">Net due</span>
            <span class="breakdown-val">{fmtDollar(data.federal_net_due)}</span>
            {#if showState}<span class="breakdown-val">{fmtDollar(data.state_net_due)}</span>{/if}
          </div>
          <div class="breakdown-row result">
            <span class="breakdown-label">Due this quarter (Q{data.quarter})</span>
            <span class="breakdown-val">{fmtDollar(data.federal_quarterly_amount)}</span>
            {#if showState}<span class="breakdown-val"
                >{fmtDollar(data.state_quarterly_amount)}</span
              >{/if}
          </div>
          <details class="info-panel">
            <summary>How the quarterly amount is determined</summary>
            <p>
              {#if method === 'annualized'}
                Net due = total annual liability - annualized withholding - estimated payments
                already made. Federal installments are 25% each quarter.
                {#if showState}{installmentDescription}{/if} As W-2 withholding and income data are updated
                each quarter, the per-quarter amount self-corrects.
              {:else}
                Safe harbor uses last year's total tax divided by 4 as each quarterly payment.
                Withholding is subtracted first. This avoids underpayment penalties regardless of
                current-year income changes.
              {/if}
            </p>
            {#if data.se_income_annualized > 0}
              <p>
                The SE income bears an effective marginal rate of ~{fmtPct(seEffectiveRate)} when stacked
                on top of W-2 income, because it's taxed at the household's marginal bracket (not starting
                from zero).
              </p>
            {/if}
          </details>
        </div>

        <div class="provenance-footnote">
          <p class="micro-label">Where these rates come from</p>
          <div class="prov-block">
            <span class="prov-label">Federal</span>
            <RateProvenanceLine provenance={data.federal_rates} taxYear={data.tax_year} />
          </div>
          {#if data.state_rates && data.state}
            <div class="prov-block">
              <span class="prov-label">{data.state_name || data.state}</span>
              <RateProvenanceLine
                provenance={data.state_rates}
                taxYear={data.tax_year}
                available={data.state_available}
              />
            </div>
          {/if}
          <p class="settings-link">
            <a href="{base}/money/settings/taxes">Override any of these in settings</a>
          </p>
        </div>

        <TaxDisclaimer />
      </section>
    </div>
  {/if}
</div>

<style>
  /* A growing column so the whole-pane states (`.center-msg`) center in the
	   section body rather than sitting against the top edge. */
  .tax-content {
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    flex: 1 0 auto;
  }

  .tax-layout {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: var(--space-3);
    align-items: start;
  }

  section h2 {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 var(--space-2) var(--space-1);
  }

  /* Inputs */
  .input-card {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .input-group {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .input-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .input-field {
    display: flex;
    align-items: center;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .input-prefix {
    padding: var(--space-1) 0 var(--space-1) var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .input-field input {
    flex: 1;
    padding: var(--space-1) var(--space-2) var(--space-1) var(--space-1);
    background: transparent;
    border: none;
    color: var(--text-primary);
    font-size: var(--text-sm);
    font-variant-numeric: tabular-nums;
    /* .input-field:focus-within below is the affordance for this input. */
    outline: none;
    min-width: 0;
    -moz-appearance: textfield;
    appearance: textfield;
  }

  .input-field input::-webkit-outer-spin-button,
  .input-field input::-webkit-inner-spin-button {
    -webkit-appearance: none;
    margin: 0;
  }

  .input-suffix {
    padding: var(--space-1) var(--space-2) var(--space-1) 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .input-field:focus-within {
    border-color: var(--text-muted);
  }

  .input-meta {
    display: flex;
    gap: var(--space-3);
    margin-top: var(--space-1);
    padding: 0 var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  /* Summary cards */
  .summary-cards {
    display: flex;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  /* The tiles are `StatTile`; only their share of the row is this page's.
     :global because the children are components now, and Svelte prunes a
     selector whose subject it cannot see — silently. Scoped under the row, so
     it is placement rather than a leak. */
  .summary-cards > :global(*) {
    flex: 1;
  }

  /* Breakdown table */
  .breakdown-table {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    overflow: hidden;
  }

  .breakdown-header {
    display: flex;
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
    border-bottom: 1px solid var(--border-subtle);
  }

  .breakdown-group-label {
    padding: var(--space-2) var(--space-3) 0.15rem;
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-weight: 500;
    border-top: 1px solid var(--border-subtle);
  }

  .breakdown-row {
    display: flex;
    padding: 0.2rem var(--space-3);
    font-size: var(--text-sm);
  }

  .breakdown-row.highlight {
    font-weight: 600;
    padding-top: var(--space-1);
    padding-bottom: var(--space-1);
  }

  .breakdown-row.result {
    font-weight: 600;
    background: var(--surface-raised);
    padding-top: var(--space-2);
    padding-bottom: var(--space-2);
  }

  .breakdown-label {
    flex: 1;
    color: var(--text-secondary);
  }

  .breakdown-val {
    width: 8rem;
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: var(--text-primary);
  }

  .breakdown-val.dim {
    color: var(--text-dim);
  }

  .breakdown-val.combined {
    width: 16rem;
    color: var(--text-muted);
  }

  .breakdown-header .breakdown-label {
    color: transparent;
  }

  .breakdown-header .breakdown-val {
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
  }

  /* Info panels */
  .info-panel {
    margin: 0;
    padding: 0 var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }

  .info-panel summary {
    padding: var(--space-2) 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
    cursor: pointer;
    user-select: none;
    list-style: none;
  }

  .info-panel summary::before {
    content: '+ ';
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
  }

  .info-panel[open] summary::before {
    content: '- ';
  }

  .info-panel summary::-webkit-details-marker {
    display: none;
  }

  .info-panel p {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
    line-height: 1.5;
  }

  .state-notice {
    margin-bottom: var(--space-3);
  }

  .provenance-footnote {
    margin-top: var(--space-3);
    margin-bottom: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .prov-block {
    display: flex;
    flex-direction: column;
  }

  .prov-label {
    font-size: var(--text-xs);
    font-weight: 600;
    color: var(--text-secondary);
  }

  .settings-link {
    margin: 0;
    font-size: var(--text-xs);
  }

  .basis-note {
    color: var(--text-dim);
    font-size: var(--text-xs);
  }

  .settings-link a,
  .empty a,
  .state-notice a {
    color: var(--link);
  }

  /* 640px, the app's narrow breakpoint, rather than a one-off 720px. */
  @media (max-width: 640px) {
    .tax-layout {
      grid-template-columns: 1fr;
    }

    .summary-cards {
      flex-direction: column;
    }

    .breakdown-val {
      width: 6rem;
    }
  }
</style>
