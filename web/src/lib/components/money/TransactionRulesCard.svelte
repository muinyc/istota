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
    isOfferedScope,
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
  // Distinct from "this deployment configures no ledgers": with both empty the
  // only scope on offer is the wildcard one, so a rule written here would go
  // to every ledger — the outcome the unset sentinel exists to prevent, and
  // the user has to be told which of the two states they are in.
  let ledgersFailed = $state(false);
  let loaded = $state(false);
  // Whether a load has *ever* succeeded, which is a different question from
  // whether one has finished: it is what decides between "show the banner
  // instead of the card" and "show the banner above a card that still works".
  let everLoaded = $state(false);
  let loadError = $state('');

  // Every load takes a ticket and only the newest one may write. Two picks in
  // quick succession are not two equivalent round trips: a filtered load makes
  // two sequential requests and an unfiltered one makes a single request, so
  // the *later* pick routinely resolves first and the earlier one then lands
  // on top of it, leaving the list scoped to a filter the pickers no longer
  // show.
  let loadSeq = 0;

  let filterLedger = $state(ALL_SCOPES);
  let filterSource = $state(ALL_SCOPES);

  let addLedger = $state(UNSET_SCOPE);
  let addSource = $state(UNSET_SCOPE);
  let addField = $state('category');
  let addKind = $state('iexact');
  let addValue = $state('');
  let addAction = $state('posting_account');
  let addTarget = $state('');
  // Seeded as a string; a bound number input replaces it with a number.
  let addPriority: string | number = $state('100');
  let addBusy = $state(false);

  let editingId: number | null = $state(null);
  let editLedger = $state('');
  let editSource = $state('');
  let editField = $state('category');
  let editKind = $state('iexact');
  let editValue = $state('');
  let editAction = $state('posting_account');
  let editTarget = $state('');
  let editPriority: string | number = $state('100');
  let editEnabled = $state('true');
  let editBusy = $state(false);

  let confirmDelete: TransactionRule | null = $state(null);

  const filterLedgerOptions = $derived(ledgerScopeOptions(ledgers, allRules, { all: true }));
  const filterSourceOptions = $derived(sourceScopeOptions(allRules, { all: true }));
  // `keep` is what stops a write form rendering its placeholder where a scope
  // is actually set: the union deduplicates ledgers case-insensitively and
  // lets the configured spelling win, so a rule stored as `Personal` against a
  // `personal` config has no option of its own until its own value is added.
  const addLedgerOptions = $derived(ledgerScopeOptions(ledgers, allRules, { keep: addLedger }));
  const addSourceOptions = $derived(sourceScopeOptions(allRules, { keep: addSource }));
  const editLedgerOptions = $derived(ledgerScopeOptions(ledgers, allRules, { keep: editLedger }));
  const editSourceOptions = $derived(sourceScopeOptions(allRules, { keep: editSource }));

  const scopeChosen = $derived(isPickedScope(addLedger) && isPickedScope(addSource));
  // A filtered list is not the set an import is scored against: the store
  // matches one scope, while the engine also applies every ''-scoped rule. The
  // filter is the only place that gap is visible, so it is stated there rather
  // than folding ~50 seeded rows into a list the user did not write them into.
  const scopeFiltered = $derived(isPickedScope(filterLedger) || isPickedScope(filterSource));
  const canAdd = $derived(
    scopeChosen &&
      addValue.trim() !== '' &&
      (addAction === 'skip' || addTarget.trim() !== '') &&
      isIntegerPriority(addPriority) &&
      !addBusy,
  );

  // The edit form gets the same guard as the add form. Without it, clearing
  // the match value or switching a `skip` to `posting_account` without a
  // target posts a body the store rejects, and the user is told so by a
  // transient notice naming no field.
  const canSaveEdit = $derived(
    editValue.trim() !== '' &&
      (editAction === 'skip' || editTarget.trim() !== '') &&
      isIntegerPriority(editPriority) &&
      !editBusy,
  );

  // `Number.parseInt` alone accepts values a number input offers and then
  // silently changes them: '1e3' parses to 1 and '1.9' to 1. An empty field is
  // worse, since it becomes a plausible 100 the user never picked.
  //
  // Both take `string | number` because that is what a bound number input
  // actually produces: the state is seeded with a string and Svelte's binding
  // hands back a number once the field is typed into, so a `raw.trim()` here
  // throws inside the deriveds below and leaves Save disabled for ever. It is
  // invisible on the add form, whose default is never retyped in most flows.
  function isIntegerPriority(raw: string | number): boolean {
    return /^-?\d+$/.test(String(raw).trim());
  }

  function priorityOf(raw: string | number, fallback: number): number {
    return isIntegerPriority(raw) ? Number.parseInt(String(raw).trim(), 10) : fallback;
  }

  async function load() {
    const mine = ++loadSeq;
    try {
      // The unscoped read first: it is the vocabulary, and the filtered read
      // below cannot supply it.
      const all = await getTransactionRules();
      const query = scopeQuery(filterLedger, filterSource);
      const scoped =
        Object.keys(query).length > 0 ? (await getTransactionRules(query)).rules : all.rules;
      if (mine !== loadSeq) return;
      allRules = all.rules;
      rules = scoped;
      loadError = '';
      everLoaded = true;
      dropVanishedFilters();
    } catch (e) {
      if (mine !== loadSeq) return;
      loadError = e instanceof Error ? e.message : 'Failed to load transaction rules';
    } finally {
      if (mine === loadSeq) loaded = true;
    }
  }

  /**
   * A filter naming a scope the picker no longer offers is returned to "all".
   *
   * The vocabulary includes every ledger a rule names, so filtering to a scope
   * that exists only because one rule sits there — the renamed-ledger case the
   * union is for — and then deleting or moving that rule drops the option
   * while the filter still holds its value. `Select` renders its placeholder
   * for a value it cannot find, so the trigger would read "Select…" while the
   * query kept sending that ledger and the list stayed permanently empty.
   */
  function dropVanishedFilters() {
    if (!isOfferedScope(filterLedger, filterLedgerOptions)) filterLedger = ALL_SCOPES;
    if (!isOfferedScope(filterSource, filterSourceOptions)) filterSource = ALL_SCOPES;
  }

  onMount(async () => {
    // A ledger list that cannot be read costs the picker its configured
    // entries and nothing else — the rules still carry their own scopes.
    try {
      ledgers = await getLedgers();
      ledgersFailed = false;
    } catch {
      ledgers = [];
      ledgersFailed = true;
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
    // Compared untrimmed and *sent* trimmed. Comparing the trimmed value would
    // make an untouched form dirty for any row the CLI or the agent stored
    // with surrounding whitespace, so opening the editor and pressing Save
    // would rewrite a rule nobody edited.
    if (editValue !== rule.match_value) patch.match_value = editValue.trim();
    if (editAction !== rule.action) patch.action = editAction as NewTransactionRule['action'];
    if (target !== rule.target) patch.target = target;
    if (priority !== rule.priority) patch.priority = priority;
    if (enabled !== rule.enabled) patch.enabled = enabled;
    return patch;
  }

  async function saveEdit(rule: TransactionRule) {
    if (!canSaveEdit) return;
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

  // The unique index is case-sensitive on `ledger` while the engine and this
  // list are not, so changing only a ledger's case produces a second row the
  // store accepts and the import treats as the same scope. Said here rather
  // than only in the notice, which has no room for it.
  function scopeCaseWarning(): string {
    return 'A ledger differing only in case is a second rule the store accepts and an import treats as the same scope.';
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

  // `action` is typed closed but the wire is not: a hand-edited row can carry
  // anything, and the preview's `ignored` outcome exists for exactly that. An
  // unrecognized action renders as itself rather than as `undefined`.
  function outcomeOf(rule: TransactionRule): string {
    if (rule.action === 'skip') return 'skip';
    return `${ACTION_LABELS[rule.action] ?? rule.action} ${rule.target}`;
  }
</script>

<SettingsCard title="Rules ({rules.length})">
  <!-- The banner sits above the controls rather than replacing them. A failed
       *reload* used to wipe the card to a banner, taking the scope pickers
       with it, so nothing on screen could trigger another load and only a page
       reload got the section back. -->
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {/if}
  {#if everLoaded || !loadError}
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
          options={addLedgerOptions}
          placeholder="Pick…"
          fullWidth
          ariaLabel="Rule ledger"
        />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Source</span>
        <Select
          bind:value={addSource}
          options={addSourceOptions}
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
        <Input
          bind:value={addPriority}
          type="number"
          min="0"
          max="9999"
          step="1"
          invalid={!isIntegerPriority(addPriority)}
          aria-label="Rule priority"
        />
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
        {#if ledgersFailed}
          The configured ledgers could not be read, so only scopes already in use are offered.
        {/if}
      </p>
    {/if}
    {#if scopeFiltered}
      <p class="caption scope-note">
        Filtered to one scope. Rules written at <em>any ledger</em> or <em>any source</em> also apply
        to it and are not listed here — switch the filter to see them.
      </p>
    {/if}

    {#if loaded && rules.length === 0}
      {#if !loadError}
        <p class="empty">No rules in this scope yet.</p>
      {/if}
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
                    options={editLedgerOptions}
                    fullWidth
                    ariaLabel="Edit ledger"
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Source</span>
                  <Select
                    bind:value={editSource}
                    options={editSourceOptions}
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
                  <Input
                    bind:value={editValue}
                    invalid={editValue.trim() === ''}
                    aria-label="Edit match value"
                  />
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
                    invalid={editAction !== 'skip' && editTarget.trim() === ''}
                    aria-label="Edit target account"
                  />
                </div>
                <div class="ctl ctl-tiny">
                  <span class="micro-label" aria-hidden="true">Priority</span>
                  <Input
                    bind:value={editPriority}
                    type="number"
                    min="0"
                    max="9999"
                    step="1"
                    invalid={!isIntegerPriority(editPriority)}
                    aria-label="Priority"
                  />
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
                    disabled={!canSaveEdit}
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
