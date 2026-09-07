"""Database operations for istota task queue."""

import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import sqlite_util
from .user_scope import is_scopable_user_id

logger = logging.getLogger("istota.db")


@dataclass
class Task:
    id: int
    status: str
    source_type: str
    user_id: str
    prompt: str
    command: str | None = None
    conversation_token: str | None = None
    parent_task_id: int | None = None
    is_group_chat: bool = False
    attachments: list[str] | None = None
    result: str | None = None
    actions_taken: str | None = None
    execution_trace: str | None = None
    error: str | None = None
    confirmation_prompt: str | None = None
    priority: int = 5
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: str | None = None
    scheduled_for: str | None = None
    output_target: str | None = None
    talk_message_id: int | None = None
    talk_response_id: int | None = None
    reply_to_talk_id: int | None = None
    reply_to_content: str | None = None
    #: Canonical `messages.id` of the cited parent. A *different namespace*
    #: from `reply_to_talk_id` — never assign one to the other.
    reply_to_message_id: int | None = None
    #: This exchange is deliberately not part of the room `conversation_token`
    #: names (ISSUE-255) — a self-addressed thread reply, which keeps the room as
    #: its token for context but is never written back into it. Read by the
    #: history fallback, the channel memory namespace, the channel sleep cycle,
    #: and the two failure paths that would otherwise have no channel at all.
    withheld_from_room: bool = False
    heartbeat_silent: bool = False
    skip_log_channel: bool = False
    scheduled_job_id: int | None = None
    # Briefing identity for deferred-prompt briefing tasks (ISSUE-143). When
    # set, the executor builds the full briefing prompt (slow network I/O) at
    # worker-pickup time instead of on the scheduler dispatch thread.
    briefing_name: str | None = None
    queue: str = "foreground"
    confirmed_at: str | None = None
    selected_skills: str | None = None  # JSON array of skill names
    model: str | None = None  # Per-task model override; empty/None = use config default
    effort: str | None = None  # Per-task effort override; empty/None = use config default
    model_used: str | None = None  # Model the brain actually ran (resolved canonical ID), set post-run
    # Per-task brain override (a kind: claude_code / native / tmux_claude);
    # None = resolve from config. Frozen here at creation from `rooms.brain` so
    # a room edited while this task runs cannot change what it is, and copied by
    # retries and subtasks. Outranks `[brain.source_type_overrides]`.
    brain: str | None = None
    # The namespace `model` was resolved in, frozen at creation beside it
    # (ISSUE-420). `executor._pin_origin_namespace` prefers it to inferring one
    # from `brain` or from the lane, because neither inference can tell a pin
    # written while a room's brain was still admitted from one written after
    # the operator dropped that kind from `[brain] room_selectable` — the two
    # leave identical rows and want opposite answers. None = not recorded, and
    # the inference answers as it did before.
    model_namespace: str | None = None
    talk_delivery_token: str | None = None  # Real Talk room for this task's notifications; NULL falls back to conversation_token
    # Phase 1.3 (unified credential resolution refactor): skill-task
    # dispatch. Set when the task should run a single CLI skill (e.g.
    # auto-seeded `_module.feeds.run_scheduled`) without going through
    # Claude. ``skill_args`` is a JSON-encoded ``list[str]`` of argv to
    # the skill module. When ``skill`` is non-NULL the scheduler routes
    # the task through ``_execute_skill_task`` instead of the prompt /
    # command paths.
    skill: str | None = None
    skill_args: str | None = None
    # What the model had written when a run was cancelled or timed out
    # (ISSUE-372). Set post-run by the executor, read by the scheduler in the
    # same process, and never loaded from a row — it is a hand-off between two
    # halves of one attempt, not task state. The durable copy is what the
    # scheduler writes into the `result` column; there is no column here.
    partial_result: str | None = None


@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str
    resource_path: str
    display_name: str | None
    permissions: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedEmail:
    id: int
    email_id: str
    sender_email: str
    subject: str | None
    thread_id: str | None
    message_id: str | None  # RFC 5322 Message-ID for reply threading
    references: str | None  # RFC 5322 References header for thread chain
    user_id: str | None
    task_id: int | None
    processed_at: str
    routing_method: str | None = None  # plus_address, sender_match, thread_match, discarded, quiet, read_error, throttled
    # The namespace `email_id` counts in; the two together are the key
    # (ISSUE-250). 0 means the server never reported a UIDVALIDITY.
    uidvalidity: int = 0


@dataclass
class SentEmail:
    """Outbound email tracked for emissary thread matching."""
    id: int
    user_id: str
    task_id: int | None
    message_id: str
    to_addr: str
    subject: str | None
    thread_id: str | None
    in_reply_to: str | None
    references: str | None
    conversation_token: str | None
    sent_at: str
    talk_delivery_token: str | None = None  # Originating task's resolved Talk room
    origin_target: str | None = None  # output_target descriptor of the originating surface


@dataclass
class IstotaFileTask:
    """Task tracked from a user's TASKS.md file."""
    id: int
    user_id: str
    content_hash: str
    original_line: str
    normalized_content: str
    status: str
    task_id: int | None
    result_summary: str | None
    error_message: str | None
    attempt_count: int
    max_attempts: int
    file_path: str
    created_at: str | None
    started_at: str | None
    completed_at: str | None


@dataclass
class ScheduledJob:
    id: int
    user_id: str
    name: str
    cron_expression: str
    prompt: str
    conversation_token: str | None
    output_target: str | None
    enabled: bool
    last_run_at: str | None
    created_at: str | None
    command: str | None = None
    silent_unless_action: bool = False
    skip_log_channel: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_success_at: str | None = None
    #: When the scheduler suspended this job; None = not suspended. The
    #: daemon's column, distinct from ``enabled`` (the user's intent).
    auto_disabled_at: str | None = None
    #: When ``!cron disable`` last switched this job off; None = it has not.
    #: Records the *author* of an ``enabled = 0``, which that column cannot
    #: carry on its own. Only meaningful while ``enabled`` is False.
    disabled_at: str | None = None
    once: bool = False
    model: str | None = None  # Per-job model override; empty/None = use config default
    effort: str | None = None  # Per-job effort override; empty/None = use config default
    #: Per-job brain kind pin (cron-per-job-brain-override spec). None/empty
    #: = resolve from config. Written from CRON.md only for an admin; see
    #: ``cron_loader.fj_brain_or_none``.
    brain: str | None = None
    # Phase 1.3 — skill-task dispatch (mirrors ``Task.skill`` / ``skill_args``).
    skill: str | None = None
    skill_args: str | None = None
    # admin-shared-briefing-blocks spec: post-success shared_kv publish target
    # ("<ns>/<key>" or bare "<key>") + trusted flag.
    publish_shared_kv: str | None = None
    publish_shared_kv_trusted: bool = False


@dataclass
class SharedBlockConfigRow:
    """An admin-editable shared briefing block definition row.

    Mirrors :class:`istota.config.BriefingSharedBlock` plus the DB-only ``id`` /
    timestamps. ``sources`` is decoded from the stored JSON into a list of
    ``{"kind", "config"}`` dicts.
    """
    id: int
    name: str
    cron: str
    title: str = ""
    directive: str | None = None
    render_mode: str = "synthesis"
    enabled: bool = True
    trusted: bool = False
    sources: list = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


def _backfill_briefing_output(conn: sqlite3.Connection) -> None:
    """Hoist a legacy ``__output__`` component key into the ``output`` column.

    Per-row best-effort: a bad row is left intact (its ``output`` stays at the
    default) rather than failing startup. Runs after the add-column migration;
    a no-op once every row has been migrated (no row still carries
    ``__output__``).
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(briefing_configs)").fetchall()}
    except sqlite3.OperationalError:
        return  # Table doesn't exist yet.
    if "output" not in cols or "components" not in cols:
        return

    try:
        rows = conn.execute(
            "SELECT id, components FROM briefing_configs"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        raw = row[1]
        if not raw or "__output__" not in raw:
            continue
        try:
            comps = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(comps, dict) or "__output__" not in comps:
            continue
        output = comps.pop("__output__")
        if not (isinstance(output, str) and output.strip()):
            output = "talk"
        try:
            conn.execute(
                "UPDATE briefing_configs SET output = ?, components = ? WHERE id = ?",
                (output, json.dumps(comps, sort_keys=True), row[0]),
            )
        except sqlite3.OperationalError:
            continue


def _add_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> list[str]:
    """`sqlite_util.add_columns` with this file's tolerance, stated once.

    Every framework migration below used to be a bare
    `try: ALTER / except sqlite3.OperationalError: pass`, whose one handler
    carried three conditions: the column is already there, the *table* is not
    there yet (these run before `schema.sql`), and anything else — a lock, in
    practice. The helper answers the first two by reading the schema instead of
    by catching, which is what closes the check-then-ALTER race two connections
    reach at a first post-upgrade boot; `tolerate_errors` keeps the third
    exactly as it was, and the reasoning for that is at the `user_profiles`
    block. `outbound_drafts.reply_to` is the one site here that must not
    tolerate it, and it calls `sqlite_util.add_columns` directly.
    """
    return sqlite_util.add_columns(conn, table, columns, tolerate_errors=True)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Run ALTER TABLE migrations before schema to avoid index failures on new columns.

    Every migration below shares one connection, in Python's legacy
    `isolation_level` mode. DDL autocommits, but any DML opens an implicit
    transaction and holds it open until someone commits — a zero-row UPDATE is
    enough — so whether a transaction is already open at a given migration
    depends on which tables happen to exist in the DB being upgraded.

    The contract for a migration that wants its own transaction: commit the
    inherited one first, then `BEGIN`. Skipping that raises "cannot start a
    transaction within a transaction" on exactly the DBs that have the most to
    lose — the upgraded ones — and passes on a fresh install, which is how
    ISSUE-261 shipped green and killed inbound email for two days.
    """
    # Tasks table migrations
    _add_columns(conn, "tasks", {
        "talk_message_id": "INTEGER",
        "talk_response_id": "INTEGER",
        "reply_to_talk_id": "INTEGER",
        "reply_to_content": "TEXT",
        # Canonical `messages.id` — a different namespace from
        # `reply_to_talk_id`; see the schema.sql comment.
        "reply_to_message_id": "INTEGER",
        "cancel_requested": "INTEGER DEFAULT 0",
        "worker_pid": "INTEGER",
        "last_heartbeat": "TEXT",
        # This exchange is deliberately not part of the room its
        # `conversation_token` names (ISSUE-255); see the schema.sql comment.
        # A constant DEFAULT, so SQLite backfills every existing row with 0 —
        # which is the right answer for all of them: nothing before this wrote
        # the flag, and every consumer's filter reads 0 as "part of the room".
        "withheld_from_room": "INTEGER DEFAULT 0",
        "heartbeat_silent": "INTEGER DEFAULT 0",
        "skip_log_channel": "INTEGER DEFAULT 0",
        "scheduled_job_id": "INTEGER",
        "briefing_name": "TEXT",
        "command": "TEXT",
        "queue": "TEXT DEFAULT 'foreground'",
        "actions_taken": "TEXT",
        "execution_trace": "TEXT",
        "selected_skills": "TEXT",
        "model": "TEXT",
        "effort": "TEXT",
        # The model the brain actually used (resolved canonical ID), recorded
        # post-run. Distinct from `model` (the per-task override): `model` stays
        # empty for default-model tasks so retries re-resolve the current
        # default, while `model_used` records what ran for display/audit.
        "model_used": "TEXT",
        "talk_delivery_token": "TEXT",
        # Phase 1.3 — skill-task dispatch
        "skill": "TEXT",
        "skill_args": "TEXT",
        # Per-task brain override, frozen from `rooms.brain` at creation. No
        # backfill: NULL is the right answer for every existing row and reads
        # as "resolve from config", which is what they all did.
        "brain": "TEXT",
        # The namespace `model` was resolved in (ISSUE-420). No backfill, and
        # for the same reason as `brain` but with more riding on it: NULL means
        # "not recorded", which `executor._pin_origin_namespace` answers with
        # the inference it used before this column, so no existing row's
        # outcome moves. A backfill would have to guess between the two cases
        # this column exists to tell apart, and they leave identical rows.
        "model_namespace": "TEXT",
    })

    # Sent emails: carry the originating task's resolved Talk room so
    # thread-match follow-ups can deliver to the right channel without re-resolving.
    _add_columns(conn, "sent_emails", {
        "talk_delivery_token": "TEXT",
        "origin_target": "TEXT",
    })

    # Scheduled jobs table migrations
    _add_columns(conn, "scheduled_jobs", {
        "silent_unless_action": "INTEGER DEFAULT 0",
        "command": "TEXT",
        "consecutive_failures": "INTEGER DEFAULT 0",
        "last_error": "TEXT",
        "last_success_at": "TEXT",
        # cron-enabled-authority-split spec: the daemon's own disable,
        # kept out of the user-authored `enabled` column the CRON.md sync
        # overwrites every tick. No backfill — nothing on an existing
        # `enabled = 0` row separates an operator disable from an
        # auto-disable, so every existing row starts unsuspended.
        "auto_disabled_at": "TEXT",
        # ISSUE-392: the user's counterpart to the column above — when
        # `!cron disable` last switched this job off. No backfill, for the
        # same reason: an existing `enabled = 0` row does not say who wrote
        # it. That ambiguity is what the module sync's legacy rescue arm
        # exists to resolve, and backfilling either strands the rows it was
        # written for or claims a user disabled something they did not.
        "disabled_at": "TEXT",
        "once": "INTEGER DEFAULT 0",
        "skip_log_channel": "INTEGER DEFAULT 0",
        "model": "TEXT",
        "effort": "TEXT",
        # cron-per-job-brain-override spec: per-job brain kind pin. No
        # backfill — NULL reads as "resolve from config", which is what
        # every existing row already means.
        "brain": "TEXT",
        # Phase 1.3 — skill-task dispatch
        "skill": "TEXT",
        "skill_args": "TEXT",
        # admin-shared-briefing-blocks spec: publish a job's result text into
        # shared_kv on success (gated on is_shared_kv_writer at write time).
        "publish_shared_kv": "TEXT",
        "publish_shared_kv_trusted": "INTEGER NOT NULL DEFAULT 0",
    })

    # Places table: drop source column (moved to config)
    try:
        conn.execute("ALTER TABLE places DROP COLUMN source")
    except sqlite3.OperationalError:
        pass  # Column already dropped or doesn't exist

    # Heartbeat state: what the last alert was about, so a standing failure
    # pages once rather than once per cooldown for ever. Nullable and read as
    # "no signature recorded", so an upgraded database is simply a deployment
    # whose next alert establishes one.
    _add_columns(conn, "heartbeat_state", {"last_alert_signature": "TEXT"})

    # Processed emails migrations
    _add_columns(conn, "processed_emails", {"routing_method": "TEXT"})

    # Memory chunks metadata columns
    _add_columns(conn, "memory_chunks", {
        "topic": "TEXT",
        "entities": "TEXT",
        # ISSUE-109 #2 — episode window for retrieval-time suppression of
        # closed episodic memories.
        "valid_from": "TEXT",
        "valid_until": "TEXT",
    })

    # NOT vestigial, unlike everything else about these two tables (ISSUE-427).
    # `schema.sql` keeps a *partial* index on monarch_synced_transactions
    # filtered on `recategorized_at`, and `init_db` runs migrations before
    # `executescript` exactly so a column an index names exists first. A
    # database whose table predates these seven columns would otherwise fail
    # that CREATE INDEX with "no such column" and abort the rest of the
    # script — every table declared after it. The framework code that read and
    # wrote these tables is gone; this loop and the index it feeds are what
    # remain, and they stand or fall together.
    for col, col_type in [
        ("tags_json", "TEXT"),
        ("amount", "REAL"),
        ("merchant", "TEXT"),
        ("posted_account", "TEXT"),
        ("txn_date", "TEXT"),
        ("recategorized_at", "TEXT"),
        ("content_hash", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE monarch_synced_transactions ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # User resources: JSON extras for resource-type-specific config
    # (overland ingest_token, money config_path/data_dir, feeds tumblr_api_key, etc.).
    # Lets DB-managed resources carry the same payload that TOML rows do, so
    # ansible can drive resource provisioning through `istota resource ensure`
    # instead of templating per-user TOML.
    _add_columns(conn, "user_resources", {"extras": "TEXT"})

    # User profiles: per-user disabled modules list (Phase 1 of the modules /
    # connected services refactor). Default-on, JSON array of disabled module
    # names from istota.modules.MODULE_NAMES.
    #
    # Ahead of the DROP below, because ADD COLUMN appends and the resulting
    # column order is the order these ran in.
    _add_columns(conn, "user_profiles", {
        "disabled_modules": "TEXT NOT NULL DEFAULT '[]'",
    })

    # User profiles: drop legacy ntfy_topic column. ntfy is now a per-user
    # connected service stored in the encrypted secrets table; the profile
    # column is unused.
    try:
        conn.execute("ALTER TABLE user_profiles DROP COLUMN ntfy_topic")
    except sqlite3.OperationalError:
        pass  # Column already dropped or never existed.

    # Every column below swallows OperationalError beyond the two `add_columns`
    # handles itself, which covers the third expected case: a *lock*. There the
    # degradation is asymmetric but safe — reads go through `_row_get` and fall
    # back to a defined default ('' = follow the floor, 'collapsed'), both safe
    # directions, while every write names the column explicitly and fails loudly
    # with "no such column" rather than silently dropping the setting. That is
    # what `_add_columns` passes for this file; `outbound_drafts.reply_to` below
    # is the site where it is not safe and does not.
    _add_columns(conn, "user_profiles", {
        # Purpose-keyed delivery routing. `routing` is a JSON object
        # {purpose -> output_target descriptor}; `default_destination` is the
        # fallback descriptor. Defaults reproduce current behaviour
        # (everything → Talk).
        "routing": "TEXT NOT NULL DEFAULT '{}'",
        "default_destination": "TEXT NOT NULL DEFAULT 'talk'",
        # Email-reply mirror policy: origin+thread (default) | origin | thread.
        "email_reply_routing": "TEXT NOT NULL DEFAULT 'origin+thread'",
        # Quiet email senders: fnmatch patterns whose mail is filed silently
        # (no task, no session). Mirrors trusted_email_senders. JSON array.
        "quiet_email_senders": "TEXT NOT NULL DEFAULT '[]'",
        # Per-user default-briefings opt-in (retire-legacy-briefing-components
        # spec). Default-on; when true the shared [[default_briefings]] set is
        # seeded into the user's briefings. Mirrors disabled_modules handling.
        "default_briefings": "INTEGER NOT NULL DEFAULT 1",
        # Per-user HTML briefing email opt-out (briefing-newsletter-links-html-
        # email spec). Default-on; when true a briefing email is sent
        # multipart/alternative (HTML + plain fallback) so links are clickable.
        "briefing_email_html": "INTEGER NOT NULL DEFAULT 1",
        # Follow the GPS timezone on travel (ISSUE-096). Default OFF — this
        # rewrites a value the user chose, so it is opted into rather than
        # inferred, and an existing row must not start following on upgrade.
        "timezone_follow_location": "INTEGER NOT NULL DEFAULT 0",
        # Per-user Google Workspace scope selection (ISSUE-240). JSON object
        # {service -> off|readonly|full}, bounded by the operator's
        # [google_workspace] scopes ceiling. Empty means "unset", which
        # resolves to the whole ceiling — so an existing user's next reconnect
        # asks for exactly what it asked for before this column existed.
        "google_scopes": "TEXT NOT NULL DEFAULT '{}'",
        # Outbound email approval policy. '' means unset and resolves to the
        # operator's [email] outbound_approval_floor — deliberately, so a user
        # who never touched the setting follows the operator when the operator
        # raises the floor, which is what a floor is for.
        "outbound_approval": "TEXT NOT NULL DEFAULT ''",
        # How an external-origin turn renders in web chat. full | collapsed |
        # hidden — body only. 'collapsed' for existing rows because the
        # alternative is a stranger's full mail body rendered as an ordinary
        # user bubble, which is what this setting exists to stop.
        "external_turn_display": "TEXT NOT NULL DEFAULT 'collapsed'",
    })

    # Outbound drafts: emails the approval gate held instead of sending.
    # Created here for existing DBs; schema.sql also has it (with the full
    # commentary) for fresh installs.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_drafts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT NOT NULL,
            task_id       INTEGER,
            room_token    TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            to_addrs      TEXT NOT NULL DEFAULT '[]',
            cc_addrs      TEXT NOT NULL DEFAULT '[]',
            bcc_addrs     TEXT NOT NULL DEFAULT '[]',
            subject       TEXT NOT NULL DEFAULT '',
            body          TEXT NOT NULL DEFAULT '',
            html          INTEGER NOT NULL DEFAULT 0,
            in_reply_to   TEXT,
            "references"  TEXT,
            reply_to      TEXT,
            attachments   TEXT NOT NULL DEFAULT '[]',
            origin_target TEXT,
            hold_reason   TEXT NOT NULL DEFAULT '',
            sent_message_id TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at   TEXT,
            nagged_at     TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
        """
    )

    # `reply_to` arrived after the table did (the gate wires up `email send
    # --reply-to`, which the store had nowhere to put). Dropping the header
    # would reroute the recipient's answer, so an existing draft table gets the
    # column rather than the gate refusing to hold such a send.
    #
    # The one site in this function that does **not** take `_add_columns`'
    # tolerance, unlike the `user_profiles` ALTERs above, because here the
    # degradation is not safe. Those columns are read through `_row_get` and
    # fall back to a defined default; this one is read unconditionally by
    # `outbound_drafts._row`, so a swallowed lock leaves every draft read
    # raising `IndexError` and every `hold` failing with `no such column` —
    # which the gate turns into "refusing to send", stopping all outbound mail
    # on the instance. Better to fail the open loudly. The race is still
    # absorbed; nothing else is. `add_columns` would also absorb a table that
    # does not exist yet, which the old handler here re-raised — unreachable,
    # because the CREATE TABLE IF NOT EXISTS above is unconditional, so read it
    # as an unreachable arm rather than as a relaxation. Pinned by
    # `tests/test_guarded_column_migrations.py::TestTheOneSiteThatDoesNotTolerate`.
    sqlite_util.add_columns(conn, "outbound_drafts", {"reply_to": "TEXT"})

    # Briefing configs: real `output` delivery column (retire-legacy-briefing-
    # components spec). Previously smuggled into components JSON under the
    # reserved `__output__` key. Add the column, then hoist `__output__` out of
    # each row's components into the column and rewrite components without it.
    # Idempotent: a re-run is a no-op once the column exists and no row still
    # carries `__output__`.
    _add_columns(conn, "briefing_configs", {"output": "TEXT NOT NULL DEFAULT 'talk'"})
    _backfill_briefing_output(conn)

    # Briefing configs: explicit display `title`. Empty means "derive from the
    # briefing name", which reproduces the previous behaviour for every
    # existing row — no backfill needed.
    _add_columns(conn, "briefing_configs", {"title": "TEXT NOT NULL DEFAULT ''"})

    # Knowledge facts dedup: invalidate older duplicate current facts so the
    # partial unique index in schema.sql can be created without IntegrityError
    # on legacy DBs written before ISSUE-042's fix landed. Keeps the newest id
    # per (user_id, subject, predicate, object) group as current; older rows
    # get valid_until = today so they stay in the historical record.
    try:
        conn.execute("""
            UPDATE knowledge_facts
            SET valid_until = date('now'), updated_at = datetime('now')
            WHERE valid_until IS NULL
              AND id NOT IN (
                  SELECT MAX(id) FROM knowledge_facts
                  WHERE valid_until IS NULL
                  GROUP BY user_id, subject, predicate, object
              )
        """)
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet (fresh install before schema.sql runs)

    # Task event stream table (task-event-streaming spec). Created here for
    # existing DBs; schema.sql also has it for fresh installs. The cascade
    # clause is decorative (PRAGMA foreign_keys is unset) — events are
    # hand-deleted in cleanup_old_tasks and delete_task_events.
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                id          INTEGER PRIMARY KEY,
                task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                seq         INTEGER NOT NULL,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (task_id, seq)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_seq "
            "ON task_events (task_id, seq)"
        )
    except sqlite3.OperationalError:
        pass

    # Web chat rooms (web chat surface). Created here for existing DBs;
    # schema.sql also has it for fresh installs. Each room's token is the
    # conversation_token used by its tasks, so each room gets its own
    # CHANNEL.md + sleep-cycle handling.
    try:
        # `token` is NOT globally unique: a shared Talk room (one Nextcloud
        # conversation) has one handle row per participant so it can surface in
        # each member's web room list (ISSUE-134). Uniqueness is per (user, token).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_chat_rooms (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                name        TEXT NOT NULL,
                archived    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, token)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user "
            "ON web_chat_rooms (user_id, archived, id)"
        )
        # Per-user sidebar tint (ISSUE-433). Backfilled on an existing table the
        # same way the `rooms` columns above are, and after the CREATE for the
        # same reason: the table has to exist before the ALTER. NOT NULL with a
        # '' default, so an existing row reads as "no colour" rather than NULL —
        # `_row_to_web_chat_room` maps '' to None either way, but the default
        # keeps the column's contract identical on a fresh install and on a
        # migrated one.
        _add_columns(
            conn, "web_chat_rooms", {"color": "TEXT NOT NULL DEFAULT ''"}
        )
        # Unsolicited (bot-delivered) messages posted into a web chat room —
        # alerts, the verbose execution log, and any notification routed to the
        # `web` surface. Distinct from task-backed chat turns (which live in
        # `tasks`): these have no originating user prompt, so they render as a
        # single system message merged into the room transcript by time.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_chat_messages (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'system',
                title       TEXT,
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_messages_token "
            "ON web_chat_messages (token, id)"
        )
    except sqlite3.OperationalError:
        pass

    # Unified Talk / web room sync (surface-independent room registry).
    # Created here for existing DBs; schema.sql also has these for fresh
    # installs. The cascade clauses are decorative (PRAGMA foreign_keys unset).
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                name        TEXT,
                origin      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                archived    INTEGER NOT NULL DEFAULT 0,
                model       TEXT,
                effort      TEXT,
                brain       TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_user ON rooms (user_id, archived)")
        # Backfill the per-room model / effort / brain columns on an existing
        # rooms table (created by an earlier build without them). Placed here,
        # after the CREATE, so the table exists before the ALTER.
        _add_columns(conn, "rooms", {
            "model": "TEXT",
            "effort": "TEXT",
            "brain": "TEXT",
            "model_namespace": "TEXT",
        })
        # Per-user room membership (ISSUE-134). A room is shared (one token, one
        # transcript) but each participant has a membership row; web visibility is
        # resolved through this, not the single-owner `rooms.user_id`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_members (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                user_id     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, user_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_room_members_user "
            "ON room_members (user_id)"
        )
        # Per-user "hide this room" tombstone. The web hide-an-imported-room
        # action drops the `room_members` row, but the poll-time Talk-room
        # registration backfill re-adds membership for every participant — so
        # the dropped row alone is no longer a durable hide. This tombstone is:
        # written on hide, consulted by `list_member_rooms` (excluded from the
        # web list even while a member), and cleared on the user's own next
        # inbound (`record_inbound`) — "re-engagement un-hides".
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_dismissals (
                room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                user_id      TEXT NOT NULL,
                dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_bindings (
                room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface      TEXT NOT NULL,
                surface_ref  TEXT NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, surface)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_room_bindings_ref "
            "ON room_bindings (surface, surface_ref)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY,
                room_token    TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                role          TEXT NOT NULL,
                body          TEXT NOT NULL,
                title         TEXT,
                task_id       INTEGER,
                origin_surface TEXT NOT NULL,
                external_ids  TEXT,
                attachments   TEXT,
                attachment_paths TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Existing deploys: the transcript has to keep showing that a turn
        # carried a file after retention deletes the `tasks` row holding the
        # paths, so the display names live on the message row too — and, for
        # the ones that sit in the sender's own workspace, the path the chip
        # links at.
        _add_columns(conn, "messages", {
            "attachments": "TEXT",
            "attachment_paths": "TEXT",
            "client_msg_id": "TEXT",
        })
        # Who wrote the row. A room bound to several surfaces is multi-human by
        # construction, so a transcript with no author has to guess, and the
        # guess ("the reader") is wrong for every co-member and every external
        # sender. Two columns because "a known istota user" and "an arbitrary
        # external label" are different kinds of thing and only the second needs
        # sanitizing — see the schema.sql comment. No index: projected, never
        # filtered.
        _add_columns(conn, "messages", {
            "author_user_id": "TEXT",
            "author_label": "TEXT",
        })
        # The citation, so the transcript can render a reply as a reply after
        # retention has deleted the task row that also carries it.
        _add_columns(conn, "messages", {"reply_to_message_id": "INTEGER"})
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room ON messages (room_token, id)")
        # A retry of a send the client gave up on but the server accepted must
        # resolve to the first turn rather than a second one. Partial, so the
        # rows that carry no key (every surface but web, and any web client
        # predating it) are unconstrained. Distinct columns from idx_messages_ext,
        # so the two do not interact.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_msg "
            "ON messages (room_token, client_msg_id) "
            "WHERE client_msg_id IS NOT NULL"
        )
        # Correct an existing deploy's looser index in place. The original keyed
        # on (room_token, origin_surface, role, task_id); drop it so the tighter
        # (room_token, role, task_id) form below replaces it (CREATE IF NOT
        # EXISTS alone would keep the stale definition).
        # Index by position, not name: _run_migrations also runs under init_db's
        # plain (non-Row-factory) connection, where row["sql"] would raise.
        _old_ext = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_messages_ext'"
        ).fetchone()
        if _old_ext and _old_ext[0] and "origin_surface" in _old_ext[0]:
            conn.execute("DROP INDEX idx_messages_ext")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_ext "
            "ON messages (room_token, role, task_id) "
            "WHERE task_id IS NOT NULL"
        )
        # Read cursors are per (room, surface, user) — an unread badge in one
        # member's web view isn't cleared by another member reading (ISSUE-134).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_read_state (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface     TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT '',
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_token, surface, user_id)
            )
        """)
        # Per-user message bookmarks ("stars", web UI). Rooms are shared, so
        # stars are keyed per (message, user) — one member's star never shows
        # for another. The FK cascade is decorative (PRAGMA foreign_keys
        # unset); delete_web_chat_room hand-deletes matching rows.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_stars (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id    TEXT NOT NULL,
                starred_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (message_id, user_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_stars_user "
            "ON message_stars (user_id, message_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migration_state (
                name        TEXT PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # User-scoped Nextcloud OAuth pair, encrypted with the *web-only* key
        # (ISTOTA_WEB_TOKEN_KEY — not the shared ISTOTA_SECRET_KEY). Written and
        # decrypted only by the web process (istota.web_tokens); the scheduler
        # reads nothing here. expires_at is plaintext ISO UTC so refresh checks
        # don't need a decrypt.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_user_tokens (
                user_id       TEXT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Mid-flight steering channel (`!steer`). Created here for existing DBs;
        # schema.sql also has it for fresh installs. The cascade clause is
        # decorative (PRAGMA foreign_keys unset).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_steers (
                id           INTEGER PRIMARY KEY,
                task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                seq          INTEGER NOT NULL,
                text         TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                source       TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                consumed_at  TEXT,
                UNIQUE (task_id, seq)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_steers_pending "
            "ON task_steers (task_id, status, seq)"
        )
        # Shared (cross-user) KV store. Created here for existing DBs; schema.sql
        # also has it for fresh installs. Admin-gated at the caller — the table
        # itself does no auth (see shared_kv_* below + Config.is_shared_kv_writer).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_kv (
                namespace  TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                written_by TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (namespace, key)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_kv_ns ON shared_kv(namespace)"
        )
        # Cron bookkeeping for module-owned shared briefing blocks.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS briefing_shared_block_state (
                name         TEXT PRIMARY KEY,
                last_run_at  TEXT
            )
        """)
        # Admin-editable shared briefing block definitions (admin-shared-briefing-
        # blocks spec). Seeded once from config, DB-wins thereafter. Created here
        # for existing DBs; schema.sql also has it for fresh installs.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_block_configs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                cron        TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT '',
                directive   TEXT,
                render_mode TEXT NOT NULL DEFAULT 'synthesis',
                enabled     INTEGER NOT NULL DEFAULT 1,
                trusted     INTEGER NOT NULL DEFAULT 0,
                sources     TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
    except sqlite3.OperationalError:
        pass

    # Profile pictures. Created here for existing DBs; schema.sql also has both
    # (with the full commentary) for fresh installs. Deliberately outside the
    # try/except above: that block swallows OperationalError, and a swallowed
    # failure here is a daemon that 500s on /me rather than one that says so.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_avatars (
            user_id      TEXT NOT NULL,
            source       TEXT NOT NULL,
            mime         TEXT NOT NULL DEFAULT 'image/webp',
            content_hash TEXT NOT NULL DEFAULT '',
            image        BLOB,
            remote_etag  TEXT NOT NULL DEFAULT '',
            checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, source)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_avatar (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            mime         TEXT NOT NULL DEFAULT 'image/webp',
            content_hash TEXT NOT NULL,
            image        BLOB NOT NULL,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    _migrate_processed_emails_uidvalidity(conn)
    _migrate_unified_rooms(conn)
    _migrate_scheduled_transcript_cleanup(conn)
    _migrate_nonconversational_transcript_cleanup(conn)
    _migrate_web_chat_rooms_peruser(conn)
    _migrate_room_read_state_peruser(conn)
    _migrate_room_members(conn)
    # Last of the `messages` migrations, so it does not attribute rows the
    # cleanup passes above are about to delete. Ordering only — deliberately not
    # gated on their markers: those re-arm on failure, and blocking attribution
    # behind an unrelated retry would leave every transcript unattributed for as
    # long as that failure persists. Attributing a row that a later re-run then
    # deletes costs nothing.
    _migrate_messages_author(conn)

    # Encrypt any plaintext Google OAuth tokens at rest. Idempotent --
    # rows already in Fernet form (the new write path) are detected via
    # decrypt-or-fail and skipped. No-op on fresh installs (table not
    # created until schema.sql runs below) and on deployments without
    # $ISTOTA_SECRET_KEY (the read path will fail loudly so operators
    # notice and the user re-auths).
    _migrate_google_oauth_encryption(conn)

    # Pure DDL with no marker — see the docstring. Last because it depends on
    # nothing above it.
    _migrate_notifications(conn)
    # And then the inbox's one-shot seed, which needs that table to exist. It
    # takes a transaction of its own, so it commits whatever the migrations
    # above left open first (ISSUE-261); nothing after it depends on the
    # inherited one, which is the other reason it goes last.
    _backfill_notifications(conn)


def _resolve_schema_path() -> Path:
    """Locate schema.sql for both a source checkout and an installed wheel.

    In a source checkout db.py lives at ``<repo>/src/istota/db.py`` and the
    schema is ``<repo>/schema.sql`` (``parent.parent.parent``). In a non-editable
    install (``uv tool install`` / pip wheel) there is no repo root — the schema
    is force-included into the package as ``istota/schema.sql`` (``parent``).
    Prefer the packaged copy, fall back to the source-tree copy.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "schema.sql",              # packaged wheel (force-include)
        here.parent.parent / "schema.sql",  # source checkout: <repo>/schema.sql
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Nothing found — return the packaged path so the FileNotFoundError names the
    # location an installed user would expect.
    return candidates[0]


def init_db(db_path: Path) -> None:
    """Initialize database with schema."""
    schema_path = _resolve_schema_path()
    # timeout=30.0 to match `get_db`, not sqlite3's 5s default. Migrations run
    # against a live daemon — the auto-update script calls this from its own
    # process while the services are still up — and the uidvalidity rebuild
    # takes a write lock of its own after committing the earlier migrations
    # (ISSUE-261). A 5s budget turns ordinary writer contention into a rebuild
    # that logs a warning and leaves the schema unmigrated.
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        # WAL is set ONCE here, not on every get_db open. journal_mode is
        # persistent in the SQLite file header, so re-issuing it per
        # connection only buys a needless write-lock acquisition that races
        # sibling readers (the dispatch-loop stall root cause). istota.db is
        # on local disk, so WAL's mmap'd -shm is safe (unlike the per-user
        # module DBs, which historically lived on the FUSE mount).
        conn.execute("PRAGMA journal_mode=WAL")
        # Migrations read rows by column name (e.g. the unified-rooms backfill),
        # so this connection needs the same Row factory the runtime get_db path
        # uses — a raw connection yields tuples and name-indexing raises
        # TypeError mid-migration (crashed init on upgrade DBs that already held
        # completed tasks). Row supports both name and positional access, so it's
        # a safe superset for every migration step.
        conn.row_factory = sqlite3.Row
        # Run migrations first so new columns exist before schema creates indexes on them
        _run_migrations(conn)
        conn.executescript(schema_path.read_text())


@contextmanager
def get_db(
    db_path: Path, *, busy_timeout_ms: int | None = None
) -> Iterator[sqlite3.Connection]:
    """Get database connection with row factory.

    ``busy_timeout_ms`` overrides the default 30s lock wait — pass a small
    value (e.g. 2000) for the main dispatch loop's read-only scans so a lock
    held past that budget raises ``OperationalError`` (caller skips the tick)
    instead of blocking the loop for 30s and tripping the stall watchdog.
    """
    # timeout=30.0 waits up to 30s for locks instead of failing immediately, and
    # is itself what installs the busy handler — `busy_timeout_ms` overrides it
    # for this connection. journal_mode is NOT re-issued here; see sqlite_util.
    # synchronous is a per-connection setting (not stored in the file header),
    # so it is set each open; NORMAL is the safe, faster choice under WAL.
    with sqlite_util.open_db(
        db_path,
        timeout=30.0,
        busy_timeout_ms=busy_timeout_ms,
        foreign_keys=False,
        synchronous="NORMAL",
        commit=True,
    ) as conn:
        yield conn


def create_task(
    conn: sqlite3.Connection,
    prompt: str = "",
    user_id: str = "",  # not a usable default — see the guard below
    source_type: str = "cli",
    conversation_token: str | None = None,
    parent_task_id: int | None = None,
    is_group_chat: bool = False,
    attachments: list[str] | None = None,
    priority: int = 5,
    scheduled_for: str | None = None,
    output_target: str | None = None,
    talk_message_id: int | None = None,
    reply_to_talk_id: int | None = None,
    reply_to_content: str | None = None,
    reply_to_message_id: int | None = None,
    # This exchange is deliberately not part of the room `conversation_token`
    # names (ISSUE-255). Written by the one caller that knows — `record_inbound`,
    # from the same answer that turned off the transcript mirror.
    withheld_from_room: bool = False,
    heartbeat_silent: bool = False,
    skip_log_channel: bool = False,
    scheduled_job_id: int | None = None,
    briefing_name: str | None = None,
    command: str | None = None,
    queue: str = "foreground",
    model: str | None = None,
    effort: str | None = None,
    brain: str | None = None,
    model_namespace: str | None = None,
    talk_delivery_token: str | None = None,
    skill: str | None = None,
    skill_args: str | None = None,
) -> int:
    """Create a new task and return its ID.

    Raises ``ValueError`` for a ``user_id`` that cannot name a directory of its
    own. The column is ``TEXT NOT NULL``, which SQLite satisfies with ``''``,
    and this function used to default the parameter to ``''`` and validate
    nothing — so an unowned task row was one omitted argument away, and every
    path derived from it collapsed to the shared parent (ISSUE-402). Every
    network-facing producer already gates on membership in ``config.users``;
    what this covers is the local entry points that do not (``istota task -u``,
    ``istota repl -u``, ``execute_task_interactive``) and the omitted argument
    itself. The path joins fail closed on their own — this is the second layer,
    at the source, so the row never exists rather than existing and binding
    nothing.
    """
    if not is_scopable_user_id(user_id):
        raise ValueError(
            f"create_task: user_id {user_id!r} cannot name a per-user directory; "
            "it must be a non-empty path component other than '.' or '..'."
        )
    # Guard against duplicate Talk messages (race between overlapping poll cycles)
    if talk_message_id is not None:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE talk_message_id = ? AND conversation_token = ?",
            (talk_message_id, conversation_token),
        ).fetchone()
        if existing:
            logger.warning(
                "Duplicate talk_message_id %d in conversation %s — "
                "task %d already exists, skipping",
                talk_message_id, conversation_token, existing[0],
            )
            return existing[0]

    cursor = conn.execute(
        """
        INSERT INTO tasks (
            prompt, command, user_id, source_type, conversation_token,
            parent_task_id, is_group_chat, attachments, priority, scheduled_for,
            output_target, talk_message_id, reply_to_talk_id, reply_to_content,
            reply_to_message_id, withheld_from_room,
            heartbeat_silent, skip_log_channel, scheduled_job_id, briefing_name,
            queue, model, effort, brain, model_namespace,
            talk_delivery_token, skill, skill_args
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            prompt,
            command,
            user_id,
            source_type,
            conversation_token,
            parent_task_id,
            1 if is_group_chat else 0,
            json.dumps(attachments) if attachments else None,
            priority,
            scheduled_for,
            output_target,
            talk_message_id,
            reply_to_talk_id,
            reply_to_content,
            reply_to_message_id,
            1 if withheld_from_room else 0,
            1 if heartbeat_silent else 0,
            1 if skip_log_channel else 0,
            scheduled_job_id,
            briefing_name,
            queue,
            model or None,
            effort or None,
            brain or None,
            model_namespace or None,
            talk_delivery_token,
            skill,
            skill_args,
        ),
    )
    task_id = cursor.fetchone()[0]
    logger.debug("Created task %d for user %s (source: %s)", task_id, user_id, source_type)
    return task_id


# Canonical SELECT/RETURNING column list for any query that reconstructs a
# `Task` via `_row_to_task`. Update this when adding a column to `tasks`;
# `_row_to_task` will then trip an `IndexError` from any SELECT that forgot
# to include the column, surfacing the omission as a test failure rather
# than a silent `None` (see commit 027eb1a — a missed `skill, skill_args`
# in `claim_task`'s RETURNING caused a 5-minute-loop production bug).
_TASK_COLUMNS = (
    "id, status, source_type, user_id, prompt, command, "
    "conversation_token, parent_task_id, is_group_chat, attachments, "
    "result, actions_taken, execution_trace, error, confirmation_prompt, "
    "priority, attempt_count, max_attempts, created_at, scheduled_for, "
    "output_target, talk_message_id, talk_response_id, reply_to_talk_id, "
    "reply_to_content, reply_to_message_id, withheld_from_room, "
    "heartbeat_silent, skip_log_channel, scheduled_job_id, "
    "briefing_name, queue, confirmed_at, selected_skills, model, effort, model_used, "
    "brain, model_namespace, talk_delivery_token, skill, skill_args"
)


def _row_to_task(row: sqlite3.Row) -> Task:
    """Convert a database row to a Task object.

    The row must include every column in `_TASK_COLUMNS`. Callers should
    use `_TASK_COLUMNS` in their SELECT/RETURNING clause; missing columns
    raise `IndexError` from `sqlite3.Row` rather than producing a silent
    `None`.
    """
    return Task(
        id=row["id"],
        status=row["status"],
        source_type=row["source_type"],
        user_id=row["user_id"],
        prompt=row["prompt"],
        command=row["command"],
        conversation_token=row["conversation_token"],
        parent_task_id=row["parent_task_id"],
        is_group_chat=bool(row["is_group_chat"]),
        attachments=json.loads(row["attachments"]) if row["attachments"] else None,
        result=row["result"],
        actions_taken=row["actions_taken"],
        execution_trace=row["execution_trace"],
        error=row["error"],
        confirmation_prompt=row["confirmation_prompt"],
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        scheduled_for=row["scheduled_for"],
        output_target=row["output_target"],
        talk_message_id=row["talk_message_id"],
        talk_response_id=row["talk_response_id"],
        reply_to_talk_id=row["reply_to_talk_id"],
        reply_to_content=row["reply_to_content"],
        reply_to_message_id=row["reply_to_message_id"],
        withheld_from_room=bool(row["withheld_from_room"]),
        heartbeat_silent=bool(row["heartbeat_silent"]),
        skip_log_channel=bool(row["skip_log_channel"]),
        scheduled_job_id=row["scheduled_job_id"],
        briefing_name=row["briefing_name"],
        queue=row["queue"],
        confirmed_at=row["confirmed_at"],
        selected_skills=row["selected_skills"],
        model=row["model"],
        effort=row["effort"],
        model_used=row["model_used"],
        brain=row["brain"],
        model_namespace=row["model_namespace"],
        talk_delivery_token=row["talk_delivery_token"],
        skill=row["skill"],
        skill_args=row["skill_args"],
    )


# A 'running' task counts as stuck (its worker presumed dead) when its liveness
# ping has gone silent — last_heartbeat older than ``heartbeat_stuck_minutes`` —
# or, when the worker never recorded a heartbeat (legacy rows, or a worker whose
# pinger never started), when it has simply been running past
# ``stuck_running_minutes``. A live worker refreshes last_heartbeat every cycle,
# so a healthy long task is never reclaimed regardless of how long it runs
# (ISSUE-112). The fragment binds two params, in this order: heartbeat window
# then started_at window — build them with ``_stuck_running_params``.
_STUCK_RUNNING_PREDICATE = (
    "((last_heartbeat IS NOT NULL "
    "AND last_heartbeat < datetime('now', ? || ' minutes')) "
    "OR (last_heartbeat IS NULL "
    "AND started_at < datetime('now', ? || ' minutes')))"
)

# Source types whose tasks are executed inline by their creator
# (`scheduler.run_task_inline`), never by a daemon worker. The daemon must not
# claim or dispatch these — a REPL turn creates a `pending` row and runs it
# in-process, so a concurrently-running daemon worker for the same user would
# otherwise claim and double-execute it (second brain run + second deferred-op
# drain). `claim_task` is the enforcement boundary; the discovery helpers below
# exclude them too so the daemon never spawns an idle worker for an inline task.
INLINE_ONLY_SOURCE_TYPES = ("repl",)
_INLINE_ONLY_IN = ", ".join(f"'{s}'" for s in INLINE_ONLY_SOURCE_TYPES)

# Per-channel gate: one active foreground task per conversation_token.
# A pending fg task is unclaimable while another fg task in the same channel is
# locked/running/pending_confirmation (the latter parks the room awaiting the
# user's confirmation, so the next queued message must wait rather than barge
# ahead — web chat's single-active-per-room + queue). Talk is unaffected: it
# cancels pending confirmations in the same poll transaction before creating the
# new task. Tasks with no conversation_token (cron, email) and background-queue
# tasks are unaffected. References the outer query's `tasks` alias.
#
# Shared verbatim between claim_task (what it will actually claim) and
# count_claimable_tasks_for_user_queue (what dispatch / the idle pre-check use
# to decide whether to spawn or poll a worker) so the count can never disagree
# with claimability — otherwise a worker spun up for a gated task busy-polls
# claim_task (and its stale-lock maintenance UPDATEs) until the gate clears.
_CLAIM_CHANNEL_GATE_SQL = """
            NOT (
                tasks.queue = 'foreground'
                AND tasks.conversation_token IS NOT NULL
                AND tasks.conversation_token != ''
                AND EXISTS (
                    SELECT 1 FROM tasks t2
                    WHERE t2.conversation_token = tasks.conversation_token
                    AND t2.queue = 'foreground'
                    AND t2.status IN ('locked', 'running', 'pending_confirmation')
                    AND t2.cancel_requested = 0
                    AND t2.id != tasks.id
                )
            )
            """


def _stuck_running_params(heartbeat_stuck_minutes: int, stuck_running_minutes: int) -> tuple:
    return (f"-{heartbeat_stuck_minutes}", f"-{stuck_running_minutes}")


def claim_task(
    conn: sqlite3.Connection,
    worker_id: str,
    max_retry_age_minutes: int = 60,
    user_id: str | None = None,
    queue: str | None = None,
    stuck_running_minutes: int = 15,
    heartbeat_stuck_minutes: int = 5,
) -> Task | None:
    """Atomically claim the next available task. Returns None if no tasks available.

    Args:
        worker_id: Unique identifier for the claiming worker.
        max_retry_age_minutes: Tasks older than this are failed instead of retried.
        user_id: If provided, only claim tasks for this user.
        queue: If provided, only claim tasks in this queue ('foreground' or 'background').
        stuck_running_minutes: Fallback stuck threshold for a 'running' task that
            never recorded a heartbeat (legacy rows). Must exceed the task
            timeout, or a healthy still-running worker — especially the in-process
            native brain, which has no killable PID — gets reclaimed and a second
            worker runs a duplicate (ISSUE-112). Callers pass
            ``task_timeout_minutes`` + a grace margin.
        heartbeat_stuck_minutes: Stuck threshold once the worker has recorded a
            heartbeat — how long last_heartbeat may go silent before the worker is
            presumed dead. Small (a few missed pings); independent of the timeout.
    """
    # First, fail old stale locks (created too long ago to be worth retrying)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stale lock)',
            locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at < datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Release recent stale locks (younger tasks get retried)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at >= datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Fail old stuck 'running' tasks (too old to be worth retrying)
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stuck running)'
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at < datetime('now', ? || ' minutes')
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Release recent stuck 'running' tasks for retry. Clear last_heartbeat too:
    # leaving the dead worker's stale heartbeat on the row would keep the
    # _STUCK_RUNNING_PREDICATE firing after the next worker re-claims and re-runs
    # it, letting a second concurrent claimer re-steal it (duplicate execution).
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            last_heartbeat = NULL, attempt_count = attempt_count + 1
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at >= datetime('now', ? || ' minutes')
        AND attempt_count < max_attempts
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Mark stuck 'running' tasks as failed if they've exhausted retries
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task stuck in running state - worker may have crashed'
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND attempt_count >= max_attempts
        """,
        _stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
    )

    # Atomically claim a task (optionally filtered by user_id and/or queue).
    # Inline-only source types (REPL) are never claimed here — their creator
    # runs them in-process via run_task_inline.
    filters = [
        "status = 'pending'",
        f"source_type NOT IN ({_INLINE_ONLY_IN})",
        "(scheduled_for IS NULL OR scheduled_for <= datetime('now'))",
    ]
    params: list = [worker_id]
    if user_id is not None:
        filters.append("user_id = ?")
        params.append(user_id)
    if queue is not None:
        filters.append("queue = ?")
        params.append(queue)

    # Per-channel single-active-foreground gate (see _CLAIM_CHANNEL_GATE_SQL).
    if queue == "foreground" or queue is None:
        filters.append(_CLAIM_CHANNEL_GATE_SQL)

    where_clause = " AND ".join(filters)

    # Reset liveness (last_heartbeat + started_at) on claim so the new owner
    # starts with a clean slate. Without this, a task re-claimed from the
    # stuck-running path carries the dead worker's stale heartbeat into its
    # running window — until the new worker's first ping lands — and a second
    # worker calling claim_task in that window re-reclaims and re-runs it
    # (the duplicate-execution race; ISSUE-112). update_task_status('running')
    # sets started_at=now immediately after, before the row can look stuck.
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'locked', locked_at = datetime('now'), locked_by = ?,
            last_heartbeat = NULL, started_at = NULL
        WHERE id = (
            SELECT id FROM tasks
            WHERE {where_clause}
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
        )
        RETURNING {_TASK_COLUMNS}
        """,
        params,
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


def get_users_with_pending_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get distinct user IDs that have pending tasks ready to run."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type NOT IN ({_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    """Get a task by ID."""
    cursor = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


# Lifecycle columns for the narrow, user-scoped task read surface behind
# `istota-skill tasks` (ISSUE-237). Deliberately NOT `_TASK_COLUMNS`: that list
# exists to rebuild a full `Task` and omits `started_at` / `completed_at`, which
# are the two fields a caller waiting on an out-of-band job actually needs. It
# also carries `prompt`, `attachments` and the whole reply/delivery block, none
# of which belong in an answer handed back to a sandboxed agent.
#
# ``conversation_token`` is included so a caller can see which room a task
# belongs to. The scope here is the user, not the room — a scheduled job's run
# and the chat asking about it are in different rooms, so a room predicate
# would break the main use — but that makes it the reader's job to notice when
# a result came from somewhere else, and it can only notice if we say.
_TASK_STATE_COLUMNS = (
    "id, status, source_type, user_id, queue, conversation_token, "
    "created_at, updated_at, started_at, completed_at, "
    "attempt_count, max_attempts, "
    "parent_task_id, scheduled_job_id, briefing_name"
)

# Prompt text is echoed back only as an identifying excerpt — enough to tell
# two of your own queued jobs apart, not a way to page prompts back out.
_TASK_PROMPT_EXCERPT_CHARS = 160


def get_task_state_for_user(
    conn: sqlite3.Connection, task_id: int, user_id: str,
) -> dict | None:
    """Read one task's lifecycle state and result, scoped to its owner.

    ``user_id`` is a mandatory ownership predicate, not an optional filter:
    this backs a read surface reachable from a task, so a caller must never be
    able to name another user's task id and learn anything about it. Returns
    ``None`` both when the task does not exist and when it belongs to someone
    else, so the surface is not an existence oracle for task ids.

    Returns a plain dict rather than a ``Task`` because the two most useful
    fields here (``started_at`` / ``completed_at``) are not on the dataclass.
    """
    cursor = conn.execute(
        f"SELECT {_TASK_STATE_COLUMNS}, result, error, "
        f"substr(prompt, 1, {_TASK_PROMPT_EXCERPT_CHARS}) AS prompt_excerpt "
        "FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def list_recent_tasks_for_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    since: str | None = None,
    parent_task_id: int | None = None,
    status: str | None = None,
    source_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List a user's own recent tasks, newest first.

    The index view for the read surface above: it answers "did the subtask /
    scheduled run I kicked off finish yet", so it carries no ``result`` column
    — a caller reads one result with ``get_task_state_for_user``. Returning
    them here would page every stored result body back through a single
    response.

    ``since`` is compared against ``created_at``, which SQLite writes as a UTC
    ``YYYY-MM-DD HH:MM:SS`` string; pass the same shape.
    """
    query = f"SELECT {_TASK_STATE_COLUMNS}, " \
            f"substr(prompt, 1, {_TASK_PROMPT_EXCERPT_CHARS}) AS prompt_excerpt " \
            "FROM tasks WHERE user_id = ?"
    params: list = [user_id]

    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if parent_task_id is not None:
        query += " AND parent_task_id = ?"
        params.append(parent_task_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if source_type:
        query += " AND source_type = ?"
        params.append(source_type)

    # id DESC, not created_at DESC: created_at has one-second granularity, so
    # two tasks queued in the same second (a parent and its subtask) would come
    # back in arbitrary order.
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    return [dict(row) for row in conn.execute(query, params).fetchall()]


_SUBTASK_DEPTH_HARD_CAP = 100


def get_subtask_depth(conn: sqlite3.Connection, task_id: int) -> int:
    """Walk parent_task_id chain and return how deep `task_id` sits.

    A user-initiated task (no parent) returns 0; its first child returns 1, etc.
    Capped at _SUBTASK_DEPTH_HARD_CAP to terminate on pathological chains;
    callers should treat the cap as "very deep, refuse further work."
    """
    depth = 0
    current = task_id
    while depth < _SUBTASK_DEPTH_HARD_CAP:
        row = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (current,),
        ).fetchone()
        if row is None or row["parent_task_id"] is None:
            return depth
        current = row["parent_task_id"]
        depth += 1
    return depth


def update_task_status(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    result: str | None = None,
    error: str | None = None,
    actions_taken: str | None = None,
    execution_trace: str | None = None,
) -> None:
    """Update task status and optionally result/error."""
    if status == "running":
        conn.execute(
            "UPDATE tasks SET status = ?, started_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )
    elif status == "completed":
        # worker_pid is cleared on every transition out of `running`: the
        # subprocess it named is gone, and `!stop` / the web cancel endpoint
        # both os.kill whatever the row holds — a stale PID the OS has since
        # recycled would send SIGTERM to an unrelated process (ISSUE-191).
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = datetime('now'), result = ?, actions_taken = ?, execution_trace = ?, worker_pid = NULL, updated_at = datetime('now') WHERE id = ?",
            (status, result, actions_taken, execution_trace, task_id),
        )
    elif status in ("failed", "cancelled"):
        # Persist the execution trace + tool descriptions alongside the error
        # so an interrupted task's intermediate output survives a reload
        # (ISSUE-183). The native brain builds a full trace even on cancel /
        # error; dropping it here left the web chat with a blank agent bubble
        # on reload (the live stream had the tools, the `tasks` row did not).
        # `completed_at` is set so `cleanup_old_tasks`' retention window can
        # reap cancelled rows (NULL `completed_at` stranded them forever) and
        # the web duration badge renders.
        #
        # `result` goes with them (ISSUE-372). The trace records the tool calls
        # and their descriptions; the model's own prose was the one thing an
        # interrupted run lost outright, and this argument was accepted and then
        # dropped on the floor here. It carries the *partial* answer, never the
        # error text — the error has its own column and the two must stay
        # distinguishable to anything reading the row back.
        #
        # `COALESCE`, not a plain assignment, and that is the load-bearing half:
        # this branch is also reached by a row that already **completed** and had
        # its answer written. `process_one_task` re-marks a completed task
        # `failed` when its email delivery fails, passing no `result` — so a
        # plain write would blank the answer with the argument's `None` default,
        # and with an email-only plan `tasks.result` is the only copy of it left
        # (ISSUE-255). Three of the six failure-branch callers pass a value and
        # three do not; the column is preserved for the ones that do not.
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = datetime('now'), "
            "result = COALESCE(?, result), error = ?, actions_taken = ?, "
            "execution_trace = ?, worker_pid = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, result, error, actions_taken, execution_trace, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )


def set_task_pending_retry(
    conn: sqlite3.Connection,
    task_id: int,
    error: str,
    retry_delay_minutes: int,
) -> None:
    """Mark task for retry after a delay.

    Clears last_heartbeat/started_at so the retried row doesn't carry the prior
    attempt's liveness into the next claim — the claim itself also resets these
    (defense in depth), but a pending row shouldn't advertise a dead worker's
    heartbeat in the meantime. worker_pid goes with them: the failed attempt's
    subprocess is dead, and leaving its number on the row lets `!stop` /
    the web cancel endpoint SIGTERM whatever the OS recycled it onto
    (ISSUE-191).
    """
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            attempt_count = attempt_count + 1,
            error = ?,
            scheduled_for = datetime('now', '+' || ? || ' minutes'),
            locked_at = NULL,
            locked_by = NULL,
            last_heartbeat = NULL,
            started_at = NULL,
            worker_pid = NULL,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (error, retry_delay_minutes, task_id),
    )


def release_task_for_restart(
    conn: sqlite3.Connection,
    task_id: int,
    error: str,
) -> None:
    """Return a running task to the queue after the daemon's own shutdown
    signal killed its subprocess.

    Under systemd's default ``KillMode=control-group`` a ``systemctl restart``
    SIGTERMs every process in the cgroup, so an in-flight task's `claude` child
    dies while the daemon shuts down gracefully — and the surviving worker
    records the corpse as an ordinary task failure (ISSUE-191). It isn't one:
    nothing about the task failed, so the attempt is **not** charged against
    ``attempt_count`` and no backoff is set. The next daemon claims it
    immediately, which is what ``recover_orphaned_tasks_on_startup`` already
    does for the SIGKILL variant of the same event.

    Bounded by ``fail_ancient_pending_tasks``: a released row that never gets
    claimed is reaped like any other stale pending task.
    """
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            error = ?,
            locked_at = NULL,
            locked_by = NULL,
            last_heartbeat = NULL,
            started_at = NULL,
            worker_pid = NULL,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (error, task_id),
    )


def set_task_confirmation(
    conn: sqlite3.Connection,
    task_id: int,
    confirmation_prompt: str,
) -> None:
    """Set task to pending confirmation status."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending_confirmation',
            confirmation_prompt = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (confirmation_prompt, task_id),
    )


def confirm_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Confirm a task that was pending confirmation."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            confirmed_at = datetime('now'),
            updated_at = datetime('now')
        WHERE id = ? AND status = 'pending_confirmation'
        """,
        (task_id,),
    )


def cancel_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Cancel a task (sets status to 'cancelled')."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (task_id,),
    )


def cancel_pending_confirmations(
    conn: sqlite3.Connection,
    conversation_token: str,
    user_id: str,
) -> int:
    """Cancel all pending_confirmation tasks for a user in a conversation.

    Called when a new task is created in the same conversation, indicating the
    user has moved on from the pending confirmation.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            updated_at = datetime('now')
        WHERE conversation_token = ?
          AND user_id = ?
          AND status = 'pending_confirmation'
        """,
        (conversation_token, user_id),
    )
    return cursor.rowcount


def get_pending_confirmation(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> Task | None:
    """
    Get a task that is pending confirmation for a conversation.

    Returns the most recent task awaiting confirmation, or None if none found.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'pending_confirmation'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_token,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


def list_pending_confirmations_for_user(
    conn: sqlite3.Connection,
    user_id: str,
) -> list[Task]:
    """Every pending_confirmation task for a user, oldest first.

    Replaced a singular `get_pending_confirmation_for_user` that returned only
    the newest. That is fine when exactly one question is open and wrong when
    several are: a bare "yes" then lands on whichever arrived last rather than
    on the one being answered, which for an untrusted-sender gate approves the
    wrong email (ISSUE-241). Returning the set is what lets a caller notice the
    ambiguity and ask which — so the singular form is deliberately gone rather
    than left around to be reached for again.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE user_id = ? AND status = 'pending_confirmation'
        ORDER BY id ASC
        """,
        (user_id,),
    )
    return [_row_to_task(row) for row in cursor.fetchall()]


def get_pending_confirmation_by_response_id(
    conn: sqlite3.Connection,
    talk_response_id: int,
) -> Task | None:
    """Get a pending_confirmation task by its Talk confirmation message ID."""
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE talk_response_id = ? AND status = 'pending_confirmation'
        LIMIT 1
        """,
        (talk_response_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_task(row)


def _decode_resource_extras(raw: object) -> dict[str, Any]:
    """Parse the extras JSON column. Falls back to {} on missing or corrupt data."""
    if raw is None or raw == "":
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("user_resources.extras contained invalid JSON; defaulting to {}")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def get_user_resources(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str | None = None,
) -> list[UserResource]:
    """Get resources accessible to a user."""
    if resource_type:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions, extras
            FROM user_resources
            WHERE user_id = ? AND resource_type = ?
            """,
            (user_id, resource_type),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions, extras
            FROM user_resources
            WHERE user_id = ?
            """,
            (user_id,),
        )

    return [
        UserResource(
            id=row["id"],
            user_id=row["user_id"],
            resource_type=row["resource_type"],
            resource_path=row["resource_path"],
            display_name=row["display_name"],
            permissions=row["permissions"],
            extras=_decode_resource_extras(row["extras"]),
        )
        for row in cursor.fetchall()
    ]


# Sentinel for add_user_resource: distinguishes "caller didn't pass extras"
# (preserve existing column on update) from "caller passed an explicit value
# including {}" (overwrite). Operators clearing extras through the CLI / web
# UI must produce a write, not a no-op.
_EXTRAS_UNCHANGED = object()


def add_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str,
    resource_path: str,
    display_name: str | None = None,
    permissions: str = "read",
    extras: "dict[str, Any] | object" = _EXTRAS_UNCHANGED,
) -> int:
    """Upsert a resource for a user.

    On conflict (user_id, resource_type, resource_path) the row's
    display_name + permissions are overwritten. ``extras`` follows
    partial-update semantics matching ``istota user ensure``: when the caller
    omits the kwarg, the existing column value is preserved; passing an
    explicit dict (including ``{}``) overwrites.
    """
    if extras is _EXTRAS_UNCHANGED:
        cursor = conn.execute(
            """
            INSERT INTO user_resources (user_id, resource_type, resource_path, display_name, permissions, extras)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT (user_id, resource_type, resource_path) DO UPDATE SET
                display_name = excluded.display_name,
                permissions = excluded.permissions
            RETURNING id
            """,
            (user_id, resource_type, resource_path, display_name, permissions),
        )
    else:
        extras_json = json.dumps(extras) if extras else None
        cursor = conn.execute(
            """
            INSERT INTO user_resources (user_id, resource_type, resource_path, display_name, permissions, extras)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, resource_type, resource_path) DO UPDATE SET
                display_name = excluded.display_name,
                permissions = excluded.permissions,
                extras = excluded.extras
            RETURNING id
            """,
            (user_id, resource_type, resource_path, display_name, permissions, extras_json),
        )
    return cursor.fetchone()[0]


def upsert_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str,
    resource_path: str,
    *,
    display_name: str | None = None,
    permissions: str = "read",
    extras: "dict[str, Any] | object" = _EXTRAS_UNCHANGED,
) -> "tuple[int, str]":
    """Idempotent resource upsert. Returns ``(resource_id, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``user_briefings.ensure_briefing`` and
    ``secrets_store.upsert_secret``. ``extras`` follows the same partial-
    update sentinel as :func:`add_user_resource`: omitting the kwarg
    preserves the existing value; passing an explicit dict (including
    ``{}``) overwrites.
    """
    existing = next(
        (r for r in get_user_resources(conn, user_id)
         if r.resource_type == resource_type and r.resource_path == resource_path),
        None,
    )

    if existing is None:
        state = "created"
    else:
        # Compute would-be value so omitted extras = preserve.
        next_extras = existing.extras if extras is _EXTRAS_UNCHANGED else extras
        same = (
            (existing.display_name or "") == (display_name or "")
            and (existing.permissions or "read") == permissions
            and (existing.extras or {}) == (next_extras or {})
        )
        state = "noop" if same else "updated"

    resource_id = add_user_resource(
        conn,
        user_id=user_id,
        resource_type=resource_type,
        resource_path=resource_path,
        display_name=display_name,
        permissions=permissions,
        extras=extras,
    )
    return resource_id, state


def delete_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_id: int,
) -> bool:
    """Delete a resource by id, scoped to user_id (web UI safety).

    Returns True if a row was removed. The user_id scope prevents one user
    from deleting another user's resource by guessing IDs from the URL.
    """
    cur = conn.execute(
        "DELETE FROM user_resources WHERE id = ? AND user_id = ?",
        (resource_id, user_id),
    )
    return cur.rowcount > 0


# Resource types retired by the modules / connected services refactor and
# the Resources sunset. Their data flows through is_module_enabled (feeds,
# money, location), the encrypted secrets table (karakeep, monarch, overland),
# CalDAV discovery (calendar), or workspace conventions (todo/reminders/notes).
# Cleaning them out of user_resources keeps stale rows from leaking into the
# executor / web UI.
#
# todo_file / reminders_file are intentionally NOT here, but no longer because
# anything reads them: the briefings module owns reminder selection via a
# source ``path``, the fetcher that read reminders_file has been deleted, and
# todo_file never had a reader at all. They stay out of the auto-clean set
# because deleting a user's rows is a data migration, not dead-code removal —
# an operator removes them by hand. Adding them here is the follow-up.
_OBSOLETE_RESOURCE_TYPES = (
    "feeds", "money", "monarch", "moneyman", "karakeep", "overland",
    "calendar", "email_folder", "notes_folder",
)


def cleanup_obsolete_resources(db_path: Path) -> int:
    """Delete rows from ``user_resources`` whose type is no longer recognized.

    Idempotent: a missing DB or table is treated as a no-op. Returns the
    number of rows removed; intended to run once at scheduler startup after
    the secrets-store import has absorbed the credentials.
    """
    if db_path is None or not Path(db_path).exists():
        return 0
    placeholders = ",".join("?" * len(_OBSOLETE_RESOURCE_TYPES))
    try:
        with get_db(db_path) as conn:
            cur = conn.execute(
                f"DELETE FROM user_resources WHERE resource_type IN ({placeholders})",
                _OBSOLETE_RESOURCE_TYPES,
            )
            return cur.rowcount or 0
    except sqlite3.OperationalError:
        return 0


def get_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> str | None:
    """Get the last run timestamp for a config-based briefing."""
    cursor = conn.execute(
        "SELECT last_run_at FROM briefing_state WHERE user_id = ? AND briefing_name = ?",
        (user_id, briefing_name),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> None:
    """Set the last run timestamp for a config-based briefing.

    Truncates seconds to :00 so croniter (minute resolution) never computes
    a next-fire time within the same minute, preventing double-fires.
    """
    conn.execute(
        """
        INSERT INTO briefing_state (user_id, briefing_name, last_run_at)
        VALUES (?, ?, strftime('%Y-%m-%d %H:%M:00', 'now'))
        ON CONFLICT (user_id, briefing_name) DO UPDATE SET
            last_run_at = strftime('%Y-%m-%d %H:%M:00', 'now')
        """,
        (user_id, briefing_name),
    )


@dataclass
class ConversationMessage:
    id: int
    prompt: str
    result: str
    created_at: str
    actions_taken: str | None = None
    source_type: str = "talk"
    user_id: str | None = None
    # The envelope sender of an email-sourced turn, when it is NOT one of the
    # task user's own addresses (ISSUE-226). `user_id` is the istota user the
    # mail was routed *to*, so it cannot answer "who wrote this"; a formatter
    # that labels the turn with it asserts the principal said something an
    # external contact said. None means "attribute to `user_id` as usual".
    external_sender: str | None = None


@dataclass
class TalkMessage:
    """A message from the Talk API, used for Talk-based conversation context."""
    message_id: int          # Talk message ID
    actor_id: str            # Nextcloud username
    actor_display_name: str  # Display name from API
    is_bot: bool             # actor_id == bot_username
    content: str             # cleaned text (placeholders resolved)
    timestamp: int           # unix timestamp
    actions_taken: str | None  # from DB, only for bot result messages
    message_role: str        # "user" | "bot_result" | "scheduled"
    task_id: int | None      # parsed from referenceId


# Source types whose *complete* history the canonical `messages` store is
# guaranteed to hold, and which therefore gate the caught-up dual-read.
# Scheduled/cron, briefing, and heartbeat posts are one-directional bot output
# (assistant-only, no user turn) and don't count toward the completeness check.
#
# `email` is deliberately absent even though it is now mirrored as a
# user+assistant pair too (ISSUE-136). The store holds email turns only from
# that change forward: an email task completed before it — or under a `thread`
# reply-routing policy, whose email-only plan never reached `_store_room_turn` —
# has no assistant row and never will. Counting those would make the gap probe
# below return False for the room forever, pinning it to the legacy `tasks` path
# until retention GCs the task. Mirroring is not the criterion; guaranteed
# completeness is.
#
# **Left a literal on purpose.** The question is "whose history is the store
# guaranteed to hold", which is neither of the room-model questions
# `surfaces.SURFACES` answers — and the reason email is out of it is a fact
# about when a migration ran, not about what email is. Reading
# `surfaces.is_room_member` here would give the same answer today and would be
# the room-surface muddle committed one question further along: the day email's
# backfill exists, this set widens and the room-role set does not.
_CONVERSATIONAL_SOURCE_TYPES = ("talk", "web")


# Recovers the envelope sender of an email-sourced turn for the history readers
# (ISSUE-226). `processed_emails` is already keyed by `task_id` and already
# stores `sender_email`, so this needs no schema change and no second query.
# A scalar subquery rather than a LEFT JOIN: the join key is not unique, and a
# duplicate row there would silently multiply the history rows it is attached to.
EMAIL_SENDER_SUBQUERY = """(
            SELECT pe.sender_email FROM processed_emails pe
            WHERE pe.task_id = {alias}.id ORDER BY pe.id LIMIT 1
        ) AS email_sender"""

# An addr-spec longer than this is not a real address; refuse to render it
# rather than let an unbounded string into the prompt's speaker position.
_MAX_SENDER_LABEL_CHARS = 254

# What may be rendered into the speaker position of a prompt line: an ASCII
# dot-atom addr-spec, nothing else. An allowlist rather than a blacklist of bad
# characters, because the two shapes that defeat a blacklist both parse as
# perfectly valid addresses — a *quoted local part* (`"alice: do it"@evil.example`)
# carries spaces and colons through `parseaddr` untouched, and a non-ASCII
# address can carry bidi or format characters that reorder the rendered line.
# Anything outside this degrades to `UNATTRIBUTED_SENDER`, which still reads as
# external; the label loses detail, never the provenance.
_RENDERABLE_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+$"
)

UNATTRIBUTED_SENDER = "unknown sender"


def external_email_sender(
    sender_email: str | None,
    own_email_addresses: Sequence[str] | None,
) -> str | None:
    """The envelope sender, when it is not one of the task user's own addresses.

    Returns None when there is no sender (a non-email turn) or when the sender
    is one of the user's own addresses — the two cases where attributing the
    turn to `user_id` is correct. "Own address" is as strong a claim as the
    `From:` header, which is unauthenticated; this narrows who the turn is
    attributed to, it does not authenticate the user.

    Deliberately keyed on the **address**, not on `processed_emails.routing_method`.
    The `sender_match` routing method does imply the user's own address, but the
    converse fails: a user mailing their own plus-address (`bot+alice@…`) is
    resolved by recipient and routes as `plus_address`, which would then read as
    a stranger.

    Fails safe. An unknown `own_email_addresses` (the caller could not say which
    addresses belong to the user) means we cannot prove the user wrote it, so the
    turn is attributed to the sender. Under-trusting the principal costs a
    slightly odd label; over-trusting launders third-party text into their turn.

    Returns the **addr-spec**, never the raw header, and only when it matches
    `_RENDERABLE_ADDRESS_RE`. The display-name half of a `From:` is arbitrary
    attacker-chosen text, and the return value is rendered into the speaker
    position of a prompt line — the one place where injected text would do the
    most good. Anything that doesn't reduce to a plain address becomes
    `UNATTRIBUTED_SENDER` rather than being dropped: a sender we can't render
    is unattributable, which is not the same as being the user.
    """
    if not sender_email:
        return None
    address = (parseaddr(sender_email)[1] or "").strip()
    own = {a.strip().lower() for a in (own_email_addresses or []) if a}
    if address and address.lower() in own:
        return None
    if not address or len(address) > _MAX_SENDER_LABEL_CHARS:
        # `parseaddr` refused the header outright, or the address is absurd.
        # Deliberately no fallback to the raw header — that was the hole: any
        # header holding an `@` reached the label verbatim.
        return UNATTRIBUTED_SENDER
    if not _RENDERABLE_ADDRESS_RE.match(address):
        return UNATTRIBUTED_SENDER
    return address


def _external_sender_for_row(
    row: sqlite3.Row,
    user_email_addresses: "Mapping[str, Sequence[str]] | None",
) -> str | None:
    """`external_email_sender` for one history row, keyed on the row's own user.

    A room is shared — one token, one transcript, several members (ISSUE-134) —
    so the turns a reader returns are not all the requesting user's. Checking a
    co-member's email turn against the *requester's* addresses would mark it
    external and throw away a real identity, when `user_id` names them correctly.
    """
    if user_email_addresses is None:
        own: Sequence[str] | None = None
    else:
        own = user_email_addresses.get(row["user_id"] or "", [])
    return external_email_sender(row["email_sender"], own)


def email_sender_for_task(conn: sqlite3.Connection, task_id: int) -> str | None:
    """The recorded envelope sender for one task, or None if it wasn't email.

    The single-row counterpart to `EMAIL_SENDER_SUBQUERY`, for callers that
    build a `ConversationMessage` from an already-fetched `Task`.
    """
    row = conn.execute(
        "SELECT sender_email FROM processed_emails WHERE task_id = ? "
        "ORDER BY id LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["sender_email"] if row else None


def own_addresses_without_config(
    conn: sqlite3.Connection, user_id: str | None,
) -> list[str]:
    """A user's own email addresses, recovered from the database alone.

    `Config.users[uid].email_addresses` is the real answer, but two author
    callers cannot reach a `Config` — the `messages_author_v1` backfill (which
    runs under `init_db`) and, when its caller did not supply one, the
    confirmation-approval mirror. Getting this wrong is not cosmetic: too narrow
    a list labels the user's *own* mail with their own address as an external
    speaker, and the backfill writes that permanently.

    So it unions two sources:

    - `user_profiles.email_addresses`. Incomplete on its own — config is the
      union of the TOML `[users.X]` block and this row, and nothing seeds this
      row from TOML, so a purely TOML-configured deployment has none of its
      addresses here.
    - every distinct `processed_emails.sender_email` this user has received
      under `routing_method = 'sender_match'`. That route is *defined* by the
      `From:` matching one of the user's configured addresses, so each such row
      is the router having already recorded "this address is theirs" — against
      the full config, whichever file it came from. It is a config-free proxy
      for the part `user_profiles` cannot see.

    Still not a guarantee: a TOML-only user who has never had a `sender_match`
    mail contributes nothing to either source. That residue is the accepted
    limit of a config-free resolver, and it is why every caller that *can* pass
    a config does.
    """
    if not user_id:
        return []
    found: list[str] = []
    try:
        row = conn.execute(
            "SELECT email_addresses FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # table not created yet (very early migration order)
    if row and row["email_addresses"]:
        try:
            parsed = json.loads(row["email_addresses"])
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            found.extend(a for a in parsed if isinstance(a, str))
    try:
        rows = conn.execute(
            "SELECT DISTINCT sender_email FROM processed_emails "
            "WHERE user_id = ? AND routing_method = 'sender_match'",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        # Stored as the raw envelope sender, so reduce to the addr-spec —
        # `external_email_sender` compares against addr-specs.
        address = (parseaddr(r["sender_email"] or "")[1] or "").strip()
        if address:
            found.append(address)
    return found


def author_for_email_task(
    conn: sqlite3.Connection,
    task_id: int,
    user_id: str | None,
    own_addresses: Sequence[str] | None = None,
) -> tuple[str | None, str | None]:
    """`(author_user_id, author_label)` for a turn, resolved from the DB.

    The counterpart to `transport.ingest.resolve_author` for callers that reach
    a task rather than an inbound message. Same rule: an external sender becomes
    a sanitized label and no user id, anything else is the task's own user.

    `own_addresses` is that user's own addresses. **Pass
    `Config.users[uid].email_addresses` whenever a config is in scope** — it is
    the authoritative list. Omitting it falls back to
    `own_addresses_without_config`, which is a best effort with a documented
    residue; see there.

    `routing_method == 'sender_match'` short-circuits ahead of the address
    comparison either way, because that route is defined by the own-address
    match, so it is already the answer.

    A task with no `processed_emails` row is not an email turn (or predates the
    ledger); it belongs to its user. Raises nothing that `own_addresses` would —
    a missing `processed_emails` table propagates `sqlite3.OperationalError` to
    the caller, both of which run inside a broad handler.
    """
    row = conn.execute(
        "SELECT sender_email, routing_method FROM processed_emails "
        "WHERE task_id = ? ORDER BY id LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return (user_id or None), None
    if (row["routing_method"] or "") == "sender_match":
        return (user_id or None), None
    if own_addresses is None:
        own_addresses = own_addresses_without_config(conn, user_id)
    label = external_email_sender(row["sender_email"], own_addresses)
    if label:
        return None, label
    return (user_id or None), None


def get_conversation_history(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    limit: int = 10,
    exclude_source_types: list[str] | None = None,
    user_email_addresses: Mapping[str, Sequence[str]] | None = None,
) -> list[ConversationMessage]:
    """
    Get completed conversation history for a conversation token.

    Returns the most recent N completed tasks (oldest-first order),
    excluding the current task if specified.

    Reads from the canonical `messages` store (unified Talk/web room sync) when
    that store is caught up to the latest completed task for the token,
    otherwise falls back to the legacy `tasks` reconstruction. The dual-read is
    self-healing: until live assistant-message writes land for every surface,
    any token whose newest completed turn isn't yet mirrored into `messages`
    transparently uses the `tasks` path, so context never goes stale.

    Args:
        exclude_source_types: If provided, exclude tasks with these source_types
            from the history (e.g. ["scheduled", "briefing", "heartbeat"]).
        user_email_addresses: Maps each user id to that user's own email
            addresses, used to decide whether an email-sourced turn was authored
            by them or by an external contact (ISSUE-226). Keyed per user rather
            than taking one list, because a shared room's turns are not all the
            requesting user's. Omitting it fails safe — every email turn is then
            attributed to its envelope sender.
    """
    if _messages_caught_up(conn, conversation_token):
        return _conversation_history_from_messages(
            conn, conversation_token, exclude_task_id, limit, exclude_source_types,
            user_email_addresses,
        )
    return _conversation_history_from_tasks(
        conn, conversation_token, exclude_task_id, limit, exclude_source_types,
        user_email_addresses,
    )


def _conversation_history_from_tasks(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None,
    limit: int,
    exclude_source_types: list[str] | None,
    user_email_addresses: Mapping[str, Sequence[str]] | None = None,
) -> list[ConversationMessage]:
    """Legacy path: reconstruct history from completed `tasks` rows."""
    # `withheld_from_room` excludes an exchange that keeps this token for context
    # but is deliberately not part of the room (ISSUE-255): a self-addressed
    # thread reply. The `messages` path needs no equivalent — a withheld turn was
    # never written there, which is the half ISSUE-254 already closed. This
    # fallback is where the quoted chain was still being charged to every later
    # task in the room, and it is not a rare path: it serves any room with no
    # completed talk/web task left in `tasks`, i.e. a mail-only room, or one
    # whose last chat turn aged past `task_retention_days`.
    query = f"""
        SELECT id, prompt, result, created_at, actions_taken, source_type, user_id,
               {EMAIL_SENDER_SUBQUERY.format(alias="tasks")}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND COALESCE(withheld_from_room, 0) = 0
    """
    params: list = [conversation_token]

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    # Get most recent N, then reverse for oldest-first order
    # Use id as tiebreaker for same-second timestamps
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    # Return in oldest-first order
    return [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            external_sender=_external_sender_for_row(row, user_email_addresses),
        )
        for row in reversed(rows)
    ]


def _conversation_history_from_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None,
    limit: int,
    exclude_source_types: list[str] | None,
    user_email_addresses: Mapping[str, Sequence[str]] | None = None,
) -> list[ConversationMessage]:
    """Unified path: re-pair `messages` user/assistant rows (keyed on task_id)
    back into the (prompt, result) ConversationMessage shape callers expect.

    The user row and assistant row of one turn share a `task_id`; the join to
    `tasks` recovers per-task metadata (source_type, user_id, actions_taken) the
    role/body-only message rows don't carry, and applies the same
    completed/result-present + exclusion filters as the legacy path. An in-flight
    turn (user row, no assistant row yet) is excluded by the inner join, exactly
    as the `result IS NOT NULL` filter excludes it today. `id` stays the task id
    so reply-parent / memory-dedup callers keyed on it are unaffected.
    """
    query = f"""
        SELECT t.id AS id, mu.body AS prompt, ma.body AS result,
               t.created_at AS created_at, t.actions_taken AS actions_taken,
               t.source_type AS source_type, t.user_id AS user_id,
               {EMAIL_SENDER_SUBQUERY.format(alias="t")}
        FROM messages mu
        JOIN messages ma
          ON ma.room_token = mu.room_token AND ma.task_id = mu.task_id
             AND ma.role = 'assistant'
        JOIN tasks t ON t.id = mu.task_id
        WHERE mu.room_token = ? AND mu.role = 'user'
          AND t.status = 'completed'
    """
    params: list = [conversation_token]

    if exclude_task_id is not None:
        query += " AND t.id != ?"
        params.append(exclude_task_id)

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND t.source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    query += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            external_sender=_external_sender_for_row(row, user_email_addresses),
        )
        for row in reversed(rows)
    ]


def _messages_caught_up(conn: sqlite3.Connection, conversation_token: str) -> bool:
    """True when the canonical `messages` store can authoritatively serve a
    token's history: there is at least one completed turn and *every* completed
    task (with a result) for the token has its assistant row present in
    `messages`.

    This is a completeness check, not a newest-only check. Keying solely on the
    single newest task (the original implementation) made the dual-read
    all-or-nothing: once the latest turn was mirrored, the reader switched
    entirely to `messages` and silently dropped any *older* turn that wasn't yet
    mirrored — the exact state left by a partial migration or a mid-rollout
    window. A single missing assistant row now keeps the reader on the
    always-complete `tasks` path instead of truncating context to the mirrored
    subset.

    Cheap (one scalar + one bounded existence probe). Until live assistant
    writes land for every completed turn, this returns False and the caller
    falls back to `tasks` — no staleness during rollout.

    Scoped to *conversational* source types (talk/web). The dual-read protects
    conversational history from going stale; scheduled/cron and briefing posts
    aren't conversational turns, are never re-paired into history by
    `_conversation_history_from_messages` (they carry no user row), and a silent
    NO_ACTION tick deliberately has no message row at all. Letting them gate the
    caught-up check would peg any room with a cron job to the `tasks` path
    forever (and re-expose the dormant-room history loss once those tasks are
    GC'd). `_CONVERSATIONAL_SOURCE_TYPES` keys the check on the turns the store
    actually mirrors as user+assistant pairs."""
    placeholders = ", ".join("?" for _ in _CONVERSATIONAL_SOURCE_TYPES)
    types = tuple(_CONVERSATIONAL_SOURCE_TYPES)
    row = conn.execute(
        f"SELECT MAX(id) AS mx FROM tasks "
        f"WHERE conversation_token = ? AND status = 'completed' "
        f"AND result IS NOT NULL AND source_type IN ({placeholders})",
        (conversation_token, *types),
    ).fetchone()
    latest = row["mx"] if row else None
    if latest is None:
        return False  # no completed conversational history -> tasks path returns []
    # Any completed conversational turn missing its assistant row -> not caught
    # up -> fall back.
    gap = conn.execute(
        f"SELECT 1 FROM tasks t "
        f"WHERE t.conversation_token = ? AND t.status = 'completed' "
        f"  AND t.result IS NOT NULL AND t.source_type IN ({placeholders}) "
        f"  AND NOT EXISTS ("
        f"    SELECT 1 FROM messages m "
        f"    WHERE m.room_token = t.conversation_token "
        f"      AND m.task_id = t.id AND m.role = 'assistant'"
        f"  ) LIMIT 1",
        (conversation_token, *types),
    ).fetchone()
    return gap is None


def scheduled_assistant_body(heartbeat_silent: bool, result: str) -> str | None:
    """The transcript body for a scheduled (cron) job's assistant turn, or None
    when the turn was never delivered and must be omitted.

    Mirrors the scheduler's silent ACTION/NO_ACTION handling so a transcript
    (live write or backfill) matches what was actually posted: a silent job's
    `NO_ACTION:` tick posted nothing (omit), an `ACTION: X` posted "X" (store
    stripped), anything without a prefix posted as-is (fail-safe). Non-silent
    jobs post their raw result unchanged. Single source of truth — the scheduler
    delivery path (`_strip_action_prefix`) delegates here."""
    if not heartbeat_silent:
        return result
    if result.startswith("ACTION:"):
        return result[len("ACTION:"):].strip()
    idx = result.find("\nACTION:")
    if idx != -1:
        return result[idx + len("\nACTION:"):].strip()
    if "NO_ACTION:" in result:
        return None
    return result


def _backfill_turns_for(conn: sqlite3.Connection, where: str, params: tuple) -> int:
    """Shared transcript backfill: fold completed `tasks` rows matching `where`
    into the canonical `messages` store. One user row (body=prompt) + one
    assistant row (body=result) per conversational turn; a *scheduled* job
    contributes the assistant post only, body-normalized via
    `scheduled_assistant_body` (its synthetic cron prompt was never
    user-authored, so no user row, and a NO_ACTION tick is omitted entirely).
    Idempotent via the partial unique index (room_token, origin_surface, role,
    task_id). Returns rows inserted."""
    rows = conn.execute(
        f"SELECT id, conversation_token, prompt, result, source_type, "
        f"heartbeat_silent, created_at FROM tasks "
        f"WHERE {where} AND status = 'completed' AND result IS NOT NULL",
        params,
    ).fetchall()
    inserted = 0
    for r in rows:
        token = r["conversation_token"]
        st = r["source_type"]
        created = r["created_at"]
        if st == "scheduled":
            body = scheduled_assistant_body(bool(r["heartbeat_silent"]), r["result"])
            if body is None:
                continue  # never-delivered NO_ACTION tick
            inserted += _insert_recovered_message(
                conn, token, "assistant", body, r["id"], created, origin_surface=st,
            )
            continue
        inserted += _insert_recovered_message(
            conn, token, "user", r["prompt"], r["id"], created, origin_surface=st,
        )
        inserted += _insert_recovered_message(
            conn, token, "assistant", r["result"], r["id"], created, origin_surface=st,
        )
    return inserted


def backfill_room_messages_from_tasks(
    conn: sqlite3.Connection, conversation_token: str,
) -> int:
    """Populate the canonical `messages` store from completed `tasks` for one
    room token. See `_backfill_turns_for` for the per-turn shape."""
    return _backfill_turns_for(
        conn, "conversation_token = ?", (conversation_token,),
    )


def backfill_room_messages_from_talk_cache(
    conn: sqlite3.Connection, conversation_token: str,
) -> int:
    """Recover a room's durable transcript from the Talk message cache.

    The web transcript is rebuilt from the canonical `messages` store, but for a
    room whose conversation predates the unified-room-sync migration (or the
    task-retention window) the originating `tasks` rows are gone — the only
    surviving copy of those turns is `talk_messages`. This folds them in: one
    user row (the prompt) + one assistant row (the result) per completed Talk
    turn, keyed on the task id parsed from the bot result's
    `istota:task:<id>:result` reference. Idempotent via the messages unique
    index. Returns the number of message rows inserted.

    A turn is reconstructed from the cache's shape (message_id ascending):

        [human  comment]            -> the prompt
        [bot    :ack    comment]    -> skipped
        [bot    system  "edited"]   -> skipped
        [bot    :result comment]    -> the answer, carries the task id

    The prompt is the nearest preceding human comment before the result; an
    unpaired result (its prompt predates the cache window) yields the assistant
    row alone. Failed/cancelled turns have no `:result` cache row, so this only
    recovers completed turns — which is exactly the set task-retention GCs.
    """
    rows = conn.execute(
        "SELECT message_id, actor_id, message_type, reference_id, message_text, "
        "message_parameters, timestamp FROM talk_messages "
        "WHERE conversation_token = ? AND deleted = 0 "
        "ORDER BY message_id ASC",
        (conversation_token,),
    ).fetchall()
    if not rows:
        return 0
    # The bot is whoever authors the istota:task:* references.
    bot_actor: str | None = None
    for r in rows:
        if (r["reference_id"] or "").startswith("istota:task:"):
            bot_actor = r["actor_id"]
            break
    if bot_actor is None:
        return 0  # no bot turns cached -> nothing to recover

    def _iso(ts) -> str | None:
        try:
            return datetime.fromtimestamp(
                int(ts), tz=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OSError, OverflowError):
            return None

    # Resolve Talk rich-object placeholders ({file}, {mention-…}, polls, …)
    # against the cached messageParameters before folding text into the durable
    # store — the cache holds the *raw* body, so without this the recovered
    # transcript leaks literal placeholder tokens to the web UI (ISSUE-132). The
    # live inbound path already resolves; only this cache-recovery path didn't.
    from .talk import clean_message_content

    def _resolved(row) -> str:
        params = row["message_parameters"]
        if params:
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        else:
            params = {}
        return clean_message_content(
            {"message": row["message_text"] or "", "messageParameters": params}
        )

    inserted = 0
    pending_user: tuple[str, object] | None = None  # (text, ts), unconsumed
    for r in rows:
        if r["message_type"] != "comment":
            continue  # system notices ("You edited a message"), etc.
        ref = r["reference_id"] or ""
        is_bot_ref = ref.startswith("istota:task:")
        if r["actor_id"] != bot_actor and not is_bot_ref:
            # A human comment — the candidate prompt for the next bot result.
            pending_user = (_resolved(r), r["timestamp"])
            continue
        if is_bot_ref and ref.endswith(":result"):
            parts = ref.split(":")
            try:
                task_id = int(parts[2])
            except (IndexError, ValueError):
                continue
            # User row first so id/created_at order matches the conversation.
            if pending_user is not None:
                inserted += _insert_recovered_message(
                    conn, conversation_token, "user",
                    pending_user[0], task_id, _iso(pending_user[1]),
                )
                pending_user = None
            inserted += _insert_recovered_message(
                conn, conversation_token, "assistant",
                _resolved(r), task_id, _iso(r["timestamp"]),
            )
        # ack rows (:ack) and any other bot comment fall through (skipped).
    return inserted


def _insert_recovered_message(
    conn: sqlite3.Connection, token: str, role: str, body: str,
    task_id: int, created_at: str | None, origin_surface: str = "talk",
) -> int:
    """INSERT OR IGNORE one recovered turn message with an explicit historical
    `created_at`. Idempotent via the (room_token, origin_surface, role,
    task_id) unique index. Returns 1 if a row was inserted, else 0."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(room_token, role, body, task_id, origin_surface, created_at) "
        "VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
        (token, role, body, task_id, origin_surface, created_at),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def get_previous_tasks(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    limit: int = 3,
    exclude_source_types: list[str] | None = None,
    user_email_addresses: Mapping[str, Sequence[str]] | None = None,
) -> list[ConversationMessage]:
    """
    Get the most recent completed tasks in a conversation.

    Deliberately re-surfaces recent tasks whose ``source_type`` the primary
    ``get_conversation_history`` excludes (e.g. ``scheduled`` / ``briefing`` cron
    output the user may reference), so they stay available in the email /
    Talk-API-fallback context builder. ``exclude_source_types`` hard-excludes
    types that must NOT be re-surfaced even here — non-conversational internal
    artifacts (``subtask``'s synthetic orchestration prompt, throwaway
    ``heartbeat`` posts) that would otherwise read back as prior user
    conversation (canonical-room-transcript spec: this is the ``get_previous_tasks``
    half of the LLM-context isolation invariant, complementing
    ``get_conversation_history``'s ``exclude_source_types``). Returns up to
    ``limit`` tasks in oldest-first order. ``user_email_addresses`` drives the
    email-sender attribution described on ``get_conversation_history``.

    Excludes ``withheld_from_room`` for the same reason the history reader does
    (ISSUE-255) — and it matters more here, not less. This re-surfacing path runs
    on **every** task in the room, with no ``_messages_caught_up`` gate above it,
    so without the filter a withheld exchange reached LLM context even for a room
    whose history reads cleanly from ``messages``.
    """
    query = f"""
        SELECT id, prompt, result, created_at, actions_taken, source_type, user_id,
               {EMAIL_SENDER_SUBQUERY.format(alias="tasks")}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND COALESCE(withheld_from_room, 0) = 0
    """
    params: list = [conversation_token]

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    results = [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            external_sender=_external_sender_for_row(row, user_email_addresses),
        )
        for row in rows
    ]
    # Return in oldest-first order (query fetches newest-first)
    results.reverse()
    return results


def get_task_metadata_for_context(
    conn: sqlite3.Connection,
    task_ids: list[int],
) -> dict[int, dict]:
    """Batch lookup of task metadata for Talk-based context enrichment.

    Given a list of task IDs (parsed from referenceIds in Talk messages),
    returns a dict mapping task_id to {"actions_taken": ..., "source_type": ...}.
    """
    if not task_ids:
        return {}

    placeholders = ", ".join("?" for _ in task_ids)
    query = f"""
        SELECT id, actions_taken, source_type
        FROM tasks
        WHERE id IN ({placeholders})
        AND status = 'completed'
    """
    cursor = conn.execute(query, task_ids)
    return {
        row["id"]: {
            "actions_taken": row["actions_taken"],
            "source_type": row["source_type"],
        }
        for row in cursor.fetchall()
    }


def log_task(
    conn: sqlite3.Connection,
    task_id: int,
    level: str,
    message: str,
) -> None:
    """Add a log entry for a task."""
    conn.execute(
        "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
        (task_id, level, message),
    )


def get_task_logs(
    conn: sqlite3.Connection,
    task_id: int,
    level: str | None = None,
) -> list[dict]:
    """Get logs for a task."""
    if level:
        cursor = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? AND level = ? ORDER BY timestamp",
            (task_id, level),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        )
    return [dict(row) for row in cursor.fetchall()]


# ============================================================================
# Task event stream (task-event-streaming spec)
# ============================================================================


def get_task_events(
    conn: sqlite3.Connection,
    task_id: int,
    since_seq: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Return a task's events with ``seq > since_seq``, oldest first.

    ``payload`` is decoded from JSON into a dict. Used by the web SSE generator
    and the admin task-detail view — a range scan on the ``(task_id, seq)``
    index, fast regardless of table size.
    """
    sql = (
        "SELECT id, task_id, seq, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND seq > ? ORDER BY seq"
    )
    params: list = [task_id, since_seq]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = conn.execute(sql, params)
    events = []
    for row in cursor.fetchall():
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        events.append(d)
    return events


def get_max_task_event_seq(conn: sqlite3.Connection, task_id: int) -> int:
    """The highest ``seq`` written for a task, or 0 if it has no events.

    Lets a retry's fresh ``EventWriter`` resume the counter instead of restarting
    at 1 — keeping ``seq`` monotonic across attempts so a watching web client's
    resume cursor stays valid (the log is no longer wiped between attempts) and
    UNIQUE(task_id, seq) never collides.
    """
    row = conn.execute(
        "SELECT MAX(seq) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row[0] or 0) if row else 0


def has_task_event_kind(conn: sqlite3.Connection, task_id: int, kind: str) -> bool:
    """Whether a task's log already holds an event of this kind.

    The log spans every attempt — nothing wipes it between retries, which is why
    ``EventWriter`` resumes its ``seq`` rather than restarting at 1 — so this
    answers "did an earlier attempt already say this". That is what keeps a
    once-per-turn notice from being repeated by the retry ladder (ISSUE-361).

    Meaningless for a kind something prunes (``text_delta``, ``thinking``,
    ``confirmation``, ``done`` — see ``delete_task_events_by_kind``): a pruned
    row reads as never emitted.
    """
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
        (task_id, kind),
    ).fetchone()
    return row is not None


def delete_task_events(conn: sqlite3.Connection, task_id: int) -> int:
    """Delete all events for a task. Returns the row count.

    Nothing in the daemon calls this today, and that is the intended state: a
    task's event log spans every attempt, so the live stream survives a retry
    (``EventWriter`` resumes ``seq`` via ``get_max_task_event_seq`` rather than
    resetting to 1) and a confirmed re-run keeps the parked attempt's work
    instead of erasing it (ISSUE-235 — ``confirmations.approve`` prunes that
    attempt's ``confirmation``/``done`` by kind, and nothing else). Retention is
    the one thing that still drops these rows wholesale, in bulk SQL over the
    whole expired set (``cleanup_old_tasks``), not one task at a time here.

    Kept as the tested single-task primitive. Before reaching for it, note that
    ``task_events`` is the only durable record of a task parked at
    ``pending_confirmation``: the park path persists no ``execution_trace``.
    """
    cursor = conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    return cursor.rowcount


def delete_task_events_by_kind(
    conn: sqlite3.Connection, task_id: int, kind: str,
) -> int:
    """Delete a task's events of one kind only. Returns the row count.

    Used to prune ephemeral ``text_delta`` rows for stream surfaces once the
    canonical ``result`` has been emitted (web-chat streaming): the deltas were a
    cosmetic live preview, so steady state retains zero of them. Gaps in ``seq``
    are harmless — SSE resume is ``seq > last``. Mirrors ``delete_task_events``'
    connection-handling convention (caller supplies the connection)."""
    cursor = conn.execute(
        "DELETE FROM task_events WHERE task_id = ? AND kind = ?", (task_id, kind),
    )
    return cursor.rowcount


def append_task_event(
    conn: sqlite3.Connection, task_id: int, kind: str, payload: dict | None = None,
) -> int | None:
    """Append a single event to a task's log from *outside* the running worker.

    The worker owns an in-memory ``EventWriter`` that increments ``seq`` per
    emit; a foreign writer (the ``!steer`` command, running in the poller / web
    process) can't share that counter, so it computes the next ``seq`` atomically
    as ``MAX(seq)+1`` in one statement and inserts. A rare collision with the
    live worker's concurrent insert (both picking the same ``seq``) raises
    ``IntegrityError`` on ``UNIQUE(task_id, seq)``; we retry a few times, then
    give up (best-effort, exactly like ``EventWriter._write_to_db``). Returns the
    assigned ``seq`` on success, ``None`` if it couldn't be persisted.
    """
    import json as _json

    payload_json = _json.dumps(payload or {}, default=str)
    for _ in range(5):
        try:
            row = conn.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) "
                "VALUES (?, "
                "  (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?), "
                "  ?, ?) "
                "RETURNING seq",
                (task_id, task_id, kind, payload_json),
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else None
        except sqlite3.IntegrityError:
            # Seq collided with the live worker's concurrent emit — recompute.
            continue
    logger.debug("append_task_event gave up after seq collisions (task=%s)", task_id)
    return None


# ---------------------------------------------------------------------------
# Mid-flight steering (`!steer`)
# ---------------------------------------------------------------------------


@dataclass
class Steer:
    id: int
    task_id: int
    seq: int
    text: str
    user_id: str
    source: str
    status: str
    created_at: str
    consumed_at: str | None


def _row_to_steer(row: sqlite3.Row) -> Steer:
    return Steer(
        id=row["id"],
        task_id=row["task_id"],
        seq=row["seq"],
        text=row["text"],
        user_id=row["user_id"],
        source=row["source"],
        status=row["status"],
        created_at=row["created_at"],
        consumed_at=row["consumed_at"],
    )


def add_task_steer(
    conn: sqlite3.Connection, task_id: int, text: str, user_id: str, source: str,
) -> int:
    """Insert a ``pending`` steer for a running task. Returns the new row id.

    ``seq`` is per-task monotonic (``MAX(seq)+1``), computed atomically in the
    INSERT so concurrent steers on the same task can't collide. Commits in its
    own transaction — the write is a cheap, non-blocking control signal, like
    ``!stop``'s ``cancel_requested`` flip.
    """
    row = conn.execute(
        "INSERT INTO task_steers (task_id, seq, text, user_id, source) "
        "VALUES (?, "
        "  (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_steers WHERE task_id = ?), "
        "  ?, ?, ?) "
        "RETURNING id",
        (task_id, task_id, text, user_id, source),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def claim_pending_steers(conn: sqlite3.Connection, task_id: int) -> list[Steer]:
    """Atomically flip a task's ``pending`` steers to ``consumed`` and return them.

    Ordered by ``seq`` (oldest first). The ``UPDATE ... RETURNING`` makes the
    claim atomic, so a re-poll can't double-deliver the same steer. Commits its
    own transaction. Returns ``[]`` when nothing is pending.
    """
    rows = conn.execute(
        "UPDATE task_steers SET status = 'consumed', consumed_at = datetime('now') "
        "WHERE task_id = ? AND status = 'pending' "
        "RETURNING id, task_id, seq, text, user_id, source, status, created_at, consumed_at",
        (task_id,),
    ).fetchall()
    conn.commit()
    steers = [_row_to_steer(r) for r in rows]
    steers.sort(key=lambda s: s.seq)
    return steers


def drop_pending_steers(conn: sqlite3.Connection, task_id: int) -> int:
    """Mark a task's still-``pending`` steers as ``dropped``. Returns the count.

    Called at task finalization so a steer that never drained (task finished /
    suspended before its next boundary) doesn't leak to a later execution and is
    visible in audit as dropped rather than silently deleted.
    """
    cursor = conn.execute(
        "UPDATE task_steers SET status = 'dropped' "
        "WHERE task_id = ? AND status = 'pending'",
        (task_id,),
    )
    conn.commit()
    return cursor.rowcount


def count_pending_steers(conn: sqlite3.Connection, task_id: int) -> int:
    """Number of ``pending`` steers for a task (backs the per-task depth cap)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM task_steers WHERE task_id = ? AND status = 'pending'",
        (task_id,),
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Web chat rooms (web chat surface)
# ---------------------------------------------------------------------------


@dataclass
class WebChatRoom:
    id: int
    user_id: str
    token: str
    name: str
    archived: bool
    #: Sidebar tint: a `room_colors.ROOM_COLORS` name, or None for no colour.
    #: The column is `NOT NULL DEFAULT ''`, so the empty string is the stored
    #: "none" and it is mapped to None here — one absent value for every
    #: consumer, rather than a '' the JSON payload would carry as a colour.
    color: str | None
    created_at: str
    updated_at: str


def _row_to_web_chat_room(row: sqlite3.Row) -> WebChatRoom:
    return WebChatRoom(
        id=row["id"],
        user_id=row["user_id"],
        token=row["token"],
        name=row["name"],
        archived=bool(row["archived"]),
        # Tolerant read, like `_row_to_room`'s ALTER-added columns and for the
        # same reason: the `ALTER` that adds this sits inside a `try` whose
        # `except sqlite3.OperationalError: pass` covers two earlier statements,
        # so a lock — most plausible against the live daemon a deploy migrates
        # under — skips it silently. Every reader here is `SELECT *`, so an
        # unguarded `row["color"]` would raise in the room listing, the SSE
        # snapshot, the PATCH response and the scheduler's handle lookup at
        # once, permanently, naming nothing.
        color=(row["color"] if "color" in row.keys() else "") or None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _new_web_chat_token(user_id: str) -> str:
    """Per-room channel token. The ``web-`` prefix is informational, not a
    security boundary — handlers always derive ``user_id`` from the session."""
    return f"web-{user_id}-{uuid.uuid4().hex[:12]}"


def list_web_chat_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[WebChatRoom]:
    """Rooms for a user, oldest first (creation order)."""
    sql = "SELECT * FROM web_chat_rooms WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY id ASC"
    return [_row_to_web_chat_room(r) for r in conn.execute(sql, params).fetchall()]


def get_web_chat_room(conn: sqlite3.Connection, room_id: int) -> WebChatRoom | None:
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE id = ?", (room_id,)
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def get_web_chat_room_by_token(
    conn: sqlite3.Connection, token: str,
) -> WebChatRoom | None:
    """Return *any one* handle for `token`. A token no longer maps to a unique
    handle — a shared Talk room has one handle per participant (ISSUE-134) — so
    this is only safe for reading token-invariant fields (e.g. the room name).
    Callers needing a specific user's handle must scope by `user_id`."""
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE token = ? LIMIT 1", (token,)
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def create_web_chat_room(
    conn: sqlite3.Connection, user_id: str, name: str,
) -> WebChatRoom:
    """Create a room with a freshly generated channel token.

    Also registers the room in the unified `rooms` registry (origin=web) with a
    self-referential `web` binding, so newly-created web rooms appear in the
    cross-surface room list without waiting for the one-time migration.
    """
    token = _new_web_chat_token(user_id)
    display = name.strip() or "general"
    row = conn.execute(
        "INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) "
        "RETURNING *",
        (user_id, token, display),
    ).fetchone()
    register_room(conn, token, user_id, origin="web", name=display)
    add_room_binding(conn, token, "web", token)
    return _row_to_web_chat_room(row)


def update_web_chat_room(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    name: str | None = None,
    archived: bool | None = None,
    color: str | None = None,
) -> WebChatRoom | None:
    """Rename, (un)archive and/or re-tint a room. Returns the updated row, or
    None if the id is unknown.

    `color` follows the same "None leaves it alone" contract as its neighbours,
    which leaves the **empty string** as the clear (ISSUE-433) — it is what the
    column stores for "no colour", so no sentinel is needed to tell the two
    apart. Validation against the palette belongs to the route, like `model`'s:
    this writes what it is given.
    """
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip() or "general")
    if archived is not None:
        sets.append("archived = ?")
        params.append(1 if archived else 0)
    if color is not None:
        sets.append("color = ?")
        params.append(color)
    if not sets:
        return get_web_chat_room(conn, room_id)
    sets.append("updated_at = datetime('now')")
    params.append(room_id)
    row = conn.execute(
        f"UPDATE web_chat_rooms SET {', '.join(sets)} WHERE id = ? RETURNING *",
        params,
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def ensure_web_chat_handle(
    conn: sqlite3.Connection, user_id: str, token: str, name: str,
) -> WebChatRoom:
    """Ensure a ``web_chat_rooms`` handle row exists for ``user_id`` against an
    existing registry room ``token`` (used as the frontend's integer room id when
    the room originated on another surface, e.g. a Talk room surfaced in web).
    Idempotent on (user_id, token) — a shared Talk room has one handle per
    participant (ISSUE-134). Returns the requesting user's handle."""
    conn.execute(
        "INSERT OR IGNORE INTO web_chat_rooms (user_id, token, name) "
        "VALUES (?, ?, ?)",
        (user_id, token, name.strip() or "room"),
    )
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE user_id = ? AND token = ?",
        (user_id, token),
    ).fetchone()
    assert row is not None
    return _row_to_web_chat_room(row)


def ensure_default_web_chat_room(
    conn: sqlite3.Connection, user_id: str,
) -> WebChatRoom:
    """The user's default room, creating a ``general`` one when they have none.

    This is a **delivery** question, not a listing one, and the difference is
    what ISSUE-342 turned on. `transport.web.default_web_room_token` is the only
    caller that needs a room invented — a bare `web` route (an alert, the
    execution log, a routed notification) has to land somewhere. The web listing
    calls this only when the registry has nothing at all, because it mints Talk
    handles in its own loop a few lines later and never needed one invented for
    it; counting `web_chat_rooms` handles here is what produced a second
    `general` beside the Talk one on a user's first web visit, since a Talk
    room's handle does not exist until that loop runs.

    Two rules on the fallback, both about it being a delivery target:

    - **A room the user is alone in.** A shared Talk room is one other people
      read, and a personal alert delivered into it is delivered in front of
      them.
    - **Not a channel room.** `log_channel` and `alerts_channel` are
      machine-owned; the entrypoint even posts into `alerts` at boot, so
      activity order alone would hand a user's default to whichever the daemon
      last wrote to. Worse, the pick sticks: the handle minted here becomes the
      user's oldest, and the first branch returns it from then on.

    A user whose only rooms are shared or machine-owned gets a private
    `general`, which is the old behaviour and the right answer for delivery.
    """
    rooms = list_web_chat_rooms(conn, user_id, include_archived=False)
    if rooms:
        return rooms[0]
    for room in _default_room_candidates(conn, user_id):
        handle = ensure_web_chat_handle(
            conn, user_id, room.token, room.name or "Talk room",
        )
        # `list_member_rooms` filters `rooms.archived` while the branch above
        # filters the per-user handle flag, so a room the user hid and was then
        # re-added to (ISSUE-134) arrives here with an archived handle. Clear
        # it, the same way the web listing does, or this returns a row the
        # payload would report as hidden.
        if handle.archived:
            handle = update_web_chat_room(conn, handle.id, archived=False) or handle
        return handle
    return create_web_chat_room(conn, user_id, "general")


def _default_room_candidates(
    conn: sqlite3.Connection, user_id: str,
) -> "list[Room]":
    """Registry rooms usable as ``user_id``'s default delivery target.

    Activity-ordered, like `list_member_rooms`, minus the two classes that must
    never become a delivery default: rooms with another member in them, and the
    user's own log / alerts channels. See `ensure_default_web_chat_room`.
    """
    row = conn.execute(
        "SELECT log_channel, alerts_channel FROM user_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    channels = {
        (row["log_channel"] or ""), (row["alerts_channel"] or "")
    } if row else set()
    channels.discard("")
    out: "list[Room]" = []
    for room in list_member_rooms(conn, user_id, include_archived=False):
        if room.token in channels:
            continue
        if len(list_room_members(conn, room.token)) > 1:
            continue
        out.append(room)
    return out


# The legacy `web_chat_messages` accessors (`add_web_chat_message` /
# `list_web_chat_messages` / `WebChatMessage`) are gone. Bot-delivered room
# messages — alerts, the verbose execution log, any notification routed to
# `web` — are written by `WebTransport.deliver` into the canonical `messages`
# store as `role='system'` rows and read back by `list_system_messages` /
# `list_system_messages_in_band` / `list_room_events_since`. The table itself
# is kept for now (its `delete_web_chat_room` cascade still references it);
# dropping it is a migration and out of scope.


def count_recent_web_tasks(
    conn: sqlite3.Connection, user_id: str, window_seconds: int,
) -> int:
    """Count this user's web-chat tasks created within the last
    ``window_seconds`` — backs the per-user rate limit (no extra state)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND source_type = 'web' "
        "AND created_at > datetime('now', ?)",
        (user_id, f"-{int(window_seconds)} seconds"),
    ).fetchone()
    return int(row[0]) if row else 0


def count_recent_email_tasks(
    conn: sqlite3.Connection, user_id: str, window_seconds: int,
) -> int:
    """Count this user's email-origin tasks created within the last
    ``window_seconds`` — backs the per-user inbound volume budget (ISSUE-250).

    The email twin of ``count_recent_web_tasks``, and deliberately the same
    shape: counting `tasks` rather than keeping a separate counter means the
    budget survives a daemon restart and cannot drift from what was actually
    created. A held (`pending_confirmation`) task counts — it cost a prompt and
    it will cost an invocation the moment it is approved.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND source_type = 'email' "
        "AND created_at > datetime('now', ?)",
        (user_id, f"-{int(window_seconds)} seconds"),
    ).fetchone()
    return int(row[0]) if row else 0


def count_recent_email_tasks_from_sender(
    conn: sqlite3.Connection, user_id: str, sender_email: str, window_seconds: int,
) -> int:
    """The same count, narrowed to one correspondent — the per-sender sub-budget.

    Read off `processed_emails` rather than `tasks`, because that is where the
    sender lives; `tasks` has no column for it. Only rows that actually produced
    a task count: a `quiet`, `discarded` or `throttled` row cost nothing, and
    counting the throttled ones would make throttling self-sustaining — a sender
    over budget could never come back under it.

    Compared on the addr-spec, not the raw header. The ledger stores the
    envelope sender verbatim, so the same person arrives as
    ``Loud <loud@example.com>`` on one message and ``loud@example.com`` on the
    next; treating those as two senders would leave the budget trivially
    evadable by varying the display name.
    """
    address = (parseaddr(sender_email or "")[1] or "").strip().lower()
    if not address:
        return 0
    rows = conn.execute(
        'SELECT sender_email FROM processed_emails WHERE user_id = ? '
        "AND task_id IS NOT NULL AND processed_at > datetime('now', ?)",
        (user_id, f"-{int(window_seconds)} seconds"),
    ).fetchall()
    count = 0
    for row in rows:
        other = (parseaddr(row["sender_email"] or "")[1] or "").strip().lower()
        if other == address:
            count += 1
    return count


def count_inflight_tasks_for_scheduled_job(
    conn: sqlite3.Connection, scheduled_job_id: int,
) -> int:
    """Count non-terminal tasks already queued for a scheduled job — backs the
    overlap guard in check_scheduled_jobs. A cron that can't keep up (e.g. a
    ``* * * * *`` job behind a wedged single background worker) must not stack a
    new run each tick; that grew a 130+ deep backlog one row/minute in the
    location-alert incident."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE scheduled_job_id = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (scheduled_job_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def count_active_web_tasks(
    conn: sqlite3.Connection, token: str, user_id: str,
) -> int:
    """Count non-terminal tasks targeting a room's token — backs the busy-room
    guard on delete (won't drop a room a worker is still writing against).

    Counts every source_type, not just ``web``: a foreign task routed into the
    room (e.g. an email reply with ``conversation_token`` set to the room token)
    will also write to it via WebTransport.deliver, so deletion must wait on it
    too."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE conversation_token = ? AND user_id = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (token, user_id),
    ).fetchone()
    return int(row[0]) if row else 0


def count_active_room_tasks(conn: sqlite3.Connection, token: str) -> int:
    """Count non-terminal tasks targeting a room's token, **across every user**.

    The token-scoped sibling of `count_active_web_tasks`, for a guard on state
    that is room-global rather than per-user. A room's `CHANNEL.md` is one file
    shared by every member, so any member's worker may be writing it — filtering
    by the caller the way the delete guard does would refuse nothing in exactly
    the shared-room case that needs the guard most. Delete is per-user because
    it drops only the caller's own handle; this is not.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE conversation_token = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (token,),
    ).fetchone()
    return int(row[0]) if row else 0


def delete_web_chat_room(
    conn: sqlite3.Connection, room_id: int, user_id: str,
) -> bool:
    """Hard-delete a room and every row keyed on its token, in one transaction.

    Returns ``False`` (deleting nothing) when the room is unknown or owned by
    another user. Removes, in order: the room's tasks' ``task_events``, those
    tasks, its ``web_chat_messages``, its ``channel_sleep_cycle_state``, and the
    room row itself. The ``CHANNEL.md`` directory and channel ``memory_chunks``
    are not touched here — the caller removes the former best-effort; the latter
    is a documented residual.
    """
    room = get_web_chat_room(conn, room_id)
    if room is None or room.user_id != user_id:
        return False
    token = room.token
    conn.execute(
        "DELETE FROM task_events WHERE task_id IN "
        "(SELECT id FROM tasks WHERE conversation_token = ? AND user_id = ?)",
        (token, user_id),
    )
    conn.execute(
        "DELETE FROM tasks WHERE conversation_token = ? AND user_id = ?",
        (token, user_id),
    )
    conn.execute("DELETE FROM web_chat_messages WHERE token = ?", (token,))
    conn.execute(
        "DELETE FROM channel_sleep_cycle_state WHERE conversation_token = ?",
        (token,),
    )
    # Unified-rooms tables — FK cascades are decorative (foreign_keys unset),
    # so hand-delete every row keyed on the token. Stars first: they key on
    # message ids that are about to disappear.
    conn.execute(
        "DELETE FROM message_stars WHERE message_id IN "
        "(SELECT id FROM messages WHERE room_token = ?)",
        (token,),
    )
    conn.execute("DELETE FROM messages WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM message_deletions WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_bindings WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_read_state WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_members WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_dismissals WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM rooms WHERE token = ?", (token,))
    # Drop every participant's handle for the token, not just the requester's
    # (room_id): a promoted web room can accrue handles for other members, and
    # leaving an orphan handle pointing at a now-deleted room would suppress
    # their default-room creation and yield an empty room list (ISSUE-134).
    conn.execute("DELETE FROM web_chat_rooms WHERE token = ?", (token,))
    return True


# ---------------------------------------------------------------------------
# Unified Talk / web room sync — registry, bindings, canonical messages
# ---------------------------------------------------------------------------


@dataclass
class Room:
    """A surface-independent conversation. `token` is the canonical
    conversation_token; `origin` is the surface it was created on."""

    token: str
    user_id: str
    name: str | None
    origin: str
    created_at: str
    archived: bool
    model: str | None = None
    effort: str | None = None
    #: Standing brain default for this room (a kind), or None to inherit the
    #: instance default. Copied onto `tasks.brain` at `record_inbound`.
    brain: str | None = None
    #: The model namespace `model` was resolved in, recorded when it was
    #: written. Not derivable from `brain`: a kind the operator has dropped
    #: from `[brain] room_selectable` is refused at resolution time, so a
    #: `!room model` typed afterwards lands in the lane's namespace while
    #: `brain` still names the refused kind (ISSUE-420). None = not recorded.
    model_namespace: str | None = None
    # Newest row in the room's canonical transcript, falling back to the room's
    # own creation time when it has never been spoken in. Populated only by the
    # queries that compute it (`list_member_rooms`) — a room fetched by token
    # carries None rather than a stale stamp.
    last_activity: str | None = None


@dataclass
class RoomBinding:
    """Maps a room's canonical token to one surface's native reference."""

    room_token: str
    surface: str
    surface_ref: str
    created_at: str


@dataclass
class Message:
    """One canonical, surface-neutral message in a room transcript."""

    id: int
    room_token: str
    role: str
    body: str
    title: str | None
    task_id: int | None
    origin_surface: str
    external_ids: dict | None
    created_at: str
    #: Display names of files the turn carried (display-only; the host paths
    #: live on the task row, which retention eventually deletes).
    attachments: list[str] | None = None
    #: Workspace paths for those files, positional against `attachments`, for
    #: linking a chip at `/chat/files`. A None entry isn't servable.
    attachment_paths: list[str | None] | None = None
    #: Canonical id of the message this one replies to, or None. May dangle —
    #: the parent can be hard-deleted, and the citation outlives it.
    reply_to_message_id: int | None = None


def _row_to_room(row: sqlite3.Row) -> Room:
    keys = row.keys()
    return Room(
        token=row["token"],
        user_id=row["user_id"],
        name=row["name"],
        origin=row["origin"],
        created_at=row["created_at"],
        archived=bool(row["archived"]),
        # Older DBs mid-migration may lack these columns; default to None.
        model=row["model"] if "model" in keys else None,
        effort=row["effort"] if "effort" in keys else None,
        brain=row["brain"] if "brain" in keys else None,
        model_namespace=(
            row["model_namespace"] if "model_namespace" in keys else None
        ),
        last_activity=row["last_activity"] if "last_activity" in keys else None,
    )


def _row_to_room_binding(row: sqlite3.Row) -> RoomBinding:
    return RoomBinding(
        room_token=row["room_token"],
        surface=row["surface"],
        surface_ref=row["surface_ref"],
        created_at=row["created_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    raw = row["external_ids"]
    external = json.loads(raw) if raw else None
    keys = row.keys()
    raw_att = row["attachments"] if "attachments" in keys else None
    raw_paths = row["attachment_paths"] if "attachment_paths" in keys else None
    return Message(
        attachments=json.loads(raw_att) if raw_att else None,
        attachment_paths=json.loads(raw_paths) if raw_paths else None,
        reply_to_message_id=(
            row["reply_to_message_id"] if "reply_to_message_id" in keys else None
        ),
        id=row["id"],
        room_token=row["room_token"],
        role=row["role"],
        body=row["body"],
        title=row["title"],
        task_id=row["task_id"],
        origin_surface=row["origin_surface"],
        external_ids=external,
        created_at=row["created_at"],
    )


def register_room(
    conn: sqlite3.Connection,
    token: str,
    user_id: str,
    *,
    origin: str,
    name: str | None = None,
) -> Room:
    """Idempotently register a room. If a row already exists for `token` it is
    returned unchanged (name/origin are not overwritten — first writer wins).

    The registering user is recorded as a member either way: `rooms.user_id` is
    only the creator/origin owner, but visibility is resolved through
    `room_members` (ISSUE-134), so a second participant registering against an
    existing room still becomes a member."""
    conn.execute(
        "INSERT OR IGNORE INTO rooms (token, user_id, name, origin) "
        "VALUES (?, ?, ?, ?)",
        (token, user_id, name, origin),
    )
    add_room_member(conn, token, user_id)
    room = get_room(conn, token)
    assert room is not None  # just inserted or already present
    return room


def add_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Idempotently record that `user_id` is a participant in `room_token`."""
    conn.execute(
        "INSERT OR IGNORE INTO room_members (room_token, user_id) VALUES (?, ?)",
        (room_token, user_id),
    )


def remove_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Drop `user_id`'s membership — the per-user "hide this room" switch. The
    shared room, its transcript, and other members are untouched."""
    conn.execute(
        "DELETE FROM room_members WHERE room_token = ? AND user_id = ?",
        (room_token, user_id),
    )


def is_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM room_members WHERE room_token = ? AND user_id = ? LIMIT 1",
        (room_token, user_id),
    ).fetchone()
    return row is not None


def shares_room_with(conn: sqlite3.Connection, user_a: str, user_b: str) -> bool:
    """True when both users are members of at least one room in common.

    The visibility predicate for a co-member's avatar. Written now rather than
    when web rooms gain a second member, because retrofitting an authorization
    predicate onto a URL clients have already cached is the worse order — and
    group Talk rooms are already multi-member, so it is not purely future work.

    Wider than "a co-member sees a co-member", and deliberately so rather than
    by oversight: `_istota_members_for_conversation` seeds `room_members` for
    every istota user in a Talk conversation the first time the poll registers
    it, and `ingest.record_inbound` adds any sender. So the real predicate is
    "anyone who can put A in a Talk room with them can read A's face" — the
    same access they already have to A's name and presence in Talk. Accepted
    for a face; a future source with more to disclose must not inherit it
    unexamined.

    **Membership here, and not what the room list renders.** `room_dismissals`
    and `rooms.archived` are deliberately not subtracted, though
    `list_member_rooms` subtracts both. Hiding a room in the web UI is a
    display decision, not leaving the conversation: the two people are still in
    that Talk room together, and the poll re-adds a dropped `room_members` row
    the next time it registers the room — which is what the schema comment on
    `room_dismissals` says outright, and why the web delete and archive paths
    pair `remove_room_member` with `dismiss_room`. So a caller must not read a
    `remove_room_member` on a Talk-**backed** room as a durable revocation —
    `web_app._is_talk_backed` is that predicate, and it is wider than `origin`:
    a web-origin room promoted to Talk is one too, and takes the same hide path
    since ISSUE-408. What is durable is a room that is gone:
    `delete_web_chat_room` clears its members, and it is now reached only for a
    room with no Talk conversation behind it — which is exactly the room no poll
    re-seeds.

    The consequence for the avatar endpoint is stated where the cache policy
    is: the five-minute window bounds how long a client may hold whatever this
    answers, and claims nothing about what ends the grant.

    `idx_room_members_user` covers both sides of the self-join.
    """
    row = conn.execute(
        """
        SELECT 1 FROM room_members a
        JOIN room_members b ON a.room_token = b.room_token
        WHERE a.user_id = ? AND b.user_id = ? LIMIT 1
        """,
        (user_a, user_b),
    ).fetchone()
    return row is not None


def dismiss_room(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Tombstone a room as hidden for `user_id` (the web hide action). Durable
    against the poll-time membership backfill — `list_member_rooms` excludes a
    dismissed room even while the user is still a member. Cleared by
    `undismiss_room` (the user's own next inbound)."""
    conn.execute(
        "INSERT OR IGNORE INTO room_dismissals (room_token, user_id) VALUES (?, ?)",
        (room_token, user_id),
    )


def undismiss_room(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Clear a hide tombstone — re-engagement un-hides (called from
    `record_inbound` on the sender's own message)."""
    conn.execute(
        "DELETE FROM room_dismissals WHERE room_token = ? AND user_id = ?",
        (room_token, user_id),
    )


def is_room_dismissed(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM room_dismissals WHERE room_token = ? AND user_id = ? LIMIT 1",
        (room_token, user_id),
    ).fetchone()
    return row is not None


def list_room_members(conn: sqlite3.Connection, room_token: str) -> list[str]:
    rows = conn.execute(
        "SELECT user_id FROM room_members WHERE room_token = ? ORDER BY user_id",
        (room_token,),
    ).fetchall()
    return [r["user_id"] for r in rows]


def list_member_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[Room]:
    """Rooms `user_id` is a member of, **most recently active first**. This is
    the visibility query for the web room list (ISSUE-134) — it replaces the
    single-owner `list_rooms`, so a shared Talk room surfaces for every
    participant — and it is also the order the sidebar renders in, so the room
    someone just spoke in is at the top.

    Activity is the newest row in the room's canonical transcript, taken as the
    `created_at` of its highest `messages.id` rather than a `MAX(created_at)`:
    the ids are the same order and `idx_messages_room (room_token, id)` turns
    the lookup into one index seek per room instead of a scan over every
    message the room ever carried. A room nobody has spoken in falls back to
    its own creation time, so a brand-new room still sorts above a stale one.
    Ties break on creation time, then token, so the order is total.

    A room the user has hidden (`room_dismissals` tombstone) is excluded even
    while they remain a member — the poll-time backfill re-adds membership, so
    membership alone can't keep a hidden room hidden."""
    sql = (
        "SELECT r.*, COALESCE(("
        "  SELECT msg.created_at FROM messages msg "
        "  WHERE msg.room_token = r.token ORDER BY msg.id DESC LIMIT 1"
        "), r.created_at) AS last_activity "
        "FROM rooms r "
        "JOIN room_members m ON m.room_token = r.token "
        "WHERE m.user_id = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM room_dismissals d "
        "  WHERE d.room_token = r.token AND d.user_id = m.user_id"
        ")"
    )
    params: list = [user_id]
    if not include_archived:
        sql += " AND r.archived = 0"
    sql += " ORDER BY last_activity DESC, r.created_at DESC, r.token ASC"
    return [_row_to_room(r) for r in conn.execute(sql, params).fetchall()]


def get_room(conn: sqlite3.Connection, token: str) -> Room | None:
    row = conn.execute("SELECT * FROM rooms WHERE token = ?", (token,)).fetchone()
    return _row_to_room(row) if row else None


def list_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[Room]:
    """Rooms for a user, oldest-first (creation order)."""
    sql = "SELECT * FROM rooms WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY created_at ASC, token ASC"
    return [_row_to_room(r) for r in conn.execute(sql, params).fetchall()]


def set_room_archived(conn: sqlite3.Connection, token: str, archived: bool) -> None:
    conn.execute(
        "UPDATE rooms SET archived = ? WHERE token = ?",
        (1 if archived else 0, token),
    )


def archive_orphaned_talk_rooms(
    conn: sqlite3.Connection, live_tokens: set[str],
) -> int:
    """Archive Talk-origin registry rooms whose token is no longer among the
    bot's live Talk conversations (`live_tokens`) — i.e. the conversation was
    deleted in Nextcloud, or the bot was removed from it. Without this a deleted
    Talk room keeps surfacing in the web room list forever, because its registry
    row is never reconciled against Nextcloud.

    Archive, not hard-delete: a re-add (or a later reconcile) shouldn't destroy
    mirrored history, and this mirrors the web-side delete of a Talk room.
    Web-origin rooms are never touched. Returns the number archived.

    The caller MUST pass a *complete* live-token set from a successful Talk
    `list_conversations` (not a partial/failed fetch) — an empty set here means
    the bot is genuinely in zero Talk rooms and archives all of them."""
    rows = conn.execute(
        "SELECT token FROM rooms WHERE origin = 'talk' AND archived = 0"
    ).fetchall()
    archived = 0
    for row in rows:
        if row["token"] not in live_tokens:
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", (row["token"],)
            )
            archived += 1
    return archived


def rename_room(conn: sqlite3.Connection, token: str, name: str) -> None:
    conn.execute("UPDATE rooms SET name = ? WHERE token = ?", (name, token))


def set_room_model_effort(
    conn: sqlite3.Connection,
    token: str,
    model: str | None,
    effort: str | None,
    *,
    namespace: str | None = None,
) -> None:
    """Set the room's standing model + effort as a pair (both canonical values,
    or None to clear). This is the `!room model <alias>` / room-settings write —
    an alias resolves to a (model, effort) pair, so both columns move together.

    ``namespace`` is the model namespace ``model`` was resolved in, and it moves
    with the model rather than being a third knob (ISSUE-420): a caller clearing
    the pin passes nothing and the column clears too, because a namespace left
    behind by a model that is gone would be read as describing whatever is
    written next. It defaults to ``None`` rather than being required so that the
    clearing callers read as clears; a *setting* caller that omits it stores
    "not recorded", which is the pre-column behaviour and not a new state.

    **Keyword-only, and that is a guard rather than a style choice.** The
    four-positional call was the whole API until this parameter existed, and it
    now means "set a model and erase its recorded namespace" — the shape every
    existing test call site still has, so a future producer written from one as
    a template would inherit it with nothing going red. Requiring the keyword
    makes the omission visible at the call site instead.
    """
    conn.execute(
        "UPDATE rooms SET model = ?, effort = ?, model_namespace = ? "
        "WHERE token = ?",
        (model, effort, namespace, token),
    )


def set_room_effort(conn: sqlite3.Connection, token: str, effort: str | None) -> None:
    """Set only the room's effort level (None clears it), leaving the model
    default untouched — the `!room effort <level>` convenience knob."""
    conn.execute(
        "UPDATE rooms SET effort = ? WHERE token = ?", (effort, token)
    )


def set_room_model(
    conn: sqlite3.Connection,
    token: str,
    model: str | None,
    *,
    namespace: str | None = None,
) -> None:
    """Set only the room's model default (None clears it), leaving the effort
    level untouched — so `!room model <alias>` and `!room effort <level>` are
    orthogonal knobs. (An effort-bearing alias like `opus-high` still sets both
    via set_room_model_effort — that's the caller's explicit both-pick.)

    ``namespace`` moves with the model, as in `set_room_model_effort`. Effort is
    the knob that stays put here; the namespace is a property of the model and
    would be stale the moment the model changed without it.
    """
    conn.execute(
        "UPDATE rooms SET model = ?, model_namespace = ? WHERE token = ?",
        (model, namespace, token),
    )


def set_room_brain(conn: sqlite3.Connection, token: str, brain: str | None) -> None:
    """Set the room's standing brain default (a kind), or None to clear it.

    Its own knob: unlike the alias resolution behind `set_room_model_effort`,
    nothing about a brain kind implies a model or an effort. Clearing a model
    pin that the new brain's namespace cannot resolve is a decision the command
    and HTTP layers make, above this, because only they know what the outgoing
    brain was.

    The value is stored as given and validated at *dispatch* rather than here,
    so that an operator shortening `[brain] room_selectable` takes effect at the
    next turn without a sweep, and a later restoration brings the room's setting
    back rather than finding it erased.
    """
    conn.execute(
        "UPDATE rooms SET brain = ? WHERE token = ?", (brain, token)
    )


def add_room_binding(
    conn: sqlite3.Connection, room_token: str, surface: str, surface_ref: str,
) -> None:
    """Idempotently bind a room to a surface (PK (room_token, surface))."""
    conn.execute(
        "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
        "VALUES (?, ?, ?)",
        (room_token, surface, surface_ref),
    )


def replace_room_binding(
    conn: sqlite3.Connection,
    room_token: str,
    surface: str,
    surface_ref: str,
    *,
    expected_ref: str | None,
) -> bool:
    """Point a room's binding at a different ref. Compare-and-set; returns
    whether the write landed.

    `add_room_binding` is `INSERT OR IGNORE` and stays that way: it is called on
    every inbound poll by three writers, and an `ON CONFLICT DO UPDATE` there
    would let a stale or misrouted inbound rewrite a good binding. Replacement
    is a separate verb with one caller — the promote path, which has established
    that the bound conversation is gone (ISSUE-401).

    `expected_ref` is what the caller saw when it looked: `None` for no binding
    at all, otherwise the ref it read. A binding that changed in between is left
    alone and False comes back. That matters because the caller's check and its
    write are separated by an OCS round trip that creates a conversation, so two
    concurrent promotes can interleave — and the loser overwriting the winner
    would point the room at a conversation the winner is not using, on top of
    the orphan it already made.
    """
    if expected_ref is None:
        cur = conn.execute(
            "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
            "VALUES (?, ?, ?)",
            (room_token, surface, surface_ref),
        )
    else:
        cur = conn.execute(
            "UPDATE room_bindings SET surface_ref = ? "
            "WHERE room_token = ? AND surface = ? AND surface_ref = ?",
            (surface_ref, room_token, surface, expected_ref),
        )
    return cur.rowcount > 0


def get_room_binding(
    conn: sqlite3.Connection, room_token: str, surface: str,
) -> RoomBinding | None:
    row = conn.execute(
        "SELECT * FROM room_bindings WHERE room_token = ? AND surface = ?",
        (room_token, surface),
    ).fetchone()
    return _row_to_room_binding(row) if row else None


def list_room_bindings(
    conn: sqlite3.Connection, room_token: str,
) -> list[RoomBinding]:
    rows = conn.execute(
        "SELECT * FROM room_bindings WHERE room_token = ? ORDER BY surface",
        (room_token,),
    ).fetchall()
    return [_row_to_room_binding(r) for r in rows]


def talk_refs_for_member(
    conn: sqlite3.Connection, user_id: str,
) -> dict[str, str]:
    """Canonical room token -> its Talk conversation ref, for one user's rooms.

    One query rather than a binding lookup per room: the web room listing needs
    this for every entry it renders and is polled. Scoped by membership rather
    than by a token list, so it takes one host parameter however many rooms the
    user is in — an `IN (...)` built from `list_member_rooms` is unbounded, and
    that query has no `LIMIT`. A room with no Talk binding is absent from the
    result rather than present with None, so the caller decides what "not on
    Talk" renders as; a room the caller has already filtered out is harmless.
    """
    rows = conn.execute(
        "SELECT b.room_token, b.surface_ref FROM room_bindings b "
        "JOIN room_members m ON m.room_token = b.room_token "
        "WHERE b.surface = 'talk' AND m.user_id = ?",
        (user_id,),
    ).fetchall()
    return {row["room_token"]: row["surface_ref"] for row in rows}


def resolve_room_token(
    conn: sqlite3.Connection, surface: str, surface_ref: str,
) -> str | None:
    """Find the canonical room token for a surface's native reference, or None
    if no binding exists (origin-surface case: caller treats surface_ref as the
    canonical token)."""
    row = conn.execute(
        "SELECT room_token FROM room_bindings WHERE surface = ? AND surface_ref = ?",
        (surface, surface_ref),
    ).fetchone()
    return row["room_token"] if row else None


def find_room_token_by_ref(
    conn: sqlite3.Connection, surface_ref: str,
) -> str | None:
    """The canonical room token for a surface ref, whichever surface owns it.

    `resolve_room_token` needs to be told which surface's namespace a ref
    belongs to, which a caller holding a token from *another* surface cannot
    say. The live case is an email continuation: its `conversation_token` is
    whatever the originating send recorded — on a promoted room that is the Talk
    ref — while the task's own surface is `email`, which has no bindings at all,
    so the surface-scoped lookup always misses and the room reads as
    unregistered.

    Prefer `resolve_room_token` when the surface is known; refs are only unique
    *within* a surface, so this orders deterministically rather than pretending
    the answer cannot be ambiguous.
    """
    row = conn.execute(
        "SELECT room_token FROM room_bindings WHERE surface_ref = ? "
        "ORDER BY room_token LIMIT 1",
        (surface_ref,),
    ).fetchone()
    return row["room_token"] if row else None


def add_message(
    conn: sqlite3.Connection,
    room_token: str,
    *,
    role: str,
    body: str,
    origin_surface: str,
    title: str | None = None,
    task_id: int | None = None,
    external_ids: dict | None = None,
    attachments: list[str] | None = None,
    attachment_paths: list[str | None] | None = None,
    client_msg_id: str | None = None,
    reply_to_message_id: int | None = None,
    author_user_id: str | None = None,
    author_label: str | None = None,
) -> int:
    """Append a message to a room's canonical transcript. Returns the new id.

    `reply_to_message_id` is a canonical id in this same table — the message
    being replied to. Never a Talk id (see `tasks.reply_to_talk_id`).

    `author_user_id` names an istota user; `author_label` is an external sender
    and **must already be sanitized** — pass it through `external_email_sender`,
    never a raw `From:` header. Set at most one; both NULL means "the room
    owner", which is what every pre-migration row falls back to. Readers resolve
    in that order.
    """
    row = conn.execute(
        "INSERT INTO messages "
        "(room_token, role, body, title, task_id, origin_surface, external_ids, "
        " attachments, attachment_paths, client_msg_id, reply_to_message_id, "
        " author_user_id, author_label) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            room_token,
            role,
            body,
            title,
            task_id,
            origin_surface,
            json.dumps(external_ids) if external_ids else None,
            json.dumps(attachments) if attachments else None,
            json.dumps(attachment_paths) if attachment_paths else None,
            # An empty string is a perfectly valid unique key, so it would
            # collapse a room's entire history onto whichever send stored it
            # first. Same rule as `location_pings.client_id`.
            client_msg_id or None,
            reply_to_message_id,
            author_user_id or None,
            author_label or None,
        ),
    ).fetchone()
    return int(row["id"])


def find_send_by_client_msg_id(
    conn: sqlite3.Connection, room_token: str, client_msg_id: str
) -> tuple[int, str] | None:
    """``(task_id, sender)`` for a stored send under this key in this room, or None.

    What makes a retry of an accepted-but-unreported send idempotent: the
    client cannot tell a request that never arrived from one whose answer was
    lost, so it re-sends, and this is how the second POST resolves to the first
    turn rather than a duplicate of it.

    The sender comes back with it rather than being filtered on, because a room
    is shared and the key is arbitrary client-supplied text. Filtering would
    make a co-member's reused key miss the lookup and then collide on the
    room-scoped unique index; returning it lets the caller tell "my own retry"
    from "somebody else's key" and degrade rather than fail. The index stays
    room-scoped — it is the storage invariant, not the resolution rule.
    """
    if not client_msg_id:
        return None
    row = conn.execute(
        "SELECT m.task_id, t.user_id FROM messages m "
        "JOIN tasks t ON t.id = m.task_id "
        "WHERE m.room_token = ? AND m.client_msg_id = ? "
        "LIMIT 1",
        (room_token, client_msg_id),
    ).fetchone()
    return (int(row["task_id"]), row["user_id"]) if row else None


def find_confirmation_exchange(
    conn: sqlite3.Connection, room_token: str, client_msg_id: str | None,
) -> tuple[int, int, str] | None:
    """``(user_msg_id, system_msg_id, ack)`` of an answer already recorded under
    this key in this room, or None.

    The sibling of :func:`find_send_by_client_msg_id` for the one exchange that
    is *not* a task. `confirmations.record_exchange` writes a `task_id IS NULL`
    pair, and that inner join drops a NULL — so the send-durability lookup
    cannot see this row while the `(room_token, client_msg_id)` unique index
    can. Without a lookup that spans both, a retried "yes" re-resolves from
    scratch: with a second gate parked in the meantime it approves a question
    the user never answered, and with none it takes an `IntegrityError` on the
    index. Both were found in review; the first is an authorization defect.

    The ack is the `system` row written immediately after the answer in the
    same transaction, so it is the next system row in the room by id.
    """
    if not client_msg_id:
        return None
    answer = conn.execute(
        "SELECT id FROM messages "
        "WHERE room_token = ? AND client_msg_id = ? AND role = 'user' "
        "AND task_id IS NULL LIMIT 1",
        (room_token, client_msg_id),
    ).fetchone()
    if answer is None:
        return None
    ack = conn.execute(
        "SELECT id, body FROM messages "
        "WHERE room_token = ? AND role = 'system' AND id > ? "
        "ORDER BY id ASC LIMIT 1",
        (room_token, answer["id"]),
    ).fetchone()
    if ack is None:
        return None
    return (int(answer["id"]), int(ack["id"]), ack["body"])


def get_messages(
    conn: sqlite3.Connection, room_token: str, limit: int | None = None,
) -> list[Message]:
    """A room's messages, oldest-first (by id). With `limit`, returns the most
    recent `limit` messages, still oldest-first."""
    if limit is None:
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_token = ? ORDER BY id ASC",
            (room_token,),
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? ORDER BY id DESC LIMIT ?",
        (room_token, limit),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def store_turn_message(
    conn: sqlite3.Connection,
    room_token: str,
    *,
    role: str,
    body: str,
    task_id: int,
    origin_surface: str,
) -> int | None:
    """Idempotently store a turn's user/assistant message. Returns the new id,
    or None if a row for (room_token, role, task_id) already exists — so a retry
    that re-completes a task, or a duplicate inbound poll, won't duplicate it."""
    existing = conn.execute(
        "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
        "AND role = ? LIMIT 1",
        (room_token, task_id, role),
    ).fetchone()
    if existing:
        return None
    return add_message(
        conn, room_token, role=role, body=body,
        origin_surface=origin_surface, task_id=task_id,
    )


def get_message_room_for_task(conn: sqlite3.Connection, task_id: int) -> str | None:
    """The canonical room token for a task's stored turn, or None if absent.

    A thin read over the durable `messages` store so a conversation search hit
    keeps its room scope after the `tasks` row ages out of retention (the tasks
    table is a display concern; `messages` holds the durable room↔turn mapping).
    Returns the room of the first message carrying this task_id."""
    row = conn.execute(
        "SELECT room_token FROM messages WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["room_token"] if row else None


def get_turn_message_id(
    conn: sqlite3.Connection,
    room_token: str,
    task_id: int,
    role: str = "assistant",
) -> int | None:
    """The durable `messages.id` of a task's stored turn, or None if absent.

    The star key the web transcript gates on. Recovers the id when
    `store_turn_message` returned None (a retry that re-completed a task —
    the row already existed) and lets the terminal event / synthetic backstop
    tell a freshly-settled turn its id so it becomes starrable without a
    history refetch (ISSUE-172)."""
    row = conn.execute(
        "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
        "AND role = ? LIMIT 1",
        (room_token, task_id, role),
    ).fetchone()
    return row["id"] if row else None


def room_for_task_turn(
    conn: sqlite3.Connection, task_id: int, role: str = "user",
) -> str | None:
    """The room a task's stored turn lives in, or None if it has none.

    The inverse of `get_turn_message_id`: that one asks "is this task's turn in
    *this* room", which presumes the caller already knows which room. An email
    task does not — its `conversation_token` is a thread hash, and the room its
    exchange landed in was resolved from the routing (ISSUE-247). Asking the
    store where the question actually went is what makes the answer land under
    it rather than in a second place derived a second way.

    Oldest row wins. The `(room_token, role, task_id)` uniqueness is per room,
    not global, so nothing structurally forbids a second row in a second room;
    this being the strongest rung of the resolution ladder, an arbitrary answer
    would be a hard bug to see.
    """
    row = conn.execute(
        "SELECT room_token FROM messages WHERE task_id = ? AND role = ? "
        "ORDER BY id ASC LIMIT 1",
        (task_id, role),
    ).fetchone()
    return row["room_token"] if row else None


def list_system_messages(
    conn: sqlite3.Connection, room_token: str, limit: int = 50,
) -> list[Message]:
    """The most recent bot-delivered system messages for a room (role='system',
    task_id NULL) — alerts / verbose log / web-routed notifications. Oldest-first.
    Replaces the legacy web_chat_messages read path."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? AND role = 'system' "
        "ORDER BY id DESC LIMIT ?",
        (room_token, limit),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def list_system_messages_in_band(
    conn: sqlite3.Connection, room_token: str, *, lo_ts: str, hi_ts: str,
) -> list[Message]:
    """System messages within the half-open band ``lo_ts <= created_at < hi_ts``
    (the web-chat older-page path — ISSUE-131). Oldest-first. ``lo_ts`` / ``hi_ts``
    are *raw* stored `created_at` strings (`YYYY-MM-DD HH:MM:SS`), the same format
    the keyset cursor travels in — not the `_iso_utc` display value."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? AND role = 'system' "
        "AND created_at >= ? AND created_at < ? ORDER BY id DESC",
        (room_token, lo_ts, hi_ts),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def set_message_external_id(
    conn: sqlite3.Connection, message_id: int, surface: str, external_id: str,
) -> None:
    """Record where a message has been materialized on a surface (the
    loop-prevention ledger). Merges into the existing JSON map.

    The merge is a read-modify-write with no locking above SQLite's own, and
    since ISSUE-287 two processes reach it for the same row — the web send
    path and the Talk poller's echo reconciliation. That is safe only because
    both write the same key (`talk`) with the same value (the echo *is* the
    post), so a lost update loses nothing. A second surface key landing on a
    web-origin user row would make it a real lost-update hazard; make this one
    statement (`json_patch`) before adding one.
    """
    row = conn.execute(
        "SELECT external_ids FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return
    current = json.loads(row["external_ids"]) if row["external_ids"] else {}
    current[surface] = external_id
    conn.execute(
        "UPDATE messages SET external_ids = ? WHERE id = ?",
        (json.dumps(current), message_id),
    )


def clear_stale_talk_delivery_token(
    conn: sqlite3.Connection, room_token: str, stale_ref: str,
) -> int:
    """Drop `tasks.talk_delivery_token` where it still names a Talk conversation
    that is gone. Returns how many rows changed.

    The other half of a rebind (ISSUE-401). `talk_channel_for_task`'s rung 0
    returns this column **absolutely**, before the binding is ever consulted, so
    a task carrying it keeps delivering to the dead conversation no matter what
    the binding now says. Clearing it drops those tasks to rung 1, which is the
    repaired binding.

    Scoped to non-terminal tasks in this room whose value is exactly the ref
    being replaced: a finished task's column is a record of where its answer
    went and is not rewritten, and a task naming some other conversation is not
    this room's problem to fix.
    """
    cur = conn.execute(
        "UPDATE tasks SET talk_delivery_token = NULL "
        "WHERE conversation_token = ? AND talk_delivery_token = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (room_token, stale_ref),
    )
    return cur.rowcount


def clear_room_external_ids(
    conn: sqlite3.Connection, room_token: str, surface: str,
) -> int:
    """Drop one surface's recorded ids for every message in a room. Returns how
    many rows changed.

    The counterpart to a binding being repointed (ISSUE-401). A surface id is a
    claim that this message exists over there, and it is only meaningful
    relative to the conversation the binding named when it was written — Talk
    message ids are per-conversation and start low, so after a rebind a stale
    id does not merely fail to resolve, it can name a *different* message in
    the new conversation. `get_message_external_id` feeds a web reply's Talk
    `replyTo`, so keeping them would attach replies to whatever happens to
    share the number.

    A read-modify-write per row rather than one `json_remove`, matching
    `set_message_external_id` above; the row count is bounded by one room's
    stamped messages and this runs on a rare, explicit user action.
    """
    rows = conn.execute(
        "SELECT id, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL",
        (room_token,),
    ).fetchall()
    changed = 0
    for r in rows:
        try:
            ext = json.loads(r["external_ids"])
        except (ValueError, TypeError):
            continue
        if not isinstance(ext, dict) or surface not in ext:
            continue
        del ext[surface]
        conn.execute(
            "UPDATE messages SET external_ids = ? WHERE id = ?",
            (json.dumps(ext) if ext else None, r["id"]),
        )
        changed += 1
    return changed


def user_turn_has_external_id(
    conn: sqlite3.Connection, task_id: int, surface: str,
) -> bool:
    """True when a task's user turn already carries an external id on
    `surface` — the scheduler's signal that the web process already posted the
    turn as the user (post-as-user mirroring), so the legacy attributed repost
    must be suppressed. A pure framework-DB read; the scheduler never touches
    the token itself."""
    row = conn.execute(
        "SELECT external_ids FROM messages "
        "WHERE task_id = ? AND role = 'user' LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["external_ids"]:
        return False
    try:
        ext = json.loads(row["external_ids"])
    except (ValueError, TypeError):
        return False
    return isinstance(ext, dict) and surface in ext


def stamp_webmirror_echo(
    conn: sqlite3.Connection, room_token: str, message_id: int, talk_id: str,
    actor_id: str,
) -> bool:
    """Record a post-as-user mirror's Talk id from the echo of the message
    itself, when the send-time stamp never landed. Returns whether it stamped.

    `_post_as_user` runs inline in the web send path under a 5 s timeout, so a
    slow Nextcloud hands the caller a timeout for a post it has already stored.
    Nothing then records the Talk id; the scheduler reads the missing stamp as
    "the web process never mirrored this turn" and posts its legacy attributed
    repost, so the question shows twice. The echo carries both halves — the
    canonical row id in `referenceId`, the Talk id in the message — so the
    poller can close the gap with no extra request, and it closes it for a
    crash or a web restart as well as for a timeout.

    **`message_id` is untrusted, and so is `talk_id`.** Both are read off a
    message any participant in the room can compose: `referenceId` is free
    text on Talk's chat API, and `messages.id` is a sequential integer, so
    without a check on *who sent the echo* a participant could spray
    `istota:webmirror:<n>` over a range and stamp every unstamped web turn in
    the room with Talk ids of their choosing. Scoping the row is not enough to
    stop that — it bounds which row may be stamped and not which id it gets,
    and the ledger is read back by the scheduler's repost suppression (which
    would then drop the question from Talk entirely), by the `replyTo`
    resolution for web replies, and by the Talk-side delete path. `actor_id`
    is the echo's author, and it must match the row's own `author_user_id`:
    a genuine post-as-user echo is authored by the same user as the turn it
    mirrors, so equality is the whole test. A NULL `author_user_id` fails it,
    leaving such a row unreconciled rather than reconciled on no evidence.
    """
    if not actor_id:
        return False
    row = conn.execute(
        "SELECT role, origin_surface, author_user_id, external_ids "
        "FROM messages WHERE id = ? AND room_token = ?",
        (message_id, room_token),
    ).fetchone()
    if row is None or row["role"] != "user" or row["origin_surface"] != "web":
        return False
    if row["author_user_id"] != actor_id:
        return False
    if row["external_ids"]:
        try:
            ext = json.loads(row["external_ids"])
        except (ValueError, TypeError):
            ext = {}
        if isinstance(ext, dict) and "talk" in ext:
            return False
    set_message_external_id(conn, message_id, "talk", talk_id)
    return True


def room_max_talk_synced_message_id(
    conn: sqlite3.Connection, room_token: str,
) -> int:
    """The highest `messages.id` in a room that has a `"talk"` external id —
    i.e. the newest canonical message that demonstrably exists in Talk. The
    Talk→web read-sync cursor is capped here so Talk read-state can't swallow
    web-only rows (WebTransport system messages) the user never saw in Talk.
    0 when nothing is stamped yet (sync starts working for post-deploy rows)."""
    rows = conn.execute(
        "SELECT id, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL ORDER BY id DESC",
        (room_token,),
    ).fetchall()
    for r in rows:
        try:
            ext = json.loads(r["external_ids"])
        except (ValueError, TypeError):
            continue
        if isinstance(ext, dict) and "talk" in ext:
            return int(r["id"])
    return 0


def get_message_external_id(
    conn: sqlite3.Connection, message_id: int, surface: str,
) -> str | None:
    """Where a message exists on `surface`, or None.

    The value-returning sibling of `message_has_external_id`, which only
    answers yes/no over a whole room. Used to mirror a web reply into Talk as a
    real Talk reply: the parent's Talk id is what `replyTo` takes, so a bool
    would say the citation is mirrorable without saying what to send.
    """
    row = conn.execute(
        "SELECT external_ids FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None or not row["external_ids"]:
        return None
    try:
        ext = json.loads(row["external_ids"])
    except (TypeError, ValueError):
        return None
    value = ext.get(surface) if isinstance(ext, dict) else None
    return str(value) if value is not None else None


def find_message_by_external_id(
    conn: sqlite3.Connection, room_token: str, surface: str, external_id: str,
) -> int | None:
    """The canonical id of the room's message carrying `external_id` on
    `surface`, or None.

    What lets a Talk-native reply parent be recorded canonically too. Scoped to
    the room because a Talk message id is per-conversation, so an unscoped
    lookup would match an unrelated turn elsewhere.

    `external_ids` is a JSON blob, so this is an unindexed scan of the room's
    stamped rows — the same shape `message_has_external_id` and
    `room_max_talk_synced_message_id` already use. Bounded by the room, and run
    once per inbound reply rather than per message.
    """
    rows = conn.execute(
        "SELECT id, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL ORDER BY id DESC",
        (room_token,),
    ).fetchall()
    for r in rows:
        try:
            ext = json.loads(r["external_ids"])
        except (TypeError, ValueError):
            continue
        if isinstance(ext, dict) and str(ext.get(surface)) == str(external_id):
            return int(r["id"])
    return None


def message_has_external_id(
    conn: sqlite3.Connection,
    room_token: str,
    surface: str,
    external_id: str,
    *,
    exclude_origin: str | None = None,
) -> bool:
    """True if any message in the room already records `external_id` on
    `surface` — used by inbound echo detection.

    ``exclude_origin`` skips rows whose `origin_surface` matches: a row that
    originated on the inbound surface itself isn't a mirror echo — it's the
    same message re-polled (inbound Talk ids are stamped at ingest now), and
    that case must fall through to `create_task`'s duplicate dedup so the
    caller gets the existing task id instead of an echo drop."""
    rows = conn.execute(
        "SELECT origin_surface, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL",
        (room_token,),
    ).fetchall()
    for r in rows:
        if exclude_origin is not None and r["origin_surface"] == exclude_origin:
            continue
        try:
            ext = json.loads(r["external_ids"])
        except (ValueError, TypeError):
            continue
        if isinstance(ext, dict) and ext.get(surface) == external_id:
            return True
    return False


def get_room_read_state(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> int:
    row = conn.execute(
        "SELECT last_read_message_id FROM room_read_state "
        "WHERE room_token = ? AND surface = ? AND user_id = ?",
        (room_token, surface, user_id),
    ).fetchone()
    return int(row["last_read_message_id"]) if row else 0


def set_room_read_state(
    conn: sqlite3.Connection,
    room_token: str,
    surface: str,
    last_read_message_id: int,
    user_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO room_read_state "
        "(room_token, surface, user_id, last_read_message_id) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (room_token, surface, user_id) DO UPDATE SET "
        "last_read_message_id = excluded.last_read_message_id",
        (room_token, surface, user_id, last_read_message_id),
    )


def room_max_message_id(conn: sqlite3.Connection, room_token: str) -> int:
    """The highest `messages.id` in a room, or 0 when the room is empty. Used to
    seed / advance a read cursor to "everything so far"."""
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE room_token = ?",
        (room_token,),
    ).fetchone()
    return int(row["m"])


def count_unread_messages(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> int:
    """Number of unread bot/system messages in a room for a user on a surface:
    messages past the surface's read cursor, excluding the user's own turns
    (`role = 'user'`) so a user's input — including Talk turns mirrored into the
    canonical store — never rings their own room as unread."""
    cursor = get_room_read_state(conn, room_token, surface, user_id)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE room_token = ? AND id > ? AND role != 'user'",
        (room_token, cursor),
    ).fetchone()
    return int(row["n"])


def initialize_room_read_state(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> bool:
    """Seed a read cursor for a room the first time it's surfaced on a surface.

    When no `room_read_state` row yet exists for `(room_token, surface,
    user_id)`, insert one at the room's current `MAX(messages.id)` so a
    pre-existing backlog (e.g. a Talk room newly mirrored into web) reads as
    already-seen instead of flooding the unread indicator. Returns True if it
    seeded a row, False if one already existed (left untouched)."""
    existing = conn.execute(
        "SELECT 1 FROM room_read_state "
        "WHERE room_token = ? AND surface = ? AND user_id = ?",
        (room_token, surface, user_id),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO room_read_state "
        "(room_token, surface, user_id, last_read_message_id) VALUES (?, ?, ?, ?)",
        (room_token, surface, user_id, room_max_message_id(conn, room_token)),
    )
    return True


# ---------------------------------------------------------------------------
# Per-message stars + cross-room aggregate views (web chat)
# ---------------------------------------------------------------------------

# Which `messages` rows render as transcript turns. Shared by the per-room
# spine query (web_app._SPINE_SURFACE) and the cross-room aggregate below.
#
# Storage is the real filter now: an assistant row only exists in a room
# because a task delivered a result into that web-visible room (via
# scheduler._store_room_turn, for ANY source type — subtask/scheduled/briefing/
# heartbeat/talk/web), so "render every assistant row" is exactly right. The
# origin_surface guard is retained only on role='user' rows, whose sole job was
# to hide the synthetic prompt of a non-conversational post — and since the
# producer never writes a user row for those, this guard is belt-and-suspenders
# against any future code that does. Expects the messages table aliased as `m`.
#
# `email` joins web/talk here (ISSUE-136): an inbound email continuing a room
# the user can already see is a conversational turn, so hiding it left the room
# showing the bot's reply with no question above it. The one-directional bot
# source types (scheduled/briefing/heartbeat/subtask) stay out — that is the
# case the guard exists for.
#
# Two writers produce email user rows, not one: `record_inbound`'s mirror-only
# path (live) and `_backfill_turns_for` (migration, `origin_surface` = the task's
# source type). Keep this list in sync with the DELETE in
# `_migrate_nonconversational_transcript_cleanup` — that migration is what
# decides which of those rows survive, and the two disagreeing is how live turns
# get silently swept.
# Declared once here and imported by `commands.py` (`_TRANSCRIPT_SURFACES`),
# which used to carry a hand copy of the same tuple. It answers the third of
# the three questions the room-surface literals used to share — "may this
# surface deposit a `role='user'` row in a room at all" — and is deliberately
# **not** derived from `surfaces.SURFACES`: the column it filters holds
# `source_type` values rather than surface names, and the DELETE below must
# stay in sync with a fourth value (`scheduled`) no surface table will ever
# have. Its members happen to equal `room_role == "member"` plus the one
# `guest`, which is a coincidence rather than a derivation — the room-ownership
# question is `surfaces.is_room_member`, and reading that one here would stop
# an email `!confirm` recording its exchange.
TRANSCRIPT_SURFACES = ("web", "talk", "email")

TRANSCRIPT_SURFACE_FILTER = (
    "(m.role = 'assistant' "
    "OR (m.origin_surface IN ("
    + ", ".join(f"'{s}'" for s in TRANSCRIPT_SURFACES)
    + ") AND m.role = 'user'))"
)


def set_message_starred(
    conn: sqlite3.Connection, message_id: int, user_id: str, starred: bool,
) -> bool:
    """Star/unstar a durable message for one user. Idempotent both ways.
    Returns False only when the message id doesn't exist (the star state then
    matches the request by definition of there being nothing to star)."""
    exists = conn.execute(
        "SELECT 1 FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if exists is None:
        return False
    if starred:
        conn.execute(
            "INSERT OR IGNORE INTO message_stars (message_id, user_id) "
            "VALUES (?, ?)",
            (message_id, user_id),
        )
    else:
        conn.execute(
            "DELETE FROM message_stars WHERE message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
    return True


def delete_message(
    conn: sqlite3.Connection, message_id: int, user_id: str,
) -> str | None:
    """Hard-delete one transcript row. Returns the room token it belonged to,
    or None when the id is unknown (so a repeat delete is a clean no-op rather
    than a fabricated success).

    Hard, not a tombstone: the row is gone from every read path at once, which
    is the whole point of the affordance. Every other row keyed on the message
    id has to go by hand — `PRAGMA foreign_keys` is unset, so the schema's
    cascades are decorative — and the deletion is recorded in
    `message_deletions` so the room stream can tell other open clients.

    Read cursors (`room_read_state.last_read_message_id`) are deliberately left
    alone: they are a high-water mark, not a reference, so a deleted id simply
    stops existing below the line.
    """
    row = conn.execute(
        "SELECT room_token FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    token = row["room_token"]
    conn.execute("DELETE FROM message_stars WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.execute(
        "INSERT INTO message_deletions (message_id, room_token, deleted_by) "
        "VALUES (?, ?, ?)",
        (message_id, token, user_id),
    )
    return token


def get_message_external_ids(
    conn: sqlite3.Connection, message_id: int,
) -> dict[str, str]:
    """The `{surface: external_id}` mirror ledger for one message, or `{}`.

    Read *before* a delete by the Talk-propagation path — the row is gone
    afterwards, and the Talk message id lives nowhere else."""
    row = conn.execute(
        "SELECT external_ids FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None or not row["external_ids"]:
        return {}
    try:
        ext = json.loads(row["external_ids"])
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in ext.items()} if isinstance(ext, dict) else {}


def max_message_deletion_id(conn: sqlite3.Connection) -> int:
    """The highest `message_deletions.id`, or 0 when nothing was ever deleted.

    The room stream's O(1) gate for the deletion tail, mirroring
    `max_message_id` — on a deployment where nobody deletes anything (the
    overwhelming case) the per-user visibility join never runs at all."""
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM message_deletions"
    ).fetchone()
    return int(row["m"])


def list_message_deletions_since(
    conn: sqlite3.Connection, user_id: str, *, since_id: int, limit: int = 500,
) -> list[sqlite3.Row]:
    """Deletions with `id > since_id` in rooms ``user_id`` can see, oldest-first.

    Scoped by the same membership-minus-dismissal-minus-archived predicate the
    message tail uses, so a deletion frame can never disclose that a message
    existed in a room the caller was never in.
    """
    return conn.execute(
        "SELECT d.id AS id, d.message_id AS message_id, "
        "  d.room_token AS room_token "
        "FROM message_deletions d "
        "JOIN rooms r ON r.token = d.room_token AND r.archived = 0 "
        "JOIN room_members mm ON mm.room_token = d.room_token "
        "  AND mm.user_id = :user "
        "WHERE NOT EXISTS (SELECT 1 FROM room_dismissals dd "
        "  WHERE dd.room_token = d.room_token AND dd.user_id = :user) "
        "AND d.id > :since_id ORDER BY d.id ASC LIMIT :limit",
        {"user": user_id, "since_id": since_id, "limit": limit},
    ).fetchall()


def prune_message_deletions(conn: sqlite3.Connection, retention_days: int) -> int:
    """Drop ledger rows older than ``retention_days`` (0 = keep forever).

    The ledger only exists to catch clients up, and a client further behind
    than this has long since reloaded from scratch. Returns rows removed."""
    if retention_days <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM message_deletions "
        "WHERE deleted_at < datetime('now', ?)",
        (f"-{int(retention_days)} days",),
    )
    return cur.rowcount or 0


def get_message_room(conn: sqlite3.Connection, message_id: int) -> str | None:
    """The room token a message belongs to (for server-side membership checks
    on the star and delete endpoints), or None for an unknown id."""
    row = conn.execute(
        "SELECT room_token FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return row["room_token"] if row else None


def get_reply_target(
    conn: sqlite3.Connection, message_id: int,
) -> tuple[str, str] | None:
    """``(room_token, body)`` for a message a client wants to cite, or None.

    One read serving both halves of the send-side validation: the room the
    parent lives in (which must equal the room being posted into) and the body
    the server snapshots into `tasks.reply_to_content`. The snapshot is derived
    here rather than accepted from the client — it becomes text the model reads
    as an authoritative record of what it previously said, and there is no
    reason to trust a caller for a row we hold.
    """
    row = conn.execute(
        "SELECT room_token, body FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return (row["room_token"], row["body"]) if row else None


def get_starred_message_ids(
    conn: sqlite3.Connection, user_id: str, message_ids: list[int],
) -> set[int]:
    """The subset of `message_ids` this user has starred."""
    ids = [int(i) for i in message_ids]
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id FROM message_stars "
        f"WHERE user_id = ? AND message_id IN ({placeholders})",
        [user_id, *ids],
    ).fetchall()
    return {int(r["message_id"]) for r in rows}


# --- Shared cross-room read fragments -------------------------------------
#
# The paginated aggregate views (`list_messages_across_rooms`) and the live
# room-event tail (`list_room_events_since`, the web chat stream) must never
# disagree about what a user is allowed to see, so both are assembled from
# these fragments rather than each spelling the join out. Visibility =
# membership, minus dismissals, minus archived rooms — matching
# `list_member_rooms`. Bind parameter `:user` throughout.
_CROSS_ROOM_COLUMNS = (
    "SELECT m.role AS role, m.body AS body, m.title AS title, "
    "  m.task_id AS task_id, m.id AS msg_id, m.created_at AS created_at, "
    "  m.room_token AS room_token, r.name AS room_name, "
    "  m.attachments AS attachments, t.attachments AS task_attachments, "
    "  m.attachment_paths AS attachment_paths, "
    "  t.status AS status, t.actions_taken AS actions_taken, "
    "  t.execution_trace AS execution_trace, t.started_at AS started_at, "
    "  t.completed_at AS completed_at, t.model_used AS model_used, "
    "  (s.message_id IS NOT NULL) AS starred, "
    "  m.reply_to_message_id AS reply_to_message_id, "
    # Who wrote a `role='user'` row, for the cases where it is not the reader: a
    # co-member of a shared room, or the external contact whose mail was
    # mirrored in. Read off the row now that it records this. The recovery this
    # replaces re-derived the sender per read through a scalar subquery on
    # `processed_emails` (ISSUE-226) — correct, but it answered for email alone
    # and pinned a retention rule on the ledger to stay correct.
    "  m.author_user_id AS author_user_id, m.author_label AS author_label, "
    # Where the turn entered from. Already on the row and long dropped on the
    # way out, so a stranger's mail reached the client as an ordinary user
    # bubble with an unfamiliar name in it — no provenance, no collapse. The
    # dict builder decides what to publish (see `web_app._user_row_display`);
    # this fragment's job is only to stop losing it. Selected in the per-room
    # spine too (`web_app._SPINE_COLUMNS`), or a turn would read as external in
    # one view and ordinary in the other.
    "  m.origin_surface AS origin_surface, "
    # Truncated in SQLite rather than in the dict builder: this fragment also
    # backs the live room-event stream, which is byte-budgeted, and a reply to
    # a long answer would otherwise carry that whole answer a second time.
    # The literal must track `web_app._REPLY_EXCERPT_CHARS`, which the dict
    # builder still slices at — shorten this and that slice quietly no-ops.
    "  p.role AS reply_role, substr(p.body, 1, 200) AS reply_body "
)
_CROSS_ROOM_FROM = (
    "FROM messages m "
    "JOIN rooms r ON r.token = m.room_token AND r.archived = 0 "
    "JOIN room_members mm ON mm.room_token = m.room_token "
    "  AND mm.user_id = :user "
    "LEFT JOIN message_stars s ON s.message_id = m.id "
    "  AND s.user_id = :user "
    "LEFT JOIN tasks t ON t.id = m.task_id "
    # The cited parent (primary-key lookup). A NULL result against a non-NULL
    # `reply_to_message_id` is the deleted-parent case, which the client
    # renders muted rather than dropping.
    "LEFT JOIN messages p ON p.id = m.reply_to_message_id "
)
# System rows (alerts / logs / web-routed notifications) render in the
# aggregate views and stream too — count_unread_messages counts them, so
# Unread (and the live unread badge) must show them.
_CROSS_ROOM_WHERE = (
    "WHERE NOT EXISTS (SELECT 1 FROM room_dismissals d "
    "  WHERE d.room_token = m.room_token AND d.user_id = :user) "
    f"AND ({TRANSCRIPT_SURFACE_FILTER} OR m.role = 'system') "
)


def max_message_id(conn: sqlite3.Connection) -> int:
    """The highest `messages.id` in the whole store, or 0 when empty.

    The O(1) primary-key probe the room-event stream uses as a cheap gate: only
    when it exceeds a connection's cursor does the per-user visibility join run,
    so an idle deployment costs one trivial query per connection per tick."""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone()
    return int(row["m"])


def list_room_events_since(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    since_id: int,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Every message visible to ``user_id`` with ``id > since_id``, oldest-first.

    The tail behind the live web-chat room stream. Same visibility predicate and
    row shape as `list_messages_across_rooms` (shared SQL fragments), but
    cursored on the raw monotonic `messages.id` rather than the
    `(created_at, id)` keyset — one integer covering user turns, assistant
    turns and system messages across every room the user is a member of.

    ``limit`` is the caller's resource guard; pass ``max_batch + 1`` to detect
    truncation.
    """
    sql = (
        _CROSS_ROOM_COLUMNS + _CROSS_ROOM_FROM + _CROSS_ROOM_WHERE
        + "AND m.id > :since_id ORDER BY m.id ASC LIMIT :limit"
    )
    return conn.execute(sql, {
        "user": user_id,
        "since_id": since_id,
        "limit": limit,
    }).fetchall()


def list_messages_across_rooms(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    view: str = "all",
    limit: int = 50,
    before_ts: str | None = None,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    """One page of the cross-room message stream for the All / Unread / Starred
    web views, newest-first, keyset-paginated on ``(created_at, id)``.

    Reads the durable `messages` store only (no `tasks` gap-fill, no in-flight
    placeholders — a cross-room reading surface doesn't need the live-room aux
    merge). Visibility = membership minus dismissals minus archived rooms,
    matching `list_member_rooms`. Rows carry the room token/name, the LEFT JOIN
    `tasks` enrichment columns the per-room spine also selects, and a `starred`
    flag for the requesting user.

    Views:
    - ``all``: every transcript-rendered row, own turns included.
    - ``unread``: rows past the user's per-room web read cursor, excluding
      their own turns — the same math as `count_unread_messages`, so the view
      and the sidebar badges always agree. A room with no cursor row yet
      contributes everything (COALESCE to 0); in practice the rooms listing
      seeds cursors on every load.
    - ``starred``: rows the user has starred, still in transcript order.
    """
    if view not in ("all", "unread", "starred"):
        raise ValueError(f"unknown view: {view!r}")
    sql = _CROSS_ROOM_COLUMNS + _CROSS_ROOM_FROM
    if view == "unread":
        sql += (
            "LEFT JOIN room_read_state rs ON rs.room_token = m.room_token "
            "  AND rs.surface = 'web' AND rs.user_id = :user "
        )
    sql += _CROSS_ROOM_WHERE
    if view == "unread":
        sql += (
            "AND m.role != 'user' "
            "AND m.id > COALESCE(rs.last_read_message_id, 0) "
        )
    elif view == "starred":
        sql += "AND s.message_id IS NOT NULL "
    if before_ts is not None:
        sql += "AND (m.created_at, m.id) < (:before_ts, :before_id) "
    sql += "ORDER BY m.created_at DESC, m.id DESC LIMIT :limit"
    return conn.execute(sql, {
        "user": user_id,
        "limit": limit,
        "before_ts": before_ts,
        "before_id": before_id,
    }).fetchall()


def mark_all_rooms_read(conn: sqlite3.Connection, user_id: str) -> int:
    """Advance the user's web read cursor to the newest message in every room
    they can see (same visibility as `list_member_rooms`). Returns the number
    of rooms whose cursor actually moved."""
    return len(mark_all_rooms_read_tokens(conn, user_id))


def mark_all_rooms_read_tokens(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Same as `mark_all_rooms_read`, returning the tokens of the rooms whose
    cursor actually moved — the web→Talk read-sync push needs the identities,
    not just the count (only actually-advanced rooms get an NC call)."""
    moved: list[str] = []
    for room in list_member_rooms(conn, user_id):
        max_id = room_max_message_id(conn, room.token)
        if max_id > get_room_read_state(conn, room.token, "web", user_id):
            set_room_read_state(conn, room.token, "web", max_id, user_id)
            moved.append(room.token)
    return moved


def _migrate_unified_rooms(conn: sqlite3.Connection) -> None:
    """One-time fold of legacy stores into the unified room model.

    Markered (`unified_rooms_v1`) so the heavier backfills (web_chat_messages
    copy, distinct-Talk-token scan over `tasks`) run once. Each step is also
    structurally idempotent (INSERT OR IGNORE / marker), so a re-run before the
    marker is set is harmless. No-op on fresh installs (legacy tables empty or
    not yet created — wrapped in try/except)."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'unified_rooms_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return

    def _step(fn) -> bool:
        """Run one backfill step. A genuinely-absent legacy table ("no such
        table") is the fresh-install path and is benign — skip it and keep
        going. Any other OperationalError (disk I/O, locked, constraint) is a
        real mid-backfill failure: log it and signal abort so the completion
        marker is *not* written and the next boot retries the whole fold. Every
        step is structurally idempotent (INSERT OR IGNORE / NOT EXISTS), so a
        partial first run replays cleanly."""
        try:
            fn()
            return True
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return True  # legacy table absent -> nothing to fold, not a failure
            logger.warning("unified_rooms migration step failed, will retry: %s", e)
            return False

    def _fold_web_rooms():
        # web_chat_rooms -> rooms (origin=web) + self-referential web binding.
        conn.execute(
            "INSERT OR IGNORE INTO rooms (token, user_id, name, origin, archived) "
            "SELECT token, user_id, name, 'web', archived FROM web_chat_rooms"
        )
        conn.execute(
            "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
            "SELECT token, 'web', token FROM web_chat_rooms"
        )

    def _fold_talk_rooms():
        # Distinct Talk conversation_tokens -> rooms (origin=talk) + talk binding.
        # Only interactive Talk tasks; scheduled/briefing/etc tokens aren't rooms.
        conn.execute(
            "INSERT OR IGNORE INTO rooms (token, user_id, name, origin) "
            "SELECT conversation_token, user_id, NULL, 'talk' FROM tasks "
            "WHERE source_type = 'talk' AND conversation_token IS NOT NULL "
            "GROUP BY conversation_token"
        )
        conn.execute(
            "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
            "SELECT token, 'talk', token FROM rooms WHERE origin = 'talk'"
        )

    def _fold_web_messages():
        # web_chat_messages -> messages (role=system, task_id NULL). Guarded so
        # this one-time copy isn't duplicated on the rare pre-marker re-run.
        conn.execute(
            "INSERT INTO messages "
            "(room_token, role, body, title, task_id, origin_surface, created_at) "
            "SELECT w.token, w.role, w.text, w.title, NULL, 'web', w.created_at "
            "FROM web_chat_messages w "
            "WHERE w.token IN (SELECT token FROM rooms) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM messages m "
            "  WHERE m.room_token = w.token AND m.origin_surface = 'web' "
            "    AND m.task_id IS NULL AND m.body = w.text "
            "    AND IFNULL(m.title,'') = IFNULL(w.title,'') "
            "    AND m.created_at = w.created_at"
            ")"
        )

    def _backfill_turns():
        # Backfill the canonical messages store (user+assistant turns) from
        # completed tasks for every registered room, so the unified history
        # reader has the historical backlog. Live writes (Stage 3/4) keep it
        # current going forward. Scheduled-job turns are normalized (assistant
        # post only, NO_ACTION ticks omitted) — see `_backfill_turns_for`.
        _backfill_turns_for(
            conn, "conversation_token IN (SELECT token FROM rooms)", (),
        )

    ok = True
    for step in (_fold_web_rooms, _fold_talk_rooms, _fold_web_messages, _backfill_turns):
        if not _step(step):
            ok = False
            break

    # Only mark the fold complete if every step succeeded. A swallowed
    # mid-backfill failure used to set the marker anyway, stranding a partially
    # populated `messages` store that never retried.
    if ok:
        conn.execute(
            "INSERT OR IGNORE INTO _migration_state (name) VALUES ('unified_rooms_v1')"
        )


def _migrate_processed_emails_uidvalidity(conn: sqlite3.Connection) -> None:
    """Rebuild `processed_emails` so the dedupe key is (uidvalidity, email_id)
    rather than the bare IMAP UID (ISSUE-250).

    A UID identifies a message only within its folder's UIDVALIDITY. On the old
    key, a mailbox recreation or a server migration restarted numbering at 1
    and every new message matched an existing row: `is_email_processed` said
    yes, so the mail was dropped, and the insert that eventually ran raised
    IntegrityError. `UNIQUE` was declared inline on the column, which makes it
    an implicit index no `DROP INDEX` can reach, so widening it needs a table
    rebuild.

    Self-guarding on the live DDL, like `_migrate_web_chat_rooms_peruser`: a
    no-op on a fresh install (already composite) and on re-runs. Existing rows
    get `uidvalidity = 0`, the same "not reported" namespace a server that will
    not answer STATUS produces — so a deployment whose UIDVALIDITY is readable
    starts a fresh namespace on the next poll and re-ingests nothing, because
    the cursor starts from 0 and the rows it walks past are its own.
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'processed_emails'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    sql = row[0] if row and row[0] else ""
    if not sql or "UNIQUE (uidvalidity, email_id)" in sql:
        return  # already migrated (or fresh install with the new DDL)
    # Explicitly transactional. SQLite makes DDL transactional, but Python's
    # legacy `isolation_level` only opens a transaction for DML — so left to
    # autocommit, a failure after the RENAME leaves no `processed_emails` at
    # all. `init_db` runs `schema.sql` immediately afterwards, whose
    # `CREATE TABLE IF NOT EXISTS` would then recreate it *empty*, the next run
    # would see the new DDL and no-op forever, and the first poll against an
    # empty ledger re-ingests every message in the mailbox as a fresh task.
    # A dropped dedupe ledger is a mail storm, so it gets a real rollback.
    #
    # Commit whatever the earlier migrations left open first (ISSUE-261).
    # `_run_migrations` runs every migration on one connection in Python's
    # legacy `isolation_level` mode, where a DML statement opens an implicit
    # transaction and holds it until someone commits — and a zero-row UPDATE
    # is enough. Two earlier migrations are DML (the `briefing_configs` output
    # backfill and the `knowledge_facts` dedupe, the latter unconditional on
    # any DB that has the table, which is every released one), so `BEGIN
    # IMMEDIATE` here raised "cannot start a transaction within a transaction"
    # on every upgraded DB. The rebuild rolled back and re-armed, so it failed
    # identically every time migrations ran, while the poller shipped in the
    # same commit queried a `uidvalidity` column that was therefore never
    # created: inbound email was dead. Nothing on the daemon or web startup
    # path runs framework migrations — only `istota init` and the auto-update
    # script do — so a service restart never cleared it either. Committing
    # also scopes the handler's ROLLBACK below to this rebuild's own work,
    # instead of discarding the earlier migrations'.
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Re-read the guard now that the write lock is held. The check above
        # ran under the inherited transaction's snapshot, and the commit that
        # followed dropped the lock — so a concurrent `istota init` (the
        # auto-update script runs one against a live daemon) could have
        # completed the rebuild in between. Acting on the stale answer would
        # rename the already-migrated table and re-copy it with `uidvalidity`
        # forced back to 0, and since adoption only runs on a folder's first
        # poll those rows would never be re-namespaced: the next poll would
        # re-ingest the whole mailbox.
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'processed_emails'"
        ).fetchone()
        sql = row[0] if row and row[0] else ""
        if not sql or "UNIQUE (uidvalidity, email_id)" in sql:
            conn.execute("COMMIT")
            return
        conn.execute(
            "ALTER TABLE processed_emails RENAME TO _processed_emails_old"
        )
        conn.execute("""
            CREATE TABLE processed_emails (
                id INTEGER PRIMARY KEY,
                uidvalidity INTEGER NOT NULL DEFAULT 0,
                email_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT,
                thread_id TEXT,
                message_id TEXT,
                "references" TEXT,
                user_id TEXT,
                task_id INTEGER,
                routing_method TEXT,
                processed_at TEXT DEFAULT (datetime('now')),
                UNIQUE (uidvalidity, email_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)
        # Preserve ids: `get_email_for_task` and the conversation-history
        # readers join on task_id, but the row identity is worth keeping stable
        # for anything holding one.
        conn.execute("""
            INSERT INTO processed_emails
            (id, uidvalidity, email_id, sender_email, subject, thread_id,
             message_id, "references", user_id, task_id, routing_method,
             processed_at)
            SELECT id, 0, email_id, sender_email, subject, thread_id,
                   message_id, "references", user_id, task_id, routing_method,
                   processed_at
            FROM _processed_emails_old
        """)
        conn.execute("DROP TABLE _processed_emails_old")
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        # `sqlite3.Error`, not just OperationalError: an IntegrityError or a
        # DatabaseError out of the INSERT…SELECT would otherwise escape and
        # abort `init_db` outright, leaving the renamed table behind.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        logger.warning(
            "processed_emails uidvalidity rebuild failed and was rolled back; "
            "the ledger is unchanged and the rebuild will be retried: %s", e,
        )


def _migrate_web_chat_rooms_peruser(conn: sqlite3.Connection) -> None:
    """Rebuild `web_chat_rooms` so `token` is unique per (user, token) rather
    than globally (ISSUE-134), letting every participant of a shared Talk room
    hold their own handle. Self-guarding: inspects the live table DDL and only
    rebuilds the legacy single-token-UNIQUE shape, so it's a no-op on fresh
    installs (already composite) and on re-runs."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'web_chat_rooms'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    sql = row[0] if row and row[0] else ""
    if not sql or "UNIQUE (user_id, token)" in sql:
        return  # already migrated (or fresh install with the new DDL)
    try:
        conn.execute("ALTER TABLE web_chat_rooms RENAME TO _web_chat_rooms_old")
        conn.execute("""
            CREATE TABLE web_chat_rooms (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                name        TEXT NOT NULL,
                archived    INTEGER NOT NULL DEFAULT 0,
                color       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, token)
            )
        """)
        # Preserve ids so any in-flight frontend room id stays valid.
        #
        # `color` (ISSUE-433) is in the CREATE above because this rebuild runs
        # *after* the ALTER that adds it, so omitting it would drop the column
        # again on exactly the legacy-shape database this function exists for.
        # It is carried in the INSERT only when the old table actually has it,
        # rather than being named unconditionally or left to its default:
        # naming it unconditionally makes the statement depend on a shape this
        # function cannot assume, and the failure arm below merely warns, so a
        # raise here would strand every row in `_web_chat_rooms_old`. Leaving
        # it to the default silently resets every colour on one narrow path —
        # the ALTER succeeds, this rebuild fails and only warns, the deployment
        # runs and colours are set against the legacy-shaped table, and the
        # next boot's successful rebuild drops them. Asking the table closes
        # that without assuming anything.
        old_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(_web_chat_rooms_old)")
        }
        carried = ["id", "user_id", "token", "name", "archived",
                   "created_at", "updated_at"]
        if "color" in old_cols:
            carried.append("color")
        cols = ", ".join(carried)  # code-owned literals, never user input
        conn.execute(
            f"INSERT INTO web_chat_rooms ({cols}) "
            f"SELECT {cols} FROM _web_chat_rooms_old"
        )
        conn.execute("DROP TABLE _web_chat_rooms_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user "
            "ON web_chat_rooms (user_id, archived, id)"
        )
    except sqlite3.OperationalError as e:
        logger.warning("web_chat_rooms per-user rebuild failed: %s", e)


def _migrate_room_read_state_peruser(conn: sqlite3.Connection) -> None:
    """Add `user_id` to `room_read_state`'s key (ISSUE-134) so an unread cursor
    is per participant. Read cursors are ephemeral and there are no readers yet,
    so the legacy table is dropped and recreated rather than backfilled.
    Self-guarding on the DDL; no-op once `user_id` is present."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'room_read_state'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    sql = row[0] if row and row[0] else ""
    if not sql or "user_id" in sql:
        return
    try:
        conn.execute("DROP TABLE room_read_state")
        conn.execute("""
            CREATE TABLE room_read_state (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface     TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT '',
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_token, surface, user_id)
            )
        """)
    except sqlite3.OperationalError as e:
        logger.warning("room_read_state per-user rebuild failed: %s", e)


def _migrate_room_members(conn: sqlite3.Connection) -> None:
    """Backfill `room_members` for existing deploys (ISSUE-134). Folds in every
    participant of every registered room so a shared Talk room surfaces for all
    of them, not just the arbitrary `rooms.user_id` the unified-rooms fold picked.

    Markered (`room_members_v1`); each insert is `OR IGNORE` so a pre-marker
    re-run is harmless. Sources: the registry owner, every web handle's user, and
    every distinct Talk-task sender for a token that is a room."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'room_members_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return

    # `rooms` / `web_chat_rooms` always exist by now (created in init_db's CREATE
    # block before migrations run), so a failure on those is a real error and
    # must NOT mark the migration done. `tasks`, however, is created by schema.sql
    # which runs *after* migrations on a fresh install — its absence is the
    # benign fresh-install case (nothing to backfill), tolerated like
    # `_migrate_unified_rooms._step` does. Mirrors that per-step contract.
    try:
        # The registry owner is always a member.
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT token, user_id FROM rooms"
        )
        # Every web handle's owner (covers web-origin rooms + any prior handle).
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT token, user_id FROM web_chat_rooms "
            "WHERE token IN (SELECT token FROM rooms)"
        )
    except sqlite3.OperationalError as e:
        logger.warning("room_members backfill failed, will retry: %s", e)
        return  # leave the marker unset so the next boot retries

    try:
        # Every distinct human who sent an interactive Talk turn into the room.
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT conversation_token, user_id FROM tasks "
            "WHERE source_type = 'talk' AND conversation_token IS NOT NULL "
            "AND conversation_token IN (SELECT token FROM rooms) "
            "GROUP BY conversation_token, user_id"
        )
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            logger.warning("room_members talk backfill failed, will retry: %s", e)
            return
        # `tasks` absent → fresh install, nothing to fold; fall through and mark.

    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) VALUES ('room_members_v1')"
    )


def _migrate_scheduled_transcript_cleanup(conn: sqlite3.Connection) -> None:
    """Repair scheduled-job rows the earlier blanket backfill folded into the
    canonical `messages` store verbatim (ISSUE-133 follow-up).

    `_migrate_unified_rooms._backfill_turns` originally copied every completed
    task's raw `result` into `messages`, so silent location/monitor crons left a
    trail of literal `NO_ACTION:` and `ACTION: …`-prefixed assistant rows plus
    empty synthetic-prompt user rows — all of which the web transcript reader
    renders (it shows `origin_surface='scheduled'` assistant posts). This brings
    the historical rows in line with what was actually delivered:

      * drop scheduled `user` rows (the cron prompt was never user-authored),
      * drop scheduled `NO_ACTION:` assistant rows (never posted anywhere),
      * strip the `ACTION:` prefix from the rest.

    Markered (`scheduled_transcript_cleanup_v1`); idempotent regardless. No-op on
    fresh installs (nothing matches)."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return
    try:
        conn.execute(
            "DELETE FROM messages WHERE origin_surface = 'scheduled' AND role = 'user'"
        )
        conn.execute(
            "DELETE FROM messages WHERE origin_surface = 'scheduled' "
            "AND role = 'assistant' AND body LIKE 'NO_ACTION:%'"
        )
        conn.execute(
            "UPDATE messages SET body = TRIM(SUBSTR(body, 8)) "
            "WHERE origin_surface = 'scheduled' AND role = 'assistant' "
            "AND body LIKE 'ACTION:%'"
        )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install, messages not created yet
        logger.warning("scheduled transcript cleanup failed: %s", e)
        return
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) "
        "VALUES ('scheduled_transcript_cleanup_v1')"
    )


def _migrate_nonconversational_transcript_cleanup(conn: sqlite3.Connection) -> None:
    """Normalize the non-conversational rows the `unified_rooms_v1` blanket
    backfill folded into the canonical `messages` store (ISSUE-176).

    `_migrate_unified_rooms._backfill_turns` ran with no source-type filter, so
    it inserted a `user` row (raw synthetic prompt) and an `assistant` row (raw
    `result`) for every completed task of every registered room — including
    `subtask` / `briefing` / etc., not just conversational turns. When the
    generalized `TRANSCRIPT_SURFACE_FILTER` (assistant-any) goes live, those
    hidden rows would surface at read time: briefing assistant rows as raw
    `{"subject":…,"body":…}` JSON, and the synthetic user rows would re-pair
    into LLM context (breaking the "user rows conversational-only" invariant).

    This one-shot generalizes `_migrate_scheduled_transcript_cleanup` from
    scheduled-only to every non-conversational source type. Over rows whose
    `origin_surface NOT IN ('web','talk','scheduled','email')` (scheduled is
    owned by its own marker, conversational surfaces are real turns, and `email`
    joined them at ISSUE-136 — see the comment on the statement itself), and
    never touching
    `role='system'` (the notification/log lane), it:

      * drops the `role='user'` rows — synthetic prompts, never user-authored;
        this is what restores the invariant for every reader without touching
        the context builders,
      * repairs `role='briefing'` assistant bodies that are stored JSON to the
        delivered body (the same parse live delivery does), leaving anything
        that doesn't parse — and every other source type's verbatim body — as-is
        (a plain-text subtask block is already exactly what was delivered).

    Markered (`nonconversational_transcript_cleanup_v1`); idempotent regardless.
    No-op on fresh installs (nothing matches). Must ship with the filter flip —
    it is what makes the read-time reveal clean."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state "
            "WHERE name = 'nonconversational_transcript_cleanup_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return
    try:
        # Resolve the briefing parser BEFORE any mutation: init_db commits this
        # migration in one transaction, so a mid-migration import failure after
        # the DELETE would commit a half-applied state (user rows dropped) with
        # no marker, leaving briefing rows to reveal as raw JSON until a later
        # deploy re-runs it. Importing first means a failure aborts cleanly with
        # zero mutation, and the unmarked retry re-applies the whole thing.
        from .skills.briefing import parse_briefing_json

        # Drop synthetic non-conversational user rows (restore the invariant).
        #
        # `email` is spared alongside the conversational surfaces (ISSUE-136):
        # an email user row is a real inbound message continuing a real room, not
        # a synthesized prompt, and `TRANSCRIPT_SURFACE_FILTER` now renders it.
        # This list and that filter must stay in sync — the two `except` branches
        # below `return` *after* this DELETE without writing the marker, so a
        # partial run re-arms the migration, and a DB restored from a
        # pre-migration snapshot re-runs it from scratch. Either would silently
        # strip live email turns back to the orphaned-reply state ISSUE-136 fixed.
        #
        # **Spelled out rather than built from `TRANSCRIPT_SURFACES`**, which is
        # the question it is asking one step removed: this list is that set plus
        # `scheduled`, whose synthetic user rows the `unified_rooms_v1` backfill
        # inserted and which no surface table will ever hold. The relation is a
        # superset, not an equality, so a pinned string cannot express it and
        # interpolating the tuple would quietly drop the fourth value on the
        # next reader who assumed the two were the same list.
        conn.execute(
            "DELETE FROM messages "
            "WHERE role = 'user' "
            "AND origin_surface NOT IN ('web', 'talk', 'scheduled', 'email')"
        )
        # Normalize briefing assistant bodies that were stored as raw JSON.
        briefing_rows = conn.execute(
            "SELECT id, body FROM messages "
            "WHERE role = 'assistant' AND origin_surface = 'briefing'"
        ).fetchall()
        for row in briefing_rows:
            parsed = parse_briefing_json(row["body"] or "")
            if parsed and parsed.get("body") is not None:
                conn.execute(
                    "UPDATE messages SET body = ? WHERE id = ?",
                    (parsed["body"], row["id"]),
                )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install, messages not created yet
        logger.warning("nonconversational transcript cleanup failed: %s", e)
        return
    except Exception as e:  # briefing parser import / edge — don't wedge init
        logger.warning("nonconversational transcript cleanup failed: %s", e)
        return
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) "
        "VALUES ('nonconversational_transcript_cleanup_v1')"
    )


def _migrate_messages_author(conn: sqlite3.Connection) -> None:
    """Backfill `messages.author_user_id` / `author_label` for existing rows.

    Every `role='user'` row whose author is recoverable belongs to that task's
    user, except the email turns an external contact wrote — those get the
    sanitized sender label and no user id. Rows with no task (the `task_id IS
    NULL` confirmation-exchange and steer rows) keep both columns NULL and fall
    back to the room owner. They predate the columns, so nothing recorded who
    typed them; new ones carry an author.

    Assistant and system rows are left alone. The bot is not a user and has no
    label; readers already know an assistant row is the assistant.

    **The email pass runs first, and its identity comes from `processed_emails`
    rather than from `tasks`.** Both matter. `messages` is never age-pruned
    while `tasks` is (`task_retention_days`, default 7), and
    `cleanup_old_processed_emails` deliberately refuses to prune a row a
    `messages` row still references — precisely so an email turn's attribution
    outlives its task. Joining `tasks` would therefore drop the label for every
    turn older than a week and, because the marker is one-shot, drop it
    permanently. Running it before the blanket pass matters because `init_db`
    commits all migrations in one transaction with no rollback on the handled
    error paths: with the order reversed, a failure between the two would
    durably commit "this external contact's mail is the room owner's own
    words" — a positive mislabelling rather than a neutral NULL.

    Markered (`messages_author_v1`) and idempotent regardless — each pass is
    scoped to rows that are still unattributed, so a re-run after a partial pass
    finishes the job rather than rewriting it. Any failure returns without
    writing the marker, re-arming the whole migration; a partially backfilled
    table renders correctly in the meantime, because a NULL author falls back to
    the room owner exactly as it did before the columns existed.
    """
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'messages_author_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return
    try:
        # Pass 1: the exception, and the one that has to run first (see above).
        # An email turn whose sender is not the task's own user was written by
        # someone else, and saying otherwise is the mislabelling these columns
        # exist to end.
        #
        # Row by row, because the sanitizer (`external_email_sender`) is Python
        # — a regex and a length bound SQL cannot express, and the one thing
        # standing between a raw `From:` header and the rendered speaker
        # position. `tasks` is LEFT JOINed for its `user_id` only, falling back
        # to the `processed_emails` copy when retention has taken the task.
        email_rows = conn.execute(
            "SELECT m.id AS mid, pe.sender_email AS sender_email, "
            "  pe.routing_method AS routing_method, "
            "  COALESCE(t.user_id, pe.user_id) AS user_id "
            "FROM messages m "
            "JOIN processed_emails pe ON pe.task_id = m.task_id "
            "LEFT JOIN tasks t ON t.id = m.task_id "
            "WHERE m.role = 'user' AND m.origin_surface = 'email' "
            "AND m.author_user_id IS NULL AND m.author_label IS NULL "
            # `processed_emails.task_id` is not unique, so a message with two
            # ledger rows comes back twice; the loop keeps the first. Ordering
            # by `pe.id` makes "first" the oldest row, matching what
            # `EMAIL_SENDER_SUBQUERY` picks for the same message.
            "ORDER BY m.id, pe.id"
        ).fetchall()
        # One address lookup per user, not per row: this holds init_db's write
        # transaction, and the same user owns most of a room's mail.
        own_by_user: dict[str, list[str]] = {}
        seen_messages: set[int] = set()
        for row in email_rows:
            if row["mid"] in seen_messages:
                continue
            seen_messages.add(row["mid"])
            user_id = row["user_id"]
            if (row["routing_method"] or "") == "sender_match":
                continue  # defined as the own-address match; leave to pass 2
            if user_id not in own_by_user:
                own_by_user[user_id] = own_addresses_without_config(conn, user_id)
            author_label = external_email_sender(
                row["sender_email"], own_by_user[user_id],
            )
            if author_label:
                conn.execute(
                    "UPDATE messages SET author_user_id = NULL, author_label = ? "
                    "WHERE id = ?",
                    (author_label, row["mid"]),
                )
        # Pass 2: the common case, in one statement. Everything still
        # unattributed and still joinable to a task belongs to that task's user.
        conn.execute(
            "UPDATE messages SET author_user_id = ("
            "  SELECT t.user_id FROM tasks t WHERE t.id = messages.task_id"
            ") "
            "WHERE role = 'user' AND task_id IS NOT NULL "
            "AND author_user_id IS NULL AND author_label IS NULL "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = messages.task_id)"
        )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install, messages/tasks not created yet
        logger.warning("messages author backfill failed: %s", e)
        return
    except Exception as e:  # never wedge init over an attribution backfill
        logger.warning("messages author backfill failed: %s", e)
        return
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) "
        "VALUES ('messages_author_v1')"
    )


def _migrate_notifications(conn: sqlite3.Connection) -> None:
    """Create the `notifications` inbox table and its indexes on existing DBs.

    Pure DDL, all `IF NOT EXISTS`, so it is idempotent and needs no marker row —
    there is nothing here that a second run could do twice, and no backfill to
    skip. It also wants no transaction of its own, which is what makes it safe
    wherever in the shared-connection list it sits: sqlite3's legacy
    `isolation_level` mode issues an implicit BEGIN for DML only, so these
    statements join whichever transaction the migrations before them left open
    instead of colliding with it (the ISSUE-261 shape). It reads and alters no
    existing table, so there is nothing for a failure here to leave half done.

    Kept in sync with the block at the end of `schema.sql`, which is what a
    fresh install gets. See there for the column and index commentary.
    """
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           TEXT NOT NULL,
                source            TEXT NOT NULL,
                dedup_key         TEXT NOT NULL,
                object_type       TEXT,
                object_id         TEXT,
                severity          TEXT NOT NULL DEFAULT 'info',
                actionable        INTEGER NOT NULL DEFAULT 0,
                title             TEXT NOT NULL,
                body              TEXT NOT NULL DEFAULT '',
                params            TEXT NOT NULL DEFAULT '{}',
                link              TEXT,
                room_token        TEXT,
                created_at        TEXT NOT NULL
                                  DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at        TEXT NOT NULL
                                  DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                last_delivered_at TEXT,
                occurrences       INTEGER NOT NULL DEFAULT 1,
                seen_at           TEXT,
                state             TEXT NOT NULL DEFAULT 'open',
                resolved_at       TEXT,
                resolved_by       TEXT,
                UNIQUE (user_id, source, dedup_key)
            )
        """)
        # Leads with `user_id` — see the schema.sql comment; without it every
        # per-user-module source resolves across users.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_state "
            "ON notifications (user_id, state, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_object "
            "ON notifications (user_id, source, object_type, object_id)"
        )
    except sqlite3.OperationalError as e:
        # The next boot retries; the bell is empty until then rather than init
        # failing outright.
        logger.warning("notifications table migration failed, will retry: %s", e)


# The one-shot guard on `_backfill_notifications`.
_NOTIFICATIONS_BACKFILL_MARKER = "notifications_backfill_v1"


def _iso_z_from_sql_datetime(value: str | None) -> str:
    """A `datetime('now')` timestamp in the notifications table's ISO-Z form.

    Named for both formats on purpose. `web_app` has its own `_iso_z`, which
    takes a `datetime` and is the other half of the same hazard `web-ui.md`
    records: this database stores two timestamp spellings, `' '` sorts below
    `'T'`, and a bound built in the wrong one silently drops a boundary day.

    `tasks.created_at` and `outbound_drafts.created_at` store
    `YYYY-MM-DD HH:MM:SS`; `notifications.created_at` stores the ISO-Z
    millisecond form `iso_utc_now` writes. The column is compared
    lexicographically — by the two sweeps' bounds, by `ORDER BY updated_at`, and
    by `mark_seen`'s version check — so a second spelling in it would sort and
    compare against the first in ways nobody intended. Anything unparseable
    falls back to now: a first-seen date is worth having right and is not worth
    dropping an inbox row over.
    """
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    return iso_utc_now()


def _backfill_notifications(conn: sqlite3.Connection) -> None:
    """Seed the inbox from the two queues that already exist (one-shot).

    Every task in `pending_confirmation` and every `outbound_drafts` row in
    `pending` is something waiting on a user *right now*. Before the inbox, the
    web UI showed both in two floating strips above the chat transcript; those
    come out in the same change as this, so without the backfill an upgrade
    silently empties a user's held queue — the objects stay parked, and nothing
    in the app says so.

    Three things this has to get right, all of them load-bearing:

    **The keys are the producers' own.** `source`, `dedup_key`, `object_type`
    and `object_id` all come from the resolver modules the producers call, not
    from strings retyped here. Idempotency is claimed from
    `UNIQUE (user_id, source, dedup_key)`, so one character of drift means every
    held item shows twice, permanently, with only one of the two closable — and
    the constraint that is supposed to catch it never fires, because the keys
    differ. `INSERT OR IGNORE` rather than the store's own `write_notification`:
    that call is a read-modify-write whose `open` branch bumps `occurrences`, and
    a backfill is not a second occurrence of the thing being notified about.

    **It commits the transaction it inherited before beginning its own.**
    `_run_migrations` shares one connection in Python's legacy `isolation_level`
    mode, where a DML statement — a zero-row UPDATE is enough — opens an implicit
    transaction and holds it open. `BEGIN IMMEDIATE` under one of those raises
    "cannot start a transaction within a transaction" on exactly the DBs with
    the most to lose, and passes on a fresh install: the ISSUE-261 shape, which
    shipped green and killed inbound email for two days. It wants its own
    transaction so the rows and the marker land together — a marker written over
    a half-inserted pass would strand the rest for good.

    **The title never comes from `tasks.prompt`.** For a gated email that column
    is the untrusted body the gate is withholding. It comes from
    `confirmations.describe`, which reads sender and subject off
    `processed_emails` and flattens both. `confirmations` imports this module,
    so the import is function-local.

    Nothing here delivers. A migration that pushed would fan every held item in
    the backlog out to Talk and ntfy at once, and `last_delivered_at` is left
    null so nothing later mistakes these for rows a user was told about.

    Two scope calls worth stating rather than leaving to be discovered:

    `pending` drafts only, not `sending`. A row stuck in `sending` is one whose
    process died between the claim and the finalize, and the resolver *does*
    render it — with a status note and no actions, because nobody can say
    whether the mail went out. Seeding it here would need a per-row `actionable`
    and a stored body that does not say "Nothing was sent", which the spec
    scoped to `pending` and this stage is not the place to widen. A pre-upgrade
    row in that state stays invisible; noted so somebody can decide it.

    The keys are the producers' own; the stored *text* deliberately is not
    everywhere. The daemon's draft producer names only the single recipient it
    held on, while this names the whole To/Cc set (Bcc by count) because the row
    is right here to read. Both are fallback text — the resolver rebuilds title
    and body from the live draft on every panel read — and the fuller one is the
    better fallback.

    Markered (`notifications_backfill_v1`) and structurally idempotent
    regardless. Any failure returns without writing the marker, so the next
    `istota init` retries the whole pass.
    """
    from . import confirmations  # noqa: PLC0415 — `confirmations` imports db
    from . import outbound_drafts as drafts  # noqa: PLC0415
    from .notification_resolvers import confirmation as confirmation_source  # noqa: PLC0415
    from .notification_resolvers import outbound_draft as draft_source  # noqa: PLC0415

    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = ?",
            (_NOTIFICATIONS_BACKFILL_MARKER,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Not the fresh-install path — `_migration_state` is created earlier in
        # `_run_migrations`, on this same connection. Reaching here means an
        # earlier statement in that block already failed, so the honest move is
        # to do nothing and let the next run try again.
        return
    if already:
        return

    # Read before the write lock is taken, so a large backlog is not held over
    # the whole scan. Re-reading under the lock would buy nothing: the objects
    # can only stop being held, and `INSERT OR IGNORE` plus the resolvers'
    # close paths handle one that did.
    #
    # Every read below is inside this block, not only the two queries. The row
    # build calls `get_task` and `confirmations.describe`, both of which read —
    # and `describe` reads `processed_emails`, whose `uidvalidity` column is
    # created by a migration that logs and re-arms on failure rather than
    # raising. An unguarded build therefore turned that migration's documented
    # retry state into "no such column: uidvalidity" escaping out of
    # `_run_migrations`, which aborts `init_db` before `schema.sql` runs and so
    # kills every migration after this one. That is the ISSUE-261 blast radius
    # reached by a different road.
    rows: list[tuple] = []
    try:
        held_tasks = conn.execute(
            "SELECT id FROM tasks WHERE status = ? ORDER BY id",
            (confirmation_source.HELD_STATUS,),
        ).fetchall()
        # The statuses come from the modules that write them, never re-spelled
        # here — the same rule the resolvers state about their own literals, and
        # the other half of the keys being the producers' own.
        held_drafts = conn.execute(
            "SELECT id, user_id, to_addrs, cc_addrs, bcc_addrs, subject, "
            "       room_token, created_at "
            "  FROM outbound_drafts WHERE status = ? ORDER BY id",
            (drafts.STATUS_PENDING,),
        ).fetchall()
        rows = _notification_backfill_rows(
            conn, held_tasks, held_drafts, confirmations, confirmation_source,
            draft_source,
        )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install — the tables arrive with schema.sql below
        logger.warning("notification backfill could not read the queues: %s", e)
        return
    except Exception as e:  # noqa: BLE001 — never be what aborts init_db
        logger.warning("notification backfill could not read the queues: %s", e)
        return

    _notification_backfill_write(conn, rows)


def _notification_backfill_rows(
    conn, held_tasks, held_drafts, confirmations, confirmation_source, draft_source,
) -> list[tuple]:
    """The rows to insert. Reads only; the caller owns the guard."""
    rows: list[tuple] = []
    now = iso_utc_now()
    for record in held_tasks:
        task = get_task(conn, record[0])
        if task is None or not task.user_id:
            continue
        rows.append((
            task.user_id,
            confirmation_source.SOURCE,
            confirmation_source.dedup_key(task.id),
            confirmation_source.OBJECT_TYPE,
            str(task.id),
            confirmation_source.SEVERITY,
            confirmations.describe(conn, task),
            confirmation_source.body_for(task.confirmation_prompt),
            # Deliberately null, unlike the draft rows below. `room_token` is
            # provenance and nothing reads it yet; the two producers disagree
            # about it (the email gate writes none), and a held task's
            # `conversation_token` may be a synthetic thread hash naming no room
            # at all — which is the case the strips existed for. A guess is
            # worse than the absence.
            None,
            _iso_z_from_sql_datetime(task.created_at),
            now,
        ))
    for record in held_drafts:
        user_id = record["user_id"]
        if not user_id:
            # `notifications.user_id` is NOT NULL and the panel is per-user, so
            # a row nobody owns is unreachable — writing one would only fail
            # the pass that rescues everybody else's.
            logger.warning(
                "notification backfill skipped draft %s: no user", record["id"],
            )
            continue
        to_addrs = _json_list(record["to_addrs"])
        # To and Cc by address, Bcc by count only — `!drafts`' rule, and the
        # same reason: a row's stored text can be delivered into a shared room.
        recipients = draft_source.visible_recipients(
            to_addrs,
            _json_list(record["cc_addrs"]),
            _json_list(record["bcc_addrs"]),
        )
        rows.append((
            user_id,
            draft_source.SOURCE,
            draft_source.dedup_key(record["id"]),
            draft_source.OBJECT_TYPE,
            str(record["id"]),
            draft_source.SEVERITY,
            draft_source.title_for(to_addrs[0] if to_addrs else ""),
            draft_source.delivery_body_for(
                record["subject"], record["id"], recipients,
            ),
            record["room_token"],
            _iso_z_from_sql_datetime(record["created_at"]),
            now,
        ))
    return rows


def _notification_backfill_write(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Insert the rows and stamp the marker, in one transaction of our own."""
    # See the docstring: commit the inherited transaction, then take our own.
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO notifications "
            "(user_id, source, dedup_key, object_type, object_id, severity, "
            " actionable, title, body, room_token, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            rows,
        )
        # `OR IGNORE` is here for `UNIQUE (user_id, source, dedup_key)`, but it
        # swallows NOT NULL and CHECK too — and the marker below lands in the
        # same transaction, so a row dropped for any other reason is stranded
        # for good with nothing said. Every skip is a duplicate today; counting
        # them is what makes the day one is not into something greppable rather
        # than an item silently missing from somebody's bell.
        #
        # `rowcount` is -1 when the driver will not say (an empty `executemany`
        # among others), which is "unknown", not "none written" — so treat it as
        # no information rather than letting it report a negative skip and a
        # negative seed.
        inserted = cursor.rowcount
        skipped = len(rows) - inserted if inserted >= 0 else 0
        if skipped > 0:
            logger.warning(
                "notification backfill skipped %d of %d row(s); expected only "
                "rows a producer had already written", skipped, len(rows),
            )
        conn.execute(
            "INSERT OR IGNORE INTO _migration_state (name) VALUES (?)",
            (_NOTIFICATIONS_BACKFILL_MARKER,),
        )
        conn.execute("COMMIT")
        # Rows written, not rows offered. `OR IGNORE` means the two differ on
        # the path `test_a_second_run_before_the_marker_lands_is_still_one_row`
        # drives, where every row is a duplicate and none is inserted — and a
        # log line claiming to have seeded them is one an operator would read as
        # proof the queue came across.
        written = inserted if inserted >= 0 else len(rows)
        if written > 0:
            logger.info("notification backfill seeded %d held item(s)", written)
    except sqlite3.Error as e:
        # `sqlite3.Error`, not OperationalError alone: an IntegrityError out of
        # the insert would otherwise escape and abort `init_db` outright.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        logger.warning(
            "notification backfill failed and was rolled back; the held queue "
            "is unchanged and the backfill will be retried: %s", e,
        )


def _json_list(value) -> list[str]:
    """A stored JSON array of addresses, or an empty list."""
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [a for a in parsed if isinstance(a, str)] if isinstance(parsed, list) else []


def list_tasks(
    conn: sqlite3.Connection,
    status: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
) -> list[Task]:
    """List tasks with optional filters."""
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def is_email_processed(
    conn: sqlite3.Connection, email_id: str, uidvalidity: int = 0,
) -> bool:
    """Check if an email has already been processed.

    Scoped to the UID's namespace: the same UID under a different UIDVALIDITY
    is a different message, not a duplicate (ISSUE-250).
    """
    cursor = conn.execute(
        "SELECT 1 FROM processed_emails WHERE uidvalidity = ? AND email_id = ?",
        (uidvalidity, email_id),
    )
    return cursor.fetchone() is not None


def get_email_poll_cursor(
    conn: sqlite3.Connection, folder: str,
) -> tuple[int, int] | None:
    """`(uidvalidity, last_uid)` for a polled folder, or None if never polled."""
    row = conn.execute(
        "SELECT uidvalidity, last_uid FROM email_poll_state WHERE folder = ?",
        (folder,),
    ).fetchone()
    if row is None:
        return None
    return (row["uidvalidity"], row["last_uid"])


def set_email_poll_cursor(
    conn: sqlite3.Connection, folder: str, uidvalidity: int, last_uid: int,
) -> None:
    """Record how far the inbound poll has walked this folder."""
    conn.execute(
        """
        INSERT INTO email_poll_state (folder, uidvalidity, last_uid, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(folder) DO UPDATE SET
            uidvalidity = excluded.uidvalidity,
            last_uid = excluded.last_uid,
            updated_at = excluded.updated_at
        """,
        (folder, uidvalidity, last_uid),
    )


def highest_processed_uid(
    conn: sqlite3.Connection, uidvalidity: int,
) -> int | None:
    """Highest UID already in the ledger for a namespace, or None if empty.

    `CAST` because `email_id` is TEXT — a lexicographic MAX puts "9" above
    "10". Rows whose id is not a number sort to 0 and cannot win.
    """
    row = conn.execute(
        "SELECT MAX(CAST(email_id AS INTEGER)) AS top FROM processed_emails "
        "WHERE uidvalidity = ?",
        (uidvalidity,),
    ).fetchone()
    if row is None or row["top"] is None:
        return None
    return int(row["top"])


def adopt_legacy_email_namespace(
    conn: sqlite3.Connection, uidvalidity: int,
) -> int:
    """Move pre-ISSUE-250 ledger rows into the namespace they were written in.

    Rows that predate the UIDVALIDITY column carry 0, the "not reported"
    namespace. Left there, the first poll after the upgrade would find no
    match for any real UID and re-ingest every message still in the mailbox as
    a new task — a task storm on deploy, which is the opposite of the fix.

    They were written against this same server, so the validity now observed is
    the one they belong to. Claiming that is a one-time act, done only on the
    first poll of a folder (no cursor row yet), which is also why it cannot
    swallow a genuine mailbox recreation: after this runs, a later validity
    change finds the rows namespaced and correctly treats the new UIDs as new
    mail. Returns the number of rows adopted.
    """
    if not uidvalidity:
        return 0
    cursor = conn.execute(
        "UPDATE processed_emails SET uidvalidity = ? WHERE uidvalidity = 0",
        (uidvalidity,),
    )
    return cursor.rowcount or 0


def mark_email_processed(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    references: str | None = None,
    user_id: str | None = None,
    task_id: int | None = None,
    routing_method: str | None = None,
    uidvalidity: int = 0,
) -> int:
    """Record a processed email, keyed by (uidvalidity, email_id)."""
    cursor = conn.execute(
        """
        INSERT INTO processed_emails (uidvalidity, email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id, routing_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (uidvalidity, email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id, routing_method),
    )
    return cursor.fetchone()[0]


def get_email_for_task(conn: sqlite3.Connection, task_id: int) -> ProcessedEmail | None:
    """Get the original email info for a task."""
    cursor = conn.execute(
        """
        SELECT id, uidvalidity, email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id, processed_at, routing_method
        FROM processed_emails
        WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return ProcessedEmail(
        id=row["id"],
        email_id=row["email_id"],
        sender_email=row["sender_email"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        references=row["references"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        processed_at=row["processed_at"],
        routing_method=row["routing_method"],
        uidvalidity=row["uidvalidity"],
    )


# ============================================================================
# Sent email tracking (outbound emails for emissary thread matching)
# ============================================================================


def record_sent_email(
    conn: sqlite3.Connection,
    user_id: str,
    message_id: str,
    to_addr: str,
    subject: str | None = None,
    task_id: int | None = None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    conversation_token: str | None = None,
    talk_delivery_token: str | None = None,
    origin_target: str | None = None,
) -> int:
    """Record an outbound email for thread matching."""
    cursor = conn.execute(
        """
        INSERT INTO sent_emails
            (user_id, task_id, message_id, to_addr, subject, thread_id,
             in_reply_to, "references", conversation_token, talk_delivery_token,
             origin_target)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (user_id, task_id, message_id, to_addr, subject, thread_id,
         in_reply_to, references, conversation_token, talk_delivery_token,
         origin_target),
    )
    return cursor.fetchone()[0]


def find_sent_email_by_message_id(
    conn: sqlite3.Connection,
    message_id: str,
) -> SentEmail | None:
    """Look up a sent email by its Message-ID (for In-Reply-To matching)."""
    cursor = conn.execute(
        """
        SELECT id, user_id, task_id, message_id, to_addr, subject, thread_id,
               in_reply_to, "references", conversation_token, sent_at,
               talk_delivery_token, origin_target
        FROM sent_emails
        WHERE message_id = ?
        """,
        (message_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return SentEmail(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        message_id=row["message_id"],
        to_addr=row["to_addr"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        in_reply_to=row["in_reply_to"],
        references=row["references"],
        conversation_token=row["conversation_token"],
        sent_at=row["sent_at"],
        talk_delivery_token=row["talk_delivery_token"] if "talk_delivery_token" in row.keys() else None,
        origin_target=row["origin_target"] if "origin_target" in row.keys() else None,
    )


def list_sent_message_ids(
    conn: sqlite3.Connection,
    user_id: str,
    limit: int = 50,
) -> list[str]:
    """Message-IDs this user's outbound mail went out under, most recent first.

    Feeds the thread arm of the read-side ``--scope mine`` prefilter: a reply
    carrying one of these in References / In-Reply-To belongs to ``user_id``
    even though it has no plus tag and an external sender, which is the one
    ownership route with no server-side IMAP form of its own.

    ``limit`` is the only bound, and it is the real one — every id returned
    becomes IMAP search terms, so the count is what keeps the SEARCH command a
    sane length. There is deliberately no date window on top of it: a date cut
    can only ever remove ids the limit would otherwise have kept, so it narrows
    coverage without buying any further bound. Most-recent-first is the right
    bias when the limit bites, since a reply is far likelier to quote a recent
    send than an old one.

    Grouped by ``message_id`` because the cap should mean N distinct threads
    rather than N rows: nothing constrains the column to be unique
    (``idx_sent_emails_message_id`` is a plain index), so a re-recorded id would
    otherwise spend the budget twice.
    """
    rows = conn.execute(
        """
        SELECT message_id, MAX(sent_at) AS last_sent
        FROM sent_emails
        WHERE user_id = ? AND message_id IS NOT NULL AND message_id != ''
        GROUP BY message_id
        ORDER BY last_sent DESC, message_id DESC
        LIMIT ?
        """,
        (user_id, int(limit)),
    )
    return [row["message_id"] for row in rows]


def find_sent_email_by_references(
    conn: sqlite3.Connection,
    references: list[str],
) -> SentEmail | None:
    """Find a sent email matching any of the given Message-IDs.

    Used to match inbound emails whose References header contains one of our
    sent Message-IDs. Returns the most recent match.
    """
    if not references:
        return None
    placeholders = ", ".join("?" for _ in references)
    cursor = conn.execute(
        f"""
        SELECT id, user_id, task_id, message_id, to_addr, subject, thread_id,
               in_reply_to, "references", conversation_token, sent_at,
               talk_delivery_token, origin_target
        FROM sent_emails
        WHERE message_id IN ({placeholders})
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        references,
    )
    row = cursor.fetchone()
    if not row:
        return None
    return SentEmail(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        message_id=row["message_id"],
        to_addr=row["to_addr"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        in_reply_to=row["in_reply_to"],
        references=row["references"],
        conversation_token=row["conversation_token"],
        sent_at=row["sent_at"],
        talk_delivery_token=row["talk_delivery_token"] if "talk_delivery_token" in row.keys() else None,
        origin_target=row["origin_target"] if "origin_target" in row.keys() else None,
    )


# ============================================================================
# Google OAuth token functions
# ============================================================================


def get_google_token(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """Get Google OAuth tokens for a user.

    access_token and refresh_token are Fernet-decrypted via $ISTOTA_SECRET_KEY.
    Returns None if the row is missing, the secret key is unavailable, or the
    stored ciphertext fails to decrypt (treated as a corrupt/rotated-key row;
    the user has to re-connect Google).
    """
    cursor = conn.execute(
        "SELECT access_token, refresh_token, token_expiry, scopes FROM google_oauth_tokens WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    from istota import secrets_store

    if not secrets_store.secret_key_available():
        logger.warning(
            "google_oauth: cannot decrypt tokens for user=%s (ISTOTA_SECRET_KEY missing)",
            user_id,
        )
        return None

    try:
        fernet = secrets_store._get_fernet()
        access_token = fernet.decrypt(_as_bytes(row["access_token"])).decode("utf-8")
        refresh_token = fernet.decrypt(_as_bytes(row["refresh_token"])).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "google_oauth: decrypt failed user=%s (stale ISTOTA_SECRET_KEY?): %s",
            user_id, exc,
        )
        return None

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": row["token_expiry"],
        "scopes": row["scopes"],
    }


def upsert_google_token(
    conn: sqlite3.Connection,
    user_id: str,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    scopes: str = "[]",
) -> None:
    """Insert or update Google OAuth tokens for a user.

    access_token and refresh_token are Fernet-encrypted at rest via
    $ISTOTA_SECRET_KEY. Raises if the key is unavailable -- writing plaintext
    is exactly what this table no longer tolerates.
    """
    from istota import secrets_store

    fernet = secrets_store._get_fernet()
    access_ct = fernet.encrypt(access_token.encode("utf-8"))
    refresh_ct = fernet.encrypt(refresh_token.encode("utf-8"))

    conn.execute(
        """INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry, scopes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            token_expiry = excluded.token_expiry,
            scopes = excluded.scopes,
            updated_at = datetime('now')""",
        (user_id, access_ct, refresh_ct, token_expiry, scopes),
    )
    conn.commit()


def _as_bytes(value) -> bytes:
    """Coerce an SQLite cell to bytes for Fernet decrypt.

    Cells may come back as bytes (BLOB) or str (TEXT, on legacy schemas where
    plaintext UTF-8 was stored as text). Fernet.decrypt accepts both forms
    when given bytes, so we normalise here.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"unexpected token cell type: {type(value).__name__}")


def _migrate_google_oauth_encryption(conn: sqlite3.Connection) -> int:
    """Encrypt any plaintext rows in google_oauth_tokens.

    Detection is decrypt-or-fail: a Fernet token validates an HMAC, so a real
    plaintext value reliably raises and gets re-encrypted. Idempotent --
    rows that already decrypt are left alone. No-ops without
    $ISTOTA_SECRET_KEY (logged once, leaves rows as-is for a later boot).

    Returns the number of rows re-encrypted.
    """
    try:
        rows = conn.execute(
            "SELECT user_id, access_token, refresh_token FROM google_oauth_tokens"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0

    from istota import secrets_store

    if not secrets_store.secret_key_available():
        logger.info(
            "google_oauth: %d row(s) present but ISTOTA_SECRET_KEY unset -- "
            "skipping plaintext-to-Fernet migration", len(rows),
        )
        return 0

    fernet = secrets_store._get_fernet()
    migrated = 0
    for user_id, at, rt in rows:
        at_b, rt_b = _as_bytes(at), _as_bytes(rt)
        try:
            fernet.decrypt(at_b)
            fernet.decrypt(rt_b)
            continue  # already encrypted
        except Exception:
            pass

        try:
            at_ct = fernet.encrypt(at_b)
            rt_ct = fernet.encrypt(rt_b)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "google_oauth: re-encrypt failed user=%s: %s", user_id, exc,
            )
            continue
        conn.execute(
            "UPDATE google_oauth_tokens SET access_token = ?, refresh_token = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (at_ct, rt_ct, user_id),
        )
        migrated += 1

    if migrated:
        conn.commit()
        logger.info("google_oauth: encrypted %d plaintext row(s) at rest", migrated)
    return migrated


def delete_google_token(conn: sqlite3.Connection, user_id: str) -> bool:
    """Delete Google OAuth tokens for a user. Returns True if a row was deleted."""
    cursor = conn.execute(
        "DELETE FROM google_oauth_tokens WHERE user_id = ?", (user_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def has_google_token(conn: sqlite3.Connection, user_id: str) -> bool:
    """Decryption-free existence check for the UI's "connected" badge.

    Distinct from ``get_google_token`` returning a non-None value -- that's
    "row present AND decryptable AND key available". This one is just "row
    present", which is what the UI cares about (a stale-key row still wants
    a Disconnect button).
    """
    row = conn.execute(
        "SELECT 1 FROM google_oauth_tokens WHERE user_id = ? LIMIT 1", (user_id,),
    ).fetchone()
    return row is not None


def get_google_scopes(conn: sqlite3.Connection, user_id: str) -> list[str] | None:
    """The scopes Google actually granted, without decrypting the tokens.

    Sibling of ``has_google_token``: the settings card needs to say *what*
    was granted, and ``get_google_token`` is the wrong read for that — it
    decrypts two tokens the display never uses, and returns None on a rotated
    ``ISTOTA_SECRET_KEY``, which would blank the scope list on a row that is
    still very much present.

    Returns None when there is no row (never connected), and ``[]`` when the
    row's column is empty or unparseable — a row is a row, so the caller
    still renders Connected rather than a failure.
    """
    row = conn.execute(
        "SELECT scopes FROM google_oauth_tokens WHERE user_id = ?", (user_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["scopes"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("google_oauth: unparseable scopes column for user=%s", user_id)
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "google_oauth: scopes column for user=%s is %s, not a list",
            user_id, type(parsed).__name__,
        )
        return []
    return [str(s) for s in parsed]


# ============================================================================
# Talk message tracking functions
# ============================================================================


def update_task_pid(conn: sqlite3.Connection, task_id: int, pid: int) -> None:
    """Store the subprocess PID for a running task."""
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
    conn.commit()


def get_running_task_pids(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """``(task_id, worker_pid)`` for every running task with a pid recorded.

    Read by the host-pressure snapshot, which uses the pids to reach into each
    task's bwrap mount namespace and attribute the tmpfs the daemon's own mount
    table cannot see (ISSUE-286).

    ``worker_pid`` is cleared on every transition out of ``running``, so a pid
    on a ``running`` row is a live attempt rather than a stale one from a
    finished task. It can still exit between this read and the ``/proc`` read a
    moment later; the caller renders that as an unavailable row.

    **Every running task is returned, including one with no pid**, which is
    reported as ``0``. Filtering those out would have printed ``sandbox
    none-running`` on a host that was running work: ``NativeBrain`` never calls
    ``on_pid`` at all, and neither does ``ClaudeCodeBrain``'s non-streaming
    path, so on those deployments the column is legitimately NULL while a task
    is running. The caller renders a ``0`` as "no worker pid recorded" — which
    is the true answer, and unlike an omission it does not read as an
    idle host.
    """
    cursor = conn.execute(
        "SELECT id, worker_pid FROM tasks WHERE status = 'running'"
    )
    return [(int(row["id"]), int(row["worker_pid"] or 0)) for row in cursor.fetchall()]


def get_users_with_live_tasks(conn: sqlite3.Connection) -> set[str]:
    """User ids whose work is in the hands of a worker right now.

    ``locked`` as well as ``running``: a locked row has been claimed and its
    worker is setting the task up, which is exactly when a package cache is
    about to be written and before anything has been.

    ``pending_confirmation`` is deliberately outside the set. A parked task has
    no live process — it resumes as a new one later — so its cache is not in
    use, and counting it would hold a user's cache back for the two hours a
    confirmation can sit unanswered.

    The caller is the package-cache sweep (ISSUE-317), which uses this as the
    guard that stops it wiping a cache a ``uv sync`` is reading. That is why it
    returns a set rather than rows: the question is membership, and a user with
    no id (there is none today) would be a hole in a guard rather than a blank
    row.
    """
    cursor = conn.execute(
        "SELECT DISTINCT user_id FROM tasks WHERE status IN ('locked', 'running')"
    )
    return {row["user_id"] for row in cursor.fetchall() if row["user_id"]}


def set_task_model_used(conn: sqlite3.Connection, task_id: int, model: str) -> None:
    """Record the model that actually ran a task (resolved canonical ID).

    Writes the dedicated ``model_used`` column, leaving ``model`` (the per-task
    override; empty = config default) untouched so a retry of a default-model
    task still re-resolves the current default rather than pinning attempt 1's
    model. Surfaces (web-chat meta) read ``model_used``.
    """
    conn.execute("UPDATE tasks SET model_used = ? WHERE id = ?", (model, task_id))
    conn.commit()


def touch_task_heartbeat(conn: sqlite3.Connection, task_id: int) -> None:
    """Record a liveness ping from the worker executing ``task_id``.

    A running worker calls this periodically. Stuck-task reclaim uses the
    heartbeat to tell a slow-but-alive worker from a dead one — see claim_task()
    (ISSUE-112). Scoped to status='running' so a ping that races task completion
    can't resurrect the heartbeat on a finished task.
    """
    conn.execute(
        "UPDATE tasks SET last_heartbeat = datetime('now') "
        "WHERE id = ? AND status = 'running'",
        (task_id,),
    )
    conn.commit()


def is_task_cancelled(conn: sqlite3.Connection, task_id: int) -> bool:
    """Check if a task has been flagged for cancellation."""
    row = conn.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row and row[0])


def update_talk_response_id(
    conn: sqlite3.Connection,
    task_id: int,
    talk_response_id: int,
) -> None:
    """Store the Talk message ID of bot's response for a task."""
    conn.execute(
        "UPDATE tasks SET talk_response_id = ?, updated_at = datetime('now') WHERE id = ?",
        (talk_response_id, task_id),
    )


def get_reply_parent_task(
    conn: sqlite3.Connection,
    conversation_token: str,
    reply_to_talk_id: int,
) -> Task | None:
    """
    Find the task whose Talk message matches the replied-to ID.

    Checks both talk_message_id (user's message) and talk_response_id (bot's response)
    to find the conversation exchange being replied to.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND (talk_message_id = ? OR talk_response_id = ?)
        AND status = 'completed'
        AND result IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_token, reply_to_talk_id, reply_to_talk_id),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_task(row)


def get_reply_parent_task_by_message_id(
    conn: sqlite3.Connection,
    conversation_token: str,
    reply_to_message_id: int,
) -> Task | None:
    """The completed task behind a cited canonical message, or None.

    The canonical-namespace sibling of `get_reply_parent_task`, resolving
    `messages.id` → `messages.task_id` → `tasks`. Deliberately a separate
    lookup: the Talk-native one matches on `talk_message_id`/`talk_response_id`,
    and in a Talk-bound room those ids collide numerically with canonical ones,
    so routing a canonical citation through it would silently surface an
    unrelated turn.

    Returns None for the three cases that correctly fall through to the stored
    snapshot alone: a `role='system'` row (no task), a turn whose task retention
    deleted, and a turn that failed, was cancelled, or is still running.
    """
    row = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE id = (
            SELECT task_id FROM messages
            WHERE id = ? AND room_token = ?
        )
        AND conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
        """,
        (reply_to_message_id, conversation_token, conversation_token),
    ).fetchone()
    return _row_to_task(row) if row else None


def save_task_selected_skills(
    conn: sqlite3.Connection,
    task_id: int,
    selected_skills: list[str],
) -> None:
    """Store the skills selected for a task (called right after skill selection)."""
    conn.execute(
        "UPDATE tasks SET selected_skills = ? WHERE id = ?",
        (json.dumps(selected_skills), task_id),
    )


def get_recent_conversation_skills(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    max_age_minutes: int = 30,
    limit: int = 2,
) -> set[str]:
    """Get skill names from recent completed tasks in the same conversation.

    Returns a union of skills from the last N tasks within the time window.
    Used for skill stickiness in follow-up messages.

    Excludes ``withheld_from_room`` (ISSUE-255) — the weakest of that column's
    readers and swept for consistency rather than for cost: it carries skill
    names, not content, so leaving it would leak nothing of the exchange. It is
    still the same class as the rest, and an unswept reader keyed on this column
    invites the next person to assume the sweep was exhaustive.
    """
    query = """
        SELECT selected_skills
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND selected_skills IS NOT NULL
        AND created_at > datetime('now', ?)
        AND COALESCE(withheld_from_room, 0) = 0
    """
    params: list = [conversation_token, f"-{max_age_minutes} minutes"]

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    skills: set[str] = set()
    for row in rows:
        try:
            skills.update(json.loads(row["selected_skills"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return skills


# ============================================================================
# Cleanup functions for scheduler robustness
# ============================================================================


def expire_stale_confirmations(conn: sqlite3.Connection, timeout_minutes: int) -> list[dict]:
    """
    Cancel tasks that have been pending_confirmation longer than timeout.
    Returns list of cancelled task info for notification.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            error = 'Confirmation request timed out',
            updated_at = datetime('now')
        WHERE status = 'pending_confirmation'
        AND updated_at < datetime('now', '-' || ? || ' minutes')
        RETURNING id, user_id, conversation_token, prompt, source_type
        """,
        (timeout_minutes,),
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "conversation_token": row["conversation_token"],
            "prompt": row["prompt"][:100] if row["prompt"] else None,
            "source_type": row["source_type"],
        }
        for row in cursor.fetchall()
    ]


def get_stale_pending_tasks(conn: sqlite3.Connection, warn_minutes: int) -> list[Task]:
    """
    Get tasks that have been pending longer than threshold for logging.
    Excludes tasks that are scheduled for the future.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE status = 'pending'
        AND created_at < datetime('now', '-' || ? || ' minutes')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """,
        (warn_minutes,),
    )
    return [_row_to_task(row) for row in cursor.fetchall()]


def fail_ancient_pending_tasks(conn: sqlite3.Connection, fail_hours: int) -> list[dict]:
    """
    Auto-fail tasks that have been pending too long.
    Returns list of failed task info for notification.
    Excludes tasks that are scheduled for the future.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = 'Task timed out - pending too long without being processed',
            completed_at = datetime('now'),
            updated_at = datetime('now')
        WHERE status = 'pending'
        AND created_at < datetime('now', '-' || ? || ' hours')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        RETURNING id, user_id, conversation_token, source_type, prompt
        """,
        (fail_hours,),
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "conversation_token": row["conversation_token"],
            "source_type": row["source_type"],
            "prompt": row["prompt"][:100] if row["prompt"] else None,
        }
        for row in cursor.fetchall()
    ]


def fail_stuck_locked_running_tasks(
    conn: sqlite3.Connection, max_retry_age_minutes: int = 60,
    stuck_running_minutes: int = 15,
    heartbeat_stuck_minutes: int = 5,
) -> list[dict]:
    """Fail or release tasks stuck in 'locked' or 'running' state.

    This mirrors the recovery logic in claim_task() but runs independently
    so stuck tasks are cleaned up even when no new tasks are being claimed.
    See claim_task() for ``stuck_running_minutes`` / ``heartbeat_stuck_minutes``
    (ISSUE-112).

    Returns list of failed task info for logging.
    """
    failed = []

    # Fail old stale locks (created too long ago to be worth retrying)
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stale lock)',
            locked_at = NULL, locked_by = NULL,
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at < datetime('now', ? || ' minutes')
        RETURNING id, user_id, conversation_token, source_type
        """,
        (f"-{max_retry_age_minutes}",),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    # Release recent stale locks (younger tasks get retried)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at >= datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Fail old stuck 'running' tasks
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stuck running)',
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at < datetime('now', ? || ' minutes')
        RETURNING id, user_id, conversation_token, source_type
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    # Release recent stuck 'running' tasks for retry. Clear last_heartbeat too:
    # leaving the dead worker's stale heartbeat on the row would keep the
    # _STUCK_RUNNING_PREDICATE firing after the next worker re-claims and re-runs
    # it, letting a second concurrent claimer re-steal it (duplicate execution).
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            last_heartbeat = NULL, attempt_count = attempt_count + 1
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at >= datetime('now', ? || ' minutes')
        AND attempt_count < max_attempts
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Fail stuck 'running' tasks that have exhausted retries
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed',
            error = 'Task stuck in running state - worker may have crashed',
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND attempt_count >= max_attempts
        RETURNING id, user_id, conversation_token, source_type
        """,
        _stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    return failed


def recover_orphaned_tasks(
    conn: sqlite3.Connection, max_retry_age_minutes: int = 60,
) -> list[dict]:
    """Reclaim tasks left mid-execution by a dead prior daemon instance.

    Called once at daemon startup, under the singleton flock and before any
    worker spawns, so every ``running``/``locked`` row is definitionally an
    orphan — no live worker owns it. Unlike ``fail_stuck_locked_running_tasks``
    there is no time-based liveness guess: a fresh daemon knows the previous
    one is gone, so recovery is immediate instead of waiting out
    ``worker_stuck_minutes``. ``pending_confirmation`` is left alone (it's
    legitimately awaiting the user).

    Each orphan is resolved one of three ways, in priority order:

    - **cancelled** — ``cancel_requested`` was set (the user asked to cancel
      during the orphan window). Resolve straight to ``cancelled`` rather than
      re-running the whole task just to cancel on its first event.
    - **failed** — retries exhausted, too old to retry, or an inline-only
      source type (REPL runs in a separate process the daemon never claims, so
      releasing it would strand it ``pending`` forever).
    - **released** — otherwise: back to ``pending`` with ``attempt_count``
      bumped and every liveness column cleared, for a fresh attempt by the next
      worker.

    Returns one dict per recovered task — ``id``, ``user_id``,
    ``conversation_token``, ``source_type``, ``action`` (cancelled/failed/
    released) — so the caller can emit terminal event frames for the non-rerun
    cases. Ordering matches the branch priority above; each UPDATE filters on
    ``status IN ('running','locked')`` so a row resolved by an earlier branch
    is excluded from the later ones.
    """
    recovered: list[dict] = []

    # 1. User asked to cancel — honor it without a re-run.
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled', error = 'Cancelled by user',
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked') AND cancel_requested = 1
        RETURNING id, user_id, conversation_token, source_type
        """
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "cancelled"})

    # 2. Not worth retrying: out of attempts, too old, or inline-only source.
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', completed_at = datetime('now'),
            error = 'Worker died mid-task (scheduler restart); not retried',
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked')
        AND (
            attempt_count >= max_attempts
            OR created_at < datetime('now', ? || ' minutes')
            OR source_type IN ({_INLINE_ONLY_IN})
        )
        RETURNING id, user_id, conversation_token, source_type
        """,
        (f"-{max_retry_age_minutes}",),
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "failed"})

    # 3. Retry-eligible: requeue with liveness cleared so the stuck predicate
    # can't re-fire and a second claimer can't re-steal it.
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', attempt_count = attempt_count + 1,
            last_heartbeat = NULL, started_at = NULL,
            locked_at = NULL, locked_by = NULL, worker_pid = NULL,
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked')
        RETURNING id, user_id, conversation_token, source_type
        """
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "released"})

    return recovered


def cleanup_old_processed_emails(
    conn: sqlite3.Connection, retention_days: int,
) -> int:
    """Prune the ``processed_emails`` dedup ledger (ISSUE-231).

    One row is written per *polled message*, not per task — bot self-mail,
    unroutable mail and quiet-sender mail all get one and produce nothing else
    — so an internet-facing address grows the table forever. Nothing else
    deletes from it: ``cleanup_old_tasks`` touches only tasks and their logs.

    Keyed on ``processed_at``, not on whether the row's ``task`` still exists.
    The FK is unenforced (``PRAGMA foreign_keys`` is never set) and tasks are
    pruned at a far shorter window, so after a week most rows hold a dangling
    ``task_id``. Dedup is not the only job left, though — see below.

    **A row still referenced by the canonical transcript is never pruned.**
    ``EMAIL_SENDER_SUBQUERY`` (`:1855`) recovers an email turn's envelope
    sender from here, by ``task_id``, and ``messages`` is *not* age-pruned —
    the transcript deliberately outlives the ``tasks`` row it came from. Delete
    the ledger row and ``external_sender`` goes NULL, so ``_speaker_label``
    falls back to the task's ``user_id`` and an external contact's mail is
    rendered into the prompt as the principal's own words. That is exactly the
    misattribution ISSUE-226 exists to prevent, and the sleep cycle writes it
    durably to ``USER.md`` and the knowledge graph. The exclusion costs almost
    nothing against the growth this prune is for: the rows that actually pile
    up are the ones that produced no task at all (bot self-mail, ``discarded``,
    quiet senders), and those have no transcript row to protect them.

    ``retention_days <= 0`` disables the prune.
    """
    if retention_days <= 0:
        return 0

    cursor = conn.execute(
        """
        DELETE FROM processed_emails
        WHERE processed_at < datetime('now', '-' || ? || ' days')
        AND (
            task_id IS NULL
            OR NOT EXISTS (SELECT 1 FROM messages WHERE messages.task_id = processed_emails.task_id)
        )
        """,
        (retention_days,),
    )
    return cursor.rowcount


def cleanup_old_tasks(conn: sqlite3.Connection, retention_days: int) -> int:
    """
    Delete old completed/failed/cancelled tasks and their logs.
    Returns number of tasks deleted.
    """
    # First, delete logs for tasks that will be deleted
    conn.execute(
        """
        DELETE FROM task_logs
        WHERE task_id IN (
            SELECT id FROM tasks
            WHERE status IN ('completed', 'failed', 'cancelled')
            AND completed_at < datetime('now', '-' || ? || ' days')
        )
        """,
        (retention_days,),
    )

    # ON DELETE CASCADE is a no-op without PRAGMA foreign_keys, so hand-delete
    # the event stream alongside the logs (same retention window).
    conn.execute(
        """
        DELETE FROM task_events
        WHERE task_id IN (
            SELECT id FROM tasks
            WHERE status IN ('completed', 'failed', 'cancelled')
            AND completed_at < datetime('now', '-' || ? || ' days')
        )
        """,
        (retention_days,),
    )

    # Delete the tasks themselves
    cursor = conn.execute(
        """
        DELETE FROM tasks
        WHERE status IN ('completed', 'failed', 'cancelled')
        AND completed_at < datetime('now', '-' || ? || ' days')
        """,
        (retention_days,),
    )
    return cursor.rowcount


# ============================================================================
# Trusted Email Senders
# ============================================================================


def add_trusted_sender(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Add a trusted email sender. Returns True if newly added, False if already exists."""
    try:
        conn.execute(
            "INSERT INTO trusted_email_senders (user_id, sender_email) VALUES (?, ?)",
            (user_id, sender_email.lower()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_trusted_sender(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Remove a trusted email sender. Returns True if removed, False if not found."""
    cursor = conn.execute(
        "DELETE FROM trusted_email_senders WHERE user_id = ? AND sender_email = ?",
        (user_id, sender_email.lower()),
    )
    return cursor.rowcount > 0


def list_trusted_senders(
    conn: sqlite3.Connection, user_id: str,
) -> list[dict]:
    """List all trusted email senders for a user. Returns list of {sender_email, added_at}."""
    cursor = conn.execute(
        "SELECT sender_email, added_at FROM trusted_email_senders WHERE user_id = ? ORDER BY sender_email",
        (user_id,),
    )
    return [{"sender_email": row["sender_email"], "added_at": row["added_at"]} for row in cursor]


def is_sender_trusted_in_db(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Check if an email sender is in the runtime trusted senders table."""
    cursor = conn.execute(
        "SELECT 1 FROM trusted_email_senders WHERE user_id = ? AND sender_email = ?",
        (user_id, sender_email.lower()),
    )
    return cursor.fetchone() is not None


# ============================================================================
# Key-Value Store
# ============================================================================


def kv_get(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str
) -> dict | None:
    """Get a value from the KV store. Returns dict with value and updated_at, or None."""
    cursor = conn.execute(
        "SELECT value, updated_at FROM istota_kv WHERE user_id = ? AND namespace = ? AND key = ?",
        (user_id, namespace, key),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"value": row["value"], "updated_at": row["updated_at"]}


def kv_set(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str, value: str
) -> None:
    """Set a value in the KV store. Upserts if key already exists."""
    conn.execute(
        """
        INSERT INTO istota_kv (user_id, namespace, key, value, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id, namespace, key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (user_id, namespace, key, value),
    )


def kv_delete(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str
) -> bool:
    """Delete a key from the KV store. Returns True if key existed."""
    cursor = conn.execute(
        "DELETE FROM istota_kv WHERE user_id = ? AND namespace = ? AND key = ?",
        (user_id, namespace, key),
    )
    return cursor.rowcount > 0


def kv_list(
    conn: sqlite3.Connection, user_id: str, namespace: str
) -> list[dict]:
    """List all entries in a namespace. Returns list of dicts with key, value, updated_at."""
    cursor = conn.execute(
        "SELECT key, value, updated_at FROM istota_kv WHERE user_id = ? AND namespace = ? ORDER BY key",
        (user_id, namespace),
    )
    return [
        {"key": row["key"], "value": row["value"], "updated_at": row["updated_at"]}
        for row in cursor.fetchall()
    ]


def kv_namespaces(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """List distinct namespaces for a user."""
    cursor = conn.execute(
        "SELECT DISTINCT namespace FROM istota_kv WHERE user_id = ? ORDER BY namespace",
        (user_id,),
    )
    return [row["namespace"] for row in cursor.fetchall()]


# ============================================================================
# Shared (cross-user) Key-Value Store
# ============================================================================
#
# The shared_kv table is the shared scope: no user_id in the key. These are
# pure DB operations — they do NO authorization. The gate lives at the caller
# (Config.is_shared_kv_writer for user-task writes; trusted-daemon paths write
# directly). Reads are open to any user by design.


def shared_kv_get(conn: sqlite3.Connection, namespace: str, key: str) -> dict | None:
    """Get a value from the shared KV store.

    Returns dict with value, updated_at, written_by, or None if absent.
    """
    cursor = conn.execute(
        "SELECT value, updated_at, written_by FROM shared_kv "
        "WHERE namespace = ? AND key = ?",
        (namespace, key),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "value": row["value"],
        "updated_at": row["updated_at"],
        "written_by": row["written_by"],
    }


def shared_kv_set(
    conn: sqlite3.Connection, namespace: str, key: str, value: str, written_by: str,
) -> None:
    """Set a value in the shared KV store. Upserts if the key already exists.

    ``written_by`` records the writer for audit only — it is never an
    authorization input.
    """
    conn.execute(
        """
        INSERT INTO shared_kv (namespace, key, value, written_by, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(namespace, key) DO UPDATE SET
            value = excluded.value,
            written_by = excluded.written_by,
            updated_at = excluded.updated_at
        """,
        (namespace, key, value, written_by),
    )


def shared_kv_delete(conn: sqlite3.Connection, namespace: str, key: str) -> bool:
    """Delete a key from the shared KV store. Returns True if the key existed."""
    cursor = conn.execute(
        "DELETE FROM shared_kv WHERE namespace = ? AND key = ?",
        (namespace, key),
    )
    return cursor.rowcount > 0


def shared_kv_list(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    """List all entries in a shared namespace, ordered by key."""
    cursor = conn.execute(
        "SELECT key, value, updated_at, written_by FROM shared_kv "
        "WHERE namespace = ? ORDER BY key",
        (namespace,),
    )
    return [
        {
            "key": row["key"],
            "value": row["value"],
            "updated_at": row["updated_at"],
            "written_by": row["written_by"],
        }
        for row in cursor.fetchall()
    ]


def shared_kv_namespaces(conn: sqlite3.Connection) -> list[str]:
    """List distinct namespaces in the shared KV store."""
    cursor = conn.execute(
        "SELECT DISTINCT namespace FROM shared_kv ORDER BY namespace",
    )
    return [row["namespace"] for row in cursor.fetchall()]


# ============================================================================
# Shared briefing-block cron state
# ============================================================================


def get_briefing_shared_block_last_run(
    conn: sqlite3.Connection, name: str,
) -> str | None:
    """Return the last successful generation time for a shared block, or None."""
    cursor = conn.execute(
        "SELECT last_run_at FROM briefing_shared_block_state WHERE name = ?",
        (name,),
    )
    row = cursor.fetchone()
    return row["last_run_at"] if row else None


def set_briefing_shared_block_last_run(
    conn: sqlite3.Connection, name: str, last_run_at: str,
) -> None:
    """Stamp the last generation time for a shared block. Upsert."""
    conn.execute(
        """
        INSERT INTO briefing_shared_block_state (name, last_run_at)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET last_run_at = excluded.last_run_at
        """,
        (name, last_run_at),
    )


# ============================================================================
# Shared briefing-block definitions (admin-editable; admin-shared-briefing-blocks)
# ============================================================================


def _row_to_shared_block_config(row: sqlite3.Row) -> SharedBlockConfigRow:
    try:
        sources = json.loads(row["sources"]) if row["sources"] else []
    except (json.JSONDecodeError, TypeError):
        sources = []
    if not isinstance(sources, list):
        sources = []
    return SharedBlockConfigRow(
        id=int(row["id"]),
        name=row["name"],
        cron=row["cron"],
        title=row["title"] or "",
        directive=row["directive"],
        render_mode=row["render_mode"] or "synthesis",
        enabled=bool(row["enabled"]),
        trusted=bool(row["trusted"]),
        sources=sources,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_shared_block_configs(conn: sqlite3.Connection) -> list[SharedBlockConfigRow]:
    """Return all shared-block definitions, ordered by name."""
    cursor = conn.execute(
        "SELECT * FROM shared_block_configs ORDER BY name"
    )
    return [_row_to_shared_block_config(r) for r in cursor.fetchall()]


def get_shared_block_config(
    conn: sqlite3.Connection, name: str,
) -> SharedBlockConfigRow | None:
    """Return a single shared-block definition by name, or None."""
    row = conn.execute(
        "SELECT * FROM shared_block_configs WHERE name = ?", (name,),
    ).fetchone()
    return _row_to_shared_block_config(row) if row else None


def upsert_shared_block_config(
    conn: sqlite3.Connection,
    *,
    name: str,
    cron: str,
    title: str = "",
    directive: str | None = None,
    render_mode: str = "synthesis",
    enabled: bool = True,
    trusted: bool = False,
    sources: list | None = None,
) -> SharedBlockConfigRow:
    """Create or update a shared-block definition (keyed on ``name``)."""
    sources_json = json.dumps(list(sources or []))
    conn.execute(
        """
        INSERT INTO shared_block_configs
            (name, cron, title, directive, render_mode, enabled, trusted,
             sources, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(name) DO UPDATE SET
            cron = excluded.cron,
            title = excluded.title,
            directive = excluded.directive,
            render_mode = excluded.render_mode,
            enabled = excluded.enabled,
            trusted = excluded.trusted,
            sources = excluded.sources,
            updated_at = datetime('now')
        """,
        (
            name, cron, title, directive, render_mode,
            1 if enabled else 0, 1 if trusted else 0, sources_json,
        ),
    )
    fresh = get_shared_block_config(conn, name)
    assert fresh is not None
    return fresh


def delete_shared_block_config(conn: sqlite3.Connection, name: str) -> bool:
    """Delete a shared-block definition by name. Returns True if it existed."""
    cursor = conn.execute(
        "DELETE FROM shared_block_configs WHERE name = ?", (name,),
    )
    return cursor.rowcount > 0


# ============================================================================
# Talk polling state functions
# ============================================================================


def get_talk_poll_state(conn: sqlite3.Connection, conversation_token: str) -> int | None:
    """Get the last known message ID for a conversation."""
    cursor = conn.execute(
        "SELECT last_known_message_id FROM talk_poll_state WHERE conversation_token = ?",
        (conversation_token,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_talk_poll_state(
    conn: sqlite3.Connection,
    conversation_token: str,
    message_id: int,
) -> None:
    """Advance the last known message ID for a conversation. Never rewinds it.

    **The cursor only ever moves forward**, and that is enforced here rather
    than at the call sites. It used to be an unconditional upsert, so any
    writer could move a room's cursor backwards — and the reassuring reading,
    that redelivery is idempotent because the cursor guards it, is false: the
    poll advances the cursor at the top of the results loop *before* every
    filter, and below it sit `!command` dispatch, confirmation replies with
    their ack post, and `cancel_for_conversation`. None of those is idempotent
    and only `ingest_message` is deduped, so a rewind re-runs that window —
    a command dispatched twice, an ack posted twice, a confirmation cancelled
    twice.

    There is no legitimate rewind to preserve. Talk comment ids are global and
    monotonic; neither `clear-history` nor deleting a message resets them, so a
    lower id reaching here is a stale writer rather than a correction. With a
    second inbound driver on the way (the signaling event stream), "stale
    writer" stops being hypothetical.

    An INSERT is unguarded, because a room's first cursor has nothing to be
    compared against: `_apply_room_pass` seeds it from `latest_id - 1`, which is
    lower than everything the room will hold afterwards. `updated_at` moves on
    every call, refused id or not — it records when a writer last reported on
    the room, which is how an operator tells a quiet room from a stalled poller.

    The guard does **not** make a forward jump safe, which is the other half of
    the same problem: `_apply_room_pass` still writes its seed only when the
    cursor is still absent, because `MAX` would otherwise carry a room's cursor
    *past* messages nobody has read.

    **A non-integer id is refused here rather than stored**, because with `MAX`
    in place it would not merely look wrong — it would be permanent. SQLite's
    `INTEGER` affinity converts a numeric string (`"501"` stores as `501`), but
    a non-numeric one is stored as TEXT, TEXT sorts above every integer, and no
    later integer can ever win the comparison again: the room goes deaf with
    nothing in the log to say so. Refusing is a warning and a skipped advance,
    which the next poll re-reads; raising would roll back the whole results
    batch and replay the non-idempotent window this function exists to protect.
    A `bool` is an `int` in Python and would compare as 0 or 1 against a real
    id, so it is refused with the rest — the same rule `_has_news` applies to
    the same field coming off the same payload.

    **There is deliberately no way to lower a cursor from here**, which leaves
    one legitimate case unserved: a Nextcloud restored from backup or
    re-provisioned restarts its comment id space, and the room's stored cursor
    is then permanently ahead of every message it will ever be sent. That is an
    operator event with an operator remedy (delete the room's `talk_poll_state`
    row), not something a poller should infer.
    """
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        logger.warning(
            "Refusing a non-integer Talk poll cursor for %s: %r",
            conversation_token, message_id,
        )
        return
    conn.execute(
        """
        INSERT INTO talk_poll_state (conversation_token, last_known_message_id, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(conversation_token) DO UPDATE SET
            last_known_message_id = MAX(
                excluded.last_known_message_id, last_known_message_id
            ),
            updated_at = excluded.updated_at
        """,
        (conversation_token, message_id),
    )


# ============================================================================
# TASKS.md file task functions
# ============================================================================


def is_istota_task_tracked(conn: sqlite3.Connection, user_id: str, content_hash: str) -> bool:
    """Check if a TASKS.md task has already been tracked."""
    cursor = conn.execute(
        "SELECT 1 FROM istota_file_tasks WHERE user_id = ? AND content_hash = ?",
        (user_id, content_hash),
    )
    return cursor.fetchone() is not None


def track_istota_file_task(
    conn: sqlite3.Connection,
    user_id: str,
    content_hash: str,
    original_line: str,
    normalized_content: str,
    file_path: str,
    task_id: int,
) -> int:
    """Track a new task from a TASKS.md file."""
    cursor = conn.execute(
        """
        INSERT INTO istota_file_tasks (
            user_id, content_hash, original_line, normalized_content,
            file_path, task_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        RETURNING id
        """,
        (user_id, content_hash, original_line, normalized_content, file_path, task_id),
    )
    return cursor.fetchone()[0]


def get_istota_file_task(conn: sqlite3.Connection, istota_task_id: int) -> IstotaFileTask | None:
    """Get a TASKS.md file task by its ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, content_hash, original_line, normalized_content,
               status, task_id, result_summary, error_message, attempt_count,
               max_attempts, file_path, created_at, started_at, completed_at
        FROM istota_file_tasks WHERE id = ?
        """,
        (istota_task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return IstotaFileTask(
        id=row["id"],
        user_id=row["user_id"],
        content_hash=row["content_hash"],
        original_line=row["original_line"],
        normalized_content=row["normalized_content"],
        status=row["status"],
        task_id=row["task_id"],
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        file_path=row["file_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def get_istota_file_task_by_task_id(conn: sqlite3.Connection, task_id: int) -> IstotaFileTask | None:
    """Get a TASKS.md file task by its associated task ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, content_hash, original_line, normalized_content,
               status, task_id, result_summary, error_message, attempt_count,
               max_attempts, file_path, created_at, started_at, completed_at
        FROM istota_file_tasks WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return IstotaFileTask(
        id=row["id"],
        user_id=row["user_id"],
        content_hash=row["content_hash"],
        original_line=row["original_line"],
        normalized_content=row["normalized_content"],
        status=row["status"],
        task_id=row["task_id"],
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        file_path=row["file_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def update_istota_file_task_status(
    conn: sqlite3.Connection,
    istota_task_id: int,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update the status of a TASKS.md file task."""
    if status == "in_progress":
        conn.execute(
            "UPDATE istota_file_tasks SET status = ?, started_at = datetime('now') WHERE id = ?",
            (status, istota_task_id),
        )
    elif status == "completed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = datetime('now'), result_summary = ?
            WHERE id = ?
            """,
            (status, result_summary, istota_task_id),
        )
    elif status == "failed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = datetime('now'), error_message = ?,
                attempt_count = attempt_count + 1
            WHERE id = ?
            """,
            (status, error_message, istota_task_id),
        )
    else:
        conn.execute(
            "UPDATE istota_file_tasks SET status = ? WHERE id = ?",
            (status, istota_task_id),
        )


# ============================================================================
# Scheduled job functions
# ============================================================================


def get_enabled_scheduled_jobs(conn: sqlite3.Connection) -> list[ScheduledJob]:
    """Fetch every scheduled job that may fire.

    The conjunction of the two authors' columns: the user has not switched it
    off in CRON.md, and the scheduler has not suspended it after N consecutive
    failures. Nothing arbitrates between them because nothing is stored twice.
    """
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               auto_disabled_at, disabled_at,
               once, model, effort, brain, skill, skill_args,
               publish_shared_kv, publish_shared_kv_trusted
        FROM scheduled_jobs
        WHERE enabled = 1 AND auto_disabled_at IS NULL
        """
    )
    return [_row_to_scheduled_job(row) for row in cursor.fetchall()]


def get_user_scheduled_jobs(conn: sqlite3.Connection, user_id: str) -> list[ScheduledJob]:
    """Fetch all scheduled jobs for a user (enabled and disabled)."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               auto_disabled_at, disabled_at,
               once, model, effort, brain, skill, skill_args,
               publish_shared_kv, publish_shared_kv_trusted
        FROM scheduled_jobs
        WHERE user_id = ?
        ORDER BY name
        """,
        (user_id,),
    )
    return [_row_to_scheduled_job(row) for row in cursor.fetchall()]


def _row_to_scheduled_job(row: sqlite3.Row) -> ScheduledJob:
    """Convert a database row to a ScheduledJob object."""
    return ScheduledJob(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        cron_expression=row["cron_expression"],
        prompt=row["prompt"],
        conversation_token=row["conversation_token"],
        output_target=row["output_target"],
        enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"],
        created_at=row["created_at"],
        command=row["command"] if "command" in row.keys() else None,
        silent_unless_action=bool(row["silent_unless_action"]) if "silent_unless_action" in row.keys() else False,
        skip_log_channel=bool(row["skip_log_channel"]) if "skip_log_channel" in row.keys() else False,
        consecutive_failures=row["consecutive_failures"] if "consecutive_failures" in row.keys() else 0,
        last_error=row["last_error"] if "last_error" in row.keys() else None,
        last_success_at=row["last_success_at"] if "last_success_at" in row.keys() else None,
        auto_disabled_at=row["auto_disabled_at"] if "auto_disabled_at" in row.keys() else None,
        disabled_at=row["disabled_at"] if "disabled_at" in row.keys() else None,
        once=bool(row["once"]) if "once" in row.keys() else False,
        model=row["model"] if "model" in row.keys() else None,
        effort=row["effort"] if "effort" in row.keys() else None,
        brain=row["brain"] if "brain" in row.keys() else None,
        skill=row["skill"] if "skill" in row.keys() else None,
        skill_args=row["skill_args"] if "skill_args" in row.keys() else None,
        publish_shared_kv=(
            row["publish_shared_kv"] if "publish_shared_kv" in row.keys() else None
        ),
        publish_shared_kv_trusted=(
            bool(row["publish_shared_kv_trusted"])
            if "publish_shared_kv_trusted" in row.keys()
            else False
        ),
    )


def set_scheduled_job_last_run(conn: sqlite3.Connection, job_id: int) -> None:
    """Update last_run_at to now for a scheduled job.

    Truncates seconds to :00 so croniter (minute resolution) never computes
    a next-fire time within the same minute, preventing double-fires.
    """
    conn.execute(
        "UPDATE scheduled_jobs SET last_run_at = strftime('%Y-%m-%d %H:%M:00', 'now') WHERE id = ?",
        (job_id,),
    )


def increment_scheduled_job_failures(
    conn: sqlite3.Connection, job_id: int, error: str,
) -> int:
    """Increment consecutive failure count and store error. Returns new count."""
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET consecutive_failures = consecutive_failures + 1,
            last_error = ?
        WHERE id = ?
        """,
        (error[:500], job_id),
    )
    row = conn.execute(
        "SELECT consecutive_failures FROM scheduled_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return row[0] if row else 0


def reset_scheduled_job_failures(conn: sqlite3.Connection, job_id: int) -> None:
    """Reset failure tracking on success.

    Lifts a suspension too. Unreachable for a suspended job by definition — a
    suspended job does not fire, so it cannot succeed — but it is the right
    place for the module rescue and for a job re-enabled by hand that then
    works.
    """
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET consecutive_failures = 0, last_error = NULL,
            last_success_at = datetime('now'),
            auto_disabled_at = NULL
        WHERE id = ?
        """,
        (job_id,),
    )


def disable_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Switch a job off on the *user's* behalf — the `!cron disable` verb.

    Its counterpart is :func:`suspend_scheduled_job`, and the two must not be
    collapsed back into one. This writes `enabled`, which CRON.md authors and
    the sync overwrites from the file on every tick; that is correct here,
    because `!cron disable` writes the file in the same breath and the sync
    then reads back what the user asked for. The daemon's failure path must
    never come through here: its write would be reverted within the tick,
    which is the defect the column split exists to end.

    It also stamps `disabled_at`, which is not bookkeeping: `enabled = 0` says
    a job is off and does not say who said so, and on a `_module.*` row that
    is load-bearing. Those rows are in nobody's CRON.md, so the module sync's
    legacy rescue arm had nothing to read but the failure count and inferred
    the author from it — reading a user's disable of a job that had already
    failed as the daemon's, and re-enabling it on the next tick past the
    cooldown, indefinitely (ISSUE-392). The stamp is the fact that inference
    was standing in for.
    """
    conn.execute(
        "UPDATE scheduled_jobs "
        "SET enabled = 0, disabled_at = datetime('now') WHERE id = ?",
        (job_id,),
    )


def suspend_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Stop a job firing on the *scheduler's* behalf, after N failures.

    Leaves `enabled` alone: the user has not said to switch this job off, and
    saying it for them is what let the next sync tick undo the suspension. Only
    three things clear it — a successful run, `!cron enable`, and an edit to
    what the job dispatches in CRON.md.
    """
    conn.execute(
        "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now') WHERE id = ?",
        (job_id,),
    )


def get_scheduled_job(conn: sqlite3.Connection, job_id: int) -> ScheduledJob | None:
    """Look up a scheduled job by ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               auto_disabled_at, disabled_at,
               once, model, effort, brain, skill, skill_args,
               publish_shared_kv, publish_shared_kv_trusted
        FROM scheduled_jobs
        WHERE id = ?
        """,
        (job_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_scheduled_job(row)


def delete_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Delete a scheduled job from the database."""
    conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))


def enable_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Enable a scheduled job, reset failure count, and reset last_run_at to now.

    The one verb that writes both authors' columns, and the only one that
    should: a person re-enabling a job means "I want this on" and "stop holding
    it back" at once, so it clears the suspension as well.

    Resetting last_run_at prevents the scheduler from treating the re-enable as
    a catch-up opportunity and firing immediately. The next run will occur at the
    next scheduled window after the enable time.

    `disabled_at` goes with them: it records that the user's disable verb was
    used, and a running job carrying that stamp would be a lie to whoever reads
    the column next.
    """
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET enabled = 1, consecutive_failures = 0, last_error = NULL,
            last_run_at = datetime('now'), auto_disabled_at = NULL,
            disabled_at = NULL
        WHERE id = ?
        """,
        (job_id,),
    )


def get_scheduled_job_by_name(
    conn: sqlite3.Connection, user_id: str, name: str,
) -> ScheduledJob | None:
    """Look up a scheduled job by user_id and name."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               auto_disabled_at, disabled_at,
               once, model, effort, brain, skill, skill_args,
               publish_shared_kv, publish_shared_kv_trusted
        FROM scheduled_jobs
        WHERE user_id = ? AND name = ?
        """,
        (user_id, name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_scheduled_job(row)


# ============================================================================
# Worker pool isolation queries
# ============================================================================


def get_users_with_pending_interactive_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending interactive (talk/email) tasks."""
    cursor = conn.execute(
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type IN ('talk', 'email')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_background_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending background (non-interactive) tasks only."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type NOT IN ('talk', 'email', {_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


# Longest-waiting user first, for both queue scans. `dispatch` walks this list
# and breaks at the instance cap, so the order decides who gets a worker when
# there are more users with pending work than slots. It used to be a bare
# `SELECT DISTINCT` with no `ORDER BY` — arbitrary — which meant a user late in
# whatever order SQLite returned could get zero workers tick after tick while a
# user flooding the instance reliably held theirs (ISSUE-250). With the default
# caps, three users with pending work saturate the five foreground slots, so
# this is reachable at very ordinary volumes and is not an attack-only concern.
#
# Oldest-pending-first rather than round-robin: it needs no remembered offset
# (dispatch is called every ~0.5s and holds no scan state), and it ages
# naturally — a user passed over on one tick has an older oldest-task on the
# next, so they move up rather than depending on where a rotation happens to
# be. It is not strict fairness; it is the property the per-user caps were
# always meant to imply, which is that waiting eventually wins.
_PENDING_USERS_SQL = """
        SELECT user_id FROM tasks
        WHERE status = 'pending'
        AND queue = ?
        AND source_type NOT IN ({inline_only})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        GROUP BY user_id
        ORDER BY MIN(created_at) ASC, user_id ASC
        """


def get_users_with_pending_fg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Users with pending foreground tasks, longest-waiting first."""
    cursor = conn.execute(
        _PENDING_USERS_SQL.format(inline_only=_INLINE_ONLY_IN), ("foreground",),
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_bg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Users with pending background tasks, longest-waiting first."""
    cursor = conn.execute(
        _PENDING_USERS_SQL.format(inline_only=_INLINE_ONLY_IN), ("background",),
    )
    return [row[0] for row in cursor.fetchall()]


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Count all tasks currently in the ``running`` state.

    Process-wide denominator for the scheduler_stats health line — a thread
    or fd spike with zero running tasks is a leak, the same spike during
    heavy task processing is expected.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'")
    return cursor.fetchone()[0]


def count_pending_tasks_for_user_queue(
    conn: sqlite3.Connection, user_id: str, queue: str,
) -> int:
    """Count pending tasks for a specific user and queue type.

    Raw backlog: counts every ready pending row, ignoring the per-channel
    single-active gate. Use for status / observability. For spawn-or-poll
    decisions use count_claimable_tasks_for_user_queue, which excludes tasks
    claim_task would currently refuse.
    """
    cursor = conn.execute(
        """
        SELECT COUNT(*) FROM tasks
        WHERE user_id = ? AND queue = ? AND status = 'pending'
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """,
        (user_id, queue),
    )
    return cursor.fetchone()[0]


def count_claimable_tasks_for_user_queue(
    conn: sqlite3.Connection, user_id: str, queue: str,
) -> int:
    """Count pending tasks for (user, queue) that claim_task could claim *now*.

    Mirrors claim_task's claimability WHERE clause — same inline-only exclusion,
    same schedule gate, and (for the foreground queue) the same per-channel
    single-active gate via the shared _CLAIM_CHANNEL_GATE_SQL — so dispatch's
    spawn count and the idle worker's pre-check never count a task claim_task
    would refuse. Without this, a follow-up queued behind an active task in the
    same room reads as "1 pending" to dispatch (spawns a doomed worker) and to
    the idle pre-check (busy-polls claim_task every tick) for the whole lifetime
    of the blocking task.

    It does NOT replay the stale-lock / stuck-running maintenance UPDATEs
    claim_task runs first; a soon-to-be-released stuck task is simply not counted
    until released (a safe undercount, picked up on the next tick).
    """
    filters = [
        "user_id = ?",
        "queue = ?",
        "status = 'pending'",
        f"source_type NOT IN ({_INLINE_ONLY_IN})",
        "(scheduled_for IS NULL OR scheduled_for <= datetime('now'))",
    ]
    params: list = [user_id, queue]
    if queue == "foreground" or queue is None:
        filters.append(_CLAIM_CHANNEL_GATE_SQL)
    where_clause = " AND ".join(filters)
    cursor = conn.execute(
        f"SELECT COUNT(*) FROM tasks WHERE {where_clause}",
        params,
    )
    return cursor.fetchone()[0]


def count_long_running_tasks_by_user(
    conn: sqlite3.Connection, queue: str, threshold_minutes: int,
) -> dict[str, int]:
    """Per-user count of running tasks on ``queue`` older than the threshold.

    Feeds the scheduler's elapsed-time slot reclassification: a task that has
    demonstrated it is not interactive stops counting against the user's
    interactive worker cap. See `plan_foreground_slots` in scheduler.py.

    A dict rather than a per-user call because dispatch already builds its
    pending counts in one pre-lock scan, and a per-user variant would put N
    queries inside a tick that runs every ~0.5s.

    Only `running` counts. A pending task holds no worker to discount however
    long it has waited, and `started_at` is NULL until the row goes running —
    so the age predicate excludes both, which is also the specified reading of
    a NULL `started_at`: unknown age counts as short, keeping the interactive
    cap tight rather than loose.

    A non-positive `threshold_minutes` is the off switch and returns empty
    without querying; zero would otherwise make every task long the instant it
    started.
    """
    if threshold_minutes <= 0:
        return {}
    cursor = conn.execute(
        """
        SELECT user_id, COUNT(*) FROM tasks
        WHERE status = 'running'
        AND queue = ?
        AND started_at < datetime('now', ?)
        GROUP BY user_id
        """,
        (queue, f"-{int(threshold_minutes)} minutes"),
    )
    return {row[0]: row[1] for row in cursor.fetchall()}


def has_active_foreground_task_for_channel(
    conn: sqlite3.Connection, conversation_token: str,
) -> bool:
    """Check if there's an active foreground task for the given channel.

    Active means pending, locked, or running — but not if cancellation
    has been requested (the task is winding down).
    """
    cursor = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE conversation_token = ?
        AND queue = 'foreground'
        AND status IN ('pending', 'locked', 'running')
        AND cancel_requested = 0
        LIMIT 1
        """,
        (conversation_token,),
    )
    return cursor.fetchone() is not None


# ============================================================================
# Sleep cycle state functions
# ============================================================================


def get_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    user_id: str,
) -> tuple[str | None, int | None]:
    """
    Get the last sleep cycle run state for a user.

    Returns (last_run_at, last_processed_task_id).
    """
    cursor = conn.execute(
        "SELECT last_run_at, last_processed_task_id FROM sleep_cycle_state WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row["last_run_at"], row["last_processed_task_id"]


def set_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    user_id: str,
    last_task_id: int | None,
) -> None:
    """Update the sleep cycle state for a user."""
    conn.execute(
        """
        INSERT INTO sleep_cycle_state (user_id, last_run_at, last_processed_task_id)
        VALUES (?, datetime('now'), ?)
        ON CONFLICT (user_id) DO UPDATE SET
            last_run_at = datetime('now'),
            last_processed_task_id = excluded.last_processed_task_id
        """,
        (user_id, last_task_id),
    )


# ============================================================================
# Channel sleep cycle state functions
# ============================================================================


def get_channel_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> tuple[str | None, int | None]:
    """
    Get the last channel sleep cycle run state.

    Returns (last_run_at, last_processed_task_id).
    """
    cursor = conn.execute(
        "SELECT last_run_at, last_processed_task_id FROM channel_sleep_cycle_state WHERE conversation_token = ?",
        (conversation_token,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row["last_run_at"], row["last_processed_task_id"]


def set_channel_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    conversation_token: str,
    last_task_id: int | None,
) -> None:
    """Update the channel sleep cycle state."""
    conn.execute(
        """
        INSERT INTO channel_sleep_cycle_state (conversation_token, last_run_at, last_processed_task_id)
        VALUES (?, datetime('now'), ?)
        ON CONFLICT (conversation_token) DO UPDATE SET
            last_run_at = datetime('now'),
            last_processed_task_id = excluded.last_processed_task_id
        """,
        (conversation_token, last_task_id),
    )


def get_completed_channel_tasks_since(
    conn: sqlite3.Connection,
    conversation_token: str,
    since_datetime: str,
    after_task_id: int | None = None,
) -> list[Task]:
    """
    Fetch completed tasks for a conversation token since a given datetime.

    Returns list of Task objects ordered by id ascending.

    Excludes ``withheld_from_room`` (ISSUE-255): the channel sleep cycle distils
    what it collects into ``CHANNEL.md``, which is durable and reaches every
    later prompt in the room — so an exchange deliberately kept out of the room
    must not arrive there by the back door.
    """
    query = f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND completed_at >= ?
        AND COALESCE(withheld_from_room, 0) = 0
    """
    params: list = [conversation_token, since_datetime]

    if after_task_id is not None:
        query += " AND id > ?"
        params.append(after_task_id)

    query += " ORDER BY id ASC"

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def get_active_channel_tokens(
    conn: sqlite3.Connection,
    since_datetime: str,
) -> list[str]:
    """
    Get distinct conversation tokens from recent completed tasks.

    Used to auto-discover active channels for sleep cycle processing.

    Excludes ``withheld_from_room`` (ISSUE-255) to match the collector this feeds.
    A room whose only recent traffic was withheld is not an active channel, and
    discovering it anyway would run a distillation pass over the empty string
    ``gather_channel_data`` returns — once per cycle, for as long as the mail
    keeps arriving.
    """
    cursor = conn.execute(
        """
        SELECT DISTINCT conversation_token
        FROM tasks
        WHERE status = 'completed'
        AND conversation_token IS NOT NULL
        AND conversation_token != ''
        AND completed_at >= ?
        AND COALESCE(withheld_from_room, 0) = 0
        ORDER BY conversation_token
        """,
        (since_datetime,),
    )
    return [row[0] for row in cursor.fetchall()]


def get_completed_tasks_since(
    conn: sqlite3.Connection,
    user_id: str,
    since_datetime: str,
    after_task_id: int | None = None,
) -> list[Task]:
    """
    Fetch completed tasks for a user since a given datetime.

    Args:
        since_datetime: ISO format datetime string (UTC)
        after_task_id: Only return tasks with id > this value (to avoid reprocessing)

    Returns list of Task objects ordered by id ascending.
    """
    query = f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE user_id = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND completed_at >= ?
    """
    params: list = [user_id, since_datetime]

    if after_task_id is not None:
        query += " AND id > ?"
        params.append(after_task_id)

    query += " ORDER BY id ASC"

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def list_istota_file_tasks(
    conn: sqlite3.Connection,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[IstotaFileTask]:
    """List TASKS.md file tasks with optional filters."""
    query = "SELECT * FROM istota_file_tasks WHERE 1=1"
    params: list = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    return [
        IstotaFileTask(
            id=row["id"],
            user_id=row["user_id"],
            content_hash=row["content_hash"],
            original_line=row["original_line"],
            normalized_content=row["normalized_content"],
            status=row["status"],
            task_id=row["task_id"],
            result_summary=row["result_summary"],
            error_message=row["error_message"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            file_path=row["file_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
        for row in cursor.fetchall()
    ]


# ============================================================================
# Heartbeat state functions
# ============================================================================


@dataclass
class HeartbeatState:
    """State for a heartbeat check."""
    user_id: str
    check_name: str
    last_check_at: str | None
    last_alert_at: str | None
    last_healthy_at: str | None
    last_error_at: str | None
    consecutive_errors: int
    #: The failing set the last alert was about, for a check type that names
    #: one. None for every other type, and on a row written before the column
    #: existed.
    last_alert_signature: str | None = None


def get_heartbeat_state(
    conn: sqlite3.Connection,
    user_id: str,
    check_name: str,
) -> HeartbeatState | None:
    """Get the state for a heartbeat check."""
    cursor = conn.execute(
        """
        SELECT user_id, check_name, last_check_at, last_alert_at,
               last_healthy_at, last_error_at, consecutive_errors,
               last_alert_signature
        FROM heartbeat_state
        WHERE user_id = ? AND check_name = ?
        """,
        (user_id, check_name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return HeartbeatState(
        user_id=row["user_id"],
        check_name=row["check_name"],
        last_check_at=row["last_check_at"],
        last_alert_at=row["last_alert_at"],
        last_healthy_at=row["last_healthy_at"],
        last_error_at=row["last_error_at"],
        consecutive_errors=row["consecutive_errors"],
        last_alert_signature=row["last_alert_signature"],
    )


def update_heartbeat_state(
    conn: sqlite3.Connection,
    user_id: str,
    check_name: str,
    *,
    last_check_at: bool = False,
    last_alert_at: bool = False,
    last_healthy_at: bool = False,
    last_error_at: bool = False,
    reset_errors: bool = False,
    increment_errors: bool = False,
    last_alert_signature: str | None = None,
    clear_alert_signature: bool = False,
) -> None:
    """
    Update heartbeat state fields.

    Pass True for timestamp fields to set them to now.
    Pass reset_errors=True to reset consecutive_errors to 0.
    Pass increment_errors=True to increment consecutive_errors.

    ``last_alert_signature`` records what the alert just sent was *about*;
    ``clear_alert_signature`` forgets it. They are two parameters rather than
    one nullable value because ``None`` already means "leave it alone" for
    every other field here, and a recovery has to be able to say "forget it"
    without that reading as "don't touch it" — otherwise a deployment that
    broke, was fixed, and broke the same way again would never page a second
    time. Passing both is a caller error and the clear wins, since it is the
    safer of the two: it can only cause an extra alert, never a missing one.
    """
    # Ensure row exists first
    conn.execute(
        """
        INSERT INTO heartbeat_state (user_id, check_name)
        VALUES (?, ?)
        ON CONFLICT (user_id, check_name) DO NOTHING
        """,
        (user_id, check_name),
    )

    updates = []
    params: list = []
    if last_check_at:
        updates.append("last_check_at = datetime('now')")
    if last_alert_at:
        updates.append("last_alert_at = datetime('now')")
    if last_healthy_at:
        updates.append("last_healthy_at = datetime('now')")
    if last_error_at:
        updates.append("last_error_at = datetime('now')")
    if reset_errors:
        updates.append("consecutive_errors = 0")
    if increment_errors:
        updates.append("consecutive_errors = consecutive_errors + 1")
    if clear_alert_signature:
        updates.append("last_alert_signature = NULL")
    elif last_alert_signature is not None:
        updates.append("last_alert_signature = ?")
        params.append(last_alert_signature)

    if updates:
        params.extend([user_id, check_name])
        conn.execute(
            f"""
            UPDATE heartbeat_state
            SET {", ".join(updates)}
            WHERE user_id = ? AND check_name = ?
            """,
            params,
        )


# ============================================================================
# Reminder state functions (for shuffle-queue rotation)
# ============================================================================


@dataclass
class ReminderState:
    """State for reminder rotation queue."""
    user_id: str
    queue: list[int]  # Remaining reminder indices
    content_hash: str  # Hash of reminders content


def get_reminder_state(conn: sqlite3.Connection, user_id: str) -> ReminderState | None:
    """Get the reminder rotation state for a user."""
    cursor = conn.execute(
        "SELECT user_id, queue, content_hash FROM reminder_state WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return ReminderState(
        user_id=row["user_id"],
        queue=json.loads(row["queue"]),
        content_hash=row["content_hash"],
    )


def set_reminder_state(
    conn: sqlite3.Connection,
    user_id: str,
    queue: list[int],
    content_hash: str,
) -> None:
    """Set the reminder rotation state for a user."""
    conn.execute(
        """
        INSERT INTO reminder_state (user_id, queue, content_hash, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT (user_id) DO UPDATE SET
            queue = excluded.queue,
            content_hash = excluded.content_hash,
            updated_at = datetime('now')
        """,
        (user_id, json.dumps(queue), content_hash),
    )


# ============================================================================
# Skills fingerprint functions
# ============================================================================


def get_user_skills_fingerprint(conn: sqlite3.Connection, user_id: str) -> str | None:
    """Get the stored skills fingerprint for a user."""
    cursor = conn.execute(
        "SELECT fingerprint FROM user_skills_fingerprint WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_user_skills_fingerprint(conn: sqlite3.Connection, user_id: str, fingerprint: str) -> None:
    """Store or update the skills fingerprint for a user."""
    conn.execute(
        """
        INSERT INTO user_skills_fingerprint (user_id, fingerprint, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT (user_id) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            updated_at = datetime('now')
        """,
        (user_id, fingerprint),
    )


# ============================================================================


# ============================================================================
# Talk message cache functions
# ============================================================================


def upsert_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    messages: list[dict],
) -> int:
    """Bulk insert/replace Talk API messages into the cache.

    Maps raw API field names to DB columns. Returns count of rows affected.
    """
    if not messages:
        return 0

    count = 0
    for msg in messages:
        parent = msg.get("parent")
        parent_id = None
        if isinstance(parent, dict) and parent.get("id"):
            parent_id = parent["id"]

        message_params = msg.get("messageParameters")
        if message_params is not None:
            params_json = json.dumps(message_params)
        else:
            params_json = None

        conn.execute(
            """
            INSERT INTO talk_messages (
                message_id, conversation_token, actor_id, actor_display_name,
                actor_type, message_text, message_type, message_parameters,
                timestamp, reference_id, deleted, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_token, message_id) DO UPDATE SET
                actor_id = excluded.actor_id,
                actor_display_name = excluded.actor_display_name,
                actor_type = excluded.actor_type,
                message_text = excluded.message_text,
                message_type = excluded.message_type,
                message_parameters = excluded.message_parameters,
                timestamp = excluded.timestamp,
                deleted = excluded.deleted,
                parent_id = excluded.parent_id,
                reference_id = CASE
                    WHEN talk_messages.reference_id LIKE '%:result'
                    THEN talk_messages.reference_id
                    ELSE excluded.reference_id
                END
            """,
            (
                msg.get("id"),
                conversation_token,
                msg.get("actorId", ""),
                msg.get("actorDisplayName", ""),
                msg.get("actorType", "users"),
                msg.get("message", ""),
                msg.get("messageType", "comment"),
                params_json,
                msg.get("timestamp", 0),
                msg.get("referenceId"),
                1 if msg.get("deleted") else 0,
                parent_id,
            ),
        )
        count += 1
    return count


def get_cached_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    limit: int = 100,
) -> list[dict]:
    """Retrieve cached messages in oldest-first order (same format as Talk API).

    Returns dicts matching the structure that build_talk_context() expects.
    """
    cursor = conn.execute(
        """
        SELECT message_id, actor_id, actor_display_name, actor_type,
               message_text, message_type, message_parameters,
               timestamp, reference_id, deleted, parent_id
        FROM talk_messages
        WHERE conversation_token = ?
        ORDER BY message_id DESC
        LIMIT ?
        """,
        (conversation_token, limit),
    )
    rows = cursor.fetchall()

    # Reverse to oldest-first (query fetches newest-first for LIMIT)
    messages = []
    for row in reversed(rows):
        params = row["message_parameters"]
        if params is not None:
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        else:
            params = {}

        msg = {
            "id": row["message_id"],
            "actorId": row["actor_id"],
            "actorDisplayName": row["actor_display_name"],
            "actorType": row["actor_type"],
            "message": row["message_text"],
            "messageType": row["message_type"],
            "messageParameters": params,
            "timestamp": row["timestamp"],
            "referenceId": row["reference_id"],
            "deleted": bool(row["deleted"]),
        }
        if row["parent_id"] is not None:
            msg["parent"] = {"id": row["parent_id"]}
        messages.append(msg)

    return messages


def has_cached_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> bool:
    """Check if any cached messages exist for a conversation."""
    cursor = conn.execute(
        "SELECT 1 FROM talk_messages WHERE conversation_token = ? LIMIT 1",
        (conversation_token,),
    )
    return cursor.fetchone() is not None


def cleanup_old_talk_messages(
    conn: sqlite3.Connection,
    max_per_conversation: int = 200,
) -> int:
    """Trim cached talk messages to keep only the latest N per conversation.

    Uses a per-conversation cap instead of time-based retention to avoid
    deleting old-but-still-useful context messages (which would trigger
    repeated backfills).

    Returns count of rows deleted.
    """
    cursor = conn.execute(
        """
        DELETE FROM talk_messages
        WHERE rowid IN (
            SELECT rowid FROM talk_messages AS t
            WHERE (
                SELECT COUNT(*) FROM talk_messages AS t2
                WHERE t2.conversation_token = t.conversation_token
                  AND t2.message_id >= t.message_id
            ) > ?
        )
        """,
        (max_per_conversation,),
    )
    return cursor.rowcount


# ============================================================================
# Geocode cache functions
# ============================================================================


def get_cached_geocode(
    conn: sqlite3.Connection,
    location_text: str,
) -> tuple[float, float] | None:
    """Look up a cached geocode result. Returns (lat, lon) or None."""
    cursor = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE location_text = ?",
        (location_text,),
    )
    row = cursor.fetchone()
    if row:
        return (row["lat"], row["lon"])
    return None


def cache_geocode(
    conn: sqlite3.Connection,
    location_text: str,
    lat: float,
    lon: float,
) -> None:
    """Cache a geocode result."""
    conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache (location_text, lat, lon)
        VALUES (?, ?, ?)
        """,
        (location_text, lat, lon),
    )


def get_reverse_geocode(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
) -> dict | None:
    """Look up a cached reverse geocode result. Returns dict or None."""
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    cursor = conn.execute(
        """SELECT display_name, neighborhood, suburb, road, city
           FROM reverse_geocode_cache
           WHERE lat_rounded = ? AND lon_rounded = ?""",
        (lat_rounded, lon_rounded),
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def cache_reverse_geocode(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    result: dict,
) -> None:
    """Cache a reverse geocode result. Rounds to 4 decimal places (~11m)."""
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    conn.execute(
        """INSERT OR REPLACE INTO reverse_geocode_cache
           (lat_rounded, lon_rounded, display_name, neighborhood, suburb, road, city, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lat_rounded,
            lon_rounded,
            result.get("display_name"),
            result.get("neighborhood"),
            result.get("suburb"),
            result.get("road"),
            result.get("city"),
            json.dumps(result.get("raw", {})),
        ),
    )


# ---------------------------------------------------------------------------
# code_review call budget
# ---------------------------------------------------------------------------


def code_review_calls_get(conn: sqlite3.Connection, task_id: int) -> int:
    """Review rounds this task has already spent.

    Zero for a task that has never run one. The table's `ON DELETE CASCADE` is
    decorative like every other FK in this module — `PRAGMA foreign_keys` is
    never enabled on these connections — so a counter does outlive its task and
    is pruned by whatever sweeps `tasks`, not by the constraint.
    """
    row = conn.execute(
        "SELECT calls FROM code_review_calls WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["calls"]) if row else 0


def code_review_calls_increment(
    conn: sqlite3.Connection, task_id: int, count: int = 1
) -> int:
    """Count `count` review rounds against a task. Returns the new total.

    `count` is not always 1: a review whose reviewers took the `need_files`
    round trip spent two model rounds and charges both in one call. Doing it in
    one statement rather than looping is what keeps the upsert's guarantee —
    two reviews for one task cannot interleave into a single increment.

    Commits in its own transaction, like the other control-signal writes: the
    caller is a short-lived CLI process and the count must survive it.
    """
    row = conn.execute(
        """
        INSERT INTO code_review_calls (task_id, calls, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(task_id) DO UPDATE
            SET calls = calls + excluded.calls, updated_at = datetime('now')
        RETURNING calls
        """,
        (task_id, count),
    ).fetchone()
    conn.commit()
    return int(row["calls"])


# ---------------------------------------------------------------------------
# Token/cost usage (`task_usage`, `task_usage_models`)
#
# Every date comparison in this section builds its bounds in the ISO-Z format
# `task_usage.created_at` stores. Do NOT reach for the `datetime('now', '-N
# days')` idiom `cleanup_old_tasks` uses: against ISO-Z values ' ' sorts below
# 'T' and same-day comparisons invert, which loses rows without raising.
# `unmeasured_task_count` is the one function here that reads `tasks`, and it
# therefore takes the *other* format — see its docstring.
# ---------------------------------------------------------------------------

USAGE_GROUP_BY = {
    "day": "substr(u.created_at, 1, 10)",
    "user": "u.user_id",
    "source": "u.source_type",
    "brain": "u.brain_kind",
    "origin": "u.origin",
    # "model" is handled separately: it reads the child table, because a
    # multi-model run has no single model on its parent row.
}


def iso_utc_now() -> str:
    """`task_usage.created_at`'s format, for building query bounds in Python."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def iso_utc_days_ago(days: int) -> str:
    """An ISO-Z bound `days` before now."""
    then = datetime.now(timezone.utc) - timedelta(days=days)
    return then.strftime("%Y-%m-%dT%H:%M:%S.") + f"{then.microsecond // 1000:03d}Z"


def sql_datetime_days_ago(days: int) -> str:
    """The `datetime('now')` format `tasks.created_at` stores.

    Kept beside its ISO-Z sibling on purpose: a caller that needs both (the
    admin Users section does) should be picking between two named functions
    rather than reusing one bound in the wrong place.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def insert_task_usage(
    conn: sqlite3.Connection,
    *,
    usage: Any,
    task_id: int | None = None,
    origin: str = "task",
    user_id: str = "",
    source_type: str = "",
    brain_kind: str = "",
    is_fallback: bool = False,
    model: str = "",
    effort: str = "",
    stop_reason: str = "",
    success: bool = False,
) -> int:
    """Write one usage row plus its per-model children. Returns the parent id.

    ``model`` is the model the caller knows the attempt actually ran, and it
    wins over ``usage.model`` when set. The two can differ: ``usage.model`` is
    the CLI's cost-weighted dominant model, which is not the same answer on a
    run whose out-of-band calls outweigh a cheap main turn, and it is empty
    outright for a native row, which reports one total with no per-model split.

    Two things here are load-bearing and neither is obvious.

    **`attempt_seq` is assigned in a single statement.** A `SELECT MAX(...)+1`
    followed by an `INSERT` is not safe even "inside the same transaction":
    `get_db` uses the default isolation level, so a bare SELECT opens no
    transaction and takes no write lock, and nothing in this codebase issues
    `BEGIN IMMEDIATE`. Two workers really can run one task — the
    duplicate-execution guard in the scheduler is post-hoc, running after
    `execute_task` returns. The `INSERT ... SELECT` below closes that window,
    and the whole insert is retried once on a write conflict: the losing worker
    re-reads `MAX(attempt_seq)` and takes the next one. The retry catches
    `sqlite3.Error` rather than `IntegrityError` alone because the observed
    concurrent failure is an `OperationalError` ("database is locked", or
    SQLITE_BUSY_SNAPSHOT against a pinned stale read snapshot, which the busy
    handler does not retry). A second failure is raised rather than swallowed —
    real spend disappearing deserves more than a debug line, and the caller
    logs it as a warning.

    **Parent and children land together, or not at all.** A failure at child 3
    of 5 would otherwise commit a parent whose totals do not equal the sum of
    its children, which is the exact invariant `--by model` depends on. The
    SAVEPOINT means the bare `except` in the best-effort caller swallows a
    complete failure, never a partial split.

    **On committing.** A `SAVEPOINT` statement is not DML, so pysqlite issues no
    implicit `BEGIN` for it and the savepoint itself opens the transaction. When
    this is the first write on a connection the savepoint is therefore the
    outermost one, and SQLite commits on its `RELEASE`. Called inside a
    caller's open transaction it nests properly and commits nothing. Both are
    fine for best-effort telemetry, but a caller that means to roll its own work
    back must not assume this row goes with it.
    """
    try:
        return _insert_task_usage_once(
            conn, usage=usage, task_id=task_id, origin=origin, user_id=user_id,
            source_type=source_type, brain_kind=brain_kind,
            is_fallback=is_fallback, model=model, effort=effort,
            stop_reason=stop_reason, success=success,
        )
    except sqlite3.Error:
        return _insert_task_usage_once(
            conn, usage=usage, task_id=task_id, origin=origin, user_id=user_id,
            source_type=source_type, brain_kind=brain_kind,
            is_fallback=is_fallback, model=model, effort=effort,
            stop_reason=stop_reason, success=success,
        )


def _insert_task_usage_once(
    conn: sqlite3.Connection,
    *,
    usage: Any,
    task_id: int | None,
    origin: str,
    user_id: str,
    source_type: str,
    brain_kind: str,
    is_fallback: bool,
    model: str,
    effort: str,
    stop_reason: str,
    success: bool,
) -> int:
    """One attempt at the write. See `insert_task_usage` for the reasoning."""
    savepoint = f"usage_{uuid.uuid4().hex[:12]}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        cursor = conn.execute(
            """
            INSERT INTO task_usage (
                task_id, attempt_seq, origin, user_id, source_type, brain_kind,
                is_fallback, model, effort, stop_reason, success,
                has_totals, totals_source, billed_input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, cost_usd, cost_basis,
                turns, model_requests, subagent_requests, compacted_requests,
                initial_context_tokens, peak_context_tokens, context_window,
                duration_ms, duration_api_ms, service_tier, session_id,
                rate_limit_type, rate_limit_status, rate_limit_resets_at
            )
            SELECT
                ?, COALESCE(MAX(attempt_seq), 0) + 1, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            FROM task_usage WHERE task_id IS NOT NULL AND task_id = ?
            """,
            (
                task_id, origin, user_id, source_type, brain_kind,
                1 if is_fallback else 0, model or usage.model, effort, stop_reason,
                1 if success else 0,
                1 if usage.has_totals else 0, usage.totals_source,
                usage.billed_input_tokens, usage.output_tokens,
                usage.cache_read_tokens, usage.cache_write_tokens,
                usage.cost_usd, usage.cost_basis,
                usage.turns, usage.model_requests, usage.subagent_requests,
                usage.compacted_requests,
                usage.initial_context_tokens, usage.peak_context_tokens,
                usage.context_window,
                usage.duration_ms, usage.duration_api_ms, usage.service_tier,
                usage.session_id,
                _rate_limit_field(usage, "rateLimitType"),
                _rate_limit_field(usage, "status"),
                _rate_limit_field(usage, "resetsAt", numeric=True),
                task_id,
            ),
        )
        row_id = int(cursor.lastrowid)

        for model in usage.models:
            conn.execute(
                """
                INSERT INTO task_usage_models (
                    task_usage_id, model, billed_input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd, context_window
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id, model.model, model.billed_input_tokens,
                    model.output_tokens, model.cache_read_tokens,
                    model.cache_write_tokens, model.cost_usd, model.context_window,
                ),
            )
    except Exception:
        # The recovery is itself best-effort. SQLite cancels every savepoint
        # when a statement error forces an automatic rollback (disk full, I/O
        # error) — exactly the class this guard exists for — and `ROLLBACK TO`
        # then raises "no such savepoint", replacing the real cause. The caller
        # would log a disk-full incident as a savepoint-naming problem.
        try:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        except Exception:
            pass
        raise
    conn.execute(f"RELEASE {savepoint}")
    return row_id


def _rate_limit_field(usage: Any, key: str, *, numeric: bool = False):
    """Pull one field out of the captured rate-limit posture, or None."""
    info = getattr(usage, "rate_limit", None)
    if not isinstance(info, dict):
        return None
    value = info.get(key)
    if value is None:
        return None
    if numeric:
        # `bool` is an `int` subclass, so an unguarded isinstance would store a
        # `resetsAt: true` frame as the timestamp 1. Same rule as `usage._int`.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)
    return str(value)


def _usage_filters(
    *,
    since: str | None,
    until: str | None,
    user_id: str | None,
    brain_kind: str | None,
    source_type: str | None,
    origin: str | None,
) -> tuple[str, list]:
    """Shared WHERE clause. Date bounds are half-open `[since, until)`."""
    clauses = []
    params: list = []
    if since:
        clauses.append("u.created_at >= ?")
        params.append(since)
    if until:
        clauses.append("u.created_at < ?")
        params.append(until)
    if user_id:
        clauses.append("u.user_id = ?")
        params.append(user_id)
    if brain_kind:
        clauses.append("u.brain_kind = ?")
        params.append(brain_kind)
    if source_type:
        clauses.append("u.source_type = ?")
        params.append(source_type)
    if origin:
        clauses.append("u.origin = ?")
        params.append(origin)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_usage(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    user_id: str | None = None,
    brain_kind: str | None = None,
    source_type: str | None = None,
    origin: str | None = None,
    model: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Raw usage rows in a window. Bounds are ISO-Z and half-open.

    `model` filters through the child table, because a multi-model run's parent
    row names only its largest cost share.
    """
    where, params = _usage_filters(
        since=since, until=until, user_id=user_id, brain_kind=brain_kind,
        source_type=source_type, origin=origin,
    )
    if model:
        clause = (
            "u.id IN (SELECT task_usage_id FROM task_usage_models WHERE model = ?)"
        )
        where = f"{where} AND {clause}" if where else f" WHERE {clause}"
        params.append(model)
    sql = f"SELECT u.* FROM task_usage u{where} ORDER BY u.created_at DESC"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(conn.execute(sql, params).fetchall())


# Token aggregates filter `has_totals = 1`; context aggregates filter
# `initial_context_tokens IS NOT NULL` (which `COUNT` and `AVG` do for free).
# The two filters are independent because the two measures are: a run killed
# before its result frame has real context and meaningless zero tokens.
_USAGE_TOKEN_AGGREGATES = """
    COUNT(*) AS row_count,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN 1 ELSE 0 END), 0) AS measured_rows,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.billed_input_tokens ELSE 0 END), 0)
        AS billed_input_tokens,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.output_tokens ELSE 0 END), 0)
        AS output_tokens,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.cache_read_tokens ELSE 0 END), 0)
        AS cache_read_tokens,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.cache_write_tokens ELSE 0 END), 0)
        AS cache_write_tokens,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.turns ELSE 0 END), 0) AS turns,
    COALESCE(SUM(CASE WHEN u.has_totals = 1 THEN u.model_requests ELSE 0 END), 0)
        AS model_requests,
    COUNT(u.initial_context_tokens) AS context_rows,
    AVG(u.initial_context_tokens) AS avg_initial_context_tokens,
    AVG(u.peak_context_tokens) AS avg_peak_context_tokens,
    AVG(u.context_window) AS avg_context_window
"""


def _usage_row_to_dict(row: sqlite3.Row) -> dict:
    out = {k: row[k] for k in row.keys()}
    out["rows"] = out.pop("row_count")
    total_prompt = (
        out["billed_input_tokens"] + out["cache_read_tokens"] + out["cache_write_tokens"]
    )
    out["total_prompt_tokens"] = total_prompt
    out["total_tokens"] = total_prompt + out["output_tokens"]
    out["cache_hit_rate"] = (
        out["cache_read_tokens"] / total_prompt if total_prompt > 0 else 0.0
    )
    return out


def usage_summary(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    user_id: str | None = None,
    brain_kind: str | None = None,
    source_type: str | None = None,
    origin: str | None = None,
    model: str | None = None,
    group_by: str | None = None,
) -> Any:
    """Aggregate usage. Returns one dict, or a list of dicts when grouped.

    Cost is always a **map keyed by `cost_basis`**, never a scalar. A window's
    rows can span bases — an operator switching the CLI from a subscription to
    an API key mid-window is exactly the case — and summing a plan-equivalent
    into real spend is the misread this whole design refuses. Nothing here sums
    across bases, at any grouping.
    """
    where, params = _usage_filters(
        since=since, until=until, user_id=user_id, brain_kind=brain_kind,
        source_type=source_type, origin=origin,
    )
    model_clause = ""
    model_params: list = []
    if model:
        model_clause = (
            " AND u.id IN (SELECT task_usage_id FROM task_usage_models WHERE model = ?)"
        )
        model_params = [model]
        if not where:
            where = " WHERE 1=1"

    if group_by == "model":
        return _usage_summary_by_model(
            conn, where, params, model_clause, model_params
        )

    if group_by:
        expr = USAGE_GROUP_BY.get(group_by)
        if expr is None:
            raise ValueError(f"unknown grouping: {group_by}")
        rows = conn.execute(
            f"SELECT {expr} AS key, {_USAGE_TOKEN_AGGREGATES}"
            f" FROM task_usage u{where}{model_clause}"
            f" GROUP BY {expr}",
            params + model_params,
        ).fetchall()
        groups = [_usage_row_to_dict(r) for r in rows]
        for group in groups:
            group["cost_by_basis"] = _cost_by_basis(
                conn, where, params, model_clause, model_params,
                extra=f"{expr} IS ?", extra_params=[group["key"]],
            )
        if group_by == "day":
            groups.sort(key=lambda g: g["key"] or "")
        else:
            groups.sort(key=lambda g: -g["total_tokens"])
        return groups

    row = conn.execute(
        f"SELECT {_USAGE_TOKEN_AGGREGATES} FROM task_usage u{where}{model_clause}",
        params + model_params,
    ).fetchone()
    summary = _usage_row_to_dict(row)
    summary["cost_by_basis"] = _cost_by_basis(
        conn, where, params, model_clause, model_params
    )
    return summary


def _cost_by_basis(
    conn: sqlite3.Connection,
    where: str,
    where_params: list,
    model_clause: str = "",
    model_params: list | None = None,
    *,
    extra: str = "",
    extra_params: list | None = None,
) -> dict:
    """Cost totalled per `cost_basis`. Never collapsed into one figure.

    The three clause fragments are taken with their own parameter lists rather
    than pre-concatenated. Assembling the text in one order and the parameters
    in another binds the group key to the model predicate and vice versa, and
    because both are strings SQLite raises nothing — the query simply matches
    no rows and every group reports zero cost beside correct token counts.
    Keeping each fragment next to its own parameters is what makes the two
    orders impossible to get out of step.
    """
    clause = where
    all_params = list(where_params)
    if extra:
        clause = f"{clause} AND {extra}" if clause else f" WHERE {extra}"
        all_params += list(extra_params or [])
    # Appended last because `model_clause` is interpolated last.
    all_params += list(model_params or [])
    rows = conn.execute(
        f"SELECT u.cost_basis, COALESCE(SUM(u.cost_usd), 0.0) AS cost"
        f" FROM task_usage u{clause}{model_clause}"
        f" GROUP BY u.cost_basis",
        all_params,
    ).fetchall()
    return {r["cost_basis"]: float(r["cost"]) for r in rows}


UNATTRIBUTED_MODEL = "(unattributed)"


def _usage_summary_by_model(
    conn: sqlite3.Connection,
    where: str,
    params: list,
    model_clause: str,
    model_params: list | None = None,
) -> list[dict]:
    """Per-model grouping, over the child table plus the rows that have none.

    The parent row carries only the run's largest cost share, so grouping on it
    would attribute a whole multi-model run to one model. But reading the child
    table *alone* loses every measured row that produced no per-model split,
    and that is not a hypothetical: the native brain reports one total with no
    breakdown, so its whole spend would vanish from this grouping while still
    appearing in the ungrouped totals. Those rows are attributed to the parent's
    own `model`, or to `(unattributed)` when even that is empty, so the token
    columns partition the same population at every grouping.

    Context measures belong to the run rather than to a model and are NULL
    here — averaging a run's peak once per model it used would count one
    measurement several times. `has_totals` is filtered for the same reason
    every other aggregate filters it: a run killed before its result frame has
    meaningless zero tokens.
    """
    mparams = list(model_params or [])
    child_where = f"{where} AND u.has_totals = 1" if where else " WHERE u.has_totals = 1"
    rows = conn.execute(
        f"""
        SELECT key, COUNT(*) AS row_count, COUNT(*) AS measured_rows,
               COALESCE(SUM(billed_input_tokens), 0) AS billed_input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               0 AS turns, 0 AS model_requests, 0 AS context_rows,
               NULL AS avg_initial_context_tokens,
               NULL AS avg_peak_context_tokens,
               NULL AS avg_context_window
        FROM (
            SELECT m.model AS key, m.billed_input_tokens, m.output_tokens,
                   m.cache_read_tokens, m.cache_write_tokens
            FROM task_usage_models m
            JOIN task_usage u ON u.id = m.task_usage_id
            {child_where}{model_clause}

            UNION ALL

            SELECT CASE WHEN u.model = '' THEN ? ELSE u.model END AS key,
                   u.billed_input_tokens, u.output_tokens,
                   u.cache_read_tokens, u.cache_write_tokens
            FROM task_usage u
            {child_where}{model_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM task_usage_models m WHERE m.task_usage_id = u.id
              )
        )
        GROUP BY key
        """,
        params + mparams + [UNATTRIBUTED_MODEL] + params + mparams,
    ).fetchall()
    groups = [_usage_row_to_dict(r) for r in rows]
    for group in groups:
        basis_rows = conn.execute(
            f"""
            SELECT cost_basis, COALESCE(SUM(cost_usd), 0.0) AS cost FROM (
                SELECT u.cost_basis, m.cost_usd
                FROM task_usage_models m
                JOIN task_usage u ON u.id = m.task_usage_id
                {child_where}{model_clause} AND m.model = ?

                UNION ALL

                SELECT u.cost_basis, u.cost_usd
                FROM task_usage u
                {child_where}{model_clause}
                  AND (CASE WHEN u.model = '' THEN ? ELSE u.model END) = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM task_usage_models m
                      WHERE m.task_usage_id = u.id
                  )
            )
            GROUP BY cost_basis
            """,
            params + mparams + [group["key"]]
            + params + mparams + [UNATTRIBUTED_MODEL, group["key"]],
        ).fetchall()
        group["cost_by_basis"] = {
            r["cost_basis"]: float(r["cost"]) for r in basis_rows
        }
    groups.sort(key=lambda g: -g["total_tokens"])
    return groups


def unmeasured_task_count(
    conn: sqlite3.Connection,
    *,
    since: str,
    until: str | None = None,
    user_id: str | None = None,
) -> int:
    """Tasks in the window with no `task_usage` row at all.

    An honesty counter: `TmuxClaudeBrain` spends real tokens and writes no row,
    and recording a synthetic zero for it would drag every average down while
    making the dashboard look complete.

    **`since` and `until` are in `tasks.created_at`'s format**
    (`2026-08-20 09:00:00`), not the ISO-Z format every other function in this
    section takes. Passing an ISO-Z bound here compares `'…T…'` against
    `'… …'` and silently excludes every task on the boundary day — no error,
    just a smaller number. Build them with `sql_datetime_days_ago`.
    """
    clauses = ["t.created_at >= ?"]
    params: list = [since]
    if until:
        clauses.append("t.created_at < ?")
        params.append(until)
    if user_id:
        clauses.append("t.user_id = ?")
        params.append(user_id)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM tasks t
        WHERE {" AND ".join(clauses)}
          AND NOT EXISTS (SELECT 1 FROM task_usage u WHERE u.task_id = t.id)
        """,
        params,
    ).fetchone()
    return int(row[0])


def prune_old_usage(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete usage rows older than `retention_days`. Returns rows deleted.

    `0` disables pruning. Children are deleted explicitly and first: the FK is
    decorative because `PRAGMA foreign_keys` is never enabled on these
    connections, so ON DELETE CASCADE would leave orphans behind.

    Bounds are built in Python as ISO-Z rather than with `datetime('now', '-N
    days')`. That idiom is what `cleanup_old_tasks` uses against
    `tasks.created_at`, and copying it here would compare a space-separated
    bound against ISO-Z values and invert same-day comparisons.
    """
    if retention_days <= 0:
        return 0
    cutoff = iso_utc_days_ago(retention_days)
    conn.execute(
        """
        DELETE FROM task_usage_models
        WHERE task_usage_id IN (SELECT id FROM task_usage WHERE created_at < ?)
        """,
        (cutoff,),
    )
    cursor = conn.execute("DELETE FROM task_usage WHERE created_at < ?", (cutoff,))
    # Sweep any child whose parent is already gone. The delete above is scoped
    # through `task_usage`, so an orphan from an earlier partial delete would be
    # invisible to it forever — the row would never age out because nothing
    # reads its date.
    conn.execute(
        """
        DELETE FROM task_usage_models
        WHERE NOT EXISTS (
            SELECT 1 FROM task_usage p WHERE p.id = task_usage_models.task_usage_id
        )
        """
    )
    return cursor.rowcount
