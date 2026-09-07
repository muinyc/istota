<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getGarminStatus,
    connectGarmin,
    submitGarminMfa,
    disconnectGarmin,
    syncGarmin,
    importGarminTracks,
    type GarminStatus,
  } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  import SettingsField from './SettingsField.svelte';
  import { formatDateTime } from '$lib/dateFormat';

  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let info = $state('');

  let status: GarminStatus = $state({
    connected: false,
    email: null,
    last_sync: null,
    error: null,
  });

  // Connect flow state.
  let mode: 'idle' | 'mfa' = $state('idle');
  let emailInput = $state('');
  let passwordInput = $state('');
  let mfaCodeInput = $state('');

  async function refresh() {
    loading = true;
    error = '';
    try {
      status = await getGarminStatus();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load Garmin status';
    } finally {
      loading = false;
    }
  }

  // L3: clear stale banners on the next user-initiated action so the
  // green "Sync complete" message doesn't linger forever and the
  // previous red error doesn't bleed into the new attempt.
  function resetBanners() {
    error = '';
    info = '';
  }

  async function startConnect() {
    busy = true;
    resetBanners();
    try {
      const resp = await connectGarmin(emailInput, passwordInput);
      if (resp.status === 'mfa_required') {
        mode = 'mfa';
        info = resp.prompt || 'Enter Garmin MFA code';
      } else if (resp.status === 'ok') {
        passwordInput = '';
        await refresh();
        info = 'Connected to Garmin Connect.';
      } else {
        error = resp.error || 'Garmin connect failed';
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Garmin connect failed';
    } finally {
      busy = false;
    }
  }

  async function submitMfa() {
    busy = true;
    resetBanners();
    try {
      const resp = await submitGarminMfa(mfaCodeInput);
      if (resp.status === 'ok') {
        mode = 'idle';
        mfaCodeInput = '';
        passwordInput = '';
        await refresh();
        info = 'Connected to Garmin Connect.';
      } else {
        error = resp.error || 'MFA verification failed';
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'MFA verification failed';
    } finally {
      busy = false;
    }
  }

  async function syncNow() {
    busy = true;
    resetBanners();
    try {
      const r = await syncGarmin(7);
      if (r.auth_error) {
        error = 'Garmin token expired — please reconnect.';
        await refresh();
      } else {
        info = `Sync complete: ${r.inserted} added, ${r.skipped} already present, ${r.errored} errors.`;
        await refresh();
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Sync failed';
    } finally {
      busy = false;
    }
  }

  async function importTracks() {
    busy = true;
    resetBanners();
    try {
      const r = await importGarminTracks(30);
      if (r.activities === 0) {
        info = 'No new GPS activities to import in the last 30 days.';
      } else {
        info = `Imported ${r.inserted} track points from ${r.activities} activit${r.activities === 1 ? 'y' : 'ies'} into your location history.`;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Track import failed';
    } finally {
      busy = false;
    }
  }

  async function doDisconnect() {
    busy = true;
    resetBanners();
    try {
      await disconnectGarmin();
      emailInput = '';
      passwordInput = '';
      mode = 'idle';
      await refresh();
      info = 'Disconnected from Garmin Connect.';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Disconnect failed';
    } finally {
      busy = false;
    }
  }

  const formatTimestamp = (iso: string | null) => formatDateTime(iso, { empty: 'never' });

  onMount(refresh);
</script>

<SettingsCard
  title="Garmin Connect"
  description="Connect once for both Health and Location. Sync daily summaries (sleep, stress, body battery, steps, SpO₂, HRV, VO₂ max, resting HR, body composition) into your stats, and import GPS tracks from watch-recorded runs, hikes, and walks into your location history."
>
  {#if loading}
    <p class="muted">Loading…</p>
  {:else if status.connected}
    <div class="status-row">
      <div>
        <div class="label">Connected as</div>
        <div class="value">{status.email || '—'}</div>
      </div>
      <div>
        <div class="label">Last sync</div>
        <div class="value">{formatTimestamp(status.last_sync)}</div>
      </div>
    </div>
    {#if status.error}
      <div class="banner error">
        {status.error === 'token_expired' ? 'Token expired — please reconnect.' : status.error}
      </div>
    {/if}
    <div class="actions">
      <Button variant="primary" onclick={syncNow} disabled={busy}>
        {busy ? 'Working…' : 'Sync health data'}
      </Button>
      <Button variant="subtle" onclick={importTracks} disabled={busy}>
        {busy ? 'Working…' : 'Import GPS tracks'}
      </Button>
      <Button variant="ghost" onclick={doDisconnect} disabled={busy}>Disconnect</Button>
    </div>
  {:else if mode === 'mfa'}
    <SettingsField label="MFA code" hint="6-digit code from your Garmin authenticator app">
      <input
        type="text"
        inputmode="numeric"
        autocomplete="one-time-code"
        bind:value={mfaCodeInput}
        placeholder="000000"
      />
    </SettingsField>
    <div class="actions">
      <Button variant="primary" onclick={submitMfa} disabled={busy || !mfaCodeInput}>
        {busy ? 'Verifying…' : 'Verify'}
      </Button>
      <Button variant="ghost" onclick={() => (mode = 'idle')} disabled={busy}>Cancel</Button>
    </div>
  {:else}
    <SettingsField label="Email">
      <input type="email" bind:value={emailInput} autocomplete="username" />
    </SettingsField>
    <SettingsField
      label="Password"
      hint="Credentials are used only during the OAuth exchange and are not stored."
    >
      <input type="password" bind:value={passwordInput} autocomplete="current-password" />
    </SettingsField>
    <div class="actions">
      <Button
        variant="primary"
        onclick={startConnect}
        disabled={busy || !emailInput || !passwordInput}
      >
        {busy ? 'Connecting…' : 'Connect'}
      </Button>
    </div>
  {/if}

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if info}
    <div class="banner success">{info}</div>
  {/if}
</SettingsCard>

<style>
  .status-row {
    display: flex;
    gap: var(--space-8);
    flex-wrap: wrap;
    margin-bottom: var(--space-3);
  }
  .label {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .value {
    font-size: var(--text-base);
  }
  .actions {
    display: flex;
    gap: var(--space-2);
    margin-top: var(--space-3);
  }
</style>
