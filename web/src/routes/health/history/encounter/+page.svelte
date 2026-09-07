<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import {
    deleteEncounter,
    getEncounter,
    linkDiagnosisEncounter,
    listDiagnoses,
    unlinkDiagnosisEncounter,
    updateEncounter,
    type Diagnosis,
    type Encounter,
    type HealthDocument,
    type HealthPanel,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    Field,
    KebabMenu,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import { diagnosisStatusVariant } from '$lib/health/status';
  import { linkableConditionOptions } from '$lib/health/conditions';
  import DocumentList from '$lib/components/health/DocumentList.svelte';
  import { formatDate } from '$lib/dateFormat';

  let loading = $state(true);
  let error = $state('');
  let saving = $state(false);
  let encounter: Encounter | null = $state(null);
  let diagnoses: Diagnosis[] = $state([]);
  let panels: HealthPanel[] = $state([]);
  let documents: HealthDocument[] = $state([]);

  let editing = $state(false);
  let form: Partial<Encounter> = $state({});
  let confirmDelete = $state(false);

  // Condition linking. The pool is every condition, not just the ones on this
  // encounter, so it is loaded separately from `getEncounter`.
  let allDiagnoses: Diagnosis[] = $state([]);
  let linkPick = $state('');
  let linking = $state(false);
  let linkError = $state('');
  let unlinkTarget: Diagnosis | null = $state(null);
  let confirmUnlink = $state(false);

  const CANONICAL_TYPES = [
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

  function typeLabel(t: string | null | undefined): string {
    if (!t) return '';
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
    return m[t] ?? t.charAt(0).toUpperCase() + t.slice(1);
  }

  // Make sure the current encounter_type is always selectable so Svelte's
  // bind doesn't silently switch a free-text type to the first option.
  const editTypeOptions = $derived.by(() => {
    const current = (form.encounter_type ?? '') as string;
    const opts = [...CANONICAL_TYPES] as string[];
    if (current && !opts.includes(current)) opts.unshift(current);
    return opts;
  });

  const editTypeSelectOptions: SelectOption[] = $derived(
    editTypeOptions.map((t) => ({ value: t, label: typeLabel(t) })),
  );

  const encounterId = $derived.by(() => {
    const raw = page.url.searchParams.get('id');
    const n = raw ? Number(raw) : NaN;
    return Number.isFinite(n) ? n : null;
  });

  async function load() {
    if (encounterId === null) {
      error = 'Missing encounter id';
      loading = false;
      return;
    }
    loading = true;
    error = '';
    try {
      const resp = await getEncounter(encounterId);
      encounter = resp.encounter;
      diagnoses = resp.diagnoses;
      panels = resp.panels;
      documents = resp.documents ?? [];
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load encounter';
    } finally {
      loading = false;
    }
  }

  /**
   * The pool for the link picker.
   *
   * Deliberately non-fatal and separate from `load()`: an encounter that
   * renders without its picker is still usable, so a failure here must not
   * replace the page with an error.
   */
  async function loadConditions() {
    try {
      const resp = await listDiagnoses({ status: 'all', limit: 500 });
      allDiagnoses = resp.diagnoses;
    } catch {
      allDiagnoses = [];
    }
  }

  // A condition already linked to some *other* encounter is still offered:
  // that is the point of the many-to-many. Only what is on THIS encounter is
  // excluded, so the picker never offers a no-op.
  const linkOptions: SelectOption[] = $derived(
    linkableConditionOptions(
      allDiagnoses,
      diagnoses.map((d) => d.id),
    ),
  );

  async function linkCondition() {
    if (!linkPick || encounterId === null) return;
    linking = true;
    linkError = '';
    try {
      await linkDiagnosisEncounter(Number(linkPick), encounterId);
      linkPick = '';
      await Promise.all([load(), loadConditions()]);
    } catch (e) {
      linkError = e instanceof Error ? e.message : 'Failed to link condition';
    } finally {
      linking = false;
    }
  }

  async function unlinkCondition(d: Diagnosis) {
    if (encounterId === null) return;
    linking = true;
    linkError = '';
    try {
      await unlinkDiagnosisEncounter(d.id, encounterId);
      await Promise.all([load(), loadConditions()]);
    } catch (e) {
      linkError = e instanceof Error ? e.message : 'Failed to unlink condition';
    } finally {
      linking = false;
      unlinkTarget = null;
      confirmUnlink = false;
    }
  }

  function conditionMenu(d: Diagnosis): KebabItem[] {
    return [
      { label: 'Open conditions', href: `${base}/health/history/diagnoses` },
      {
        label: 'Unlink from this encounter',
        onSelect: () => {
          unlinkTarget = d;
          confirmUnlink = true;
        },
      },
    ];
  }

  function startEdit() {
    if (!encounter) return;
    form = { ...encounter };
    editing = true;
  }

  async function save(e: Event) {
    e.preventDefault();
    if (encounterId === null) return;
    saving = true;
    try {
      await updateEncounter(encounterId, form);
      editing = false;
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function destroy() {
    if (encounterId === null) return;
    try {
      await deleteEncounter(encounterId);
      goto(`${base}/health/history`);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete';
      confirmDelete = false;
    }
  }

  onMount(loadConditions);
  onMount(load);
  $effect(() => {
    encounterId;
    load();
  });
</script>

{#if !loading && !error}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <div class="header">
    <div>
      <a class="back" href="{base}/health/history">← Medical history</a>
      <h1>Encounter</h1>
    </div>
    {#if encounter && !editing}
      <div class="actions">
        <Button onclick={startEdit}>Edit</Button>
        <Button variant="danger" onclick={() => (confirmDelete = true)}>Delete</Button>
      </div>
    {/if}
  </div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{:else if encounter}
  {#if editing}
    <form class="form" onsubmit={save}>
      <div class="row">
        <Field label="Date">
          <input type="date" bind:value={form.encounter_date} required />
        </Field>
        <Field label="Type">
          <Select
            value={form.encounter_type ?? ''}
            options={editTypeSelectOptions}
            onValueChange={(v) => (form.encounter_type = v)}
            ariaLabel="Type"
            fullWidth
          />
        </Field>
        <Field label="Provider">
          <input type="text" bind:value={form.provider} />
        </Field>
        <Field label="Facility">
          <input type="text" bind:value={form.facility} />
        </Field>
        <Field label="Specialty">
          <input type="text" bind:value={form.specialty} />
        </Field>
      </div>
      <Field label="Reason" class="full">
        <input type="text" bind:value={form.reason} />
      </Field>
      <Field label="Notes" class="full">
        <textarea bind:value={form.notes} rows="5"></textarea>
      </Field>
      <div class="form-actions">
        <Button onclick={() => (editing = false)}>Cancel</Button>
        <Button variant="primary" type="submit" loading={saving}>Save</Button>
      </div>
    </form>
  {:else}
    <section class="meta">
      <dl>
        <div>
          <dt>Date</dt>
          <dd>{formatDate(encounter.encounter_date)}</dd>
        </div>
        <div>
          <dt>Type</dt>
          <dd>{typeLabel(encounter.encounter_type)}</dd>
        </div>
        {#if encounter.provider}
          <div>
            <dt>Provider</dt>
            <dd>{encounter.provider}</dd>
          </div>
        {/if}
        {#if encounter.facility}
          <div>
            <dt>Facility</dt>
            <dd>{encounter.facility}</dd>
          </div>
        {/if}
        {#if encounter.specialty}
          <div>
            <dt>Specialty</dt>
            <dd>{encounter.specialty}</dd>
          </div>
        {/if}
        {#if encounter.reason}
          <div>
            <dt>Reason</dt>
            <dd>{encounter.reason}</dd>
          </div>
        {/if}
      </dl>
    </section>

    {#if encounter.notes}
      <section class="notes">
        <h2 class="micro-label">Notes</h2>
        <p>{encounter.notes}</p>
      </section>
    {/if}
  {/if}

  <section class="related">
    <h2 class="micro-label">Linked diagnoses</h2>
    {#if diagnoses.length === 0}
      <div class="empty small">None.</div>
    {:else}
      <!-- Same card shape as the conditions list: name, then tags on their own
           line, then the date line. Unlike the panels grid below, the card is
           not itself a link — the kebab lives inside it, and a nested
           interactive element inside an anchor is not addressable. -->
      <ul class="card-grid dx-grid">
        {#each diagnoses as d (d.id)}
          <li class="dx-card">
            <div class="card-head">
              <a class="name" href="{base}/health/history/diagnoses">{d.name}</a>
              <KebabMenu items={conditionMenu(d)} ariaLabel="Condition actions" />
            </div>
            <div class="tags">
              {#if d.icd10}<span class="icd">{d.icd10}</span>{/if}
              <Badge variant={diagnosisStatusVariant(d.status)}>{d.status}</Badge>
            </div>
            <div class="card-meta">
              {#if d.date_diagnosed}<span>Dx {formatDate(d.date_diagnosed)}</span>{/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="link-row">
      <Select
        value={linkPick}
        options={linkOptions}
        onValueChange={(v) => (linkPick = v)}
        placeholder={linkOptions.length ? 'Link an existing condition…' : 'No conditions to link'}
        disabled={linking || linkOptions.length === 0}
        ariaLabel="Link an existing condition"
        size="md"
      />
      <Button
        onclick={linkCondition}
        disabled={!linkPick || linking}
        loading={linking}
        loadingLabel="Linking…">Link</Button
      >
    </div>
    {#if linkError}
      <div class="banner error">{linkError}</div>
    {/if}
  </section>

  <section class="related">
    <h2 class="micro-label">Linked panels</h2>
    {#if panels.length === 0}
      <div class="empty small">None.</div>
    {:else}
      <ul class="card-grid">
        {#each panels as p (p.id)}
          <li>
            <a href="{base}/health/bloodwork/panel?id={p.id}">
              <h3 class="name">{formatDate(p.drawn_at)}</h3>
              <div class="tags">
                <span class="count-tag">
                  {p.biomarker_count} marker{p.biomarker_count === 1 ? '' : 's'}
                </span>
              </div>
              <div class="card-meta">
                {#if p.lab_name}<span>{p.lab_name}</span>{/if}
              </div>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="related">
    <h2 class="micro-label">Documents</h2>
    {#if encounter}
      <DocumentList entityType="encounter" entityId={encounter.id} {documents} />
    {/if}
  </section>
{/if}

<ConfirmDialog
  bind:open={confirmDelete}
  title="Delete encounter"
  message="Are you sure you want to delete this encounter? Linked panels, diagnoses and documents keep their data but lose the link."
  confirmLabel="Delete"
  onConfirm={destroy}
/>

<!-- Unlinking keeps the condition, so this takes the primary confirm rather
     than the red one: it is the "did you mean that" gate a document detach
     gets, not a destructive action. -->
<ConfirmDialog
  bind:open={confirmUnlink}
  title="Unlink condition"
  message={`Are you sure you want to unlink ${unlinkTarget?.name ?? 'this condition'} from this encounter? The condition is kept, along with its links to any other encounters.`}
  confirmLabel="Unlink"
  confirmVariant="primary"
  onConfirm={() => unlinkTarget && unlinkCondition(unlinkTarget)}
  onCancel={() => (unlinkTarget = null)}
/>

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
    margin: 1.25rem 0 var(--space-2);
  }

  .actions {
    display: flex;
    gap: var(--space-2);
  }

  .meta {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
  }
  .meta dl {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(200px, 100%), 1fr));
    gap: var(--space-2) var(--space-6);
    margin: 0;
  }
  .meta dt {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin-bottom: 0.15rem;
  }
  .meta dd {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .notes {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    margin-top: var(--space-4);
  }
  .notes h2 {
    margin-top: 0;
  }
  .notes p {
    white-space: pre-wrap;
    line-height: 1.5;
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  /* Three across on desktop, two on a narrow window, one on a phone —
     matching the conditions list. */
  .card-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-2);
  }
  @media (max-width: 1100px) {
    .card-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (max-width: 768px) {
    .card-grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  /* Direct child only: the diagnoses cards below hold their name in a nested
     anchor beside a kebab, and must not each pick up the card chrome. */
  .card-grid li > a {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    height: 100%;
    box-sizing: border-box;
    padding: var(--space-3) var(--space-4);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    text-decoration: none;
    color: var(--text-primary);
    min-width: 0;
  }
  .card-grid li > a:hover {
    border-color: var(--border-hover);
  }
  .card-grid .name {
    margin: 0;
    font-size: var(--text-sm);
    font-weight: 500;
    line-height: 1.35;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .card-grid .tags {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .card-grid .card-meta {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    font-size: var(--text-xs);
    color: var(--text-dim);
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
  .count-tag {
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-raised);
    padding: 0.05rem var(--space-2);
    border-radius: var(--radius-sm);
  }

  .form {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
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
  /* The diagnoses grid keeps the shared grid geometry but moves the card
     chrome from the anchor onto the <li>, since the card holds a kebab
     alongside the name rather than being one big link. */
  .dx-grid li.dx-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    height: 100%;
    box-sizing: border-box;
    padding: var(--space-3) var(--space-4);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    min-width: 0;
  }
  .dx-grid li.dx-card:hover {
    border-color: var(--border-hover);
  }
  .dx-card .card-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-2);
  }
  .dx-card .name {
    color: var(--text-primary);
    text-decoration: none;
  }
  .dx-card .name:hover {
    text-decoration: underline;
  }

  .link-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-top: var(--space-3);
  }
</style>
