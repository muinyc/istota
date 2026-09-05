<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Badge, Button, ConfirmDialog, Input, KebabMenu, Select } from '$lib/components/ui';
  import { notifyError, notifyInfo, notifySuccess } from '$lib/stores/notices';
  import {
    createTransactionRule,
    deleteTransactionRule,
    getLedgers,
    getTransactionRules,
    updateTransactionRule,
    type NewTransactionRule,
    type TransactionRule,
  } from '$lib/money/api';
  import {
    ALL_SCOPES,
    UNSET_SCOPE,
    isPickedScope,
    ledgerScopeOptions,
    scopeLabel,
    scopeQuery,
    sourceScopeOptions,
  } from '$lib/money/ruleScopes';

  // The ordered rule list for one scope, and the forms that write it. Rules
  // are rendered in the order the store returned them — (priority, id), which
  // is evaluation order — and never re-sorted here: the section's claim is
  // that the list is the pass, so a list sorted on anything else would be a
  // different statement about the same rows.
  //
  // Reordering is by editing the priority number rather than by dragging. A
  // drag needs a bulk-write endpoint, a stable index and an answer for a
  // concurrent edit; the number is one field on a form that already exists,
  // and it is visible, which a drag order is not.
  //
  // Add and the inline edit are per-record forms, so they keep their own
  // buttons — the app-bar Save is for page-level state.

  const FIELDS = ['category', 'account', 'payee', 'notes', 'tag'];
  const MATCH_KINDS = ['exact', 'iexact', 'contains'];
  const ACTIONS = ['posting_account', 'contra_account', 'skip'];

  const ACTION_LABELS: Record<string, string> = {
    posting_account: 'post to',
    contra_account: 'contra',
    skip: 'skip',
  };

  const fieldOptions = FIELDS.map((v) => ({ value: v, label: v }));
  const kindOptions = MATCH_KINDS.map((v) => ({ value: v, label: v }));
  const actionOptions = ACTIONS.map((v) => ({ value: v, label: ACTION_LABELS[v] }));
  const enabledOptions = [
    { value: 'true', label: 'Enabled' },
    { value: 'false', label: 'Disabled' },
  ];

  let rules: TransactionRule[] = $state([]);
  // Every rule, whatever the filter — the scope vocabulary has to include the
  // ones the filter is currently hiding, or a rule stranded at a renamed
  // ledger could never be selected back into view.
  let allRules: TransactionRule[] = $state([]);
  let ledgers: string[] = $state([]);
  let loaded = $state(false);
  let loadError = $state('');

  let filterLedger = $state(ALL_SCOPES);
  let filterSource = $state(ALL_SCOPES);

  let addLedger = $state(UNSET_SCOPE);
  let addSource = $state(UNSET_SCOPE);
  let addField = $state('category');
  let addKind = $state('iexact');
  let addValue = $state('');
  let addAction = $state('posting_account');
  let addTarget = $state('');
  let addPriority = $state('100');
  let addBusy = $state(false);

  let editingId: number | null = $state(null);
  let editLedger = $state('');
  let editSource = $state('');
  let editField = $state('category');
  let editKind = $state('iexact');
  let editValue = $state('');
  let editAction = $state('posting_account');
  let editTarget = $state('');
  let editPriority = $state('100');
  let editEnabled = $state('true');
  let editBusy = $state(false);

  let confirmDelete: TransactionRule | null = $state(null);

  const filterLedgerOptions = $derived(ledgerScopeOptions(ledgers, allRules, { all: true }));
  const filterSourceOptions = $derived(sourceScopeOptions(allRules, { all: true }));
  const writeLedgerOptions = $derived(ledgerScopeOptions(ledgers, allRules));
  const writeSourceOptions = $derived(sourceScopeOptions(allRules));

  const scopeChosen = $derived(isPickedScope(addLedger) && isPickedScope(addSource));
  const canAdd = $derived(
    scopeChosen &&
      addValue.trim() !== '' &&
      (addAction === 'skip' || addTarget.trim() !== '') &&
      !addBusy,
  );

  function priorityOf(raw: string, fallback: number): number {
    const parsed = Number.parseInt(raw, 10);
    return Number.isNaN(parsed) ? fallback : parsed;
  }

  async function load() {
    try {
      // The unscoped read first: it is the vocabulary, and the filtered read
      // below cannot supply it.
      const all = await getTransactionRules();
      allRules = all.rules;
      const query = scopeQuery(filterLedger, filterSource);
      rules = Object.keys(query).length > 0 ? (await getTransactionRules(query)).rules : allRules;
      loadError = '';
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load transaction rules';
    } finally {
      loaded = true;
    }
  }

  onMount(async () => {
    // A ledger list that cannot be read costs the picker its configured
    // entries and nothing else — the rules still carry their own scopes.
    try {
      ledgers = await getLedgers();
    } catch {
      ledgers = [];
    }
    await load();
  });

  // Picking a scope to look at is also the scope a new rule most likely
  // belongs in, so the add form follows the filter — but only when the filter
  // names one. "All scopes" leaves the add form unset, because the widest
  // scope has to be chosen rather than fallen into.
  function onFilterLedger(value: string) {
    if (isPickedScope(value)) addLedger = value;
    load();
  }

  function onFilterSource(value: string) {
    if (isPickedScope(value)) addSource = value;
    load();
  }

  function resetAdd() {
    addValue = '';
    addTarget = '';
  }

  async function add() {
    if (!canAdd) return;
    addBusy = true;
    try {
      const body: NewTransactionRule = {
        ledger: addLedger,
        source: addSource,
        field: addField as NewTransactionRule['field'],
        match_kind: addKind as NewTransactionRule['match_kind'],
        match_value: addValue.trim(),
        action: addAction as NewTransactionRule['action'],
        target: addAction === 'skip' ? '' : addTarget.trim(),
        priority: priorityOf(addPriority, 100),
        origin: 'user',
      };
      await createTransactionRule(body);
      notifySuccess('Rule added', { key: 'money:transaction-rule' });
      resetAdd();
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not add the rule', {
        key: 'money:transaction-rule',
      });
    } finally {
      addBusy = false;
    }
  }

  function startEdit(rule: TransactionRule) {
    editingId = rule.id;
    editLedger = rule.ledger;
    editSource = rule.source;
    editField = rule.field;
    editKind = rule.match_kind;
    editValue = rule.match_value;
    editAction = rule.action;
    editTarget = rule.target;
    editPriority = String(rule.priority);
    editEnabled = rule.enabled ? 'true' : 'false';
  }

  /**
   * Only what the form actually changed.
   *
   * The route merges a partial onto the stored row, so sending the untouched
   * fields back is not rejected — it silently overwrites whatever the CLI or
   * the agent changed in between.
   */
  function editPatch(rule: TransactionRule): Partial<NewTransactionRule> {
    const patch: Partial<NewTransactionRule> = {};
    const target = editAction === 'skip' ? '' : editTarget.trim();
    const priority = priorityOf(editPriority, rule.priority);
    const enabled = editEnabled === 'true';
    if (editLedger !== rule.ledger) patch.ledger = editLedger;
    if (editSource !== rule.source) patch.source = editSource;
    if (editField !== rule.field) patch.field = editField as NewTransactionRule['field'];
    if (editKind !== rule.match_kind) {
      patch.match_kind = editKind as NewTransactionRule['match_kind'];
    }
    if (editValue.trim() !== rule.match_value) patch.match_value = editValue.trim();
    if (editAction !== rule.action) patch.action = editAction as NewTransactionRule['action'];
    if (target !== rule.target) patch.target = target;
    if (priority !== rule.priority) patch.priority = priority;
    if (enabled !== rule.enabled) patch.enabled = enabled;
    return patch;
  }

  async function saveEdit(rule: TransactionRule) {
    const patch = editPatch(rule);
    if (Object.keys(patch).length === 0) {
      editingId = null;
      return;
    }
    editBusy = true;
    try {
      await updateTransactionRule(rule.id, patch);
      notifySuccess(`Saved rule ${rule.id}`, { key: 'money:transaction-rule' });
      editingId = null;
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not save the rule', {
        key: 'money:transaction-rule',
      });
    } finally {
      editBusy = false;
    }
  }

  // A copy of the same scope, match and action is refused by the unique index,
  // so a duplicate is only useful once the user has changed something. It
  // fills the add form and waits there rather than posting.
  function duplicate(rule: TransactionRule) {
    addLedger = rule.ledger;
    addSource = rule.source;
    addField = rule.field;
    addKind = rule.match_kind;
    addValue = rule.match_value;
    addAction = rule.action;
    addTarget = rule.target;
    addPriority = String(rule.priority);
    notifyInfo('Change the copy before adding it — an identical rule is refused', {
      key: 'money:transaction-rule',
    });
  }

  async function handleDelete() {
    const rule = confirmDelete;
    confirmDelete = null;
    if (!rule) return;
    try {
      await deleteTransactionRule(rule.id);
      notifySuccess(`Removed rule ${rule.id}`, { key: 'money:transaction-rule' });
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not remove the rule', {
        key: 'money:transaction-rule',
      });
    }
  }

  function menu(rule: TransactionRule) {
    return [
      { label: 'Edit', onSelect: () => startEdit(rule) },
      { label: 'Duplicate', onSelect: () => duplicate(rule) },
      { label: 'Delete', danger: true, onSelect: () => (confirmDelete = rule) },
    ];
  }

  function outcomeOf(rule: TransactionRule): string {
    return rule.action === 'skip' ? 'skip' : `${ACTION_LABELS[rule.action]} ${rule.target}`;
  }
</script>

<SettingsCard title="Rules ({rules.length})">
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {:else}
    <p class="card-hint">
      One ordered pass per imported transaction, low priority first. A rule fills its slot only if
      that slot is still empty, so first match wins per slot and a <code>skip</code> ends the pass.
      An empty slot falls back to <code>Expenses:Uncategorized</code> and the profile's default account.
    </p>

    <div class="scope-row control-row">
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Ledger</span>
        <Select
          bind:value={filterLedger}
          options={filterLedgerOptions}
          fullWidth
          ariaLabel="Filter by ledger"
          onValueChange={onFilterLedger}
        />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Source</span>
        <Select
          bind:value={filterSource}
          options={filterSourceOptions}
          fullWidth
          ariaLabel="Filter by source"
          onValueChange={onFilterSource}
        />
      </div>
    </div>

    <div class="rule-form add-form control-row">
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Ledger</span>
        <Select
          bind:value={addLedger}
          options={writeLedgerOptions}
          placeholder="Pick…"
          fullWidth
          ariaLabel="Rule ledger"
        />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Source</span>
        <Select
          bind:value={addSource}
          options={writeSourceOptions}
          placeholder="Pick…"
          fullWidth
          ariaLabel="Rule source"
        />
      </div>
      <div class="ctl ctl-narrow">
        <span class="micro-label" aria-hidden="true">Field</span>
        <Select bind:value={addField} options={fieldOptions} fullWidth ariaLabel="Rule field" />
      </div>
      <div class="ctl ctl-narrow">
        <span class="micro-label" aria-hidden="true">Match</span>
        <Select bind:value={addKind} options={kindOptions} fullWidth ariaLabel="Rule match kind" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Value</span>
        <Input bind:value={addValue} placeholder="Software" aria-label="Match value" />
      </div>
      <div class="ctl ctl-narrow">
        <span class="micro-label" aria-hidden="true">Action</span>
        <Select bind:value={addAction} options={actionOptions} fullWidth ariaLabel="Rule action" />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Target</span>
        <Input
          bind:value={addTarget}
          placeholder="Expenses:Business:Software"
          monospace
          disabled={addAction === 'skip'}
          aria-label="Target account"
        />
      </div>
      <div class="ctl ctl-tiny">
        <span class="micro-label" aria-hidden="true">Priority</span>
        <Input bind:value={addPriority} type="number" aria-label="Rule priority" />
      </div>
      <div class="ctl-action">
        <Button
          variant="primary"
          size="sm"
          disabled={!canAdd}
          loading={addBusy}
          loadingLabel="Adding…"
          onclick={add}
        >
          Add rule
        </Button>
      </div>
    </div>
    {#if !scopeChosen}
      <p class="caption scope-note">
        Pick a ledger and a source for the rule. Both accept <em>any</em>, which is the widest scope
        there is — so it is chosen rather than left blank.
      </p>
    {/if}

    {#if loaded && rules.length === 0}
      <p class="empty">No rules in this scope yet.</p>
    {:else}
      <ul class="rule-list">
        {#each rules as rule (rule.id)}
          <li class="rule-row" class:off={!rule.enabled}>
            {#if editingId === rule.id}
              <div class="rule-form control-row">
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Ledger</span>
                  <Select
                    bind:value={editLedger}
                    options={writeLedgerOptions}
                    fullWidth
                    ariaLabel="Edit ledger"
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Source</span>
                  <Select
                    bind:value={editSource}
                    options={writeSourceOptions}
                    fullWidth
                    ariaLabel="Edit source"
                  />
                </div>
                <div class="ctl ctl-narrow">
                  <span class="micro-label" aria-hidden="true">Field</span>
                  <Select
                    bind:value={editField}
                    options={fieldOptions}
                    fullWidth
                    ariaLabel="Edit field"
                  />
                </div>
                <div class="ctl ctl-narrow">
                  <span class="micro-label" aria-hidden="true">Match</span>
                  <Select
                    bind:value={editKind}
                    options={kindOptions}
                    fullWidth
                    ariaLabel="Edit match kind"
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Value</span>
                  <Input bind:value={editValue} aria-label="Edit match value" />
                </div>
                <div class="ctl ctl-narrow">
                  <span class="micro-label" aria-hidden="true">Action</span>
                  <Select
                    bind:value={editAction}
                    options={actionOptions}
                    fullWidth
                    ariaLabel="Edit action"
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Target</span>
                  <Input
                    bind:value={editTarget}
                    monospace
                    disabled={editAction === 'skip'}
                    aria-label="Edit target account"
                  />
                </div>
                <div class="ctl ctl-tiny">
                  <span class="micro-label" aria-hidden="true">Priority</span>
                  <Input bind:value={editPriority} type="number" aria-label="Priority" />
                </div>
                <div class="ctl ctl-narrow">
                  <span class="micro-label" aria-hidden="true">State</span>
                  <Select
                    bind:value={editEnabled}
                    options={enabledOptions}
                    fullWidth
                    ariaLabel="Enabled"
                  />
                </div>
                <div class="ctl-action">
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={editBusy}
                    loading={editBusy}
                    onclick={() => saveEdit(rule)}
                  >
                    Save
                  </Button>
                  <Button variant="ghost" size="sm" onclick={() => (editingId = null)}>
                    Cancel
                  </Button>
                </div>
              </div>
            {:else}
              <div class="rule-line">
                <span class="rule-prio" title="Priority">{rule.priority}</span>
                <span class="rule-desc">
                  <span class="muted">{rule.field} {rule.match_kind}</span>
                  <code class="rule-value">{rule.match_value}</code>
                  <span class="muted">→</span>
                  <span class="rule-target">{outcomeOf(rule)}</span>
                  <span class="rule-scope muted">{scopeLabel(rule.ledger, rule.source)}</span>
                </span>
                {#if rule.origin}
                  <Badge variant={rule.origin === 'seed' ? 'neutral' : 'info'}>{rule.origin}</Badge>
                {/if}
                {#if !rule.enabled}
                  <Badge variant="warn">disabled</Badge>
                {/if}
                <KebabMenu items={menu(rule)} ariaLabel="Actions for rule {rule.id}" />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</SettingsCard>

<ConfirmDialog
  open={confirmDelete !== null}
  title="Delete rule"
  message="Are you sure you want to delete rule {confirmDelete?.id}? Transactions it matched will resolve from whatever rule is next in the pass, or fall back to Expenses:Uncategorized."
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={handleDelete}
  onCancel={() => (confirmDelete = null)}
/>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .scope-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
    margin-bottom: var(--space-3);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
  }

  .rule-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-3);
  }

  .add-form {
    margin-bottom: var(--space-2);
  }

  .scope-note {
    margin: 0 0 var(--space-3);
  }

  .ctl {
    display: grid;
    gap: var(--space-1);
    flex: 1 1 9rem;
    min-width: 0;
  }

  .ctl-narrow {
    flex: 0 1 7rem;
  }

  .ctl-tiny {
    flex: 0 1 5rem;
  }

  .ctl-action {
    display: flex;
    align-items: center;
    gap: var(--space-1);
  }

  .rule-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .rule-row {
    padding: var(--space-2) 0;
  }

  .rule-row + .rule-row {
    border-top: 1px solid var(--border-subtle);
  }

  /* A disabled rule stays in the list — the editor has to show it — and reads
     as inert rather than being hidden, since only an import ignores it. */
  .rule-row.off .rule-desc {
    opacity: 0.6;
  }

  .rule-line {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .rule-prio {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-muted);
    min-width: 2.5rem;
    text-align: right;
  }

  .rule-desc {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-2);
    flex: 1 1 auto;
    min-width: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .rule-value {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
  }

  .rule-target {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
  }

  .rule-scope {
    font-size: var(--text-xs);
  }
</style>
