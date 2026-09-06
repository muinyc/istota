<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Badge, Button, Input, Select } from '$lib/components/ui';
  import {
    ApiError,
    getLedgers,
    getTransactionRules,
    testTransactionRule,
    type RuleResolution,
    type RuleTraceEntry,
    type TransactionRule,
  } from '$lib/money/api';
  import { KNOWN_SOURCES, ledgerScopeOptions, sourceScopeOptions } from '$lib/money/ruleScopes';

  // Resolve a made-up transaction against the stored rules, and show the pass
  // rather than only its answer. A resolution alone cannot say why a rule did
  // nothing, and "why did this not apply" is the question the section exists
  // to answer, so the trace carries one line per rule in scope.
  //
  // Unlike the add form next door, the scope here defaults rather than
  // insisting on a pick: this writes nothing, and a preview that refuses to
  // run until two dropdowns are set is a preview nobody uses. The scope it
  // used is on screen, which is what makes the defaulting honest.

  const OUTCOMES: Record<string, { label: string; variant: 'success' | 'warn' | 'neutral' }> = {
    applied: { label: 'applied', variant: 'success' },
    shadowed: { label: 'shadowed', variant: 'warn' },
    superseded_by_skip: { label: 'discarded', variant: 'warn' },
    no_match: { label: 'no match', variant: 'neutral' },
    not_evaluated: { label: 'not reached', variant: 'neutral' },
    ignored: { label: 'ignored', variant: 'warn' },
  };

  let ledgers: string[] = $state([]);
  let rules: TransactionRule[] = $state([]);
  let ready = $state(false);

  let ledger = $state('');
  let source = $state(KNOWN_SOURCES[0]);
  let category = $state('');
  let account = $state('');
  let payee = $state('');
  let notes = $state('');
  let tags = $state('');

  let busy = $state(false);
  // Two Runs in flight resolve in whatever order the server answers, and the
  // slower one must not paint its result over the newer one.
  let runSeq = 0;
  let error = $state('');
  let resolution: RuleResolution | null = $state(null);
  let trace: RuleTraceEntry[] = $state([]);
  let dropped: number[] = $state([]);

  const ledgerOptions = $derived(ledgerScopeOptions(ledgers, rules));
  const sourceOptions = $derived(sourceScopeOptions(rules));

  onMount(async () => {
    try {
      ledgers = await getLedgers();
      // '' is a legal ledger scope, so an empty configuration is not a
      // failure — it previews the any-ledger rules.
      ledger = ledgers[0] ?? '';
    } catch {
      ledgers = [];
    }
    try {
      rules = (await getTransactionRules()).rules;
    } catch {
      rules = [];
    }
    ready = true;
  });

  function tagList(): string[] {
    return tags
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
  }

  async function run() {
    const mine = ++runSeq;
    busy = true;
    try {
      const body = await testTransactionRule({
        ledger,
        source,
        category: category.trim(),
        account: account.trim(),
        payee: payee.trim(),
        notes: notes.trim(),
        tags: tagList(),
      });
      if (mine !== runSeq) return;
      resolution = body.resolution;
      trace = body.trace;
      dropped = body.dropped;
      error = '';
    } catch (e) {
      if (mine !== runSeq) return;
      resolution = null;
      trace = [];
      dropped = [];
      // Reported in the banner below and deliberately not also as a notice.
      // This surface has somewhere in-band to put it, and a notice would
      // double-report the failure and then take the report away.
      // A 409 is not a failed preview: the deployment's one-time migration did
      // not complete, so an import still resolves from the legacy maps and
      // there is nothing honest to show.
      error =
        e instanceof ApiError && e.status === 409
          ? 'Transaction rules are not in force here yet — the one-time migration has not ' +
            'completed, so imports still resolve from the legacy Monarch maps.'
          : e instanceof Error
            ? e.message
            : 'Could not run the preview';
    } finally {
      if (mine === runSeq) busy = false;
    }
  }

  function outcomeOf(entry: RuleTraceEntry) {
    return OUTCOMES[entry.outcome] ?? { label: entry.outcome, variant: 'neutral' as const };
  }

  // Two outcomes read alike and are not: `shadowed` is "a higher-priority rule
  // already held this slot", `superseded_by_skip` is "this rule held it, and a
  // later skip then ended the pass and emptied it".
  function outcomeDetail(entry: RuleTraceEntry): string {
    if (entry.outcome === 'shadowed') {
      // `shadowed_by` is non-null exactly here by the route's contract, not by
      // the type — so a slip renders a sentence rather than "rule null".
      return entry.shadowed_by == null
        ? 'a higher-priority rule already filled this slot'
        : `rule ${entry.shadowed_by} already filled this slot`;
    }
    if (entry.outcome === 'superseded_by_skip') return 'the pass short-circuited on a later skip';
    if (entry.outcome === 'not_evaluated') return 'a skip ended the pass before this rule';
    if (entry.outcome === 'ignored') return 'this release has no slot for that action';
    return '';
  }

  function ruleSummary(entry: RuleTraceEntry): string {
    const target = entry.action === 'skip' ? 'skip' : `${entry.action} ${entry.target}`;
    return `${entry.field} ${entry.match_kind} "${entry.match_value}" → ${target}`;
  }
</script>

<SettingsCard title="Test a transaction">
  <p class="card-hint">
    A made-up transaction run through the ordered pass, against the enabled rules in this scope —
    the same set an import is scored against, so a disabled rule is absent here as it is there.
    Nothing is written.
  </p>

  {#if !ready}
    <p class="empty">Loading…</p>
  {:else}
    <div class="test-form control-row">
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Ledger</span>
        <Select bind:value={ledger} options={ledgerOptions} fullWidth ariaLabel="Preview ledger" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Source</span>
        <Select bind:value={source} options={sourceOptions} fullWidth ariaLabel="Preview source" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Category</span>
        <Input bind:value={category} placeholder="Software" aria-label="Category" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Account</span>
        <Input bind:value={account} placeholder="Chase Business" aria-label="Account" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Payee</span>
        <Input bind:value={payee} aria-label="Payee" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Notes</span>
        <Input bind:value={notes} aria-label="Notes" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Tags</span>
        <Input bind:value={tags} placeholder="one, two" aria-label="Tags" />
      </div>
      <div class="ctl-action">
        <Button
          variant="primary"
          size="sm"
          disabled={busy}
          loading={busy}
          loadingLabel="Running…"
          onclick={run}
        >
          Run
        </Button>
      </div>
    </div>

    {#if error}
      <div class="banner error">{error}</div>
    {/if}

    {#if resolution}
      <div class="result">
        <h4 class="micro-label">Resolution</h4>
        {#if resolution.skip}
          <p class="skip-line">
            A skip rule matched, so this transaction is not imported and nothing is posted.
          </p>
        {:else}
          <div class="res-row">
            <span class="micro-label">Posting account</span>
            {#if resolution.posting_account}
              <code class="res-value">{resolution.posting_account}</code>
            {:else}
              <span class="muted"
                >no rule filled this slot — falls back to Expenses:Uncategorized</span
              >
            {/if}
          </div>
          <div class="res-row">
            <span class="micro-label">Contra account</span>
            {#if resolution.contra_account}
              <code class="res-value">{resolution.contra_account}</code>
            {:else}
              <span class="muted">no rule filled this slot — falls back to the default account</span
              >
            {/if}
          </div>
        {/if}
        <p class="caption">{resolution.considered} rules evaluated.</p>

        {#if dropped.length > 0}
          <div class="banner warn">
            Skipped as uncompilable, so they did not run: rules {dropped.join(', ')}. Dropping a
            skip rule imports a transaction that was meant to be excluded.
          </div>
        {/if}

        <h4 class="micro-label trace-head">Trace</h4>
        <ul class="trace-list">
          {#each trace as entry (entry.rule_id)}
            {@const outcome = outcomeOf(entry)}
            <li class="trace-row">
              <span class="trace-rule">rule {entry.rule_id}</span>
              <span class="trace-prio muted">p{entry.priority}</span>
              <span class="trace-summary">{ruleSummary(entry)}</span>
              <Badge variant={outcome.variant}>{outcome.label}</Badge>
              {#if outcomeDetail(entry)}
                <span class="trace-detail muted">{outcomeDetail(entry)}</span>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</SettingsCard>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .test-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-3);
    margin-bottom: var(--space-3);
  }

  .ctl {
    display: grid;
    gap: var(--space-1);
    flex: 1 1 9rem;
    min-width: 0;
  }

  .ctl-action {
    display: flex;
    align-items: center;
    gap: var(--space-1);
  }

  .result {
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3);
  }

  .res-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2);
    padding: var(--space-1) 0;
    font-size: var(--text-sm);
  }

  .res-value {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
  }

  .skip-line {
    margin: 0 0 var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .trace-head {
    margin-top: var(--space-3);
  }

  .trace-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .trace-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-2);
    padding: var(--space-1) 0;
    font-size: var(--text-xs);
  }

  .trace-row + .trace-row {
    border-top: 1px solid var(--border-subtle);
  }

  .trace-rule {
    font-family: var(--font-mono);
    color: var(--text-primary);
  }

  .trace-summary {
    flex: 1 1 12rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
