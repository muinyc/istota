<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import {
    getLocationCurrent,
    getLocationPings,
    getDaySummary,
    type CurrentLocation,
    type LocationPing,
    type DaySummary,
    type DaySummaryStop,
  } from '$lib/api';
  import { segmentTrips, type Trip } from '$lib/location-path';
  import { elevationSummary } from '$lib/location-elevation';
  import {
    locationPlaces,
    mapFlyTo,
    selectedPlaceId,
    onPlaceMove,
    pickingPlace,
    requestNewPlace,
  } from '$lib/stores/location';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';
  import ElevationProfile from '$lib/components/location/ElevationProfile.svelte';
  import LocationMap from '$lib/components/location/LocationMap.svelte';
  import StopsPanel from '$lib/components/location/StopsPanel.svelte';
  import { formatMinutes } from '$lib/dateFormat';

  let current = $state<CurrentLocation | null>(null);
  let pings: LocationPing[] = $state([]);
  let summary = $state<DaySummary | null>(null);
  // Trips are derived from the same filtered-ping pipeline the map draws, so
  // each trip is one continuous line between stops (no separate backend call).
  let trips = $derived<Trip[]>(segmentTrips(pings));
  // The strip lives behind "Show details", so the page needs its verdict twice
  // over before the strip itself is mounted: for the toggle's availability and
  // for the stats-bar figure that says the day has a profile at all.
  let elevation = $derived(elevationSummary(pings));
  let loading = $state(true);
  let error = $state('');
  let pollInterval: ReturnType<typeof setInterval> | undefined;
  let loadInFlight = false;
  let destroyed = false;
  let mapComponent: LocationMap | undefined = $state();
  let panelOpen = $state(loadSetting('location.panelOpen', false));
  let browserPos = $state<{ lat: number; lon: number; at: number } | null>(null);
  let browserPosTried = false;

  $effect(() => {
    saveSetting('location.panelOpen', panelOpen);
  });

  function localDate(d: Date = new Date()): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  let places = $derived($locationPlaces);

  let currentPos = $derived(
    current?.last_ping
      ? { lat: current.last_ping.lat, lon: current.last_ping.lon }
      : browserPos
        ? { lat: browserPos.lat, lon: browserPos.lon }
        : null,
  );

  let currentSource = $derived<'tracker' | 'browser'>(current?.last_ping ? 'tracker' : 'browser');

  const formatDuration = (minutes: number | null) => formatMinutes(minutes);

  function timeAgo(timestamp: string): string {
    const diff = Date.now() - new Date(timestamp).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  let currentLabel = $derived.by(() => {
    if (!current?.last_ping) return null;
    const placeName = current.current_visit?.place_name ?? current.last_ping.place ?? null;
    const visitDuration = current.current_visit
      ? formatDuration(current.current_visit.duration_minutes)
      : '';
    return {
      placeName,
      visitDuration,
      ago: timeAgo(current.last_ping.timestamp),
      battery:
        current.last_ping.battery != null ? `${Math.round(current.last_ping.battery * 100)}%` : '',
    };
  });

  function tryBrowserGeolocation() {
    if (browserPosTried) return;
    browserPosTried = true;
    if (typeof navigator === 'undefined' || !navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        browserPos = {
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          at: Date.now(),
        };
      },
      () => {
        // Permission denied or unavailable — leave map empty
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
    );
  }

  let hasDetails = $derived(
    (summary?.stops.length ?? 0) > 0 || trips.length > 0 || pings.length > 1 || elevation.show,
  );

  async function loadData() {
    if (loadInFlight) return;
    loadInFlight = true;
    const today = localDate();
    try {
      const [currentResult, pingsResult, summaryResult] = await Promise.allSettled([
        getLocationCurrent(),
        getLocationPings({ date: today }),
        getDaySummary(today),
      ]);
      if (destroyed) return;

      if (currentResult.status === 'fulfilled') current = currentResult.value;
      if (pingsResult.status === 'fulfilled') pings = pingsResult.value.pings;
      if (summaryResult.status === 'fulfilled') summary = summaryResult.value;

      const loadedAny = [currentResult, pingsResult, summaryResult].some(
        (result) => result.status === 'fulfilled',
      );
      if (loadedAny) error = '';
      else if (loading) error = 'Failed to load location data';
      if (currentResult.status === 'fulfilled' && !currentResult.value.last_ping) {
        tryBrowserGeolocation();
      }
    } catch {
      if (loading) error = 'Failed to load location data';
    } finally {
      loadInFlight = false;
      if (!destroyed) loading = false;
    }
  }

  function handleStopClick(stop: DaySummaryStop) {
    mapComponent?.flyTo(stop.lat, stop.lon);
  }

  function handleTripClick(trip: Trip) {
    mapComponent?.flyTo(
      (trip.start_lat + trip.end_lat) / 2,
      (trip.start_lon + trip.end_lon) / 2,
      13,
    );
  }

  onMount(() => {
    loadData();
    pollInterval = setInterval(loadData, 60000);
  });

  onDestroy(() => {
    destroyed = true;
    if (pollInterval) clearInterval(pollInterval);
    mapFlyTo.set(undefined);
  });

  $effect(() => {
    if (mapComponent) {
      mapFlyTo.set((lat, lon, zoom) => mapComponent?.flyTo(lat, lon, zoom));
    }
  });
</script>

<div class="page-fill">
  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else}
    <div class="map-area">
      <LocationMap
        bind:this={mapComponent}
        {pings}
        {places}
        currentPosition={currentPos}
        {currentSource}
        showPath={true}
        selectedPlaceId={$selectedPlaceId}
        onPlaceMove={$onPlaceMove}
        pickingLocation={$pickingPlace}
        onMapClick={(lat, lon) => $requestNewPlace?.({ lat, lon })}
      />
    </div>

    <div class="stats-bar">
      {#if currentLabel}
        <span class="current">
          {#if currentLabel.placeName}
            <span class="place">{currentLabel.placeName}</span>
            {#if currentLabel.visitDuration}
              <span class="stat">{currentLabel.visitDuration}</span>
            {/if}
          {:else}
            <span class="place dim">No place</span>
          {/if}
          <span class="stat">{currentLabel.ago}</span>
          {#if currentLabel.battery}
            <span class="stat">{currentLabel.battery}</span>
          {/if}
        </span>
      {:else if browserPos}
        <span class="current">
          <span class="place dim">Browser location</span>
          <span class="stat">set up Overland for tracking</span>
        </span>
      {:else}
        <span class="stat dim">No location data</span>
      {/if}
      {#if pings.length > 0}
        <span class="stat">{pings.length} pings</span>
      {/if}
      {#if summary && summary.stops.length > 0}
        <span class="stat">{summary.stops.length} stops</span>
      {/if}
      {#if summary && summary.transit_pings > 0}
        <span class="stat">{summary.transit_pings} transit</span>
      {/if}
      {#if trips.length > 0}
        <span class="stat">{trips.length} trips</span>
      {/if}
      {#if elevation.range}
        <span class="stat">{Math.round(elevation.range.max - elevation.range.min)} m elevation</span
        >
      {/if}
      {#if hasDetails}
        <button class="stops-btn" onclick={() => (panelOpen = !panelOpen)} type="button">
          {panelOpen ? 'Hide details' : 'Show details'}
        </button>
      {/if}
    </div>

    {#if panelOpen && hasDetails}
      <StopsPanel
        {pings}
        {trips}
        {summary}
        onStopClick={handleStopClick}
        onTripClick={handleTripClick}
      />
      <ElevationProfile summary={elevation} />
    {/if}
  {/if}
</div>

<style>
  .page-fill {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .map-area {
    flex: 1;
    min-height: 0;
    position: relative;
  }

  .stats-bar {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .current {
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-2);
  }

  .place {
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .place.dim {
    color: var(--text-dim);
    font-weight: 400;
    font-size: var(--text-xs);
  }

  .stat {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .stat.dim {
    color: var(--text-dim);
  }

  .stops-btn {
    margin-left: auto;
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
    padding: 0;
  }

  .stops-btn:hover {
    color: var(--text-primary);
  }
</style>
