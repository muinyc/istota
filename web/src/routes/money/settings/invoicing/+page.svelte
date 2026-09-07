<script lang="ts">
  import {
    getBusinessSettings,
    createEntity,
    updateEntity,
    deleteEntity,
    createService,
    updateService,
    deleteService,
    ApiError,
    type EntityRow,
    type ServiceRow,
    type EntityInput,
    type ServiceInput,
    type BusinessDefaults,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { SettingsLayout, SettingsCard } from '$lib/components/settings';
  import {
    Button,
    ConfirmDialog,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
  } from '$lib/components/ui';
  import EntityForm from '$lib/components/money/EntityForm.svelte';
  import ServiceForm from '$lib/components/money/ServiceForm.svelte';
  import { formatDecimal } from '$lib/format';

  let loading = $state(true);
  let entities: EntityRow[] = $state([]);
  let services: ServiceRow[] = $state([]);
  let defaults: BusinessDefaults | null = $state(null);
  let businessError = $state('');

  async function loadBusiness() {
    try {
      const resp = await getBusinessSettings();
      entities = resp.entities;
      services = resp.services;
      defaults = resp.defaults;
      businessError = '';
    } catch (e) {
      businessError = e instanceof Error ? e.message : 'Failed to load business settings';
    } finally {
      loading = false;
    }
  }

  // The ledger effect covers the first load too — it runs on mount and again on
  // every ledger switch. (The single page this was split out of also called
  // `refresh()` from `onMount`, so every visit fetched this twice.)
  $effect(() => {
    $selectedLedger;
    void loadBusiness();
  });

  const formatRate = formatDecimal;

  function typeLabel(t: string): string {
    const labels: Record<string, string> = {
      hours: 'per hour',
      days: 'per day',
      flat: 'flat rate',
      other: 'variable',
    };
    return labels[t] || t;
  }

  // --- Entity + service editing ---
  //
  // A refused delete (409) gets its own banner rather than being folded into
  // the card's error: the server's reason names records the user has to go
  // look at — the clients still pointing at an entity, or the work entries
  // still naming a service.
  let entityFormOpen = $state(false);
  let editingEntity: EntityRow | null = $state(null);
  let entityFormError = $state('');
  let entitySaving = $state(false);
  let entityNotice = $state('');

  let serviceFormOpen = $state(false);
  let editingService: ServiceRow | null = $state(null);
  let serviceFormError = $state('');
  let serviceSaving = $state(false);
  let serviceNotice = $state('');

  let confirmOpen = $state(false);
  let pendingDelete: { kind: 'entity' | 'service'; key: string; label: string } | null =
    $state(null);

  function openEntityForm(entity: EntityRow | null) {
    editingEntity = entity;
    entityFormError = '';
    entityFormOpen = true;
  }

  function openServiceForm(service: ServiceRow | null) {
    editingService = service;
    serviceFormError = '';
    serviceFormOpen = true;
  }

  async function saveEntity(key: string, data: EntityInput) {
    entitySaving = true;
    entityFormError = '';
    try {
      if (editingEntity) await updateEntity(key, data);
      else await createEntity(key, data);
      entityFormOpen = false;
      editingEntity = null;
      await loadBusiness();
    } catch (e) {
      entityFormError = e instanceof Error ? e.message : 'Failed to save entity';
    } finally {
      entitySaving = false;
    }
  }

  async function saveService(key: string, data: ServiceInput) {
    serviceSaving = true;
    serviceFormError = '';
    try {
      if (editingService) await updateService(key, data);
      else await createService(key, data);
      serviceFormOpen = false;
      editingService = null;
      await loadBusiness();
    } catch (e) {
      serviceFormError = e instanceof Error ? e.message : 'Failed to save service';
    } finally {
      serviceSaving = false;
    }
  }

  function askDelete(kind: 'entity' | 'service', key: string, label: string) {
    pendingDelete = { kind, key, label };
    confirmOpen = true;
  }

  async function handleDelete() {
    const target = pendingDelete;
    confirmOpen = false;
    pendingDelete = null;
    if (!target) return;

    entityNotice = '';
    serviceNotice = '';
    try {
      if (target.kind === 'entity') await deleteEntity(target.key);
      else await deleteService(target.key);
      await loadBusiness();
    } catch (e) {
      const msg = e instanceof Error ? e.message : `Failed to delete ${target.kind}`;
      const refused = e instanceof ApiError && e.status === 409;
      if (target.kind === 'entity') entityNotice = msg;
      else serviceNotice = msg;
      if (!refused) businessError = msg;
    }
  }

  const deleteMessage = $derived.by(() => {
    if (!pendingDelete) return '';
    if (pendingDelete.kind === 'entity') {
      return (
        `Are you sure you want to delete ${pendingDelete.label}? ` +
        'Clients that bill under it, and the default entity, are protected — ' +
        'the delete is refused rather than silently rebilling under another entity.'
      );
    }
    return (
      `Are you sure you want to delete ${pendingDelete.label}? ` +
      'A service any work entry still names cannot be deleted, because it would ' +
      'unbill that work and shrink the totals of invoices that already went out.'
    );
  });

  function entityMenu(entity: EntityRow): KebabItem[] {
    return [
      { label: 'Edit', onSelect: () => openEntityForm(entity) },
      {
        label: 'Delete',
        onSelect: () => askDelete('entity', entity.key, entity.name || entity.key),
        danger: true,
      },
    ];
  }

  function serviceMenu(svc: ServiceRow): KebabItem[] {
    return [
      { label: 'Edit', onSelect: () => openServiceForm(svc) },
      {
        label: 'Delete',
        onSelect: () => askDelete('service', svc.key, svc.display_name || svc.key),
        danger: true,
      },
    ];
  }
</script>

<SettingsLayout
  title="Invoicing"
  description="The entities that bill, the services they bill for, and the defaults an invoice is generated from. Clients are edited on the Business → Clients tab."
  {loading}
>
  <SettingsCard title="Business defaults">
    {#if businessError}
      <div class="banner error">{businessError}</div>
    {:else if !defaults}
      <p class="empty">No invoicing configuration found.</p>
    {:else}
      <dl class="kv">
        <dt>Currency</dt>
        <dd>{defaults.currency}</dd>
        <dt>Default entity</dt>
        <dd>{defaults.default_entity}</dd>
        <dt>A/R account</dt>
        <dd><code>{defaults.default_ar_account}</code></dd>
        <dt>Bank account</dt>
        <dd><code>{defaults.default_bank_account}</code></dd>
        <dt>Invoice output</dt>
        <dd><code>{defaults.invoice_output}</code></dd>
        <dt>Next invoice #</dt>
        <dd>{defaults.next_invoice_number}</dd>
        {#if defaults.days_until_overdue > 0}
          <dt>Days until overdue</dt>
          <dd>{defaults.days_until_overdue}</dd>
        {/if}
        {#if defaults.notifications}
          <dt>Notifications</dt>
          <dd>{defaults.notifications}</dd>
        {/if}
      </dl>
    {/if}
  </SettingsCard>

  <!-- Outside the `{#if defaults}` guard on purpose: a user with no
       invoicing configuration has to be able to create the first entity
       and the first service from here. -->
  <SettingsCard title="Entities ({entities.length})">
    {#snippet actions()}
      <Button variant="primary" size="sm" onclick={() => openEntityForm(null)}>Add entity</Button>
    {/snippet}
    {#if entityNotice}
      <NoticeBanner title={entityNotice} variant="warn" />
    {/if}
    {#if entities.length === 0}
      <p class="empty">No entities yet — add the one that bills your clients.</p>
    {:else}
      <div class="entity-grid card-grid">
        {#each entities as entity (entity.key)}
          <div class="entity">
            <div class="entity-head">
              <span>{entity.name}</span>
              <span class="entity-key">
                <code>{entity.key}</code>
                <KebabMenu items={entityMenu(entity)} ariaLabel="Entity actions" />
              </span>
            </div>
            <dl class="kv compact">
              {#if entity.email}
                <dt>Email</dt>
                <dd>{entity.email}</dd>
              {/if}
              {#if entity.address}
                <dt>Address</dt>
                <dd class="pre">{entity.address}</dd>
              {/if}
              {#if entity.currency}
                <dt>Currency</dt>
                <dd>{entity.currency}</dd>
              {/if}
              {#if entity.ar_account}
                <dt>A/R</dt>
                <dd><code>{entity.ar_account}</code></dd>
              {/if}
              {#if entity.bank_account}
                <dt>Bank</dt>
                <dd><code>{entity.bank_account}</code></dd>
              {/if}
              {#if entity.payment_instructions}
                <dt>Payment</dt>
                <dd class="pre">{entity.payment_instructions}</dd>
              {/if}
              {#if entity.logo}
                <dt>Logo</dt>
                <dd><code>{entity.logo}</code></dd>
              {/if}
            </dl>
          </div>
        {/each}
      </div>
    {/if}
  </SettingsCard>

  <SettingsCard title="Services ({services.length})">
    {#snippet actions()}
      <Button variant="primary" size="sm" onclick={() => openServiceForm(null)}>Add service</Button>
    {/snippet}
    {#if serviceNotice}
      <NoticeBanner title={serviceNotice} variant="warn" />
    {/if}
    {#if services.length === 0}
      <p class="empty">No services yet — add what you bill for.</p>
    {:else}
      <div class="table-scroll">
        <table class="grid">
          <thead>
            <tr>
              <th>Service</th>
              <th>Type</th>
              <th class="num">Rate</th>
              <th>Income account</th>
              <th class="actions" aria-label="Actions"></th>
            </tr>
          </thead>
          <tbody>
            {#each services as svc (svc.key)}
              <tr>
                <td>
                  {svc.display_name}
                  <span class="muted"> <code>{svc.key}</code></span>
                </td>
                <td class="muted">{typeLabel(svc.type)}</td>
                <td class="num">
                  {svc.type === 'other' ? '—' : `$${formatRate(svc.rate)}`}
                </td>
                <td class="muted"><code>{svc.income_account || '—'}</code></td>
                <td class="actions">
                  <KebabMenu items={serviceMenu(svc)} ariaLabel="Service actions" />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </SettingsCard>
</SettingsLayout>

{#if entityFormOpen}
  <EntityForm
    entity={editingEntity}
    onSave={saveEntity}
    onCancel={() => {
      entityFormOpen = false;
      editingEntity = null;
    }}
    error={entityFormError}
    saving={entitySaving}
  />
{/if}

{#if serviceFormOpen}
  <ServiceForm
    service={editingService}
    onSave={saveService}
    onCancel={() => {
      serviceFormOpen = false;
      editingService = null;
    }}
    error={serviceFormError}
    saving={serviceSaving}
  />
{/if}

<ConfirmDialog
  bind:open={confirmOpen}
  title={pendingDelete?.kind === 'entity' ? 'Delete entity' : 'Delete service'}
  message={deleteMessage}
  confirmLabel="Delete"
  onConfirm={handleDelete}
  onCancel={() => (pendingDelete = null)}
/>

<style>
  /* Shared .settings/.card/.field/.grid/.banner primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only money-specific
	   styling (kv, entity grid, numeric column tweaks) stays. */

  .kv.compact {
    /* design-lint-allow: sub---space-1 hairline between the rows of a compact
       definition list — the ramp starts a step above what this wants */
    gap: 0.15rem var(--space-2);
    font-size: var(--text-xs);
  }

  .kv dt {
    color: var(--text-dim);
  }

  .kv dd {
    margin: 0;
    color: var(--text-secondary);
    word-break: break-word;
  }

  .kv dd.pre {
    white-space: pre-line;
  }

  .entity-grid {
    --card-min: 220px;
    --card-gap: 0.6rem;
  }

  .entity {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .entity-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-2);
    font-weight: 600;
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  .entity-key {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-weight: 400;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }

  /* Money's services table sizes by content; shared .settings .grid uses
	   fixed layout, so opt back to auto here. */
  .grid {
    table-layout: auto;
  }

  .grid th.num,
  .grid td.num {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .grid th.actions,
  .grid td.actions {
    width: 1.5rem;
    text-align: right;
  }
</style>
