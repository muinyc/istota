-- Istota task queue and configuration schema

-- Core task queue
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'pending',  -- pending, locked, running, completed, failed, pending_confirmation, cancelled
    priority INTEGER DEFAULT 5,

    -- Source context
    source_type TEXT NOT NULL,      -- 'talk', 'cli', 'scheduled', 'subtask', 'briefing', 'email'
    conversation_token TEXT,
    user_id TEXT NOT NULL,
    parent_task_id INTEGER,
    is_group_chat INTEGER DEFAULT 0,

    -- Task content
    prompt TEXT NOT NULL DEFAULT '',
    command TEXT,                    -- Shell command (mutually exclusive with prompt)
    attachments TEXT,               -- JSON array of file paths

    -- Execution tracking
    locked_at TEXT,
    locked_by TEXT,
    started_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,

    -- Results
    result TEXT,
    actions_taken TEXT,             -- JSON array of tool use descriptions from execution
    execution_trace TEXT,           -- JSON array of interleaved tool/text events from execution
    error TEXT,

    -- Confirmation flow
    confirmation_prompt TEXT,
    confirmed_at TEXT,

    -- Scheduling
    scheduled_for TEXT,

    -- Delivery
    output_target TEXT,             -- 'talk', 'email', or NULL (default: inferred from source_type)

    -- Talk message tracking (for reply context)
    talk_message_id INTEGER,        -- Talk API ID of the user's incoming message
    talk_response_id INTEGER,       -- Talk API ID of bot's response message
    reply_to_talk_id INTEGER,       -- Talk API ID of the message being replied to
    reply_to_content TEXT,          -- Snapshot of the replied-to message (capped at 1000 chars)

    -- Canonical `messages.id` of the message being replied to — a DIFFERENT
    -- namespace from reply_to_talk_id above, and deliberately not folded into
    -- it: in a Talk-bound room both are small integers, so a canonical id
    -- stored there would resolve to whichever Talk turn happens to share the
    -- number, with no signal at any layer that it went wrong.
    reply_to_message_id INTEGER,

    -- Execution control
    cancel_requested INTEGER DEFAULT 0,  -- Flag to signal task cancellation
    worker_pid INTEGER,                  -- PID of worker process
    last_heartbeat TEXT,                 -- Liveness ping from the running worker (ISSUE-112)

    -- This exchange is deliberately not part of the room `conversation_token`
    -- names (ISSUE-255). Set at ingest for a thread reply the user sent from
    -- their own address: the reply continues that conversation's context, so it
    -- keeps the room as its token, but nothing about it is written back into the
    -- room (ISSUE-254). Without the column each consumer keyed on the token —
    -- the history fallback, the channel memory namespace, the channel sleep
    -- cycle, and the two failure paths — had no way to tell the two apart.
    -- Distinct from the untrusted-sender gate's hold, which withholds a turn
    -- that *does* belong in the room until the user approves it.
    withheld_from_room INTEGER DEFAULT 0,

    -- Silent mode (for scheduled jobs with silent_unless_action)
    heartbeat_silent INTEGER DEFAULT 0,  -- Whether to suppress output on no-action

    -- Log channel suppression (per-task opt-out)
    skip_log_channel INTEGER DEFAULT 0,  -- Whether to suppress log channel output

    -- Scheduled job tracking
    scheduled_job_id INTEGER,       -- Links task back to originating scheduled job

    -- Briefing identity for deferred-prompt briefing tasks (ISSUE-143). When
    -- set, the executor builds the full briefing prompt (slow network I/O) at
    -- worker-pickup time instead of on the scheduler dispatch thread.
    briefing_name TEXT,

    -- Worker queue (foreground = interactive, background = scheduled/briefing/subtask)
    queue TEXT NOT NULL DEFAULT 'foreground',

    -- Skill selection tracking (JSON array of skill names)
    selected_skills TEXT,

    -- Per-task model override (e.g. "claude-sonnet-4-6"); empty = use config default
    model TEXT,
    -- Per-task effort override (low/medium/high/xhigh/max); empty = use config default
    effort TEXT,
    -- The model the brain actually ran (resolved canonical ID), recorded post-run.
    -- Distinct from `model`: stays NULL for default-model tasks so retries
    -- re-resolve the current default; surfaces (web-chat meta) read this.
    model_used TEXT,
    -- Per-task brain override (a kind: claude_code / native / tmux_claude);
    -- NULL = no override, resolve from config. Frozen at task creation from
    -- `rooms.brain`, or from `scheduled_jobs.brain` for a scheduled job, so a
    -- room or a CRON.md edit mid-flight does not change a running task,
    -- and copied by retries and subtasks. Outranks
    -- `[brain.source_type_overrides]`; see `brain.resolve_brain_kind`.
    brain TEXT,
    -- The model namespace `model` was resolved in, frozen from
    -- `rooms.model_namespace` (or, for an inline `!model`, from the brain the
    -- room actually admits) at task creation. Read by
    -- `executor._pin_origin_namespace` in preference to inferring one from
    -- `brain` or from the lane. NULL = not recorded, and the inference still
    -- answers it — which is what keeps an upgrade from changing any existing
    -- row's outcome (ISSUE-420).
    model_namespace TEXT,

    -- Real Talk room for this task's notifications. Distinct from
    -- conversation_token, which doubles as an email-thread grouping key for
    -- email-source tasks. NULL falls back to conversation_token at delivery time.
    talk_delivery_token TEXT,

    -- Skill-task dispatch (Phase 1.3 of unified credential resolution).
    -- When skill is non-NULL the scheduler runs `python -m istota.skills.<skill>`
    -- with skill_args (JSON list[str]) and credentials pre-resolved on the
    -- trusted side. Mutually exclusive with prompt and command.
    skill TEXT,
    skill_args TEXT,

    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON tasks(scheduled_for) WHERE scheduled_for IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue, status);
-- Dispatch's per-tick long-task scan (count_long_running_tasks_by_user): two
-- equalities then a started_at range. idx_tasks_queue above covers only the
-- equality prefix, and this scan runs every ~0.5s. user_id is carried to make
-- the index *covering*, so the scan never touches the table; it does not order
-- the GROUP BY, which still builds a temp b-tree — started_at is a range
-- constraint, so index order is not user_id order. That b-tree spans one row
-- per long task, which is single digits.
CREATE INDEX IF NOT EXISTS idx_tasks_queue_started ON tasks(queue, status, started_at, user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at);

-- User resource permissions
CREATE TABLE IF NOT EXISTS user_resources (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,    -- 'calendar', 'folder', 'email_folder', 'todo_file'
    resource_path TEXT NOT NULL,
    display_name TEXT,
    permissions TEXT DEFAULT 'read', -- 'read', 'write'
    extras TEXT,                     -- JSON dict of resource-type-specific config (e.g. overland ingest_token, money config_path)
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, resource_type, resource_path)
);

CREATE INDEX IF NOT EXISTS idx_user_resources_user ON user_resources(user_id);

-- Briefing configurations
CREATE TABLE IF NOT EXISTS briefing_configs (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,             -- 'morning', 'evening', etc.
    title TEXT NOT NULL DEFAULT '', -- display title; '' = derive from name
    cron_expression TEXT NOT NULL,  -- '0 7 * * 1-5' for 7am weekdays
    conversation_token TEXT NOT NULL,
    components TEXT NOT NULL,       -- JSON: legacy component bag, retained only as a one-time components→blocks migration carrier
    output TEXT NOT NULL DEFAULT 'talk',  -- delivery descriptor (talk/email/both/all/ntfy/surface:chan/comma lists)
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

-- Task logs for observability
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    timestamp TEXT DEFAULT (datetime('now')),
    level TEXT NOT NULL,            -- 'debug', 'info', 'warn', 'error'
    message TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id);

-- Task event stream — real-time, persisted observability for every output
-- surface (web SSE, Talk, push, log channel, admin). seq is monotonic per
-- task_id and assigned by the EventWriter. The ON DELETE CASCADE clause is
-- documentation only: SQLite enforces FKs only with PRAGMA foreign_keys=ON,
-- which istota never sets, so task_events is hand-deleted in cleanup_old_tasks
-- and on retry (see db.delete_task_events).
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_seq ON task_events (task_id, seq);

-- Processed emails (to avoid duplicate processing)
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY,
    -- An IMAP UID is unique only within a folder's UIDVALIDITY. Keyed on the
    -- bare UID, a recreated or migrated mailbox restarts numbering at 1 and
    -- every new message collides with an old row — read as "already
    -- processed", so the mail is dropped, and an IntegrityError on insert
    -- (ISSUE-250). 0 is the "server did not report it" namespace.
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    email_id TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    subject TEXT,
    thread_id TEXT,  -- for conversation context grouping
    message_id TEXT,  -- RFC 5322 Message-ID for reply threading
    "references" TEXT,  -- RFC 5322 References header for thread chain
    user_id TEXT,
    task_id INTEGER,
    routing_method TEXT,  -- plus_address, sender_match, thread_match, discarded, quiet, read_error, throttled
    processed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (uidvalidity, email_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_processed_emails_email_id ON processed_emails(email_id);
CREATE INDEX IF NOT EXISTS idx_processed_emails_thread_id ON processed_emails(thread_id);
-- The conversation-history readers look the envelope sender up per task
-- (ISSUE-226); without this the lookup is a table scan per history row.
CREATE INDEX IF NOT EXISTS idx_processed_emails_task_id ON processed_emails(task_id);
-- The retention prune (ISSUE-231) selects by age on every cleanup tick, on a
-- table that has been growing one row per polled message since the deployment
-- was stood up. Without this the steady-state no-op prune is a full scan a
-- minute, under a write transaction on the DB the dispatch loop shares.
CREATE INDEX IF NOT EXISTS idx_processed_emails_processed_at ON processed_emails(processed_at);

-- Inbound email poll cursor, one row per polled folder (ISSUE-250). The poll
-- used to fetch the newest 50 messages in the folder and dedupe afterwards,
-- so anything that dropped below the top 50 between two ticks was never
-- fetched again — silent, permanent mail loss at ~50 messages per interval.
-- The cursor makes the batch a boundary instead of a window: each tick takes
-- the oldest `email_poll_batch_size` UIDs above `last_uid` and leaves the rest
-- for the next one, so a backlog drains rather than truncating.
CREATE TABLE IF NOT EXISTS email_poll_state (
    folder TEXT PRIMARY KEY,
    -- The namespace `last_uid` counts in. A change means the mailbox was
    -- recreated and UIDs restarted, so the cursor is meaningless and resets.
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_uid INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Briefing state (tracks last_run_at for config-based briefings)
CREATE TABLE IF NOT EXISTS briefing_state (
    user_id TEXT NOT NULL,
    briefing_name TEXT NOT NULL,
    last_run_at TEXT,
    PRIMARY KEY (user_id, briefing_name)
);

-- Talk polling state (tracks last message ID per conversation for polling)
CREATE TABLE IF NOT EXISTS talk_poll_state (
    conversation_token TEXT PRIMARY KEY,
    last_known_message_id INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- TASKS.md file tasks (tracks tasks from user's TASKS.md files)
CREATE TABLE IF NOT EXISTS istota_file_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    original_line TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    task_id INTEGER,
    result_summary TEXT,
    error_message TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    file_path TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(user_id, content_hash),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_istota_file_tasks_user ON istota_file_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_istota_file_tasks_status ON istota_file_tasks(status);

-- Scheduled recurring jobs (managed at runtime via sqlite3)
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    command TEXT,                    -- Shell command (mutually exclusive with prompt)
    conversation_token TEXT,
    output_target TEXT,             -- 'talk', 'email', or NULL
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    silent_unless_action INTEGER DEFAULT 0,  -- Suppress output unless ACTION: prefix
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT,
    last_success_at TEXT,
    -- When the scheduler suspended this job after N consecutive failures;
    -- NULL = not suspended. The daemon's column: CRON.md cannot express it
    -- and the sync never writes it. `enabled` is the user's intent alone.
    auto_disabled_at TEXT,
    -- When the user last switched this job off with `!cron disable`; NULL =
    -- they have not. `enabled` says a job is off and this says who said so,
    -- which `enabled` alone cannot: the CRON.md sync writes that column from
    -- the file and the daemon used to write it too. Only meaningful while
    -- `enabled = 0`; `enable_scheduled_job` clears it.
    disabled_at TEXT,
    once INTEGER DEFAULT 0,                 -- One-time job: auto-removed after successful execution
    skip_log_channel INTEGER DEFAULT 0,     -- Suppress log channel output for tasks from this job
    model TEXT,                             -- Per-job model override; empty = use config default
    effort TEXT,                            -- Per-job effort override; empty = use config default
    brain TEXT,                             -- Per-job brain kind override; empty = resolve from config
    -- Skill-task dispatch (Phase 1.3). Mutually exclusive with command.
    skill TEXT,
    skill_args TEXT,
    -- Publish this job's result text into shared_kv on success (admin-shared-
    -- briefing-blocks spec). "<ns>/<key>" or bare "<key>" (→ briefing_shared_blocks).
    -- Gated on is_shared_kv_writer at write time.
    publish_shared_kv TEXT,
    publish_shared_kv_trusted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_user ON scheduled_jobs(user_id);

-- Sleep cycle state (tracks last run for nightly memory extraction)
CREATE TABLE IF NOT EXISTS sleep_cycle_state (
    user_id TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_processed_task_id INTEGER
);

-- Heartbeat monitoring state (tracks check execution and alerting)
CREATE TABLE IF NOT EXISTS heartbeat_state (
    user_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    last_check_at TEXT,           -- When check was last evaluated
    last_alert_at TEXT,           -- When last alert was sent (for cooldown)
    last_healthy_at TEXT,         -- When check last passed (for recovery detection)
    last_error_at TEXT,           -- When check implementation itself failed
    consecutive_errors INTEGER DEFAULT 0,
    -- What the last alert was *about*, for a check that can name its own
    -- failures (`CheckResult.alert_signature`). The cooldown above rate-limits
    -- a standing failure; this ends it, so a condition documented as normal on
    -- a deployment pages once rather than once per cooldown for ever. NULL for
    -- every check type that does not opt in, which is all of them but
    -- `self-check`.
    last_alert_signature TEXT,
    PRIMARY KEY (user_id, check_name)
);

-- Reminder rotation state (tracks shuffle queue for briefing reminders)
CREATE TABLE IF NOT EXISTS reminder_state (
    user_id TEXT PRIMARY KEY,
    queue TEXT NOT NULL,          -- JSON array of remaining reminder indices
    content_hash TEXT NOT NULL,   -- Hash of reminders content (reset queue on change)
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Monarch Money API-synced transactions (deduplication + reconciliation tracking)
--
-- ISSUE-427: this pair of tables and their indexes have no reader and no writer
-- left. They predate the money module, which arrived with its own
-- `monarch_synced_transactions` / `csv_imported_transactions` in each user's
-- money DB and is where every live read and write goes. The framework code that
-- touched these is gone; the tables stay because two empty tables cost nothing
-- and dropping them is a migration on deployments that may hold rows. Do not
-- write new code against them — `istota.money.db` is the authoritative pair.
--
-- One live coupling survives that, and it is the reason `db._run_migrations`
-- still carries an ALTER loop for a table nothing reads: `idx_monarch_synced_active`
-- below is a *partial* index filtering on `recategorized_at`. `init_db` runs
-- migrations before this script so that column exists first, and a database whose
-- table predates it would fail the CREATE INDEX with "no such column" and abort
-- every statement after it. Remove the column, the index or that loop only
-- together.
CREATE TABLE IF NOT EXISTS monarch_synced_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    monarch_transaction_id TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now')),
    -- Reconciliation tracking (added for tag change detection)
    tags_json TEXT,                -- JSON array of tags at sync time
    amount REAL,                   -- Transaction amount for reversal
    merchant TEXT,                 -- Merchant name for reversal narration
    posted_account TEXT,           -- Beancount expense account posted to
    txn_date TEXT,                 -- Transaction date (YYYY-MM-DD)
    recategorized_at TEXT,         -- When reversal was created (NULL if still valid)
    content_hash TEXT,             -- SHA-256 of date+amount+merchant for cross-source dedup
    UNIQUE(user_id, monarch_transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_monarch_synced_user ON monarch_synced_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_monarch_synced_active ON monarch_synced_transactions(user_id)
    WHERE recategorized_at IS NULL;

-- CSV imported transactions (deduplication via content hash)
CREATE TABLE IF NOT EXISTS csv_imported_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,   -- SHA-256 of date+amount+merchant+account
    source_file TEXT,             -- Original filename for reference
    imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_csv_imported_user ON csv_imported_transactions(user_id);

-- Channel sleep cycle state (tracks last run for channel-level memory extraction)
CREATE TABLE IF NOT EXISTS channel_sleep_cycle_state (
    conversation_token TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_processed_task_id INTEGER
);

-- Memory search chunks (hybrid BM25 + vector search)
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- conversation, memory_file, user_memory, channel_memory
    source_id TEXT NOT NULL,          -- task_id or file path
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,       -- SHA-256 for dedup
    metadata_json TEXT,
    topic TEXT,                       -- coarse classifier: work, tech, personal, finance, admin, learning, meta
    entities TEXT,                    -- JSON array of entity names (lowercase)
    valid_from TEXT,                  -- episode window open (ISSUE-109 #2); NULL = standing
    valid_until TEXT,                 -- episode window close; chunk suppressed from recall once passed
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_user ON memory_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_source ON memory_chunks(user_id, source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_topic ON memory_chunks(user_id, topic);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_valid_until ON memory_chunks(user_id, valid_until) WHERE valid_until IS NOT NULL;

-- Per-user skills version fingerprint (for "what's new" detection)
CREATE TABLE IF NOT EXISTS user_skills_fingerprint (
    user_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- FTS5 external content table (synced via triggers, no content duplication)
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
    content, content='memory_chunks', content_rowid='id'
);

-- Triggers to keep FTS5 in sync with memory_chunks
CREATE TRIGGER IF NOT EXISTS memory_chunks_ai AFTER INSERT ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_chunks_ad AFTER DELETE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_chunks_au AFTER UPDATE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memory_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Talk message cache (poller-fed, replaces per-task API fetches for context)
CREATE TABLE IF NOT EXISTS talk_messages (
    message_id INTEGER NOT NULL,
    conversation_token TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    actor_display_name TEXT NOT NULL DEFAULT '',
    actor_type TEXT NOT NULL DEFAULT 'users',
    message_text TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT 'comment',
    message_parameters TEXT,  -- JSON string (dict or list)
    timestamp INTEGER NOT NULL DEFAULT 0,
    reference_id TEXT,
    deleted INTEGER DEFAULT 0,
    parent_id INTEGER,
    PRIMARY KEY (conversation_token, message_id)
);

-- Key-value store for script runtime state (scoped by user and namespace)
CREATE TABLE IF NOT EXISTS istota_kv (
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_istota_kv_ns ON istota_kv(user_id, namespace);

-- Shared (cross-user) key-value store. Namespaced JSON values readable by any
-- user; writes are admin-gated at the caller (the table itself does no auth).
-- The table *is* the shared scope (no user_id in the key), so a write here is
-- definitionally a shared write. `written_by` is audit-only, never an
-- authorization input.
CREATE TABLE IF NOT EXISTS shared_kv (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    written_by TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_shared_kv_ns ON shared_kv(namespace);

-- Cron bookkeeping for module-owned shared briefing blocks (generated once
-- globally, written into shared_kv). Mirrors briefing_state.
CREATE TABLE IF NOT EXISTS briefing_shared_block_state (
    name         TEXT PRIMARY KEY,
    last_run_at  TEXT
);

-- Admin-editable shared briefing block definitions (admin-shared-briefing-blocks
-- spec). Seeded once from config (DEFAULT_SHARED_BLOCKS / [[briefing_shared_blocks]])
-- and DB-wins thereafter, so an admin's web edit survives operator re-runs — the
-- same seed-once + edit-preservation contract as default_briefings.
CREATE TABLE IF NOT EXISTS shared_block_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    cron        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    directive   TEXT,
    render_mode TEXT NOT NULL DEFAULT 'synthesis',   -- 'synthesis' | 'structured'
    enabled     INTEGER NOT NULL DEFAULT 1,
    trusted     INTEGER NOT NULL DEFAULT 0,
    sources     TEXT NOT NULL DEFAULT '[]',          -- JSON: [{"kind":..., "config":{...}}]
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Trusted email senders (runtime-managed via !trust command / confirmation flow)
CREATE TABLE IF NOT EXISTS trusted_email_senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sender_email TEXT NOT NULL,         -- Exact email address (lowercase)
    added_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, sender_email)
);

CREATE INDEX IF NOT EXISTS idx_trusted_email_senders_user ON trusted_email_senders(user_id);

-- Sent emails (outbound email tracking for emissary thread matching)
CREATE TABLE IF NOT EXISTS sent_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    task_id INTEGER,
    message_id TEXT NOT NULL,          -- Generated RFC 5322 Message-ID
    to_addr TEXT NOT NULL,
    subject TEXT,
    thread_id TEXT,                    -- Computed thread ID (same algo as email_poller)
    in_reply_to TEXT,                  -- If this was a reply to another message
    "references" TEXT,                 -- RFC 5322 References thread chain
    conversation_token TEXT,           -- Talk conversation where send was requested
    talk_delivery_token TEXT,          -- Originating task's resolved Talk room (real, not synthetic)
    origin_target TEXT,                -- output_target descriptor of the originating surface (web:tok / talk:tok)
    sent_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_sent_emails_message_id ON sent_emails(message_id);
CREATE INDEX IF NOT EXISTS idx_sent_emails_user ON sent_emails(user_id);

-- Outbound drafts: an email the approval gate held rather than sent, waiting
-- for the user to approve, edit or discard it.
--
-- A durable table rather than a file in $ISTOTA_DEFERRED_DIR, for three
-- reasons: deferred files are unlinked on drain, they carry no identity the
-- web UI can address, and they cannot survive an edit-and-resend cycle.
--
-- The row must be self-sufficient. `reply` builds its threading headers from a
-- message fetched over IMAP at compose time; re-fetching at release time is a
-- second network round trip that can fail or return something different. So
-- the recipients, subject and threading headers are snapshotted at hold time
-- and the release sends from the row.
--
-- These rows are deliberately NOT touched by `expire_stale_confirmations`. The
-- inbound gate's 120-minute auto-cancel is right for its own case — dropping an
-- unapproved stranger's email destroys nothing of the user's — and wrong here,
-- where the draft is the user's own intended reply. They sit in `pending` until
-- answered; a 24-hour sweep notifies once (`nagged_at`) rather than expiring.
CREATE TABLE IF NOT EXISTS outbound_drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    task_id       INTEGER,
    room_token    TEXT,                  -- room to render the card in; NULL = global list only
    -- pending | sending | sent | discarded.
    -- `sending` is the claim: `release` takes it in its own committed
    -- transaction *before* touching SMTP, so two concurrent approvals cannot
    -- both send and a discard/edit arriving mid-send is refused rather than
    -- silently losing the race. A row stuck in `sending` means the process died
    -- between the claim and the result — deliberately terminal, because we
    -- cannot know whether the mail went out, and guessing `pending` would risk
    -- sending it twice.
    status        TEXT NOT NULL DEFAULT 'pending',
    to_addrs      TEXT NOT NULL DEFAULT '[]',        -- JSON array
    cc_addrs      TEXT NOT NULL DEFAULT '[]',
    bcc_addrs     TEXT NOT NULL DEFAULT '[]',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    html          INTEGER NOT NULL DEFAULT 0,
    in_reply_to   TEXT,
    "references"  TEXT,
    -- The Reply-To header the holding verb was given (`email send --reply-to`).
    -- Snapshotted like the threading headers: it decides where the recipient's
    -- answer lands, so dropping it on the way through the hold would silently
    -- reroute the conversation.
    reply_to      TEXT,
    attachments   TEXT NOT NULL DEFAULT '[]',        -- JSON array of host paths, re-confined at release
    origin_target TEXT,                  -- originating surface descriptor, copied to sent_emails on release
    hold_reason   TEXT NOT NULL DEFAULT '',
    sent_message_id TEXT,                -- set on release
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at   TEXT,
    nagged_at     TEXT,                  -- stale-draft notification sent; NULL = not yet
    -- Decorative: `PRAGMA foreign_keys` is never enabled on this database, and
    -- `cleanup_old_tasks` prunes tasks at `task_retention_days` (7) while a
    -- draft is designed to outlive that, so `task_id` is *expected* to dangle.
    -- Nothing reads through it; reply routing keys on `origin_target`.
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_outbound_drafts_user_status
    ON outbound_drafts(user_id, status);
CREATE INDEX IF NOT EXISTS idx_outbound_drafts_room
    ON outbound_drafts(room_token, status);
-- The stale-draft sweep filters on status alone and runs on the scheduler's
-- dispatch path, so neither index above is usable for it. Without this one it
-- is a full scan that grows forever, since resolved rows are never pruned.
CREATE INDEX IF NOT EXISTS idx_outbound_drafts_stale
    ON outbound_drafts(status, nagged_at, created_at);

-- Note: Per-user location data (location_pings, places, visits,
-- dismissed_clusters, location_state) lives in per-user
-- {workspace}/location/data/location.db files; see src/istota/location/.
-- Framework istota.db keeps only the global geocode caches below.

-- Geocode cache (forward geocoding results for calendar event locations)
CREATE TABLE IF NOT EXISTS geocode_cache (
    location_text TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Reverse geocode cache (coords → place name via Nominatim)
CREATE TABLE IF NOT EXISTS reverse_geocode_cache (
    lat_rounded REAL NOT NULL,
    lon_rounded REAL NOT NULL,
    display_name TEXT,
    neighborhood TEXT,
    suburb TEXT,
    road TEXT,
    city TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lat_rounded, lon_rounded)
);

-- Google OAuth tokens (per-user Google Workspace access). access_token and
-- refresh_token are stored as Fernet ciphertext (BLOB) keyed off
-- $ISTOTA_SECRET_KEY -- same primitive as the `secrets` table. SQLite is
-- declared-type-lenient, so existing deployments with TEXT columns keep
-- working; the migration in db._run_migrations encrypts any plaintext rows
-- in place on first run with a key available.
CREATE TABLE IF NOT EXISTS google_oauth_tokens (
    user_id TEXT PRIMARY KEY,
    access_token BLOB NOT NULL,
    refresh_token BLOB NOT NULL,
    token_expiry TEXT NOT NULL,     -- ISO 8601 datetime
    scopes TEXT NOT NULL DEFAULT '[]',  -- JSON array of granted scopes
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Knowledge graph (temporal entity-relationship triples)
CREATE TABLE IF NOT EXISTS knowledge_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    temporary INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    source_task_id INTEGER,
    source_type TEXT DEFAULT 'extracted',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kf_user_subject ON knowledge_facts(user_id, subject);
CREATE INDEX IF NOT EXISTS idx_kf_user_predicate ON knowledge_facts(user_id, predicate);
CREATE INDEX IF NOT EXISTS idx_kf_current ON knowledge_facts(user_id, valid_until)
    WHERE valid_until IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_kf_unique_current
    ON knowledge_facts(user_id, subject, predicate, object)
    WHERE valid_until IS NULL;

-- Audit trail for knowledge_facts mutations. Captures inserts,
-- supersessions, fuzzy-dedup skips, invalidations, and deletes so users can
-- inspect why a fact arrived or disappeared. Pruned on the unified retention
-- sweep at 4x the user memory retention window.
CREATE TABLE IF NOT EXISTS knowledge_facts_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    fact_id INTEGER,
    op TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    source_task_id INTEGER,
    source_type TEXT,
    ts TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_kfa_user_ts ON knowledge_facts_audit(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_kfa_fact_id ON knowledge_facts_audit(fact_id);

-- Tier-2 credentials (web-UI-managed, encrypted at rest with a Fernet key
-- derived from $ISTOTA_SECRET_KEY). One row per (user, service, key) — e.g.
-- ("alice", "monarch", "email"), ("alice", "monarch", "password"),
-- ("alice", "karakeep", "api_key"). Encrypted_value is a Fernet token (bytes).
-- last_accessed_at is bumped on read so admins can see which secrets are
-- live vs stale.
CREATE TABLE IF NOT EXISTS secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    service TEXT NOT NULL,
    key TEXT NOT NULL,
    encrypted_value BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_accessed_at TEXT,
    UNIQUE(user_id, service, key)
);
CREATE INDEX IF NOT EXISTS idx_secrets_user_service ON secrets(user_id, service);

-- User profiles (Phase 6 of the Docker onboarding spec).
-- Replaces per-user TOML files (config/users/{user}.toml). Resource entries
-- still live in config.toml under [[users.X.resources]] (deployment-level
-- topology, ansible-managed); profile fields move here so the web UI can
-- write them without touching disk and so Docker can auto-seed at first
-- login. See `.claude/rules/config.md`.
--
-- list/dict columns store JSON arrays/objects.
-- Empty/zero values mean "use defaults" (matches UserConfig dataclass).
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    email_addresses TEXT NOT NULL DEFAULT '[]',          -- JSON array
    timezone TEXT NOT NULL DEFAULT 'UTC',
    log_channel TEXT NOT NULL DEFAULT '',                -- Talk room token
    alerts_channel TEXT NOT NULL DEFAULT '',             -- Talk room token
    max_foreground_workers INTEGER NOT NULL DEFAULT 0,   -- 0 = use global default
    max_background_workers INTEGER NOT NULL DEFAULT 0,
    disabled_skills TEXT NOT NULL DEFAULT '[]',          -- JSON array
    trusted_email_senders TEXT NOT NULL DEFAULT '[]',    -- JSON array (patterns)
    quiet_email_senders TEXT NOT NULL DEFAULT '[]',      -- JSON array (fnmatch patterns; mail filed silently, no task)
    disabled_modules TEXT NOT NULL DEFAULT '[]',         -- JSON array (default-on otherwise)
    routing TEXT NOT NULL DEFAULT '{}',                  -- JSON object: purpose -> output_target descriptor
    default_destination TEXT NOT NULL DEFAULT 'talk',    -- fallback delivery descriptor
    email_reply_routing TEXT NOT NULL DEFAULT 'origin+thread', -- email-reply mirror policy: origin+thread | origin | thread
    outbound_approval TEXT NOT NULL DEFAULT '',          -- outbound email approval: '' = unset (follow [email] outbound_approval_floor) | off | untrusted | all
    external_turn_display TEXT NOT NULL DEFAULT 'collapsed', -- external-origin turn body in web chat: full | collapsed | hidden (the turn itself always renders)
    default_briefings INTEGER NOT NULL DEFAULT 1,        -- seed the shared [[default_briefings]] set into this user
    briefing_email_html INTEGER NOT NULL DEFAULT 1,      -- briefing email as multipart/alternative (HTML + plain) vs plain only
    timezone_follow_location INTEGER NOT NULL DEFAULT 0, -- follow the GPS timezone on travel (opt-in; rewrites a user-chosen value)
    google_scopes TEXT NOT NULL DEFAULT '{}',            -- JSON object: Google service -> off|readonly|full (bounded by the operator's ceiling; {} = unset = the whole ceiling)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Web chat rooms (in-app chat surface). Each room owns a per-user channel
-- token used as the task's conversation_token, so every room gets its own
-- CHANNEL.md memory and sleep-cycle treatment with no special-casing.
-- Always-on surface: there is no per-user opt-out.
-- `token` is unique per (user, token), NOT globally: a shared Talk room (one
-- Nextcloud conversation) has one handle row per participant so it surfaces in
-- each member's web room list (ISSUE-134).
CREATE TABLE IF NOT EXISTS web_chat_rooms (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    token       TEXT NOT NULL,                   -- conversation_token (channel id)
    name        TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, token)
);

CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user
    ON web_chat_rooms (user_id, archived, id);

-- Unsolicited (bot-delivered) messages posted into a web chat room: alerts,
-- the verbose execution log, and any notification routed to the `web` surface.
-- Unlike task-backed chat turns (in `tasks`) these have no originating user
-- prompt, so they render as a single system message merged into the transcript.
CREATE TABLE IF NOT EXISTS web_chat_messages (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    token       TEXT NOT NULL,                  -- room channel id (web_chat_rooms.token)
    role        TEXT NOT NULL DEFAULT 'system',
    title       TEXT,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_web_chat_messages_token
    ON web_chat_messages (token, id);

-- ---------------------------------------------------------------------------
-- Unified Talk / web room sync (surface-independent room registry).
--
-- A `room` is the unit of conversation, identified by its canonical
-- `conversation_token`. For a Talk-origin room that token IS the Nextcloud
-- room token; for a web-origin room it IS the web channel token — so no data
-- moves. `room_bindings` maps the canonical token to each surface's native
-- reference (only needed once a room is exposed on a surface other than its
-- origin). `messages` is the canonical, surface-neutral transcript store.
-- ---------------------------------------------------------------------------

-- Master room registry. One row per conversation, surface-independent.
CREATE TABLE IF NOT EXISTS rooms (
    token       TEXT PRIMARY KEY,          -- canonical conversation_token
    user_id     TEXT NOT NULL,
    name        TEXT,                       -- display name (room title)
    origin      TEXT NOT NULL,              -- 'talk' | 'web' — surface created on
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    archived    INTEGER NOT NULL DEFAULT 0,
    -- Standing per-room model/effort default (canonical model id + effort
    -- level). Applied to every message in the room, on any surface, by
    -- record_inbound when the message carries no inline `!model` override.
    -- NULL = inherit the instance default.
    model       TEXT,
    effort      TEXT,
    -- Standing per-room brain default (a kind: claude_code / native /
    -- tmux_claude). NULL = inherit. Copied onto `tasks.brain` by record_inbound
    -- for a room-member surface, and resolved
    -- `tasks.brain > [brain.source_type_overrides] > [brain] kind`. Admitted
    -- only for a kind the operator listed in `[brain] room_selectable`.
    brain       TEXT,
    -- The namespace `model` above was resolved in, recorded by whichever writer
    -- set it (ISSUE-420). A stored fact rather than an inference from `brain`:
    -- `commands.brain_for_room` refuses a kind the operator has since dropped
    -- from `[brain] room_selectable` and resolves the alias against the lane
    -- instead, so a write made after that point leaves `brain` naming one
    -- namespace and `model` holding an id from another. Also settles the
    -- cross-surface half of ISSUE-421: this row is shared by every surface
    -- bound to the room, written against the writing surface's lane and read
    -- against the inbound one. NULL = not recorded.
    model_namespace TEXT
);
CREATE INDEX IF NOT EXISTS idx_rooms_user ON rooms (user_id, archived);

-- Per-user room membership (ISSUE-134). A room is shared (one token, one
-- transcript) but each participant has a membership row; web visibility is
-- resolved through this, not the single-owner `rooms.user_id`.
CREATE TABLE IF NOT EXISTS room_members (
    room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (room_token, user_id)
);
CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members (user_id);

-- Per-user "hide this room" tombstone. The Talk poll re-adds membership when a
-- room is first registered (and the message loop re-adds active senders), so a
-- dropped room_members row alone isn't a durable hide; list_member_rooms also
-- excludes a tombstoned room. Cleared by the user's own next inbound.
CREATE TABLE IF NOT EXISTS room_dismissals (
    room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (room_token, user_id)
);

-- One row per (room, surface) the room is exposed on.
CREATE TABLE IF NOT EXISTS room_bindings (
    room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
    surface      TEXT NOT NULL,             -- 'talk' | 'web'
    surface_ref  TEXT NOT NULL,             -- Talk: Nextcloud room token; web: room_token
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (room_token, surface)
);
CREATE INDEX IF NOT EXISTS idx_room_bindings_ref ON room_bindings (surface, surface_ref);

-- Canonical message store. Folds the de-facto tasks-as-history store (user +
-- assistant turns) and the bot-notification lane (role='system') into one
-- surface-neutral transcript. The FK cascade is decorative (PRAGMA
-- foreign_keys is unset) — room deletion hand-deletes from here.
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    room_token    TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
    role          TEXT NOT NULL,            -- 'user' | 'assistant' | 'system'
    body          TEXT NOT NULL,            -- markdown/plaintext (final answer text)
    title         TEXT,                     -- system msgs only (heading), else NULL
    task_id       INTEGER,                  -- turn's task; NULL for system msgs
    origin_surface TEXT NOT NULL,           -- surface the message was authored on
    external_ids  TEXT,                     -- JSON {surface: external_id} mirror ledger
    -- JSON [display_name] for a turn that carried files. Display-only, and
    -- deliberately duplicated off `tasks.attachments` (which holds paths):
    -- retention GCs the task row, and the transcript has to keep showing that
    -- a file was part of the turn.
    attachments   TEXT,
    -- JSON [workspace_path | null], positional against `attachments`, so a
    -- chip can link at the session-scoped /chat/files endpoint. Stored beside
    -- the names for the same reason: the host paths live on the `tasks` row
    -- retention deletes. A null entry = not servable (another user's file, or
    -- one outside a workspace) → the chip stays inert.
    attachment_paths TEXT,
    -- Client-minted identity for a send, carried by every attempt at it, so a
    -- retry of a request the server accepted but never got to report resolves
    -- to the first turn instead of creating a second. Web only; NULL for every
    -- other surface and for any client predating it. An empty string is
    -- coerced to NULL on the way in — it is a valid unique key, and would
    -- collapse a room's whole history onto its first send.
    client_msg_id TEXT,
    -- The message this one replies to, as a canonical id in this same table.
    -- Deliberately no foreign key: keeping a dangling id is what lets a reply
    -- to a hard-deleted message render "Original message deleted" instead of
    -- silently becoming an ordinary message. Duplicated off
    -- `tasks.reply_to_message_id` for the same reason `attachments` is —
    -- retention GCs the task row while the transcript must keep rendering the
    -- turn as a reply.
    reply_to_message_id INTEGER,
    -- Who wrote this row. A room bound to several surfaces is multi-human by
    -- construction, so a transcript with no author has to guess, and guessing
    -- "the reader" is wrong for every co-member and every external sender.
    --
    -- Two columns rather than one, because "a known istota user" and "an
    -- arbitrary external label" are different kinds of thing and only the
    -- second needs sanitizing. Collapsing them into one string would move that
    -- decision to every reader and lose the single write-point boundary.
    --
    -- Exactly one is set, or neither — by convention, not by a CHECK, since
    -- SQLite would make adding the constraint later a table rebuild. Readers
    -- therefore need a tiebreak, and **the label wins**: if a writer ever set
    -- both, the label is the external sender and preferring the user id would
    -- render a stranger's mail as the account it was routed to, which is the
    -- exact defect these columns exist to end. Break the tie toward the more
    -- cautious answer.
    author_user_id TEXT,   -- an istota user id, when the writer is one
    -- An external sender, already sanitized through `db.external_email_sender`
    -- on the way in — so it is an addr-spec or the fixed unattributed
    -- sentinel, never a raw `From:` header. Readers render it as-is.
    author_label   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- No index on either author column: they are projected, never filtered.
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages (room_token, id);
-- Partial, so the rows carrying no key are unconstrained. Keyed on different
-- columns from idx_messages_ext below, so the two never interact.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_msg
    ON messages (room_token, client_msg_id)
    WHERE client_msg_id IS NOT NULL;
-- One user row + one assistant row per turn share a task_id; the partial index
-- enforces that and excludes system rows (task_id IS NULL). Keyed on
-- (room_token, role, task_id) — NOT origin_surface — so it actually backstops
-- the app-level idempotency guards (store_turn_message / record_inbound), which
-- dedupe on those three columns. Including origin_surface here would let two
-- rows for the same turn with differing surfaces both slip past.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_ext
    ON messages (room_token, role, task_id)
    WHERE task_id IS NOT NULL;
-- idx_messages_ext leads on room_token, so it can't serve a lookup by task_id
-- alone. `cleanup_old_processed_emails` needs exactly that: "does the
-- transcript still reference this task?", per candidate row.
CREATE INDEX IF NOT EXISTS idx_messages_task_id
    ON messages (task_id)
    WHERE task_id IS NOT NULL;

-- Per-surface read cursors (an unread badge in web isn't cleared by reading
-- on the phone). Talk read state is owned by Nextcloud and not synced back.
CREATE TABLE IF NOT EXISTS room_read_state (
    room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
    surface     TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room_token, surface, user_id)
);

-- Per-user message bookmarks ("stars", web UI). Rooms are shared, so stars
-- are keyed per (message, user) — one member's star never shows for another.
-- FK cascade decorative; delete_web_chat_room hand-deletes matching rows.
CREATE TABLE IF NOT EXISTS message_stars (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    starred_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_message_stars_user
    ON message_stars (user_id, message_id);

-- Deletion ledger for per-message delete. A message delete is HARD — the
-- `messages` row is gone — so the room stream, which tails `messages.id >
-- cursor`, has nothing left to carry the news to another open tab. This table
-- is that carrier: its own monotonic id is a second stream cursor, and a client
-- resumes from it exactly as it resumes from the message cursor.
--
-- Deliberately NOT a tombstone on `messages`: every read path would then have
-- to filter it, and a soft-deleted row still occupies its slot in the
-- (room_token, role, task_id) unique index, so a re-run of the same turn would
-- collide with a message the user believes they removed.
--
-- `room_token` is retained (rather than joined back through the vanished
-- message) so the frame can be scoped to the rooms a caller may see.
CREATE TABLE IF NOT EXISTS message_deletions (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL,
    room_token  TEXT NOT NULL,
    deleted_by  TEXT NOT NULL DEFAULT '',
    deleted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One-time data-migration ledger (markered, so heavy backfills run once).
CREATE TABLE IF NOT EXISTS _migration_state (
    name        TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User-scoped Nextcloud OAuth pair (post-as-user Talk mirroring + read-state
-- sync). Encrypted with the *web-only* ISTOTA_WEB_TOKEN_KEY — not the shared
-- ISTOTA_SECRET_KEY — so only the web process can decrypt. expires_at is
-- plaintext ISO UTC so refresh checks don't need a decrypt.
CREATE TABLE IF NOT EXISTS web_user_tokens (
    user_id       TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,            -- Fernet ciphertext (web key)
    refresh_token TEXT NOT NULL,            -- Fernet ciphertext (web key)
    expires_at    TEXT NOT NULL,            -- ISO 8601 UTC, plaintext
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Mid-flight steering messages (`!steer`). A control channel from the poller /
-- web-POST process into a running worker, like cancellation — but ordered,
-- multi-valued, and consumable, so it gets a table rather than a boolean column.
-- The running (steering-capable) brain drains `pending` rows at its next loop
-- boundary and appends each as a user turn. `dropped` marks steers still pending
-- when the task terminated/suspended (visible in audit, never leaked to a later
-- task).
CREATE TABLE IF NOT EXISTS task_steers (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,          -- per-task monotonic, ordering
    text         TEXT NOT NULL,             -- the steering user message
    user_id      TEXT NOT NULL,             -- who steered (audit)
    source       TEXT NOT NULL,             -- 'talk' | 'web' | 'cli' (provenance)
    status       TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'consumed' | 'dropped'
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    consumed_at  TEXT,
    UNIQUE (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_task_steers_pending
    ON task_steers (task_id, status, seq);

-- Per-task budget for `istota-skill code_review run`. A loop that re-reviews
-- its own diff spends the operator's money one model round at a time, so the
-- count is capped per task.
--
-- In the framework database rather than a file under the task's temp dir on
-- purpose: `ISTOTA_DEFERRED_DIR` is bound read-write into the sandbox, so a
-- loop that reached a file-backed cap could delete the counter and carry on.
-- The CLI runs host-side with `ISTOTA_DB_PATH` (a proxy-only var the model
-- never holds) and the framework DB is masked out of the sandbox entirely.
--
-- A round is one `code_review run` that reached the model at all: a run refused
-- by a guard or short-circuited by the availability breaker is free, and the
-- retry half of a malformed-output round belongs to the round that provoked it
-- rather than counting again. A round that paid for calls and got nothing back
-- still counts, or a reviewer answering in prose would loop unbounded past a
-- cap that never moved. One round is up to four model invocations (two agents,
-- each with one retry).
--
-- At the cap the review degrades to `skipped` rather than erroring, so a task
-- that has already finished its work is not stopped from landing it by the loop
-- guard. The FK below is decorative — `PRAGMA foreign_keys` is not enabled on
-- these connections, matching every other FK in this schema.
CREATE TABLE IF NOT EXISTS code_review_calls (
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    calls      INTEGER NOT NULL DEFAULT 0,   -- rounds charged, parsed or not
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id)
);

-- One row per brain attempt, task-bound or not.
--
-- Deliberately NOT foreign-keyed to tasks: `cleanup_old_tasks` deletes tasks at
-- `scheduler.task_retention_days` (7) and this record must outlive that, at
-- `scheduler.usage_retention_days` (180). `task_id` dangles afterwards, and is
-- NULL for the daemon's model calls that have no task at all (sleep cycle,
-- shared briefing blocks, health OCR, code review) — see `origin`. The
-- denormalized identity columns keep every row self-sufficient, so no aggregate
-- ever joins `tasks`. `tasks.id` is AUTOINCREMENT, so a dangling `task_id` can
-- never be reassigned to a different task.
--
-- Every date comparison against this table uses the ISO-Z format below, NOT the
-- `datetime('now')` idiom `cleanup_old_tasks` uses: ' ' (0x20) sorts below 'T'
-- (0x54), so mixing the two silently drops boundary-day rows instead of raising.
CREATE TABLE IF NOT EXISTS task_usage (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                INTEGER,          -- NULL for non-task calls; may dangle
    attempt_seq            INTEGER NOT NULL DEFAULT 1,
    origin                 TEXT NOT NULL DEFAULT 'task',
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    user_id                TEXT NOT NULL,
    source_type            TEXT NOT NULL DEFAULT '',  -- '' for non-task rows
    brain_kind             TEXT NOT NULL,
    is_fallback            INTEGER NOT NULL DEFAULT 0,
    model                  TEXT NOT NULL DEFAULT '',  -- largest cost share
    effort                 TEXT NOT NULL DEFAULT '',
    stop_reason            TEXT NOT NULL DEFAULT '',
    success                INTEGER NOT NULL DEFAULT 0,

    -- Token totals. Valid only when has_totals = 1; a run killed before the
    -- result frame has real context columns and meaningless zeroes here, so
    -- EVERY token aggregate must filter on has_totals.
    has_totals             INTEGER NOT NULL DEFAULT 0,
    totals_source          TEXT NOT NULL DEFAULT 'unknown',  -- model_usage|derived
    billed_input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens     INTEGER NOT NULL DEFAULT 0,

    cost_usd               REAL NOT NULL DEFAULT 0.0,
    cost_basis             TEXT NOT NULL DEFAULT 'unknown',  -- api|subscription|estimated|unknown

    turns                  INTEGER NOT NULL DEFAULT 0,
    model_requests         INTEGER NOT NULL DEFAULT 0,
    subagent_requests      INTEGER NOT NULL DEFAULT 0,
    compacted_requests     INTEGER NOT NULL DEFAULT 0,

    -- NULL, never 0, when unmeasured (native brain, non-streaming run, or a run
    -- with no message_delta). SQL AVG skips NULL, which is exactly what the
    -- context averages need — a zero would halve a mixed-brain mean.
    initial_context_tokens INTEGER,
    peak_context_tokens    INTEGER,
    context_window         INTEGER,

    duration_ms            INTEGER NOT NULL DEFAULT 0,
    duration_api_ms        INTEGER NOT NULL DEFAULT 0,
    service_tier           TEXT NOT NULL DEFAULT '',
    session_id             TEXT NOT NULL DEFAULT '',

    rate_limit_type        TEXT,
    rate_limit_status      TEXT,
    rate_limit_resets_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_task_usage_created ON task_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_task_usage_user    ON task_usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_usage_task    ON task_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_task_usage_origin  ON task_usage(origin, created_at);
-- Partial: non-task rows all carry task_id NULL and must not collide.
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_usage_attempt
    ON task_usage(task_id, attempt_seq) WHERE task_id IS NOT NULL;

-- Per-model split. The FK is documentation only: istota never sets
-- PRAGMA foreign_keys=ON, so `prune_old_usage` deletes children explicitly,
-- parents second, on one connection.
CREATE TABLE IF NOT EXISTS task_usage_models (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_usage_id       INTEGER NOT NULL REFERENCES task_usage(id) ON DELETE CASCADE,
    model               TEXT NOT NULL,
    billed_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL    NOT NULL DEFAULT 0.0,
    context_window      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_task_usage_models_parent ON task_usage_models(task_usage_id);
CREATE INDEX IF NOT EXISTS idx_task_usage_models_model  ON task_usage_models(model);

-- The notification inbox: what is currently waiting on a user.
--
-- Distinct from `src/istota/notifications.py`, which is *delivery*. This table
-- is the durable open set behind the bell; raising a notification writes a row
-- here and, separately, fans out through the delivery layer. A user with no
-- alerts channel loses nothing, because the bell is always there.
--
-- `title` / `body` are fallback text, not the authoritative render: a row whose
-- source has been unregistered still renders something, and the delivery
-- fan-out needs text without loading a resolver. The rich render comes from
-- `params` through the source's resolver at read time.
CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    source            TEXT NOT NULL,      -- registered producer id
    dedup_key         TEXT NOT NULL,      -- stable identity of the thing being notified about
    object_type       TEXT,               -- 'task', 'draft', 'scheduled_job', 'health_panel', 'secret'
    object_id         TEXT,               -- opaque to the store; validated by the resolver
    severity          TEXT NOT NULL DEFAULT 'info',    -- info | success | warning | danger
    actionable        INTEGER NOT NULL DEFAULT 0,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL DEFAULT '',
    params            TEXT NOT NULL DEFAULT '{}',      -- JSON, owned by the source
    link              TEXT,                            -- in-app route; validated on read
    room_token        TEXT,                            -- provenance only
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Set only when a send actually reached a destination. `send_notification`
    -- returns False when none is configured, and suppressing a re-delivery on
    -- the strength of a send that reached nobody is the exact failure the inbox
    -- exists to fix.
    last_delivered_at TEXT,
    occurrences       INTEGER NOT NULL DEFAULT 1,
    seen_at           TEXT,
    state             TEXT NOT NULL DEFAULT 'open',    -- open | resolved | dismissed | stale
    resolved_at       TEXT,
    resolved_by       TEXT,                            -- 'web' | 'talk' | 'email' | 'cli' | 'system'
    UNIQUE (user_id, source, dedup_key)
);

-- Ordering is `updated_at DESC`, not `created_at DESC`. A reopen preserves
-- `created_at` and refreshes `updated_at`; under `created_at` ordering an old
-- row reopened today would deliver a push, bump the badge and sort below fifty
-- newer rows — unreachable in the panel, and never seen means never closed for
-- an auto-resolving source.
CREATE INDEX IF NOT EXISTS idx_notifications_user_state
    ON notifications (user_id, state, updated_at DESC);
-- Leads with `user_id`, and so does `resolve_by_object`. Panel ids come from
-- the per-user health module DB, where every user has a panel `12`, so a close
-- path keyed on (source, object_type, object_id) alone would resolve every
-- other user's row when one user confirms theirs.
CREATE INDEX IF NOT EXISTS idx_notifications_object
    ON notifications (user_id, source, object_type, object_id);

-- ---------------------------------------------------------------------------
-- Profile pictures
-- ---------------------------------------------------------------------------

-- One row per (user, source). The row set is the precedence chain: a user may
-- hold an uploaded avatar and an imported one at the same time, and removing
-- the upload reveals the import rather than leaving nothing behind.
--
-- The bytes live here rather than in the workspace. Health's uploads_dir is the
-- precedent for user *documents*, which are large, kept verbatim and served
-- rarely; an avatar is ~10 KB, disposable, regenerable and requested on every
-- page load. `Config.workspace_root` also returns None on an rclone deployment,
-- so a file-backed avatar would be unavailable on exactly the deployments with
-- no other place to put it.
--
-- No indexes. The primary key covers every per-user read, and all but one read
-- names a user; the exception is the import job's ETag lookup, which filters on
-- `source` alone and so scans, once per import tick, bounded by the user count.
CREATE TABLE IF NOT EXISTS user_avatars (
    user_id      TEXT NOT NULL,
    source       TEXT NOT NULL,                        -- 'upload' | 'nextcloud'
    mime         TEXT NOT NULL DEFAULT 'image/webp',
    -- sha256 hex of `image`, so it identifies what is *served* rather than what
    -- was sent. The ETag and the cache-busting `?v` both carry it. '' when
    -- image IS NULL.
    content_hash TEXT NOT NULL DEFAULT '',
    -- Normalized 192x192 WebP, or NULL. A NULL image is not an avatar: it is
    -- the negative result of an import probe ("this user has no custom
    -- Nextcloud avatar"), kept so the job can send If-None-Match and stop
    -- re-downloading a generated placeholder every tick. Every read of the
    -- chain filters `image IS NOT NULL`.
    image        BLOB,
    remote_etag  TEXT NOT NULL DEFAULT '',             -- ETag the probe last saw; '' for an upload
    checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, source)
);

-- The deployment's bot icon. One row, or none — distinct from the bot's
-- Nextcloud Talk avatar, which an app password cannot set.
CREATE TABLE IF NOT EXISTS bot_avatar (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    mime         TEXT NOT NULL DEFAULT 'image/webp',
    content_hash TEXT NOT NULL,
    image        BLOB NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
