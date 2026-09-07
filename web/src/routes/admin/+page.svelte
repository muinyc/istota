<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import {
    AVATAR_ACCEPT,
    AuthError,
    deleteBotAvatar,
    getAdminStats,
    uploadBotAvatar,
    type AdminStats,
    type AdminStatsJob,
    type AdminStatsUser,
    type AdminStatsUserSource,
    type AdminSubscriptionSpend,
  } from '$lib/api';
  import { Avatar, AvatarPicker, Field, NoticeBanner, StatTile } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import {
    formatCost,
    formatContext,
    formatNumber,
    formatPercent,
    formatResetIn,
    formatUtilization,
    usageOriginTitle,
  } from '$lib/usageFormat';
  import { formatBytes } from '$lib/format';
  import { formatDuration as formatIsoDuration } from '$lib/dateFormat';

  let stats: AdminStats | null = $state(null);
  let loading = $state(true);
  let error = $state('');
  let expandedJobs: Record<number, boolean> = $state({});
  let modulesExpanded = $state(false);
  let standaloneCollapsed = $state(true);

  const REFRESH_MS = 60_000;
  let timer: ReturnType<typeof setInterval> | null = null;

  /* The bot icon. The identity the layout resolved carries the current hash,
     and `reload()` is what publishes a new one to the nav and the chat gutter
     — this page never fetches `/me` itself. */
  const identity = getCurrentUser();
  let botIconBusyLabel = $state('');
  let botIconError = $state('');
  let botIconNote = $state('');

  async function uploadBotIcon(file: File) {
    botIconBusyLabel = 'Saving the icon…';
    botIconError = '';
    botIconNote = '';
    try {
      await uploadBotAvatar(file);
      /* The preview reads the shared record, so a reload that does not paint
         leaves it on the old hash — which the browser holds as `immutable` and
         will not re-fetch. Say so rather than showing a stale icon silently. */
      if (!(await identity.reload()))
        botIconNote = 'Icon saved. The page could not refresh — reload to see it.';
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else botIconError = (e as Error).message || 'Could not save that icon.';
    } finally {
      botIconBusyLabel = '';
    }
  }

  async function removeBotIcon() {
    botIconBusyLabel = 'Removing the icon…';
    botIconError = '';
    botIconNote = '';
    try {
      const { deleted } = await deleteBotAvatar();
      const confirmed = await identity.reload();
      if (!confirmed) botIconNote = 'Removed. The page could not refresh — reload to see it.';
      else if (!deleted) botIconNote = 'There was nothing to remove.';
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else botIconError = (e as Error).message || 'Could not remove that icon.';
    } finally {
      botIconBusyLabel = '';
    }
  }

  async function refresh() {
    try {
      stats = await getAdminStats();
      error = '';
    } catch (e) {
      error = (e as Error).message || 'Failed to load admin stats';
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void refresh();
    timer = setInterval(refresh, REFRESH_MS);
  });

  onDestroy(() => {
    if (timer) clearInterval(timer);
  });

  // `—` rather than `0s` for an absent uptime: the figure is missing, not zero.
  const formatDuration = (seconds: number) => (seconds ? formatIsoDuration(seconds) : '—');

  function formatTimestamp(ts: string | null): string {
    if (!ts) return '—';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 0) return 'just now';
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  /**
   * A plan tile's tint, by the operator's own thresholds and nothing else.
   *
   * `warn_percent` and `high_percent` ride the stats payload precisely so this
   * agrees with `istota doctor`'s verdict on the same number. Hardcoding 80/95
   * here would make a configured threshold something the dashboard silently
   * ignores, which is worse than not colouring at all — so a payload that
   * carries neither (an older backend) gets no tint rather than a guess.
   *
   * The server's own `severity` is on the wire and deliberately unused. Its
   * scale is undocumented and we have only ever seen `"normal"`; two surfaces
   * applying one rule to one number always agree, while deferring to a second
   * scale on one of them guarantees they eventually will not.
   */
  function utilizationColor(
    percent: number,
    warn: number | undefined,
    high: number | undefined,
  ): string {
    if (typeof warn !== 'number' || typeof high !== 'number') return '';
    if (!Number.isFinite(percent)) return '';
    if (percent >= high) return 'var(--status-danger-fg)';
    // Ordered high-first, so an inverted pair arriving past the loader's
    // correction collapses the amber band into danger at the lower of the two
    // rather than tinting nothing. That is the same reading doctor reaches: it
    // has one WARN branch at `min(warn, high)`, and every percentage it flags
    // is one this tints. `Math.min` here would be dead code — the branch above
    // has already returned for everything at or over `high`.
    if (percent >= warn) return 'var(--status-warn-fg)';
    return 'var(--status-success-fg)';
  }

  /**
   * One pay-as-you-go money figure, from minor units.
   *
   * This is a real dollar figure on a subscription dashboard, and it does not
   * break the rule that keeps one off the Token usage card: that rule refuses
   * to price plan-equivalent *tokens* at list. These are credits the account
   * has actually committed, reported by the endpoint in minor units with an
   * explicit currency.
   *
   * The divisor comes from `exponent`, never a hardcoded 100 — the removed
   * `!usage` command divided by 100 and was wrong for any currency that is not
   * two-decimal. `Intl` is asked for exactly that many digits rather than the
   * currency's own default, so the figure shown is the figure reported.
   */
  function spendMoney(spend: AdminSubscriptionSpend, minor: number): string {
    const digits =
      Number.isFinite(spend.exponent) && spend.exponent >= 0 && spend.exponent <= 6
        ? Math.floor(spend.exponent)
        : 2;
    const scale = Math.pow(10, digits);
    const value = (Number.isFinite(minor) ? minor : 0) / scale;
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: spend.currency || 'USD',
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(value);
    } catch {
      // A *malformed* code makes `Intl` throw — one that is not three
      // letters. An unknown but well-formed one does not: `XYZ` formats as
      // `XYZ 4.65`, which is the right outcome and needs no branch. The
      // number is still worth showing either way, and the code beside it
      // still says what it is.
      return `${value.toFixed(digits)} ${spend.currency}`;
    }
  }

  /**
   * The credits spent, which is the Extra usage tile's value.
   *
   * The money is the value rather than the percentage, unlike every plan
   * window beside it, because this tile is the one figure on the card that is
   * money rather than a share of a quota. The percentage rides the sub-line
   * with the cap it is a percentage *of*, where the two read together.
   */
  function formatSpendUsed(spend: AdminSubscriptionSpend): string {
    return spendMoney(spend, spend.used_minor);
  }

  /**
   * The cap and the share of it, as `of $20.00 (23.3%)`.
   *
   * The percentage is rendered **unclamped**, unlike a plan window's. Above 100
   * here is real money already committed, and
   * `subscription_usage._unclamped_percent` exists on the Python side to keep
   * it; clamping it back would hide an overage while the two money figures
   * beside it still showed one.
   */
  function formatSpendCap(spend: AdminSubscriptionSpend): string {
    const percent = formatUtilization(spend.percent, { clamp: false });
    return `of ${spendMoney(spend, spend.limit_minor)} (${percent})`;
  }

  function moduleErrorCount(mod: Record<string, unknown>): number {
    const v = mod['poll_errors_24h'] ?? mod['sync_errors_24h'] ?? mod['resolve_errors'];
    return typeof v === 'number' ? v : 0;
  }

  const FIELD_LABELS: Record<string, string> = {
    status: 'Status',
    users_configured: 'Users configured',
    users_resolved: 'Users resolved',
    resolve_errors: 'Resolve errors',
    feeds_total: 'Feeds',
    entries_total: 'Entries',
    entries_unread: 'Unread',
    last_poll: 'Last poll',
    poll_errors_24h: 'Poll errors (24h)',
    transactions_total: 'Transactions',
    last_sync: 'Last sync',
    sync_errors_24h: 'Sync errors (24h)',
    visits_total: 'Visits',
    places_total: 'Places',
    last_update: 'Last update',
    panels_total: 'Panels',
    biomarkers_total: 'Biomarkers',
    encounters_total: 'Encounters',
    immunizations_total: 'Immunizations',
    blocks_total: 'Blocks',
    sources_total: 'Sources',
    archived_total: 'Archived',
    last_generated: 'Last generated',
  };

  function fieldLabel(key: string): string {
    return FIELD_LABELS[key] ?? key.replace(/_/g, ' ');
  }

  const TIMESTAMP_KEYS = new Set([
    'last_poll',
    'last_sync',
    'last_update',
    'last_generated',
    'last_active',
    'last_run_at',
    'last_success_at',
    'last_backup',
    'last_scheduler_run',
  ]);

  function toggleJob(id: number) {
    expandedJobs = { ...expandedJobs, [id]: !expandedJobs[id] };
  }

  // Source colours — a categorical palette: the hue identifies *which* source,
  // it carries no severity meaning. The four sources that actually carry
  // traffic (scheduled / talk / web / briefing) take hues roughly a quarter of
  // the wheel apart — orange, blue, green, magenta — so they can never read as
  // shades of one another; the rarer sources fill the gaps between them. Mid
  // lightness throughout so each holds up as a fill on both the dark and light
  // backgrounds, and red is left free so it keeps meaning "failed" everywhere
  // else on the page.
  // design-lint-allow-begin: categorical palette — the hue identifies a task
  // source, not a severity, so these cannot fold onto the status scale.
  const SOURCE_COLOR: Record<string, string> = {
    scheduled: '#f5851f', // orange
    email: '#d9b40f', // yellow
    istota_file: '#9ac41b', // lime
    tasks_file: '#9ac41b', // legacy alias for istota_file
    web: '#2fbf5f', // green
    cli: '#14b8c4', // cyan
    talk: '#3d8bfd', // blue
    repl: '#9b5cf0', // violet
    subtask: '#c455e0', // orchid
    briefing: '#e0479e', // magenta
    heartbeat: '#a86a3c', // brown
  };

  function sourceColor(name: string): string {
    return SOURCE_COLOR[name] ?? '#8a93a0';
  }
  // design-lint-allow-end

  const INTERACTIVE_SOURCES = new Set(['talk', 'web', 'email', 'repl', 'istota_file', 'cli']);

  interface SourceSegment {
    source: string;
    count: number;
    failed: number;
    avg: number | null;
  }

  function userSegments(u: AdminStatsUser): SourceSegment[] {
    const entries = Object.entries(u.tasks_by_source_24h ?? {}) as [string, AdminStatsUserSource][];
    return entries
      .filter(([, v]) => v.count > 0)
      .sort((a, b) => {
        // Interactive first, then by count descending — keeps the
        // useful sources at the visible left edge of the bar.
        const ai = INTERACTIVE_SOURCES.has(a[0]) ? 0 : 1;
        const bi = INTERACTIVE_SOURCES.has(b[0]) ? 0 : 1;
        if (ai !== bi) return ai - bi;
        return b[1].count - a[1].count;
      })
      .map(([source, v]) => ({
        source,
        count: v.count,
        failed: v.failed,
        avg: v.avg_duration_seconds,
      }));
  }

  function segmentTooltip(seg: SourceSegment): string {
    const parts = [`${seg.source}: ${seg.count}`];
    if (seg.failed > 0) parts.push(`${seg.failed} failed`);
    if (seg.avg !== null) parts.push(`avg ${seg.avg.toFixed(1)}s`);
    return parts.join(' · ');
  }

  function isModuleJob(name: string): boolean {
    return name.startsWith('_module.');
  }

  /** A job fires only when both of its two authors agree: the user has not
   *  switched it off in CRON.md, and the scheduler has not suspended it after
   *  N consecutive failures. Same conjunction the backend counts `jobs_active`
   *  on and `db.get_enabled_scheduled_jobs` selects on. */
  function jobRuns(j: AdminStatsJob): boolean {
    return j.enabled && !j.auto_disabled_at;
  }

  /** Three states, not two. `paused` is the user's own doing and there is a
   *  verb for it; `suspended` is the scheduler's, and it says so because the
   *  two need different things done about them. A job that is both reads as
   *  `paused`, the more informative of the two. */
  function jobStatusLabel(j: AdminStatsJob): string {
    if (!j.enabled) return 'paused';
    return j.auto_disabled_at ? 'suspended' : 'enabled';
  }

  interface PartitionedJobs {
    regular: AdminStatsJob[];
    moduleJobs: AdminStatsJob[];
  }

  function partitionJobs(jobs: AdminStatsJob[]): PartitionedJobs {
    const regular: AdminStatsJob[] = [];
    const moduleJobs: AdminStatsJob[] = [];
    for (const j of jobs) {
      (isModuleJob(j.name) ? moduleJobs : regular).push(j);
    }
    return { regular, moduleJobs };
  }

  function moduleJobSummary(jobs: AdminStatsJob[]): { failures: number; lastRun: string | null } {
    let failures = 0;
    let lastRun: string | null = null;
    for (const j of jobs) {
      failures += j.consecutive_failures;
      if (j.last_run_at && (!lastRun || j.last_run_at > lastRun)) {
        lastRun = j.last_run_at;
      }
    }
    return { failures, lastRun };
  }

  const BRAIN_LABELS: Record<string, string> = {
    claude_code: 'Claude Code',
    native: 'Native',
    tmux_claude: 'Tmux Claude',
  };

  function brainLabel(kind: string): string {
    return BRAIN_LABELS[kind] ?? kind;
  }
</script>

<!-- The AppShell + ShellHeader live in admin/+layout.svelte, shared with the
     Configuration and Logs sections. This page is the Status section's body. -->
<div class="settings admin-page">
  {#if loading && !stats}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else if stats}
    {#if stats.runtime?.mode === 'standalone'}
      <NoticeBanner
        title="Running in standalone (local single-user) mode"
        bind:collapsed={standaloneCollapsed}
      >
        <p class="standalone-lead">
          This instance runs the slimmed-down local shape. What that means here:
        </p>
        <ul class="standalone-caveats">
          {#each stats.runtime.caveats as caveat}
            <li>
              <span class="caveat-title">{caveat.title}</span>
              <span class="caveat-detail">{caveat.detail}</span>
            </li>
          {/each}
        </ul>
      </NoticeBanner>
    {/if}

    <!-- System banner -->
    <section class="card system-banner card-grid">
      <div class="banner-cell">
        <div class="cell-label">Status</div>
        <div class="cell-value">
          <span
            class="dot"
            class:dot-ok={stats.system.scheduler_healthy && !stats.brain_status?.degraded}
            class:dot-warn={stats.system.scheduler_healthy && stats.brain_status?.degraded}
            class:dot-bad={!stats.system.scheduler_healthy}
          ></span>
          {#if !stats.system.scheduler_healthy}
            Stale
          {:else if stats.brain_status?.degraded}
            Degraded
          {:else}
            Healthy
          {/if}
        </div>
        <div class="cell-sub">
          {#if stats.system.scheduler_healthy && stats.brain_status?.degraded}
            {#if stats.brain_status.active}
              on fallback ({brainLabel(stats.brain_status.active)}) · {brainLabel(
                stats.brain_status.primary,
              )} down
            {:else}
              {brainLabel(stats.brain_status.primary)} down · no fallback
            {/if}
          {:else}
            last activity {formatTimestamp(stats.system.last_scheduler_run)}
          {/if}
        </div>
      </div>
      <div class="banner-cell">
        <div class="cell-label">Version</div>
        <div class="cell-value">{stats.system.version}</div>
        <div class="cell-sub">Python {stats.system.python_version}</div>
      </div>
      <div class="banner-cell">
        <div class="cell-label">Web uptime</div>
        <div class="cell-value">{formatDuration(stats.system.uptime_seconds)}</div>
      </div>
      <div class="banner-cell">
        <div class="cell-label">Database</div>
        <div class="cell-value">{formatBytes(stats.system.db_size_bytes)}</div>
        {#if stats.storage.nextcloud_configured}
          <div class="cell-sub">mount {stats.storage.nextcloud_mount_healthy ? '✓' : '✗'}</div>
        {/if}
      </div>
    </section>

    <!-- Models / brain backend -->
    {#if stats.models && !stats.models.error}
      {@const m = stats.models}
      <section class="card">
        <header class="section-header">
          <h2>Models</h2>
        </header>
        <dl class="kv model-kv">
          <dt>Brain</dt>
          <dd>{brainLabel(m.brain_kind)}</dd>
          {#if m.endpoint}
            <dt>Endpoint</dt>
            <dd class="endpoint">
              <span class="endpoint-url">{m.endpoint}</span>{#if m.provider}<span
                  class="endpoint-provider">· {m.provider}</span
                >{/if}
            </dd>
          {/if}
        </dl>

        <dl class="kv model-kv role-kv">
          <dt>Default</dt>
          <dd class="model-value">
            <code>{m.default_model}</code>
            {#if m.default_effort}<span class="effort-chip">{m.default_effort}</span>{/if}
          </dd>
          {#each m.roles as r (r.role)}
            <dt>{r.role}</dt>
            <dd class="model-value"><code>{r.resolved}</code></dd>
          {/each}
        </dl>

        {#if m.source_type_overrides && Object.keys(m.source_type_overrides).length > 0}
          <div class="overrides">
            <div class="cell-label">Source-type routing</div>
            <dl class="kv model-kv">
              {#each Object.entries(m.source_type_overrides) as [src, kind] (src)}
                <dt>{src}</dt>
                <dd>{brainLabel(kind)}</dd>
              {/each}
            </dl>
          </div>
        {/if}

        {#if m.room_selectable && m.room_selectable.length > 0}
          <div class="overrides">
            <div class="cell-label">Rooms may select</div>
            <div class="room-selectable">
              {#each m.room_selectable as kind (kind)}
                <span class="effort-chip">{brainLabel(kind)}</span>
              {/each}
            </div>
          </div>
        {/if}
      </section>
    {/if}

    <!-- Users -->
    <section class="card">
      <header class="section-header">
        <h2>Users</h2>
      </header>
      <div class="table-scroll">
        <table class="grid users-grid">
          <thead>
            <tr>
              <th class="col-user">User</th>
              <th class="num col-total">Total</th>
              <th class="col-24h">24h activity</th>
              <th class="num col-failed">Failed</th>
              <th class="num col-avg">Avg/day</th>
              <th
                class="num col-tokens"
                title="Tokens in the last 24h. Includes spend with no task row — a nightly sleep cycle, health OCR — so this can exceed what the task counts suggest."
                >Tokens 24h</th
              >
              <th
                class="num col-cost"
                title="Money actually spent in the last 24h. A subscription's list-price equivalent and a catalog estimate are not spend, so they show a dash rather than a figure."
                >Cost 24h</th
              >
              <th
                class="col-active"
                title="Most recent interactive task. Scheduled jobs, briefings, heartbeats and subtasks don't count."
                >Last active</th
              >
            </tr>
          </thead>
          <tbody>
            {#each stats.users as u (u.username)}
              {@const segments = userSegments(u)}
              {@const totalSeg = segments.reduce((acc, s) => acc + s.count, 0)}
              <tr>
                <td>
                  <!-- A flex row inside the cell rather than on it: `display:
									     flex` on a `<td>` takes it out of the table's own
									     layout, which is what sizes these columns. -->
                  <span class="user-cell">
                    <!-- Bare: `/me` carries the reader's own hash and the
										     bot's, and nothing carries a third party's (D13), so
										     this revalidates on an ETag. A user this admin
										     shares no room with 404s — being an admin is not the
										     endpoint's predicate, deliberately — and falls back
										     to the chip the table drew before.

										     Two consequences of the keyed `{#each}`, both
										     accepted. The 60s refresh hands every row the same
										     props, so the URL never changes and nothing
										     re-requests — which is also why `Avatar`'s failure
										     chip is sticky here where the transcript's is not
										     (that one re-mints a cid per rebuild): a request
										     that failed once holds the chip until the page is
										     left. And a mount costs one request per listed user
										     at once, bounded after that by the 404's own 30s
										     negative cache. -->
                    <span class="user-face">
                      <Avatar
                        kind="user"
                        userId={u.username}
                        label={u.display_name || u.username}
                      />
                    </span>
                    <span class="username">{u.display_name || u.username}</span>
                    {#if u.is_admin}<span class="admin-badge">admin</span>{/if}
                  </span>
                </td>
                <td class="num col-total">{formatNumber(u.tasks_total)}</td>
                <td class="source-cell">
                  <div class="source-summary">
                    <span class="muted">int</span>
                    <strong>{u.tasks_interactive_24h}</strong>
                    <span class="sep">·</span>
                    <span class="muted">auto</span>
                    <strong>{formatNumber(u.tasks_automated_24h)}</strong>
                  </div>
                  {#if totalSeg > 0}
                    <div class="stack-bar" aria-label="24h source breakdown">
                      {#each segments as seg (seg.source)}
                        <span
                          class="stack-seg"
                          style="width: {(seg.count / totalSeg) * 100}%; background: {sourceColor(
                            seg.source,
                          )};"
                          title={segmentTooltip(seg)}
                        ></span>
                      {/each}
                    </div>
                    <div class="source-list">
                      {#each segments as seg (seg.source)}
                        <span class="source-pill" title={segmentTooltip(seg)}>
                          <span
                            class="dot dot-source"
                            style="background: {sourceColor(seg.source)};"
                          ></span>
                          {seg.source}
                          {formatNumber(seg.count)}
                        </span>
                      {/each}
                    </div>
                  {/if}
                </td>
                <td class="num col-failed">
                  {#if u.tasks_failed_24h > 0}
                    <span class="failed-pill">{u.tasks_failed_24h}</span>
                  {:else}
                    0
                  {/if}
                </td>
                <td class="num col-avg">{u.tasks_avg_per_day}</td>
                <td class="num col-tokens" title={usageOriginTitle(u)}
                  >{formatNumber(u.usage_tokens_24h)}</td
                >
                <td class="num col-cost">{formatCost(u.usage_cost_24h)}</td>
                <td class="col-active">{formatTimestamp(u.last_active)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Tasks -->
    <section class="card">
      <header class="section-header">
        <h2>Task activity</h2>
      </header>
      <div class="kpi-grid card-grid">
        <StatTile label="Interactive 24h" sub="{stats.tasks.interactive_avg_per_day_30d}/day (30d)">
          {formatNumber(stats.tasks.interactive_24h)}
        </StatTile>
        <!-- De-emphasized by colour alone. The row reads as one set of figures,
             so stepping this one's size down as well made it look like a
             different kind of measurement rather than a quieter one. -->
        <StatTile
          label="Automated 24h"
          sub="{formatNumber(stats.tasks.automated_avg_per_day_30d)}/day (30d)"
          valueColor="var(--text-muted)"
        >
          {formatNumber(stats.tasks.automated_24h)}
        </StatTile>
        <StatTile label="Avg duration">{stats.tasks.avg_duration_seconds}s</StatTile>
        <StatTile
          label="Failed 24h"
          sub="{(stats.tasks.error_rate_24h * 100).toFixed(2)}% error rate"
          valueColor={stats.tasks.failed_24h > 0 ? 'var(--status-warn-fg)' : ''}
        >
          {stats.tasks.failed_24h}
        </StatTile>
        <StatTile label="Total tasks" class="col-total-kpi">
          {formatNumber(stats.tasks.total)}
        </StatTile>
      </div>
      {#if Object.keys(stats.tasks.by_source).length > 0}
        {@const maxN = Math.max(...Object.values(stats.tasks.by_source))}
        <div class="source-bars">
          {#each Object.entries(stats.tasks.by_source).sort((a, b) => b[1] - a[1]) as [src, count] (src)}
            {@const failed = stats.tasks.failed_by_source_24h?.[src] ?? 0}
            <div class="source-row">
              <div class="source-label">
                <span class="dot dot-source" style="background: {sourceColor(src)};"></span>
                {src}
              </div>
              <div class="source-bar">
                <div
                  class="source-fill"
                  style="width: {(count / maxN) * 100}%; background: {sourceColor(src)};"
                ></div>
              </div>
              <div class="source-count">
                {formatNumber(count)}{#if failed > 0}<span
                    class="failed-inline"
                    title="failed in 24h">·{failed} failed</span
                  >{/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <!-- Claude Code subscription. Above Token usage and separate from it, which
         is the whole point of the pair: on a subscription deployment the cost
         column below is deliberately all dashes — a plan-equivalent list price
         is not spend — and these windows are the budget that column cannot
         report. Folding them into one card would put a live external reading
         with its own failure mode under the same heading as an aggregate over
         `task_usage`, leaving one card half-stale and half-fresh with two error
         branches. Two cards each say one thing. -->
    <!-- The key is absent unless there is a card to draw: the backend omits it
         when Claude Code is neither the brain nor the fallback, and when the
         reading could not be obtained. So this one guard is the whole gate. It
         used to render a "Plan limits unavailable" note instead of vanishing,
         so an operator who expected the reading learned why — right while a
         missing reading meant something was wrong, and wrong once we learned the
         endpoint will not serve the long-lived setup-token credential both
         server shapes deploy. That note was permanent on those hosts and named
         nothing anyone could do. `runtime.subscription_usage` carries the reason
         now, as a SKIP. -->
    {#if stats.subscription && (stats.subscription.windows ?? []).length > 0}
      {@const sub = stats.subscription}
      <section class="card">
        <header class="section-header">
          <h2>Claude Code subscription</h2>
        </header>
        <!-- Extra usage is a tile in this grid rather than a footer line
               under it, so the card reads as one row of meters the way the
               system banner above does. It is last because it is the one
               figure here that is money rather than a share of a plan
               window. -->
        <div class="kpi-grid card-grid">
          {#each sub.windows ?? [] as w (w.key)}
            <StatTile
              label={w.label}
              sub={formatResetIn(w.resets_in_seconds)}
              valueColor={utilizationColor(w.percent, sub.warn_percent, sub.high_percent)}
            >
              {formatUtilization(w.percent)}
            </StatTile>
          {/each}
          {#if sub.spend?.enabled}
            <StatTile
              label="Extra usage"
              sub={formatSpendCap(sub.spend)}
              valueColor={utilizationColor(sub.spend.percent, sub.warn_percent, sub.high_percent)}
            >
              {formatSpendUsed(sub.spend)}
            </StatTile>
          {/if}
        </div>
        <p class="usage-note">
          Updated {formatTimestamp(sub.fetched_at ?? null)}{#if sub.stale}
            — reading is stale{#if sub.error}: {sub.error}{/if}
          {/if}
        </p>
      </section>
    {/if}

    <!-- Token usage. Per-model, per-brain and per-origin live here; per-user
         lives on the Users rows above, beside that user's task counts.
         Duplicating per-user here would give the same data two places to
         disagree. -->
    {#if stats.usage && !stats.usage.error && stats.usage.totals_30d}
      {@const u24 = stats.usage.totals_24h}
      {@const u30 = stats.usage.totals_30d}
      <section class="card">
        <header class="section-header">
          <h2>Token usage</h2>
        </header>
        <div class="kpi-grid card-grid">
          <StatTile label="Tokens 24h" sub="{formatNumber(u30.total_tokens)} (30d)">
            {formatNumber(u24?.total_tokens ?? 0)}
          </StatTile>
          <StatTile label="Cost 24h" sub="{formatCost(u30.cost_by_basis)} (30d)">
            {formatCost(u24?.cost_by_basis)}
          </StatTile>
          <StatTile label="Cache hit rate" sub="30d">
            {formatPercent(u30.cache_hit_rate)}
          </StatTile>
          <!-- The two context measures are a first and a max over per-request
               prompt sizes. They do not sum, so they are never shown beside a
               token total as though they were the same kind of number. -->
          <StatTile label="Avg initial context" sub="30d, measured rows only">
            {formatContext(u30.avg_initial_context_tokens)}
          </StatTile>
          <StatTile label="Avg peak context" sub="30d, measured rows only">
            {formatContext(u30.avg_peak_context_tokens)}
          </StatTile>
        </div>

        {#if (stats.usage.by_model_30d ?? []).length > 0}
          <h3 class="usage-sub">
            By model (30d){#if (stats.usage.by_model_30d_omitted ?? 0) > 0}<span
                class="usage-omitted"
              >
                — top 5 of {(stats.usage.by_model_30d ?? []).length +
                  (stats.usage.by_model_30d_omitted ?? 0)}</span
              >{/if}
          </h3>
          <div class="usage-rows">
            {#each stats.usage.by_model_30d ?? [] as g (g.key)}
              <div class="usage-row">
                <div class="usage-key">{g.key}</div>
                <div class="usage-num">{formatNumber(g.total_tokens)}</div>
                <div class="usage-cost">{formatCost(g.cost_by_basis)}</div>
              </div>
            {/each}
          </div>
        {/if}

        {#if (stats.usage.by_brain_30d ?? []).length > 0}
          <h3 class="usage-sub">By brain (30d)</h3>
          <div class="usage-rows">
            {#each stats.usage.by_brain_30d ?? [] as g (g.key)}
              <div class="usage-row">
                <div class="usage-key">{g.key}</div>
                <div class="usage-num">{formatNumber(g.total_tokens)}</div>
                <div class="usage-cost">{formatCost(g.cost_by_basis)}</div>
              </div>
            {/each}
          </div>
        {/if}

        {#if (stats.usage.by_origin_24h ?? []).length > 0}
          <h3 class="usage-sub">By origin (24h)</h3>
          <div class="usage-rows">
            {#each stats.usage.by_origin_24h ?? [] as g (g.key)}
              <div class="usage-row">
                <div class="usage-key">{g.key}</div>
                <div class="usage-num">{formatNumber(g.total_tokens)}</div>
                <div class="usage-cost">{formatCost(g.cost_by_basis)}</div>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Two honesty counters. A tmux-brain task spends real tokens and
             writes no row, and the native brain records no context; a synthetic
             zero for either would make this pane complete and wrong. -->
        {#if (stats.usage.unmeasured_tasks_24h ?? 0) > 0 || (stats.usage.context_unmeasured_rows_30d ?? 0) > 0}
          <p class="usage-note">
            {#if (stats.usage.unmeasured_tasks_24h ?? 0) > 0}
              {formatNumber(stats.usage.unmeasured_tasks_24h ?? 0)} task(s) in the last 24h recorded no
              usage.
            {/if}
            {#if (stats.usage.context_unmeasured_rows_30d ?? 0) > 0}
              {formatNumber(stats.usage.context_unmeasured_rows_30d ?? 0)} task row(s) in 30d carry no
              context measurement.
            {/if}
          </p>
        {/if}
      </section>
    {:else if stats.usage?.error}
      <section class="card">
        <header class="section-header">
          <h2>Token usage</h2>
        </header>
        <p class="usage-note">Usage stats unavailable: {stats.usage.error}</p>
      </section>
    {/if}

    <!-- Modules -->
    {#if Object.keys(stats.modules).length > 0}
      <section class="card">
        <header class="section-header">
          <h2>Modules</h2>
        </header>
        <div class="module-grid card-grid">
          {#each Object.entries(stats.modules) as [name, mod] (name)}
            <div class="module-card" class:module-warn={moduleErrorCount(mod) > 0}>
              <div class="module-name">{name}</div>
              <dl class="module-fields">
                {#each Object.entries(mod) as [k, v] (k)}
                  <dt>{fieldLabel(k)}</dt>
                  <dd>
                    {v === null
                      ? '—'
                      : TIMESTAMP_KEYS.has(k)
                        ? formatTimestamp(String(v))
                        : String(v)}
                  </dd>
                {/each}
              </dl>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <!-- Scheduler -->
    <section class="card">
      <header class="section-header">
        <h2>Scheduler</h2>
        <span class="muted meta"
          >{stats.scheduler.jobs_active} active · {stats.scheduler.jobs_paused} paused</span
        >
      </header>
      {#if stats.scheduler.jobs.length === 0}
        <p class="empty">No scheduled jobs.</p>
      {:else}
        {@const parts = partitionJobs(stats.scheduler.jobs)}
        <div class="table-scroll">
          <table class="grid jobs-grid">
            <thead>
              <tr>
                <th>Job</th>
                <th class="col-cron">Cron</th>
                <th class="col-status">Status</th>
                <th class="col-lastrun">Last run</th>
                <th class="num col-failures"
                  ><span class="label-full">Failures</span><span class="label-abbr">Fails</span></th
                >
              </tr>
            </thead>
            <tbody>
              {#each parts.regular as j (j.id)}
                {@const expandable = !!j.last_error}
                <tr
                  class:row-error={j.consecutive_failures > 0}
                  class:row-clickable={expandable}
                  onclick={() => expandable && toggleJob(j.id)}
                >
                  <td>
                    <span class="username">{j.user_id}</span>
                    <span class="muted">/</span>
                    <span class="job-name">{j.name}</span>
                  </td>
                  <td class="col-cron"><code>{j.cron}</code></td>
                  <td class="col-status">
                    <span class="dot" class:dot-ok={jobRuns(j)} class:dot-mute={!jobRuns(j)}></span>
                    <span class="status-label">{jobStatusLabel(j)}</span>
                  </td>
                  <td class="col-lastrun">{formatTimestamp(j.last_run_at)}</td>
                  <td class="num col-failures">{j.consecutive_failures}</td>
                </tr>
                {#if expandable && expandedJobs[j.id]}
                  <tr class="error-row">
                    <td colspan="5"><pre>{j.last_error}</pre></td>
                  </tr>
                {/if}
              {/each}
              {#if parts.moduleJobs.length > 0}
                {@const summary = moduleJobSummary(parts.moduleJobs)}
                <tr
                  class:row-error={summary.failures > 0}
                  class="row-clickable module-summary-row"
                  onclick={() => (modulesExpanded = !modulesExpanded)}
                >
                  <td>
                    <span class="disclosure">{modulesExpanded ? '▾' : '▸'}</span>
                    <span class="muted">Module pollers</span>
                    <span class="badge">{parts.moduleJobs.length}</span>
                  </td>
                  <td class="col-cron"><span class="muted">—</span></td>
                  <td class="col-status"><span class="muted">—</span></td>
                  <td class="col-lastrun">{formatTimestamp(summary.lastRun)}</td>
                  <td class="num col-failures">{summary.failures}</td>
                </tr>
                {#if modulesExpanded}
                  {#each parts.moduleJobs as j (j.id)}
                    {@const expandable = !!j.last_error}
                    <tr
                      class:row-error={j.consecutive_failures > 0}
                      class:row-clickable={expandable}
                      class="module-child-row"
                      onclick={() => expandable && toggleJob(j.id)}
                    >
                      <td>
                        <span class="username">{j.user_id}</span>
                        <span class="muted">/</span>
                        <span class="job-name">{j.name}</span>
                      </td>
                      <td class="col-cron"><code>{j.cron}</code></td>
                      <td class="col-status">
                        <span class="dot" class:dot-ok={jobRuns(j)} class:dot-mute={!jobRuns(j)}
                        ></span>
                        <span class="status-label">{jobStatusLabel(j)}</span>
                      </td>
                      <td class="col-lastrun">{formatTimestamp(j.last_run_at)}</td>
                      <td class="num col-failures">{j.consecutive_failures}</td>
                    </tr>
                    {#if expandable && expandedJobs[j.id]}
                      <tr class="error-row">
                        <td colspan="5"><pre>{j.last_error}</pre></td>
                      </tr>
                    {/if}
                  {/each}
                {/if}
              {/if}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Storage -->
    <section class="card">
      <header class="section-header">
        <h2>Storage</h2>
      </header>
      <dl class="kv">
        <dt>Database size</dt>
        <dd>{formatBytes(stats.storage.db_size_bytes)}</dd>
        <dt>Backups</dt>
        <dd>{stats.storage.backups_count}</dd>
        <dt>Last backup</dt>
        <dd>{formatTimestamp(stats.storage.last_backup)}</dd>
        {#if stats.storage.nextcloud_configured}
          <dt>Nextcloud mount</dt>
          <dd>
            <span
              class="dot"
              class:dot-ok={stats.storage.nextcloud_mount_healthy}
              class:dot-bad={!stats.storage.nextcloud_mount_healthy}
            ></span>
            {stats.storage.nextcloud_mount_healthy ? 'healthy' : 'unavailable'}
          </dd>
        {/if}
      </dl>
    </section>

    <!-- Bot icon.
         The one control on a page of read-only cards, so it sits at the foot
         rather than above the status an operator scans first. A card here and
         not a pane of its own: the admin dashboard is one page, and a single
         upload control does not earn a section in the sidebar. -->
    <section class="card">
      <header class="section-header">
        <h2>Bot icon</h2>
      </header>
      <Field label="Picture" labelled={false} wide error={botIconError} warning={botIconNote}>
        <AvatarPicker
          pickLabel="Choose the bot icon"
          prompt="Click the icon to choose a file, or drop or paste one here."
          accept={AVATAR_ACCEPT}
          busyLabel={botIconBusyLabel}
          removable={!!identity.user.avatars?.bot}
          onPick={(file) => void uploadBotIcon(file)}
          onRemove={removeBotIcon}
        >
          {#snippet preview()}
            <!-- Named rather than decorative: here the picture is the thing
                 being edited, and the only other signal is whether Remove
                 exists. -->
            <Avatar
              kind="bot"
              version={identity.user.avatars?.bot ?? null}
              label={identity.user.bot_name || 'Istota'}
              alt="The bot icon"
            />
          {/snippet}
        </AvatarPicker>
      </Field>
      <p class="hint">
        Shown wherever the web UI names the bot. It applies to everyone on this deployment.
        {#if stats.storage.nextcloud_username}
          This is not the picture Nextcloud shows for
          <code>{stats.storage.nextcloud_username}</code>, and changing it here does not change that
          one — set it in Nextcloud if you want them to match.
        {:else}
          This is separate from the bot's Nextcloud profile picture, which Nextcloud shows in Talk;
          changing it here does not change that one.
        {/if}
      </p>
    </section>

    {#if stats.error}
      <div class="banner error">Partial data: {stats.error}</div>
    {/if}

    <p class="refresh-note">Auto-refreshes every 60s.</p>
  {/if}
</div>

<style>
  /* Layout primitives (.settings / .card / .grid / .banner / .placeholder /
	   .section-header / .hint) come from web/src/lib/styles/settings.css.
	   Admin-specific bits below: KPIs, source bars, dot indicators. */

  /* Page metadata, not content. It sat in the app bar until the admin sidebar
	   moved that bar into the layout, which is shared with Configuration and
	   Logs — neither of which auto-refreshes, so the note belongs with the data
	   it describes rather than with the chrome. */
  .refresh-note {
    margin-top: var(--space-4);
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-align: right;
  }

  /* Standalone-mode notice content — rendered inside the NoticeBanner slot at
     the top of the page. The banner chrome (border, toggle, title) lives in the
     NoticeBanner component, and so does the body type size: this slot used to
     restate 0.9rem/0.85rem here, which put the one banner a reader meets on a
     fresh local install a step above every other banner in the app. Nothing
     here sets a font-size — only the list's own layout, weight and dimming. */
  .standalone-lead {
    margin: 0 0 var(--space-2);
    opacity: 0.85;
  }

  .standalone-caveats {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .standalone-caveats li {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .caveat-title {
    font-weight: 600;
  }

  .caveat-detail {
    opacity: 0.8;
  }

  /* `.settings .card` (from settings.css) sets `display: flex; flex-direction:
	   column` at specificity (0,2,0), which beats the global `.card-grid`
	   layout (0,1,0). The banner is itself a `.card`, so it needs `display: grid`
	   restated here (same specificity as `.settings .card`, but scoped, so it
	   wins) — otherwise the cells stack in a column. The grid track sizing still
	   comes from `.card-grid` via `--card-min` / `--card-gap`. */
  .admin-page .system-banner {
    display: grid;
    --card-min: 150px;
    --card-gap: 0.75rem 1.5rem;
  }

  .banner-cell {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .cell-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .cell-value {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .cell-sub {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-dim);
    margin-right: var(--space-1);
    flex-shrink: 0;
  }
  .dot-ok {
    background: var(--status-dot-ok);
  }
  .dot-bad {
    background: var(--status-dot-bad);
  }
  .dot-warn {
    background: var(--status-dot-warn);
  }
  .dot-mute {
    background: var(--text-dim);
  }
  .dot-source {
    width: 6px;
    height: 6px;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* Face, name, badge on one line. The name is what may be too long for the
	   column, so it is the part that gives — the face keeps its box. */
  .user-cell {
    display: flex;
    align-items: center;
    min-width: 0;
  }
  .user-face {
    /* Set on the avatar's own wrapper, never on the cell or the row:
		   --avatar-size inherits, and a shared container would resize every
		   avatar nested under it. */
    --avatar-size: 1.5rem;
    display: flex;
    flex: 0 0 auto;
    margin-right: var(--space-2);
  }
  .username {
    font-weight: 500;
  }
  /* Only in this cell, which is the one that now shares its width with a
	   picture. The scheduler tables use the same class and are left alone. */
  .user-cell .username {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Neutral count chip (module-poller row). Distinct from .admin-badge — this
	   one carries a number, not an identity. */
  .badge {
    display: inline-block;
    margin-left: var(--space-2);
    font-size: var(--text-xs);
    padding: 0.05rem var(--space-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    color: var(--text-muted);
  }

  /* Matches .effort-chip's metrics so the two chips read as one family;
	   amber is the identity accent, not a severity. */
  .admin-badge {
    display: inline-block;
    margin-left: var(--space-2);
    padding: 0.05rem var(--space-2);
    font-size: 0.55rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-radius: var(--radius-pill);
    background: color-mix(in srgb, var(--accent-amber) 18%, transparent);
    color: var(--accent-amber);
  }

  /* The tiles themselves are `StatTile`; this sizes the wall they sit in. */
  .kpi-grid {
    --card-min: 140px;
    --card-gap: 0.75rem 1.5rem;
  }

  /* Source distribution bars (horizontal, one per source_type). */
  .source-bars {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-top: var(--space-1);
  }

  .source-row {
    display: grid;
    grid-template-columns: minmax(80px, 100px) 1fr minmax(70px, max-content);
    gap: var(--space-3);
    align-items: center;
    font-size: var(--text-sm);
  }

  .source-label {
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: var(--space-1);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .source-bar {
    height: 6px;
    background: var(--surface-base);
    border-radius: 3px;
    overflow: hidden;
  }

  .source-fill {
    height: 100%;
  }

  .source-count {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .failed-inline {
    margin-left: var(--space-2);
    color: var(--status-danger-fg);
    font-size: var(--text-xs);
  }

  /* Per-user 24h breakdown — stacked bar + tag list. */
  .users-grid {
    /* The sized columns below come to 48.5rem; this is that plus 10rem for
		   `col-24h`, the one column deliberately left auto, since it carries the
		   stacked bar and the per-source chips. The sized columns must not add up to
		   the whole table, or that column is squeezed to nothing at the declared
		   minimum — which is what the two usage columns did on mobile (ISSUE-276).
		   In rem rather than px for the same reason: the columns are rem, so a px
		   floor stops covering them the moment the reader picks a larger text
		   scale. At 120% the columns alone came to within a few pixels of the old
		   904px, which left 24h activity nothing again — the same defect reached by
		   changing a setting rather than a breakpoint. 56.5rem was that 904px at the
		   default scale; the extra 2rem is the User column's picture and its gap,
		   added to the floor rather than taken out of `col-24h`. */
    min-width: 58.5rem;
  }

  /* .grid is table-layout: fixed, so a cell min-width is ignored and unsized
	   columns split the table evenly. Size the narrow columns explicitly and
	   leave 24h activity auto so it soaks up whatever is left — it carries the
	   stacked bar plus the per-source chips and needs the room; username and
	   failure count do not.

	   The User column carries a 1.5rem picture and a --space-2 gap ahead of the
	   name, so it is 2rem wider than the 9rem it held before, and the table's
	   own min-width above went up by the same 2rem. Both, or the shortfall
	   comes out of the one unsized column — which is ISSUE-276 exactly, and is
	   what `adminTables.test.ts` does the arithmetic for. */
  .users-grid .col-user {
    width: 11rem;
  }

  /* Total holds a lifetime count, which reaches seven figures on a long-running
	   install; measured, "1,284,507" at --text-sm needs 66px and 4.5rem leaves a
	   63px content box. Failed is a small integer or a pill. */
  .users-grid .col-total {
    width: 5rem;
  }

  .users-grid .col-failed {
    width: 4.5rem;
  }

  .users-grid .col-avg {
    width: 5rem;
  }

  .users-grid .col-tokens {
    width: 7rem;
  }

  .users-grid .col-cost {
    width: 7rem;
  }

  .users-grid .col-active {
    width: 9rem;
  }

  .usage-sub {
    margin: var(--space-2) 0 var(--space-1);
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-muted);
  }

  .usage-rows {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .usage-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: var(--space-2);
    align-items: baseline;
    font-size: var(--text-sm);
  }

  .usage-key {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .usage-num,
  .usage-cost {
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  .usage-cost {
    min-width: 7rem;
    color: var(--text-muted);
  }

  .usage-omitted {
    font-weight: 400;
    text-transform: none;
    color: var(--text-dim);
  }

  .usage-note {
    margin-top: var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .users-grid .col-24h {
    width: auto;
  }

  .source-summary {
    font-size: var(--text-sm);
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1);
  }

  .source-summary strong {
    font-variant-numeric: tabular-nums;
  }

  .sep {
    color: var(--text-dim);
    margin: 0 0.15rem;
  }

  .stack-bar {
    display: flex;
    height: 5px;
    border-radius: 3px;
    overflow: hidden;
    margin: var(--space-1) 0;
    background: var(--surface-base);
  }

  .stack-seg {
    display: block;
    height: 100%;
  }

  .source-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: 0.15rem;
  }

  .source-pill {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .failed-pill {
    display: inline-block;
    padding: 0.05rem var(--space-2);
    background: rgba(255, 90, 90, 0.15);
    color: var(--status-danger-fg);
    border-radius: var(--radius-pill);
    font-size: var(--text-xs);
  }

  /* Scheduler table */
  .jobs-grid {
    min-width: 580px;
  }

  .job-name {
    word-break: break-word;
  }

  .label-abbr {
    display: none;
  }

  .row-clickable {
    cursor: pointer;
  }

  .row-clickable:hover {
    background: var(--surface-raised);
  }

  .row-error td:first-child::before {
    content: '!';
    display: inline-block;
    color: var(--status-danger-fg);
    margin-right: var(--space-2);
    font-weight: 700;
  }

  .module-summary-row td {
    color: var(--text-muted);
  }

  .module-child-row td:first-child {
    padding-left: var(--space-6);
  }

  .disclosure {
    display: inline-block;
    width: 1em;
    color: var(--text-dim);
    margin-right: var(--space-1);
  }

  .error-row td {
    background: var(--surface-base);
    padding: var(--space-2) var(--space-3);
  }

  .error-row pre {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    white-space: pre-wrap;
  }

  code {
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }

  .module-grid {
    --card-min: 220px;
  }

  .module-card {
    background: var(--surface-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
  }

  .module-warn {
    border-color: var(--status-warn-bg);
  }

  .module-name {
    font-weight: 600;
    font-size: var(--text-base);
    margin-bottom: var(--space-2);
  }

  .module-fields {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.1rem var(--space-3);
    margin: 0;
    font-size: var(--text-xs);
  }

  .module-fields dt {
    color: var(--text-dim);
  }

  .module-fields dd {
    margin: 0;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  /* Short key/value pairs, which read better with the columns further
     apart than the shared default. */
  .kv {
    --kv-gap: var(--space-6);
  }

  .kv dt {
    color: var(--text-dim);
  }

  .kv dd {
    margin: 0;
  }

  /* Models / brain backend */
  .model-kv {
    gap: var(--space-2) 1.25rem;
    align-items: baseline;
  }

  .model-value {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  /* Separate the config block (Brain/Endpoint) from the resolution list. */
  .role-kv {
    margin-top: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px solid var(--border-subtle);
  }

  .role-kv dt {
    text-transform: capitalize;
  }

  .endpoint {
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    font-weight: 400;
    color: var(--text-secondary);
  }

  .endpoint-url {
    word-break: break-all;
  }

  .endpoint-provider {
    margin-left: var(--space-2);
    color: var(--text-dim);
    white-space: nowrap;
  }

  .effort-chip {
    display: inline-block;
    padding: 0.05rem var(--space-2);
    font-size: 0.55rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-radius: var(--radius-pill);
    background: var(--status-info-bg);
    color: var(--status-info-fg);
  }

  .room-selectable {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .overrides {
    margin-top: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  /* Mobile: keep every column and scroll the table sideways, the same treatment
	   the briefings tables get. Hiding columns was the previous answer and it did
	   not survive two more of them. `.grid` is table-layout: fixed, so the widths
	   below are honoured whatever the screen is, and dropping the table's
	   min-width never made it narrower — it made `col-24h`, the one column left
	   auto on purpose, absorb everything that no longer fit. Once Tokens and Cost
	   were added there was nothing left to absorb: measured at a 390px viewport
	   that column came out exactly 0px wide, with the headings painted over each
	   other. `.table-scroll` scrolls instead. */
  @media (max-width: 768px) {
    /* The 36.5rem of column widths below, plus 11rem for the auto 24h column —
		   a rem more than the desktop table leaves it, since the chips have a whole
		   phone screen less to be legible in. Every column keeps a legible width and
		   the surplus becomes horizontal scroll. */
    .users-grid {
      min-width: 47.5rem;
    }
    /* Tighten the columns whose content has a known short bound, so that scroll
		   stays as short as it can be, and leave the numeric ones at a width their
		   strings fit. Nothing sets `overflow` on a `.grid` cell, so a number too
		   wide for its box paints over the column beside it rather than wrapping —
		   `toLocaleString` puts commas in but no break opportunity. Total is the one
		   that bit: it keeps its (widened) desktop width here rather than the 3.5rem
		   this block used to give it. */
    /* Widened by the same 2rem the desktop column was, so the picture is not
		   paid for out of `col-24h`. The name has less room than it had, which is
		   what the ellipsis on `.user-cell .username` is for — at this width it
		   was already the column most likely to be cut. */
    .users-grid .col-user {
      width: 8.5rem;
    }
    .users-grid .col-failed {
      width: 3.5rem;
    }
    /* "1234d ago" is the longest `formatTimestamp` produces — it has no upper
		   branch, so a dormant account counts days indefinitely. */
    .users-grid .col-active {
      width: 5rem;
    }
    .users-grid .col-avg {
      width: 4rem;
    }
    .users-grid .col-tokens {
      width: 5rem;
    }
    .users-grid .col-cost {
      width: 5.5rem;
    }
    /* The user column is too narrow to hold name + chip on one line, and the
		   wrap leaves the chip stranded under an off-centre name. Admin status
		   isn't actionable from the mobile view, so drop it. */
    .admin-badge {
      display: none;
    }
    /* The narrow-screen step-down for the KPI numerals is gone with the
       hand-rolled tile: StatTile's default sits at --text-lg (1.05rem), which
       is already under the 1.1rem this breakpoint was asking for. */
  }

  @media (max-width: 640px) {
    .col-cron {
      display: none;
    }
    /* :global because the subject is a class handed to StatTile now, and
       Svelte prunes a selector whose subject it cannot see in the markup —
       silently. Scoped under the page container, so it is placement rather
       than a leak. */
    .admin-page :global(.col-total-kpi) {
      display: none;
    }
    .jobs-grid {
      min-width: 0;
    }
    /* Fixed table layout splits unsized columns evenly, which gave the job
		   name the same quarter of a phone-width table as a status dot and a
		   one-digit failure count. Pin the three narrow columns to just what
		   their content (and header) needs and leave Job unsized so it takes
		   the remainder — same approach as .users-grid. */
    .jobs-grid .col-status {
      width: 3.75rem;
    }
    .jobs-grid .col-lastrun {
      width: 4.5rem;
    }
    .jobs-grid .col-failures {
      width: 3.25rem;
    }
    /* "Failures" is one long word that can't wrap; the short form is what
		   lets that column shrink to its integer. */
    .col-failures .label-full {
      display: none;
    }
    .col-failures .label-abbr {
      display: inline;
    }
    /* Source greys are hard to tell apart in the stack-bar at any width;
		   on mobile the bar is even narrower, so keep the labelled chips
		   visible — they're the only colour-independent legend the user
		   gets when hover tooltips aren't available. */
    .source-list {
      gap: var(--space-1);
    }
    .source-pill {
      font-size: 0.7rem;
    }
  }

  @media (max-width: 480px) {
    .col-status .status-label {
      display: none;
    }
    .col-lastrun {
      max-width: 6rem;
    }
    .admin-page .system-banner {
      grid-template-columns: 1fr 1fr;
    }
  }

  /* No light-theme override block: every status color on this page now comes
	   from the --status-* tokens, which define both themes. The JS chart color
	   constants (SOURCE_COLOR) stay hardcoded — they are a categorical data-viz
	   palette on their own colored swatches, not a severity scale. */
</style>
