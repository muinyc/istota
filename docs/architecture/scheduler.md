# Scheduler

The scheduler is the central coordinator. It runs a main loop that checks every subsystem on configurable intervals and dispatches worker threads to process tasks.

## Modes

**Daemon mode** (`run_daemon`): Long-running process with a `WorkerPool`. Acquires a file lock on `/tmp/istota-scheduler-daemon.lock` to prevent duplicate instances. Handles SIGTERM/SIGINT for graceful shutdown. It also starts the persistent `AsyncRuntime` (see below) explicitly and stops it on shutdown.

**Single-pass mode** (`run_scheduler`): Runs all checks once, processes tasks until none remain or a `max_tasks` limit is hit, then exits. Used for testing and one-off runs. It shares `process_one_task` (which uses `run_coro`), so it lazily starts the same persistent runtime, then calls `reset_async_runtime()` before returning for a clean shutdown.

## Persistent asyncio runtime

All Nextcloud Talk I/O runs on **one** long-lived asyncio loop on a dedicated daemon thread, against **one** pooled `httpx.AsyncClient`, instead of a fresh `asyncio.run` loop + fresh client per call (`src/istota/async_runtime.py`). This gives TCP/TLS connection reuse to Nextcloud and removes the per-call loop-teardown leak surface.

- **`AsyncRuntime`** owns the loop thread; `submit(coro, *, timeout=None)` bridges sync→async via `run_coroutine_threadsafe`. `stop(timeout=10)` cancels in-flight coroutines, runs cleanup hooks (closing the shared client), then stops the loop — cancel-before-aclose so a hook can't close the client out from under a live request.
- **`run_coro(coro, *, timeout=None)`** is the workhorse every sync Talk call site uses (`run_coro(post_result_to_talk(...))`, `run_coro(poll_talk_conversations(config))`, …). It lazily starts the process-global runtime on first use.
- **`get_talk_client(config)`** is a process-global persistent `TalkClient` singleton; every Talk delivery path pulls from it so they share one connection pool.

**Invariant:** every `TalkClient` method invocation must end up on the persistent loop (via `run_coro`), because the methods issue requests on the loop-bound client. There are no transient `TalkClient(config)` constructions left in daemon Talk paths. Email delivery stays on `asyncio.run` (sync SMTP, not httpx).

## Main loop

```python
recover_orphaned_tasks_on_startup()  # once, under the flock, before any worker spawns

while not shutdown_requested:
    watchdog.tick()
    pool.dispatch()             # FIRST — spawn workers for users with pending tasks
    check_briefings()           # every briefing_check_interval (60s)
    check_shared_blocks()       # every briefing_check_interval
    check_briefing_triggers()   # every tasks_file_poll_interval (30s; NC-app trigger files)
    check_scheduled_jobs()      # every briefing_check_interval
    _run_sleep_cycles()         # off-thread; polled every briefing_check_interval
    check_travel_timezone()     # off-thread; every TRAVEL_TZ_CHECK_INTERVAL
    poll_emails()               # off-thread; every email_poll_interval (60s)
    discover_and_organize_shared_files()  # every shared_file_check_interval (120s)
    poll_all_tasks_files()      # every tasks_file_poll_interval (30s)
    run_cleanup_checks()        # every briefing_check_interval
    write_status()              # every 60s
    check_db_health()           # off-thread; every db_health_check_interval (24h)
    check_doctor()              # off-thread; every doctor_check_interval (1h)
    check_worktree_reap()       # off-thread; every worktree_reap_interval (6h)
    check_sandbox_cache_sweep() # off-thread; every sandbox_cache_sweep_interval (6h)
    check_skill_overlay_reindex()  # off-thread; every skill_overlay_reindex_interval (6h)
    _run_db_backup()            # off-thread; every db_backup_interval (24h)
    _maybe_alert_backup_stale() # every tick
    _emit_scheduler_stats()     # every scheduler_stats_interval (60s; 0 disables)
    _emit_host_pressure_breadcrumb()  # every host_pressure_breadcrumb_interval_seconds (300s)
    _check_host_pressure()      # every host_pressure_sample_interval_seconds (30s); feeds the admission gate
    check_heartbeats()          # every heartbeat_check_interval (60s) — last gated check
    sleep(poll_interval)        # 2s, sub-ticking pool.dispatch() every dispatch_interval
```

`pool.dispatch()` runs first, before the clock is even read — pending-task latency is the thing the loop is optimizing for, and every interval check below it is a potential delay.

Talk polling runs in a separate daemon thread, started at scheduler launch.

### Off-thread periodic checks

Nine checks can outlast a tick, and each is on the list for a reason of its own:

| Check | Why it can run long |
|---|---|
| `db-health` | walks every per-user DB |
| `db-backup` | walks every per-user DB, writing to the rclone FUSE mount, where latency is unbounded |
| `sleep-cycles` | synchronous per-user LLM calls |
| `travel-timezone` | opens a per-user `location.db` and may send a notification |
| `email-poll` | one IMAP connection per message read and another per message with attachments, each attachment uploaded to Nextcloud over WebDAV — unbounded network I/O whose duration an outside sender can influence (ISSUE-250) |
| `doctor` | `runtime.model_cli` and the forge version checks each spawn a `--version`, so a wedged binary would starve dispatch |
| `worktree-reap` | fetches each bare clone and walks each candidate checkout, so a slow forge or a cold cache would starve dispatch |
| `sandbox-cache-sweep` | walks every cache tree and shells to `uv` and `npm`, either of which can take minutes on a cold cache |
| `skill-overlay-reindex` | embeds every binding overlay, so a cold `sentence-transformers` load would starve dispatch |

Run inline they blocked `pool.dispatch()` for their whole duration. `_spawn_background_check(name, fn, inflight, *, overlap_expected=False)` puts each on a short-lived `bgcheck-<name>` daemon thread, skipping the tick when the previous run under the same name is still alive, so a wedged sweep cannot stack one thread per tick. `overlap_expected=True` (sleep cycles, travel timezone, email poll) demotes that skip log to DEBUG, because for those three a pass outliving the poll interval is the normal case rather than a symptom — an email batch draining a backlog legitimately runs past the interval. Exceptions are contained and logged. The interval clocks advance at spawn time (fixed cadence; the in-flight guard prevents overlap), while the *staleness* alert reads the persisted last-run clock, which only advances on a durable OK run.

`doctor` re-runs the runtime self-check rather than trusting the one at boot, because the drift it catches happens *after* boot: the auto-update cron changes what is installed under a config the daemon already loaded, so a boot-only check is blind to it.

Because none of the known-long checks runs on the loop thread any more, there are no `LoopWatchdog.suspended()` call sites left — the stall watchdog covers the whole loop.

### Host memory breadcrumb

Every `host_pressure_breadcrumb_interval_seconds` (default 300), the loop writes one `host_pressure` line carrying `MemAvailable`, `Shmem`, `SwapFree`, the PSI memory figures, per-tmpfs usage, and `shmem_unaccounted_kb` — `Shmem` minus the summed tmpfs usage. It runs whether or not the box is under pressure, which is the point: a slow leak crosses no alarm threshold until the day it is fatal, and the August 2026 host loss accumulated 4.64 GB of unreclaimable shmem over five days with nothing recording any of it.

`shmem_unaccounted_kb` is the field that does the work. It separates memory some mount can be `du`'d for from memory that lives in no filesystem at all — the distinction the outage investigation could not make, and the reason it never named a culprit.

The line goes to its own logger (`istota.scheduler.pressure`), so a multi-day series comes out whole with `journalctl … | grep host_pressure` and none of the surrounding scheduler chatter. A sampling failure logs under `host_pressure_error` instead, deliberately not sharing the prefix, so a parsed series never picks up a row with no fields. The whole emit is wrapped: instrumentation must not take the daemon down.

It costs six small file reads plus a `statvfs` and a `stat` per tmpfs mount, so it stays on the loop thread rather than paying for a thread every interval. `host_pressure_enabled = false` turns it off; so does an interval of 0. On a platform with no PSI interface (macOS, a kernel without `CONFIG_PSI`) it says so once and then no-ops.

`host_pressure.py` is a stdlib-only leaf. Every reader takes its `/proc` root as a parameter and none of them raise. `python -m istota.host_pressure --snapshot` produces the threshold snapshot that attributes shmem to mounts, containers and `memfd` fd holders.

### Admission gate

`_check_host_pressure()` samples on its own cadence (`host_pressure_sample_interval_seconds`, default 30 — faster than the breadcrumb, because this reading feeds a decision rather than a series) and hands the sample to the worker pool. `WorkerPool._admission_decision()` reads it: below `min_available_memory_mb` of `MemAvailable` (default 768), or with PSI `memory some avg10` above `host_pressure_psi_threshold` (default 40), the gate is shut.

**It is about starting work, never about stopping it.** Running tasks are not consulted, counted, preempted or failed. A shut gate refuses a *start*, and the pending row waits exactly as it does when a worker cap is full. Both dispatch call sites are gated, and so is the claim a worker makes for itself — gating `dispatch()` alone would bound new worker *threads* while an already-alive worker kept claiming into the slot it had, which is the whole of the incident this came from.

**It fails open in every uncertain case**: sampling disabled, both thresholds at 0, no sample yet, a sample that would not parse. `update_pressure(None)` clears rather than latches, so a sampler that starts failing cannot leave a stale bad reading holding the queue shut for the life of the process. The asymmetry is deliberate — one task admitted onto a busy box is a slow task, whereas a sampler defect that halts all dispatch presents as an unexplained total outage.

A shut gate logs `dispatch_admission_closed` once on the first closed tick and then at most once per `host_pressure_alert_cooldown_seconds` (default 900), re-arming when the gate reopens so a fresh squeeze reads as a new event. The same cooldown bounds the threshold snapshot and the operator alert. `host_pressure_shmem_unaccounted_alert_mb` (default 1024) is a third snapshot trigger and is deliberately **not** wired into the gate: unaccounted shmem that swap absorbs is a reason to collect evidence, not a reason to refuse work.

### Per-task cgroups

`task_cgroup.py` puts each task's subprocesses in `<unit cgroup>/task-<id>/` with `memory.max`, `pids.max` and `cpu.max` set from `task_memory_max_mb` (2048), `task_pids_max` (512) and `task_cpu_max_percent` (200, a percentage of one core). A tree that overruns is OOM-killed inside its own group: one failed task instead of a host-wide event. `MemoryHigh=` on the unit bounds the daemon as a whole and does nothing about this case, and bwrap gives a task filesystem and network isolation with no resource isolation at all.

The directory is a **sibling** of the daemon's own leaf, not a child of it. cgroup v2 forbids a non-root cgroup from both holding processes and enabling controllers for its children, so a `task-<id>/` made inside the daemon's cgroup would be created successfully and then contain no `memory.max` — containment that never engages and never reports. `Delegate=memory pids cpu` plus `DelegateSubgroup=supervisor` on the scheduler unit is what makes the sibling shape available; `resolve_root()` walks up from `/proc/self/cgroup` to the `.service` / `.scope` component to find it.

There is no separate capability probe. On a real cgroup2fs the interface files are kernel-made and a writer cannot create them, so a `memory.max` write that succeeds *is* the proof the controller is delegated here; a failure removes the directory again rather than leaving an empty cgroup that reads as containment in `systemd-cgls`.

**Fails open, never silently.** A deployment that has not taken the updated unit file keeps working exactly as before — every function returns quietly rather than raising, and `create()` returns `None` so the caller spawns as it always did. The reason is logged once per process, because "containment engaged" and "containment never engaged" must not look alike in a log.

### Startup orphan recovery

The heartbeat-based stuck-task reclaim *infers* a dead worker and takes up to `worker_stuck_minutes` to do it. A scheduler restart is deterministic instead: the daemon holds a singleton flock, so the moment a fresh instance boots, every `running` / `locked` row belongs to the dead instance. `recover_orphaned_tasks_on_startup` runs once under the flock, before any worker spawns, and resolves each orphan in priority order — `cancel_requested` → `cancelled`; retries exhausted, too old, or an inline-only source (REPL) → `failed`; otherwise back to `pending` with `attempt_count` bumped and every liveness column cleared. Terminal outcomes emit a terminal event frame so a watching web client gets closure instead of a hung spinner; released orphans emit nothing, since the re-run streams its own `task_started`. `pending_confirmation` rows are left alone.

## Worker pool

`WorkerPool` manages concurrent `UserWorker` threads with three-tier concurrency control:

**Instance-level caps**: `max_foreground_workers` (default 5) and `max_background_workers` (default 3) limit total concurrent workers by queue type. Dispatch is two-phase: foreground first, then background.

**Per-user limits**: `user_max_foreground_workers` (default 2) and `user_max_background_workers` (default 1) cap how many workers a single user can have. Individual users can override these in their config.

**Long-task reclassification**: A *running* foreground task whose `started_at` is older than `long_task_threshold_minutes` (default 10) stops counting against its user's interactive cap and counts against a separate long allowance instead. The task itself is untouched — it keeps running on the same worker. This is what stops a forty-minute job from blocking a short question sent to the same queue. Per user the allowance is additive (`user_max_long_workers`, default 1, so the ceiling becomes 3 threads); instance-wide it is partitioned (`max_long_workers`, default 2, inside the unchanged `max_foreground_workers` ceiling). Both bound *discounts*, not long tasks: a task becomes long while already running, so long tasks beyond the allowance keep counting as ordinary occupancy. The freed slot is a loan — once the long task ends, a user over their interactive cap has the surplus worker asked to finish its current task and exit. Note this does not override the per-channel gate below: a follow-up in the *same* conversation as the long task is still held there, so what the allowance unblocks is a question in a different room.

**Per-channel gate**: Before creating a task, the Talk poller checks if an active foreground task exists for the conversation. If so, it queues the message but sends "Still working on a previous request" as an immediate response.

Workers are keyed by `(user_id, queue_type, slot)`. Each `UserWorker` is a thread that processes tasks serially, exiting after `worker_idle_timeout` (10s) of no tasks. Thread safety: fresh DB connections per call, new `asyncio.run()` event loop per worker, `threading.Lock` on the workers dict.

## Task claiming

`claim_task()` uses atomic `UPDATE...RETURNING` with stale lock detection:

1. Fail old stale locked tasks (created > `max_retry_age`, locked > 30 min)
2. Release recent stale locks for retry
3. Fail old stuck running tasks
4. Release recent stuck running tasks for retry
5. Claim next pending: `ORDER BY priority DESC, created_at ASC`

The claim sets `status='locked', locked_at=now, locked_by=worker_id` atomically.

### Stuck-running detection by worker liveness (ISSUE-112)

Steps 3–5 (and the standalone `fail_stuck_locked_running_tasks()` maintenance pass) share `_STUCK_RUNNING_PREDICATE`, which decides "stuck" by **worker liveness**, not raw runtime. A `running` task is stuck when its `last_heartbeat` has been silent longer than `worker_stuck_minutes` (default 10); when no heartbeat was ever recorded it falls back to `started_at` older than `task_timeout_minutes` + grace. The running worker pings `last_heartbeat` every `worker_heartbeat_seconds` (default 60) via the `_task_heartbeat` context manager (`db.touch_task_heartbeat`), so a slow-but-alive worker — notably the in-process native brain, which has no killable PID — is never reclaimed, while a crashed worker is recovered in minutes. (This is distinct from the health-check heartbeat system in `heartbeat.py`.)

`claim_task` and every other `Task`-returning helper (`get_task`, `get_pending_confirmation*`, `get_reply_parent_task`, `get_stale_pending_tasks`, `get_completed_*_since`) route their SELECT/RETURNING through a single `_TASK_COLUMNS` constant in `db.py`. Adding a column means editing one place; missing columns now raise `IndexError` rather than silently returning `None` (the failure mode that masked the brief 027eb1a regression where `task.skill` came back unset and module-poller rows fell through to the LLM path with an empty prompt).

## Task dispatch

`process_one_task` decides between three execution paths based on the task's columns:

| Task shape | Dispatcher | Notes |
|---|---|---|
| `task.skill` set | `_execute_skill_task()` | `python -m istota.skills.<skill>` subprocess. Trusted env via `build_skill_env(list(skill_index), skill_index, ctx)` over the **full** index, so co-declared vars (e.g. `NC_URL` declared on both `files` and `nextcloud`) reach the subprocess. No proxy split. |
| `task.command` set | `_execute_command_task()` | Shell command. Admin-gated (non-admin tasks refused at runtime + dropped at sync time). Same trusted env resolver as skill-tasks. JSON `{"status":"error","error":"…"}` envelopes on stdout are detected and surfaced as failures even when returncode is 0. |
| neither | LLM path via the brain | Default; runs through `execute_task` |

Auto-seeded `_module.*` rows dispatch as skill-tasks — `feeds.run_scheduled` and `feeds.prune`, `money.run_scheduled`, and `health.garmin_sync` for users with a Garmin connection. `_purge_obsolete_skill_jobs` removes rows whose skill name is no longer in the index.

## Task processing

`process_one_task()` handles the full lifecycle:

1. Claim a task (with optional `user_id` filter)
2. Update status to `running`
3. Get user resources, send Talk acknowledgment, download attachments
4. Call `execute_task()` -> `(success, result, actions_taken, execution_trace)`
5. On success:
    - Check for malformed output (leaked tool-call XML) -> reclassify as failure
    - Check for confirmation request (regex pattern)
    - Update to `completed`
    - Index conversation for memory search
    - Deliver results
6. On failure:
    - Check cancellation
    - Retry with exponential backoff if attempts remain (1, 4, 16 min delays)
    - Mark permanently failed after max attempts

## Retry logic

Failed tasks retry with exponential backoff: 1 min, 4 min, 16 min (up to `max_attempts`, default 3). Transient API errors (5xx, 429) get 3 fast retries with 5s delay before counting against task attempts.

**Shutdown collateral is not a failure.** Under systemd's default `KillMode=control-group`, a `systemctl restart` (the auto-update cron issues one per commit) SIGTERMs the whole cgroup, killing an in-flight task's model subprocess while the daemon shuts down gracefully — so the surviving worker recorded the corpse as a failure, permanently so on a final attempt. When `_shutdown_requested` is set *and* the failure text carries a signal-termination marker, the task goes back to `pending` via `db.release_task_for_restart` with **no attempt charged and no backoff**, and its deferred-op files are purged; `fail_ancient_pending_tasks` remains the bound. The client sees a "Scheduler restarting…" progress notice rather than a terminal frame. The unit template also sets `KillMode=mixed`, which converts the same event into the startup orphan-recovery path — this branch is the belt-and-braces half, and the one that ships via auto-update since unit files need an Ansible run.

A related invariant: `worker_pid` is cleared on *every* transition out of `running` (completed, failed, cancelled, pending-retry, restart-release, orphan recovery). It used to survive a failed attempt, so a retry row could carry a dead attempt's PID — and both cancel paths (`!stop`, the web cancel endpoint) signal whatever the row holds, which would eventually land on an unrelated recycled PID.

## Task event streaming

One persistent, typed event stream per task feeds every output surface. `process_one_task` builds an `EventWriter` (`events.py`) per brain-path task and subscribes the in-process consumers (`TalkEventSubscriber`, `LogChannelSubscriber`, `PushNotificationSubscriber`) before passing the writer to `execute_task(event_writer=…)`. The executor adapts the brain's `StreamEvent` stream into `TaskEvent`s, persisted to the `task_events` table (WAL, shared scheduler ⇄ web). When the task reaches a non-retry terminal state the scheduler emits the terminal event (`confirmation` / `result` / `cancelled` / `error` + `done`) and calls `writer.finish()`.

**Retry continuity:** on a retry-eligible failure the event log is kept (not wiped). The retry branch emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice, and the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic across attempts and a watching web client survives the retry instead of hanging on "Working…". The SSE / snapshot endpoints synthesize a terminal frame from the task row (`web_app._synthetic_terminal_events`) for any terminal-without-`done` gap (e.g. a crash that skipped `finish()`).

Config under `[scheduler]`: `progress_show_tool_use`, `progress_show_text`, `event_log_enabled`, `stream_text_gate_chars`, `push_notification_threshold_seconds`, `push_notification_sources`.

## Delivery routing

Where a task's result goes is resolved by `transport.routing.resolve_delivery_plan(config, task, registry)`, which turns a task into an ordered, deduplicated, channel-resolved list of destinations. Precedence: explicit `output_target` > reply-to-origin (interactive source types) > source-type default > drop. `process_one_task` builds the plan once and fans out to every push destination; `stream` destinations (REPL, web) contribute no push work — the `task_events` log is the delivery. Separately, a per-user **purpose-keyed routing table** (`UserConfig.routing`, purposes `reply`/`alert`/`log`/`briefing`/`notification`) routes *notifications* via `notifications.send_notification(..., purpose=…)`. See the [Transport abstraction](overview.md) and `.claude/rules/transport.md`.

## Deferred DB operations

With the bubblewrap sandbox, no database is reachable inside the subprocess at all — the framework DB directory and the per-user module-DB root are covered by empty, read-only tmpfs masks applied as the last mount operations. Claude and skill CLIs write JSON files to a writable temp dir instead. The scheduler processes these after successful completion. The handlers and the file envelope helper live in `scheduler_deferred.py` (extracted from `scheduler.py` for size and testability; `scheduler.py` keeps a re-export shim so `from istota.scheduler import _process_deferred_*` still works).

| File | Handler | Purpose |
|---|---|---|
| `task_{id}_subtasks.json` | `_process_deferred_subtasks` | Subtask creation (admin-only, depth- and rate-capped) |
| `task_{id}_sent_emails.json` | `_process_deferred_sent_emails` | Outbound email tracking for emissary thread matching |
| `task_{id}_kv_ops.json` | `_process_deferred_kv_ops` | KV store set/delete operations |
| `task_{id}_kg_ops.json` | `_process_deferred_kg_ops` | Knowledge-graph fact add/invalidate/delete (per-op commit) |
| `task_{id}_user_alerts.json` | `_process_deferred_user_alerts` | Model-raised notices, one row per `(task, grade)`. `security` and `action_needed` are pushed; `note` is written to the panel and never delivered |
| `task_{id}_health_ops.json` | `_process_deferred_health_ops` | Health module inserts/updates replayed against the per-user `health.db` |
| `task_{id}_email_output.json` | `_deliver_deferred_email_output` | Structured email reply (preferred over the legacy stdout-JSON parser) |
| `task_{id}_garmin_import.json` | `_process_deferred_garmin_import` | Garmin Connect sync requested from inside the sandbox |

`_load_deferred_json(user_temp_dir, task_id, suffix, expected_type=...)` is the shared envelope helper: builds the path, exists-checks, parses JSON, validates the top-level shape (`list` or `dict`), and warns + unlinks on a malformed file. Each handler then runs its own business logic and unlinks at the call site so per-handler invariants (admin gate, depth gate, KG per-op commit) read cleanly.

`_purge_deferred_files_for_retry` clears the slate when a task is re-claimed after a crash, a restart, or a retry, so a non-idempotent op like a KG `invalidate` isn't replayed twice across attempts. `_warn_unconsumed_deferred_files` scans the user temp dir after the drain phase and logs WARN for files missing the `task_` prefix or carrying an unknown suffix; the misnamed file is left on disk for inspection.

One suffix is recognized but never purged: `task_{id}_health_op_failures.json`, in `_KNOWN_ARTIFACT_SUFFIXES`. It is written *by* a handler rather than read by one — the rows a health op lost mid-batch, preserved so an operator can recover them after the task settles.

Identity fields (`user_id`, `conversation_token`) always come from the task, not from the JSON, to prevent spoofing.

## Cleanup

Runs every `briefing_check_interval`:

- Cancel stale confirmations after 120 min, notify user
- Recover stuck `locked`/`running` tasks (mirrors the `claim_task` recovery, for rows no claim happens to touch)
- Log warnings for tasks pending longer than 30 min
- Auto-fail tasks pending longer than `stale_pending_fail_hours` (2)
- Delete completed tasks older than `task_retention_days` (7)
- Prune `task_usage` / `task_usage_models` rows older than `usage_retention_days` (180) — far above the task window on purpose, so spend history survives task cleanup
- Prune `processed_emails` rows older than `processed_email_retention_days` (90, floored at `email_retention_days + 1`), excluding rows the stored transcript still references
- Prune the `message_deletions` ledger (fixed 30 days) — it exists only to tell a reconnecting client what vanished while it was away
- Close open fire-and-forget [notification](../features/notifications.md) rows older than 14 days, then delete closed rows past 30 days. Two sweeps, each in its own transaction for the same reason the usage prune is split out: `get_db` commits once on exit, so a shared block would hold the write lock from the first row the age sweep closes right through the retention delete. The retention delete is chunked at 500, because the first run after a long accumulation would otherwise take the whole backlog in one statement while every reader waits out its busy timeout. Object-backed rows are never swept at any age — their close condition is the object, not the clock — and neither is a row whose source failed to register in *this* process, since one broken import in the scheduler would otherwise close every open row of a source that is live in the web process
- Delete processed emails from IMAP older than `email_retention_days` (7), via one server-side `BEFORE` search
- Trim the Talk message cache
- Delete old temp files
- Delete location pings older than `location_ping_retention_days` (365) from each per-user `location.db`
- Reconcile visits from pings (batch cleanup of visit state-machine drift)
- Delete old Claude session logs

## Poller intervals

| Poller | Default interval | Config key |
|---|---|---|
| Task queue | 2s | `poll_interval` |
| Pending-task dispatch sub-tick | 0.5s | `dispatch_interval` |
| Talk conversations | 10s | `talk_poll_interval` |
| Email (IMAP) | 60s | `email_poll_interval` |
| Briefings/jobs/sleep/cleanup | 60s | `briefing_check_interval` |
| TASKS.md files | 30s | `tasks_file_poll_interval` |
| Shared files | 120s | `shared_file_check_interval` |
| Heartbeats | 60s | `heartbeat_check_interval` |
| SQLite health (`quick_check` + self-heal `REINDEX`) | 86400s (24h) | `db_health_check_interval` |
| Runtime self-check (`istota doctor`) | 3600s (1h) | `doctor_check_interval` |
| Developer worktree reap | 21600s (6h) | `worktree_reap_interval` |
| Sandbox package-cache sweep | 21600s (6h) | `sandbox_cache_sweep_interval` |
| Per-skill overlay search reindex | 21600s (6h) | `skill_overlay_reindex_interval` |
| DB backup snapshot | 86400s (24h) | `db_backup_interval` |
| Scheduler process-health line | 60s | `scheduler_stats_interval` |
| Host memory breadcrumb | 300s | `host_pressure_breadcrumb_interval_seconds` |
| Host memory gate/snapshot sample | 30s | `host_pressure_sample_interval_seconds` |

A 0 disables the check in every row below the dispatch sub-tick. The worktree reap additionally needs `developer.enabled`, `developer.repos_dir` and `developer.worktree_reap_enabled`; the cache sweep needs `security.sandbox_cache_sweep_enabled` and a cache root that resolves under the layout in force; the overlay reindex needs `memory_search.enabled`, `memory_search.auto_index_memory_files` and a mount; the backup needs `db_backup_enabled`.

`dispatch_interval` decouples cold pending-task pickup latency from the interval-gated checks: the main loop runs `pool.dispatch()` on this sub-tick cadence without re-running the per-subsystem checks (0 or ≥ `poll_interval` = legacy one-dispatch-per-tick). `cron_max_staleness_minutes` (default 60) is the insertion-time staleness gate for `check_scheduled_jobs` / `check_briefings` — after a long outage it skips the catch-up insert and resumes from the next future fire, suppressing thundering-herd catch-up.
