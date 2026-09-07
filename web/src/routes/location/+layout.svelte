<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    createPlace,
    deletePlace,
    dismissCluster,
    updatePlace,
    getPlaceStats,
    type DiscoveredCluster,
    type Place,
    type PlaceStats,
  } from '$lib/api';
  import {
    locationPlaces,
    reloadPlaces,
    mapFlyTo,
    selectedPlaceId as selectedPlaceIdStore,
    onPlaceMove as onPlaceMoveStore,
    pickingPlace as pickingPlaceStore,
    requestNewPlace as requestNewPlaceStore,
    bumpDiscoverDirty,
  } from '$lib/stores/location';
  import PlaceForm from '$lib/components/location/PlaceForm.svelte';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    CategoryGroup,
    HeaderNav,
    Chip,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from 'lucide-svelte';
  import { formatDate as formatIsoDate, formatMinutes } from '$lib/dateFormat';

  let { children } = $props();

  let places = $derived($locationPlaces);
  let sidebarOpen = $state(false);
  let selectedPlace: Place | null = $state(null);
  let placeStats: PlaceStats | null = $state(null);
  let statsLoading = $state(false);
  let editingPlace: Place | null = $state(null);

  let picking = $state(false);
  let creating: { lat: number; lon: number; cluster?: DiscoveredCluster } | null = $state(null);
  let createError = $state('');

  $effect(() => {
    pickingPlaceStore.set(picking);
  });

  $effect(() => {
    requestNewPlaceStore.set((args) => {
      picking = false;
      creating = args;
    });
    return () => requestNewPlaceStore.set(undefined);
  });

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  function isExactActive(path: string): boolean {
    const current = page.url.pathname;
    return current === `${base}${path}` || current === `${base}${path}/`;
  }

  const navItems = $derived([
    { href: `${base}/location`, label: 'Today', active: isExactActive('/location') },
    { href: `${base}/location/history`, label: 'History', active: isActive('/location/history') },
  ]);

  const onSettings = $derived(page.url.pathname.startsWith(`${base}/location/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/location`);
    else goto(`${base}/location/settings`);
  }

  function startPicking() {
    if (creating) return;
    picking = !picking;
    if (picking) sidebarOpen = false;
  }

  function closeCreate() {
    creating = null;
    createError = '';
  }

  async function handleCreate(data: {
    name: string;
    lat: number;
    lon: number;
    radius_meters: number;
    category: string;
    notes: string;
  }) {
    try {
      await createPlace(data);
      closeCreate();
      await reloadPlaces();
      bumpDiscoverDirty();
    } catch (e) {
      createError = e instanceof Error ? e.message : 'Failed to save place';
    }
  }

  async function handleDismissCluster(data: { lat: number; lon: number; radius_meters: number }) {
    try {
      await dismissCluster(data);
      closeCreate();
      bumpDiscoverDirty();
    } catch (e) {
      createError = e instanceof Error ? e.message : 'Failed to dismiss';
    }
  }

  async function handlePlaceClick(place: Place) {
    const fly = $mapFlyTo;
    if (fly) fly(place.lat, place.lon, 15);

    if (selectedPlace?.id === place.id) {
      selectedPlace = null;
      placeStats = null;
      return;
    }

    selectedPlace = place;
    placeStats = null;
    statsLoading = true;
    try {
      placeStats = await getPlaceStats(place.id);
    } catch {
      placeStats = null;
    } finally {
      statsLoading = false;
    }
  }

  function handleEditPlace(place: Place) {
    editingPlace = place;
  }

  async function handleEditSave(data: {
    name: string;
    lat: number;
    lon: number;
    radius_meters: number;
    category: string;
    notes: string;
  }) {
    if (!editingPlace) return;
    try {
      await updatePlace(editingPlace.id, data);
      editingPlace = null;
      selectedPlace = null;
      placeStats = null;
      await reloadPlaces();
    } catch {
      // ignore
    }
  }

  async function handleDeletePlace(place: Place) {
    try {
      await deletePlace(place.id);
      if (selectedPlace?.id === place.id) {
        selectedPlace = null;
        placeStats = null;
      }
      if (editingPlace?.id === place.id) {
        editingPlace = null;
      }
      await reloadPlaces();
    } catch {
      // ignore
    }
  }

  let groupedPlaces = $derived.by(() => {
    const groups: Record<string, Place[]> = {};
    for (const p of places) {
      const cat = p.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    }
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  });

  const formatDuration = (minutes: number | null) => formatMinutes(minutes, '—');

  const formatDate = (iso: string | null) => formatIsoDate(iso, { empty: '—' });

  async function handlePlaceMove(placeId: number, lat: number, lon: number) {
    try {
      await updatePlace(placeId, { lat, lon });
      await reloadPlaces();
      if (selectedPlace?.id === placeId) {
        placeStats = null;
        statsLoading = true;
        try {
          placeStats = await getPlaceStats(placeId);
        } catch {
          placeStats = null;
        } finally {
          statsLoading = false;
        }
      }
    } catch {
      await reloadPlaces();
    }
  }

  $effect(() => {
    selectedPlaceIdStore.set(selectedPlace?.id ?? null);
  });

  $effect(() => {
    onPlaceMoveStore.set(handlePlaceMove);
    return () => onPlaceMoveStore.set(undefined);
  });

  function handleVisibility() {
    if (document.visibilityState === 'visible') {
      reloadPlaces().catch(() => {});
    }
  }

  onMount(() => {
    reloadPlaces().catch(() => {});
    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Location"
      onTitleClick={onSettings ? undefined : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open places"
    >
      {#snippet leading()}
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Places"
            count={places.length}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
      {/snippet}
      {#snippet nav()}
        <HeaderNav items={navItems} ariaLabel="Location section" />
      {/snippet}
      {#snippet tools()}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Location settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#if !onSettings}
      <Sidebar
        title="Places"
        count={places.length}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        {#snippet extras()}
          <div class="sidebar-actions">
            <button
              class="new-place-btn"
              class:active={picking}
              onclick={startPicking}
              type="button"
              title={picking ? 'Click anywhere on the map to set the location' : 'Add a new place'}
            >
              {picking ? 'Click on map…' : '+ New place'}
            </button>
            {#if createError}
              <div class="action-error">{createError}</div>
            {/if}
          </div>

          {#if selectedPlace && (statsLoading || placeStats)}
            <div class="stats-panel">
              <div class="stats-header">
                <span class="stats-name">{selectedPlace.name}</span>
                <div class="stats-actions">
                  <button
                    class="stats-edit"
                    onclick={() => handleEditPlace(selectedPlace!)}
                    type="button"
                    title="Edit place">&#9998;</button
                  >
                  <button
                    class="stats-close"
                    onclick={() => {
                      selectedPlace = null;
                      placeStats = null;
                    }}
                    type="button">&times;</button
                  >
                </div>
              </div>
              {#if selectedPlace.notes}
                <div class="stats-notes">{selectedPlace.notes}</div>
              {/if}
              {#if statsLoading}
                <div class="stats-loading">Loading...</div>
              {:else if placeStats && placeStats.total_visits > 0}
                <div class="stats-grid">
                  <div class="stat">
                    <span class="stat-value">{placeStats.total_visits}</span>
                    <span class="stat-label"
                      >{placeStats.total_visits === 1 ? 'visit' : 'visits'}</span
                    >
                  </div>
                  <div class="stat">
                    <span class="stat-value">{formatDuration(placeStats.avg_duration_min)}</span>
                    <span class="stat-label">avg</span>
                  </div>
                  <div class="stat">
                    <span class="stat-value">{formatDuration(placeStats.longest_visit_min)}</span>
                    <span class="stat-label">longest</span>
                  </div>
                  <div class="stat">
                    <span class="stat-value">{formatDuration(placeStats.total_duration_min)}</span>
                    <span class="stat-label">total</span>
                  </div>
                </div>
                <div class="stats-dates">
                  <span>First: {formatDate(placeStats.first_visit)}</span>
                  <span>Last: {formatDate(placeStats.last_visit)}</span>
                </div>
              {:else}
                <div class="stats-empty">No visits recorded</div>
              {/if}
            </div>
          {/if}
        {/snippet}

        {#each groupedPlaces as [category, catPlaces] (category)}
          <CategoryGroup label={category} count={catPlaces.length} collapsible>
            {#each catPlaces as place (place.id)}
              <button
                class="place-btn"
                class:selected={selectedPlace?.id === place.id}
                onclick={() => handlePlaceClick(place)}
                type="button"
              >
                <span class="place-name">{place.name}</span>
              </button>
            {/each}
          </CategoryGroup>
        {/each}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

{#if editingPlace}
  <PlaceForm
    place={editingPlace}
    onSave={handleEditSave}
    onDelete={handleDeletePlace}
    onCancel={() => (editingPlace = null)}
  />
{/if}

{#if creating}
  <PlaceForm
    cluster={creating.cluster}
    initialLat={creating.lat}
    initialLon={creating.lon}
    onSave={handleCreate}
    onCancel={closeCreate}
    onDismiss={creating.cluster ? handleDismissCluster : undefined}
  />
{/if}

<style>
  .sidebar-actions {
    padding: 0 var(--space-3) var(--space-2);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .new-place-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-pill);
    cursor: pointer;
    text-align: center;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }

  .new-place-btn:hover {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  .new-place-btn.active {
    color: var(--accent-amber);
    border-color: var(--accent-amber);
  }

  .action-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }

  .stats-panel {
    border-bottom: 1px solid var(--border-subtle);
    padding: var(--space-2) var(--space-3);
    flex-shrink: 0;
  }

  .stats-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: var(--space-2);
  }

  .stats-name {
    font-size: var(--text-sm);
    font-weight: 500;
  }

  .stats-actions {
    display: flex;
    gap: 0.15rem;
  }

  .stats-edit,
  .stats-close {
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: var(--text-sm);
    cursor: pointer;
    padding: 0 var(--space-1);
    line-height: 1;
  }

  .stats-edit:hover,
  .stats-close:hover {
    color: var(--text-muted);
  }

  .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-2) var(--space-3);
    margin-bottom: var(--space-2);
  }

  .stat {
    display: flex;
    flex-direction: column;
  }

  .stat-value {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .stat-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .stats-dates {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .stats-loading,
  .stats-empty {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .stats-notes {
    font-size: var(--text-xs);
    color: var(--text-muted);
    font-style: italic;
    white-space: pre-wrap;
    padding: var(--space-1) 0;
    border-top: 1px solid var(--border-subtle);
  }

  .place-btn {
    display: block;
    width: 100%;
    min-width: 0;
    max-width: 100%;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    font-size: var(--text-sm);
    line-height: 1.5;
    cursor: pointer;
    padding: 0.2rem var(--space-3);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .place-btn:hover {
    background: var(--surface-raised);
  }

  .place-btn.selected {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .place-name {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
