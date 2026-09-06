---
paths:
  - "src/istota/scheduler.py"
  - "src/istota/scheduler_deferred.py"
  - "src/istota/db.py"
---

# Scheduler & DB Internals

## Scheduler Functions

### `run_daemon()`
```python
def run_daemon(config: Config, *, install_signal_handlers: bool = True,
               ready_event: threading.Event | None = None) -> None
```
The `install_signal_handlers` / `ready_event` kwargs support the combined
`istota serve` launcher (local install — see AGENTS.md "Local single-user
install"). Defaults reproduce the standalone-daemon behaviour exactly. `serve`
runs this on a worker thread with `install_signal_handlers=False` (signal
handlers are main-thread-only) and owns SIGINT/SIGTERM via uvicorn; it drives
shutdown through `scheduler.request_shutdown()` (sets the shared
`_shutdown_requested` flag). `run_daemon` clears that flag at start, sets
`ready_event` once the pool + pollers are up (right before the loop), and on
flock contention raises `_DaemonAlreadyRunning` (not `return`) so `serve` can
report "already running" — the standalone `main()` catches it → clean `SystemExit(1)`.
The lock path is the module constant `DAEMON_LOCK_PATH` (default
`/tmp/istota-scheduler-daemon.lock`; overridable, notably in tests).

1. Acquire flock on `DAEMON_LOCK_PATH`
2. Set SIGTERM/SIGINT handlers (skipped when `install_signal_handlers=False`)
3. Hydrate user configs from Nextcloud API
4. Ensure user directories
4a. `recover_orphaned_tasks_on_startup(config)` — reclaim tasks left `running`/`locked` by a dead prior instance (see "Startup orphan recovery" below)
4b. Start the persistent `AsyncRuntime` (`async_runtime.get_async_runtime()`) — see below
5. Start Talk polling in daemon thread
6. Create `WorkerPool`
7. Main loop (while not `_shutdown_requested`): `pool.dispatch()`, then
   `_tick_interval_gates(...)` over the gate table, then `_dispatch_sleep`.
   The periodic checks below are **not** written into the loop — each is a row
   in `build_interval_gates(config, …)`, and the loop body is one call. See
   "The interval gate table" below.
   - Check briefings (every `briefing_check_interval`)
   - Check scheduled jobs (every `briefing_check_interval`)
   - Poll the sleep-cycle crons (every `briefing_check_interval`) and run a due pass — per-user then per-channel — **off the loop thread** (`_run_sleep_cycles` = both halves as one unit) — see below
   - Follow opted-in users' timezones on travel (every `TRAVEL_TZ_CHECK_INTERVAL`, 15 min, gated on `location.enabled`) — also **off the loop thread**
   - Poll emails (every `email_poll_interval`) — also **off the loop thread** (`_run_email_poll`)
   - Organize shared files (every `shared_file_check_interval`)
   - Poll TASKS.md files (every `tasks_file_poll_interval`)
   - Run cleanup checks (every `briefing_check_interval`)
   - Check heartbeats (every `heartbeat_check_interval`, off-thread via `_run_heartbeat_checks`)
   - Sweep SQLite DBs (framework + per-user feeds/health/location/money, all local now) with `PRAGMA quick_check` + self-healing `REINDEX` (every `db_health_check_interval`, default 24h; runs immediately on the first tick of the daemon so a fresh deploy surfaces latent index corruption without waiting a day). Dispatched **off the loop thread** via `_spawn_background_check` — see below
   - Snapshot local DBs to `{mount}/istota-db-backups/<date>/…` (dated dirs, retention + collapse guard) via the SQLite online-backup API (every `db_backup_interval`, default 24h; off-host durability now that module DBs are local — clock starts at boot, first snapshot after one interval); alerts the operator on any errored/suspect DB and on backup staleness. Also **off the loop thread** (`_run_db_backup` = snapshot + problem alert as one unit)
   - Emit the `scheduler_stats` health line (every `scheduler_stats_interval`, default 60s; first emit after one full interval; `0` disables)
   - Emit the `host_pressure` memory breadcrumb (every `host_pressure_breadcrumb_interval_seconds`, default 300s; **first emit on the first tick**, not after an interval — a restart is when the post-restart baseline is worth recording; `0` or `host_pressure_enabled = false` disables)
   - Sample for the admission gate + threshold snapshot (`_check_host_pressure`, every `host_pressure_sample_interval_seconds`, default 30s). Separate cadence *and* separate purpose from the breadcrumb: the breadcrumb feeds a multi-day series, this feeds a decision. Hands the reading to `pool.update_pressure()` and, on a `snapshot_trigger` crossing, writes one `host_pressure_snapshot` block and sends one operator alert per `host_pressure_alert_cooldown_seconds`
   - Check invoice schedules (every `briefing_check_interval`)
   - `pool.dispatch()`
   - Sleep `poll_interval`
8. Shutdown workers (`pool.shutdown()`), stop the persistent runtime (`runtime.stop(timeout=10)`), release lock

### The interval gate table (F33)

`build_interval_gates(config, *, pool, background_checks, doctor_state, pressure_state, backup_state)` returns the periodic checks as a list of `IntervalGate` rows, **in the order the loop runs them**. `seed_interval_clocks(gates, config)` builds the one clock dict, keyed by gate name. Two readers:

- `_tick_interval_gates(gates, clocks, config, now, inflight)` — the daemon loop. Runs every gate whose `enabled(config)` holds and whose clock has aged past `interval(config)`, in table order, then advances that gate's clock to `now`. A `background` row goes through `_spawn_background_check` (so its clock advances at *spawn* time, and the in-flight guard — not the clock — is what prevents overlap).
- `_run_interval_gates_once(gates, config)` — `run_scheduler`. Runs the nine rows marked `one_shot`, synchronously, in the same relative order, with no clocks and no interval test. This is what removed the six re-inlined bodies that used to sit in `run_scheduler`.

Ordering is part of the table and some of it is load-bearing: `shared-files` runs before `tasks-file-poll` so the poller finds the files in place, and `backup-stale-alert` sits immediately after `db-backup`. `tests/test_scheduler_interval_gates.py` pins the ordered `(name, field)` list against the bindings the inline gates had, and a grep-shaped guard fails if either loop states a gate again.

Two things about a row read wrongly at a glance:

- **`field` is authoritative and `interval()` is derived from it**, so a name a test reads and a value the loop uses cannot drift. `None` means the interval is not a config field: `travel-timezone` reads `TRAVEL_TZ_CHECK_INTERVAL`, `status-write` a literal 60, and `backup-stale-alert` is not an interval gate at all — it ran on every tick before and still does, and is in the table only so its position stays stated in one place.
- **`enabled` carries the `bool(interval)` term** wherever the inline gate had one, because an interval of 0 otherwise reads as "due every tick" rather than "off".

**Four rows read `briefing_check_interval` for something that is not a briefing** — `shared-blocks`, `scheduled-jobs`, `sleep-cycles` and `cleanup`. Preserved exactly and commented as known: giving them their own keys is an operator-visible config change across `config.py`, `config.example.toml`, the Ansible template and the Docker render, and is separate work.

**`on_error` and `one_shot_on_error` are two fields because the two paths differed.** `None` means the gate propagates on that path. The background spawns were bare in the loop, and `check_briefings` / `check_scheduled_jobs` were bare in `run_scheduler` — so a failure there aborts the one-shot pass while the daemon logs past both, exactly as before.

### Off-thread periodic checks (`_spawn_background_check`, ISSUE-144)

Five periodic checks can run long: the DB-health sweep and the DB-backup
snapshot both walk every per-user DB (the backup writing to the rclone FUSE
mount, where latency is unbounded), the nightly sleep cycles make synchronous
per-user LLM calls, the heartbeat sweep runs the whole doctor registry once per
user with a `self-check` (process spawns, a socket per configured service, and
optionally a live model call), and the inbound email poll makes one IMAP
connection per
message it reads plus another per message with attachments, uploading each
attachment to Nextcloud over WebDAV — network I/O whose duration an outside
sender can influence (ISSUE-250). Run synchronously they blocked `pool.dispatch()` for their
whole duration, and the `LoopWatchdog.suspended()` wrapper needed to keep a
healthy nightly run from paging left the watchdog blind to *real* stalls in the
same window. All five now run on short-lived daemon threads (Tier 1 = the DB
pair, Tier 2 = the sleep cycles, ISSUE-250 = the email poll, and the heartbeat
sweep once `self-check` became a doctor run), and **no `suspended()` call site remains in
`run_daemon`** — the watchdog has full coverage.

Moving the heartbeat sweep off the loop was only half of its fix, and the half
that is easy to stop at. `get_db` is in legacy implicit-transaction mode, so the
sweep's first state write opened the daemon's single SQLite write transaction
and held it until the sweep ended — across every remaining check, and across
`send_heartbeat_alert`, which opens its own connection to the same database on
the web and Talk legs. That self-serialized harmlessly while the sweep *was* the
loop and would have become contention against the loop and the workers once it
was not, so `check_heartbeats` commits per check. Unlike `_run_sleep_cycles`,
which is recorded below as keeping that hold as a residual, this one does not
have it.

- `_spawn_background_check(name, fn, inflight, *, overlap_expected=False)` — spawns
  `fn` on a `bgcheck-<name>` daemon thread, unless the previous run under the same
  name is still alive (then it logs `background_check_still_running` and skips the
  tick, so a wedged sweep can't stack one thread per tick — and a sleep-cycle pass
  outliving its poll interval can't re-fire against state it hasn't stamped yet).
  Exceptions are contained and logged as `background_check_failed`; a crashed run
  frees the slot for the next tick. `inflight` is `run_daemon`'s own
  `background_checks` dict — loop-local, not process-global, so tests and a
  re-entered daemon each start clean. `overlap_expected` demotes the skip log to
  DEBUG for a check polled far more often than it runs (the sleep cycles): a
  nightly pass spanning several 60s ticks is by design, not an overrun.
- `_run_db_backup(config)` — `backup_databases` + `_alert_backup_problems` as one
  unit, since the alert needs the results of the run that produced it.
- `_run_email_poll(config)` — `poll_emails` plus its "queued N task(s)" log.
  `overlap_expected=True`: a batch draining a backlog legitimately outlives the
  60s poll interval, so the in-flight skip is routine rather than an overrun.
  Also the single-pass `run_scheduler` body, synchronously — one-shot mode has
  no dispatch loop to starve, and sharing the function keeps the two paths from
  drifting.
- `_run_sleep_cycles(config)` — `check_sleep_cycles` then
  `check_channel_sleep_cycles` as one unit. Bundled because both are halves of the
  same nightly pass and always came due on the same interval, so one thread
  preserves their dispatch-thread ordering instead of putting two brain-calling
  passes in flight at once; each half is independently try/excepted so a failing
  per-user pass still lets the channel pass run. Each opens **its own** short-lived
  connection (the loop-owned one they used to borrow is gone; nothing inside the
  sleep cycle commits mid-pass, so one shared connection would hold a single write
  transaction across both halves). The two loop clocks collapsed into one
  `last_sleep_cycle_check`. Also the single-pass `run_scheduler` body — one-shot
  mode has nothing to starve, so there it stays synchronous.
- The interval clocks (`last_db_health_check` / `last_db_backup` /
  `last_sleep_cycle_check`) advance at **spawn** time, not completion — fixed
  cadence, and the in-flight guard is what prevents overlap. The staleness alert is
  unaffected: it reads the *persisted* clock, which still only advances on a
  durable OK run.
- Daemon threads by design — an in-flight snapshot dies with the process at
  shutdown rather than delaying it. Backups write dated dirs and the restore path
  sanity-checks them, so a torn snapshot can't clobber the last good one. A sleep
  cycle killed mid-pass leaves its `last_run` unstamped and re-runs next cycle,
  the same outcome as any daemon restart during a nightly run (previously SIGTERM
  waited out the pass, because the loop was blocked inside it).

Residual: the sleep cycle still holds one write transaction per half for the
duration of that half (it has no intra-pass commit), and dispatch now keeps
spawning workers during it, so writer contention in that window is marginally
higher than before — pre-existing, since already-spawned workers always raced it.
Committing per user/channel would need `sleep_cycle.py` atomicity changes.

## Persistent asyncio runtime (`async_runtime.py`)

All Nextcloud Talk I/O runs on **one** long-lived asyncio loop on a dedicated
daemon thread, against **one** pooled `httpx.AsyncClient`, instead of a fresh
`asyncio.run` loop + fresh client per call. This gives TCP/TLS connection reuse
to Nextcloud and removes the per-call loop-teardown leak surface.

- **`AsyncRuntime`** — owns the loop thread. `submit(coro, *, timeout=None)`
  bridges sync→async via `run_coroutine_threadsafe(...).result(timeout)`
  (`timeout=None` = wait forever, matching `asyncio.run`; on timeout it cancels
  the coroutine and raises `TimeoutError`). Calling `submit` from the loop's own
  thread raises (the reentry guard) instead of deadlocking. `stop(timeout=10)`
  cancels in-flight coroutines first, then runs registered cleanup hooks
  (closing the shared client), then stops the loop — cancel-before-aclose so a
  cleanup hook can't close the client out from under a live request. `start()`
  clears stale cleanup hooks so an in-process restart doesn't accumulate them.
- **`run_coro(coro, *, timeout=None)`** — the workhorse every sync Talk call site
  uses: `run_coro(post_result_to_talk(...))`, `run_coro(edit_talk_message(...))`,
  `run_coro(poll_talk_conversations(config))`, etc. Lazily starts the process-global
  runtime on first use (convenience for CLI/tests; `run_daemon` starts it
  explicitly).
- **`get_talk_client(config)`** — process-global persistent `TalkClient`
  singleton. Every Talk delivery path pulls from it (the `TalkTransport` seam,
  the event consumers, `notifications._send_talk`, the inbound poller,
  `commands.dispatch`, `_resolve_channel_name`, `_finalize_log_channel`) so they
  share one connection pool. It is a **synchronous, reentry-safe accessor**: it
  must not call `run_coro` (it's invoked from inside Talk coroutines already on
  the loop), so the underlying httpx pool opens lazily on the first awaited
  method call — which always runs on the persistent loop because every call site
  goes through `run_coro`. `get_async_runtime()` is called so the registered
  `aclose` cleanup hook fires on `stop()`. `reset_async_runtime()` /
  `reset_talk_client()` are test-teardown helpers.

**Invariant:** every `TalkClient` method invocation must end up on the persistent
loop (via `run_coro`), because the methods issue requests on the loop-bound
`self._client`. There are no transient `TalkClient(config)` constructions left in
daemon Talk paths. Email delivery stays on `asyncio.run` (sync SMTP, not httpx).
Single-pass `run_scheduler` shares `process_one_task` (which uses `run_coro`), so
it lazily uses the same persistent runtime; it calls `reset_async_runtime()`
before returning (as does the `istota run` CLI) so the shared client's `aclose`
runs a clean shutdown instead of connections being dropped on process exit.
`run_cleanup_checks` is synchronous — its rare notices (expired-confirmation,
failed-ancient) go through `send_notification` (→ `run_coro`), keeping its
blocking DB/IMAP/fs cleanup off the persistent loop. The expiry notices are
buffered and sent after the DB transaction closes, since one routed to `web`
writes to the same database.

### `run_scheduler()`
```python
def run_scheduler(config: Config, max_tasks: int | None = None, dry_run: bool = False) -> int
```
Single-pass mode: runs all checks once, then processes tasks until none remain or `max_tasks` hit.

### `process_one_task()`
```python
def process_one_task(config: Config, dry_run: bool = False, user_id: str | None = None) -> tuple[int, bool] | None
```
1. `claim_task()` with user_id filter → None if nothing
2. Update to `running`
3. Get user resources, send Talk ack, download attachments
4. `execute_task()` → (success, result, actions_taken, execution_trace)
5. **Success path**:
   - Check malformed output (`detect_malformed_result()`) → reclassify as failure and retry
   - Check confirmation request (regex `CONFIRMATION_PATTERN`)
   - Update to `completed` with `actions_taken` and `execution_trace`
   - Index conversation for memory search
   - Handle heartbeat results / silent scheduled jobs
   - Deliver results
   - Reset scheduled job failures, and close any `cron_job` inbox row the disable had raised
   - Delete a `once = true` job's row, and **buffer** `(user_id, name)` for the CRON.md removal rather than doing it here (ISSUE-387). `delete_scheduled_job` is what takes SQLite's write lock, and CRON.md is on the rclone FUSE mount, so writing it inside the transaction held that lock for whatever the mount took to answer — stalling the dispatch loop, the other workers, the web app and the pollers. `_remove_once_job_from_cron_md` is the flush, called after the `with` block closes and after `deliver_pending`; nothing on that thread reads CRON.md in between. Same shape as the buffered notifications above it and as `run_cleanup_checks`'s expiry notices. It owns the two things the hoist introduced, neither of which the single-threaded reading shows. **It never raises**, because it now runs *after* the commit: an exception would escape with the task already `completed` and everything that finishes it still ahead — delivery, the deferred-op drain, the terminal event — and nothing retries a `completed` row, so a stale file would become a silently lost answer. `remove_job_from_cron_md` states the same contract; the guard is what stops a future edit to the writer from converting one failure into the other. And **it re-deletes the row after a successful write**, because deleting the row and rewriting the file are no longer one step: `_sync_cron_files` runs on the main loop every `briefing_check_interval` with CRON.md as the authority, so a sync landing in the window reads a file that still names the job and re-inserts the row — the `once = true` job that runs a second time. Before the hoist the order was inverted, so a sync in the window saw a row the file did not name and deleted it; the window is also not brief in the case that matters, since a hung mount still answers reads from cache while the write blocks
6. **Failure path**:
   - Check cancellation (`Cancelled by user` → status `cancelled`, no retry). The row's `result` column now carries `task.partial_result` — what the model had written when the cancel landed (ISSUE-372). Nothing is posted, as before: the user stopped this on purpose and `!stop` already answered them. `update_task_status` accepted a `result` on this branch and dropped it; it writes it now, beside the trace ISSUE-183 added
   - Check policy refusal (`_is_policy_refusal()`: 400 + safety/policy/content/refused/harm/blocked keyword) → mark failed, post alert via `_post_policy_refusal_alert()` (extracts `From:` header for email tasks), no retry
   - Check shutdown collateral (`_is_shutdown_collateral()`: `_shutdown_requested` **and** `is_signal_termination(result)`) → `db.release_task_for_restart` — back to `pending`, liveness cleared, **no attempt charged, no backoff**, deferred-op files purged. Not a task failure: under systemd's default `KillMode=control-group` a `systemctl restart` (the auto-update cron issues one per commit) SIGTERMs the whole cgroup, killing an in-flight task's `claude` child while the daemon shuts down gracefully — so the surviving worker recorded the corpse as a failure, permanently on a final attempt (ISSUE-191). The attempt is not charged because it was aborted by infrastructure, so a terminal attempt recovers too; `fail_ancient_pending_tasks` is the bound. The unit template now also sets `KillMode=mixed`, which converts the same event into the startup orphan-recovery path — this branch is the belt-and-braces (and the half that ships via auto-update, since unit files need an Ansible run). The terminal-events block mirrors the classification (`is_requeued`) and emits a "Scheduler restarting…" `progress_text` instead of a terminal frame.
   - Check permanent provider error (`is_api_error_banner` **and**
     `is_permanent_api_error`: a bad model id, an expired key, an oversized
     prompt) → mark failed, no retry. The ladder would fail identically on
     every rung, ~21 minutes later (ISSUE-212). Banner-gated for the same
     reason the masquerading-success guard is: an answer *discussing* a 400
     must not suppress a legitimate retry. The terminal-events block mirrors
     the classification (`is_permanent_api`) so no "retrying" notice is emitted
   - Retry with backoff if attempts remain (1, 4, 16 min) — skipped for OOM
   - Mark failed permanently, `result` again carrying `task.partial_result`. Both user-facing failure notices append it through `_with_partial_work` — the Talk one and the email-only alert, which is the branch that most needs it since its own comment says `tasks.result` is the only surviving copy there. Below the error, never above it: the first line still has to say the task failed. Not length-capped, because the Talk transport already splits a long body at 30,000 characters and that is what a long *successful* answer does. Only on a *terminal* failure: a task going back through the retry ladder has not finished, so it has no answer to record
   - Track scheduled job failures, auto-disable after threshold. All three disable sites (this one, the policy-refusal branch, and `_record_publish_failure`) buffer a `cron_job` notification into `notification_results` and let the post-`with` `deliver_pending` send it — the disable used to write a `task_logs` warning and tell nobody. All three call `db.suspend_scheduled_job`, which writes `auto_disabled_at` and never `enabled` — CRON.md authors that column and `_sync_cron_files` writes it back from the file every tick, so a daemon disable written there was undone within the tick and auto-disable did nothing at all for a file-defined job. `!cron disable` keeps `db.disable_scheduled_job` and the user's column, because it writes the file in the same breath. The resolver's close predicate is `auto_disabled_at IS NULL`
7. Deliver results (Talk/email) outside DB context

## Deferred DB Operations

After successful task completion (not confirmation, not failure), the scheduler processes deferred JSON files from the user temp dir:

**`_process_deferred_subtasks(config, task, user_temp_dir)`**:
- Reads `task_{id}_subtasks.json` — array of `{prompt, conversation_token?, priority?}`
- Admin-only: non-admin files are ignored and deleted
- Creates tasks via `db.create_task()` with `source_type="subtask"`, inherits `queue` from parent
- Deletes file after processing

**`_process_deferred_tracking(config, task, user_temp_dir)`**:
- Reads `task_{id}_tracked_transactions.json` — `{monarch_synced: [...], csv_imported: [...], monarch_recategorized: [...]}`
- Calls `db.track_monarch_transactions_batch()`, `db.track_csv_transactions_batch()`, `db.mark_monarch_transaction_recategorized()`
- Deletes file after processing

**`_process_deferred_sent_emails(config, task, user_temp_dir)`**:
- Reads `task_{id}_sent_emails.json` — array of `{message_id, to_addr, subject, thread_id, ...}`
- Records outbound emails in `sent_emails` table for emissary thread matching
- Enables reply routing: when external contacts reply, References headers match against sent_emails
- Deletes file after processing

**Other deferred handlers**:
- `_process_deferred_kg_ops` — `task_{id}_kg_ops.json` from `istota-skill memory_search add-fact|invalidate|delete-fact`. Commits per op so a mid-loop crash can't roll back ops we've already accepted.
- `_process_deferred_kv_ops` — `task_{id}_kv_ops.json` from `istota-skill kv set|delete|set-add|set-remove|set-trim`. The set ops re-read the current value before applying, so they compose across concurrent tasks; `set-trim` carries only `keep_newest` and is skipped (rather than creating the key) when the row is absent.
- `_process_deferred_user_alerts` — `task_{id}_user_alerts.json` for the alerts/notification path.
- `_process_deferred_health_ops` — `task_{id}_health_ops.json` from `istota-skill health log|add-panel|add-biomarker|upload|set|...`. Resolves the user's `HealthContext` and replays insert/update ops against the per-user `health.db`. User id always comes from the task (defense-in-depth); recognized suffix lives in `_KNOWN_DEFERRED_SUFFIXES` so unknown-suffix warnings stay clean. Two of its ops touch the filesystem: `attach_document` (health-document-attachments spec) copies a user-supplied file into the health document store and links it to an encounter / diagnosis / immunization, and `detach_document` removes one link. Because the op file is written *inside* the sandbox, `source_path` is attacker-influenced text while the replaying daemon is not sandboxed — `_resolved_source_path` therefore resolves symlinks, confines the result (`_source_path_allowed`) to the task's own deferred dir or the user's base workspace `{mount}/Users/{uid}`, and returns the approved path so the read uses *it* rather than re-resolving (which would reopen the swap window the check just closed). The base dir rather than the bot subdir, because the driving case is an email attachment the executor dropped in `inbox/`. `register_upload` now goes through the same guard — it had none, and the new op made the omission conspicuous. Both `attach_document` and the skill's direct path read the operator's `[health] max_document_bytes` (`_health_max_document_bytes`) instead of the library default, and validate that the target encounter / diagnosis / immunization exists before writing bytes (a link to a nonexistent row would be invisible *and* sweep-exempt). An `attach_document` may carry `encounter_ref` instead of `entity_id`, resolved against a separate `encounter_refs` dict populated by `insert_encounter`'s own `ref` (kept apart from the panel `refs` dict so a shared name can't cross-resolve); an unresolved ref raises rather than mis-filing paperwork, landing the op in `failures`. The op loop's except tuple gained `OSError` + `DocumentError` so one refused attachment (bad MIME, over cap) fails only its own op. That handler also now `conn.rollback()`s: the loop commits per op, so a failing op's partial work stayed open on the connection and the *next* op's commit swept it in — an `attach_document` that inserted its row and then failed to write the bytes would have silently persisted a row pointing at a file that never landed. Prior ops are already committed, so the rollback can only discard the failed one.
- `_process_deferred_garmin_import` — `task_{id}_garmin_import.json` from `istota-skill location import-garmin-tracks` when sandboxed. Runs `istota.location.garmin_import.import_tracks` in-process (the daemon holds `ISTOTA_SECRET_KEY`, stripped in the sandbox), gated on the location module, then pushes the result to the user via `send_notification(purpose="notification")`. This is the path that works without the framework DB at all: the deferred dir is writable from the sandbox, while the database directories are masked out of it.
- `_load_deferred_email_output` — `task_{id}_email_output.json` for structured email replies (preferred over the legacy stdout-JSON parser).

All deferred-op handlers now live in `scheduler_deferred.py` (`_KNOWN_DEFERRED_SUFFIXES`, `_load_deferred_json`, per-handler functions, `_purge_deferred_files_for_retry`, `_warn_unconsumed_deferred_files`). `scheduler.py` calls into it as a thin orchestrator.

**Retry replay safety (ISSUE-074)**: deferred-op producers append to per-task files keyed only by `task.id`, and every requeue keeps that id — so without a purge, eventual success replays the failed attempt's ops alongside the successful one's (matters most for non-idempotent KG `invalidate`/`delete`, duplicate subtasks, duplicate `sent_emails` rows). The purge is **claim-time**: `process_one_task` calls `_purge_deferred_files_for_retry()` right after the claim whenever `task.attempt_count > 0`, before execution. That covers all four requeue paths at once — its own retry branch, `db.fail_stuck_locked_running_tasks` (the periodic reclaim), `db.recover_orphaned_tasks` (startup recovery), and `db.claim_task`'s inline copy of the stuck-running release, which offers no scheduler-side hook to wire a purge into at all. The retry branch and the shutdown-collateral branch still purge at requeue too: the first to free disk for a task that never comes back, the second because `release_task_for_restart` charges no attempt, so a *first*-attempt shutdown leaves `attempt_count == 0` and the claim-time guard would not fire. **A confirmation re-run is exempt whatever the count says** (`task.confirmation_prompt is None` is part of the guard): `_drain_deferred_ops` is skipped when a task asks for confirmation, so ops written before the question sit on disk by design until the user answers, and `db.confirm_task` requeues without resetting `attempt_count` — so a task that failed once earlier would otherwise arrive carrying a charged attempt and have its held writes discarded. That path keeps relying on the narrower stale-`email_output` cleanup, which is what stops a double-send.

**Unconsumed-file warnings (ISSUE-073)**: after the drain phase, `_warn_unconsumed_deferred_files()` scans the user temp dir and logs WARN for files missing the `task_` prefix or carrying an unknown suffix. The misnamed file is left on disk for inspection.

**Why deferred**: With bubblewrap sandbox, DB is RO inside the sandbox. Claude and skill CLIs write JSON to the always-RW temp dir; the scheduler (unsandboxed) processes them.

**Encoding**: every deferred-file read and write names `encoding="utf-8"` explicitly, and `_load_deferred_json` catches `UnicodeDecodeError` alongside `JSONDecodeError`/`OSError`. The producer is a task subprocess whose env was rebuilt from scratch (`build_stripped_env`) while the consumer is the daemon under systemd's, so nothing guarantees the two agree on `locale.getencoding()`. The catch is the load-bearing half: `_drain_deferred_ops` calls its handlers in sequence with no guard between them, so a decode error escaping the first one silently skips the eight after it.

## WorkerPool
```python
class WorkerPool:
    def __init__(self, config: Config)
    def dispatch(self) -> None        # Admission gate, then two-phase: fg first (fg cap), then bg (bg cap)
    def update_pressure(self, sample: PressureSample | None) -> None  # main loop → gate
    def gate_closed_seconds(self) -> float                            # 0.0 when open
    def admission_open(self) -> bool  # pure predicate; what workers consult before claiming
    def _admission_open(self) -> bool # same answer + closed-since clock + cooldown log; dispatch only
    def _admission_decision(self) -> tuple[bool, PressureSample | None]
    def _retire_surplus_foreground_workers(self, fg_threads, discounts) -> None  # takes back a lapsed long loan
    def _on_worker_exit(self, user_id: str, queue_type: str, slot: int) -> None
    def shutdown(self) -> None         # request_stop + join(10s)
    @property active_count -> int
```
- Thread-safe via `threading.Lock` on `_workers` dict
- Workers keyed by `(user_id, queue_type, slot)` 3-tuple — allows multiple workers per user per queue
- Foreground cap: `max_foreground_workers` (default 5)
- Background cap: `max_background_workers` (default 3)
- Per-user caps: `effective_user_max_fg_workers(user_id)` / `effective_user_max_bg_workers(user_id)` (global default via `user_max_foreground_workers`/`user_max_background_workers`, overridable per user)
- Workers only spawned up to `min(per_user_cap, pending_task_count)` to avoid idle workers

### Long-task slot reclassification (foreground only)

There is a third foreground slot class. A **running** foreground task whose `started_at` is older than `long_task_threshold_minutes` (default 10) stops counting against the user's interactive cap and counts against a separate long allowance. The task is not touched: it keeps running, in place, on the same worker. Nothing is preempted, migrated or killed anywhere in this feature.

Two module-level pure functions carry the rule, so it can be read and tested without a pool around it:

```python
def plan_foreground_slots(*, threads, discounted, pending, user_fg_cap, user_max_long_workers) -> ForegroundSlotPlan
def allocate_long_discounts(long_by_user, *, priority, user_cap, instance_cap) -> dict[str, int]
```

Per tick, `dispatch` adds one grouped query to its existing pre-lock scan — `db.count_long_running_tasks_by_user(conn, "foreground", threshold)` — then allocates discounts once and plans each user's slots from them:

```
discounted  = allocate_long_discounts(...)[user]      # <= user_max_long_workers, <= max_long_workers instance-wide
interactive = max(0, threads - discounted)
may_spawn   = min(user_fg_cap - interactive,
                  (user_fg_cap + user_max_long_workers) - threads,
                  claimable_pending)
slot index drawn from range(user_fg_cap + user_max_long_workers)
```

- **Reactive, not predictive.** Nothing at enqueue time separates "flex the developer skill on a worktree" from "what time is my meeting" — the task behind the 2026-08-20 head-of-line block arrived as an ordinary chat message. Elapsed time is the only signal that is always right. No completed foreground task crossed ten minutes in the seven days to 2026-08-20, so the default cannot misfire on an ordinary turn.
- **`max_long_workers` bounds discounts, not long tasks.** A task becomes long while it is already running, so the cap cannot refuse it retroactively; long tasks beyond the cap keep counting as ordinary interactive occupancy. Read the other way it would either require killing work or would let the allowance grow without bound as more tasks aged past the threshold.
- **Per user additive, instance-wide partitioned.** A user's thread ceiling becomes `user_max_foreground_workers + user_max_long_workers` (2 + 1 = 3), so one person's long job cannot consume their own interactivity. `max_foreground_workers` stays the *hard* ceiling on total foreground threads, with at most `max_long_workers` of them discounted — the box's worst-case memory exposure is the subject of this whole feature and does not grow to buy fairness. The spec's Track C text also said instance-wide interactive spawns should gate on `total_fg_threads - total_discounted < max_foreground_workers`, which would raise the ceiling from 5 to 7; that is the "both additive" option its own rejected-alternatives entry turns down, and the hard ceiling is what shipped.
- **The instance budget is allocated over every user holding a long task**, not only those with pending work, ordered `fg_users` (longest-waiting) first and the rest sorted. A user with an extra thread and an empty queue never reaches dispatch's loop but still occupies the exposure the budget bounds.
- **`max(0, threads - discounted)` is required, not defensive.** The counts are read before `_lock` is taken, so a task can complete in between and leave the discount momentarily larger than the thread count.
- **NULL `started_at` counts as short** (the conservative reading: it keeps the interactive cap tight rather than loose), and pending tasks never count however long they have waited — they hold no worker to discount.
- **Only a user already at their interactive cap is eligible for a discount.** `dispatch` filters `fg_long` on `threads >= effective_user_max_fg_workers` before allocating. Anyone below the cap spawns on the ordinary cap regardless, so a discount buys them nothing — and charging them the budget is how the user this feature exists for gets refused. `fg_users` is oldest-pending-first and does not correlate with "at cap", so without the filter two users with a free slot each can take the whole budget and leave the genuinely blocked user with `may_spawn = 0`.
- **The extra thread is a loan, and `_retire_surplus_foreground_workers` takes it back.** `dispatch` only ever adds threads, and a `UserWorker` re-claims on its own without rechecking its slot, so once a long task ended while its user still had backlog the borrowed thread went on serving ordinary interactive work — "2 interactive + 1 long" silently becoming "3 interactive" for as long as the queue stayed non-empty. Each tick, any user whose *interactive* count now exceeds their cap has the highest-index surplus workers asked to stop. `request_stop` is graceful (`UserWorker.run` checks the event at the top of its loop), so a worker mid-task finishes that task and exits — nothing is preempted, the same rule the long task itself is held to. The sweep also retires a worker stranded at an index outside the range by a cap lowered under a live pool, which was a pre-existing gap.
- **The discount is derived from DB rows, not from live threads.** `count_long_running_tasks_by_user` counts `running` rows, and a stale one — worker died, the liveness reclaim has not run yet, or a second process is draining the queue with no pool at all — spends a user's allowance with no thread behind it, giving that user one extra thread on ordinary interactive work. Clamping the discount to the thread count looks like the fix and is arithmetically inert: it changes `interactive` in no case the existing `max(0, …)` does not already cover, and it cannot tell a ghost row from a real one. The over-grant is bounded instead by the two ceilings that do hold unconditionally — the additive per-user ceiling and the hard `max_foreground_workers`, both counted off `_workers` — and clears itself when the reclaim clears the row.
- **A follow-up in the *same conversation* is not unblocked, by design.** `fg_pending` comes from `count_claimable_tasks_for_user_queue`, which applies `_CLAIM_CHANNEL_GATE_SQL` on the foreground queue, so a pending task in the same room as a running one counts as 0 and `may_spawn` collapses to 0 with it. Freeing a slot cannot help when nothing may claim it. That gate is deliberate and older than this feature — it is what answers "Still working on a previous request". What the allowance fixes is a long job in one room blocking a question in *another*, which is the case where a slot really was the binding constraint.
- **Off switch.** Any of `long_task_threshold_minutes`, `user_max_long_workers` or `max_long_workers` at 0 restores the pre-change behaviour exactly, and all three skip the extra query rather than running it and discarding the result. Background dispatch is untouched in every configuration: scheduled work has no head-of-line question to unblock.
- **The long allowance is global-only.** `user_max_foreground_workers` has a per-user override (`effective_user_max_fg_workers`, backed by `user_profiles.max_foreground_workers`); `user_max_long_workers` has none, so a user with a raised foreground cap gets `their_cap + 1` and still exactly one discount. The mixed scope is deliberate — the allowance exists to unblock a queue, not to scale with a user's cap — but note `allocate_long_discounts` is passed the global for every user, so adding an override later means changing the call site as well as the config.
- Index: `idx_tasks_queue_started ON tasks(queue, status, started_at, user_id)` is *covering* — the scan is answered without touching the table. It does not order the `GROUP BY`, which still builds a temp b-tree over one row per long task.

### Memory admission gate

Ahead of all three caps sits `_admission_open()`. Below `min_available_memory_mb` of `MemAvailable`, or above `host_pressure_psi_threshold` on `memory some avg10`, the tick spawns nothing and returns before the DB scan — a squeezed host does not even pay for the read.

**The gate covers every claim, not just the spawn — and it did not always.** Gating `dispatch()` alone bounds new worker *threads*, which is not the same as bounding new task *starts*: a `UserWorker` already alive loops straight back into `process_one_task` and, once parked, claims again from inside `_worker_idle_wait`, consulting nothing. On a squeezed host that kept work starting in whatever slots already existed, while this document and the spec both said the task would stay `pending`. The incident's own trigger was a single task running a test suite, so one claim into a lingering worker is the whole failure — bounding concurrency growth was never sufficient on its own. Three call sites now consult it: `dispatch()` via `_admission_open()`, and both worker claim paths via `pool.admission_open()`.

**Scoped to the daemon.** The single-pass drain loops behind `istota run` (`cli.py:227`, `scheduler.py:5649`) call `process_one_task` directly with no pool and are deliberately ungated: an operator running a foreground command on a squeezed box should get their task, not a silent no-op, and they get one task at a time by construction.

- **Two entry points, deliberately.** `admission_open()` is the pure predicate — no closed-since stamp, no log line — because workers poll it on the idle cadence, several times a second each, and a version that recorded would flood the log and keep re-arming state belonging to dispatch's once-per-tick view. `_admission_open()` is that same answer plus the bookkeeping, and **`dispatch()` is its only caller**; everything else (workers, and the operator-alert wording in `_emit_host_pressure_snapshot`) takes the pure one. That matters in the alert's case specifically: `_admission_open()` stamps `_gate_closed_since` when it is `None`, so asking it there would reset the very clock `gate_closed_seconds()` is read from a few lines later and suppress the escalation. Both delegate to `_admission_decision()`, which returns `(open, sample)` so the dispatch side can log what it decided on.
- **A closed gate does not park a worker forever.** On the fine-cadence path `_worker_idle_wait` keeps the *same* deadline while refusing, so a parked worker ages out at `worker_idle_timeout` and exits, and `dispatch()` declines to respawn it. Under sustained pressure the pool drains to zero rather than holding idle threads against a host with no room for them. On the legacy coarse-wait branch (`worker_idle_poll_interval` ≤ 0 or ≥ `worker_idle_timeout`) there is no polling to continue, so a closed gate returns `None` and the worker exits after its single recheck — the same exit that branch already takes on an empty queue. The gate is checked ahead of `pending_count()` so a closed gate costs nothing at all, not even the indexed read.
- **The drain changed what `workers_active` means, and two things had to follow.** A shut gate empties the pool within `worker_idle_timeout`, so a squeeze with a full backlog reports `workers_active=0` and `tasks_running=0` — indistinguishable from an idle night. `scheduler_stats` therefore carries `admission_closed_s` (0 when open), and the operator alert's "istota is a bystander" escalation keys on the *claimable backlog* (`_claimable_backlog()`) rather than on worker count. Keying it on workers would have printed "something else on the host is holding the memory" during exactly the 2026-08-20 shape, where istota's own task left the residue behind.
- **Admission only.** Nothing running is stopped, preempted, migrated or failed. A held task stays `pending` and is picked up on a later tick, exactly as when a cap is full — and that is now true of a lingering worker as well, which is what makes the sentence honest.
- **Fails open in every uncertain case**: `host_pressure_enabled = false`, no sample yet, an unreadable sample, or both thresholds set to zero. `update_pressure(None)` *clears* rather than latching, so a sampler that starts failing cannot leave the gate shut on evidence nobody is refreshing. A sampler defect silently halting all dispatch would present as an unexplained total outage — the exact failure this instrumentation exists to explain rather than cause.
- **A large `shmem_unaccounted` residue does not close the gate.** It fires the *snapshot* (`snapshot_trigger`), not the hold. Memory that swap is absorbing is not memory the next task cannot have; on 2026-08-21 zram took a 1.52 GB shmem burst with PSI at 0.07 and 2.9 GB still available, and refusing work there would have turned a mitigated event into a self-inflicted queue stall.
- One `dispatch_admission_closed` WARNING per `host_pressure_alert_cooldown_seconds`, re-armed when the gate reopens, so a 40-minute squeeze produces a handful of lines rather than thousands.
- `gate_closed_seconds()` past a cooldown window with `active_count == 0` escalates the operator alert's wording: the memory is being held by something else on the box and istota is queuing behind it, not causing it.

### Per-task cgroups (`task_cgroup.py`)

The gate above decides what may *start*. This bounds what a task that did start can do, and it is the half that addresses the incident directly: its trigger was one task's test suite, admitted onto a host that had room for it at the time. `MemoryHigh=` and `CPUWeight=` bound the daemon's cgroup as a whole; nothing bounded a single task's process tree, because bwrap gives filesystem and network isolation and no resource isolation at all.

`execute_task` creates `<unit cgroup>/task-<id>-<attempt>/` with `memory.max`, `pids.max` and `cpu.max` before the brain runs, and hangs the release off the same `ExitStack` that owns the skill and network proxies, so success, failure, timeout, cancellation and a fallback brain replacing the primary all give the directory back; a raise *before* the stack is entered is caught by the function's own handler, which releases it too. `run_daemon` sweeps `task-*` directories left by a previous run — a daemon killed by the OOM killer does not run its own cleanup, which is precisely the event here.

- **The attempt is in the name because a task id is not unique over time.** A retry reuses the row, so `task-<id>` alone would put attempt 2 in the directory attempt 1 left behind — and attempt 1's directory survives exactly when its tree escaped the kill, so the retry would share its budget with the runaway that caused it and be OOM-killed on the spot for a fault that is not its own.
- **`destroy` kills what it finds.** `EBUSY` means the task's own descendants outlived the brain — the ISSUE-257 shape, a `pytest -n auto` whose workers survived the kill aimed at their parent. Leaving them is not defensible: the task is over, they hold a budget nobody is watching, and `rmdir` will keep returning `EBUSY` for as long as they live, so the next startup sweep cannot reclaim it either. The `EBUSY` path writes `cgroup.kill` (cgroup v2, Linux 5.14+), which kills every member *including descendants that escaped their process group* — the one guarantee `killpg` cannot make. `cgroup.kill` is asynchronous, so `destroy` polls `rmdir` for half a second (10 × 50 ms) before giving up rather than trying once — that was tolerable while the cgroup held a single `bwrap` that died with the task, and became the ordinary teardown path the moment placement started working. A cgroup that still cannot be emptied is logged at warning, and the startup sweep reports survivors separately from removals: counting only removals reports a live runaway identically to a clean start.
- **Both limits that can end a task are named.** `memory.events` and `pids.events` are read before `rmdir` takes the counters with it; a non-zero `oom_kill` or `pids.events`'s `max` is logged against the task and the config key that set the cap. The feature is on by default, so its first visible effect on a real host is builds and suites that used to pass starting to fail; without this they surface as an opaque killed child, or as `fork: Resource temporarily unavailable`, with nothing anywhere mentioning a limit. Neither counter had ever moved before ISSUE-285 was fixed — the cgroup held one sleeping process, so nothing in it ever reached a limit. **The three defaults (2048 MB / 512 pids / 200% CPU) have correspondingly never been exercised against a real task tree**, and cgroup v2's pids controller counts threads rather than processes, so `pids.max` is the one most likely to bind unexpectedly; the new log lines are what will say so.
- **Swap is deliberately not bounded.** `memory.max` covers anon + page cache; `memory.swap.max` is a separate knob left at `max`, so a tree past its limit is reclaimed into zram and only OOM-killed once reclaim stops making progress. That is Track A working as intended — the same absorption that took the 1.44 GB burst on 2026-08-21 — rather than an oversight. Revisit if a contained task is seen driving host swap I/O instead of dying.

- **The directory is a sibling of the daemon's own cgroup, not a child of it.** cgroup v2 forbids a non-root cgroup from both holding processes and enabling controllers for its children (`EBUSY` on the `cgroup.subtree_control` write), so a `task-<id>/` made inside the cgroup the daemon sits in is created successfully and then contains no `memory.max` at all. `DelegateSubgroup=supervisor` puts the daemon in a leaf and leaves the *unit* cgroup free to enable controllers; `resolve_root` takes the **last** `.service`/`.scope` component of `/proc/self/cgroup` to find it. Last, not first: under a systemd *user* manager the first match is `user@1000.service`, whose subtree is also delegated and writable — so `mkdir` and the `memory.max` write both succeed there and the kernel-as-probe rule returns a false positive, while task cgroups land beside every other unit that user runs and the sweep deletes `task-*` out of a directory it does not own.
- **The kernel is the probe.** On a real cgroup2fs the interface files are kernel-made and a writer cannot create them, so a `memory.max` write that succeeds proves the memory controller is delegated here and one that fails proves it is not. There is no separate capability check to drift out of date. A failed `memory.max` write removes the directory again rather than leaving an empty cgroup that reads as containment in `systemd-cgls`; a failed `pids.max` or `cpu.max` keeps it, because memory-only containment is most of the value.
- **Placement happens in the child, before it execs.** Every brain that spawns its own subprocess gets the path on `BrainRequest.task_cgroup` and passes `task_cgroup.placement(...)` as `preexec_fn`: the parent opens `cgroup.procs`, the child writes `0` to it between `fork` and `exec`, and everything it goes on to fork inherits the group. Doing it the other way round is ISSUE-285 — membership is inherited *at* `fork`, so moving a pid afterwards leaves the children it already has where they were, permanently, and `bwrap` forks its inner process during namespace setup every single time. The observed shape was a task cgroup holding one sleeping `bwrap` at `memory.current=0` eighteen minutes in, with the CLI, its bash grandchildren and a `pytest -n auto` running unbounded in the daemon's own leaf. The old comment calling that window short was reasoning about a `bash -c` that has yet to exec, and there is no such window: the escape is permanent, not transient.
- **`preexec_fn` in a threaded daemon, made safe by shape.** Only one thread survives the fork and any lock the others held is held forever, which is why the open is in the parent and the child does one `os.write` of two bytes to an inherited descriptor — no allocation on the success path, no import, no logging. `0` means "the writing process", so the child needs to know nothing about its own pid. CPython runs `preexec_fn` before closing inherited descriptors, so the fd is still open. `clone3(CLONE_INTO_CGROUP)` is the kernel-native form and `subprocess` does not expose it.
- **The child cannot report a failure, so the parent reads it back.** `preexec_fn` swallows its own errors (an exception there makes `Popen` raise, and losing containment must not cost the task its subprocess), so each spawn calls `verify_placement(pid, path)` afterwards, which checks real membership and warns once when it is missing. Nothing is retried on a miss: moving the pid at that point is the exact write ISSUE-285 was about.
- **`place()` survives for one caller.** TmuxClaudeBrain reports a pane pid the tmux server spawned, so there is no `preexec_fn` to reach; the executor's `on_pid` callback places it after the fact. Containing the group leader is worth more than containing nothing, and it is all that path can do. For the other brains `on_pid` still fires and the write is a no-op, because the pid is already a member.
- **Fails open, never fails silent.** No `Delegate=`, an older systemd that ignored `DelegateSubgroup=`, cgroup v1, macOS: `create` returns `None`, the task spawns exactly as before, and the reason is logged once per process (keyed, so a different cause is still heard) rather than once per task. The `STARTUP Per-task cgroups:` line reports which of those happened up front, and it is a **probe** rather than a lookup — a real `mkdir` + `memory.max` write + `cgroup.procs` open + `rmdir` under `task-probe`. Delegation alone was still the wrong question after ISSUE-285, where every one of those writes succeeded on a host whose tasks all ran uncontained, so the probe also opens the scratch group's `cgroup.procs` for writing — the one part of placement that varies by deployment. It deliberately stops there: that a forked descendant inherits the group is a kernel invariant rather than a deployment fact, costs a subprocess spawn to observe, and is held down by `tests/test_task_cgroup_placement.py` instead. Resolving the root is not evidence: it succeeds on every systemd host whether or not `Delegate=` was ever applied, so the first cut of that line printed the affirmative case on exactly the deployments where every `create` would go on to fail. A report that cannot fail says nothing, and the negative case is a warning, not an info.
- `task_cgroup_enabled = false` disables it outright. `task_cpu_max_percent = 0` writes no `cpu.max` at all rather than writing `max`, so an operator reading the tree sees the knob they set.

## UserWorker
```python
class UserWorker(threading.Thread):
    def __init__(self, user_id: str, config: Config, pool: WorkerPool, queue_type: str, slot: int)
    def run(self) -> None
    def request_stop(self) -> None
```
- Loops calling `process_one_task(config, user_id=user_id)`, **each claim gated on `pool.admission_open()`** — the fast path at the top of `run()` and the poll inside `_worker_idle_wait` both. A worker is a long-lived claimer, not a one-shot, so the memory gate has to live here as well as on the spawn; see "Memory admission gate" above for why gating `dispatch()` alone was not enough. A shut gate skips the claim and falls through to the idle wait, which keeps its existing deadline, so the worker ages out normally and the pool drains under sustained pressure.
- When the queue empties, the worker lingers in `_worker_idle_wait` instead of exiting at once: it re-checks for new work every `worker_idle_poll_interval` (default 0.5s) until `worker_idle_timeout` (default 10s) of *continuous* emptiness elapses, then exits. A follow-up task arriving mid-linger is claimed within ~one idle poll (the "quick follow-up" case `dispatch()` alone can't help, because the parked worker still holds the per-user slot). A cheap `count_claimable_tasks_for_user_queue` pre-check gates the expensive `claim_task` so idle polling costs no more than a `dispatch()` scan. The deadline tracks continuous emptiness — a lost claim race does not reset it, so two idle workers can't keep each other alive forever. `worker_idle_poll_interval` ≤ 0 or ≥ `worker_idle_timeout` restores the legacy single coarse-wait + single-recheck behaviour (an interruptible `stop_event.wait`, exact pre-phase-2 parity). The fine-cadence path mirrors `_dispatch_sleep` (slice-sleep + stop/shutdown checks), so per-worker stop and global shutdown are honoured within one idle poll.
- Both the idle pre-check and `dispatch()`'s spawn count use `db.count_claimable_tasks_for_user_queue` (not the raw `count_pending_tasks_for_user_queue`), which mirrors `claim_task`'s per-channel single-active gate via the shared `_CLAIM_CHANNEL_GATE_SQL`. A follow-up gated behind an active task in the *same* room therefore counts as 0 — `dispatch()` won't spawn a doomed extra worker for it and an idle worker keeps sleeping cheaply — while a task in a *different* room still counts. Raw `count_pending_*` survives only for status/observability (the daemon's pending-backlog status file).
- During the linger both this worker and `dispatch()` may scan the same user; the overlap is harmless — `claim_task` is atomic (`UPDATE … RETURNING`), so at most one wins.
- After exit, `dispatch()` re-spawns a worker on the next pending task (phase-1 sub-tick cadence, ~0.5s).
- Each worker creates fresh DB connections and `asyncio.run()` event loops

## Poller Integrations

| Poller | Function | Interval Config | State Table |
|---|---|---|---|
| Talk | `_talk_poll_loop()` | `talk_poll_interval` | `talk_poll_state` |
| Email | `poll_emails()` (transport/email/inbound.py), via off-thread `_run_email_poll` | `email_poll_interval` (batch `email_poll_batch_size`) | `processed_emails`, `email_poll_state` |
| TASKS.md | `poll_all_tasks_files()` (tasks_file_poller.py) | `tasks_file_poll_interval` | `istota_file_tasks` |
| Heartbeat | `check_heartbeats()` (heartbeat.py), via off-thread `_run_heartbeat_checks` | `heartbeat_check_interval` | `heartbeat_state` |
| DB health | `check_db_health()` → `db_health.check_and_repair()` | `db_health_check_interval` | — (logs only) |
| DB backup | `db_backup.backup_databases()` (SQLite online-backup → `{mount}/istota-db-backups/<date>`; retention prune + collapse guard + `_alert_backup_problems` + staleness alert) | `db_backup_interval` | — (logs + operator alerts) |
| Scheduler stats | `_emit_scheduler_stats()` (daemon-only) | `scheduler_stats_interval` | — (logs only) |
| Host pressure | `_emit_host_pressure_breadcrumb()` → `host_pressure.read_sample` + `read_tmpfs_usage` + `read_memory_events` + `breadcrumb` (daemon-only) | `host_pressure_breadcrumb_interval_seconds` | — (logs only) |
| Pressure check | `_check_host_pressure()` → `pool.update_pressure` + `snapshot_trigger` → `host_pressure_snapshot` + operator alert | `host_pressure_sample_interval_seconds` | — (logs + operator alerts) |
| Shared files | `discover_and_organize_shared_files()` (shared_file_organizer.py) | `shared_file_check_interval` | `user_resources` |
| Skill overlays | `check_skill_overlay_reindex()` → `memory/search.reindex_skill_overlays` per configured user, off-thread via `_spawn_background_check` | `skill_overlay_reindex_interval` | — (`memory_chunks`, `source_type='skill_overlay'`) |
| Runtime self-check | `check_doctor()` → `doctor.run_checks`, off-thread (`runtime.model_cli` and the forge checks each spawn a `--version`) | `doctor_check_interval` (3600s) | — (logs + operator alert) |
| Worktree reap | `check_worktree_reap()` → `worktree_reaper`, off-thread (fetches each bare clone, walks each candidate checkout). Also gated on `developer.enabled`, `developer.repos_dir`, `developer.worktree_reap_enabled` | `worktree_reap_interval` (21600s) | — (logs only) |
| Package caches | `check_sandbox_cache_sweep()` → `sandbox_cache_sweeper`, off-thread (walks every cache tree, shells to `uv` / `npm`). Also gated on `security.sandbox_cache_sweep_enabled` and on `sandbox_cache_sweep_root(config)` resolving; passes in every user with a `locked` or `running` task so a live writer's cache is skipped whole | `sandbox_cache_sweep_interval` (21600s) | — (logs only) |
| Avatar import | `check_avatar_import()` → `nextcloud/avatars.fetch_avatar` per configured user, off-thread (one HTTP request each). Also gated on `config.storage_is_nextcloud` and `web.avatar_import_from_nextcloud`. The user set is **`config.users`**, never the `user_avatars` table — a user with no row is exactly the one needing a first import — and no transaction is held across a fetch: fetch, one short write, next user. Only a *custom* Nextcloud avatar is stored; a generated one writes a NULL-image probe row carrying the ETag, so the next tick revalidates instead of re-downloading a placeholder | `avatar_import_interval` (21600s) | `user_avatars`; `shared_kv` (`_avatar_import`/`last_tick`, what `doctor`'s socket-free `web.avatar_import` reads) |
| Briefings | `check_briefings()` | `briefing_check_interval` | `briefing_state` |
| Scheduled jobs | `check_scheduled_jobs()` | `briefing_check_interval` | `scheduled_jobs` |
| Sleep cycle | `check_sleep_cycles()` (memory/sleep_cycle.py), via off-thread `_run_sleep_cycles` | `briefing_check_interval` (poll cadence; own cron gates the work) | `sleep_cycle_state` |
| Travel timezone | `check_travel_timezone()` — writes `user_profiles.timezone` for opted-in users; detection in `location/timezone.py` | `TRAVEL_TZ_CHECK_INTERVAL` (900s, gated on `location.enabled`) | `istota_kv` (`location`/`auto_timezone`, the 24h re-write cooldown) |
| Channel sleep | `check_channel_sleep_cycles()` (memory/sleep_cycle.py), same off-thread unit | `briefing_check_interval` (poll cadence) | `channel_sleep_cycle_state` |

When `playbooks.enabled`, `process_user_sleep_cycle` also distills **learned playbooks** (Part B): the extraction prompt gains a `PLAYBOOKS:` section (gated on `playbooks.min_tool_calls` tool calls + success + reusability). The section instructs the model to copy commands/paths *verbatim from the per-task `Tools (N):` line* rather than reconstruct a plausible path, and to write a **thin router** (trigger + exact verified command + one gotcha) instead of re-narrating a single-script trajectory's internals (ISSUE-174 Concern 1/4). `_parse_structured_extraction` returns a 4th `playbooks` list, and `_process_extracted_playbooks` writes each to `{workspace}/Users/<uid>/<bot_dir>/playbooks/<slug>.md` (dedup by slug → update in place), indexing it into `memory_chunks` with `source_type="playbook"`. A file marked `pinned: true` in frontmatter (a human correction) keeps its on-disk content but is **re-indexed from that content** — recall serves `memory_chunks`, not the file, so the correction must reach the index without the sleep cycle clobbering the human edit (`_playbook_is_pinned`; Concern 1 correction path). `cleanup_old_playbooks(conn=…)` age-prunes by `playbooks.retention_days` (default 90; 0 = keep) and **deletes the pruned file's `memory_chunks` too** (`playbook` isn't in `EPHEMERAL_SOURCE_TYPES`, so an unlinked-but-indexed file would otherwise still be recalled). Age is **last-use mtime**: `_recall_playbooks` stamps the file's mtime on every recall hit (Concern 3), so the prune targets idle guidance, not merely un-re-derived guidance; a **grandfather one-shot** (`.retention_initialized` sentinel) refreshes existing files on the first post-upgrade run so nothing is pruned on stale write-mtime, and a `pinned` file is never pruned. Markdown-only, never executed; recalled via `executor._recall_playbooks`.

## Cleanup (`run_cleanup_checks`)
1. Expire stale confirmations → notify the user through their `alert` route (ISSUE-241; it used to post to the task's `conversation_token` verbatim, which for an email gate is a synthetic thread hash naming no room). The notice names the sender and subject, and is sent **after** the cleanup transaction closes — an alert routed to `web` opens a second connection to the same DB and would block on the write lock `expire_stale_confirmations` takes.
2. Log warnings for stale pending tasks
3. Fail ancient pending tasks → notify user **only for user-submitted source types**. The notice ("A task you submitted was cancelled…") is suppressed for `_AUTOMATED_SOURCE_TYPES` (`scheduled`/`briefing`/`heartbeat`/`subtask`): those pile up on their own when the queue wedges, so notifying their output channel turns one stuck worker into a per-minute "task cancelled" flood (and the message isn't true for them).
4. Clean old completed tasks (`task_retention_days`)
4a. Prune the `processed_emails` dedup ledger (`_effective_processed_email_retention`)
4b. Prune `task_usage` / `task_usage_models` (`usage_retention_days`), **in a transaction of its own** after the main cleanup block closes. Steps 1–4a are one long write transaction and this window is 180 days against the task table's 7, so the delete it issues on the day it first bites is far larger than anything above it — holding the write lock for it would stall the dispatch loop's readers on their busy timeout. Failure is swallowed (telemetry retention never fails a cleanup pass) but logged at WARNING, not debug: this is the only thing bounding a table that gains a row per brain attempt, and a prune failing silently for weeks looks exactly like one that is working
5. Clean old emails from IMAP (`email_retention_days`), via one server-side `BEFORE` search
6. Clean old temp files (`temp_file_retention_days`)

## Memory Search Integration
After task completion, if enabled + `auto_index_conversations`:
- Index under `user_id`
- Also index under `channel:{conversation_token}` if in channel

## Config Intervals (SchedulerConfig defaults)

| Param | Default | Used By |
|---|---|---|
| `poll_interval` | 2s | Main loop sleep, worker poll |
| `dispatch_interval` | 0.5s | Sub-tick `pool.dispatch()` cadence inside `_dispatch_sleep` — bounds cold pending-task pickup latency without re-running the interval-gated checks. 0 or ≥ `poll_interval` = legacy one-dispatch-per-tick |
| `talk_poll_interval` | 10s | Talk poller |
| `talk_poll_timeout` | 30s | Talk long-poll, and the *server-side* hold only. It is sent to Nextcloud as the `timeout` query parameter, so an answer to it cannot arrive before it elapses; the `asyncio.wait` gate over the whole cycle is therefore `talk_poll_timeout + talk_poll_wait`, never the timeout alone. Equal to it, the gate expired exactly as every room was about to reply and `results = [t.result() for t in done]` dropped all of them — invisible at 30s where the skew is a fraction of the window, the whole of it at the `1` a deployment set to stop holding a PHP worker per room. Refused at 0 or below (`_positive_int`), which used to make the gate return before any round trip could finish and turn Talk inbound into a silent no-op (ISSUE-399) |
| `talk_poll_full_sweep_interval` | 300s | How often every room is long-polled regardless of the `lastMessage` gate. Between sweeps `poll_talk_conversations` skips a room whose `lastMessage.id` in the `/api/v4/room` listing is not newer than its `talk_poll_state` cursor, so a quiet cycle costs one short request rather than one held connection per room — 97% of a production day's 95,703 Talk polls were 499s, opened and abandoned having carried nothing. The gate **fails toward fetching** (a missing or unfamiliar `lastMessage`, or a room with no cursor yet, is polled): a needless poll costs the request the gate exists to save, while a wrongly skipped room loses its message until the next sweep. The sweep is what bounds that, so the two ship together. `0` makes every cycle a sweep, i.e. the ungated behaviour, which is the escape hatch instead of a second boolean. **Gating forces the room list to be refetched every cycle** — the gate reads `lastMessage` out of that payload, and `_CONVERSATION_CACHE_TTL` is 60s, so a cached list would hold a real message for up to a minute; `_conversation_cache` degrades to the failure fallback it still needs to be. **A stale list ungates the cycle rather than gating it**, which is the one case where the gate would otherwise be worse than what it replaced: the fallback list's `lastMessage` is frozen at the last successful fetch while the cursors keep advancing, so once a cursor reaches that frozen id the room gates shut on *every* cycle for as long as the listing keeps failing — a permanent hold, where the same outage previously cost nothing. `_last_full_sweep` is also stamped after the room loop rather than beside the decision, so a cycle that died early does not spend the sweep credit, and `archive_orphaned_talk_rooms` runs only on a sweep — gating made cycles come round ~4x more often, and that is a mass-archive whose only guard is a non-empty token set (ISSUE-399) |
| `email_poll_interval` | 60s | Email poller |
| `email_poll_batch_size` | 50 | Messages one inbound poll tick walks (ISSUE-250). A **batch boundary, not a window**: the poll used to fetch the newest 50 in the folder and dedupe afterwards, so anything that dropped below the top 50 between two ticks was never fetched again — silent, permanent mail loss at roughly 50 messages per interval, which a mailing list or a CI storm reaches without malice. Now each tick takes the oldest N UIDs above the `email_poll_state` cursor and leaves the rest, so a backlog drains in arrival order and a full batch logs that more is waiting. `processed_emails` stays the authority on what has been handled; the cursor only says where to start looking, so a lagging or lost cursor costs a re-fetch, never a duplicate task |
| `email_rate_limit_messages` / `email_sender_rate_limit_messages` / `email_rate_limit_window_seconds` | 60 / 20 / 3600 | The inbound volume budget (ISSUE-250 consequence 1). `bot+{user_id}@` is public by construction, so every past correspondent holds an address that turns one SMTP transaction into a paid model invocation on that user's account; nothing bounded how many. Both counts are checked in `poll_emails` **before** `ingest_message`, after owner resolution and after the quiet-sender filter — quiet mail costs nothing and must not spend the allowance, and an unrouted message has no budget to charge. Per-user reads recent `tasks` (`db.count_recent_email_tasks`, the email twin of `count_recent_web_tasks`); per-sender reads recent `processed_emails` rows that produced a task (`db.count_recent_email_tasks_from_sender`), compared on the addr-spec so a varying display name cannot evade it, and excluding taskless rows so throttling is not self-sustaining. **Over-budget mail is filed, not dropped**: a `throttled` ledger row, no task, the message left in the folder for `email from-senders`. Deliberately *not* pushed into `ingest.py` where the entry suggested — the shared ingest path has no ledger and no mailbox, so a limiter there could only drop. One alert per user per window (`_ThrottleNotice` + `_throttle_alerted`, same in-process shape as the DMARC dedup), delivered in the `finally` beside the prompts. 0 on either count disables that half |
| `email_task_queue` | `background` | Which queue inbound mail lands on, threaded through `IncomingMessage.queue` → `record_inbound` → `create_task`. Email is the only surface an unauthenticated stranger can create work on and the one with the loosest latency contract (a 60s poll interval already), so a flood must not hold foreground slots against a live Talk or web turn. Side effect worth knowing, in both directions: `_CLAIM_CHANNEL_GATE_SQL` is foreground-only *and* its inner `EXISTS` filters on foreground too, so an email task neither takes the per-channel gate nor blocks a foreground turn that would. That removes the case where one unanswered confirmation wedged its thread for the full `confirmation_timeout_minutes`, and it also means an email turn and a live Talk/web turn in the *same room* can run concurrently — the per-user background worker cap (1) serializes email against email and says nothing about the other queue. Making the gate cross-queue would fix that and is deliberately not done here: the gate is shared by every background task carrying a room token (briefings, subtasks), so a briefing running in a room would start blocking foreground turns there, which is a wider concurrency change than this one. Priority ordering does hold: briefings and cron rows are `priority=8` against email's 5, and `claim_task` orders `priority DESC`, so mail queues behind scheduled work rather than ahead of it. `foreground` restores the old behaviour |
| `email_max_body_chars` | 32000 | The body is interpolated whole into the prompt, so one large message is its own amplification with no flood needed. Truncated with a marker (`_truncate_body`) before either prompt template — the model must not answer a cut message as though it had all of it |
| `email_max_attachment_bytes` / `email_max_attachment_bytes_per_poll` | 26214400 / 104857600 | Attachment bytes per message and across one poll tick. `download_attachments(max_total_bytes=…)` skips whole attachments past the budget rather than truncating one. The per-poll half is the load-bearing one: a per-message cap bounds one sender's message and not a batch of fifty, and this poll runs on a thread the next tick waits on |
| `briefing_check_interval` | 60s | Briefings, jobs, sleep, cleanup, invoices |
| `tasks_file_poll_interval` | 30s | TASKS.md poller |
| `shared_file_check_interval` | 120s | Shared file organizer |
| `heartbeat_check_interval` | 60s | Heartbeat checks |
| `db_health_check_interval` | 86400s (24h) | SQLite `quick_check` + `REINDEX` self-heal across framework + per-user feeds/health/location/money DBs. Backstop now that module DBs are local (was for FUSE-mount corruption); enumerates via `Config.module_db_path`, no longer mount-gated |
| `main_loop_read_timeout_ms` | 2000ms | `busy_timeout` for the dispatch scan + idle pre-check (read-only). A lock past this raises `OperationalError` → the loop skips the tick (re-dispatches ~0.5s later) instead of blocking 30s and tripping the stall watchdog. Passed as `db.get_db(..., busy_timeout_ms=)`. 0 = keep the 30s connect timeout. Defense-in-depth on top of WAL |
| `db_backup_enabled` / `db_backup_interval` / `db_backup_dir` / `db_backup_retention` | true / 86400s / "" / 7 | `db_backup.backup_databases(config, today=None)` snapshots the framework DB + every per-user module DB to `db_backup_dir/<YYYY-MM-DD>/…` (default `{nextcloud_mount}/istota-db-backups`) via SQLite's online-backup API — off-host durability now that module DBs left the Nextcloud-synced workspaces. **Dated dirs, not a single overwritten slot** (ISSUE-159): a corrupted/emptied live DB can't clobber the last good copy. `db_backup_retention` keeps the N newest dated dirs (0 = keep all) but never prunes a dir holding the newest *good* copy of any DB (`_prune_old_snapshots` protects it). A **collapse guard** (`_apply_collapse_guard` → `db_relocate._data_row_count`) quarantines a fresh snapshot as `*.suspect` (status `suspect`) when a DB that previously held data comes back empty/unreadable; exact-zero only (framework `tasks` legitimately shrinks under retention cleanup). Backup tree is `0700`/files `0600`. Cold copies are forced to DELETE journal mode (a WAL header would SIGBUS on the FUSE mount if ever opened in place). **Mount-liveness guard** (`_destination_is_durable`): a mount-derived destination is written only when `os.path.ismount` is true — a down rclone FUSE mount reverts to a local dir, and a naive `mkdir` would silently write the "backup" to local disk under the stale mountpoint; the run is skipped instead and the clock left stale. An explicit `db_backup_dir` is trusted without the check. The clock is **persisted** to `{db_path.parent}/.db_backup_last_run` and seeded at boot via `db_backup.last_backup_time`, so it survives restarts. It advances **only when ≥1 DB snapshotted OK** — a fully-errored (or mount-down) run leaves it stale so the staleness alert can fire. The scheduler alerts the operator on any errored/suspect DB (`_alert_backup_problems`) and on **staleness** (`_maybe_alert_backup_stale`: persisted last-run older than `2 × db_backup_interval`; re-armable, gated on a prior successful run so a fresh deploy doesn't false-alarm). Both go through `_send_operator_alert`, which runs `send_notification` on a short-lived daemon thread with a join timeout so a wedged Talk can't stall the dispatch loop (ISSUE-143 class). Force an immediate backup with `python -m istota.db_backup` (ignores the interval; closes the first-run gap after a deploy). Restore via `db_restore` (`python -m istota.db_restore --all`) — copies the newest good cold copy back (or `--date`), clears stale `-wal`/`-shm` sidecars, refuses an empty snapshot without `--force`, and refuses to run while the daemon holds its flock (`_daemon_running`) since a copy over a live WAL DB corrupts it; then `init_db` re-flips WAL |
| `scheduler_stats_interval` | 60s | One `scheduler_stats threads=… fds=… rss_mb=… tasks_running=… workers_active=… admission_closed_s=…` INFO line per interval on logger `istota.scheduler.stats`, daemon-only. `admission_closed_s` is how long the memory gate has been shut (0 = open) and exists because the gate's own drain destroyed the signal that used to carry this: a squeeze with a full backlog reports `workers_active=0` and `tasks_running=0`, identical to an idle night, and dispatch's one WARNING per cooldown is far too sparse to read a squeeze off. Surfaces resource leaks (ISSUE-101 class) in minutes via `journalctl … \| grep scheduler_stats`. psutil-derived fields (`fds`/`rss_mb`) omitted with a one-time WARN when psutil is unavailable; DB hiccup → `tasks_running=?`. First emit after one full interval. `0` disables. |
| `host_pressure_enabled` / `host_pressure_breadcrumb_interval_seconds` | true / 300s | One `host_pressure mem_total_kb=… mem_available_kb=… shmem_kb=… shmem_unaccounted_kb=… tmpfs_sum_kb=… swap_total_kb=… swap_free_kb=… cached_kb=… psi_*=… load1=… tmpfs_used_kb=/dev/shm:N,…` INFO line per interval on logger `istota.scheduler.pressure`, daemon-only. Written **unconditionally**, not on a threshold and not on a delta: the 2026-08-20 outage accumulated ~35 MB/hour of unreclaimable shmem for five days and never crossed a threshold until the day it became fatal, and the flat stretches are what bound when an accumulation started. The load-bearing field is `shmem_unaccounted_kb` = `Shmem` − Σ tmpfs used — a growing tmpfs figure names a mount and a writer, a growing residue means no mount will ever show it and the search moves to `/proc/*/fd`. Every figure is kB, the field order is fixed and a rename is a breaking change (this line is a data format with future readers, and `test_host_pressure.py` asserts the literal string). Six small file reads plus one `statvfs` + one `stat` per tmpfs mount, so it stays on the loop thread. **The gate is `/proc/meminfo`, not PSI**: Debian compiles PSI in but ships `CONFIG_PSI_DEFAULT_DISABLED=y`, so `/proc/pressure/` is absent without `psi=1` on the cmdline — sinking the whole sample there would have thrown away `Shmem` (the reason the module exists) to protect a supporting figure, and made the stage a silent no-op on an ordinary host. Unmeasured figures render `?`, never `0.00`; `is_under_pressure` abstains on a `?` rather than reading it as calm. A `None` sample (no readable `/proc/meminfo` at all — macOS) logs once and then no-ops; any exception is caught and warned as `host_pressure_error …`, deliberately *not* the record's prefix so a failure cannot land inside a grepped series. `shmem_unaccounted_kb` dedupes tmpfs by `st_dev` before subtracting (a bind mount's `statvfs` reports the whole underlying filesystem, and a large enough overcount floors the residue to 0, which reads as a finding), and prints `tmpfs_sum_kb` alongside so an overcount stays visible. Mount points and process `comm`s are re-escaped on render — both can contain a space. First emit on the first tick. 288 lines/day at the default. `0` or `enabled = false` disables. The line also carries `memory_events_high` / `memory_events_oom_kill` from the daemon's own cgroup `memory.events`: Stage 2 shipped `MemoryHigh=5G` but nothing read the counter it moves, and `memory.high` does not kill — it applies an allocation-time sleep to every process in the cgroup, dispatch loop included, so a throttled daemon looks exactly like a hung one. Absent (cgroup v1, no delegation, macOS) renders `?`, never `0`. |
| `host_pressure_sample_interval_seconds` / `host_pressure_psi_threshold` / `min_available_memory_mb` / `host_pressure_alert_cooldown_seconds` / `host_pressure_shmem_unaccounted_alert_mb` / `host_pressure_docker_socket` | 30s / 40.0 / 768 MB / 900s / 1024 MB / `/var/run/docker.sock` | `_check_host_pressure()` samples on its own cadence — faster than the breadcrumb, because this reading feeds a *decision* rather than a series — hands it to `pool.update_pressure()`, and on a crossing writes one `host_pressure_snapshot` block plus one operator alert per cooldown. **Two different predicates off one sample, deliberately.** `is_under_pressure` (PSI + `MemAvailable`) gates admission; `snapshot_trigger` adds a third arm on the `shmem_unaccounted` residue and gates attribution only. The production series forced that split: the host took 1.52 GB of shmem in under five minutes with `memory some avg10` peaking at **0.07** and `MemAvailable` never below 2.9 GB — zram absorbed it exactly as designed, so both original triggers were right not to fire, but the snapshot as first specified could then never have fired on the one event in 24 hours worth attributing. Growth in the residue is its own signal: it says a large shmem allocation exists that no mount can account for, which is the only case where walking `/proc/*/fd` finds the holder — and by the time such an accumulation depresses `MemAvailable`, the evidence naming its owner is days old. `host_pressure_docker_socket` is a read-only GET handle used solely to ask which pid a container has, so its tmpfs can be read via `/proc/<pid>/root` (container mounts are invisible in the host mount table); it comes from config rather than the module default because the handle is root-equivalent. **The snapshot reads running tasks' sandbox tmpfs the same way** (ISSUE-286): `_emit_host_pressure_snapshot` selects every `running` task's `(id, worker_pid)` — at `main_loop_read_timeout_ms`, not `get_db`'s 30s default, since a 30s wait here would put that much drift between the `sample=` figures and the block printed beside them — and passes them to `snapshot(task_pids=…)`. A failed read passes `None`, which renders `sandbox not-queried` rather than `none-running`. The pid is *not* read directly: `worker_pid` is the outer `bwrap` sitting in the daemon's own mount namespace, so reading it returns the daemon's mount table and would restate the host tmpfs as each task's usage; `find_sandboxed_pid` descends to the nearest descendant in a different mount namespace and nothing is printed for a pid that cannot be shown to be sandboxed. `""` disables container lookup, `0` on either threshold disables that arm, and the whole path is off with `host_pressure_enabled = false`. Never raises into the loop: every reader is wrapped, and the sample reaches the gate before the attribution half is attempted, so a snapshot failure never costs admission its input. |
| `loop_stall_alert_seconds` | 180s | Defense-in-depth (ISSUE-143). `LoopWatchdog` runs on its own daemon thread, watches a last-tick timestamp the main loop bumps each iteration (`watchdog.tick()`), and logs an ERROR + fires one operator alert (`send_notification(purpose="alert")` to the first admin/user via `_operator_alert_user`) when the loop hasn't ticked in this long. Re-arms on recovery so a transient stall pages once. `0` disables. After ISSUE-144 (both tiers) there are **no** `suspended()` call sites left — the DB health sweep, the DB backup and the sleep cycles all run off the loop thread, so the watchdog covers the whole loop. `watchdog.suspended()` survives as the escape hatch for any future known-long *in-loop* check (without it such a check would page on every run); prefer `_spawn_background_check`. |
| `worker_idle_timeout` | 10s | Cumulative-idle linger before a worker exits. The worker re-checks for work on a fine cadence for up to this long (continuous emptiness) before exiting; resets whenever a task is claimed. (Pre-phase-2 this was effectively capped to ~one `poll_interval` with a single recheck — the knob is now honoured.) |
| `worker_idle_poll_interval` | 0.5s | Idle re-check cadence inside `_worker_idle_wait`. A follow-up task is claimed within ~one interval instead of waiting a `poll_interval`. A cheap `count_claimable_tasks_for_user_queue` pre-check gates the `claim_task`. 0 or ≥ `worker_idle_timeout` = legacy single coarse-wait + single-recheck. |
| `max_foreground_workers` | 5 | Instance-level fg worker cap. `dispatch` walks `get_users_with_pending_*_queue_tasks` and **breaks** here, so the scan order decides who gets a slot when more users have pending work than there are slots — with these defaults three users saturate the five foreground slots, which is ordinary volume, not an attack. Both scans are `ORDER BY MIN(created_at)` (longest-waiting user first) since ISSUE-250; they used to be a bare `SELECT DISTINCT user_id` with no ordering, so a user late in whatever order SQLite returned could get zero workers tick after tick. Oldest-pending-first rather than round-robin because dispatch keeps no scan state between its ~0.5s ticks, and it ages naturally: a user passed over has an older oldest-task next tick |
| `max_background_workers` | 3 | Instance-level bg worker cap |
| `user_max_foreground_workers` | 2 | Global per-user fg default |
| `long_task_threshold_minutes` / `user_max_long_workers` / `max_long_workers` | 10min / 1 / 2 | Elapsed-time slot reclassification — see "Long-task slot reclassification" above. A *running* foreground task older than the threshold stops counting against its user's interactive cap; `user_max_long_workers` is the per-user discount allowance (additive, so the per-user thread ceiling becomes 3) and `max_long_workers` is the instance-wide discount budget (partitioned *inside* `max_foreground_workers`, which stays the hard thread ceiling). Both caps bound *discounts*, never long tasks: a task becomes long while already running, so long tasks beyond the allowance keep counting as interactive occupancy. `0` on the threshold or the per-user allowance restores the pre-change behaviour and skips the extra per-tick query |
| `task_cgroup_enabled` / `task_memory_max_mb` / `task_pids_max` / `task_cpu_max_percent` | true / 2048 MB / 512 / 200% | Per-task cgroup v2 containment — see "Per-task cgroups" above. The executor creates `<unit cgroup>/task-<id>/` before the brain runs and removes it on every exit path. `memory.max` is the one that matters: a tree past it is OOM-killed inside its own cgroup, so the failure is one failed task rather than a global OOM that picks an unrelated victim. Inert wherever `Delegate=` + `DelegateSubgroup=` have not been applied — `create` returns `None`, the task spawns as before, and the startup line says so after actually probing rather than after merely resolving a path. `task_cpu_max_percent` is a percentage of one core (200 = two cores); `0` writes no `cpu.max`, and `0` on `task_memory_max_mb` / `task_pids_max` writes `max` |
| `user_max_background_workers` | 1 | Global per-user bg default |
| `task_timeout_minutes` | 30 | Claude Code timeout |
| `confirmation_timeout_minutes` | 120 | Confirmation expiry |
| `max_retry_age_minutes` | 60 | Max age for retry |
| `stale_pending_fail_hours` | 2 | Ancient task auto-fail |
| `task_retention_days` | 7 | Task cleanup |
| `usage_retention_days` | 180 | `task_usage` / `task_usage_models` prune, in its own transaction (step 4b above). Far above `task_retention_days` deliberately: a separate table exists so a spend record outlives the task it came from, and `task_id` is left to dangle rather than cascade. 180 rather than a year because `db_backup` snapshots the whole framework DB into dated dirs and keeps `db_backup_retention` (7) of them, so every row here is duplicated seven times on the backup target, on top of a framework DB that already holds `memory_chunks`. Bounds are built in Python as ISO-Z — **not** `datetime('now', '-N days')`, which is what `cleanup_old_tasks` uses against `tasks.created_at`; against ISO-Z values `' '` sorts below `'T'` and same-day comparisons invert. 0 disables |
| `email_retention_days` | 7 | IMAP retention. `email_support.cleanup_old_emails` issues one `skills.email.delete_emails_before` sweep — an IMAP `BEFORE <date>` search plus a batched bulk delete on a single connection — so the work is proportional to what has actually expired. It used to paginate the *newest* 100 envelopes and delete whichever had aged out, which above roughly `100 / days` messages a day deletes nothing on every run while reporting a clean sweep (ISSUE-230). Ages on the IMAP internal date (arrival), not the sender-supplied `Date:`; `BEFORE` is date-granular, so a message is kept up to one extra day rather than deleted early. Deletes **everything** in the folder past the cutoff, not only mail the bot processed — as the paginated sweep it replaces also did, but that one rarely reached anything, so the first run after this fix clears a backlog. The candidate count is logged before any delete. Removal is scoped to the swept UIDs via `UID EXPUNGE` when the server advertises **UIDPLUS**; without it IMAP has no way to remove one message but a folder-wide `EXPUNGE`, which also takes anything another client flagged `\Deleted`, so the fallback logs one warning per host (`_supports_uid_expunge`). `delete_email` — the agent-reachable `delete --confirmed` verb — shares that path. **The capability must be read post-authentication** (`_server_capabilities` issues a live `CAPABILITY` and unions it with the cached tuple): `imaplib` fills `client.capabilities` once from the greeting and nothing refreshes it — imap-tools' `login` bypasses `imaplib.login` entirely — while Dovecot and Gmail advertise UIDPLUS only after login, so reading the cached tuple alone silently reverts every delete to the folder-wide path. A refused `UID EXPUNGE` never falls back; it rolls the `\Deleted` flag back so the refusal is a genuine no-op rather than a mailbox hidden from the user's client. The raw path re-applies imap-tools' `clean_uids` shape check itself, since `imaplib` splices `str` args into the command line unescaped. The sweep is bounded at `_MAX_DELETES_PER_SWEEP` (2000, fixed not a knob) because `run_cleanup_checks` runs it **synchronously on the dispatch loop** — an unbounded first-run backlog is unbounded time with no task dispatch and the stall watchdog counting. Bounded, each tick costs ~20 round trips and logs the remainder; at the 60s cleanup cadence even a six-figure backlog drains in under a day. (Note the socket `timeout` is per-operation, not a session budget — `batch_size`, not this bound, is what limits one command's work.) Oldest first, sorted numerically rather than trusting `SEARCH` order. A stopped sweep that made progress is a WARNING (the next tick re-finds the rest — `SEARCH` does not exclude `\Deleted`); only one that removed nothing is an ERROR. 0 = disable |
| `processed_email_retention_days` | 90 | `processed_emails` prune (ISSUE-231). One row is written per *polled message* — bot self-mail, `discarded` and quiet-sender mail included — and nothing deleted from the table before this. Keyed on `processed_at`, not on whether the row's task still exists: the FK is unenforced and tasks are pruned at `task_retention_days`, so most rows hold a dangling `task_id`. Dedup is not their only remaining job, though — `_EMAIL_SENDER_SUBQUERY` recovers an email turn's envelope sender from here by `task_id` (ISSUE-226) and the canonical `messages` transcript is *not* age-pruned, so **a row still referenced by a `messages` row is never deleted**; without that exclusion an external contact's mail would start rendering in the prompt as the principal's own words. The rows that actually pile up produced no task at all (bot self-mail, `discarded`, quiet senders), so the exclusion costs the prune almost nothing. Indexed on `processed_at` (and `messages.task_id`), since the steady-state no-op prune would otherwise be a full scan a minute under a write transaction. `_effective_processed_email_retention` floors the window at `email_retention_days + 1` (one latched WARNING when it applies; +1 rather than equal because the IMAP sweep is date-granular in the *server's* zone while `processed_at` is an exact UTC timestamp) — the row is what stops a message still physically in `poll_folder` from being re-ingested as a fresh task, and a re-ingest is indistinguishable from new mail. The key is `(uidvalidity, email_id)` since ISSUE-250, so a recreated mailbox no longer makes new UIDs collide with old rows; it is still not folder-qualified. Floor only, never a cap. `email_retention_days = 0` means mail is never deleted from IMAP, so the message's lifetime is unbounded and the prune is **disabled outright** rather than left unfloored. 0 = disable |
| `skill_overlay_reindex_interval` | 21600s (6h) | Memory-search reindex of every configured user's per-skill overlays (ISSUE-343). An overlay is a user-written file with **no CLI write path**, so there is nothing to index from on write — the memory CLI's per-write reindex went with the overlay write verbs, and it never covered a text-editor edit over Nextcloud anyway, which is the authoring mode the file is for. `reindex_skill_overlays` walks the directory, indexes only what `inspect_overlay` says binds, and reaps rows for files that no longer do, so one pass covers an edit, a create and a delete alike. **Deliberately not part of the sleep cycle**, where it first went: `check_sleep_cycles` returns before doing anything when `sleep_cycle.enabled` is false and again when `primary_brain_unavailable` is open, and `memory_search.enabled` is an independent setting — so search-on/sleep-cycle-off indexed no overlay at all, and a usage-limit cooldown cost a night's indexing for work that makes no model call. Opens its **own** connection for the same reason `_run_sleep_cycles` does: that pass holds one write transaction per half, and `index_file` deletes and re-inserts unconditionally (no content hash), so every binding overlay is re-embedded each pass and that work must not sit inside a transaction spanning an LLM call. Seeded to 0 so a fresh daemon indexes on its first tick — a restart is the one moment an overlay edited while the daemon was down is guaranteed to be unindexed. Skipped entirely without a mount, without `memory_search.auto_index_memory_files`, or at 0. Never raises; one user's failure does not cost the rest |
| `scheduled_job_max_consecutive_failures` | 5 | Auto-disable threshold |
| `cron_max_staleness_minutes` | 60 | Insertion-time staleness gate for `check_scheduled_jobs` + `check_briefings`. When `now - next_run > N`, skip the queue insert and bump `last_run_at` to now so the schedule resumes from the next future fire. Suppresses thundering-herd catch-up after a long outage. 0 = legacy unconditional catch-up. |
| `max_subtasks_per_task` | 10 | Deferred subtasks created per parent task |
| `max_subtask_depth` | 3 | Subtask chain depth cap (0 = unlimited) |
| `max_subtask_prompt_chars` | 8000 | Skip deferred subtasks with prompts over this size (0 = unlimited) |
| `log_channel_show_skills` | true | Include selected skills in log channel messages |
| `stream_text_gate_chars` | 280 | Narration gate / substance classifier for streamed answer text on stream surfaces (web/REPL). A text run emits no `text_delta` until it crosses this many chars without an intervening tool call. At a tool boundary (`executor_stream.TaskStreamAdapter.settle_at_tool_boundary`): a short lead-in that stayed under the gate is dropped; a substantial block that crossed it is flushed and kept (the web client renders kept intermediate blocks as their own prose group). Never loses text (a short final answer still arrives via `result`). 0 disables. Executor logs `stream_gate:` per flush/discard — tune against those. |

## Other Scheduler Functions

| Function | Purpose |
|---|---|
| `get_worker_id()` | `{hostname}-{pid}[-{user_id}]` |
| Event streaming (task-event-streaming spec) | `process_one_task` builds an `EventWriter` (`istota/events.py`) per brain-path task and subscribes `TalkEventSubscriber` / `LogChannelSubscriber` / `PushNotificationSubscriber` (`istota/consumers/`). It passes the writer to `execute_task(event_writer=…)`, then emits terminal events (`confirmation`/`result`/`cancelled`/`error` + `done`) and calls `writer.finish()` once the task reaches a non-retry terminal state. On a retry-eligible failure it instead emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice (no terminal frame) and keeps the event log — the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic across attempts (no `UNIQUE(task_id, seq)` collision) and a watching web client survives the retry instead of hanging on "Working…". (The log used to be wiped via `db.delete_task_events` here; that broke the client's resume cursor. `_purge_deferred_files_for_retry` still runs.) The SSE/snapshot terminal backstop (`web_app._synthetic_terminal_events`) covers any remaining terminal-without-`done` gap (e.g. a crash that skipped `finish()`). The old `_make_talk_progress_callback` / `_make_log_channel_callback` / `_composite_callback` and `progress_style` are gone; `_finalize_log_channel(config, task, log_dests, …)` posts the log-channel footer to every resolved destination (`notifications.effective_log_destinations` — opt-in `routing["log"]` > legacy `log_channel`), reading `all_descriptions` / `delivery_state` (per-destination message ids) off `LogChannelSubscriber`. Delivery is capability-keyed: edit-capable surfaces edit their streamed message into the final state, non-edit surfaces (email/ntfy) get one final-summary delivery. |
| `post_result_to_talk()` | Send result to Talk conversation. Optional `target_token` overrides `task.conversation_token` for the actual post — used when the task's stored token isn't a real Talk room. |
| `_talk_target_for_delivery()` | One-line shim over `transport.routing.talk_channel_for_task`, kept because the event consumers, `TalkTransport.resolve_target` and a lot of introspection-shaped tests call it by name (same argument as the `post_result_to_talk` shims). The ladder is documented in `.claude/rules/transport.md` under "Outbound delivery routing"; do not re-derive it here. |
| `_execute_command_task()` | Run a shell-command task in a subprocess (cwd = `config.temp_dir`, env from `build_stripped_env()` + propagated `ISTOTA_*` + manifest-derived credential / connection vars). `ISTOTA_EXPERIMENTAL_FEATURES` is always injected as CSV of `config.experimental.features` so `@requires_feature`-gated subcommands behave consistently with the LLM path. `ISTOTA_DEFERRED_DIR` is exported too, same value as the two sibling paths (`get_user_temp_dir(config, task.user_id)`, mkdir'd first), so a command row participates in the post-success drain — subtask handoff included (ISSUE-233). It was the one path omitting it, which meant a skill CLI invoked from a CRON `command:` row wrote directly where it had a fallback and silently wrote nothing where it did not (`email`'s `sent_emails` record, the one writer with no fallback — now given one). Two behaviour consequences of joining the rail: a deferred write from a command row lands only if the command exits 0 (`_purge_deferred_files_for_retry` discards a failed attempt's ops), and it is not readable back within the same command, since reads still hit the DB directly. `NC_*`, `CALDAV_*`, etc. come from `build_skill_env(list(skill_index), …)` over an `EnvContext` populated by `discover_calendars_for_task(task, config)`, so the gates the LLM path honors (notably `gate_has_discovered_calendars`) apply here too — no parallel hardcoded list. `dispatch_setup_env_hooks` runs after `build_skill_env` so vars declared `from: "setup_env"` (notably `LOCATION_DB_PATH`, `HEALTH_DB_PATH`) reach the subprocess; hook values use `env.update(...)` and overwrite any ambient daemon env because a stray systemd-leaked value would point at the wrong user's DB (ISSUE-097). Success criterion: `returncode == 0`, with one exception — when stdout starts with `{` and parses as a JSON dict containing `"status": "error"`, the task is marked failed using `parsed["error"]` as the message. This catches the silent-failure pattern where module-skill facades (feeds, money) print `{"status":"error","error":"…"}` to stdout while exiting 0 — and the same envelope is what `@requires_feature` emits on a gated-off call, so gating refusals surface as task failures with the human-readable message intact (the money facade additionally unwraps the inner envelope via `_unwrap_inner_error` before re-emitting it). Non-JSON stdout and malformed JSON are unaffected. The subprocess runs via `_run_capture` (not `subprocess.run`) so a timeout kills the whole process *group* — see below. **The command string goes through `shell_exec.shell_argv`, not `shell=True`** — the latter is `/bin/sh -c`, dash on Debian, which starts with `pipefail` off, so a row ending in a pipe reported its last stage and a job whose real work failed was recorded as healthy indefinitely (the counterpart of ISSUE-307 on the operator-authored surface — the inverse of what the five-consecutive-failure auto-disable exists to catch). `_run_capture` no longer takes a `shell` parameter at all, so the bug cannot be reintroduced by a caller passing it back. A host with no bash falls back to `/bin/sh -c`, which is exactly the old behaviour — and `shell_argv` logs once when it does, because a silently-unfixed host is the failure mode the change exists to remove. **This is an interpreter swap and `pipefail` is one consequence of it**, not the whole: bash and dash also differ on `echo` backslash expansion (`echo "a\tb"` emitted a real tab under dash and a literal `\tb` under bash) and on `$0`, and both matter because this path returns stdout *as data*. Bash additionally sources `$BASH_ENV` for a non-interactive shell where dash sources nothing, which is why `executor.build_stripped_env` strips that variable (`_SHELL_STARTUP_ENV_VARS`, exact-match; `ENV` is deliberately not stripped — POSIX shells read it only when interactive, and stripping it would break an operator using it as a deployment name). **Three upgrade consequences on a live deployment.** A row that was silently failing now *reports* failure and can reach the auto-disable threshold. A row whose pipeline contains a `grep` with no match — or any non-final stage that exits non-zero to report rather than to fail — now fails where it used to pass. And a row ending in `head` or `grep -q` reports 141: `_execute_command_task` appends `SIGPIPE_NOTE` to the error (that string becomes `scheduled_jobs.last_error`, which `!cron`, the admin UI and the `cron_job` inbox row all show a human), and `process_one_task` treats it as **non-retryable** via `is_sigpipe_failure` gated on `task.command`. That last part is not cosmetic: the ladder re-runs the whole command string, so a producer that sends mail or writes a file would do it again at 1, 4 and 16 minutes having already succeeded, and 141 recurs anyway. |
| `_execute_skill_task()` | Run an auto-seeded skill subprocess (`python -m istota.skills.<skill>`). Same trusted env resolution as `_execute_command_task`: `build_skill_env` over the **full** skill index (so co-declared vars like `NC_URL`, declared on both `files` and `nextcloud`, reach the subprocess regardless of which skill the row names) plus `discover_calendars_for_task` on the `EnvContext` and `dispatch_setup_env_hooks` for setup_env-resolved vars. `ISTOTA_EXPERIMENTAL_FEATURES` is also injected. No proxy split — skill-tasks run a trusted CLI. Same JSON-error-envelope detection (with money-facade unwrap) as command-tasks. **Special case (ISSUE-098):** `skill="health"` + `skill_args[0]=="garmin-sync"` is short-circuited to `_run_garmin_sync_inprocess` *before* the subprocess spawn. The Garmin engine reads + writes encrypted secrets multiple times per sync (oauth blob, rotated SDK tokens, error flag, last_sync) and the subprocess path strips `ISTOTA_SECRET_KEY` by design — so the engine runs on the daemon thread (where the key is in scope) instead. Mirrors the in-process call the web `/garmin/sync` endpoint already makes. Same `(success, result_text)` shape; scheduler delivery / failure tracking unchanged. |
| `_run_garmin_sync_inprocess()` | Garmin-sync dispatch helper. Parses `--days-back` from `skill_args[1:]` (default 2), resolves `HealthContext` via `health.resolve_for_user`, picks up the user's timezone from `UserConfig`, calls `garmin_sync.sync_garmin(...)` on the daemon thread. Returns a JSON payload shaped `{"status": "ok"\|"error", "inserted": …, "skipped": …, "days_processed": …, "auth_error": bool, "error"?: "token_expired"}` — same shape `_execute_skill_task` would have surfaced if the subprocess path worked. Broad `Exception` catch so an SDK crash maps to a `"garmin sync: <exc>"` failure rather than crashing the worker. |
| `_run_capture()` / `_kill_process_group()` | Subprocess runner for both `_execute_command_task` and `_execute_skill_task`. Runs the child under `subprocess.Popen(start_new_session=True)` and, on `TimeoutExpired`, SIGKILLs the whole process group via the shared `process_group.kill_process_group` (then a bounded second `communicate`) before re-raising. `subprocess.run(timeout=…)` only SIGKILLs the *direct* child and then blocks forever in its post-kill `communicate()` when an orphaned grandchild still holds the stdout/stderr pipe — a CRON `command:` that backgrounds a child (or a skill CLI that shells out) wedged its worker past the timeout that way, and since the per-task heartbeat thread keeps pinging, the liveness-based stuck-running reaper never reclaimed it (a per-minute job held its only background slot 6.5h). Returns a `CompletedProcess` so callers keep using `.returncode`/`.stdout`/`.stderr`; re-raises `TimeoutExpired` so their existing except branches are unchanged. |
| `discover_calendars_for_task()` | Best-effort CalDAV discovery (lives in `executor.py`, re-exported via scheduler imports). Returns `[]` when CalDAV is unconfigured / unreachable / the user owns no calendars. Used by all three subprocess paths (LLM `execute_task`, `_execute_skill_task`, `_execute_command_task`) so the `gate_has_discovered_calendars` env-spec gate fires consistently. |
| `_parse_email_output()` | Parse JSON email response (legacy fallback) |
| `_load_deferred_email_output()` | Load structured email output from deferred file |
| `_process_deferred_sent_emails()` | Record outbound emails for emissary thread matching |
| `post_result_to_email()` | Send email reply with threading |
| `check_briefings()` | Cron-based briefing scheduling. Creates each due briefing as a `source_type="briefing"` background task carrying only the briefing identity (`briefing_name`) + a placeholder prompt — it does **no** network prefetch on the dispatch thread (ISSUE-143). The slow prompt build (`build_briefing_prompt`: news/yfinance/FinViz/IMAP) is deferred to `executor.build_deferred_briefing_prompt`, run by the background worker that picks the task up, so a slow upstream can't stall `pool.dispatch()` for every room. `check_briefing_triggers` (NC-app trigger files) does the same. |
| `check_scheduled_jobs()` | Cron-based job scheduling. **Overlap guard:** before enqueuing a fire it skips when `db.count_inflight_tasks_for_scheduled_job(job.id) > 0` (a prior run still `pending`/`locked`/`running`/`pending_confirmation`), *without* advancing `last_run_at` — so the job fires the next tick once the in-flight run clears (correct for sparse jobs; advancing would push the next fire out a full interval). Stops a `* * * * *` job behind a wedged single background worker from stacking one row/minute. Composes with `cron_max_staleness_minutes` (a far-past `next_run` after a long run then trips staleness suppression). **Model and effort come from `_resolve_job_model_effort`**, which resolves `job.model` against the brain `resolve_brain_kind` answers with for this job — its own `brain` pin, else the `scheduled` lane — rather than the base `[brain] kind` the old line asked, and through `resolve_alias` rather than `resolve_model_name`, which discards the effort half of the pair. Before ISSUE-419 a portable `smart` on a routed deployment was resolved into a namespace the task never ran in and dropped downstream as a crossing, at INFO, and `model = "opus:high"` ran at default effort. Never raises: a brain that cannot be built costs the resolution, not the tick. |
| `cleanup_old_temp_files()` | Remove old temp files |

---

# DB Module (db.py)

## All Tables

| Table | Dataclass | Key Columns |
|---|---|---|
| `tasks` | `Task` | id, status, source_type, user_id, prompt, conversation_token, talk_delivery_token, priority, attempt_count, max_attempts, cancel_requested, worker_pid, last_heartbeat, locked_at/by, scheduled_for, output_target, talk_message_id, reply_to_talk_id, heartbeat_silent, scheduled_job_id, briefing_name, actions_taken, execution_trace, model, effort |
| `user_resources` | `UserResource` | id, user_id, resource_type, resource_path, display_name, permissions |
| `briefing_configs` | `BriefingConfig` | id, user_id, name, cron_expression, conversation_token, components (JSON), enabled |
| `briefing_state` | — | user_id, briefing_name, last_run_at |
| `processed_emails` | `ProcessedEmail` | id, uidvalidity, email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id, routing_method; `UNIQUE (uidvalidity, email_id)` — a UID is unique only within a folder's UIDVALIDITY (ISSUE-250) |
| `istota_file_tasks` | `IstotaFileTask` | id, user_id, content_hash, original_line, normalized_content, status, task_id, file_path |
| `scheduled_jobs` | `ScheduledJob` | id, user_id, name, cron_expression, prompt, conversation_token, output_target, enabled, auto_disabled_at, disabled_at, silent_unless_action, consecutive_failures, model, effort, brain. `brain` is the per-job brain kind (ISSUE-419), file-owned like `model` and `effort` and admin-only on top of that — `cron_loader.fj_brain_or_none` drops it at sync for a non-admin, since CRON.md is model-writable through the `schedules` skill. NULL means "resolve from config", which is what every pre-column row means. Only a pin `resolve_brain_kind` **admits** reaches `db.create_task(brain=…)`; a kind the operator has not listed in `[brain] room_selectable` is refused at sync-read time and the task row is left NULL. That asymmetry with a room is deliberate and is about the model beside it: `_resolve_job_model_effort` resolves `job.model` against the kind that will *run*, every fire, so recording the raw pin left the row naming one namespace while `tasks.model` held a name from another — which `executor._pin_origin_namespace` reads as a crossing and `_request_model` then drops, at INFO. `room_selectable` ships as `[]`, so that was the default outcome of the feature. A room keeps the raw pin because `rooms.model` was written once, while its pin was admitted, so the dropped kind's namespace is the true origin there. NULL rather than the fallthrough kind, because writing that would have `resolve_brain_kind` admit a pin nobody asked for and clear `fallback`, taking availability failover off a job whose pin was refused. `enabled` is the user's intent (CRON.md authors it); `auto_disabled_at` is the scheduler's suspension. A job fires only when `enabled = 1 AND auto_disabled_at IS NULL`. `disabled_at` records that `!cron disable` was what wrote an `enabled = 0`, which that column cannot say on its own — the module sync's legacy rescue arm reads it to tell a user's off switch from a pre-split auto-disable (ISSUE-392). Not backfilled, meaningful only while `enabled = 0`. The CRON.md sync never writes it *from the file* — the file has no way to say the verb did this — but does clear it alongside an `enabled = 1`, since a file that re-enables a job retracts the disable; it reaches no `_module.*` row either way. Two readers: the arm's `IS NULL` test, and the `!cron` listing, which renders `DISABLED since …` where there is a stamp and a bare `DISABLED` where there is not |
| `talk_poll_state` | — | conversation_token, last_known_message_id |
| `sleep_cycle_state` | — | user_id, last_run_at, last_processed_task_id |
| `channel_sleep_cycle_state` | — | conversation_token, last_run_at, last_processed_task_id |
| `heartbeat_state` | `HeartbeatState` | user_id, check_name, last_check_at, last_alert_at, last_healthy_at, last_error_at, consecutive_errors, last_alert_signature |
| `reminder_state` | `ReminderState` | user_id, queue (JSON), content_hash |
| `monarch_synced_transactions` | — | id, user_id, monarch_transaction_id, amount, merchant, content_hash |
| `csv_imported_transactions` | — | id, user_id, content_hash, source_file |
| `user_skills_fingerprint` | — | user_id, fingerprint, updated_at |
| `sent_emails` | — | id, user_id, task_id, message_id, to_addr, subject, thread_id, in_reply_to, references, conversation_token, talk_delivery_token, sent_at |
| `task_logs` | — | task_id, level, message, timestamp |
| `task_usage` | — | id, task_id (**nullable, may dangle**), attempt_seq, origin, created_at (ISO-Z), user_id, source_type, brain_kind, is_fallback, model, effort, stop_reason, success, has_totals, totals_source, billed_input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cost_basis, turns, model_requests, subagent_requests, compacted_requests, initial_context_tokens/peak_context_tokens/context_window (**NULL when unmeasured, never 0**), duration_ms, duration_api_ms, service_tier, session_id, rate_limit_*. One row per brain attempt. Deliberately **not** foreign-keyed to `tasks`: it must outlive `cleanup_old_tasks`, so every aggregate reads the denormalized identity columns rather than joining. `task_id` is NULL for the daemon's model calls that have no task (sleep cycle, shared briefing blocks, health OCR, code review) — `origin` names the caller. `UNIQUE(task_id, attempt_seq) WHERE task_id IS NOT NULL`, partial so non-task rows do not collide. Token aggregates filter `has_totals = 1`; context aggregates filter `initial_context_tokens IS NOT NULL`. The two filters are independent because a run killed before its result frame has real context and meaningless zero tokens |
| `task_usage_models` | — | id, task_usage_id, model, billed_input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, context_window. Per-model split of a parent row. FK decorative — `PRAGMA foreign_keys` is never on, so `prune_old_usage` deletes children explicitly and first. The native brain reports one total with no breakdown, so a measured parent can legitimately have no children; `--by model` attributes those to the parent's own `model` rather than dropping them |
| `task_events` | `TaskEvent` (in `events.py`) | id, task_id, seq, kind, payload (JSON), created_at; `UNIQUE(task_id, seq)`. Task-event-streaming log. `seq` monotonic per task (writer-assigned). Read via `db.get_task_events(task_id, since_seq)`; `seq` resumed across retries via `db.get_max_task_event_seq` (log kept, not wiped on retry). A confirm no longer wipes it either (ISSUE-235): a task parked at `pending_confirmation` keeps the record of what it did before it asked, and `confirmations.approve` prunes only that attempt's `confirmation`/`done` by kind — a web client streams from seq 0, so a surviving `done` would close the re-run's stream in `chat_task_stream` and a surviving `confirmation` would re-arm the answered card. In the shared verb, so Talk, `!confirm` and the web endpoint cannot leave different logs behind. Retention in `cleanup_old_tasks` is the only thing that clears rows wholesale, in bulk SQL over the expired set; `db.delete_task_events(task_id)` is the single-task primitive and currently has no caller. Cascade clause decorative — hand-deleted. |
| `memory_chunks` | — | (from memory_search.py schema) |
| `user_profiles` | — | user_id, display_name, timezone, log_channel, alerts_channel, max_foreground_workers, max_background_workers, email_addresses (JSON), disabled_skills (JSON), disabled_modules (JSON), trusted_email_senders (JSON) |
| `secrets` | — | user_id, service, key, value (Fernet ciphertext), created_at, updated_at, last_accessed_at |
| `knowledge_facts` | — | id, user_id, subject, predicate, object, source, source_task_id, valid_from, valid_to, created_at |
| `knowledge_facts_audit` | — | id, fact_id, user_id, op, payload (JSON), source_task_id, created_at |
| `google_oauth_tokens` | — | user_id, access_token, refresh_token, expires_at, scopes |
| `geocode_cache` / `reverse_geocode_cache` | — | global Nominatim caches (forward + reverse geocoding); shared across users so per-user splitting would lose dedup |
| (`location_pings` / `places` / `visits` / `location_state` / `dismissed_clusters`) | — | per-user `location.db` only; not in framework `istota.db`. See `src/istota/location/db.py` and AGENTS.md "GPS Location" |
| `talk_messages` / `talk_poll_state` | — | Talk poller state + message cache |
| `istota_kv` | — | user_id, namespace, key, value (JSON) — backs the `kv` skill |
| `trusted_email_senders` | — | user_id, pattern (fnmatch) — email-gate allowlist |
| `web_chat_rooms` | `WebChatRoom` | id, user_id, token (channel id), name, archived, created_at, updated_at — backs the web chat surface; the frontend's integer room id. `UNIQUE(user_id, token)` (NOT globally unique on token): a shared Talk room has one handle row per participant so it surfaces in each member's list (ISSUE-134) |
| `room_members` | — | room_token, user_id, created_at; `PRIMARY KEY (room_token, user_id)` — per-user membership of a shared room (ISSUE-134). A room is shared (one token, one transcript) but visibility is resolved through membership (`list_member_rooms`), not the single-owner `rooms.user_id`. Populated by `register_room` / inbound senders / the `room_members_v1` backfill |
| `web_chat_messages` | — | Legacy bot-delivered room messages. **No production reader or writer left** (live-web-chat-room-stream Stage 6 deleted `add_web_chat_message` / `list_web_chat_messages`): `WebTransport.deliver` writes `role='system'` rows into `messages` instead. The table survives only because `delete_web_chat_room`'s cascade still clears it; dropping it is a migration |

## Key DB Functions

### Task Operations
```python
create_task(conn, prompt, user_id, source_type="cli", conversation_token=None,
    parent_task_id=None, is_group_chat=False, attachments=None, priority=5,
    scheduled_for=None, output_target=None, talk_message_id=None,
    reply_to_talk_id=None, reply_to_content=None,
    heartbeat_silent=False, scheduled_job_id=None,
    talk_delivery_token=None) -> int
    # Raises ValueError for a `user_id` that cannot name a directory of its own —
    # empty, whitespace, `.`, `..`, or carrying a separator (`user_scope
    # .is_scopable_user_id`). The column is TEXT NOT NULL, which SQLite satisfies
    # with '', and the parameter's own default was '' with no validation, so an
    # unowned row was one omitted argument away and every path derived from it
    # collapsed to the shared parent (ISSUE-402). The default is still '' so the
    # parameter order is unchanged; what changed is that it no longer produces a
    # row. Every network-facing producer already gates on membership in
    # `config.users` and the subtask path pins the id to the parent row, so what
    # this covers is the local entry points that do not (`istota task -u`,
    # `istota repl -u`, `execute_task_interactive`) and the omitted argument.
    # The second layer, not the boundary: the path joins fail closed on their own.

claim_task(conn, worker_id, max_retry_age_minutes=60, user_id=None) -> Task | None
get_task(conn, task_id) -> Task | None
update_task_status(conn, task_id, status, result=None, error=None, actions_taken=None, execution_trace=None) -> None
    # On `failed`/`cancelled` the answer column is written as `COALESCE(?, result)`
    # (ISSUE-372): passing a value records the partial answer, passing nothing
    # leaves the column alone. The COALESCE is load-bearing — this branch is also
    # reached by a row that already *completed* and had its answer written, when
    # `process_one_task` re-marks it `failed` on an email-delivery failure with no
    # `result`; a plain assignment would blank the only surviving copy (ISSUE-255).
set_task_pending_retry(conn, task_id, error, retry_delay_minutes) -> None
release_task_for_restart(conn, task_id, error) -> None   # requeue, attempt_count untouched
set_task_confirmation(conn, task_id, confirmation_prompt) -> None
confirm_task(conn, task_id) -> None
cancel_task(conn, task_id) -> None
cancel_pending_confirmations(conn, conversation_token, user_id) -> int
is_task_cancelled(conn, task_id) -> bool
list_tasks(conn, status=None, user_id=None, limit=50) -> list[Task]
get_users_with_pending_tasks(conn) -> list[str]
get_users_with_pending_interactive_tasks(conn) -> list[str]
get_users_with_pending_background_tasks(conn) -> list[str]
```

### `claim_task()` Locking Mechanism
1. Fail old stale locked tasks (created > max_retry_age, locked > 30min)
2. Release recent stale locks for retry
3. Fail old stuck running tasks
4. Release recent stuck running for retry
5. Fail stuck running if retries exhausted
6. Atomic `UPDATE...RETURNING` to claim next pending
   - Filters by `user_id` if provided
   - Orders by `priority DESC, created_at ASC`
   - Sets `status='locked', locked_at=now, locked_by=worker_id`

Steps 3–5 (and the standalone `fail_stuck_locked_running_tasks()` maintenance
pass) share `_STUCK_RUNNING_PREDICATE` to decide "stuck" by **worker liveness**,
not raw runtime (ISSUE-112). A `running` task is stuck when its `last_heartbeat`
has been silent longer than `worker_stuck_minutes` (default 10); when no heartbeat
was ever recorded it falls back to `started_at` older than `task_timeout_minutes`
+ grace (`scheduler._stuck_running_minutes`). The running worker pings
`last_heartbeat` every `worker_heartbeat_seconds` via the `_task_heartbeat`
context manager (`db.touch_task_heartbeat`), so a slow-but-alive worker — notably
the in-process native brain, which has no killable PID — is never reclaimed,
while a crashed worker is recovered in minutes. (Distinct from the health-check
heartbeat system in `heartbeat.py`.)

**`worker_pid` invariant.** The column is cleared on *every* transition out of
`running` — `update_task_status` (completed/failed/cancelled),
`set_task_pending_retry`, `release_task_for_restart`, `recover_orphaned_tasks`.
It used to survive a failed attempt, and both cancel paths (`commands.py`'s
`!stop`, which targets the newest `running|locked|pending_confirmation` row,
and `web_app._chat_cancel_task`) signal whatever the row holds — so a retry
row carrying a dead attempt's PID could SIGTERM an unrelated process once the
OS recycled the number (ISSUE-191).

Both cancel paths signal the **process group**, via
`process_group.kill_process_group(pid, SIGTERM)`, not the pid alone: the CLI's
bash grandchildren are where a runaway's work actually is, and a bare
`os.kill` left them running after the user had visibly stopped the task
(ISSUE-257). The helper signals a group only when the pid *leads* one and
otherwise falls back to the single process — a pid that leads no group shares
one with whoever spawned it, and for a daemon child spawned without
`start_new_session` that group is the daemon's, so signalling it would kill the
scheduler.

Both of today's writers record leaders, so the group path is what actually
fires for both: ClaudeCodeBrain's streaming child is a session leader since
ISSUE-257, and a tmux pane pid is one already (tmux `setsid`s the pane child).
A tmux `!stop` therefore now takes the pane's whole command tree. The fallback
is what keeps the helper safe for a future caller that records a pid it did not
spawn.

**This widens the stale-pid hazard above, and clearing `worker_pid` is what
bounds it.** A recycled pid that happens to lead a group now costs that whole
group rather than one process. `_chat_cancel_task` additionally gates the
signal on `status IN ('running','locked')` — `cmd_stop` already selects on that
set, and without it a cancel racing a task that had just finished would read
the pre-clear row and signal whatever the number now belongs to.

### Startup orphan recovery (`recover_orphaned_tasks_on_startup`)
The time-based stuck-reclaim *infers* a dead worker from heartbeat silence — fine
for the rare worker-died-but-daemon-survived case, but slow (≤ `worker_stuck_minutes`)
for the common one: a **scheduler restart** that kills a worker mid-task and leaves
its row `running`. A restart is deterministic, not a guess — the daemon holds a
singleton flock, so the instant a fresh instance boots, every `running`/`locked`
row is definitionally an orphan of the dead instance. `run_daemon` calls
`recover_orphaned_tasks_on_startup(config)` once under the flock, **before any
worker spawns** (step 4a), so there's no live owner to race. `db.recover_orphaned_tasks`
resolves each orphan in priority order: `cancel_requested` → `cancelled` (no
re-run); retries-exhausted / older than `max_retry_age_minutes` / inline-only
source (`INLINE_ONLY_SOURCE_TYPES` — REPL is never daemon-claimed, so releasing
would strand it `pending`) → `failed`; otherwise → `pending` with `attempt_count`
bumped and every liveness column cleared (so the stuck predicate can't re-fire /
a second claimer can't re-steal). For the cancelled/failed cases the scheduler
emits a terminal event frame via a subscriber-less `EventWriter` (seq resumed
above the dead attempt's partial deltas) so a watching web/SSE client gets
immediate closure instead of a hung spinner; released orphans emit nothing — the
re-run streams its own `task_started` and the client resumes from its cursor (the
retry-continuity path). `attempt_count` is the same supersession token the
`process_one_task` ownership guard keys on, so the two compose. `pending_confirmation`
is left untouched (legitimately awaiting the user).

### Conversation & Context
```python
get_conversation_history(conn, conversation_token, exclude_task_id=None,
    limit=10, exclude_source_types=None,
    user_email_addresses=None) -> list[ConversationMessage]
get_previous_tasks(conn, conversation_token, exclude_task_id=None, limit=3,
    exclude_source_types=None, user_email_addresses=None) -> list[ConversationMessage]
get_reply_parent_task(conn, conversation_token, reply_to_talk_id) -> Task | None
external_email_sender(sender_email, own_email_addresses) -> str | None   # pure
email_sender_for_task(conn, task_id) -> str | None
```
`ConversationMessage`: `id, prompt, result, created_at, actions_taken, source_type, user_id, external_sender`

**Email sender attribution (ISSUE-226).** `user_id` on a history row is the
*task's* user — the istota user an email was routed **to**, never the address it
came **from** — so labelling an email turn with it asserts the principal said
something an external contact said. That matters most for a `thread_match`
emissary reply, which is ungated by design. Both history readers therefore
recover the envelope sender from `processed_emails` (a scalar subquery on
`task_id`, backed by `idx_processed_emails_task_id`; a join would fan out on a
non-unique key) and set `external_sender` when it is **not** one of that row's
own user's addresses. Keyed on the address, **not** on `routing_method`: a user
mailing their own plus-address routes as `plus_address`, so the routing method
alone would call them a stranger. `user_email_addresses` is a per-user map, not
one list, because a shared room's turns are not all the requesting user's.
Omitting it fails safe — every email turn is then attributed to its sender.
`context._speaker_label` is the single renderer (`External sender <addr>`), used
by both `format_context_for_prompt` and the triage prompt builder. The rendered
value is an ASCII dot-atom address or the fixed `unknown sender`, never the raw
header: a quoted local part is a valid addr-spec carrying arbitrary spaces and
colons straight into the prompt's speaker position. The same attribution is
applied by `memory/sleep_cycle.speaker_labels` (nightly extraction, whose output
is written durably to `USER.md` + `knowledge_facts`) and by the scheduler's
`index_conversation(..., speaker=…)` call, since an indexed chunk is recalled
back into a later prompt.

### Other Key Functions
```python
# Resources
get_user_resources(conn, user_id, resource_type=None) -> list[UserResource]
add_user_resource(conn, user_id, resource_type, resource_path, display_name, permissions="read") -> int

# Briefings
get_briefing_last_run(conn, user_id, briefing_name) -> str | None
set_briefing_last_run(conn, user_id, briefing_name) -> None

# Scheduled jobs
get_enabled_scheduled_jobs(conn) -> list[ScheduledJob]
increment_scheduled_job_failures(conn, job_id, error) -> int
reset_scheduled_job_failures(conn, job_id) -> None   # also lifts a suspension
disable_scheduled_job(conn, job_id) -> None          # the user's verb: enabled = 0 + disabled_at
suspend_scheduled_job(conn, job_id) -> None          # the daemon's: auto_disabled_at

# Cleanup
expire_stale_confirmations(conn, timeout_minutes) -> list[dict]
fail_ancient_pending_tasks(conn, fail_hours) -> list[dict]
cleanup_old_tasks(conn, retention_days) -> int

# Sleep cycle
get_sleep_cycle_last_run(conn, user_id) -> tuple[str | None, int | None]
set_sleep_cycle_last_run(conn, user_id, last_task_id=None) -> None
get_completed_tasks_since(conn, user_id, since, after_task_id) -> list[Task]

# Heartbeat
get_heartbeat_state(conn, user_id, check_name) -> HeartbeatState | None
update_heartbeat_state(conn, user_id, check_name, **kwargs) -> None

# Sent emails (emissary thread tracking)
record_sent_email(conn, user_id, message_id, to_addr, subject=None, task_id=None, thread_id=None, in_reply_to=None, references=None, conversation_token=None, talk_delivery_token=None) -> int
find_sent_email_by_references(conn, references: list[str]) -> SentEmail | None

# Skills fingerprint
get_user_skills_fingerprint(conn, user_id) -> str | None
set_user_skills_fingerprint(conn, user_id, fingerprint) -> None
```
