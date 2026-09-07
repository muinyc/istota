<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    createDiagnosis,
    deleteDiagnosis,
    listDiagnoses,
    listEncounters,
    updateDiagnosis,
    type Diagnosis,
    type Encounter,
  } from '$lib/api';
  import {
    Button,
    ConfirmDialog,
    Field,
    KebabMenu,
    Modal,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import {
    encounterOptionLabel,
    linkableEncounterOptions,
    resolveById,
  } from '$lib/health/conditions';
  import DocumentList from '$lib/components/health/DocumentList.svelte';
  import { getShellScrollRoot } from '$lib/components/ui/AppShell.svelte';
  import { Paperclip } from 'lucide-svelte';
  import { formatDate } from '$lib/dateFormat';

  const getScrollRoot = getShellScrollRoot();

  const statusOptions: SelectOption[] = [
    { value: 'active', label: 'Active' },
    { value: 'chronic', label: 'Chronic' },
    { value: 'resolved', label: 'Resolved' },
  ];
  const severityOptions: SelectOption[] = [
    { value: '', label: '—' },
    { value: 'mild', label: 'Mild' },
    { value: 'moderate', label: 'Moderate' },
    { value: 'severe', label: 'Severe' },
  ];

  let loading = $state(true);
  let error = $state('');
  let diagnoses: Diagnosis[] = $state([]);
  // Conditions have no detail page, so their documents open in a modal.
  let documentsFor: Diagnosis | null = $state(null);
  let encounters: Encounter[] = $state([]);

  let showResolved = $state(false);

  // Add / edit form. `editing` is the record being edited; null = adding.
  let formOpen = $state(false);
  let editing: Diagnosis | null = $state(null);
  let formName = $state('');
  let formStatus = $state<'active' | 'chronic' | 'resolved'>('active');
  let formIcd10 = $state('');
  let formDateDiagnosed = $state(new Date().toISOString().slice(0, 10));
  let formDateResolved = $state('');
  // A condition is seen at several visits, so this is a set the form builds up
  // rather than a single value.
  let formEncounterIds = $state<number[]>([]);
  let formEncounterPick = $state('');
  let formSeverity = $state<'' | 'mild' | 'moderate' | 'severe'>('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');
  let deleteTarget: Diagnosis | null = $state(null);

  async function load() {
    loading = true;
    error = '';
    try {
      const [allResp, encResp] = await Promise.all([
        listDiagnoses({ status: 'all', limit: 500 }),
        listEncounters({ limit: 100 }),
      ]);
      diagnoses = allResp.diagnoses;
      encounters = encResp.encounters;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load diagnoses';
    } finally {
      loading = false;
    }
  }

  const active = $derived(diagnoses.filter((d) => d.status === 'active'));
  const chronic = $derived(diagnoses.filter((d) => d.status === 'chronic'));
  const resolved = $derived(diagnoses.filter((d) => d.status === 'resolved'));

  const encounterLabelFor = (e: Encounter) => encounterOptionLabel(e, formatDate, (t) => t);

  const encounterOptions: SelectOption[] = $derived(
    linkableEncounterOptions(encounters, formEncounterIds, encounterLabelFor),
  );

  const stagedEncounters = $derived(resolveById(encounters, formEncounterIds));

  function stageEncounter(v: string) {
    const id = Number(v);
    if (!Number.isFinite(id) || formEncounterIds.includes(id)) return;
    formEncounterIds = [...formEncounterIds, id];
    formEncounterPick = '';
  }

  function unstageEncounter(id: number) {
    formEncounterIds = formEncounterIds.filter((x) => x !== id);
  }

  function resetForm() {
    editing = null;
    formName = '';
    formStatus = 'active';
    formIcd10 = '';
    formDateDiagnosed = new Date().toISOString().slice(0, 10);
    formDateResolved = '';
    formEncounterIds = [];
    formEncounterPick = '';
    formSeverity = '';
    formNotes = '';
    formError = '';
  }

  function toggleForm() {
    if (formOpen) {
      formOpen = false;
      resetForm();
    } else {
      resetForm();
      formOpen = true;
    }
  }

  function startEdit(d: Diagnosis) {
    editing = d;
    formName = d.name;
    formStatus = d.status;
    formIcd10 = d.icd10 ?? '';
    formDateDiagnosed = d.date_diagnosed ?? '';
    formDateResolved = d.date_resolved ?? '';
    formEncounterIds = [...d.encounter_ids];
    formEncounterPick = '';
    formSeverity = d.severity ?? '';
    formNotes = d.notes ?? '';
    formError = '';
    formOpen = true;
    // The form lives at the top of the page, so a kebab clicked from a card
    // further down would otherwise open it out of view.
    getScrollRoot?.()?.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function submit(e: Event) {
    e.preventDefault();
    formError = '';
    saving = true;
    try {
      if (editing) {
        // Send explicit nulls rather than dropping the key: the API clears a
        // field only when it is present and null, so an emptied ICD-10 has to
        // be sent to actually take. `encounter_ids` is likewise a full
        // replacement, so an emptied list clears the links.
        await updateDiagnosis(editing.id, {
          name: formName,
          status: formStatus,
          icd10: formIcd10 || null,
          date_diagnosed: formDateDiagnosed || null,
          date_resolved: formStatus === 'resolved' ? formDateResolved || null : null,
          encounter_ids: formEncounterIds,
          severity: formSeverity || null,
          notes: formNotes || null,
        });
      } else {
        await createDiagnosis({
          name: formName,
          status: formStatus,
          icd10: formIcd10 || undefined,
          date_diagnosed: formDateDiagnosed || undefined,
          date_resolved: formDateResolved || undefined,
          encounter_ids: formEncounterIds,
          severity: formSeverity || undefined,
          notes: formNotes || undefined,
        });
      }
      formOpen = false;
      resetForm();
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  // The status-transition action differs per section (active → resolve,
  // resolved → reactivate, chronic → neither); Delete is common to all three.
  function diagnosisMenu(d: Diagnosis, transition: 'resolve' | 'reactivate' | null): KebabItem[] {
    const items: KebabItem[] = [{ label: 'Edit', onSelect: () => startEdit(d) }];
    if (transition === 'resolve') {
      items.push({ label: 'Resolve', onSelect: () => void resolveOne(d) });
    } else if (transition === 'reactivate') {
      items.push({ label: 'Reactivate', onSelect: () => void reactivate(d) });
    }
    items.push({ label: 'Documents', onSelect: () => (documentsFor = d) });
    items.push({ label: 'Delete', danger: true, onSelect: () => (deleteTarget = d) });
    return items;
  }

  async function resolveOne(d: Diagnosis) {
    try {
      await updateDiagnosis(d.id, {
        status: 'resolved',
        date_resolved: new Date().toISOString().slice(0, 10),
      });
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to resolve';
    }
  }

  async function reactivate(d: Diagnosis) {
    try {
      await updateDiagnosis(d.id, { status: 'active', date_resolved: null });
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to reactivate';
    }
  }

  async function confirmDeletion() {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    deleteTarget = null;
    try {
      await deleteDiagnosis(id);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete';
    }
  }

  function encounterLabel(id: number | null): string {
    if (!id) return '';
    const e = encounters.find((x) => x.id === id);
    if (!e) return `#${id}`;
    return `${formatDate(e.encounter_date)} · ${e.encounter_type}`;
  }

  onMount(load);
</script>

{#if !loading && !error}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <div>
      <a class="back" href="{base}/health/history">← Medical history</a>
      <h1>Conditions</h1>
    </div>
    <Button onclick={toggleForm}>
      {formOpen ? 'Cancel' : '+ Add diagnosis'}
    </Button>
  </div>
{/if}

{#if formOpen}
  <form class="form" onsubmit={submit}>
    {#if editing}
      <div class="form-title">Editing <strong>{editing.name}</strong></div>
    {/if}
    <div class="row">
      <Field label="Name *" class="full">
        <input type="text" bind:value={formName} required placeholder="e.g. Hypertension" />
      </Field>
      <Field label="Status">
        <Select
          value={formStatus}
          options={statusOptions}
          onValueChange={(v) => (formStatus = v as 'active' | 'chronic' | 'resolved')}
          ariaLabel="Status"
          fullWidth
        />
      </Field>
      <Field label="ICD-10">
        <input type="text" bind:value={formIcd10} placeholder="K64.0" />
      </Field>
      <Field label="Severity">
        <Select
          value={formSeverity}
          options={severityOptions}
          onValueChange={(v) => (formSeverity = v as '' | 'mild' | 'moderate' | 'severe')}
          ariaLabel="Severity"
          fullWidth
        />
      </Field>
      <Field label="Date diagnosed">
        <input type="date" bind:value={formDateDiagnosed} />
      </Field>
      {#if formStatus === 'resolved'}
        <Field label="Date resolved">
          <input type="date" bind:value={formDateResolved} />
        </Field>
      {/if}
    </div>
    <!-- Not a <label>: the control is a Select whose trigger is a button, and
         wrapping it would make clicking the field name open the dropdown. -->
    <div class="full encounters-field">
      <span class="field-label">Linked encounters</span>
      {#if stagedEncounters.length}
        <ul class="staged">
          {#each stagedEncounters as e (e.id)}
            <li>
              <span>{encounterLabelFor(e)}</span>
              <button
                type="button"
                class="unstage"
                onclick={() => unstageEncounter(e.id)}
                aria-label="Remove {encounterLabelFor(e)}">×</button
              >
            </li>
          {/each}
        </ul>
      {/if}
      <Select
        value={formEncounterPick}
        options={encounterOptions}
        onValueChange={stageEncounter}
        placeholder={encounterOptions.length ? 'Link an encounter…' : 'No encounters to link'}
        disabled={encounterOptions.length === 0}
        ariaLabel="Link an encounter"
        fullWidth
      />
    </div>
    <Field label="Notes" class="full">
      <textarea bind:value={formNotes} rows="3"></textarea>
    </Field>
    {#if formError}
      <div class="banner error">{formError}</div>
    {/if}
    <div class="form-actions">
      <Button variant="primary" type="submit" disabled={saving}>
        {saving ? 'Saving…' : editing ? 'Save changes' : 'Save'}
      </Button>
    </div>
  </form>
{/if}

{#snippet conditionCard(d: Diagnosis, transition: 'resolve' | 'reactivate' | null)}
  <li>
    <!-- The kebab is a sibling of the name rather than of the whole card body,
         so it pins to the top-right corner however tall the card grows. -->
    <div class="card-head">
      <h3 class="name">{d.name}</h3>
      <KebabMenu items={diagnosisMenu(d, transition)} ariaLabel="Diagnosis actions" />
    </div>
    {#if d.icd10 || d.severity || (d.document_count ?? 0) > 0}
      <div class="tags">
        {#if d.icd10}<span class="icd">{d.icd10}</span>{/if}
        {#if d.severity}<span class="sev sev-{d.severity}">{d.severity}</span>{/if}
        {#if (d.document_count ?? 0) > 0}
          <button
            type="button"
            class="docs"
            onclick={() => (documentsFor = d)}
            title="{d.document_count} attached document{d.document_count === 1 ? '' : 's'}"
          >
            <Paperclip size={11} aria-hidden="true" />
            {d.document_count}
          </button>
        {/if}
      </div>
    {/if}
    <div class="d-meta">
      {#if transition === 'reactivate'}
        {#if d.date_resolved}<span>Resolved {formatDate(d.date_resolved)}</span>{/if}
      {:else}
        {#if d.date_diagnosed}<span>Dx {formatDate(d.date_diagnosed)}</span>{/if}
        <!-- Every visit this condition was seen at, not just the first. -->
        {#each d.encounter_ids as eid (eid)}
          <a href="{base}/health/history/encounter?id={eid}" class="enc">
            {encounterLabel(eid)}
          </a>
        {/each}
      {/if}
    </div>
    {#if d.notes}<p class="notes">{d.notes}</p>{/if}
  </li>
{/snippet}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else}
  <section>
    <h2 class="micro-label">Active <span class="count">{active.length}</span></h2>
    {#if active.length === 0}
      <div class="empty small">No active conditions on file.</div>
    {:else}
      <ul class="list">
        {#each active as d (d.id)}
          {@render conditionCard(d, 'resolve')}
        {/each}
      </ul>
    {/if}
  </section>

  <section>
    <h2 class="micro-label">Chronic <span class="count">{chronic.length}</span></h2>
    {#if chronic.length === 0}
      <div class="empty small">No chronic conditions on file.</div>
    {:else}
      <ul class="list">
        {#each chronic as d (d.id)}
          {@render conditionCard(d, null)}
        {/each}
      </ul>
    {/if}
  </section>

  <section>
    <h2 class="micro-label">
      Resolved <span class="count">{resolved.length}</span>
      <button class="toggle" type="button" onclick={() => (showResolved = !showResolved)}>
        {showResolved ? 'hide' : 'show'}
      </button>
    </h2>
    {#if showResolved}
      {#if resolved.length === 0}
        <div class="empty small">Nothing resolved yet.</div>
      {:else}
        <ul class="list resolved">
          {#each resolved as d (d.id)}
            {@render conditionCard(d, 'reactivate')}
          {/each}
        </ul>
      {/if}
    {/if}
  </section>
{/if}

{#if documentsFor}
  <Modal
    open={true}
    title="Documents — {documentsFor.name}"
    width="42rem"
    onOpenChange={(open) => {
      if (!open) {
        documentsFor = null;
        // The paperclip counts come from the list endpoint, so a change made
        // in here has to be reflected on the cards behind it.
        void load();
      }
    }}
  >
    <DocumentList entityType="diagnosis" entityId={documentsFor.id} autoload />
  </Modal>
{/if}

{#if deleteTarget}
  <ConfirmDialog
    open={true}
    title="Delete diagnosis"
    confirmLabel="Delete"
    onConfirm={confirmDeletion}
    onCancel={() => (deleteTarget = null)}
  >
    {#snippet body()}
      <p>
        Are you sure you want to delete <strong>{deleteTarget?.name}</strong>? Permanently removes
        it from your history. This cannot be undone.
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }
  .back {
    display: inline-block;
    font-size: var(--text-xs);
    color: var(--text-muted);
    text-decoration: none;
    margin-bottom: var(--space-1);
  }
  .back:hover {
    text-decoration: underline;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  h2 {
    margin: var(--space-6) 0 var(--space-2);
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .count {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-weight: 400;
    letter-spacing: 0;
    text-transform: none;
  }
  .toggle {
    margin-left: auto;
    font-size: var(--text-xs);
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    padding: 0;
    text-transform: none;
    letter-spacing: 0;
  }
  .toggle:hover {
    color: var(--text-primary);
    text-decoration: underline;
  }

  .form {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .form-title {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .form-title strong {
    color: var(--text-primary);
    font-weight: 500;
  }
  .form .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: var(--space-3);
  }
  .form :global(.field.full) {
    grid-column: 1 / -1;
  }
  .form input,
  .form textarea {
    padding: var(--space-1) var(--space-2);
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    box-sizing: border-box;
    min-width: 0;
  }
  .form textarea {
    resize: vertical;
    font-family: inherit;
  }
  .encounters-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .encounters-field .field-label {
    color: var(--text-muted);
    font-size: var(--text-xs);
  }
  .staged {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .staged li {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    padding: 0.1rem var(--space-1) 0.1rem 0.55rem;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    font-size: var(--text-xs);
    /* An encounter label carries date, type and provider — wrap inside the
       chip rather than running past the page gutter on a phone. */
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }
  .staged li span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .unstage {
    background: none;
    border: none;
    padding: 0 0.15rem;
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-sm);
    line-height: 1;
    cursor: pointer;
  }
  .unstage:hover {
    color: var(--text-primary);
  }

  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-2);
  }
  @media (max-width: 1100px) {
    .list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (max-width: 768px) {
    .list {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .list li {
    padding: var(--space-3) var(--space-4);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .list.resolved li {
    opacity: 0.7;
  }
  /* flex-start: a long diagnosis name wraps to a second line. */
  .card-head {
    align-items: flex-start;
  }
  .name {
    margin: 0;
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-primary);
    line-height: 1.35;
    /* Long condition names wrap rather than pushing the kebab out of the
       corner. */
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .tags {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .icd {
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-raised);
    padding: 0.05rem var(--space-2);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
  }
  .sev {
    display: inline-flex;
    align-items: center;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.05rem var(--space-2);
    border-radius: var(--radius-pill);
    font-weight: 500;
  }
  .sev-mild {
    background: hsla(145, 40%, 55%, 0.18);
    color: var(--status-success-fg);
  }
  .sev-moderate {
    background: hsla(35, 60%, 60%, 0.22);
    color: var(--status-warn-fg);
  }
  .sev-severe {
    background: hsla(0, 60%, 55%, 0.28);
    color: var(--status-danger-fg);
  }
  .d-meta {
    /* Date and source stay on one line together, directly under the tags —
       the card's content stacks from the top rather than being spread to fill
       the (stretched) card height. */
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    font-size: var(--text-xs);
    color: var(--text-dim);
    flex-wrap: wrap;
  }
  .d-meta .enc {
    color: var(--text-muted);
    text-decoration: none;
  }
  .d-meta .enc:hover {
    color: var(--accent-blue);
    text-decoration: underline;
  }
  .notes {
    margin: 0;
    font-size: var(--text-sm);
    white-space: pre-wrap;
    color: var(--text-muted);
    line-height: 1.45;
  }

  .docs {
    /* A button does not inherit font, so without this the em-relative
       sizing resolves against the UA default and stops tracking the
       text-scale preference. */
    font: inherit;
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-raised);
    border: none;
    padding: 0.05rem var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .docs:hover {
    color: var(--accent-blue);
  }
</style>
