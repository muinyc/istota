<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    deleteHealthPanel,
    getHealthPanel,
    healthPanelSourceUrl,
    listEncounters,
    saveHealthBiomarkers,
    updateHealthPanel,
    type Biomarker,
    type Encounter,
    type HealthPanel,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    Field,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { formatDate } from '$lib/dateFormat';

  // Read the panel id from ?id=… so the page is statically prerenderable
  // under adapter-static; the actual lookup happens client-side.
  let id = $derived(Number(page.url.searchParams.get('id') ?? 0));

  let loading = $state(true);
  let error = $state('');
  let info = $state('');
  let panel: HealthPanel | null = $state(null);
  let biomarkers: Biomarker[] = $state([]);
  let source = $state({ available: false, mime: null as string | null });

  let editing = $state(false);
  let saving = $state(false);
  let confirmDelete = $state(false);

  // Header field edits — populated when entering edit mode so Cancel can
  // discard cleanly without re-fetching.
  let editDrawnAt = $state('');
  let editLabName = $state('');
  let editPanelType = $state('');
  // `''` means "no link"; numeric string is an encounter id. <select> binds
  // to strings, so we round-trip through string for clean change detection.
  let editEncounterId = $state('');

  let encounters: Encounter[] = $state([]);
  let encountersLoaded = $state(false);

  function startEditing() {
    if (!panel) return;
    // Truncate any time-of-day portion for the <input type="date">.
    editDrawnAt = (panel.drawn_at || '').slice(0, 10);
    editLabName = panel.lab_name || '';
    editPanelType = panel.panel_type || '';
    editEncounterId = panel.encounter_id == null ? '' : String(panel.encounter_id);
    editing = true;
    loadEncounters();
  }

  async function loadEncounters() {
    if (encountersLoaded) return;
    try {
      const resp = await listEncounters({ limit: 200 });
      encounters = resp.encounters;
      encountersLoaded = true;
    } catch {
      // Non-fatal; the select will just be empty.
    }
  }

  function encounterLabel(e: Encounter): string {
    const parts = [e.encounter_date, e.encounter_type];
    if (e.provider) parts.push(e.provider);
    else if (e.facility) parts.push(e.facility);
    return parts.join(' · ');
  }

  const encounterOptions: SelectOption[] = $derived([
    { value: '', label: '— Not linked —' },
    ...encounters.map((e) => ({ value: String(e.id), label: encounterLabel(e) })),
  ]);

  const linkedEncounter: Encounter | null = $derived.by(() => {
    const p = panel;
    if (!p || p.encounter_id == null) return null;
    return encounters.find((e) => e.id === p.encounter_id) ?? null;
  });

  async function load() {
    loading = true;
    error = '';
    try {
      const resp = await getHealthPanel(id);
      panel = resp.panel;
      biomarkers = [...resp.biomarkers];
      source = resp.source;
      // Fetch encounters in the background so the read-mode label can
      // resolve. Cheap; the list is small.
      void loadEncounters();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load panel';
    } finally {
      loading = false;
    }
  }

  async function save(confirmDraft: boolean) {
    if (!panel) return;
    saving = true;
    error = '';
    info = '';
    try {
      // Only send header fields that actually changed; the API treats
      // an explicit empty string as "set to null", which we want for
      // the user clearing lab/panel_type but not as an accidental wipe.
      const headerPatch: Record<string, unknown> = {};
      if (editDrawnAt && editDrawnAt !== (panel.drawn_at || '').slice(0, 10)) {
        headerPatch.drawn_at = editDrawnAt;
      }
      if (editLabName !== (panel.lab_name || '')) {
        headerPatch.lab_name = editLabName;
      }
      if (editPanelType !== (panel.panel_type || '')) {
        headerPatch.panel_type = editPanelType;
      }
      const newEncounterId = editEncounterId === '' ? null : Number(editEncounterId);
      if (newEncounterId !== (panel.encounter_id ?? null)) {
        headerPatch.encounter_id = newEncounterId;
      }
      if (Object.keys(headerPatch).length > 0) {
        await updateHealthPanel(id, headerPatch);
      }

      const payload = biomarkers.map((b) => ({
        name: b.name,
        display_name: b.display_name ?? undefined,
        value: Number(b.value),
        unit: b.unit,
        ref_range_low: b.ref_range_low ?? undefined,
        ref_range_high: b.ref_range_high ?? undefined,
        flag: b.flag ?? undefined,
      }));
      await saveHealthBiomarkers(id, payload, confirmDraft);
      info = confirmDraft ? 'Saved + confirmed.' : 'Saved.';
      editing = false;
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function confirmDraftOnly() {
    try {
      await updateHealthPanel(id, { draft: false });
      info = 'Panel confirmed.';
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to confirm';
    }
  }

  async function deletePanel() {
    try {
      await deleteHealthPanel(id);
      goto(`${base}/health/bloodwork`);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete';
      confirmDelete = false;
    }
  }

  function addRow() {
    biomarkers = [
      ...biomarkers,
      {
        id: -Date.now(),
        panel_id: id,
        name: '',
        display_name: null,
        value: 0,
        unit: '',
        ref_range_low: null,
        ref_range_high: null,
        flag: null,
      },
    ];
  }

  function removeRow(index: number) {
    biomarkers = biomarkers.filter((_, i) => i !== index);
  }

  onMount(load);
</script>

<div class="page">
  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error && !panel}
    <div class="center-msg error">{error}</div>
  {:else if panel}
    <div class="header">
      <div class="header-meta">
        <a href="{base}/health/bloodwork" class="back">← Bloodwork</a>
        {#if editing}
          <div class="header-edit">
            <Field label="Date drawn">
              <input type="date" bind:value={editDrawnAt} />
            </Field>
            <Field label="Lab">
              <input type="text" bind:value={editLabName} placeholder="Quest, Kaiser, …" />
            </Field>
            <Field label="Panel type">
              <input type="text" bind:value={editPanelType} placeholder="CBC, CMP, Lipid, …" />
            </Field>
            <label class="full-row">
              <span>Linked encounter</span>
              <Select
                value={editEncounterId}
                options={encounterOptions}
                onValueChange={(v) => (editEncounterId = v)}
                ariaLabel="Linked encounter"
                fullWidth
              />
            </label>
          </div>
        {:else}
          <h1>
            {formatDate(panel.drawn_at)}
            <span class="lab">· {panel.lab_name || 'Unknown lab'}</span>
            {#if panel.panel_type}<span class="type">· {panel.panel_type}</span>{/if}
          </h1>
          {#if panel.encounter_id != null}
            <div class="encounter-link">
              <span class="encounter-label">Linked to encounter:</span>
              <a href="{base}/health/history/encounter?id={panel.encounter_id}">
                {linkedEncounter ? encounterLabel(linkedEncounter) : `#${panel.encounter_id}`}
              </a>
            </div>
          {/if}
        {/if}
        {#if panel.draft}<Badge variant="warn">Draft — review and confirm</Badge>{/if}
      </div>
      <div class="actions">
        {#if !editing}
          <Button onclick={startEditing}>Edit panel</Button>
          {#if panel.draft}
            <Button variant="primary" onclick={confirmDraftOnly}>Confirm</Button>
          {/if}
        {:else}
          <Button onclick={() => (editing = false)} disabled={saving}>Cancel</Button>
          <Button variant="primary" onclick={() => save(true)} disabled={saving}>
            {saving ? 'Saving…' : 'Save + confirm'}
          </Button>
        {/if}
        <Button variant="danger" onclick={() => (confirmDelete = true)}>Delete</Button>
      </div>
    </div>

    {#if info}<div class="banner info">{info}</div>{/if}
    {#if error && panel}<div class="banner error">{error}</div>{/if}

    <div class="split">
      <div class="biomarker-table">
        <table class="grid grid--dense">
          <thead>
            <tr>
              <th>Marker</th>
              <th>Value</th>
              <th>Unit</th>
              <th>Lab range</th>
              <th>Flag</th>
              {#if editing}<th></th>{/if}
            </tr>
          </thead>
          <tbody>
            {#each biomarkers as b, i (b.id)}
              <tr class:flag-row={b.flag}>
                <td>
                  {#if editing}
                    <input bind:value={b.name} placeholder="Hemoglobin" />
                  {:else}
                    <a
                      class="marker-link"
                      href="{base}/health/bloodwork/marker?name={encodeURIComponent(b.name)}"
                    >
                      {b.display_name || b.name}
                    </a>
                  {/if}
                </td>
                <td>
                  {#if editing}
                    <input type="number" step="any" bind:value={b.value} />
                  {:else}
                    {b.value}
                  {/if}
                </td>
                <td>
                  {#if editing}
                    <input bind:value={b.unit} placeholder="g/dL" />
                  {:else}
                    {b.unit}
                  {/if}
                </td>
                <td>
                  {#if editing}
                    <input
                      type="number"
                      step="any"
                      bind:value={b.ref_range_low}
                      placeholder="low"
                    />
                    <input
                      type="number"
                      step="any"
                      bind:value={b.ref_range_high}
                      placeholder="high"
                    />
                  {:else if b.ref_range_low != null || b.ref_range_high != null}
                    {b.ref_range_low ?? '—'} – {b.ref_range_high ?? '—'}
                  {:else}
                    —
                  {/if}
                </td>
                <td>
                  {#if b.flag}<span class="flag flag-{b.flag}">{b.flag}</span>{/if}
                </td>
                {#if editing}
                  <td>
                    <button class="del" type="button" onclick={() => removeRow(i)}>×</button>
                  </td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
        {#if editing}
          <Button onclick={addRow}>+ Add biomarker</Button>
        {/if}
      </div>

      {#if source.available}
        <div class="source">
          <div class="source-header">Source document</div>
          {#if source.mime?.startsWith('image/')}
            <img src={healthPanelSourceUrl(id)} alt="Lab report" />
          {:else}
            <embed src={healthPanelSourceUrl(id)} type={source.mime || 'application/pdf'} />
          {/if}
          <a class="source-link" href={healthPanelSourceUrl(id)} target="_blank" rel="noopener">
            Open in new tab
          </a>
        </div>
      {/if}
    </div>
  {/if}

  <ConfirmDialog
    bind:open={confirmDelete}
    title="Delete panel"
    message="Are you sure you want to delete this panel? Removes the panel, all biomarkers, derived stat entries, and the source file."
    confirmLabel="Delete"
    onConfirm={deletePanel}
  />
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    /* Grows so the loading state centers in the pane, like every other health
	     page (whose content sits directly in the frame). */
    flex: 1 0 auto;
  }
  .header {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }
  .back {
    font-size: var(--text-xs);
    color: var(--text-muted);
    text-decoration: none;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: var(--space-1) 0;
  }
  .lab,
  .type {
    color: var(--text-muted);
    font-weight: 400;
  }
  .header-meta {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .header-edit {
    display: grid;
    grid-template-columns: auto 1fr 1fr;
    gap: var(--space-2) var(--space-3);
    max-width: 32rem;
  }
  .header-edit input {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-1) var(--space-2);
    min-width: 0;
  }
  .header-edit .full-row {
    grid-column: 1 / -1;
  }
  .encounter-link {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .encounter-label {
    color: var(--text-dim);
  }
  .encounter-link a {
    color: var(--accent-blue);
    text-decoration: none;
  }
  .encounter-link a:hover {
    text-decoration: underline;
  }
  /* Buttons sit under the header (not pinned to the right) at the compact
	   size used by the main bloodwork header on mobile. */
  .actions {
    display: flex;
    gap: var(--space-1);
    flex-wrap: wrap;
  }
  .split {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--space-4);
  }
  @media (max-width: 900px) {
    .split {
      grid-template-columns: 1fr;
    }
  }
  .biomarker-table input {
    width: 100%;
    max-width: 9rem;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-xs);
    padding: 0.15rem var(--space-1);
  }
  .marker-link {
    color: var(--text-primary);
    text-decoration: none;
  }
  .marker-link:hover {
    text-decoration: underline;
  }
  .flag-row {
    background: rgba(204, 102, 102, 0.06);
  }
  .flag {
    display: inline-flex;
    justify-content: center;
    align-items: center;
    min-width: 1.5rem;
    padding: 0 var(--space-2);
    border-radius: var(--radius-pill);
    font-size: var(--text-xs);
    font-weight: 500;
  }
  .flag-H {
    background: var(--status-danger-bg);
    color: var(--status-danger-fg);
  }
  .flag-L {
    background: var(--status-info-bg);
    color: var(--status-info-fg);
  }
  /* Critical is a step above High, so it keeps a solid saturated fill rather
	   than the tinted chip. Both halves of the pair live in app.css. */
  .flag-C {
    background: var(--status-critical-bg);
    color: var(--status-critical-fg);
  }
  .add {
    margin-top: var(--space-2);
  }
  .del {
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: 1.1rem;
    cursor: pointer;
  }
  .del:hover {
    color: var(--status-danger-fg);
  }
  .source {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3);
  }
  .source-header {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .source img {
    width: 100%;
    height: auto;
  }
  .source embed {
    width: 100%;
    height: 600px;
  }
  .source-link {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }
</style>
