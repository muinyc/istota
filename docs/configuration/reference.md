# Configuration reference

Complete reference for `config/config.toml`. See `config/config.example.toml` in the repository for a commented example.

## Top-level settings

| Setting | Default | Description |
|---|---|---|
| `bot_name` | `"Istota"` | User-facing name (chat, emails, folder names) |
| `emissaries_enabled` | `true` | Include emissaries.md in system prompts |
| `model` | `""` | **Deprecated** (ISSUE-418). This was the `claude_code` brain's own default living at the top level, where it was applied to whatever brain ran and shadowed that brain's configured default — so a room pinned to `native` still got the Claude model. Still loads, and is migrated onto `[brain.claude_code]` and `[brain.tmux]` with a warning; never onto `[brain.native]`. Set the per-brain key instead. |
| `effort` | `""` | **Deprecated** (ISSUE-418), migrated the same way as `model` above. |
| `advisor_model` | `""` | Advisor model — Anthropic-namespace brains only (`claude_code` / `tmux_claude`); resolves through the same alias table as `model` but carries no effort. Must resolve to a model capable of *being* an advisor (a weak/cheap tier fails every task it runs on). Dropped for any task carrying its own model pin (`!model`, `!room model`, a `[[jobs]] model`). |
| `custom_system_prompt` | `false` | Use config/system-prompt.md instead of Claude Code default. That file — and nothing else in the config directory — is bind-mounted read-only into the sandbox, since the CLI opens it there |
| `namespace` | `"istota"` | Instance namespace — prefixes systemd units, lock paths and similar, so two instances can share a host |
| `db_path` | `"data/istota.db"` | SQLite database path |
| `module_data_dir` | derived | Root for per-user module DBs; defaults to `{db_path.parent}/modules` |
| `rclone_remote` | `"nextcloud"` | rclone remote name |
| `nextcloud_mount_path` | not set | Local mount path (enables mount mode when set) |
| `skills_dir` | `"config/skills"` | Operator skill overrides directory |
| `disabled_skills` | `[]` | Instance-wide skills to exclude |
| `temp_dir` | `"/tmp/istota"` | Temporary directory for task execution |
| `max_memory_chars` | `0` | Cap total memory in prompts (0 = unlimited) |
| `max_knowledge_facts` | `50` | Cap knowledge graph facts per prompt (0 = unlimited) |

## `[nextcloud]`

| Setting | Default | Description |
|---|---|---|
| `url` | `""` | Nextcloud server URL |
| `username` | `""` | Bot's Nextcloud username |
| `app_password` | `""` | Nextcloud app password |
| `share_default_expire_days` | `14` | Default expiry on links the bot creates |
| `dav_prefix` | `""` | Where the storage root sits in the bot's Nextcloud tree; prefixed onto every DAV/OCS path. Empty for the rclone mount, `"Shared Files"` for the Docker stack's external-storage mount |
| `auto_share_bot_dir` | `true` | Share the bot workspace back to the user over OCS at boot. False where the deployment already mounts it into the user's tree |

## `[talk]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable Talk polling |
| `bot_username` | `"istota"` | Bot's username (to filter own messages) |

## `[email]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable email |
| `imap_host` | `""` | IMAP server |
| `imap_port` | `993` | IMAP port |
| `imap_timeout_seconds` | `30` | IMAP socket timeout |
| `imap_user` | `""` | IMAP username |
| `imap_password` | `""` | IMAP password |
| `smtp_host` | `""` | SMTP server |
| `smtp_port` | `587` | SMTP port |
| `smtp_user` | `""` | SMTP username (defaults to imap_user) |
| `smtp_password` | `""` | SMTP password (defaults to imap_password) |
| `poll_folder` | `"INBOX"` | Folder to poll |
| `bot_email` | `""` | Bot's email address |
| `outbound_approval_floor` | `"untrusted"` | Minimum outbound email approval policy for every user: `off` (never hold) \| `untrusted` (hold unless every recipient is trusted) \| `all` (hold unless every recipient is one of the user's own addresses). A user may tighten past the floor but never loosen below it. An invalid value fails the config load rather than falling back — see [the outbound approval gate](../features/email.md#the-outbound-approval-gate) |
| `confirm_sender_match` | `"off"` | What an own-address claim buys: `off` (the header is proof — assumes the MTA enforces DMARC) \| `verify` (proof only when the MTA's own stamp says so; needs `authserv_id`, and the daemon refuses to load without it) \| `gate` (never proof; every self-sent message is held). Legacy `false`/`true` still load as `off`/`gate` — see [`confirm_sender_match`](../features/email.md#confirm_sender_match) |
| `dmarc_canary` | `true` | Warns when mail routed on a user's own address arrives without a `dmarc=pass` from the receiving MTA. Monitoring for the assumption above; never blocks mail — see [the DMARC canary](../features/email.md#the-dmarc-canary) |
| `dmarc_canary_warn_on_missing` | `false` | Also warn when your MTA's stamp carries no DMARC verdict at all. Off by default because a path that stamps nothing would warn on every message |
| `authserv_id` | `""` | Your receiving MTA's authserv-id — the first field of the `Authentication-Results` header it stamps. Set it and headers from any other authserv-id are discarded rather than read; blank keeps the older topmost-header-only read, which a sender can forge once the MTA stops stamping. Setting it also makes mail arriving without your stamp warn on its own — see [the DMARC canary](../features/email.md#the-dmarc-canary) |

## `[conversation]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable conversation context |
| `lookback_count` | `25` | Messages to consider |
| `skip_selection_threshold` | `3` | Include all if history <= this |
| `selection_model` | `"fast"` | Role alias for relevance matching (resolves to Haiku by default) |
| `selection_timeout` | `30.0` | Timeout for selection |
| `use_selection` | `true` | Use LLM selection |
| `always_include_recent` | `5` | Always include this many recent |
| `context_truncation` | `0` | Max chars per bot response (0 = no limit) |
| `context_recency_hours` | `0` | Exclude old messages (0 = disabled) |
| `context_min_messages` | `10` | Min messages when recency filtering |
| `previous_tasks_count` | `3` | Unfiltered tasks to inject |
| `talk_context_limit` | `100` | Messages from Talk API |

## `[logging]`

| Setting | Default | Description |
|---|---|---|
| `level` | `"INFO"` | Log level (INFO or DEBUG) |
| `output` | `"console"` | Destination: console, file, or both |
| `file` | `""` | Log file path |
| `rotate` | `true` | Enable log rotation |
| `max_size_mb` | `10` | Max log file size |
| `backup_count` | `5` | Rotated files to keep |

## `[scheduler]`

### Polling intervals

| Setting | Default | Description |
|---|---|---|
| `poll_interval` | `2` | Seconds between queue checks |
| `dispatch_interval` | `0.5` | Sub-tick cadence for `pool.dispatch()` within a poll tick — bounds cold pending-task pickup latency. 0 or ≥ `poll_interval` = legacy one-dispatch-per-tick |
| `talk_poll_interval` | `10` | Seconds between Talk polls |
| `talk_poll_timeout` | `30` | Talk long-poll timeout |
| `talk_poll_wait` | `2.0` | Max wait before processing available rooms. Also the slack the cycle's own deadline adds over `talk_poll_timeout`, since an answer to a server-side long-poll cannot arrive before that hold elapses |
| `talk_poll_full_sweep_interval` | `300` | Seconds between polls of every room regardless of the `lastMessage` gate. Between sweeps only a room the room list says has a newer message is long-polled. `0` turns the gate off |
| `email_poll_interval` | `60` | Seconds between email polls |
| `email_poll_batch_size` | `50` | Messages one email poll walks. The remainder is left for the next tick and drains in arrival order, rather than falling off the end |
| `email_rate_limit_messages` | `60` | Inbound email tasks one user's account will pay for per window. Over-budget mail is filed (`routing_method="throttled"`, left in the mailbox, one alert per window), never dropped. 0 disables |
| `email_sender_rate_limit_messages` | `20` | The same budget narrowed to one correspondent, so a single loud sender throttles alone rather than consuming the user's whole allowance. 0 disables |
| `email_rate_limit_window_seconds` | `3600` | The sliding window both counts run over, and the window the throttle alert and the collapsed confirmation prompts are deduplicated on |
| `email_task_queue` | `background` | Which worker queue inbound mail lands on. Background by default: email is the one surface an unauthenticated stranger can create work on, and the one whose turnaround nobody is watching. `foreground` restores the previous behaviour at the cost of a flood competing with live chat |
| `email_confirmation_prompts_per_window` | `3` | Untrusted-sender confirmation prompts per (user, sender) per window before they collapse into one summary notice. The held mail stays held and individually approvable with `!confirm <task-id>`; only the interruption collapses. 0 = never collapse |
| `email_max_body_chars` | `32000` | The body is interpolated whole into the prompt, so a single large message is its own amplification. Truncated with a marker past this; the full mail stays in the mailbox |
| `email_max_attachment_bytes` | `26214400` | Attachment bytes **written to disk and uploaded** per message (25 MiB). Not a bound on the IMAP transfer — the client fetches and decodes the whole message before any part can be inspected. Whole attachments only: one that would cross the budget is skipped rather than truncated, and the prompt names it. 0 = unlimited |
| `email_max_attachment_bytes_per_poll` | `104857600` | The same budget across one whole poll tick (100 MiB). A per-message cap alone bounds one message and not a batch of fifty. 0 = unlimited |
| `briefing_check_interval` | `60` | Seconds between briefing/job/cleanup checks |
| `tasks_file_poll_interval` | `30` | Seconds between TASKS.md polls |
| `shared_file_check_interval` | `120` | Seconds between shared file checks |
| `heartbeat_check_interval` | `60` | Seconds between heartbeat checks |
| `db_health_check_interval` | `86400` | Seconds between SQLite `quick_check` + self-heal `REINDEX` sweeps over framework + per-user DBs (24h) |
| `doctor_check_interval` | `3600` | Seconds between re-runs of the `istota doctor` runtime self-check (1h; 0 disables). Re-run rather than trusted from boot, because the drift it catches happens after boot — the auto-update cron changes what is installed under a config the daemon already loaded |
| `worktree_reap_interval` | `21600` | Seconds between developer-worktree reaping sweeps (ISSUE-288, 6h; 0 disables). Also gated by `[developer] worktree_reap_enabled`. Well under the 24h default retention, so nothing waits long after becoming eligible. A periodic job rather than a task setup hook: `setup_env` hooks run for every skill whatever the task selected, so a sweep there fired on every Talk reply and every heartbeat tick |
| `sandbox_cache_sweep_interval` | `21600` | Seconds between package-cache sweeps (ISSUE-317, 6h; 0 disables). It follows the caches rather than a key: with `[developer] enabled` and `repos_dir` both set it walks `{repos_dir}/{user_id}/.package-caches` for each configured user, and otherwise `{root}/{user_id}` under `[security] sandbox_cache_dir`, so it is inert only where neither is set. Also gated by `[security] sandbox_cache_sweep_enabled`. Moving the caches onto disk is what makes them persist, so this is what keeps the fix for a RAM leak from being a disk leak on the volume `worktree_reap_interval` is already fighting for |
| `avatar_import_interval` | `21600` | Seconds between Nextcloud profile-picture import ticks (6h; 0 disables). Also gated by `[web] avatar_import_from_nextcloud` and by the storage backend being Nextcloud. On a cadence rather than at login (which would put a Nextcloud timeout in front of authentication) or on render (a live proxy), so a user who has just signed in for the first time sees the initial chip until the next tick — which is why it is six hours rather than daily |
| `skill_overlay_reindex_interval` | `21600` | Seconds between memory-search reindexes of every configured user's [per-skill overlays](per-user.md#per-skill-overlays) (ISSUE-343, 6h; 0 disables). An overlay is a user-written file with no CLI write path, so there is nothing to index from on write, and the per-write reindex never covered a text-editor edit over Nextcloud anyway — which is the authoring mode the file is for. One pass walks the directory, indexes only what actually binds, and reaps rows for files that no longer do, so an edit, a create and a delete are all covered. Deliberately a scheduler tick rather than part of the sleep cycle: that pass is gated on `[sleep_cycle] enabled` and on the primary brain's breaker, while `[memory_search] enabled` is an independent setting, and this makes no model call. Seeded to 0 so a fresh daemon indexes on its first tick — a restart is the one moment an overlay edited while the daemon was down is guaranteed to be unindexed. Skipped without a mount and without `[memory_search] auto_index_memory_files` |
| `scheduler_stats_interval` | `60` | Seconds between `scheduler_stats` health-line emits (threads / fds / rss / running-tasks / active-workers) — one `key=value` INFO line per interval on the `istota.scheduler.stats` logger, for catching resource leaks early. 0 disables |
| `loop_stall_alert_seconds` | `180` | Defense-in-depth: a watchdog thread logs an ERROR and fires one operator alert if the single-threaded main dispatch loop hasn't ticked in this long (a slow call that slipped onto the loop thread, a wedged check), then re-arms when the loop recovers. Suspended around known multi-minute in-loop work (sleep cycles, DB-health sweep) to avoid false pages. 0 disables |

### Progress & event streaming

One persisted, typed event stream per task (the `task_events` table) feeds Talk, the web SSE endpoint, the log channel, and push notifications.

| Setting | Default | Description |
|---|---|---|
| `progress_updates` | `true` | Master toggle for Talk progress updates |
| `progress_show_tool_use` | `true` | Emit `tool_start` / `tool_end` events |
| `progress_show_text` | `false` | Emit `progress_text` events (intermediate text; noisy) |
| `event_log_enabled` | `true` | Write events to the `task_events` table (kill-switch for task-event-streaming) |
| `stream_text_gate_chars` | `280` | Narration gate for streamed answer text on stream surfaces (web/REPL). A text run emits no `text_delta` until it crosses this many chars without an intervening tool call, so short lead-in narration ("Let me check…") is discarded at the tool boundary instead of leaking into the answer area. Never loses text — only animation. 0 disables |
| `push_notification_threshold_seconds` | `30` | Min task duration before an ntfy completion push fires |
| `push_notification_sources` | `[]` | Source types that trigger a completion push; empty = ntfy opt-in only (never a default surface) |

### Worker pool

| Setting | Default | Description |
|---|---|---|
| `max_foreground_workers` | `5` | Instance-level fg worker cap |
| `max_background_workers` | `3` | Instance-level bg worker cap |
| `user_max_foreground_workers` | `2` | Global per-user fg default |
| `user_max_background_workers` | `1` | Global per-user bg default |
| `long_task_threshold_minutes` | `10` | A *running* foreground task older than this stops counting against its user's interactive cap (0 = disabled) |
| `user_max_long_workers` | `1` | Per-user allowance of discounted long tasks; additive, so the per-user fg thread ceiling becomes 3 |
| `max_long_workers` | `2` | Instance-wide budget of discounts, partitioned inside `max_foreground_workers`, which stays the hard thread ceiling |
| `worker_idle_timeout` | `10` | Seconds before idle worker exits |
| `worker_idle_poll_interval` | `0.5` | Idle worker's queue re-check cadence |
| `main_loop_read_timeout_ms` | `2000` | SQLite read timeout on the main loop |

### Robustness

| Setting | Default | Description |
|---|---|---|
| `task_timeout_minutes` | `30` | Claude Code execution timeout |
| `confirmation_timeout_minutes` | `120` | Auto-cancel confirmations after |
| `stale_pending_warn_minutes` | `30` | Warn for long-pending tasks |
| `stale_pending_fail_hours` | `2` | Auto-fail ancient tasks |
| `worker_heartbeat_seconds` | `60` | How often a running worker pings liveness (0 disables). Stuck-task reclaim uses the heartbeat to tell a slow-but-alive worker from a dead one. |
| `worker_stuck_minutes` | `10` | Reclaim a heartbeating worker's task after this much heartbeat silence. Independent of `task_timeout_minutes`. |
| `task_retention_days` | `7` | Delete old completed tasks |
| `usage_retention_days` | `180` | Prune token/cost records. Kept far longer than tasks so spend history survives task cleanup; `0` disables |
| `email_retention_days` | `7` | Delete old IMAP emails (0 = disable) |
| `processed_email_retention_days` | `90` | Prune the processed-email dedup ledger (0 = disable). Floored at `email_retention_days + 1`; disabled entirely when `email_retention_days` is 0 |
| `talk_cache_max_per_conversation` | `200` | Max cached Talk messages |
| `scheduled_job_max_consecutive_failures` | `5` | Auto-disable threshold |
| `cron_max_staleness_minutes` | `60` | Skip cron-driven catch-up fires older than this (jobs + briefings). After a long daemon outage, fires missed by more than N minutes are skipped and `last_run_at` is bumped so the schedule resumes from the next future fire. 0 = legacy unconditional catch-up. |
| `log_channel_show_skills` | `true` | Include selected skills in log channel messages |
| `max_retry_age_minutes` | `60` | A task older than this is failed rather than retried |
| `temp_file_retention_days` | `7` | Delete task temp files older than this |
| `location_ping_retention_days` | `365` | Prune per-user `location.db` pings older than this |

### Subtasks

| Setting | Default | Description |
|---|---|---|
| `max_subtasks_per_task` | `10` | Cap on subtasks one task may queue |
| `max_subtask_depth` | `3` | Cap on subtask nesting depth |
| `max_subtask_prompt_chars` | `8000` | Cap on a subtask's prompt length |

### Database backup

| Setting | Default | Description |
|---|---|---|
| `db_backup_enabled` | `true` | Take timed online-backup snapshots of the local DBs |
| `db_backup_interval` | `86400` | Seconds between snapshots (24h) |
| `db_backup_dir` | `""` | Destination for dated snapshot dirs; empty derives `{nextcloud_mount_path}/istota-db-backups`. Use `db_backup_enabled = false` to disable |
| `db_backup_retention` | `7` | Keep this many snapshot dirs |

### Host memory: breadcrumb, admission gate, snapshots

| Setting | Default | Description |
|---|---|---|
| `host_pressure_enabled` | `true` | Master switch for host-memory sampling. `false` turns off the breadcrumb, the gate and the snapshots together |
| `host_pressure_breadcrumb_interval_seconds` | `300` | Seconds between `host_pressure` lines (0 = disabled) |
| `host_pressure_sample_interval_seconds` | `30` | Seconds between the samples the admission gate and the snapshot trigger read (0 = disabled). Faster than the breadcrumb because this reading feeds a decision rather than a series |
| `min_available_memory_mb` | `768` | Admission floor. Below this much `MemAvailable`, no new worker is spawned and no idle worker claims; pending rows wait exactly as they do when a cap is full. 0 disables this half of the gate |
| `host_pressure_psi_threshold` | `40.0` | `memory some avg10` above this also counts as pressure and closes the gate. 0 disables this half. With both this and `min_available_memory_mb` at 0 the gate is open unconditionally |
| `host_pressure_alert_cooldown_seconds` | `900` | Minimum gap between threshold snapshots, between admin notifications, and between the `dispatch_admission_closed` log lines for one continuous closed stretch |
| `host_pressure_shmem_unaccounted_alert_mb` | `1024` | A third snapshot trigger: shmem that no filesystem accounts for, above this many MB. Deliberately **not** wired into the admission gate — a residue swap absorbs is a reason to collect evidence, not a reason to refuse work. 0 disables |
| `host_pressure_docker_socket` | `/var/run/docker.sock` | Read-only handle used only to ask Docker which pid a container has, so its tmpfs can be read through `/proc/<pid>/root` during a snapshot. Empty disables container lookup |

One breadcrumb line per interval carries `MemAvailable` / `Shmem` / `SwapFree` / PSI / per-tmpfs usage / `shmem_unaccounted`, written whether or not the host is under pressure — 288 lines a day at the default. See [host memory breadcrumb](../architecture/scheduler.md#host-memory-breadcrumb) for why it is unconditional and what `shmem_unaccounted` answers, and [admission gate](../architecture/scheduler.md#admission-gate) for what the gate does and does not touch.

The gate **fails open** in every uncertain case — sampling disabled, no sample yet, a sample that would not parse. A broken sampler must not be able to halt all dispatch.

### Per-task cgroups

| Setting | Default | Description |
|---|---|---|
| `task_cgroup_enabled` | `true` | Put each task's process tree in its own cgroup v2 group, so a runaway build or test suite is OOM-killed inside its own group instead of taking the host down. No-ops with a log line on a deployment whose unit file has no delegated subtree |
| `task_memory_max_mb` | `2048` | `memory.max` per task (0 = unbounded) |
| `task_pids_max` | `512` | `pids.max` per task — bounds a fork storm |
| `task_cpu_max_percent` | `200` | `cpu.max` as a percentage of one core (200 = two cores; 0 = unset) |

These need `Delegate=` and `DelegateSubgroup=supervisor` on the scheduler unit, which the Ansible role ships. Without them containment never engages; the daemon says so once in the startup log rather than looking protected. See [per-task cgroups](../architecture/scheduler.md#per-task-cgroups).

## `[security]`

| Setting | Default | Description |
|---|---|---|
| `sandbox_enabled` | `true` | Bubblewrap filesystem isolation (Linux only) |
| `skill_proxy_enabled` | `true` | Credential proxy via Unix socket. Required wherever `sandbox_enabled` is true, for two reasons. Turning it off leaves every configured service credential in the task environment, readable by the model from inside the sandbox rather than injected per call — the quiet cost, and the one that undoes what the sandbox is for. It also leaves skill CLIs running inside the sandbox, where the databases they read are masked out, so a CLI that can't reach the proxy refuses rather than reading nothing |
| `skill_proxy_timeout` | `300` | Proxy command timeout (seconds) |
| `passthrough_env_vars` | `["LANG", "LC_ALL", "LC_CTYPE", "TZ"]` | Extra env vars for subprocess |
| `sandbox_ro_paths` | `[]` | Extra RO bind-mounts in the sandbox, for co-located services. Keep entries narrow — a broad path sweeps in whatever lives under it. The DB directories are masked after this list either way |
| `sandbox_cache_dir` | `""` | **The fallback** root for the package managers' caches, read only where the developer skill is not configured. With `[developer] enabled` and `repos_dir` set, each user's cache is *derived* at `{repos_dir}/{user_id}/.package-caches` and this key is not consulted at all — so setting it on a developer deployment names a path nothing uses while reading like the intended one. Where it is read, each user gets `{root}/{user_id}`, bound RW into their sandbox with `UV_CACHE_DIR`, `XDG_CACHE_HOME` and `npm_config_cache` pointed at it. Empty leaves those caches on bwrap's own root tmpfs — RAM the host cannot attribute, discarded at task exit, so every task downloads again. The derivation exists for the mount rather than for tidiness: uv populates a venv by hardlinking out of its cache and `link(2)` compares *mounts* rather than devices, so a cache outside the bound subtree returns `EXDEV` even on the same filesystem and every worktree pays for a full copy. On this fallback branch nothing binds an ancestor, so that copy is what a venv in the task workspace pays — the same cost as before ISSUE-305 addressed the RAM, not a new one. The root must already exist and be writable; the Ansible role creates it when the key is set. Ignored with one warning when relative, missing, unwritable, under a database directory, or at or above a path the sandbox already mounts |
| `sandbox_cache_sweep_enabled` | `true` | Bound what the per-user caches grow to (`src/istota/sandbox_cache_sweeper.py`). Every sweep runs the package managers' own cheap reclaim (`uv cache prune`, `npm cache verify`); a per-user cache still over its ceiling afterwards is wiped with their `clean` verbs. The sweeper deletes no file itself — a tool that is missing, that fails or that times out is reported and the cache is left alone. A user with a task in flight is skipped entirely, and `--force` is never passed to uv, so its own in-use check still stands. On the derived layout the sweep is over the users the daemon knows about rather than the directory names it finds, so a cache belonging to nobody configured is reported and left rather than swept. Runs on `[scheduler] sandbox_cache_sweep_interval` |
| `sandbox_cache_max_gb` | `10.0` | The ceiling, per user, over the whole of that user's cache directory. The Ansible role sets 4.0 instead, sized against the reference deployment: one `uv sync --all-extras` writes about 1.8 GB, so the ceiling has to clear one resolution, and the caches share a 40 GB volume with the worktrees. A size budget rather than an age window for the same reason — one command writes more than any sane window's worth of bytes at once. Clamped to a 1 GiB floor: below that the ceiling is under one dependency resolution's working set, so every sweep would wipe a cache that is doing its job. `XDG_CACHE_HOME` points at the user's cache directory, so a third tool's cache counts toward the budget while neither reclaim verb can touch it — that case is reported with the largest remaining subdirectory named, not deleted by hand |

`sandbox_admin_db_write` was removed: the framework DB is no longer bound into the sandbox for anyone, so there is no bind left to widen. A stale key logs a warning and is ignored.

### `[security.network]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Network isolation via CONNECT proxy |
| `allow_pypi` | `true` | Allow PyPI access |
| `extra_hosts` | `[]` | Additional allowed hosts |

## `[skills]`

There is no `[skills]` config section. Skill disclosure is single-axis (a skill is either eager or a menu entry the model loads on demand) with no config knobs. A stale `[skills]` block only logs a warning at load time.

## `[models.aliases]`

The operator-visible model alias registry — one table holding **both** the portable tiers and the provider shortcuts, overlaying the code-shipped defaults (`brain.claude_code.DEFAULT_ALIASES`). Used by `!model <name> <prompt>` in Talk/web and by internal subsystems (`fast` for triage/classification, `general` for sleep cycle, `smart` is user-facing only).

Shipped defaults (base names, no baked effort):

| Alias | Default target |
|---|---|
| `fast` | Haiku |
| `general` | Sonnet |
| `smart` | Opus |
| `opus` / `sonnet` / `haiku` | current-latest of each |
| `default` | no override (brain/config default) |

Effort is an orthogonal **`:effort` modifier** appended to any reference (`opus:high`, `smart:low`, `claude-opus-5:xhigh`) — never baked into a name. An alias override is **per model namespace** so one definition covers every brain family: `anthropic` = the CLI brains (`claude_code` / `tmux_claude`), `openai_compat` = native. Two forms, both accepted:

```toml
# Flat (namespace-agnostic, resolved by whichever brain runs the task):
[models.aliases]
smart = "claude-opus-4-6:high"   # pin smart to Opus 4.6, effort high
deep  = "opus:max"               # a custom alias

# Per-namespace (define once, correct on every brain):
[models.aliases.smart]
anthropic     = "opus:high"                                          # CLI brains
openai_compat = { model = "anthropic/claude-opus-4.8", effort = "high" }  # native endpoint slug
[models.aliases.general]
anthropic     = "claude-sonnet-5"
openai_compat = "anthropic/claude-sonnet-4.6"                        # bare string = no effort
[models.aliases.deep]
anthropic     = "opus:max"
openai_compat = "anthropic/claude-opus-4.8"
portable      = true                                                # a cross-brain custom tier
```

Alias targets **carry effort**: a `:effort` modifier on the target (`opus:high` → high) or an explicit `effort =` on a per-namespace table reaches the wire (explicit wins). An alias uses one form (TOML: a key can't be both a string and a table); a per-namespace table missing the active brain's key falls to that brain's code default. A custom alias is a non-portable pin unless flagged `portable = true` (then it re-resolves across the cross-brain fallback boundary like the built-in tiers). Invalid *anthropic* targets (neither a known alias nor a canonical `claude-*` ID) are warned at config-load time via `Brain.validate_alias_override`; `openai_compat` slugs are sent verbatim (no alias table to validate against).

The old `[models.roles]` key is a **hard rename** to `[models.aliases]` — no longer read; a stale one present logs a one-time migration warning. The old effort-in-name forms (`opus-high`, `opus-46`) no longer resolve.

## `[brain]`

Selects which model-invocation backend the executor uses. See [architecture/brain](../architecture/brain.md) for the protocol and the [native brain runbook](native-brain.md) for the full `[brain.native]` settings.

| Setting | Default | Description |
|---|---|---|
| `kind` | `"claude_code"` | Brain implementation. `"claude_code"` (default) wraps the headless `claude -p` CLI subprocess; `"native"` runs Istota's own in-process agent loop against any OpenAI-compatible model (configured under `[brain.native]`); `"tmux_claude"` drives the interactive `claude` TUI in a detached tmux session to keep traffic on subscription billing (configured under `[brain.tmux]`; set `fallback = "claude_code"` for failover). |
| `source_type_overrides` | `{}` | Per-`source_type` brain override (e.g. route `scheduled` to `native` while interactive tasks stay on `claude_code`). |
| `room_selectable` | `[]` | Brain kinds a chat room may pin for itself, with `!brain` or the web room settings, **and** kinds a scheduled job may pin with `brain` in CRON.md. The name is narrower than the setting: one list bounds every pin written outside your own config, and both are admin-only on top of it. Empty means none may, so the feature is inert until you name kinds. A name here that is not a brain kind is warned about at load and offered to nobody. Listing a kind also widens the `doctor` checks for it — `claude` on PATH, tmux, a native API key — whether or not anything has selected it. Neither a pinned room nor a pinned job fails over: it runs the brain it names or fails saying why. |
| `fallback` | `""` | Brain to rerun a request on when the primary is unavailable |
| `fallback_on_transient` | `true` | Also reroute a persistent `transient_api_error` |
| `fallback_cooldown_seconds` | `900` | Skip an unavailable primary at most this long before retrying it; a usage limit against a Claude subscription instead ends at the quota's own reset (floor 60s). 0 disables |

`[brain.native]` (used when `kind = "native"`, when it is the `fallback`, or when a `source_type_overrides` entry routes to it): `provider` (only `"openai_compat"`), `model` (explicit id), `base_url`, `effort`, `model_overrides`, `extra_headers`, `context_window`, `max_turns`, `max_tokens`, `prompt_caching`, `compaction_reserve_tokens`, `compaction_keep_recent_tokens`, `bash_spill_full_output`, `turn_budget_nudge`, `turn_budget_nudge_early_percent`, `turn_budget_nudge_remaining`, `soft_deadline_percent`, `model_catalog_fetch`, `model_catalog_cache_ttl_hours`, plus the nested `[brain.native.web_fetch]` SSRF-policy block. The API key comes from `ISTOTA_BRAIN_NATIVE_API_KEY`, never the TOML file. Full annotations in the [native brain runbook](native-brain.md).

### `[brain.claude_code]`

This brain's own default model and effort, plus the subscription usage poll. Every field defaults in code, so an absent block is the shipping behaviour. See [the subscription reading](../features/usage.md#the-subscription-reading) for what the poll does and why.

`model` and `effort` are read whatever `kind` is set to, because a fallback or a `source_type_overrides` entry can route here from another primary. They replaced the top-level keys, which were this brain's defaults sitting where they read as deployment-wide (ISSUE-418).

| Setting | Default | Description |
|---|---|---|
| `model` | `""` | This brain's default model when the task pins none. Same values the top-level key took: a canonical id, a shortcut (`opus`), a role tier (`smart`), any of them plus a `:effort` modifier. Empty = the CLI's own default |
| `effort` | `""` | This brain's default effort: `low`, `medium`, `high`, `xhigh`, `max`. Empty = the model's own. A task that pins a *model* takes no effort from here, since an effort chosen for one model need not be valid on another |
| `subscription_usage` | `true` | Poll `api.anthropic.com` for plan utilization at all. Off = the doctor check SKIPs and the admin card is absent rather than showing a reason |
| `subscription_usage_cache_ttl_seconds` | `1800` | One deployment-wide fetch per this window, and the **minimum** backoff after a failure. A server-stated `Retry-After` overrides it, capped at six hours. Floored at 1 — a 0 would fetch on every read. 30 minutes rather than 5 because the shortest window reported is five hours, so polling faster buys no accuracy for the requests it spends, and the endpoint rate-limits a deployment that tries |
| `subscription_usage_timeout_seconds` | `10.0` | Matches `doctor.PROBE_TIMEOUT`. Floored at 1 |
| `subscription_usage_warn_percent` | `80.0` | Doctor WARNs and the tile turns amber at or above this |
| `subscription_usage_high_percent` | `95.0` | The tile turns red at or above this — still a WARN, **never a FAIL** at any utilization, since a busy plan is a fact about the plan rather than a defect in the host. Both clamp to `[0, 100]`; a warn above high is lowered to high and logged, because an inverted pair leaves no amber band |
| `subscription_usage_stale_after_seconds` | `3600` | A cached reading older than this is reported SKIP rather than as a current number |

The credential is not configured here. It is read — never written, never refreshed — from `CLAUDE_CODE_OAUTH_TOKEN`, then `~/.claude/.credentials.json`, then the macOS keychain.

`[brain.tmux]` (used when `kind = "tmux_claude"` or routed-to): every field defaults in code to the prototype's pinned values, so an absent block is behavioral parity. It carries this brain's own `model` and `effort` (ISSUE-418), on the same footing as `[brain.claude_code]`'s and taking the same values — it shares that brain's `anthropic` namespace and runs the same binary, which is why the retired top-level keys migrate onto both. Other knobs include `fallback_trip_threshold`, `fallback_cooldown_seconds`, `ready_timeout_seconds`, `tmux_command_timeout`, `cli_version_pin`, and the pane-text marker lists (`ready_markers`, `trust_markers`, `theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`, `usage_limit_markers` — the last is what drives `stop_reason=usage_limit` and therefore failover) — heuristics pinned to a `claude` CLI version, so a CLI reword that breaks readiness detection is a config hotfix, not a code release. See `config.example.toml` for the full annotated block.

## `[sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable nightly memory extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `lookback_hours` | `24` | How far back to gather day data |
| `memory_retention_days` | `0` | Prune dated memory files **and** ephemeral `memory_chunks` rows (`conversation`, `memory_file`, `channel_memory`) older than N days. Durable `user_memory` chunks are not touched. 0 = unlimited |
| `auto_load_dated_days` | `3` | Days of dated memories injected into prompts; 0 disables |
| `extraction_model` | `"general"` | Role used for the nightly extraction call |
| `curation_model` | `"general"` | Role used for the USER.md curation call |
| `curate_user_memory` | `false` | Run op-based USER.md curation after extraction |
| `curation_log_summary` | `true` | Post a one-line summary to the user's `log_channel` after applied curation ops |
| `knowledge_graph_audit_retention_days` | `365` | Prune `knowledge_facts_audit` rows older than N days. Independent of `memory_retention_days`. 0 = unlimited |

## `[channel_sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable channel memory extraction |
| `cron` | `"0 3 * * *"` | Schedule (UTC) |
| `lookback_hours` | `24` | How far back to gather channel data |
| `memory_retention_days` | `0` | Prune dated channel files and `channel_memory` chunks older than N days. 0 = unlimited |

## `[memory_search]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable memory search |
| `auto_index_conversations` | `true` | Index after task completion |
| `auto_index_memory_files` | `true` | Index after sleep cycle |
| `auto_recall` | `false` | BM25 auto-recall in prompts |
| `auto_recall_limit` | `5` | Max recall results |
| `recency_half_life_days` | `180.0` | Age half-life for the recency down-weight; 0 disables |

## `[briefings]`

Module-level settings for the briefings module (per-user content store + archive). All defaulted, so an absent block is the shipped behaviour.

| Setting | Default | Description |
|---|---|---|
| `archive_retention_days` | `90` | Prune archived briefing results older than this, on insert (0 = keep forever) |
| `default_lookback_hours` | `12` | Seeds the `email` / `rss` source window when a source omits it |
| `newsletter_max_links_per_source` | `20` | Cap on links pulled from one newsletter source |
| `max_source_chars` | `5000` | Cap on a single source's gathered text. The `todos` source spends it item by item and never cuts one in half — a half-item would read as a todo the file does not contain — dropping from the end and saying in its provenance line how many were left out |
| `max_browse_chars` | `20000` | The same cap for a `browse` source, which gathers markdown rather than flattened text. Bigger because the URLs the markdown keeps cost characters, and a frontpage spends its first couple of thousand on masthead chrome before the headline grid starts. A `browse` source's own `max_chars` wins over either cap — it is the only kind that reads one; `email`, `notes` and `todos` take the module cap directly. |
| `shared_block_timezone` | `"UTC"` | Timezone module-owned shared blocks evaluate their cron in. Shared blocks are global (generated once, no per-user timezone), so this is one operator-chosen zone — typically the operator's own, so morning/evening regeneration lines up with their day. An invalid name falls back to UTC at run time. |

## `[[briefing_shared_blocks]]`

Module-owned shared blocks generated once globally under the reserved `__system__` identity and read by any user's briefing through a `shared_block` source. Seeded once into the `shared_block_configs` table, after which the DB is authoritative and admins manage them from the web UI or `istota briefings shared`. Leave unset for the canonical defaults (`world-headlines`, `markets-summary`); an explicit empty list opts out. Only user-agnostic source kinds are allowed (`browse`, `markets`, `email`). See [briefings](../features/briefings.md#shared-curated-content).

## `[[default_briefings]]`

A canonical shared briefing set, seeded once into each opted-in user (per-user
`default_briefings` flag, default on). Same `name`/`cron`/`output`/`blocks`
shape as a per-user briefing; content is blocks-only. (Replaces the retired
`[briefing_defaults]` boolean-component defaults.)

```toml
[[default_briefings]]
name = "Daily"
cron = "0 7 * * *"
output = "talk"

  [[default_briefings.blocks]]
  title = "World News"
  render_mode = "synthesis"

    [[default_briefings.blocks.sources]]
    kind = "browse"
    config = { preset = "ap" }
```

## `[developer]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable developer skill |
| `repos_dir` | `""` | Root of the per-user repository subtrees. A clone lives at `{repos_dir}/{user_id}/{namespace}/{project}.git` with its worktrees beside it, and an admin developer task has only `{repos_dir}/{user_id}` in its sandbox — never the root, so one admin's clones, worktrees, git configs and package cache are not in another's namespace. The package cache is derived at `{repos_dir}/{user_id}/.package-caches`. Create the root and give it to the daemon; everything below it is created per user. An existing shared tree is migrated by `python -m istota.repos_relocate`, which the Ansible role runs — it needs exactly one configured admin to know who owns the clones, and refuses rather than guessing. It leaves `{repos_dir}/.package-caches` behind, orphaned by the derivation and safe to remove by hand |
| `gitlab_url` | `"https://gitlab.com"` | GitLab instance URL |
| `gitlab_token` | `""` | API token |
| `gitlab_username` | `""` | GitLab username for HTTPS auth |
| `gitlab_default_namespace` | `""` | Default namespace for short repo names |
| `gitlab_reviewer` | `""` | GitLab username to assign as MR reviewer. `glab mr create --reviewer` resolves by username |
| `gitlab_reviewer_id` | `""` | That user's numeric id. Recorded for reference; read by nothing |
| `github_url` | `"https://github.com"` | GitHub instance URL |
| `github_token` | `""` | Personal access token |
| `github_username` | `""` | GitHub username |
| `github_default_owner` | `""` | Default org/user for short repo names |
| `github_reviewer` | `""` | PR reviewer username |
| `author_credit` | `""` | Name credited as author on commits the bot makes |
| `forge_cli_extra_denied` | `[]` | Extra verbs the `gh` / `glab` wrapper refuses, written as typed (`"gh repo delete"`); no binary name applies to both |
| `forge_cli_permit` | `[]` | Baseline deny entries to turn off — each one removes a guard |
| `gh_bin_path` | `"/usr/local/bin/gh"` | Real `gh` the wrapper execs. Both deploy shapes render the path they installed to, since neither matches this default: Ansible `/usr/bin/gh` (Debian archive), docker `/usr/local/lib/istota_forge/gh` (off PATH, so the wrapper stays the only `gh` a task can resolve) |
| `glab_bin_path` | `"/usr/local/bin/glab"` | Real `glab` the wrapper execs, rendered the same way |
| `devbox_proxy_enabled` | `true` | Keep tokens host-side behind the devbox proxy |
| `devbox_proxy_socket_dir` | `"/var/run/istota"` | Where the per-user devbox proxy sockets live |
| `devbox_proxy_audit_log` | `""` | Optional path for a devbox proxy audit log |
| `worktree_reap_enabled` | `true` | Remove a task worktree under `repos_dir` once every commit on it is upstream, the checkout is clean and it has been idle for the retention window (ISSUE-288). The sweep runs on `[scheduler] worktree_reap_interval` |
| `worktree_retention_hours` | `24.0` | Idle hours before a worktree is a reap candidate. This is what protects a task running right now, not just a stale checkout. Clamped to a one-hour floor — anything shorter reaps a worktree a task is still setting up |

How the wrapper behind `gh_bin_path` / `glab_bin_path` actually works — where it takes its policy and trust anchors from, and why it refuses rather than falling back to a public host — is written out under [`[devbox]`](#devbox), since one file serves both the sandbox and the container.

### `[developer.container]`

The exec transport into the devbox. This block configures the transport; it no longer decides that there is one. Where project code builds and runs is derived from three settings, all of which have to be on: `[developer] enabled`, a non-empty `[developer] repos_dir`, and `[devbox] enabled`. With any of them off, `npm`, `uv`, `cargo` and the rest run on the host inside the task's sandbox, as they always did.

A deploy-time choice, not a runtime one: within a deployment there is exactly one place a build happens, so nothing on the host ever consumes an environment the container built and no parity rule has to hold.

| Setting | Default | Description |
|---|---|---|
| `exec_socket_dir` | `"/run/istota-exec"` | The **parent**. The socket is `{exec_socket_dir}/{user_id}/exec.sock`, and only the per-user subdirectory is ever mounted into a container |
| `connect_timeout_seconds` | `5.0` | The client's connect budget, and the only timeout on the connect path |
| `idle_timeout_seconds` | `3600` | Server-side backstop reap for a connection with no traffic either way. Deliberately longer than most task timeouts, so in practice it only fires on an orphan whose task is already gone. Do not lower it to something that would kill a long link step |
| `shim_commands` | fifteen, below | The commands fronted by a shim |

The default list is `npm npx pnpm yarn node uv uvx pip pip3 cargo rustc rustup go bundle gem`. The list is a *routing* declaration — "if you type this, it belongs in the container" — and not a promise that the container has the tool: `cargo`, `rustc` and `rustup` are routed but deliberately not in the devbox image, so they are installed on demand and a `devbox reset` takes them again. A shimmed command the container does not have refuses with a line saying so, and exits 120 having run nothing. Two absences are deliberate and one of them is not negotiable. `python3` is refused whatever you write here, because the sandbox launches its own network bridge with it — a shim would route that into the container and break egress for every developer-enabled task. So are the shells, `env`, `git`, `gh`, `glab` and `istota-skill`, each for the same class of reason. `make` is merely omitted: shimming a driver command inverts routing for everything beneath it, so a Makefile calling `git`, `gh` or `python3` would get the container's copies. Add it if you know your Makefiles; the cost of leaving it out is a recipe that calls `./node_modules/.bin/<tool>` by path.

Routing builds into the container needs the three settings above, a container for the user, and the devbox image rebuilt with the daemon's own uid. The Ansible role does the last of those from a `getent` lookup. **A mismatched uid is the one failure with no error message anywhere that names it**: the container cannot write into a worktree the daemon made, and once that is worked around the daemon cannot unlink a tree the container made, so every worktree that ever ran a build becomes permanently unreapable.

`backend` is retired. It used to sit in this block with the values `none` and `devbox`, and it could disagree with `[devbox] enabled`. Both pairings were bad: a devbox that was on alongside `backend = "none"` gave the model a devbox skill whose every verb but `reset` refused, and the reverse asked the developer skill to reach a container the role had never built. A file still carrying the key gets a WARNING at config load and a `WARN` from `istota doctor --only developer.container`. The value is ignored, not honoured. If you set it to `"none"` to keep builds on the host, turn `[devbox] enabled` off instead.

`istota doctor --only developer.container` answers the five questions that each fail silently — does the rendered config derive what the running daemon is running (re-derived from the three inputs, since there is no key left to read), does the transport answer, do the two sides agree on uid and repos root, is the derived package cache visible inside the container, and is the command reaper running. That last one needs no configuration: the cache is `{repos_dir}/{user_id}/.package-caches`, inside the subtree the container already mounts, so cache and venv share one mount and uv hardlinks instead of copying. Leave `[security] sandbox_cache_dir` blank here — it is the fallback for a deployment without the developer skill.

**`[developer] repos_dir` is a per-user root whether or not the devbox is on.** The daemon derives `{repos_dir}/{user_id}` and hands that to the sandbox bind, `DEVELOPER_REPOS_DIR` and the credential scrub. That is what stops one user's coding task reaching another's checkouts, and it has nothing to do with containers — so an upgraded host has to move its existing clones down a level before the developer skill can see them again. The Ansible role does the move; with more than one configured user it needs `istota_developer_repos_migrate_to` and fails the play until it gets one, because the old layout recorded no owner and a green play there is followed by a daemon restart into a state where the bind names an empty directory.

### `[developer.review]`

The `code_review` skill's models, caps and budget. There is no separate feature flag — the skill is already gated by `developer.enabled` and an admin check, so `enabled = false` here is the off switch.

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Run a review before opening a merge request |
| `conformance_model` | `"general"` | Role alias for the spec-conformance reviewer. A `:effort` modifier is honoured |
| `bughunt_model` | `"smart:high"` | Role alias for the second, skeptical reviewer |
| `both_agents_threshold_lines` | `150` | Diffs at or above this get both reviewers |
| `boundary_patterns` | auth, secret, credential, token, password, migration, schema.sql, billing, payment, money, crypto, sandbox, proxy, deploy, ansible | Case-insensitive substrings matched against changed paths. A hit puts both reviewers on the diff however small it is |
| `max_diff_chars` | `200000` | Cap on the diff handed to a reviewer |
| `max_context_chars` | `60000` | Cap on the assembled surrounding context |
| `max_file_chars` | `20000` | Per changed file, for whole-body inclusion; over it that file falls back to its own hunks |
| `max_callers_per_symbol` | `8` | Cap on caller sites gathered per changed symbol |
| `max_need_files` | `6` | Files a reviewer may request on its one re-invocation. `0` disables the round trip, and the offer is then kept out of the prompt rather than made and refused |
| `timeout_seconds` | `120` | Per agent. Both run concurrently, so this is wall time |
| `max_calls_per_task` | `8` | Review rounds per task |

`max_calls_per_task` counts *waves* of model calls, not `code_review run` invocations. One run charges 1, or 2 when a reviewer took its `max_need_files` round trip; a wave is up to four invocations, since each of two agents may retry a malformed answer once. Guard refusals and breaker skips are free. At the cap the review degrades to `skipped` rather than erroring — a blocking cap would stop a task that had already finished its work from landing it. `0` or less permits no reviews at all rather than reading as "unlimited"; use `enabled = false` to switch the feature off.

## ntfy push notifications

ntfy is a **per-user connected service** — there is no `[ntfy]` config block. Each user supplies their own server URL, topic, and (optional) auth via the encrypted `secrets` table (see [credentials](credentials.md) for the full per-user credential inventory):

```bash
istota secret ensure --user alice --service ntfy --key topic --value alice-alerts
istota secret ensure --user alice --service ntfy --key server_url --value https://ntfy.example.com
istota secret ensure --user alice --service ntfy --key token --value tk_…
```

Or via the web UI at `/istota/settings` (Connected services → ntfy push). Priority is hardcoded to `3` (the ntfy default).

What it IS: a one-way push channel (bot → device) used by heartbeat alerts and scheduled-job output (`output_target = "ntfy"`). What it ISN'T: two-way (no replies), a Talk replacement, operator-shared infrastructure, or required.

## Money

Money is a **module** (on by default; opt out per user via `disabled_modules = ["money"]`). Per-user money settings live in the per-user money DB, not in `config.toml`; the one instance-level knob is:

| Setting | Default | Purpose |
|---|---|---|
| `autoclass_lookup` | `true` | Allow transaction auto-classification to look up unknown payees |
 The bot auto-discovers `*.beancount` files at the top level of `{user_workspace}/ledgers/` — no per-resource path is required. Monarch credentials are a cookie pair in the encrypted `secrets` table — provision both keys (`session_id` and `csrftoken`) via the CLI or the web settings UI:

```bash
istota secret ensure --user alice --service monarch --key session_id --value …
istota secret ensure --user alice --service monarch --key csrftoken --value …
```

## `[google_workspace]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable Google Workspace skill |
| `client_id` | `""` | Google OAuth client ID |
| `client_secret` | `""` | Google OAuth client secret (or `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` env var) |
| `scopes` | Drive, Gmail, Calendar, Sheets, Docs | OAuth scopes to request |

See [Google Workspace](../features/google-workspace.md) for setup instructions.

## `[site]`

`hostname` is load-bearing beyond OAuth2 and the origin check: minting a location ingest token refuses with a 409 while it is unset, because the webhook URL it assembles would be a relative path and the phone's QR decoder accepts only `https://`. A standalone local install with no hostname therefore cannot provision a device by QR.

The deployment's public DNS name.

| Setting | Default | Description |
|---|---|---|
| `hostname` | `""` | Public DNS name; used by the web app for OAuth2 redirect derivation, origin/CSRF checks, and webhook URLs |

The agent-writable static web root (`enabled` / `base_path`) was removed. A publicly-served directory the agent could write to with an ordinary `cp` was an outbound egress channel the confirmation model treated as a benign local write, so anything the agent could read could be published to a public URL without a gate. Serve static assets outside istota, from a directory the agent cannot reach. A stale `enabled` / `base_path` key logs a warning at config load and is ignored.

## `[web]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable web interface |
| `auth` | `"nextcloud"` | Auth mode. `"nextcloud"` is OAuth2 against Nextcloud; `"none"` disables auth entirely for the single-user local install and must never be used on a reachable host. Env override: `ISTOTA_WEB_AUTH` |
| `token_storage` | `"ephemeral"` | Where per-user Nextcloud tokens live. `"ephemeral"` keeps them in the session only; `"encrypted"` retains them in `web_user_tokens` and requires `ISTOTA_WEB_TOKEN_KEY`. Any other value warns and falls back to ephemeral. **The Docker deployment renders `"encrypted"`**, because its entrypoint mints `/data/.web_token_key` itself; every other shape leaves that key to the operator and so cannot assume it exists. Env override: `ISTOTA_WEB_TOKEN_STORAGE` |
| `port` | `8766` | Web app port |
| `oauth2_provider` | `""` | Public Nextcloud URL (browser-facing), no trailing slash |
| `oauth2_client_id` | `""` | NC OAuth 2.0 client ID |
| `oauth2_client_secret` | `""` | NC OAuth 2.0 client secret (or `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` env) |
| `oauth2_token_endpoint` | `""` | Optional server-to-server token URL override |
| `oauth2_userinfo_endpoint` | `""` | Optional server-to-server userinfo URL override |
| `oauth2_redirect_uri` | `""` | Explicit redirect URI override; otherwise derived from request |
| `max_avatar_kb` | `4096` | Byte cap on a profile-picture upload, in KB. Enforced on the declared `Content-Length` before the body is read and again on the running total. `0` switches uploads off — both the user's own `PUT /settings/avatar` and the admin `PUT /admin/avatar` answer 503, and a stored picture is still served. `istota bot-icon set` keeps working and falls back to this field's own default: the cap is about an unauthenticated network body, and the CLI reads a local file as the operator. Separate from nginx's `client_max_body_size`, which bounds what reaches the process at all and is sized for chat attachments |
| `avatar_import_from_nextcloud` | `true` | Whether the scheduler imports users' Nextcloud profile pictures. Only a *custom* avatar is imported — Nextcloud generates a coloured letter for a user who has set none, and importing that would swap the app's own initial chip for Nextcloud's version of the same idea, indistinguishable afterwards. Inert on a local storage backend. Cadence: `[scheduler] avatar_import_interval` |
| `session_secret_key` | `""` | Session signing key (or `ISTOTA_WEB_SESSION_SECRET_KEY` env) |

### `[web.chat]`

Knobs for the in-app web chat surface (the "Chat" tab). The surface is always enabled when the web UI is on; these tune limits and streaming cadence.

| Setting | Default | Description |
|---|---|---|
| `max_prompt_chars` | `32000` | Max characters accepted per chat message |
| `max_attachment_mb` | `25` | Max attachment size, in MB. Application default — the Ansible role sets `100` and renders nginx's `client_max_body_size` from the same variable (see below) |
| `attachment_extensions` | `pdf png jpg jpeg heic webp gif txt md csv wav mp3 m4a ogg webm docx xlsx` | Allowed attachment file extensions — images (including `heic`, what an iPhone photo is), documents, text, and the audio formats a voice message arrives in |
| `rate_limit_messages` | `30` | Messages allowed per user per window |
| `rate_limit_window_seconds` | `300` | Rate-limit window (5 minutes) |
| `sse_poll_interval_ms` | `200` | Server-side `task_events` poll cadence for the SSE stream |
| `client_poll_interval_ms` | `1500` | Client fallback poll cadence when SSE is unavailable |
| `talk_read_sync_interval` | `60` | Talk→web read-state pull cadence, seconds (0 disables) |
| `room_stream_poll_interval_ms` | `1000` | Server-side `messages` tail cadence for the live room stream |
| `room_stream_keepalive_seconds` | `20` | SSE comment-frame cadence, so a proxy can't drop an idle stream |
| `room_stream_max_batch` | `500` | Rows before a `gap` frame tells the client to reload instead of replay |
| `room_stream_max_bytes` | `2000000` | Serialized-byte budget for the same guard |
| `room_stream_room_check_seconds` | `10` | Per-connection room-metadata diff cadence (0 disables) |

### `[web.map]`

Where the background tiles under the location maps come from (ISSUE-334). The two CARTO URLs used to be literals inside a Svelte component; CARTO began requiring an API key and watermarking unauthenticated requests with "API KEY REQUIRED", so every map surface rendered defaced tiles and no deployment could change that without a code edit.

| Setting | Default | Description |
|---|---|---|
| `provider` | `"openfreemap"` | `openfreemap`, `carto`, `osm`, or `custom`. An unrecognised name warns and falls back to `openfreemap` |
| `api_key` | `""` | CARTO only. Free from <https://carto.com/basemaps/apikey/> |
| `dark_style` | `""` | `custom` only: a MapLibre style URL |
| `light_style` | `""` | `custom` only |
| `attribution` | `""` | `custom` only |

`openfreemap` needs no key and is the default for that reason. Resolution never returns an unusable spec: an unknown provider, a `custom` with no URL or a non-`http(s)` one, **and a keyed provider with no key** all fall back to `openfreemap`. Returning the keyless CARTO templates with a "needs key" flag was the original bug wearing a label — nothing in a browser can act on a flag, so the user still got the watermark. The flag survives on the fallback as the *reason*, which is what lets `istota doctor --only web.basemap` say "carto, with no key" rather than the vaguer "did not resolve as written".

`api_key` **is not a secret.** MapLibre puts it in the tile URL, so it ships to every browser that loads a map and appears in every request they make; CARTO issues these free for exactly that. It is redacted in the admin config view because the name matches the redaction rule, which is harmless but is not a guarantee about the value.

A user can also store their own CARTO key on the Location settings page, where it is a module-owned service rather than one of the account-wide Connected services. **A stored key selects CARTO for that user**, overriding `provider` — otherwise pasting a key would do nothing visible, and the reason would live in a file the user cannot reach. `GET /istota/api/map/basemap` returns that key only already embedded in the tile URL, never as a field.

The `web.basemap` doctor check reads the same resolver as the endpoint, so it cannot pass while the map is blank. It opens no socket, deliberately: CARTO returns 200 with a byte-identical body and ETag for a keyless request and a bogus-key one, so a probe would report a working basemap for a defaced one — and tiles are fetched by the browser over a different route, so a proxied deployment would fail a probe for a basemap every browser renders.

## `[location]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable GPS webhook receiver |
| `webhooks_port` | `8765` | Receiver port |
| `accuracy_threshold_m` | `100.0` | Discard pings less accurate than this, in metres |
| `visit_exit_minutes` | `5.0` | Minutes away from a place before a visit is closed |
| `reconcile_enabled` | `true` | Batch-reconcile visits from pings, cleaning up state-machine drift |
| `reconcile_lookback_hours` | `6.0` | How far back a reconcile pass looks |
| `reconcile_buffer_minutes` | `10.0` | Buffer around the lookback window |
| `reconcile_grace_minutes` | `10.0` | Time away before an *unassigned* ping closes a visit. A gap between two pings at the same place no longer splits it (ISSUE-329): a tracker that goes quiet supplies no evidence that you left, and only an observed ping somewhere else does |
| `reconcile_min_pings` | `3` | Minimum pings for a reconstructed visit to count |
| `reconcile_min_dwell_sec` | `60` | Minimum dwell for a reconstructed visit to count |

## `[caldav]`

Explicit CalDAV override. When any field is set it wins over the value derived from `[nextcloud]`, which is how a standalone install points calendar at an external CalDAV server (Radicale, Fastmail, Google) with no Nextcloud in the picture. All-blank — the default — falls back to the Nextcloud derivation, so server deployments are unaffected.

| Setting | Default | Description |
|---|---|---|
| `url` | `""` | CalDAV base URL |
| `username` | `""` | CalDAV username |
| `password` | `""` | CalDAV password |

## `[browser]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the headless browser container |
| `api_url` | `"http://localhost:9223"` | Browser container's Flask API |
| `vnc_url` | `""` | External noVNC URL, surfaced to the user for observation |

## `[devbox]`

A persistent per-user Linux container. It is where the agent installs packages and compiles things. With `[developer] enabled` and a `repos_dir` set, `enabled` below is also what routes project builds into it over the [exec transport](#developercontainer) rather than onto the host.

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the devbox skill. With `[developer] enabled` and `repos_dir` set, this is also the switch that sends project builds into the container |
| `container_prefix` | `"devbox-"` | Container name is `{prefix}{user_id}` |
| `docker_cli` | `"/usr/bin/docker"` | Host path to the Docker CLI binary. Used by the `reset` verb and nothing else |
| `max_output_bytes` | `102400` | Cap per output stream in the skill's JSON envelope |

**No Docker socket is bound into a task's sandbox, and no `docker` binary either.** The skill CLI reaches the container over the [exec transport](#developercontainer) — a Unix socket into a server running inside it. Two verbs also speak Docker, about the container rather than into it: `status` adds a `docker inspect` for the container's own facts, and `reset` wipes `/home/dev` and restarts the container. Both run host-side in the CLI's own process, outside any sandbox. The per-user Docker-API allowlist proxy that used to stand at `/var/run/docker.sock` in every sandbox is deleted along with its `docker_socket`, `exec_timeout_seconds` and `api_proxy_*` settings; a value left for any of those in a TOML file is read by nothing.

The raw-socket diagnostics — `traceroute`, `mtr`, `tcpdump` — no longer work inside the container. They need `CAP_NET_RAW`, which the deployment dropped: a build needs none of them, and a container holding that capability can pick its own source address and walk past address-scoped firewall rules. Tools that work over ordinary sockets are unaffected, and `ping` is probably one of them: it tries an unprivileged ICMP datagram socket first, which Docker's default sysctls permit.

The devbox runs the same real `gh` and `glab` behind the same wrapper the sandbox uses. `docker/devbox/lib/istota_forge_cli.py` is a byte-identical copy of `src/istota/forge_cli.py`, kept in sync by `scripts/sync-devbox-lib.sh`. The wrapper locates its policy beside whichever copy of itself is executing, and takes `real_bin`, the forge URL and the config dirs from that file rather than from `os.environ` — it runs as a child of the model's own shell, so the environment is not a trust anchor. With no URL resolvable it refuses rather than falling back to a public host. The token comes from the [devbox credential proxy](#developer), which injects it server-side.

## `[playbooks]`

Procedural memory — see [memory](../features/memory.md#layer-6-learned-playbooks-procedural-memory).

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch |
| `recall_limit` | `3` | Top-K playbooks injected per task |
| `min_tool_calls` | `4` | Tool calls a task needs to qualify for distillation |
| `retention_days` | `90` | Age-prune by last use; 0 = keep forever |
| `max_chars` | `0` | 0 = share the global `max_memory_chars` budget |

## `[experimental]`

| Setting | Default | Description |
|---|---|---|
| `features` | `[]` | Operator-enabled feature flags. See [experimental features](../EXPERIMENTAL.md) |

## `[health]`

| Setting | Default | Description |
|---|---|---|
| `max_document_bytes` | `26214400` (25 MiB) | Cap on a single stored document (scan, discharge summary, vaccination card). 0 = unlimited |
