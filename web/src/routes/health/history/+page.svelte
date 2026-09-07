<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    createDiagnosis,
    createEncounter,
    linkDiagnosisEncounter,
    listDiagnoses,
    listEncounters,
    type Diagnosis,
    type Encounter,
  } from '$lib/api';
  import { Button, DateRangeFilter, Field, Select, type SelectOption } from '$lib/components/ui';
  import {
    conditionOptionLabel,
    linkableConditionOptions,
    resolveById,
  } from '$lib/health/conditions';
  import { formatDate } from '$lib/dateFormat';

  // Suggested types — the server accepts any free-text encounter_type, so
  // these are just defaults for the dropdowns. Unknown types from the API
  // flow through `typeLabel` and a generic badge style.
  const ENCOUNTER_TYPES = [
    'visit',
    'procedure',
    'screening',
    'hospitalization',
    'er',
    'telehealth',
    'imaging',
    'dental',
    'other',
  ] as const;

  let loading = $state(true);
  let error = $state('');
  let encounters: Encounter[] = $state([]);
  // One fetch of every condition; the two panels below are slices of it, and
  // the create form's link picker needs the whole pool (resolved included).
  // `list_diagnoses` already sorts active → chronic → resolved, so filtering
  // preserves the order each panel used to get from its own request.
  let allDiagnoses: Diagnosis[] = $state([]);
  const active = $derived(allDiagnoses.filter((d) => d.status === 'active'));
  const chronic = $derived(allDiagnoses.filter((d) => d.status === 'chronic'));

  let typeFilter = $state('');
  let sinceFilter = $state('');
  let untilFilter = $state('');

  // Quick-add encounter form
  let formOpen = $state(false);
  let formDate = $state(new Date().toISOString().slice(0, 10));
  let formType = $state('visit');
  let formProvider = $state('');
  let formFacility = $state('');
  let formSpecialty = $state('');
  let formReason = $state('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');
  let formWarning = $state('');
  // Conditions staged for linking. The encounter does not exist yet, so these
  // are held until it does and then linked one PUT each.
  let formConditionIds: number[] = $state([]);
  let formConditionPick = $state('');

  const stagedConditions = $derived(resolveById(allDiagnoses, formConditionIds));

  const conditionOptions: SelectOption[] = $derived(
    linkableConditionOptions(allDiagnoses, formConditionIds),
  );

  function stageCondition(v: string) {
    const id = Number(v);
    if (!Number.isFinite(id) || formConditionIds.includes(id)) return;
    formConditionIds = [...formConditionIds, id];
    // The picker is a staging control, not a value: clearing it leaves the
    // placeholder showing so the next pick reads as another addition.
    formConditionPick = '';
  }

  function unstageCondition(id: number) {
    formConditionIds = formConditionIds.filter((x) => x !== id);
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const [encResp, diagResp] = await Promise.all([
        listEncounters({
          type: typeFilter || undefined,
          since: sinceFilter || undefined,
          until: untilFilter || undefined,
          limit: 200,
        }),
        listDiagnoses({ status: 'all', limit: 500 }),
      ]);
      encounters = encResp.encounters;
      allDiagnoses = diagResp.diagnoses;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load history';
    } finally {
      loading = false;
    }
  }

  async function submit(e: Event) {
    e.preventDefault();
    formError = '';
    formWarning = '';
    saving = true;
    try {
      const { id } = await createEncounter({
        encounter_date: formDate,
        encounter_type: formType,
        provider: formProvider || undefined,
        facility: formFacility || undefined,
        specialty: formSpecialty || undefined,
        reason: formReason || undefined,
        notes: formNotes || undefined,
      });

      // There is no server-side "create with diagnoses" call, so linking is one
      // request per condition after the fact. The encounter is already saved by
      // this point, so a link failure must not be reported as a failed save —
      // it is named separately and the form still closes.
      const failed: string[] = [];
      await Promise.all(
        stagedConditions.map(async (d) => {
          try {
            await linkDiagnosisEncounter(d.id, id);
          } catch {
            failed.push(d.name);
          }
        }),
      );
      if (failed.length) {
        formWarning = `Encounter saved, but could not link ${failed.join(', ')}. Link from the encounter page.`;
      }

      formProvider = '';
      formFacility = '';
      formSpecialty = '';
      formReason = '';
      formNotes = '';
      formConditionIds = [];
      formConditionPick = '';
      formOpen = false;
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  function typeLabel(t: string): string {
    const m: Record<string, string> = {
      visit: 'Visit',
      procedure: 'Procedure',
      screening: 'Screening',
      hospitalization: 'Hospital',
      er: 'ER',
      telehealth: 'Telehealth',
      imaging: 'Imaging',
      dental: 'Dental',
      other: 'Other',
    };
    if (m[t]) return m[t];
    // Unknown free-text type: title-case the first segment for display.
    if (!t) return 'Unknown';
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function typeBadgeClass(t: string): string {
    return (ENCOUNTER_TYPES as readonly string[]).includes(t) ? `type-${t}` : 'type-other';
  }

  // All types observed in the data plus the canonical list — used so the
  // <select> never silently switches to a value not in its options.
  const allTypes = $derived(
    Array.from(
      new Set<string>([
        ...ENCOUNTER_TYPES,
        ...encounters.map((e) => e.encounter_type).filter(Boolean),
      ]),
    ),
  );

  const formTypeOptions: SelectOption[] = ENCOUNTER_TYPES.map((t) => ({
    value: t,
    label: typeLabel(t),
  }));
  const typeFilterOptions: SelectOption[] = $derived([
    { value: '', label: 'All types' },
    ...allTypes.map((t) => ({ value: t, label: typeLabel(t) })),
  ]);

  onMount(load);
</script>

{#if !loading && !error}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <h1>Medical history</h1>
    <div class="actions">
      <Button onclick={() => (formOpen = !formOpen)}>
        {formOpen ? 'Cancel' : '+ Add encounter'}
      </Button>
      <Button href="{base}/health/history/import">Import</Button>
      <Button href="{base}/health/history/diagnoses">Conditions</Button>
    </div>
  </div>
{/if}

{#if formWarning}
  <!-- Outside the form: it reports on a save that already closed the form. -->
  <div class="banner warn">{formWarning}</div>
{/if}

{#if formOpen}
  <form class="quick-form" onsubmit={submit}>
    <div class="row">
      <Field label="Date">
        <input type="date" bind:value={formDate} required />
      </Field>
      <Field label="Type">
        <Select
          value={formType}
          options={formTypeOptions}
          onValueChange={(v) => (formType = v)}
          ariaLabel="Type"
          fullWidth
        />
      </Field>
      <Field label="Provider">
        <input type="text" bind:value={formProvider} placeholder="Dr. Smith" />
      </Field>
      <Field label="Facility">
        <input type="text" bind:value={formFacility} placeholder="Riverside Clinic" />
      </Field>
      <Field label="Specialty">
        <input type="text" bind:value={formSpecialty} placeholder="cardiology" />
      </Field>
    </div>
    <Field label="Reason" class="full">
      <input
        type="text"
        bind:value={formReason}
        placeholder="Chief complaint or reason for visit"
      />
    </Field>
    <Field label="Notes" class="full">
      <textarea bind:value={formNotes} rows="3" placeholder="Findings, follow-ups, …"></textarea>
    </Field>

    <div class="full conditions-field">
      <span class="field-label">Conditions</span>
      {#if stagedConditions.length}
        <ul class="staged">
          {#each stagedConditions as d (d.id)}
            <li>
              <span>{conditionOptionLabel(d)}</span>
              <button
                type="button"
                class="unstage"
                onclick={() => unstageCondition(d.id)}
                aria-label="Remove {d.name}">×</button
              >
            </li>
          {/each}
        </ul>
      {/if}
      <Select
        value={formConditionPick}
        options={conditionOptions}
        onValueChange={stageCondition}
        placeholder={conditionOptions.length
          ? 'Link an existing condition…'
          : 'No conditions to link'}
        disabled={conditionOptions.length === 0}
        ariaLabel="Link an existing condition"
        fullWidth
      />
    </div>

    {#if formError}
      <div class="banner error">{formError}</div>
    {/if}
    <div class="form-actions">
      <Button variant="primary" type="submit" loading={saving}>Save</Button>
    </div>
  </form>
{/if}

<div class="filter-bar control-row">
  <Select
    value={typeFilter}
    options={typeFilterOptions}
    onValueChange={(v) => {
      typeFilter = v;
      load();
    }}
    ariaLabel="Type filter"
    widthChars={9}
  />
  <DateRangeFilter bind:from={sinceFilter} bind:to={untilFilter} onChange={load} />
</div>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else}
  <div class="layout">
    <section class="timeline">
      {#if encounters.length === 0}
        <div class="empty">
          No encounters yet. Use <strong>+ Add encounter</strong> above to record one.
        </div>
      {:else}
        <ul>
          {#each encounters as e (e.id)}
            <li>
              <a class="card" href="{base}/health/history/encounter?id={e.id}">
                <div class="card-head">
                  <span class="badge {typeBadgeClass(e.encounter_type)}"
                    >{typeLabel(e.encounter_type)}</span
                  >
                  <span class="date">{formatDate(e.encounter_date)}</span>
                </div>
                <div class="card-body">
                  {#if e.provider || e.facility}
                    <div class="who">
                      {e.provider || ''}{e.provider && e.facility ? ' · ' : ''}{e.facility || ''}
                    </div>
                  {/if}
                  {#if e.specialty}
                    <div class="muted">{e.specialty}</div>
                  {/if}
                  {#if e.reason}
                    <div class="reason">{e.reason}</div>
                  {/if}
                </div>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <aside class="sidebar">
      <h2 class="micro-label">Active conditions</h2>
      {#if active.length === 0 && chronic.length === 0}
        <div class="empty small">No active conditions on file.</div>
      {:else}
        {#if active.length > 0}
          <ul class="conditions">
            {#each active as d (d.id)}
              <li>
                <a href="{base}/health/history/diagnoses">
                  <span class="name">{d.name}</span>
                  {#if d.severity}
                    <span class="severity sev-{d.severity}">{d.severity}</span>
                  {/if}
                </a>
              </li>
            {/each}
          </ul>
        {/if}
        {#if chronic.length > 0}
          <h3>Chronic</h3>
          <ul class="conditions">
            {#each chronic as d (d.id)}
              <li>
                <a href="{base}/health/history/diagnoses">
                  <span class="name">{d.name}</span>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </aside>
  </div>
{/if}

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  .actions {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  /* Three pill buttons plus the title do not fit a phone on one line, and
	   letting them wrap inside the flex row leaves them ragged against the
	   title. Give the row its own line instead. */
  @media (max-width: 768px) {
    .header {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
    }
    .actions {
      flex-wrap: wrap;
    }
    /* :global because the children are <Button>s now; Svelte prunes a rule
       whose subject it cannot see, and this one silently stopped applying. */
    .actions > :global(*) {
      flex: 1 1 auto;
      text-align: center;
    }
  }

  .quick-form {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .quick-form .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: var(--space-3);
  }
  .quick-form input,
  .quick-form textarea {
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
  .quick-form textarea {
    resize: vertical;
    font-family: inherit;
  }
  /* Not a <label>: the control is a Select whose trigger is a button, and
     wrapping it would make clicking the field name open the dropdown. */
  .conditions-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .conditions-field .field-label {
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
    /* A condition name can be a full clinical phrase — wrap inside the chip
       rather than letting one chip run past the page gutter on a phone. */
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

  /* The money section-tools pattern: a `control-row` so the Select and the
	   date inputs resolve one height and corner rather than each computing its
	   own, `nowrap` so the type filter and the range stay one line, and the
	   only flexible member giving ground. Without the tier the trigger sat a
	   third shorter than the inputs beside it; without nowrap the range broke
	   into a three-line column on a phone. */
  .filter-bar {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }

  /* :global because the subject is DateRangeFilter's root. Placement, not
	   paint: the range is what absorbs a narrow phone, and the Select holds the
	   width its own `widthChars` reserves. */
  .filter-bar :global(.date-range) {
    flex: 1 1 auto;
    min-width: 0;
  }

  .layout {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 1.25rem;
  }
  @media (max-width: 768px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }

  .timeline ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .card {
    display: block;
    padding: var(--space-3) var(--space-4);
    color: var(--text-primary);
  }
  .card:hover {
    border-color: var(--border-hover);
  }
  .card-head {
    align-items: center;
    margin-bottom: var(--space-2);
  }
  .card-body .who {
    font-weight: 500;
    font-size: var(--text-sm);
  }
  .card-body .muted {
    font-size: var(--text-xs);
    color: var(--text-muted);
    text-transform: lowercase;
  }
  .card-body .reason {
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  /* Badge palette tuned for the dark surface: each canonical encounter
	   type gets its own hue via HSLA so they share intensity. type-other
	   is the catch-all for unknown free-text types. */
  .badge {
    display: inline-flex;
    align-items: center;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.1rem var(--space-2);
    border-radius: var(--radius-pill);
    font-weight: 500;
  }
  /* design-lint-allow-begin: categorical palette — the hue names the encounter
     type (visit / procedure / imaging / dental), not a severity. Folding these
     onto the status scale would make a routine visit and a screening the same
     color. Each carries an explicit light value because the tinted fills are
     tuned per hue rather than derived from a token pair. */
  .badge.type-visit {
    background: hsla(210, 45%, 65%, 0.22);
    color: #b6ccea;
  }
  .badge.type-procedure {
    background: hsla(35, 60%, 60%, 0.22);
    color: #e6b96b;
  }
  .badge.type-screening {
    background: hsla(195, 50%, 60%, 0.22);
    color: #9cd5ea;
  }
  .badge.type-hospitalization {
    background: hsla(0, 55%, 60%, 0.25);
    color: #f0a09c;
  }
  .badge.type-er {
    background: hsla(0, 60%, 55%, 0.32);
    color: #ff9d96;
  }
  .badge.type-telehealth {
    background: hsla(145, 40%, 55%, 0.22);
    color: #9bd6a6;
  }
  .badge.type-imaging {
    background: hsla(280, 45%, 65%, 0.22);
    color: #d0aeec;
  }
  .badge.type-dental {
    background: hsla(185, 45%, 60%, 0.22);
    color: #95d2dc;
  }
  /* design-lint-allow-end */
  .badge.type-other {
    background: hsla(220, 8%, 60%, 0.18);
    color: var(--text-muted);
  }

  .date {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .sidebar {
    border-left: 1px solid var(--border-subtle);
    padding-left: 1.25rem;
  }
  @media (max-width: 768px) {
    .sidebar {
      border-left: none;
      border-top: 1px solid var(--border-subtle);
      padding-left: 0;
      padding-top: var(--space-4);
    }
  }
  .sidebar h2 {
    margin: 0 0 var(--space-2);
  }
  .sidebar h3 {
    margin: var(--space-3) 0 var(--space-2);
    font-size: var(--text-xs);
    font-weight: 500;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  /* Cards, not flat rows. A condition name is routinely long ("Bilateral
	   patellofemoral pain syndrome with anterior knee crepitus") and the old
	   single-line row ellipsised it away rather than wrapping. `auto-fill` with
	   a `min()` floor keeps one column inside the 280px desktop sidebar and
	   goes multi-column once the sidebar becomes full width under 768px —
	   without the floor a name wider than the track would push the grid past
	   the viewport. */
  .conditions {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(200px, 100%), 1fr));
    gap: var(--space-2);
  }
  /* Equal heights only where cards actually sit beside each other. Above the
	   breakpoint this is the 280px sidebar — a single column, where `1fr` rows
	   would stretch every card to match the longest condition name in the list
	   and leave most of them mostly empty. Below it the sidebar is full width
	   and goes multi-column, where a ragged bottom edge across a row reads as
	   damage. */
  @media (max-width: 768px) {
    .conditions {
      grid-auto-rows: 1fr;
    }
  }
  .conditions li {
    min-width: 0;
    /* The grid stretches the <li>, but the <a> inside it is height:auto and
		   would sit short inside a stretched cell — so the card would still
		   render at its own content height. Make the li a flex parent and let
		   the anchor fill it. */
    display: flex;
  }
  .conditions li a {
    flex: 1;
    box-sizing: border-box;
    /* Name on its own line with the severity chip under it. Side by side, a
		   long name and a chip competed for one 200px track, and the chip's
		   width varied per card ("mild" vs "moderate") so no two names got the
		   same wrap point. */
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    text-decoration: none;
    color: var(--text-primary);
    font-size: var(--text-sm);
  }
  .conditions li a:hover {
    border-color: var(--border-hover);
  }
  .conditions .name {
    min-width: 0;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }
  /* Metrics copied from the admin dashboard's .effort-chip / .admin-badge so
	   the small identity chips read as one family across the app. */
  .severity {
    display: inline-block;
    flex: 0 0 auto;
    padding: 0.05rem var(--space-2);
    font-size: 0.55rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-radius: var(--radius-pill);
  }
  .severity.sev-mild {
    background: hsla(145, 40%, 55%, 0.18);
    color: var(--status-success-fg);
  }
  .severity.sev-moderate {
    background: hsla(35, 60%, 60%, 0.22);
    color: var(--status-warn-fg);
  }
  .severity.sev-severe {
    background: hsla(0, 60%, 55%, 0.28);
    color: var(--status-danger-fg);
  }

  /* design-lint-allow-begin: light half of the categorical encounter-type
     palette above. */
  :global(:root[data-theme='light']) .badge.type-visit {
    color: #2563b0;
  }
  :global(:root[data-theme='light']) .badge.type-procedure {
    color: #946a00;
  }
  :global(:root[data-theme='light']) .badge.type-screening {
    color: #2563b0;
  }
  :global(:root[data-theme='light']) .badge.type-hospitalization {
    color: #c0271d;
  }
  :global(:root[data-theme='light']) .badge.type-er {
    color: #c0271d;
  }
  :global(:root[data-theme='light']) .badge.type-telehealth {
    color: #15803d;
  }
  :global(:root[data-theme='light']) .badge.type-imaging {
    color: #7c3aed;
  }
  :global(:root[data-theme='light']) .badge.type-dental {
    color: #0d8f7e;
  }
  /* design-lint-allow-end */
</style>
