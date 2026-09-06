import { base } from '$app/paths';
import { uploadFromPath, nativeUploadAvailable, type Picked } from '$lib/platform/nativePicker';
import { noteTransport } from '$lib/stores/connectivity';
import type { BasemapSpec } from '$lib/basemap';

class AuthError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'AuthError';
  }
}

async function apiFetch<T>(path: string, init?: RequestInit, timeoutMs = 0): Promise<T> {
  // `fetch` has no timeout of its own: a stalled connection (a mobile handover,
  // a proxy holding the socket) hangs until the OS gives up, which can be
  // minutes or never. A caller whose own state machine is blocked on the
  // result — the chat room stream's recovery routine — passes a bound so the
  // request rejects and the state is released, and so a late resolve can never
  // clobber whatever replaced it in the meantime.
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let timedOut = false;
  if (timeoutMs > 0 && !init?.signal) {
    controller = new AbortController();
    timer = setTimeout(() => {
      timedOut = true;
      controller?.abort();
    }, timeoutMs);
  }
  try {
    let resp: Response;
    try {
      resp = await fetch(`${base}/api${path}`, {
        ...init,
        credentials: 'same-origin',
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (e) {
      // Every request in the app comes through here, which makes this the one
      // place that sees what the network is actually doing (ISSUE-202). A
      // `fetch` rejection is a transport failure or an abort, and only the
      // first is evidence about the server: a caller that cancelled its own
      // request has said nothing about the connection, so it must not raise
      // the offline banner. No caller passes a signal today — the same
      // condition the timeout guard above is written against — and the check
      // is here so the first one to do so cannot introduce that quietly.
      if (!init?.signal?.aborted) noteTransport(false, timedOut ? 'timeout' : 'unreachable');
      throw e;
    }
    // Whatever it says, something answered. A 401 or a 500 is a server, and
    // the connectivity store's whole job is to keep that apart from silence.
    // Reported before the status branches, since a status is an answer.
    noteTransport(true);
    if (resp.status === 401) throw new AuthError();
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    try {
      // **Awaited**, so the read happens inside this `try` and ahead of the
      // `finally` below. `return resp.json()` cleared the timer on the headers
      // alone and left the body unbounded — a proxy that flushes headers and
      // then stalls is the same hang the bound exists for, which is what
      // `sendChatMessage` has always said about its own body read. It also
      // reported that stall to the connectivity store as a reachable server,
      // which is worse than saying nothing: the probe rides on this call, so a
      // half-delivered answer would have cleared the offline banner.
      return await resp.json();
    } catch (e) {
      // Only our own abort. A body that is not JSON is an error page, which
      // the status already described, and says nothing about the connection.
      if (timedOut) noteTransport(false, 'timeout');
      throw e;
    }
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export interface NextcloudTokenStatus {
  connected: boolean;
  expires_at: string | null;
}

/** Ways to reach the bot outside the web UI, as actually deployed. */
export interface UserContact {
  // The user's plus-addressed inbound address, or null when email is off.
  email: string | null;
  talk: boolean;
}

export interface User {
  username: string;
  display_name: string;
  bot_name: string;
  is_admin: boolean;
  // Absent on a server that predates the field.
  contact?: UserContact;
  features: {
    chat: boolean;
    feeds: boolean;
    location: boolean;
    money: boolean;
    health: boolean;
    briefings: boolean;
    google_workspace: boolean;
    google_workspace_enabled: boolean;
    admin: boolean;
  };
  // null when the operator hasn't enabled encrypted token storage.
  nextcloud_token?: NextcloudTokenStatus | null;
  // Content hashes for the two identities every page renders, so the client
  // can build an immutable URL for each without a round trip. Null means no
  // picture is stored — render the fallback and issue no request. Absent on a
  // server that predates the field.
  avatars?: { user: string | null; bot: string | null };
}

export async function disconnectNextcloudToken(): Promise<{ ok: boolean; was_connected: boolean }> {
  return apiFetch('/settings/nextcloud-token', { method: 'DELETE' });
}

export interface AdminStatsUserSource {
  count: number;
  failed: number;
  avg_duration_seconds: number | null;
}

export interface AdminStatsUser {
  username: string;
  display_name: string;
  is_admin: boolean;
  tasks_total: number;
  tasks_last_24h: number;
  tasks_avg_per_day: number;
  tasks_by_source_24h: Record<string, AdminStatsUserSource>;
  tasks_interactive_24h: number;
  tasks_automated_24h: number;
  tasks_failed_24h: number;
  last_active: string | null;
  // Usage sits on the same row as the task counts, so "how much is this user
  // costing" is answered where "how much is this user running" already is.
  //
  // `usage_cost_*` is a map keyed by cost basis, never a scalar: one user's
  // rows can span bases when an operator switches the CLI's auth mid-window,
  // and nothing sums across them. `usage_rows_24h` can exceed
  // `tasks_last_24h` by design — it includes spend with no task row at all
  // (a nightly sleep cycle, health OCR), which `usage_by_origin_24h` is what
  // makes legible.
  usage_tokens_24h: number;
  usage_tokens_30d: number;
  usage_cost_24h: Record<string, number>;
  usage_cost_30d: Record<string, number>;
  usage_by_origin_24h: Record<string, { rows: number; tokens: number }>;
  usage_avg_initial_context: number | null;
  usage_avg_peak_context: number | null;
  usage_cache_hit_rate_24h: number;
  usage_rows_24h: number;
  usage_unmeasured_24h: number;
}

export interface AdminUsageTotals {
  rows: number;
  measured_rows: number;
  billed_input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  cost_by_basis: Record<string, number>;
  avg_initial_context_tokens: number | null;
  avg_peak_context_tokens: number | null;
  context_rows: number;
}

export interface AdminUsageGroup extends AdminUsageTotals {
  key: string;
}

export interface AdminStatsUsage {
  totals_24h?: AdminUsageTotals;
  totals_30d?: AdminUsageTotals;
  by_model_30d?: AdminUsageGroup[];
  // The model list is capped at five; this is how many it left out.
  by_model_30d_omitted?: number;
  by_brain_30d?: AdminUsageGroup[];
  by_origin_24h?: AdminUsageGroup[];
  unmeasured_tasks_24h?: number;
  context_unmeasured_rows_30d?: number;
  // Best-effort like every other section: a failure is an error string in the
  // payload rather than a 500 on the whole dashboard.
  error?: string;
}

/** One rate-limit window of the Claude Code plan, as the endpoint reports it. */
export interface AdminSubscriptionWindow {
  /** Stable id — `session`, `weekly_all`, `weekly_scoped:fable`. */
  key: string;
  label: string;
  /** 0–100, clamped server-side. */
  percent: number;
  resets_at: string | null;
  /** Floored at 0; null when the window has no scheduled reset. */
  resets_in_seconds: number | null;
  /** The server's own severity scale. Carried, never acted on — see the card. */
  severity: string;
  is_active: boolean | null;
}

/** Pay-as-you-go credits beyond the plan, in minor units with a currency. */
export interface AdminSubscriptionSpend {
  enabled: boolean;
  used_minor: number;
  limit_minor: number;
  currency: string;
  /** Minor units per major = 10 ** exponent. Never assume 2. */
  exponent: number;
  percent: number;
}

/**
 * Plan utilization for the Claude Code subscription.
 *
 * `available` is true wherever this key is present at all, kept so the shape
 * does not change under a client that still reads it. A reading the server
 * could not obtain omits the key instead, so there is no card to draw and
 * `runtime.subscription_usage` carries the reason as a SKIP. Every field is
 * optional because the whole section degrades to `{error}` when it fails — the
 * same best-effort shape `usage` has.
 */
export interface AdminSubscription {
  available?: boolean;
  windows?: AdminSubscriptionWindow[];
  /** Null when the payload carried no credit block at all. */
  spend?: AdminSubscriptionSpend | null;
  fetched_at?: string | null;
  /** Real numbers from an earlier fetch, plus the failure that made them old. */
  stale?: boolean;
  /** The resolver's branch name (`env` / `file` / `keychain`), never a token. */
  token_source?: string;
  /** The operator's own thresholds. The card tints by these and never by a
   *  literal of its own, or a configured threshold is silently ignored. */
  warn_percent?: number;
  high_percent?: number;
  error?: string;
}

export interface AdminStatsJob {
  id: number;
  user_id: string;
  name: string;
  cron: string;
  /** What the user asked for in CRON.md. Not on its own whether the job runs. */
  enabled: boolean;
  /** When the scheduler suspended the job after N consecutive failures, or
   *  null. The daemon's own column, kept apart from `enabled` because CRON.md
   *  overwrites that one on every sync tick. A job runs only when it is
   *  enabled and not suspended. */
  auto_disabled_at: string | null;
  last_run_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
}

export interface AdminStats {
  system: {
    version: string;
    uptime_seconds: number;
    db_size_bytes: number;
    python_version: string;
    last_scheduler_run: string | null;
    scheduler_healthy: boolean;
  };
  users: AdminStatsUser[];
  scheduler: {
    jobs_total: number;
    jobs_active: number;
    jobs_paused: number;
    jobs: AdminStatsJob[];
    last_errors: { job_name: string; error: string; timestamp: string | null }[];
  };
  modules: Record<string, Record<string, unknown>>;
  usage: AdminStatsUsage;
  /** Absent unless there is a card to draw: Claude Code is the brain or the
   *  fallback, and the endpoint returned windows. See `_admin_subscription_section`. */
  subscription?: AdminSubscription;
  tasks: {
    total: number;
    last_24h: number;
    avg_per_day_30d: number;
    by_source: Record<string, number>;
    failed_by_source_24h: Record<string, number>;
    avg_duration_seconds: number;
    error_rate_24h: number;
    failed_24h: number;
    interactive_24h: number;
    automated_24h: number;
    interactive_avg_per_day_30d: number;
    automated_avg_per_day_30d: number;
  };
  storage: {
    db_size_bytes: number;
    backups_count: number;
    last_backup: string | null;
    nextcloud_configured: boolean;
    nextcloud_mount_healthy: boolean;
    /** The bot's own Nextcloud account, for the bot-icon control's copy. */
    nextcloud_username?: string | null;
  };
  runtime?: {
    mode: 'standalone' | 'server';
    caveats: { title: string; detail: string }[];
  };
  models?: {
    brain_kind: string;
    default_model: string;
    default_effort: string | null;
    roles: { role: string; resolved: string }[];
    endpoint?: string;
    provider?: string;
    source_type_overrides?: Record<string, string>;
    /** Brain kinds a room may pin (`[brain] room_selectable`, intersected with
     * the kinds this build can construct). Absent when the operator has listed
     * none, which is the shipped default. */
    room_selectable?: string[];
    error?: string;
  };
  brain_status?: {
    degraded: boolean;
    active: string | null;
    primary: string;
    reason?: string | null;
    error?: string;
  };
  error?: string;
}

export async function getAdminStats(): Promise<AdminStats> {
  return apiFetch<AdminStats>('/admin/stats');
}

// ---- Admin logs + configuration (ISSUE-203) ----

export interface AdminLogSource {
  id: string;
  label: string;
  kind: 'file' | 'db';
  description: string;
  available: boolean;
  detail: string;
  /** Whether this source's timestamps are UTC or the server's local clock.
   *  Labelled rather than converted — a log line's stamp is what the server
   *  wrote, and silently shifting it makes it un-greppable against the file. */
  time_basis: 'utc' | 'server-local';
  path: string | null;
  bytes: number;
  files: number;
}

export interface AdminLogRecord {
  /** Opaque and monotonic within a source: a byte position for the file
   *  source, a row id for the DB source. Never a path. */
  cursor: string;
  timestamp: string | null;
  level: string;
  logger: string | null;
  message: string;
  task_id: number | null;
  user_id: string | null;
  source_type: string | null;
}

export interface AdminLogPage {
  /** Oldest-first, i.e. reading order. */
  records: AdminLogRecord[];
  /** Cursor for the previous (older) page; null once the start is reached. */
  next_before: string | null;
  /** Where a live tail should resume from. */
  tail_cursor: string | null;
  /** The scan budget was spent before `limit` was filled — distinct from
   *  "nothing older exists", which is `next_before === null`. */
  truncated: boolean;
}

export interface AdminLogTail {
  records: AdminLogRecord[];
  cursor: string;
  /** The source restarted under us (the live file rotated), so the client
   *  should clear rather than append. */
  reset: boolean;
}

export interface AdminLogFilters {
  level?: string;
  q?: string;
  logger?: string;
  user_id?: string;
  task_id?: number;
}

function adminLogParams(filters: AdminLogFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.level) params.set('level', filters.level);
  if (filters.q) params.set('q', filters.q);
  if (filters.logger) params.set('logger', filters.logger);
  if (filters.user_id) params.set('user_id', filters.user_id);
  if (filters.task_id !== undefined) params.set('task_id', String(filters.task_id));
  return params;
}

export async function getAdminLogSources(): Promise<{ sources: AdminLogSource[] }> {
  return apiFetch<{ sources: AdminLogSource[] }>('/admin/logs/sources');
}

export async function getAdminLogPage(
  sourceId: string,
  opts: AdminLogFilters & { limit?: number; before?: string } = {},
): Promise<AdminLogPage> {
  const params = adminLogParams(opts);
  if (opts.limit) params.set('limit', String(opts.limit));
  if (opts.before) params.set('before', opts.before);
  const qs = params.toString();
  return apiFetch<AdminLogPage>(`/admin/logs/${encodeURIComponent(sourceId)}${qs ? `?${qs}` : ''}`);
}

/** URL for the live-tail SSE stream. Built here so the query-param spelling
 *  lives beside the page fetcher's and the two cannot drift. */
export function adminLogStreamUrl(
  sourceId: string,
  cursor: string,
  filters: AdminLogFilters = {},
): string {
  const params = adminLogParams(filters);
  params.set('cursor', cursor);
  return `${base}/api/admin/logs/${encodeURIComponent(sourceId)}/stream?${params.toString()}`;
}

export interface AdminConfigField {
  /** Dotted path — `web.oauth2_client_secret`. The address a future editor
   *  would PUT to, which is why the payload is field-level not a TOML dump. */
  key: string;
  name: string;
  value: unknown;
  type: string;
  /** A credential: `value` is always null. `set` says whether it is configured. */
  secret: boolean;
  set: boolean;
}

export interface AdminConfigSection {
  key: string;
  label: string;
  fields: AdminConfigField[];
}

export interface AdminConfigView {
  config_path: string | null;
  editable: boolean;
  sections: AdminConfigSection[];
}

export async function getAdminConfig(): Promise<AdminConfigView> {
  return apiFetch<AdminConfigView>('/admin/config');
}

/** One runtime self-check. Same shape as `istota doctor --json` emits. */
export interface DoctorCheck {
  /** Stable dotted id, e.g. `developer.forge_binaries.gh`. */
  name: string;
  status: 'ok' | 'warn' | 'fail' | 'skip';
  /** What was observed. Redacted server-side; never carries a credential. */
  detail: string;
  /** What to do about it. Always present on `warn` and `fail`. */
  remedy: string;
  /** `image` = answerable from the built artifact; `deployment` = needs this install. */
  scope: 'image' | 'deployment';
}

export interface DoctorReport {
  /** Worst status present: `fail` > `warn` > `ok`. Skips don't count. */
  status: 'ok' | 'warn' | 'fail';
  summary: { ok: number; warn: number; fail: number; skip: number };
  deep: boolean;
  checks: DoctorCheck[];
}

/**
 * The runtime self-check.
 *
 * `deep` opts into the checks that spawn a sandbox namespace. Only one deep run
 * happens at a time — a second concurrent request gets a 409 rather than
 * queueing behind a subprocess.
 */
export async function getAdminDoctor(deep = false): Promise<DoctorReport> {
  return apiFetch<DoctorReport>(`/admin/doctor${deep ? '?deep=1' : ''}`);
}

export interface FeedCategory {
  id: number;
  title: string;
}

export interface Feed {
  id: number;
  title: string;
  site_url: string;
  category: FeedCategory;
}

export interface FeedEntry {
  id: number;
  title: string;
  url: string;
  content: string;
  images: string[];
  /** Images the server hid because a newer entry already showed them. */
  duplicate_image_count: number;
  /**
   * Canonical watch page for playable media (an Are.na Embed block); empty
   * otherwise. The provider's own iframe is deliberately not stored, so the
   * reader rebuilds a player from this via `$lib/feeds/embed`.
   */
  embed_url: string;
  /**
   * A downloadable document the entry is about (an Are.na Attachment, in
   * practice a PDF); empty otherwise. Distinct from `embed_url`: this one is
   * opened rather than played, and its presence is what stops the reader
   * treating a PDF's cover page as an ordinary gallery image.
   */
  file_url: string;
  /**
   * A media file played inline with a native `<video>` / `<audio>` — a
   * Mastodon video attachment, a podcast enclosure; empty otherwise. Neither
   * of the two above: `embed_url` is a provider watch page we rebuild a
   * player for, `file_url` is opened elsewhere. Such a URL used to arrive in
   * `images` and render as an `<img>` that never decodes (ISSUE-356).
   */
  media_url: string;
  /** MIME type for `media_url` (e.g. `video/mp4`); empty otherwise. */
  media_type: string;
  feed: Feed;
  status: string;
  starred: boolean;
  starred_at: string;
  published_at: string;
  created_at: string;
}

export interface FeedsResponse {
  feeds: Feed[];
  entries: FeedEntry[];
  total: number;
}

/**
 * How long `/me` may run before it counts as a gap.
 *
 * Bounded, unlike most reads here, because the root layout gates the whole app
 * on this one and then asks the connectivity store which kind of failure it
 * was (ISSUE-354). Unbounded, a stalled request outlives the store's own 5s
 * probe: the probe finds the connection back and sets the store online, the
 * stalled `/me` rejects minutes later, and the layout reads "online" for a
 * request that plainly was not — which is the error page again, on a device
 * that is working. The bound is what keeps the answer close to the failure.
 *
 * Generous rather than probe-sized: this is the request the app is waiting on,
 * not one we will repeat in five seconds, and failing a slow-but-working
 * connection would cost more than it saves.
 */
export const ME_TIMEOUT_MS = 20_000;

export async function getMe(timeoutMs = ME_TIMEOUT_MS): Promise<User> {
  return apiFetch<User>('/me', undefined, timeoutMs);
}

export async function getFeeds(params?: Record<string, string>): Promise<FeedsResponse> {
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  return apiFetch<FeedsResponse>(`/feeds${qs}`);
}

export async function updateEntryStatus(id: number, status: string): Promise<void> {
  await apiFetch(`/feeds/entries/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });
}

export async function updateEntriesStatus(ids: number[], status: string): Promise<void> {
  await apiFetch('/feeds/entries/batch', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entry_ids: ids, status }),
  });
}

export async function updateEntryStarred(id: number, starred: boolean): Promise<void> {
  await apiFetch(`/feeds/entries/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  });
}

export type MarkAsReadScope = 'all' | 'feed' | 'category';

export async function markAsRead(
  scope: MarkAsReadScope,
  opts?: { id?: number; before_id?: number },
): Promise<{ status: string; updated: number }> {
  const body: Record<string, unknown> = { scope };
  if (opts?.id != null) body.id = opts.id;
  if (opts?.before_id != null) body.before_id = opts.before_id;
  return apiFetch('/feeds/mark-as-read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// Feeds settings types

export interface FeedsConfigCategory {
  slug: string;
  title?: string;
}

export interface FeedsConfigFeed {
  url: string;
  title?: string;
  category?: string;
  poll_interval_minutes?: number;
}

export interface FeedsConfigSettings {
  default_poll_interval_minutes?: number;
  /** Look-back window for hiding repeated images; 0 disables. */
  image_dedupe_window_days?: number;
  /** Days a read entry is kept after it was added; 0 disables age pruning. */
  entry_retention_days?: number;
  /** Maximum stored entries per feed, except stars; 0 disables the cap. */
  max_entries_per_feed?: number;
}

export interface FeedsConfigPayload {
  settings: FeedsConfigSettings;
  categories: FeedsConfigCategory[];
  feeds: FeedsConfigFeed[];
}

export interface FeedsDiagnostics {
  total_feeds: number;
  total_entries: number;
  unread_entries: number;
  error_feeds: number;
  last_poll_at: string | null;
}

export interface FeedsFeedState {
  url: string;
  last_fetched_at: string | null;
  last_error: string | null;
  error_count: number;
}

export interface FeedsConfigResponse {
  config: FeedsConfigPayload;
  diagnostics: FeedsDiagnostics;
  feed_state: FeedsFeedState[];
}

export interface FeedsImportResult {
  status: string;
  feeds_added: number;
  feeds_updated: number;
  categories_added: number;
  rewritten_bridger_urls: number;
}

export async function getFeedsConfig(): Promise<FeedsConfigResponse> {
  return apiFetch<FeedsConfigResponse>('/feeds/config');
}

export async function putFeedsConfig(config: FeedsConfigPayload): Promise<{
  status: string;
  sync: { categories_added: number; feeds_added: number; feeds_updated: number };
}> {
  return apiFetch('/feeds/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config }),
  });
}

export async function importOpml(file: File): Promise<FeedsImportResult> {
  const fd = new FormData();
  fd.append('file', file);
  const resp = await fetch(`${base}/api/feeds/import-opml`, {
    method: 'POST',
    credentials: 'same-origin',
    body: fd,
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let msg = `Import failed: ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.error) msg = body.error;
    } catch {
      // ignore
    }
    throw new Error(msg);
  }
  return resp.json();
}

export function exportOpmlUrl(): string {
  return `${base}/api/feeds/export-opml`;
}

export async function refreshFeeds(): Promise<{ status: string; feeds_queued: number }> {
  return apiFetch('/feeds/refresh', { method: 'POST' });
}

// Location types

export interface LocationPing {
  timestamp: string;
  lat: number;
  lon: number;
  /**
   * Metres as the device reported them; what they are measured against varies
   * by source and is not recorded. Null on three counts: a horizontal fix with
   * no vertical one, a fix the device flagged as vertically invalid, and a
   * point the client declared rather than measured (its wifi-zone feature).
   * The last of those covers whole intervals rather than scattered samples, so
   * do not reason about coverage from a per-ping rate.
   */
  altitude: number | null;
  accuracy: number;
  place: string | null;
  speed: number | null;
  battery: number | null;
  activity_type: string | null;
}

export interface CurrentLocation {
  last_ping: LocationPing | null;
  current_visit: {
    place_name: string;
    entered_at: string;
    duration_minutes: number | null;
    ping_count: number;
  } | null;
}

export interface DaySummaryStop {
  location: string;
  location_source: string | null;
  arrived: string;
  departed: string;
  ping_count: number;
  lat: number;
  lon: number;
}

export interface DaySummary {
  date: string;
  timezone: string;
  ping_count: number;
  transit_pings: number;
  stops: DaySummaryStop[];
}

export interface PingsResponse {
  pings: LocationPing[];
  count: number;
}

export interface Place {
  id: number;
  name: string;
  lat: number;
  lon: number;
  radius_meters: number;
  category: string;
  notes?: string | null;
}

export interface PlacesResponse {
  places: Place[];
}

export interface PlaceStats {
  place_id: number;
  total_visits: number;
  first_visit: string | null;
  last_visit: string | null;
  avg_duration_min: number | null;
  total_duration_min: number | null;
  longest_visit_min: number | null;
}

export interface DiscoveredCluster {
  lat: number;
  lon: number;
  total_pings: number;
  first_seen: string;
  last_seen: string;
  radius_meters?: number;
}

export interface DiscoverResponse {
  clusters: DiscoveredCluster[];
}

export interface DismissedCluster {
  id: number;
  lat: number;
  lon: number;
  radius_meters: number;
  dismissed_at: string;
}

export interface DismissedClustersResponse {
  dismissed: DismissedCluster[];
}

// Location API

function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}

function withBrowserTz(params: Record<string, string>): URLSearchParams {
  const qs = new URLSearchParams(params);
  const tz = browserTz();
  if (tz && !qs.has('tz')) qs.set('tz', tz);
  return qs;
}

export async function getLocationCurrent(): Promise<CurrentLocation> {
  return apiFetch<CurrentLocation>('/location/current');
}

export async function getLocationPings(params: Record<string, string>): Promise<PingsResponse> {
  const qs = withBrowserTz(params).toString();
  return apiFetch<PingsResponse>(`/location/pings?${qs}`);
}

export async function getDaySummary(date?: string): Promise<DaySummary> {
  const params: Record<string, string> = {};
  if (date) params.date = date;
  const qs = withBrowserTz(params).toString();
  return apiFetch<DaySummary>(`/location/day-summary${qs ? '?' + qs : ''}`);
}

export async function getLocationPlaces(): Promise<PlacesResponse> {
  return apiFetch<PlacesResponse>('/location/places');
}

export async function createPlace(data: {
  name: string;
  lat: number;
  lon: number;
  radius_meters?: number;
  category?: string;
  notes?: string | null;
}): Promise<Place> {
  return apiFetch<Place>('/location/places', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function updatePlace(
  id: number,
  data: Partial<Pick<Place, 'name' | 'lat' | 'lon' | 'radius_meters' | 'category' | 'notes'>>,
): Promise<Place> {
  return apiFetch<Place>(`/location/places/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deletePlace(id: number): Promise<void> {
  await apiFetch(`/location/places/${id}`, { method: 'DELETE' });
}

export async function getPlaceStats(placeId: number): Promise<PlaceStats> {
  return apiFetch<PlaceStats>(`/location/places/${placeId}/stats`);
}

export async function discoverPlaces(minPings?: number): Promise<DiscoverResponse> {
  const qs = minPings ? `?min_pings=${minPings}` : '';
  return apiFetch<DiscoverResponse>(`/location/discover-places${qs}`);
}

export async function listDismissedClusters(): Promise<DismissedClustersResponse> {
  return apiFetch<DismissedClustersResponse>('/location/dismissed-clusters');
}

export async function dismissCluster(data: {
  lat: number;
  lon: number;
  radius_meters: number;
}): Promise<DismissedCluster> {
  return apiFetch<DismissedCluster>('/location/dismissed-clusters', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function restoreDismissedCluster(id: number): Promise<void> {
  await apiFetch(`/location/dismissed-clusters/${id}`, { method: 'DELETE' });
}

// ---- Settings (Phase 5) ----

export interface SettingsField {
  key: string;
  label: string;
  type: 'text' | 'email' | 'password' | 'url';
}

export interface ServiceCard {
  service: string;
  label: string;
  status: 'configured' | 'partial' | 'missing' | 'unavailable';
  fields: SettingsField[];
  configured_keys: string[];
  last_updated: string | null;
  used_by?: string[];
  // Optional prose from the service schema: where to get the credential, or
  // what setting it changes. Absent for most services.
  hint?: string;
  oauth?: boolean; // auth is an OAuth redirect rather than writable fields
  // Render a bespoke card (garmin, google_workspace) instead of fields.
  custom_ui?: boolean;
  connected?: boolean; // google_workspace OAuth state
  enabled?: boolean; // google_workspace module flag
}

export interface ServicesResponse {
  services: ServiceCard[];
}

export async function getSettingsServices(): Promise<ServicesResponse> {
  return apiFetch<ServicesResponse>('/settings/services');
}

// --- Google Workspace (ISSUE-240) ---
//
// The instance scope list is a ceiling, not a request: it names what the
// operator's Google Cloud project has enabled. `offered` therefore names every
// service, with `max_level: 'off'` for the ones this instance does not offer —
// which the card has to render differently from "you did not grant it".

export type GoogleScopeLevel = 'off' | 'readonly' | 'full';

export interface GoogleOfferedService {
  service: string;
  label: string;
  max_level: GoogleScopeLevel;
}

export interface GoogleGrantedService {
  service: string;
  label: string;
  level: Exclude<GoogleScopeLevel, 'off'>;
  scopes: string[];
  /** False when the grant holds only part of what that level needs — Google's
   *  consent screen lets a user deselect individual boxes. */
  complete: boolean;
  /** Granted scopes of this service below the reported level. In the map, so
   *  never "unrecognised"; outside the reported level's set, so they would
   *  otherwise appear nowhere. */
  also: string[];
}

export interface GoogleStatus {
  enabled: boolean;
  connected: boolean;
  offered: GoogleOfferedService[];
  granted: GoogleGrantedService[];
  unrecognized_scopes: string[];
  /** Ceiling scopes with no service row. Requested unconditionally — no picker
   *  row can turn one off — so the card names them. */
  unoffered_scopes: string[];
  /** Clamped to the current ceiling, so it never names a level the picker's
   *  own option list no longer offers. The stored value is left untouched. */
  selection: Record<string, GoogleScopeLevel>;
  /** False when the user never chose — `selection` is then the whole ceiling. */
  selection_set: boolean;
  requested_scopes: string[];
  /** Requested but not granted: reconnect to apply. */
  missing_scopes: string[];
  /** Granted but no longer requested: the grant outlived a narrowing. */
  extra_scopes: string[];
}

export interface GoogleScopesSaveResponse {
  ok: boolean;
  selection: Record<string, GoogleScopeLevel>;
  requested_scopes: string[];
  reconnect_required: boolean;
}

export async function getGoogleStatus(): Promise<GoogleStatus> {
  return apiFetch<GoogleStatus>('/google/status');
}

export async function saveGoogleScopes(
  selection: Record<string, GoogleScopeLevel>,
): Promise<GoogleScopesSaveResponse> {
  return apiFetch<GoogleScopesSaveResponse>('/google/scopes', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selection }),
  });
}

export async function disconnectGoogle(): Promise<void> {
  await apiFetch('/google/disconnect', { method: 'DELETE' });
}

// --- modules + per-module services ---

export interface ModulesResponse {
  modules: string[];
  disabled: string[];
  enabled_for_user: Record<string, boolean>;
}

export async function getModules(): Promise<ModulesResponse> {
  return apiFetch<ModulesResponse>('/settings/modules');
}

export interface ModuleServicesResponse {
  module: string;
  module_enabled: boolean;
  services: ServiceCard[];
}

export async function getModuleServices(module: string): Promise<ModuleServicesResponse> {
  return apiFetch<ModuleServicesResponse>(`/settings/module-services/${module}`);
}

export interface LocationSettingsInfo {
  webhook_url: string;
  module_enabled: boolean;
  place_detection: {
    accuracy_threshold_m: number;
    visit_exit_minutes: number;
  };
}

export async function getLocationSettingsInfo(): Promise<LocationSettingsInfo> {
  return apiFetch<LocationSettingsInfo>('/location/settings-info');
}

export interface GeneratedIngestToken {
  ok: boolean;
  /** Returned once and never again — the secret is write-only from here on. */
  token: string;
  webhook_url: string;
}

/**
 * Mint a location ingest token, rotating any existing one.
 *
 * The only call in this file whose response carries a secret. The token has to
 * reach a phone, and the alternative is the user transcribing 43 random
 * characters; the page renders it as a QR and never asks for it again.
 */
export async function generateIngestToken(): Promise<GeneratedIngestToken> {
  // Not apiFetch: this endpoint's refusal is a 409 whose *message* is the
  // whole point ("the location module is off for this user, so an ingest
  // token would not be accepted"). apiFetch discards the body and throws
  // "API error: 409", which tells the user nothing they can act on.
  const resp = await fetch(`${base}/api/settings/secrets/overland/ingest_token/generate`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    const detail = await resp
      .json()
      .then((body: { detail?: unknown }) => (typeof body?.detail === 'string' ? body.detail : ''))
      .catch(() => '');
    throw new Error(detail || `Could not generate a token (HTTP ${resp.status}).`);
  }
  return resp.json();
}

// The basemap spec embeds the calling user's stored CARTO key in the tile URL,
// so writing that key invalidates the cached spec. Without this, pasting a key
// on /location/settings and navigating to /location is a client-side
// navigation: the module stays loaded, the stale spec is served, and the map
// does not change until a full reload — which is not what the settings card
// says will happen, and gives the user no reason to suspect a reload.
function invalidateSecretDerivedCaches(service: string): void {
  if (service === 'carto') resetBasemapCache();
}

export async function setSecret(
  service: string,
  key: string,
  value: string,
): Promise<{ ok: boolean; configured: boolean }> {
  const result = await apiFetch<{ ok: boolean; configured: boolean }>(
    `/settings/secrets/${service}/${key}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    },
  );
  invalidateSecretDerivedCaches(service);
  return result;
}

export async function deleteSecret(
  service: string,
  key: string,
): Promise<{ ok: boolean; deleted: boolean }> {
  const result = await apiFetch<{ ok: boolean; deleted: boolean }>(
    `/settings/secrets/${service}/${key}`,
    { method: 'DELETE' },
  );
  invalidateSecretDerivedCaches(service);
  return result;
}

/**
 * Derive Monarch session cookies from email+password and store them.
 *
 * The plaintext credentials never persist on the server — they're used
 * once to call api.monarch.com/auth/login/, the resulting session_id +
 * csrftoken get written to the encrypted secrets table, and the password
 * is dropped at the end of the request. The MFA code (if any) is the
 * *current* 6-digit TOTP, not the secret.
 */
/** What a login attempt can come back as.
 *
 * `challenge` is deliberately not an error: Monarch accepted the password and
 * wants a one-time code, which is a step in the flow rather than a failure.
 * Throwing for it is what produced the old "login failed" message on a
 * perfectly good password.
 */
export type MonarchLoginResult =
  | { status: 'ok' }
  | { status: 'challenge'; kind: 'email_otp' | 'mfa'; message: string }
  | {
      status: 'error';
      kind: 'auth' | 'captcha' | 'cloudflare' | 'blocked' | 'other';
      message: string;
    };

/** Read `detail` out of a FastAPI error body, structured or plain. */
function monarchDetail(body: unknown): { code: string; message: string } {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === 'object') {
    const d = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof d.code === 'string' ? d.code : '',
      message: typeof d.message === 'string' ? d.message : '',
    };
  }
  return { code: '', message: typeof detail === 'string' ? detail : '' };
}

export async function monarchLogin(
  email: string,
  password: string,
  codes: { mfaTotp?: string; emailOtp?: string } = {},
): Promise<MonarchLoginResult> {
  // Not apiFetch: this endpoint's whole contract is in the response *body*
  // (which apiFetch discards) and it answers a wrong Monarch password with
  // 401 (which apiFetch turns into an AuthError, bouncing the user to the
  // istota login page as though their own session had expired).
  const resp = await fetch(`${base}/api/money/monarch/login`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email,
      password,
      mfa_totp: codes.mfaTotp ?? '',
      email_otp: codes.emailOtp ?? '',
    }),
  });

  if (resp.ok) return { status: 'ok' };

  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    // A proxy error page or an empty body — fall through to the status-based
    // wording rather than surfacing a JSON parse failure to the user.
  }
  const { code, message } = monarchDetail(body);

  if (resp.status === 412) {
    return {
      status: 'challenge',
      kind: code === 'mfa_required' ? 'mfa' : 'email_otp',
      message,
    };
  }
  if (resp.status === 401) {
    return {
      status: 'error',
      kind: 'auth',
      message: message || 'Monarch rejected that email and password.',
    };
  }
  if (resp.status === 503) {
    const lower = message.toLowerCase();
    const kind = lower.includes('captcha')
      ? 'captcha'
      : lower.includes('cloudflare')
        ? 'cloudflare'
        : 'blocked';
    return { status: 'error', kind, message };
  }
  return {
    status: 'error',
    kind: 'other',
    message: message || `Login failed (HTTP ${resp.status}).`,
  };
}

// --- Phase 6: profile + resources ---

export interface UserProfile {
  user_id: string;
  display_name: string;
  timezone: string;
  email_addresses: string[];
  trusted_email_senders: string[];
  quiet_email_senders: string[];
  log_channel: string;
  alerts_channel: string;
  disabled_skills: string[];
  disabled_modules: string[];
  max_foreground_workers: number;
  max_background_workers: number;
  default_destination: string;
  routing: Record<string, string>;
  briefing_email_html: boolean;
  // Opt-in: follow the GPS timezone on travel (ISSUE-096). Off by default —
  // it overwrites the timezone the user chose above.
  timezone_follow_location: boolean;
  // How much of a turn that arrived from outside the room (today, mirrored
  // email) the transcript shows. The row is always there whatever this says —
  // `hidden` withholds the body, never the turn.
  external_turn_display: ExternalTurnDisplay;
  // Read-only hint from the server: surfaces available for delivery routing.
  delivery_surfaces?: string[];
}

/**
 * How much of an external-origin turn the chat transcript shows.
 *
 * The type lives here with the payload shapes that carry it; the normalizer and
 * the value list live in `$lib/stores/externalTurns`, because a function on this
 * module is mocked away by every store test that replaces `$lib/api`.
 */
export type ExternalTurnDisplay = 'full' | 'collapsed' | 'hidden';

export async function getProfile(): Promise<{ profile: UserProfile | null }> {
  return apiFetch<{ profile: UserProfile | null }>('/settings/profile');
}

export async function updateProfile(
  patch: Partial<UserProfile>,
): Promise<{ ok: boolean; fields: string[] }> {
  return apiFetch('/settings/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

// --- Phase 7b: briefings ---

export interface UserBriefingRow {
  managed: 'config' | 'db';
  id?: number;
  name: string;
  cron: string;
  // Display title for the rendered briefing (email subject, archive entry).
  // Blank means "derive from the name"; the run date is appended on render.
  title: string;
  conversation_token: string;
  // A delivery surface (talk / email / ntfy) or a comma/surface:channel
  // descriptor; the dropdown is driven by the server's `outputs` list.
  output: string;
  enabled: boolean;
}

export interface BriefingRoomOption {
  token: string;
  name: string;
}

export async function getBriefings(): Promise<{
  briefings: UserBriefingRow[];
  rooms: BriefingRoomOption[];
  outputs: string[];
}> {
  return apiFetch('/settings/briefings');
}

export async function upsertBriefing(payload: {
  name: string;
  cron: string;
  title?: string;
  conversation_token?: string;
  output?: string;
  enabled?: boolean;
}): Promise<{ ok: boolean; id: number; state: 'created' | 'updated' | 'noop' }> {
  return apiFetch('/settings/briefings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefing(id: number): Promise<{ ok: boolean; deleted: boolean }> {
  return apiFetch(`/settings/briefings/${id}`, { method: 'DELETE' });
}

// --- Health (experimental module) ---

export interface HealthStat {
  id: number;
  measured_at: string;
  metric: string;
  value: number;
  unit: string;
  source: string;
  source_ref?: number | null;
  notes: string | null;
}

export interface HealthPanel {
  id: number;
  drawn_at: string;
  lab_name: string | null;
  panel_type: string | null;
  biomarker_count: number;
  flagged_count: number;
  draft: boolean;
  notes: string | null;
  has_source: boolean;
  encounter_id: number | null;
}

export interface Biomarker {
  id: number;
  panel_id: number;
  name: string;
  display_name: string | null;
  value: number;
  unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  flag: string | null;
}

export interface BiomarkerTrendPoint {
  drawn_at: string;
  value: number;
  unit: string;
  flag: string | null;
}

export interface BiomarkerTrend {
  name: string;
  display_name: string;
  points: BiomarkerTrendPoint[];
  unit_mismatch: boolean;
  ref_range_low: number | null;
  ref_range_high: number | null;
  unit: string | null;
}

export interface BiomarkerSummaryEntry {
  name: string;
  latest: { drawn_at: string; value: number; unit: string; flag: string | null };
  previous: { drawn_at: string; value: number; unit: string; flag: string | null } | null;
  direction: 'up' | 'down' | 'flat';
  sample_count: number;
}

export interface BiomarkerRef {
  name: string;
  display_name: string;
  category: string;
  default_unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  ref_range_low_m: number | null;
  ref_range_high_m: number | null;
  ref_range_low_f: number | null;
  ref_range_high_f: number | null;
  aliases: string[];
  description: string | null;
}

export interface DisplayUnits {
  weight: 'kg' | 'lb';
  height: 'cm' | 'ft_in';
  temp: 'C' | 'F';
}

export interface HealthSettings {
  dob: string | null;
  height_cm: number | null;
  sex: 'M' | 'F' | null;
  display_units: DisplayUnits;
}

export interface HealthDashboard {
  latest_stats: Record<string, HealthStat>;
  bmi: number | null;
  recent_panels: HealthPanel[];
  alerts: (Biomarker & { panel_id: number; drawn_at: string; lab_name: string | null })[];
  settings: HealthSettings;
  active_diagnoses_count?: number;
  recent_encounters?: Encounter[];
}

export interface Encounter {
  id: number;
  encounter_date: string;
  encounter_type: string;
  provider: string | null;
  facility: string | null;
  specialty: string | null;
  reason: string | null;
  notes: string | null;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface Diagnosis {
  id: number;
  name: string;
  icd10: string | null;
  status: 'active' | 'resolved' | 'chronic';
  date_diagnosed: string | null;
  date_resolved: string | null;
  /**
   * Deprecated. A condition is seen at several encounters — GP, specialist,
   * follow-up — so `encounter_ids` is the real answer. The server still emits
   * this legacy single id, but nothing here should read it.
   */
  encounter_id: number | null;
  /** Every encounter this condition has been seen at, newest first. */
  encounter_ids: number[];
  severity: 'mild' | 'moderate' | 'severe' | null;
  notes: string | null;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface HistorySummary {
  active_diagnoses: Diagnosis[];
  chronic_diagnoses: Diagnosis[];
  recent_encounters: Encounter[];
  recent_procedures: Encounter[];
}

async function healthFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${base}/api/health${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let body: { error?: string } = {};
    try {
      body = await resp.json();
    } catch {
      // ignore
    }
    throw new Error(body.error || `Health API error: ${resp.status}`);
  }
  return resp.json();
}

export async function listHealthStats(
  params: { metric?: string; since?: string; until?: string; limit?: number } = {},
): Promise<{ stats: HealthStat[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/stats${suffix}`);
}

export async function createHealthStat(body: {
  metric: string;
  value: number;
  unit: string;
  measured_at?: string;
  notes?: string;
}): Promise<{ status: string; id: number }> {
  return healthFetch('/stats', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteHealthStat(id: number): Promise<{ status: string }> {
  return healthFetch(`/stats/${id}`, { method: 'DELETE' });
}

export async function healthStatsLatest(): Promise<{ stats: Record<string, HealthStat> }> {
  return healthFetch('/stats/latest');
}

export async function healthStatsSeries(
  metric: string,
  params: { since?: string; until?: string } = {},
): Promise<{ metric: string; points: { measured_at: string; value: number; unit: string }[] }> {
  const q = new URLSearchParams({ metric });
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  return healthFetch(`/stats/series?${q.toString()}`);
}

export async function listHealthPanels(
  params: { since?: string; until?: string; include_drafts?: number; limit?: number } = {},
): Promise<{ panels: HealthPanel[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/panels${suffix}`);
}

export async function getHealthPanel(id: number): Promise<{
  panel: HealthPanel;
  biomarkers: Biomarker[];
  source: { available: boolean; mime: string | null };
}> {
  return healthFetch(`/panels/${id}`);
}

export async function createHealthPanel(body: {
  drawn_at: string;
  lab_name?: string;
  panel_type?: string;
  notes?: string;
  encounter_id?: number | null;
}): Promise<{
  status: string;
  id: number;
  collision?: { existing_id: number; drawn_at: string; lab_name: string | null };
}> {
  return healthFetch('/panels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function updateHealthPanel(
  id: number,
  body: Partial<{
    drawn_at: string;
    lab_name: string;
    panel_type: string;
    notes: string;
    draft: boolean;
    encounter_id: number | null;
  }>,
): Promise<{ status: string }> {
  return healthFetch(`/panels/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteHealthPanel(id: number): Promise<{ status: string }> {
  return healthFetch(`/panels/${id}`, { method: 'DELETE' });
}

export async function saveHealthBiomarkers(
  panelId: number,
  biomarkers: Partial<Biomarker>[],
  confirm: boolean,
): Promise<{ status: string; count: number }> {
  return healthFetch(`/panels/${panelId}/biomarkers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ biomarkers, confirm }),
  });
}

export async function uploadHealthPanel(
  file: File,
  drawn_at: string,
  lab_name?: string,
  panel_type?: string,
): Promise<{
  status: string;
  id: number;
  collision?: { existing_id: number; drawn_at: string; lab_name: string | null };
}> {
  const form = new FormData();
  form.append('file', file);
  form.append('drawn_at', drawn_at);
  if (lab_name) form.append('lab_name', lab_name);
  if (panel_type) form.append('panel_type', panel_type);
  return healthFetch('/panels/upload', { method: 'POST', body: form });
}

export async function extractHealthPanel(panelId: number): Promise<{
  biomarkers: Partial<Biomarker>[];
  drawn_at: string | null;
  lab_name: string | null;
  panel_type: string | null;
  warnings: string[];
  raw_text: string;
}> {
  return healthFetch(`/panels/${panelId}/extract`, { method: 'POST' });
}

export function healthPanelSourceUrl(panelId: number): string {
  return `${base}/api/health/panels/${panelId}/source`;
}

export interface CsvImportSummary {
  status: string;
  panels_created: number;
  panels_skipped_identical: number;
  panels_needs_review: number;
  biomarkers_created: number;
  rows_processed: number;
  warnings: string[];
}

export async function importHealthCsv(file: File): Promise<CsvImportSummary> {
  const form = new FormData();
  form.append('file', file);
  return healthFetch('/csv/import', { method: 'POST', body: form });
}

export function healthCsvExportUrl(): string {
  return `${base}/api/health/csv/export`;
}

export async function healthBiomarkerTrend(
  name: string,
  params: { since?: string; until?: string } = {},
): Promise<BiomarkerTrend> {
  const q = new URLSearchParams({ name });
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  return healthFetch(`/biomarkers/trend?${q.toString()}`);
}

export async function healthBiomarkerSummary(): Promise<{ summary: BiomarkerSummaryEntry[] }> {
  return healthFetch('/biomarkers/summary');
}

export async function healthBiomarkerRefs(): Promise<{ refs: BiomarkerRef[] }> {
  return healthFetch('/biomarkers/refs');
}

export interface BloodworkMatrixMarker {
  name: string;
  display_name: string;
  unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  category: string;
}

export interface BloodworkMatrixCategory {
  name: string;
  markers: BloodworkMatrixMarker[];
}

export interface BloodworkMatrixPanel {
  id: number;
  drawn_at: string;
  lab_name: string | null;
  panel_type: string | null;
}

export interface BloodworkMatrix {
  categories: BloodworkMatrixCategory[];
  panels: BloodworkMatrixPanel[];
  values: Record<string, Record<string, { value: number; unit: string; flag: string | null }>>;
}

export async function getBloodworkMatrix(): Promise<BloodworkMatrix> {
  return healthFetch('/bloodwork/matrix');
}

export interface BiomarkerExplainer {
  name: string;
  display_name: string;
  direction: 'high' | 'low';
  summary: string;
  causes: string[];
  mitigations: string[];
  disclaimer: string;
  source: 'cache' | 'generated' | 'fallback';
  generated_at: string | null;
}

export async function getBiomarkerExplainer(
  name: string,
  direction: 'high' | 'low',
): Promise<BiomarkerExplainer> {
  const q = new URLSearchParams({ direction });
  return healthFetch(`/biomarkers/${encodeURIComponent(name)}/explainer?${q.toString()}`);
}

export async function getHealthSettings(): Promise<{ settings: HealthSettings }> {
  return healthFetch('/settings');
}

export async function putHealthSettings(
  body: Partial<HealthSettings>,
): Promise<{ status: string; settings: HealthSettings }> {
  return healthFetch('/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getHealthDashboard(): Promise<HealthDashboard> {
  return healthFetch('/dashboard');
}

// ---- Encounters / diagnoses / history ------------------------------------

export async function listEncounters(
  params: { since?: string; until?: string; type?: string; limit?: number; offset?: number } = {},
): Promise<{ encounters: Encounter[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/encounters${suffix}`);
}

export async function createEncounter(
  body: Partial<Encounter> & { encounter_date: string; encounter_type: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/encounters', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getEncounter(id: number): Promise<{
  encounter: Encounter;
  diagnoses: Diagnosis[];
  panels: HealthPanel[];
  documents: HealthDocument[];
}> {
  return healthFetch(`/encounters/${id}`);
}

export async function updateEncounter(
  id: number,
  body: Partial<Encounter>,
): Promise<{ status: string }> {
  return healthFetch(`/encounters/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteEncounter(id: number): Promise<{ status: string }> {
  return healthFetch(`/encounters/${id}`, { method: 'DELETE' });
}

export interface ParsedDiagnosis {
  name: string;
  icd10: string | null;
  status: 'active' | 'resolved' | 'chronic';
  severity: 'mild' | 'moderate' | 'severe' | null;
}

export interface ParsedEncounter {
  encounter_date: string | null;
  encounter_type: string;
  provider: string | null;
  facility: string | null;
  specialty: string | null;
  reason: string | null;
  notes: string | null;
  diagnoses: ParsedDiagnosis[];
  confidence: 'high' | 'medium' | 'low' | 'manual';
}

export async function extractEncounters(file: File): Promise<{
  rows: ParsedEncounter[];
  mode: 'text' | 'vision';
  warnings: string[];
  /** The kept upload. Null when storing it failed — see `warnings`. */
  document_id: number | null;
}> {
  const form = new FormData();
  form.append('file', file);
  return healthFetch('/encounters/extract', { method: 'POST', body: form });
}

export async function bulkInsertEncounters(
  rows: ParsedEncounter[],
  documentId?: number | null,
): Promise<{
  status: string;
  ids: number[];
  count: number;
  diagnosis_ids: number[];
  document_id: number | null;
}> {
  return healthFetch('/encounters/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, document_id: documentId ?? null }),
  });
}

export async function listDiagnoses(
  params: { status?: string; limit?: number; offset?: number } = {},
): Promise<{ diagnoses: Diagnosis[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/diagnoses${suffix}`);
}

export async function createDiagnosis(
  body: Partial<Diagnosis> & { name: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/diagnoses', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function linkDiagnosisEncounter(
  diagnosisId: number,
  encounterId: number,
): Promise<{ status: string; created: boolean }> {
  return healthFetch(`/diagnoses/${diagnosisId}/encounters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ encounter_id: encounterId }),
  });
}

export async function unlinkDiagnosisEncounter(
  diagnosisId: number,
  encounterId: number,
): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${diagnosisId}/encounters/${encounterId}`, {
    method: 'DELETE',
  });
}

export async function getDiagnosis(
  id: number,
): Promise<{ diagnosis: Diagnosis; encounters: Encounter[]; documents: HealthDocument[] }> {
  return healthFetch(`/diagnoses/${id}`);
}

export async function updateDiagnosis(
  id: number,
  body: Partial<Diagnosis>,
): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteDiagnosis(id: number): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${id}`, { method: 'DELETE' });
}

export async function getHistorySummary(): Promise<HistorySummary> {
  return healthFetch('/history/summary');
}

// ---- Immunizations -------------------------------------------------------

export interface Immunization {
  id: number;
  name: string;
  product_name: string | null;
  date_given: string;
  manufacturer: string | null;
  dose_label: string | null;
  lot_number: string | null;
  route: string | null;
  site: string | null;
  administered_by: string | null;
  facility: string | null;
  encounter_id: number | null;
  cvx_code: string | null;
  notes: string | null;
  source: string;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface ImmunizationRef {
  name: string;
  display_name: string;
  category: 'routine' | 'booster' | 'risk_based' | 'travel';
  schedule: string;
  interval_days: number | null;
  primary_series_doses: number | null;
  aliases: string[];
  description: string | null;
  typical_age_range: string | null;
}

export type ImmunizationStatus =
  | 'up_to_date'
  | 'due_soon'
  | 'overdue'
  | 'series_incomplete'
  | 'never_recorded'
  | 'expired'
  | 'risk_based'
  | 'recorded'; // "Other" bucket

export interface CoverageEntry {
  name: string;
  display_name: string;
  category: string;
  status: ImmunizationStatus;
  last_given: string | null;
  dose_count: number;
  next_due: string | null;
  is_overdue: boolean;
  days_until_due: number | null;
}

export interface ParsedImmunization {
  name: string;
  product_name: string | null;
  date_given: string | null;
  source_line: string;
  confidence: 'high' | 'medium' | 'low' | 'manual';
  notes: string | null;
}

export interface ImmunizationExplainer {
  name: string;
  display_name: string;
  status: ImmunizationStatus;
  summary: string;
  why_it_matters: string[];
  disclaimer: string;
  source: 'static' | 'fallback';
  generated_at: string | null;
}

export async function listImmunizations(
  params: { name?: string; since?: string; until?: string; limit?: number; offset?: number } = {},
): Promise<{ immunizations: Immunization[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/immunizations${suffix}`);
}

export async function createImmunization(
  body: Partial<Immunization> & { name: string; date_given: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/immunizations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getImmunization(id: number): Promise<{
  immunization: Immunization;
  encounter: Encounter | null;
  documents: HealthDocument[];
}> {
  return healthFetch(`/immunizations/${id}`);
}

export async function updateImmunization(
  id: number,
  body: Partial<Immunization>,
): Promise<{ status: string }> {
  return healthFetch(`/immunizations/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteImmunization(id: number): Promise<{ status: string }> {
  return healthFetch(`/immunizations/${id}`, { method: 'DELETE' });
}

export async function listImmunizationRefs(): Promise<{ refs: ImmunizationRef[] }> {
  return healthFetch('/immunizations/refs');
}

export async function getImmunizationCoverage(): Promise<{
  coverage: CoverageEntry[];
  other: CoverageEntry[];
}> {
  return healthFetch('/immunizations/coverage');
}

export async function parseImmunizations(text: string): Promise<{ rows: ParsedImmunization[] }> {
  return healthFetch('/immunizations/parse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
}

export async function extractImmunizations(file: File): Promise<{
  rows: ParsedImmunization[];
  mode: 'text' | 'vision';
  warnings: string[];
  /** The kept upload. Null when storing it failed — see `warnings`. */
  document_id: number | null;
}> {
  const form = new FormData();
  form.append('file', file);
  return healthFetch('/immunizations/extract', { method: 'POST', body: form });
}

export async function bulkInsertImmunizations(
  rows: ParsedImmunization[],
  documentId?: number | null,
): Promise<{ status: string; ids: number[]; count: number; document_id: number | null }> {
  return healthFetch('/immunizations/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, document_id: documentId ?? null }),
  });
}

export async function getImmunizationExplainer(name: string): Promise<ImmunizationExplainer> {
  return healthFetch(`/immunizations/${encodeURIComponent(name)}/explainer`);
}

// ---- Documents -----------------------------------------------------------

export type DocumentEntity = 'encounter' | 'diagnosis' | 'immunization';

export interface HealthDocument {
  id: number;
  filename: string;
  original_filename: string | null;
  mime: string;
  byte_size: number;
  source: 'manual' | 'import' | 'agent';
  notes: string | null;
  created_at: string;
  /** Auth-gated stream route. Never a filesystem path. */
  url: string;
  /**
   * What this document is attached to. Present on every `documents: [...]`
   * listing, absent on the `POST /documents` upload acknowledgement — that
   * one reports what the create did (`created`, `linked`) rather than
   * describing the row, and every caller re-lists afterwards anyway.
   */
  links?: DocumentLink[];
}

export interface DocumentLink {
  entity_type: DocumentEntity;
  entity_id: number;
  label: string;
}

export interface EntityRef {
  type: DocumentEntity;
  id: number;
}

export async function listDocuments(
  entity?: EntityRef,
  page?: { limit?: number; offset?: number },
): Promise<{ documents: HealthDocument[] }> {
  const q = new URLSearchParams();
  if (entity) {
    q.set('entity_type', entity.type);
    q.set('entity_id', String(entity.id));
  }
  // Only meaningful on the unfiltered branch — the entity-scoped one returns
  // every document on that record and takes no bound. The server defaults to
  // 200 and caps at 1000, so a caller wanting all of them has to page.
  if (page?.limit !== undefined) q.set('limit', String(page.limit));
  if (page?.offset !== undefined) q.set('offset', String(page.offset));
  const qs = q.toString();
  return healthFetch(`/documents${qs ? `?${qs}` : ''}`);
}

export async function getDocument(
  id: number,
): Promise<{ document: HealthDocument; links: DocumentLink[] }> {
  return healthFetch(`/documents/${id}`);
}

export async function uploadDocument(
  file: File,
  entity?: EntityRef,
  notes?: string,
): Promise<HealthDocument & { status: string; created: boolean; linked: boolean }> {
  const form = new FormData();
  form.append('file', file);
  if (entity) {
    form.append('entity_type', entity.type);
    form.append('entity_id', String(entity.id));
  }
  if (notes) form.append('notes', notes);
  return healthFetch('/documents', { method: 'POST', body: form });
}

export async function linkDocument(
  id: number,
  entity: EntityRef,
): Promise<{ status: string; created: boolean }> {
  return healthFetch(`/documents/${id}/links`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_type: entity.type, entity_id: entity.id }),
  });
}

export async function unlinkDocument(
  id: number,
  entity: EntityRef,
): Promise<{ status: string; removed: boolean }> {
  return healthFetch(`/documents/${id}/links/${entity.type}/${entity.id}`, {
    method: 'DELETE',
  });
}

export async function deleteDocument(id: number): Promise<{ status: string }> {
  return healthFetch(`/documents/${id}`, { method: 'DELETE' });
}

// ---- Garmin --------------------------------------------------------------

export interface GarminStatus {
  connected: boolean;
  email: string | null;
  last_sync: string | null;
  error: string | null;
}

export interface GarminConnectResponse {
  status: 'ok' | 'mfa_required' | 'error';
  prompt?: string;
  error?: string;
}

export interface GarminSyncResponse {
  inserted: number;
  skipped: number;
  errored: number;
  days_processed: number;
  errors: string[];
  auth_error: boolean;
}

// Garmin auth is a cross-module connected service: its routes live at
// /api/garmin/*, not under /api/health. (Daily-summary sync stays health-
// owned — see syncGarmin below.)
async function garminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${base}/api/garmin${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let body: { error?: string } = {};
    try {
      body = await resp.json();
    } catch {
      // ignore
    }
    throw new Error(body.error || `Garmin API error: ${resp.status}`);
  }
  return resp.json();
}

export async function getGarminStatus(): Promise<GarminStatus> {
  return garminFetch('/status');
}

export async function connectGarmin(
  email: string,
  password: string,
): Promise<GarminConnectResponse> {
  return garminFetch('/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
}

export async function submitGarminMfa(code: string): Promise<GarminConnectResponse> {
  return garminFetch('/mfa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

export async function disconnectGarmin(): Promise<{ status: string }> {
  return garminFetch('/disconnect', { method: 'POST' });
}

export interface GarminImportResponse {
  dry_run: boolean;
  inserted: number;
  activities: number;
  details: Array<{
    activity_id: number;
    type: string;
    start: string;
    distance_m: number | null;
    fetched: number;
    inserted: number;
    shadowed: number;
  }>;
}

export async function importGarminTracks(days_back = 7): Promise<GarminImportResponse> {
  return garminFetch('/import-tracks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days_back }),
  });
}

export async function syncGarmin(days_back = 7): Promise<GarminSyncResponse> {
  return healthFetch('/garmin/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days_back }),
  });
}

// ---- Web chat ----

export interface ChatRoom {
  id: number;
  token: string;
  name: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  /** Surface the room was created on. Talk-origin rooms surface here
   * automatically once the bot is messaged in them. */
  origin?: 'web' | 'talk';
  /** The bound Talk conversation, or null when the room is web-only. Sent on
   * every room the listing returns, not only on a fresh promote response — the
   * room-list refresh writes this key unconditionally, so a listing that
   * omitted it erased the promote's own answer on the next poll. */
  talk_token?: string | null;
  /** Unread bot/system messages on the web surface (server-computed; excludes
   * the user's own turns). Absent on older backends → treat as 0. */
  unread_count?: number;
  /** Sidebar tint: a `ROOM_COLORS` name, or null for none. Per-*user*, unlike
   * the model/effort/brain trio below — it lives on this user's room handle,
   * so the two members of a shared Talk room can tint it differently. */
  color?: string | null;
  /** Standing per-room model default (canonical model id), shared across Talk
   * and web. null / absent → inherit the instance default. */
  model?: string | null;
  /** Standing per-room effort level (low/medium/high/xhigh/max). */
  effort?: string | null;
  /** Standing per-room brain kind, shared across Talk and web. null / absent →
   * inherit the deployment's own routing. Writing it is admin-only, and the
   * kinds on offer are an operator allowlist (`selectable_brains` below), so a
   * client that has none renders no control rather than an empty dropdown. */
  brain?: string | null;
  /** ISO-UTC stamp of the room's newest message, falling back to the room's
   * creation time when nobody has spoken in it. The sidebar's sort key — it is
   * normalized the same way a message row's `created_at` is, so an arriving
   * row's stamp can be written straight onto the room. Absent on older
   * backends → the room keeps whatever position the server gave it. */
  last_activity?: string;
}

export interface ChatConfig {
  max_prompt_chars: number;
  max_attachment_mb: number;
  attachment_extensions: string[];
  client_poll_interval_ms: number;
  /**
   * The authenticated caller's username, which the send queue's `localStorage`
   * key is built from — a shared Talk room has one token across every member,
   * so a bare token would hand one person's queued message to another on a
   * browser profile two people take turns using. Published here rather than
   * read from `getMe()` because the chat store awaits this config before
   * anything else, and `getMe()` resolves after the restore needs it. Absent
   * on an older backend, in which case the queue is in memory only.
   */
  user_id?: string;
  /** How an external-origin turn's body renders. */
  external_turn_display?: ExternalTurnDisplay;
  /** The caller's own raw setting, where `''` means "follow the operator". */
  outbound_approval?: string;
  /**
   * False when the stored value is not one the server recognizes — a
   * hand-edited row. The server resolves such a value to the floor, so a pane
   * must show it as unrecognized rather than as a live selection.
   */
  outbound_approval_valid?: boolean;
  /** What `outbound_approval` resolves to once the operator floor applies. */
  outbound_approval_effective?: string;
  /** The floor the user may tighten past but not loosen below. */
  outbound_approval_floor?: string;
}

/**
 * The message a turn replies to, resolved server-side.
 *
 * Two shapes in one type, discriminated by `deleted`: a live parent carries its
 * role and a display excerpt, a hard-deleted one carries only the id it named.
 * The dead form is kept rather than dropped — the citation records that the
 * turn had a referent, and erasing it would rewrite the conversation.
 */
export interface ReplyCitation {
  msg_id: number;
  role?: 'user' | 'assistant' | 'system';
  excerpt?: string;
  deleted: boolean;
}

export interface ChatHistoryMessage {
  role: 'user' | 'assistant' | 'system';
  text: string;
  // Task-backed turns carry task_id; bot-delivered notifications carry notif_id.
  task_id?: number;
  notif_id?: number;
  status?: string;
  confirmation?: boolean;
  created_at: string;
  // Finished task-backed turns carry their tool-use descriptions (in order)
  // and wall-clock duration so the action strip + timing persist across
  // reloads (ISSUE-122).
  tools?: string[];
  // Ordered, interleaved segment list (`text` / `tool`) for a finished turn,
  // derived server-side from the execution trace, so history reconstructs the
  // same interleaved layout as the live stream. `tools` is kept as a fallback.
  segments?: { kind: 'text' | 'tool'; text: string }[];
  duration_seconds?: number | null;
  // The model that produced this answer (canonical ID), null when unknown.
  model?: string | null;
  // Durable (messages-store-backed) rows carry the message's stable id and the
  // requesting user's star flag; aux (tasks-only) turns carry neither and are
  // not starrable.
  msg_id?: number;
  starred?: boolean;
  // Aggregate-view rows (GET /chat/messages) additionally carry their room.
  room_token?: string;
  room_name?: string;
  // Display names of files a user turn carried, so the attachment chips
  // survive a transcript rebuild (the composer's in-memory names don't).
  attachments?: string[];
  // Positional against `attachments`: the workspace path to open each chip at,
  // or null for one this user can't be served (see `chatFileUrl`).
  attachment_paths?: (string | null)[];
  // Present only on a turn that cites a parent.
  reply_to?: ReplyCitation;
  // Who wrote a user row, when it was not the reader — today, the sender of an
  // email mirrored into the room it continues. Absent means the viewer, which
  // is what every user row was assumed to be.
  author?: string;
  // The writer's istota user id, which is what the avatar endpoint keys on —
  // `author` is a display label and two people may share one. Set only where
  // the writer is an istota user who is not the reader, so an external
  // sender's turn carries a name and no id, and nothing here is requested for
  // it. The picture's content hash deliberately does not ride along: this row
  // is on the byte-budgeted room-event stream, and the client pays one
  // conditional request per author per session instead (D13).
  author_id?: string;
  // The surface a user row entered from, when it is not one the room itself
  // lives on — today `'email'` alone. Absent is the signal for "from inside
  // this conversation", so a co-member's Talk or web turn carries no key.
  origin?: string;
  // The email's subject line, lifted out of the wrapper the display body
  // strips. What a collapsed external turn shows in place of the body.
  subject?: string;
}

/** Cross-room aggregate views (sidebar All / Unread / Starred). */
export type ChatView = 'all' | 'unread' | 'starred';

export interface ChatHistory {
  messages: ChatHistoryMessage[];
  // Oldest in-flight task (back-compat). Prefer active_tasks.
  active_task: { id: number; status: string } | null;
  // All in-flight tasks for the room, oldest-first. The room runs them one at
  // a time; the client resumes the first and queues the rest in this order.
  active_tasks?: { id: number; status: string }[];
  // Older history exists below this page (ISSUE-131). Absent on a pre-paging
  // backend, so the client treats `undefined` as "no more".
  has_more?: boolean;
  // Pass back as before_ts/before_id to fetch the next older page. `ts` is the
  // RAW stored created_at (`YYYY-MM-DD HH:MM:SS`), never the display value —
  // the keyset breaks if it's round-tripped through a normalized timestamp.
  oldest_cursor?: { ts: string; id: number } | null;
}

/**
 * Why a send didn't land.
 *
 * Kept separable rather than flattened into one message string because the
 * distinction outlives the sentence: an offline outbox (ISSUE-202) holds an
 * `unreachable` send for later and fails a `rejected` one outright, and `auth`
 * is the one failure a retry can never resolve.
 */
export type SendFailure =
  | 'unreachable'
  | 'timeout'
  | 'auth'
  | 'rate_limit'
  | 'rejected'
  // The cited parent is gone or was never in this room. Its own member because
  // the recovery is unlike every other failure's: Retry cannot work (it would
  // re-POST the same dead id), so the text goes back to the composer instead.
  | 'reply_target_gone';

/** Trailing options on a send. New fields belong here, not as positionals. */
export interface SendOptions {
  /** Canonical `messages.id` this message replies to. */
  replyToMsgId?: number;
}

export interface SendResult {
  ok: boolean;
  status: number;
  // Set only when `ok` is false.
  failure?: SendFailure;
  retry_after?: number;
  task_id?: number | null;
  inline_result?: string;
  // Structured payload from an inline !command (e.g. !search result cards);
  // null/absent for plain-text commands. Rendered as a dedicated component.
  command_data?: Record<string, unknown> | null;
  stream_url?: string;
  error?: string;
}

export interface TaskEventDTO {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

/**
 * The server's chat limits.
 *
 * `timeoutMs` is for the connectivity probe (`stores/connectivity.ts`), which
 * uses this call as its "is the server there" question and cannot afford to
 * wait out a stall — a probe with no bound would leave the app believing it is
 * offline for as long as the OS takes to give up on the socket.
 */
export function getChatConfig(timeoutMs = 0): Promise<ChatConfig> {
  return apiFetch<ChatConfig>('/chat/config', undefined, timeoutMs);
}

/**
 * `getChatConfig`, shared across callers.
 *
 * The limits in here are the server's, and they are what the composer checks a
 * file against before spending an upload on it. Several places want them and
 * none of them can change them, so one request per page load is enough.
 *
 * A failure is not cached: the config is best-effort, and a client that gave up
 * permanently on one bad response would keep enforcing nothing (or, worse, a
 * stale guess) for the life of the page.
 */
let chatConfigInFlight: Promise<ChatConfig> | null = null;

export function chatConfigOnce(): Promise<ChatConfig> {
  chatConfigInFlight ??= getChatConfig().catch((e) => {
    chatConfigInFlight = null;
    throw e;
  });
  return chatConfigInFlight;
}

/** Test seam: drop the cached config so the next call fetches again. */
export function resetChatConfigCache(): void {
  chatConfigInFlight = null;
}

export function getBasemap(): Promise<BasemapSpec> {
  return apiFetch<BasemapSpec>('/map/basemap');
}

/**
 * `getBasemap`, shared across callers and across maps.
 *
 * Every map surface asks the same question and none of them can change the
 * answer, so one request per page load covers the location page, the history
 * page and anything that mounts a second map beside them.
 *
 * A failure is not cached, for the same reason `chatConfigOnce` does not cache
 * one: the caller falls back to the keyless default and a later navigation
 * should get another chance at the real answer.
 */
let basemapInFlight: Promise<BasemapSpec> | null = null;

export function basemapOnce(): Promise<BasemapSpec> {
  basemapInFlight ??= getBasemap().catch((e) => {
    basemapInFlight = null;
    throw e;
  });
  return basemapInFlight;
}

/** Test seam: drop the cached basemap so the next call fetches again. */
export function resetBasemapCache(): void {
  basemapInFlight = null;
}

export interface ChatCommand {
  name: string;
  help: string;
}

export interface ChatModelAlias {
  alias: string;
  target: string | null;
  effort: string | null;
}

/** A hidden command alias: `dispatch` resolves `alias` to `target` before it
 *  looks `target` up. Deliberately absent from `commands`, which is what feeds
 *  `!help` and the composer autocomplete. Optional so a client built against
 *  an older server still type-checks. */
export interface ChatCommandAlias {
  alias: string;
  target: string;
}

/** A brain kind, as the room settings modal shows it. `model_namespace` is
 *  what the modal compares to decide whether a pending change will clear the
 *  room's model pin. The server publishes all three brain fields to admins
 *  only, so an empty `selectable_brains` means "no control" whether that is
 *  because the operator listed no kinds or because this user may not write
 *  one. Optional so a client built against an older server still type-checks. */
export interface SelectableBrain {
  kind: string;
  label: string;
  model_namespace: string;
}

export interface ChatCommands {
  commands: ChatCommand[];
  command_aliases?: ChatCommandAlias[];
  model_aliases: ChatModelAlias[];
  /** What a room may be pinned to. */
  selectable_brains?: SelectableBrain[];
  /** Every *known* kind's model namespace, not only the offered ones — the
   *  brain a change moves *away from* need not be on the menu. It is the
   *  inherited one when the room pins nothing, and it can be a kind the
   *  operator has since dropped from the allowlist.
   *
   *  Known rather than buildable (ISSUE-417): a namespace is a property of the
   *  kind, not of the server's ability to construct the brain, so a kind whose
   *  construction fails still has one here — and the server's own clearing rule
   *  answers the same way, which is what keeps the two agreeing. */
  brain_namespaces?: Record<string, string>;
  /** What a room with no pin of its own runs, on the web surface. Null only for
   *  a non-admin, or where the deployment's kind is not a known one. */
  inherited_brain?: SelectableBrain | null;
}

/** Command, model-alias and brain catalogue powering the composer autocomplete
 *  and the room settings modal.
 *
 *  `roomId` scopes `model_aliases` to that room's own brain — a room pinned to
 *  another model namespace must not be offered names it cannot run. Omit it for
 *  the deployment default, which is what the composer's `!model` completion
 *  wants and what every caller got before rooms could pin a brain. */
export function fetchChatCommands(roomId?: number, brain?: string): Promise<ChatCommands> {
  const params = new URLSearchParams();
  if (roomId !== undefined) params.set('room_id', String(roomId));
  // The kind the caller is *considering*, which `room_id` cannot express: the
  // room holds its old brain until the save lands, so the settings modal asks
  // for the pending selection's models (ISSUE-417). Ignored server-side for a
  // non-admin or a kind the operator has not listed.
  if (brain) params.set('brain', brain);
  const q = params.toString();
  return apiFetch<ChatCommands>(`/chat/commands${q ? `?${q}` : ''}`);
}

export function getChatRooms(timeoutMs = 0): Promise<{ rooms: ChatRoom[] }> {
  return apiFetch<{ rooms: ChatRoom[] }>('/chat/rooms', undefined, timeoutMs);
}

export function createChatRoom(name: string): Promise<ChatRoom> {
  return apiFetch<ChatRoom>('/chat/rooms', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

export interface RoomPatch {
  name?: string;
  archived?: boolean;
  model?: string | null;
  effort?: string | null;
  brain?: string | null;
  /** A `ROOM_COLORS` name, or null to clear. Absent leaves it untouched. */
  color?: string | null;
}

/** The PATCH response is the room, plus one field that is not room state:
 *  `cleared` names the standing defaults this request dropped because the
 *  brain change crossed a model namespace — `["model"]`, or `["model",
 *  "effort"]`, since the two are cleared as the pair they were set as. Present
 *  only when something was dropped, and the store takes it off before merging
 *  the rest into its record. */
export type UpdatedChatRoom = ChatRoom & { cleared?: string[] };

export function updateChatRoom(id: number, patch: RoomPatch): Promise<UpdatedChatRoom> {
  return apiFetch<UpdatedChatRoom>(`/chat/rooms/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** What a promote did. Not all of these are errors, which is why the endpoint
 * answers with a status rather than a bare room: `live` and `unreachable` both
 * left the room untouched, and rendering them as a failure is what made a room
 * with a dead binding look like a button that does nothing (ISSUE-401). */
export type PromoteStatus =
  /** Promoted a room that was not bound to Talk before. */
  | 'ok'
  /** The bound conversation was gone; the room now points at a new one. */
  | 'reconnected'
  /** Already bound to a conversation that still exists. Nothing changed. */
  | 'live'
  /** The conversation is there, but the bot was removed from it. Adding the bot
   * back is the repair — replacing the binding would fork a live room. */
  | 'bot_removed'
  /** Nextcloud could not be asked about the existing binding. Nothing changed. */
  | 'unreachable'
  /** Another request bound the room first. Its conversation stands. */
  | 'raced';

export interface PromoteResult {
  status: PromoteStatus;
  /** The updated room, or null where nothing usable came back. */
  room: ChatRoom | null;
}

/** Create a real Nextcloud Talk conversation for a web-origin room and bind it
 * ("Also open in Talk"), or repair a binding whose conversation was deleted. */
export function promoteChatRoom(id: number): Promise<PromoteResult> {
  return apiFetch<PromoteResult>(`/chat/rooms/${id}/promote`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/** A room can't be deleted while a task is still running in it (HTTP 409). */
export class ChatRoomBusyError extends Error {
  constructor() {
    super('room has a task in progress');
    this.name = 'ChatRoomBusyError';
  }
}

export async function deleteChatRoom(id: number): Promise<{ status: string }> {
  const resp = await fetch(`${base}/api/chat/rooms/${id}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (resp.status === 409) throw new ChatRoomBusyError();
  // 404 → already gone; idempotent from the caller's view.
  if (resp.status === 404) return { status: 'gone' };
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

/** A room's `CHANNEL.md` — the standing instructions every task in the room
 * is given. `revision` is opaque and must be handed back on save. */
export interface ChatRoomMemory {
  room_id: number;
  token: string;
  content: string;
  /** False when the file is absent or whitespace-only; both are the empty state. */
  exists: boolean;
  /** A Talk-origin room shares one file across all its members. */
  shared: boolean;
  /** Server-supplied starting text for the empty state. */
  template: string;
  revision: string;
}

export function getRoomMemory(id: number): Promise<ChatRoomMemory> {
  return apiFetch<ChatRoomMemory>(`/chat/rooms/${id}/memory`);
}

/** The file moved under the editor — an agent write landed between load and
 * save. Reload before overwriting. */
export class ChatMemoryConflictError extends Error {
  constructor() {
    super('channel memory changed since it was loaded');
    this.name = 'ChatMemoryConflictError';
  }
}

/** Save refused because a task is in flight in the room, or because another
 * writer held the file lock. Both clear on their own; retry is the answer. */
export class ChatMemoryBusyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ChatMemoryBusyError';
  }
}

export async function saveRoomMemory(
  id: number,
  content: string,
  revision: string,
): Promise<{ status: string; revision: string }> {
  const resp = await fetch(`${base}/api/chat/rooms/${id}/memory`, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, revision }),
  });
  if (resp.status === 409) {
    const body = await resp.json().catch(() => ({}));
    if (body.code === 'conflict') throw new ChatMemoryConflictError();
    throw new ChatMemoryBusyError(body.error || 'room is busy');
  }
  if (resp.status === 413) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(`Too long — the limit is ${Math.floor((body.max_bytes ?? 0) / 1024)} KB.`);
  }
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

export function getRoomMessages(
  id: number,
  opts: { limit?: number; before?: { ts: string; id: number } | null; timeoutMs?: number } = {},
): Promise<ChatHistory> {
  const limit = opts.limit ?? 50;
  const params = new URLSearchParams({ limit: String(limit) });
  // Both cursor params travel together (the backend rejects a half-cursor) and
  // `before.ts` is the raw stored created_at, passed back verbatim.
  if (opts.before) {
    params.set('before_ts', opts.before.ts);
    params.set('before_id', String(opts.before.id));
  }
  return apiFetch<ChatHistory>(
    `/chat/rooms/${id}/messages?${params.toString()}`,
    undefined,
    opts.timeoutMs ?? 0,
  );
}

/** One page of the cross-room message stream for an aggregate view. Same
 * message shape as the per-room endpoint plus room_token / room_name; same
 * keyset-cursor contract (`before.ts` is the raw stored created_at). */
export function getChatMessagesView(
  view: ChatView,
  opts: { limit?: number; before?: { ts: string; id: number } | null } = {},
): Promise<ChatHistory> {
  const params = new URLSearchParams({ view, limit: String(opts.limit ?? 50) });
  if (opts.before) {
    params.set('before_ts', opts.before.ts);
    params.set('before_id', String(opts.before.id));
  }
  return apiFetch<ChatHistory>(`/chat/messages?${params.toString()}`);
}

/** Star / unstar a durable message for the current user. */
export function setChatMessageStarred(
  msgId: number,
  starred: boolean,
): Promise<{ ok: boolean; starred: boolean }> {
  return apiFetch<{ ok: boolean; starred: boolean }>(`/chat/messages/${msgId}/star`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  });
}

/** A message can't be deleted while its turn is still running (HTTP 409) —
 * the scheduler writes the assistant row at completion, so the delete would
 * silently undo itself. Distinguished from a generic failure because "try
 * again once it finishes" is actionable and "couldn't delete" isn't. */
export class ChatMessageBusyError extends Error {
  constructor() {
    super('message belongs to a task in progress');
    this.name = 'ChatMessageBusyError';
  }
}

/** Hard-delete one transcript row. Gone from every read path at once, and —
 * in a Talk-bound room — best-effort from the mirrored Talk message too. */
export async function deleteChatMessage(msgId: number): Promise<{ status: string }> {
  const resp = await fetch(`${base}/api/chat/messages/${msgId}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (resp.status === 409) throw new ChatMessageBusyError();
  // 404 → already gone (another tab, a repeat click); idempotent from here.
  if (resp.status === 404) return { status: 'gone' };
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

/** Advance every room's web read cursor at once (the header mark-all chip). */
export function markAllRoomsRead(): Promise<{ ok: boolean; updated: number }> {
  return apiFetch<{ ok: boolean; updated: number }>('/chat/rooms/read-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Mark a room read on the web surface — clears its sidebar unread badge by
 * advancing the per-user read cursor to the room's newest message. */
export function markRoomRead(id: number): Promise<{ ok: boolean; last_read_message_id: number }> {
  return apiFetch<{ ok: boolean; last_read_message_id: number }>(`/chat/rooms/${id}/read`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * How long a send may stay open before we call it dead.
 *
 * `fetch` has no timeout of its own, and this caller's state machine blocks on
 * the result — without a bound, a stalled connection (a mobile handover, a
 * proxy holding the socket) parks the composer in "sending" for as long as the
 * OS takes to give up, which can be minutes or never.
 *
 * Generously above the slowest legitimate send rather than tight: the endpoint
 * does real work before it answers (`record_inbound`, plus a Talk mirror that
 * is itself bounded at ~5s), so a short bound would fail sends that were about
 * to succeed.
 */
export const SEND_TIMEOUT_MS = 30_000;

export async function sendChatMessage(
  roomId: number,
  text: string,
  attachments: string[] = [],
  // Display labels, positional against `attachments`. The stored filename
  // carries a random collision suffix, so the name the user picked is only
  // knowable here — the server persists these for the transcript's chips.
  attachmentNames: string[] = [],
  timeoutMs = SEND_TIMEOUT_MS,
  // Client-minted identity for this message, carried by every attempt at it.
  // The server answers a repeat with the task the first attempt created, which
  // is what makes a retry of a send it silently accepted produce one turn
  // rather than two. Optional: omitted, the endpoint behaves as it always did.
  idempotencyKey?: string,
  // Anything added from here on goes in the options object rather than as a
  // further positional — six is already more than a call site can read.
  options: SendOptions = {},
): Promise<SendResult> {
  const controller = new AbortController();
  let timedOut = false;
  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;

  // Serialized outside the try so the catch below can honestly claim every
  // rejection it sees is the network — otherwise a body that can't be
  // stringified would be reported to the user as an unreachable server.
  const body = JSON.stringify({
    text,
    attachments,
    attachment_names: attachmentNames,
    ...(idempotencyKey ? { client_msg_id: idempotencyKey } : {}),
    // Only the id: the server reads the parent's text from the row it already
    // holds, so nothing here can dictate what the model is told it said.
    ...(options.replyToMsgId ? { reply_to_msg_id: options.replyToMsgId } : {}),
  });

  try {
    const resp = await fetch(`${base}/api/chat/rooms/${roomId}/messages`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: controller.signal,
    });

    // Something answered, whatever it went on to say (ISSUE-202). Reported
    // before the status branches below, since a 401 and a 429 are as much
    // evidence of a reachable server as a 200 is.
    noteTransport(true);

    // Also returned rather than thrown as `AuthError`: only `getMe` in the root
    // layout catches that, so from here it took the silent path below.
    if (resp.status === 401) return { ok: false, status: 401, failure: 'auth' };
    if (resp.status === 429) {
      // A `Retry-After` may legally be an HTTP-date, and an intermediary
      // (a CDN, an nginx limiter) is not obliged to send the seconds form —
      // so an unparseable value must not reach the transcript as "wait NaNs".
      const parsed = Number(resp.headers.get('Retry-After'));
      const retry = Number.isFinite(parsed) && parsed > 0 ? parsed : 60;
      return { ok: false, status: 429, failure: 'rate_limit', retry_after: retry };
    }
    // Inside the try, and ahead of clearTimeout, so the abort still covers the
    // body: a proxy that flushes headers and then stalls is the same hang the
    // bound exists for, and clearing on the headers alone would leave it open.
    let data: { error?: string } & Record<string, unknown> = {};
    try {
      const parsed = await resp.json();
      // Shape-checked rather than assigned. `resp.json()` resolves to `null`
      // for a body that is the literal `null` and to an array for a JSON list,
      // neither of which throws — so the inner catch never fired and reading
      // `data.error` below threw a TypeError that escaped to the outer catch,
      // where a plain 400 was reported to the user as an unreachable server.
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) data = parsed;
    } catch (e) {
      // A body that isn't JSON is an error page — nginx answers its own HTML
      // for a 502 or an over-cap upload — and the status below already
      // describes it. An *aborted* read is the stall this bound exists for, so
      // it has to reach the classifier instead of passing for an empty body:
      // swallowing it returned `{ok: true}` with no task id, which the caller
      // reads as an inline command that produced nothing.
      if (controller.signal.aborted) throw e;
    }
    if (!resp.ok) {
      return {
        ok: false,
        status: resp.status,
        // A 404 on a send that carried a citation is almost always the server
        // refusing the *citation*. The endpoint answers 404 for an unknown
        // room too, which is reachable if another client deletes the room
        // mid-send — the recovery is the right one either way (the text comes
        // back rather than being stranded on a dead row), so the cost of
        // conflating them is a wrong sentence, not lost work. Classified on
        // the intent rather than on the error string, which is prose.
        failure: resp.status === 404 && options.replyToMsgId ? 'reply_target_gone' : 'rejected',
        error: data.error || `error ${resp.status}`,
      };
    }
    // `data` spread first: the endpoint's own payload carries a `status`
    // ("pending"), which would otherwise shadow the numeric HTTP status this
    // type promises — and that status is now rendered to the user on failure.
    return { ...data, ok: true, status: resp.status };
  } catch {
    // A rejection here is the network, never the server: connection refused,
    // DNS, a dropped socket, a stalled body, or our own abort. There is no
    // status to report.
    //
    // Returned rather than thrown, unlike every other call in this file. The
    // caller renders this failure onto the message it belongs to, so it needs
    // the same shape as a rejection the server did answer — and a throw from
    // here is what used to escape an un-awaited caller and leave the composer
    // locked with nothing on screen (ISSUE-200).
    //
    // The same distinction the offline outbox turns on, so the connectivity
    // store hears it here rather than re-deriving it from the returned shape.
    const failure: SendFailure = timedOut ? 'timeout' : 'unreachable';
    noteTransport(false, failure);
    return { ok: false, status: 0, failure };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function getTaskEvents(taskId: number, sinceSeq = 0): Promise<{ events: TaskEventDTO[] }> {
  return apiFetch<{ events: TaskEventDTO[] }>(`/chat/tasks/${taskId}/events?since_seq=${sinceSeq}`);
}

/**
 * Outbound mail the approval gate is holding for the caller.
 *
 * Three shapes in one type, and the card must branch in this order:
 *
 * - `unreadable` — a stored column does not parse. Carries an id and nothing
 *   else, because the columns that would carry content are the ones that failed
 *   to read. Discard is the only action that works on it.
 * - `truncated` — the row exists but the stream frame spent its byte budget
 *   before reaching it. Enough to place a card; the body comes from
 *   `listOutboundDrafts()`.
 * - the full row.
 *
 * Every text field is model-composed or comes off a stranger's mail. Render it,
 * never inject it as markup.
 */
export interface OutboundDraft {
  id: number;
  /** Absent on an unreadable row, which is the only field it does carry. */
  status?: 'pending' | 'sending';
  room_token?: string | null;
  task_id?: number | null;
  subject?: string;
  body?: string;
  html?: boolean;
  to?: string[];
  cc?: string[];
  bcc?: string[];
  attachments?: string[];
  hold_reason?: string;
  created_at?: string | null;
  /** What else the task that composed this draft did, as rendered strings. */
  actions_taken?: string[];
  /** Set when the stream frame carried a stub instead of the row. */
  truncated?: boolean;
  /** Set when the stored row could not be parsed. */
  unreadable?: boolean;
}

export function listOutboundDrafts(): Promise<{ drafts: OutboundDraft[] }> {
  return apiFetch<{ drafts: OutboundDraft[] }>('/chat/drafts');
}

/**
 * Why an approve, edit or discard did not happen — the card wording turns on
 * this, and the four cases call for four different sentences.
 *
 * `conflict` carries `state`, because "your mail is going out right now" and
 * "someone already discarded this" are opposite messages behind one 409.
 * `sent_unrecorded` is the one failure that must never offer a retry: the mail
 * left the building and only the bookkeeping failed.
 */
export type DraftFailure = 'gone' | 'conflict' | 'permanent' | 'transient' | 'sent_unrecorded';

export interface DraftActionResult {
  ok: boolean;
  status: number;
  failure?: DraftFailure;
  /** On `conflict`: what the row is now. */
  state?: 'pending' | 'sending' | 'sent' | 'discarded' | 'gone';
  error?: string;
  message_id?: string;
  /** The re-read row a successful PATCH returns. */
  draft?: OutboundDraft;
}

/**
 * One place the draft routes' failures become a `DraftFailure`.
 *
 * `apiFetch` throws a bare `API error: <status>` on any non-2xx, which discards
 * the body — and the body is where `state` and `retryable` live. So these three
 * verbs go through `fetch` directly, the way the send path already does for the
 * same reason.
 */
async function draftAction(
  path: string,
  init: RequestInit = { method: 'POST' },
): Promise<DraftActionResult> {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api${path}`, {
      ...init,
      credentials: 'same-origin',
    });
  } catch {
    // The request never landed, so nothing was decided. Safe to offer again.
    return { ok: false, status: 0, failure: 'transient' };
  }
  if (resp.status === 401) throw new AuthError();
  let data: Record<string, unknown> = {};
  try {
    data = await resp.json();
  } catch {
    /* an empty or non-JSON body still has to classify by status */
  }
  if (resp.ok) return { ...data, ok: true, status: resp.status };
  const error = typeof data.error === 'string' ? data.error : `error ${resp.status}`;
  if (resp.status === 404) return { ok: false, status: 404, failure: 'gone', error };
  if (resp.status === 409) {
    // A missing `state` stays undefined rather than defaulting to `gone`. The
    // caller drops the card silently for a settled state and keeps it for
    // `sending`, so a default of `gone` turns "we don't know" into the branch
    // that reports the action as having worked.
    const state = data.state;
    return {
      ok: false,
      status: 409,
      failure: 'conflict',
      state: typeof state === 'string' ? (state as DraftActionResult['state']) : undefined,
      error,
    };
  }
  if (data.sent === true) {
    return {
      ok: false,
      status: resp.status,
      failure: 'sent_unrecorded',
      message_id: typeof data.message_id === 'string' ? data.message_id : undefined,
      error,
    };
  }
  // The server's own transient/permanent split, not a guess from the status:
  // it alone knows whether the relay refused or the instance is misconfigured.
  return {
    ok: false,
    status: resp.status,
    failure: data.retryable === true ? 'transient' : 'permanent',
    error,
  };
}

export function approveOutboundDraft(draftId: number): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}/approve`);
}

export function discardOutboundDraft(draftId: number): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}/discard`);
}

export function editOutboundDraft(draftId: number, body: string): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body }),
  });
}

export function confirmChatTask(taskId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/confirm`, { method: 'POST' });
}

export function cancelChatTask(taskId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/cancel`, { method: 'POST' });
}

export function chatStreamUrl(taskId: number): string {
  return `${base}/api/chat/tasks/${taskId}/stream`;
}

/** A row from the live room-event stream: the same shape the history endpoints
 * emit, plus the room it belongs to, so `buildHistoryMessage` can construct a
 * streamed row and a reloaded row through one code path. */
export type ChatRoomEvent = ChatHistoryMessage;

export interface ChatRoomEventsPage {
  events: ChatRoomEvent[];
  /** The cursor to adopt. On `gap` this is the max id the server *scanned*,
   * not the last one it sent — adopting the latter would strand the rows the
   * server chose not to replay. */
  cursor: number;
  /** The delta was too large to replay; reload instead (rooms list + the open
   * room), then adopt `cursor`. */
  gap: boolean;
  /** Messages deleted since `since_deletion_id`. A separate tail because a
   * delete is hard: there is no `messages` row left for the id-ordered event
   * tail to carry, so the ledger is what tells another open tab. */
  deletions?: ChatMessageDeletion[];
  /** Cursor to adopt for the deletion tail — its own sequence, unrelated to
   * `cursor`. */
  deletion_cursor?: number;
  /** Held outbound mail, as a whole-set snapshot rather than a tail: a draft's
   * transitions are not all insertions, so an id cursor would carry one of
   * three. Always present on a healthy read, even when empty. */
  drafts?: OutboundDraft[];
  /** The drafts read failed. Distinct from an empty set, which is why the key
   * above is always sent — a client reading absence as "none held" would clear
   * every card on one transient lock. */
  drafts_unavailable?: boolean;
}

export interface ChatMessageDeletion {
  msg_id: number;
  room_token: string;
}

/** Snapshot of the room-event tail — the polling fallback behind the room
 * stream. `limit=1` asks only for a fresh cursor (used after a reload). */
export function getRoomEvents(
  sinceId = 0,
  limit = 0,
  timeoutMs = 0,
  sinceDeletionId = 0,
): Promise<ChatRoomEventsPage> {
  const params = new URLSearchParams({ since_id: String(sinceId) });
  if (limit > 0) params.set('limit', String(limit));
  if (sinceDeletionId > 0) params.set('since_deletion_id', String(sinceDeletionId));
  return apiFetch<ChatRoomEventsPage>(`/chat/events?${params.toString()}`, undefined, timeoutMs);
}

/** SSE endpoint carrying every message visible to the user, across all rooms. */
export function chatRoomStreamUrl(sinceId: number, sinceDeletionId = 0): string {
  return `${base}/api/chat/stream?since_id=${sinceId}&since_deletion_id=${sinceDeletionId}`;
}

export interface ChatAttachment {
  // The host path the brain reads, or **null for a chip whose bytes are still
  // in this browser** — a file attached with no connection, waiting for the
  // outbox to upload it (ISSUE-202). A null path never reaches the wire: the
  // drain resolves every pending chip to a real path before it POSTs.
  path: string | null;
  name: string;
  size: number;
  // Where the same file is reachable through `chatFileUrl`, or null when it
  // isn't (a deployment with no local workspace). Distinct from `path`, which
  // is the host path the brain reads and the download endpoint won't take.
  workspace_path?: string | null;
  // Key into the offline `blobs` object store, set on a pending chip and on no
  // other. Its presence is what `path: null` means.
  pendingBlobId?: string;
  // The picked file's type, carried only on a pending chip so the queue entry
  // that records it has one. A file already uploaded has no use for it — the
  // server holds the file and the client only ever names it.
  mimeType?: string;
}

/**
 * An attachment upload that never reached the server.
 *
 * Distinguished from every other upload failure for the reason the send path
 * distinguishes `unreachable` from `rejected`: nothing has been refused, so a
 * queued message keeps its bytes and waits rather than failing (ISSUE-202).
 * Everything the server actually answered — a 413, a disallowed extension —
 * throws a plain `Error` carrying its message.
 */
export class UploadUnreachableError extends Error {
  constructor(message = 'Couldn’t upload — the server is unreachable.') {
    super(message);
    this.name = 'UploadUnreachableError';
  }
}

/**
 * Download URL for one of the caller's own workspace files.
 *
 * The whole point of routing a chip here rather than at a Nextcloud share is
 * that nothing becomes public: the endpoint serves the file inside the logged-in
 * session, so the user opens a file they already own.
 */
export function chatFileUrl(path: string): string {
  return `${base}/api/chat/files?path=${encodeURIComponent(path)}`;
}

/**
 * What the file picker offers for a profile picture.
 *
 * Mirrors `avatars.ACCEPT_ATTRIBUTE` in `src/istota/avatars.py`, and is
 * deliberately narrower than `image/*` — that matches TIFF, BMP, AVIF and SVG,
 * all of which the server refuses, and the user would find out only after
 * choosing one.
 */
export const AVATAR_ACCEPT = 'image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif';

/**
 * Where one identity's picture is served from.
 *
 * A plain cookie-authenticated URL to drop into `src`, exactly like
 * `chatFileUrl`. `version` is a content hash: with it the server answers
 * `immutable` for the caller's own picture and the bot icon, so every later
 * render in a transcript is a cache hit with no request. Without it the
 * answer is `no-cache` plus an `ETag`, which is what a third party's picture
 * gets — one conditional request per author per session.
 */
export function avatarUrl(kind: 'user' | 'bot', userId?: string, version?: string | null): string {
  // `userId` is optional for the bot branch alone, and nothing in the type
  // enforces the pairing. Without this, a missing id builds a URL with an
  // empty last segment, which matches no route at all — so the browser gets
  // FastAPI's own 404 rather than the one the endpoint sends, and the caller
  // sees an image that failed with no way to tell why.
  if (kind === 'user' && !userId) throw new Error('avatarUrl: a user avatar needs a userId');
  const path =
    kind === 'bot' ? '/api/avatars/bot' : `/api/avatars/user/${encodeURIComponent(userId!)}`;
  const query = version ? `?v=${encodeURIComponent(version)}` : '';
  return `${base}${path}${query}`;
}

export interface AvatarUpload {
  /** sha256 of the *normalized* bytes — what `?v` and the ETag carry. */
  hash: string;
  mime: string;
  bytes: number;
}

/**
 * PUT one picture as multipart, to whichever avatar endpoint owns it.
 *
 * Shaped like `uploadChatAttachment` rather than routed through `apiFetch`,
 * because every refusal here is one the user has to read: the server sends
 * `{error}` with a 413 for a file over the cap and a 415 for a format it will
 * not decode, and `apiFetch` would collapse both into `API error: 413`.
 */
async function putAvatar(path: string, file: File): Promise<AvatarUpload> {
  const form = new FormData();
  form.append('file', file);
  let resp: Response;
  try {
    resp = await fetch(`${base}${path}`, {
      method: 'PUT',
      credentials: 'same-origin',
      body: form,
    });
  } catch {
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `upload failed (${resp.status})`);
  return data as AvatarUpload;
}

/** Replace the caller's own profile picture. */
export async function uploadAvatar(file: File): Promise<AvatarUpload> {
  return putAvatar('/api/settings/avatar', file);
}

/**
 * Remove the caller's uploaded picture, revealing any imported one.
 *
 * `{deleted: false}` when there was none — idempotent, not a 404.
 */
export async function deleteAvatar(): Promise<{ deleted: boolean }> {
  return apiFetch('/settings/avatar', { method: 'DELETE' });
}

/**
 * Replace the deployment's bot icon. Admin only.
 *
 * Deployment-wide and one row, so two admins setting it at once resolve as
 * last writer wins. It is *not* the bot's Nextcloud Talk avatar and cannot
 * change it — the daemon holds an app password, and Nextcloud's avatar route
 * is session-and-CSRF-guarded — which is why the control says so.
 */
export async function uploadBotAvatar(file: File): Promise<AvatarUpload> {
  return putAvatar('/api/admin/avatar', file);
}

/**
 * Clear the bot icon, reverting to the amber initial chip.
 *
 * `{deleted: false}` when there was none — idempotent, not a 404.
 */
export async function deleteBotAvatar(): Promise<{ deleted: boolean }> {
  return apiFetch('/admin/avatar', { method: 'DELETE' });
}

const CHAT_ATTACHMENT_PATH = '/chat/attachments';

/**
 * Upload one attachment, by whichever route the file arrived on.
 *
 * A file the shell picked is still on disk and never had to come into the page
 * at all: the shell posts it straight from there with URLSession, and what
 * crosses the bridge is the server's answer. Everything else — a paste, a drop,
 * a file input, a voice memo — is already in memory here and goes out as an
 * ordinary multipart fetch.
 *
 * Both routes end at the same endpoint and read the same JSON back, including
 * the error bodies, so the caller cannot tell them apart and does not need to.
 */
export async function uploadChatAttachment(item: File | Picked): Promise<ChatAttachment> {
  if (!(item instanceof File) && item.nativePath) {
    return uploadFromShell(item);
  }
  const file = item instanceof File ? item : item.blob;
  if (!file) throw new Error('Nothing to upload.');
  const form = new FormData();
  form.append('file', file);
  let resp: Response;
  try {
    resp = await fetch(`${base}/api${CHAT_ATTACHMENT_PATH}`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    });
  } catch {
    // The one failure that says nothing about the file. Reported to the
    // connectivity store for the same reason every other transport completion
    // is: an upload is the request that discovers the gap when it is the first
    // thing the user does after losing signal.
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `upload failed (${resp.status})`);
  return data as ChatAttachment;
}

/**
 * The same upload, done by the shell from the file still on disk.
 *
 * The URL has to be absolute — the shell is building a URLSession request, not
 * resolving one against a document — so the page's own origin is what makes it
 * the same endpoint the fetch above would have reached.
 */
async function uploadFromShell(item: Picked): Promise<ChatAttachment> {
  const url = new URL(`${base}/api${CHAT_ATTACHMENT_PATH}`, location.origin).toString();
  // Asked here rather than left to `uploadFromPath` to throw, so that the
  // catch below is only ever the uploader's own rejection. A missing plugin is
  // a fact about this build, not about the network, and reporting it as a gap
  // would raise the offline banner and stop every room's queue draining over
  // something no request observed.
  if (!nativeUploadAvailable()) throw new Error('No native uploader.');
  let status: number;
  let body: string;
  try {
    ({ status, body } = await uploadFromPath(item, url));
  } catch {
    // `uploadFromPath` returns any HTTP answer as a status, so a throw here is
    // URLSession failing to get one — the same gap the fetch above reports.
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (status === 401) throw new AuthError();
  let data: { error?: string } & Partial<ChatAttachment> = {};
  try {
    data = JSON.parse(body);
  } catch {
    // A proxy's own error page rather than the app's JSON — an nginx 413 is
    // the one that actually happens. There is no message worth relaying, so
    // the status carries it.
  }
  if (status < 200 || status >= 300) {
    throw new Error(data.error || `upload failed (${status})`);
  }
  return data as ChatAttachment;
}

// ---------------------------------------------------------------------------
// Briefings module
// ---------------------------------------------------------------------------

export interface BriefingArchiveItem {
  id: number;
  briefing_name: string;
  subject: string;
  generated_at: string;
  task_id: number | null;
  delivered_to: string[];
  body_md?: string;
}

export interface BriefingArchiveResponse {
  items: BriefingArchiveItem[];
  total: number;
  briefing_names: string[];
}

export interface BriefingSource {
  id: number;
  position: number;
  kind: string;
  config: Record<string, unknown>;
  enabled: boolean;
}

export interface BriefingBlock {
  id: number;
  briefing_name: string;
  position: number;
  title: string;
  directive: string;
  render_mode: string;
  options: Record<string, unknown>;
  sources: BriefingSource[];
}

export interface BriefingConfigResponse {
  briefings: { name: string; blocks: BriefingBlock[] }[];
  schedule_names: string[];
  source_kinds: string[];
  structured_kinds: string[];
}

export interface BrowsePreset {
  key: string;
  name: string;
  url: string;
}

export interface FeedOptions {
  available: boolean;
  subscriptions: { kind: string; value: number; label: string }[];
  categories: { kind: string; value: number; label: string }[];
}

export async function getBriefingArchive(
  params?: Record<string, string>,
): Promise<BriefingArchiveResponse> {
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  return apiFetch<BriefingArchiveResponse>(`/briefings/archive${qs}`);
}

export async function getBriefingArchiveItem(id: number): Promise<BriefingArchiveItem> {
  return apiFetch<BriefingArchiveItem>(`/briefings/archive/${id}`);
}

export async function deleteBriefingArchiveItem(id: number): Promise<void> {
  await apiFetch(`/briefings/archive/${id}`, { method: 'DELETE' });
}

export async function getBriefingConfig(): Promise<BriefingConfigResponse> {
  return apiFetch<BriefingConfigResponse>('/briefings/config');
}

export async function putBriefingBlock(
  payload: Record<string, unknown>,
): Promise<{ status: string; block?: BriefingBlock }> {
  return apiFetch('/briefings/blocks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefingBlock(id: number): Promise<void> {
  await apiFetch(`/briefings/blocks/${id}`, { method: 'DELETE' });
}

export async function putBriefingSource(
  payload: Record<string, unknown>,
): Promise<{ status: string; id?: number }> {
  return apiFetch('/briefings/sources', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefingSource(id: number): Promise<void> {
  await apiFetch(`/briefings/sources/${id}`, { method: 'DELETE' });
}

export async function getBrowsePresets(): Promise<{ presets: BrowsePreset[] }> {
  return apiFetch('/briefings/browse-presets');
}

export async function getFeedOptions(): Promise<FeedOptions> {
  return apiFetch('/briefings/feed-options');
}

export async function checkBriefingPath(
  path: string,
): Promise<{ ok: boolean; resolved?: string; error?: string }> {
  return apiFetch(`/briefings/path-check?path=${encodeURIComponent(path)}`);
}

export async function getBriefingPathSuggestions(q = ''): Promise<{ paths: string[] }> {
  const query = q.trim();
  return apiFetch(
    query ? `/briefings/path-suggest?q=${encodeURIComponent(query)}` : '/briefings/path-suggest',
  );
}

// --- Shared briefing blocks (admin) + options (any user) ------------------

export interface SharedBlockStatus {
  last_run_at: string | null;
  value_updated_at: string | null;
  value_preview: string | null;
  stored_trusted: boolean | null;
  has_content: boolean;
}

export interface SharedBlock {
  name: string;
  cron: string;
  title: string;
  directive: string;
  render_mode: string;
  enabled: boolean;
  trusted: boolean;
  sources: { kind: string; config: Record<string, unknown> }[];
  created_at: string | null;
  updated_at: string | null;
  status: SharedBlockStatus;
}

export interface SharedBlocksResponse {
  shared_blocks: SharedBlock[];
  allowed_source_kinds: string[];
  render_modes: string[];
  shared_block_timezone: string;
}

export interface SharedBlockOption {
  name: string;
  updated_at: string | null;
  has_content: boolean;
  source: 'config' | 'custom';
}

export async function getSharedBlocks(): Promise<SharedBlocksResponse> {
  return apiFetch('/briefings/shared-blocks');
}

export async function putSharedBlock(
  payload: Record<string, unknown>,
): Promise<{ status: string; shared_block?: SharedBlock }> {
  return apiFetch('/briefings/shared-blocks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteSharedBlock(name: string, deleteValue = false): Promise<void> {
  const qs = deleteValue ? '?delete_value=true' : '';
  await apiFetch(`/briefings/shared-blocks/${encodeURIComponent(name)}${qs}`, { method: 'DELETE' });
}

export async function runSharedBlock(
  name: string,
): Promise<{ status: string; error: string | null; block_status: SharedBlockStatus }> {
  return apiFetch(`/briefings/shared-blocks/${encodeURIComponent(name)}/run`, { method: 'POST' });
}

export async function getSharedBlockOptions(): Promise<{ options: SharedBlockOption[] }> {
  return apiFetch('/briefings/shared-block-options');
}

// --- the notification inbox ------------------------------------------------

/** One button on a notification.
 *
 * `endpoint` is an existing producer API path (`/chat/tasks/12/confirm`),
 * apiFetch-relative — the same form every other call in this file takes. There
 * is deliberately no generic dispatcher endpoint on the server: those handlers
 * already own their authorization, and a dispatcher would be a second gate with
 * less context. `href` is an in-app route, relative to `base`. */
export interface NotificationAction {
  id: string;
  label: string;
  kind: 'primary' | 'default' | 'danger';
  method: 'POST' | 'LINK';
  endpoint: string | null;
  href: string | null;
}

/** A stored notification as the panel renders it.
 *
 * Every text field here is either bot-composed or lifted off a stranger's mail —
 * a gated email's sender and subject reach `title` and `body`. Render them as
 * text nodes; none of the five components touching this type uses `{@html}`. */
export interface ResolvedNotification {
  id: number;
  source: string;
  severity: 'info' | 'success' | 'warning' | 'danger';
  actionable: boolean;
  title: string;
  body: string;
  link: string | null;
  occurrences: number;
  created_at: string;
  updated_at: string;
  seen_at: string | null;
  object_type: string | null;
  object_id: string | null;
  actions: NotificationAction[];
  status_note: string | null;
}

export interface NotificationCounts {
  open: number;
  actionable: number;
}

export interface NotificationListing {
  notifications: ResolvedNotification[];
  /** The post-sweep count of the whole open set, not of the returned page.
   *  Both tab labels are derived from this and the rows, so a label can never
   *  claim more than the list below it shows. */
  total_open: number;
}

/** The id and the version the client actually rendered.
 *
 * The pair is the whole point: the server resolves a fire-and-forget row only
 * where the stored `updated_at` still matches, so an occurrence raised between
 * the fetch and this call is not closed by a user who never saw it. */
export interface NotificationSeen {
  id: number;
  updated_at: string;
}

/** Mirrors `notification_sources.SAFE_PATH_RE` on the server.
 *
 * The server validates every URL it emits, at runtime, on every view. This is
 * the second copy rather than a substitute for it: the *browser* is what
 * performs the fetch, with the session cookie attached, off a path the server
 * chose — so the side taking the risk checks it too. Anchored the same way, and
 * deliberately without the `m` flag: `$` would otherwise admit a trailing
 * newline, and a control character reaching a fetch target is what an allowlist
 * is for. */
const SAFE_ACTION_PATH = /^\/[A-Za-z0-9][A-Za-z0-9/_-]*$/;

/** A type predicate, so a caller that checks a nullable `href` also narrows it —
 *  otherwise every call site needs a second truthiness test the compiler can see,
 *  and the two can disagree about which one is the guard. */
export function isSafeActionPath(path: string | null | undefined): path is string {
  return typeof path === 'string' && SAFE_ACTION_PATH.test(path);
}

export function getNotificationCounts(): Promise<NotificationCounts> {
  return apiFetch<NotificationCounts>('/notifications/count');
}

export function listNotifications(
  filter: 'all' | 'action' = 'all',
  limit = 50,
): Promise<NotificationListing> {
  return apiFetch<NotificationListing>(`/notifications?filter=${filter}&limit=${limit}`);
}

export function dismissNotification(id: number): Promise<{ status: string }> {
  return apiFetch(`/notifications/${id}/dismiss`, { method: 'POST' });
}

export function markNotificationsSeen(seen: NotificationSeen[]): Promise<{ status: string }> {
  return apiFetch('/notifications/seen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ seen }),
  });
}

/** POST the path a resolver named for this action.
 *
 * Refuses anything the allowlist does not accept rather than fetching it. The
 * path is server-supplied and built by interpolating an `object_id` that is
 * opaque `TEXT` on the row, so `1/../../admin/x` is the shape being refused. */
export async function runNotificationAction(endpoint: string): Promise<unknown> {
  if (!isSafeActionPath(endpoint)) {
    throw new Error(`refusing an unsafe notification action path: ${endpoint}`);
  }
  return apiFetch(endpoint, { method: 'POST' });
}

export { AuthError };
