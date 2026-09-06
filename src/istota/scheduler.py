"""Task scheduler - processes pending tasks and briefings."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import random
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from croniter import croniter

# Top-level rather than imported where used: the admission gate consults it on
# every dispatch tick (~0.5s), and it is a stdlib-only leaf with no import of
# its own back into the package, so there is no cycle to avoid.
from . import host_pressure as host_pressure_mod
from . import task_cgroup

logger = logging.getLogger("istota.scheduler")
# What a partial answer from an interrupted run is labelled with when it is
# delivered beside a failure notice (ISSUE-372). Its job is to stop the prose
# reading as a finished answer, which is the same job the native brain's
# `_PARTIAL_ANSWER_MARKERS` do for the stops that deliver as a success.
PARTIAL_WORK_MARKER = "**What I had before it stopped:**"


def _with_partial_work(body: str, task: "db.Task") -> str:
    """Append an interrupted run's partial answer to a failure notice.

    One composer rather than a copy per surface: the Talk branch and the
    email-only alert branch are three lines apart and answer the same question,
    and the email-only one is the branch that most needs it — its own comment
    says `tasks.result` is the only copy of the answer left there and nothing
    puts it in front of the user. Below the error, never above it: the first
    line still has to say the task failed.

    Not length-capped. The Talk transport already splits a long body at 30,000
    characters, which is what a long *successful* answer does; truncating here
    would defeat the fix while the untruncated copy sat on a row the user cannot
    read.
    """
    if not task.partial_result:
        return body
    return f"{body}\n\n{PARTIAL_WORK_MARKER}\n\n{task.partial_result}"
# Dedicated logger so operators can isolate the periodic health line from the
# noisy general scheduler logger (`journalctl … | grep scheduler_stats`).
_SCHEDULER_STATS_LOGGER = logging.getLogger("istota.scheduler.stats")
# Same reasoning for the host-pressure breadcrumb: its own logger, so a
# multi-day series can be pulled out whole (`journalctl … | grep host_pressure`)
# without the surrounding scheduler chatter.
_HOST_PRESSURE_LOGGER = logging.getLogger("istota.scheduler.pressure")
# Warn at most once when psutil is unavailable rather than on every emit.
_psutil_unavailable_warned = False
# …and once when /proc has no pressure interface at all (macOS, a kernel built
# without CONFIG_PSI). Absence is a platform fact, not a fault, so it is said
# once and then the emit is a no-op rather than a line per interval.
_host_pressure_unavailable_warned = False

# Source types the system generates on its own (not user-submitted). Used to
# suppress the "A task you submitted was cancelled" notice when these age out —
# notifying their output channel turns one wedged worker into a notification
# flood (a `* * * * *` cron aging out 130+ backed-up runs).
_AUTOMATED_SOURCE_TYPES = frozenset({"scheduled", "briefing", "heartbeat", "subtask"})

# How long a per-message deletion stays in the ledger the room stream tails.
_MESSAGE_DELETION_RETENTION_DAYS = 30

# Keys already warned about, for _warn_once. A misconfiguration noticed inside
# a per-tick check is a static fact about the config, so logging it on every
# tick would be ~1440 identical lines a day.
_warned_keys: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen this process."""
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    logger.warning("%s", message)

from . import avatars, confirmations, db
from .brain import (
    make_brain,
    resolve_brain_kind,
    room_selectable_kinds,
    split_effort,
)
from .build_info import build_description
from .consumers import (
    LogChannelSubscriber,
    PushNotificationSubscriber,
    TalkEventSubscriber,
)
from .db_health import CheckReport, check_and_repair
from .events import EventWriter, PROGRESS_MESSAGES
from .shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE, is_sigpipe_failure, shell_argv
from .skills.briefing import (
    get_briefings_for_user,
    parse_briefing_json,
    strip_briefing_preamble,
)
from . import config as istota_config
from .config import BriefingConfig, Config, SchedulerConfig, load_config
from .brain.claude_code import is_api_error_banner, is_permanent_api_error
from .executor import (
    detect_malformed_result,
    discover_calendars_for_task,
    execute_task,
    is_no_final_answer,
    is_signal_termination,
    is_transient_api_error,
    parse_api_error,
)
from .async_runtime import reset_async_runtime, run_coro
from .nextcloud import avatars as nc_avatars
from .nextcloud._http import nc_configured
from .nextcloud_api import hydrate_user_configs
from .modules import MODULE_NAMES
from .notification_resolvers import confirmation as confirmation_source
from .notification_resolvers import cron_job as cron_job_source
from .notification_resolvers import task_alert as task_alert_source
from .notification_store import (
    RaiseResult,
    deliver_pending,
    mark_delivered,
    sweep_expired_alerts,
    sweep_retention,
)
from .notifications import effective_log_destinations, send_notification
from .process_group import kill_process_group
from .session.session_log import (
    SWEEP_STATE_KEY,
    SWEEP_STATE_NAMESPACE,
    SweepResult,
    encode_sweep_state,
    resolve_session_log_dir,
    sweep_session_logs,
)
from .transport import (
    Destination,
    is_canonical_room_view,
    make_registry,
    parse_output_target,
    plan_has_surface,
    resolve_delivery_plan,
    transcript_room_for_task,
)
from .surfaces import (
    is_room_member,
    is_room_view,
    origin_surface_for_source_type,
)
from .transport.registry import _surface_for_source_type
from .storage import ensure_user_directories_v2

# Deferred-op handlers were extracted to a sibling module; re-export the
# names so existing tests and call-sites that import them from this module
# keep working. Callers that touch this module directly (process_one_task,
# the retry path) reference these names unqualified, so the re-export is
# load-bearing.
from .scheduler_deferred import (  # noqa: F401  -- re-exported for back-compat
    _KNOWN_DEFERRED_SUFFIXES,
    _load_deferred_json,
    _process_deferred_garmin_import,
    _process_deferred_health_ops,
    _process_deferred_kg_ops,
    _process_deferred_kv_ops,
    _process_deferred_sent_emails,
    _process_deferred_subtasks,
    _process_deferred_user_alerts,
    _process_retired_deferred_files,
    _purge_deferred_files_for_retry,
    _warn_unconsumed_deferred_files,
)

def _now(tz=None):
    """Current time — thin wrapper for testability."""
    return datetime.now(tz)


def _is_stale_fire(
    name: str,
    next_run: datetime,
    now_naive: datetime,
    threshold_minutes: int,
) -> bool:
    """Return True if `next_run` is more than `threshold_minutes` behind `now_naive`.

    Suppresses thundering-herd catch-up after a long daemon outage. Callers
    bump `last_run_at` to "now" when this returns True so croniter resumes
    cleanly from the next future fire-time instead of looping on the same
    stale next_run. 0 disables the gate (legacy unconditional catch-up).
    """
    if threshold_minutes <= 0:
        return False
    staleness_min = (now_naive - next_run).total_seconds() / 60
    if staleness_min <= threshold_minutes:
        return False
    logger.warning(
        "Skipping stale fire of '%s' (missed by %.1f min, threshold %d min)",
        name, staleness_min, threshold_minutes,
    )
    return True


# Graceful shutdown flag
_shutdown_requested = False

# Singleton daemon lock. A module constant (not hardcoded inline) so the
# combined ``istota serve`` launcher and tests can point it elsewhere; the
# default matches the systemd/server deployment.
DAEMON_LOCK_PATH = Path("/tmp/istota-scheduler-daemon.lock")


class _DaemonAlreadyRunning(RuntimeError):
    """Raised by ``run_daemon`` when the singleton flock is already held.

    Lets the combined ``istota serve`` launcher report "already running" and
    exit non-zero. The standalone ``main()`` path catches it and exits cleanly
    (preserving the old "log + return" behaviour without a traceback).
    """


def _signal_handler(signum, frame):
    """Handle shutdown signals."""
    global _shutdown_requested
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _shutdown_requested = True


def request_shutdown() -> None:
    """Request a graceful shutdown of the daemon loop.

    Sets the same flag the SIGTERM/SIGINT handler sets. Used by the combined
    ``istota serve`` launcher, which owns process signal handling itself and
    runs ``run_daemon(install_signal_handlers=False)`` on a worker thread.
    """
    global _shutdown_requested
    _shutdown_requested = True

# Pattern to detect confirmation requests in Claude's output
CONFIRMATION_PATTERN = re.compile(
    r'(?:'
    r'I need your confirmation|'
    r'Please confirm|'
    r'Reply "?yes"?|'
    r'Reply yes or no|'
    r'Do you want me to proceed|'
    r'Should I proceed|'
    r'Can you confirm'
    r')',
    re.IGNORECASE
)

_POLICY_REFUSAL_KEYWORDS = ("safety", "policy", "content", "refused", "harm", "blocked")

_FROM_HEADER_PATTERN = re.compile(r"(?:^|\n)From:\s*(.+?)(?:\n|$)")

# Grace margin added to task_timeout_minutes before a 'running' task is treated
# as stuck (worker presumed dead) and reclaimed. A healthy worker self-kills at
# the timeout and writes its result; the margin covers that write. Without it,
# the reclaim window (formerly a flat 15 min) sits below the 30-min timeout, so
# a slow-but-healthy task — notably the in-process native brain, which has no
# killable PID — gets reclaimed and duplicated (ISSUE-112).
_STUCK_RUNNING_GRACE_MINUTES = 5


def _stuck_running_minutes(sched) -> int:
    """Fallback stuck threshold (minutes) for a task that never heart-beat."""
    return sched.task_timeout_minutes + _STUCK_RUNNING_GRACE_MINUTES


@contextlib.contextmanager
def _task_heartbeat(config: Config, task_id: int):
    """Ping the task's liveness while its body runs (ISSUE-112).

    A background daemon thread touches ``last_heartbeat`` every
    ``worker_heartbeat_seconds`` so stuck-task reclaim can tell a slow-but-alive
    worker from a dead one — the native brain runs in-process with no killable
    PID and no subprocess to die, so without this a long task looks identical to
    a crashed one. The first ping fires immediately, so liveness is recorded as
    soon as the body starts; the thread is stopped on exit (incl. on exception),
    which lets reclaim fire promptly once a worker really dies.
    """
    interval = config.scheduler.worker_heartbeat_seconds
    if interval <= 0:
        yield  # heartbeat disabled
        return

    stop = threading.Event()

    def _loop():
        while True:
            try:
                with db.get_db(config.db_path) as conn:
                    db.touch_task_heartbeat(conn, task_id)
            except Exception:
                logger.debug("heartbeat ping failed for task %s", task_id, exc_info=True)
            if stop.wait(interval):
                return

    thread = threading.Thread(target=_loop, name=f"heartbeat-{task_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def recover_orphaned_tasks_on_startup(config: Config) -> None:
    """Reclaim tasks abandoned mid-execution by a dead prior daemon instance.

    Runs once at startup under the flock, before any worker spawns, so every
    ``running``/``locked`` row is an orphan (see ``db.recover_orphaned_tasks``).
    This recovers a scheduler restart in seconds instead of waiting out
    ``worker_stuck_minutes``; the time-based reclaim in ``run_cleanup_checks``
    then only has to cover the rarer worker-died-but-daemon-survived case.

    For orphans that won't re-run (cancelled / failed) we emit a terminal event
    frame so a watching web client gets immediate closure instead of a hung
    spinner. The ``EventWriter`` resumes ``seq`` above any partial events the
    dead attempt streamed, and runs with no subscribers — the SSE/snapshot
    clients pick the frame up by polling the ``task_events`` table. Released
    orphans emit nothing: their re-run streams a fresh ``task_started`` and the
    client resumes from its cursor (the documented retry-continuity path).
    """
    with db.get_db(config.db_path) as conn:
        recovered = db.recover_orphaned_tasks(
            conn, config.scheduler.max_retry_age_minutes,
        )
    if not recovered:
        return

    counts = {"released": 0, "cancelled": 0, "failed": 0}
    for info in recovered:
        action = info["action"]
        counts[action] = counts.get(action, 0) + 1
        if action == "released":
            continue  # re-run emits its own task_started; nothing to emit here

        writer = EventWriter(
            info["id"], str(config.db_path),
            enabled=config.scheduler.event_log_enabled,
        )
        if action == "cancelled":
            writer.emit("cancelled")
            writer.emit("done", {"stop_reason": "cancelled", "duration_seconds": 0})
        else:  # failed
            writer.emit("error", {
                "message": "Interrupted by a scheduler restart and not retried.",
                "stop_reason": "error",
            })
            writer.emit("done", {"stop_reason": "error", "duration_seconds": 0})
        writer.finish()

    logger.warning(
        "STARTUP Recovered %d orphaned task(s) from a prior instance "
        "(released=%d, cancelled=%d, failed=%d)",
        len(recovered), counts["released"], counts["cancelled"], counts["failed"],
    )


def _is_policy_refusal(error_text: str) -> bool:
    """Check if a task failure is an API policy/safety refusal (non-retryable).

    Requires a *banner-shaped* error: this both suppresses retry and fires a
    "your content was blocked" alert at the user, so an answer that merely
    discusses a 400 content-policy error must not trigger it.
    """
    if not is_api_error_banner(error_text):
        return False
    parsed = parse_api_error(error_text)
    if not parsed:
        return False
    if parsed["status_code"] != 400:
        return False
    msg = (parsed.get("message") or "").lower()
    return any(kw in msg for kw in _POLICY_REFUSAL_KEYWORDS)


def _post_policy_refusal_alert(
    config: "Config", task: "db.Task", error_text: str,
) -> None:
    """Post an alert to the user's alerts channel when content triggers an API policy refusal.

    For email tasks, extracts the sender from the prompt's `From:` header so the user can
    see who tripped the filter. For other sources, falls back to the conversation token.
    """
    parsed = parse_api_error(error_text)
    api_msg = (parsed or {}).get("message") or "Unknown"

    sender = None
    if task.source_type == "email" and task.prompt:
        m = _FROM_HEADER_PATTERN.search(task.prompt)
        if m:
            sender = m.group(1).strip()

    if task.source_type == "email" and sender:
        message = (
            f"⚠️ **Inbound email blocked** (task #{task.id})\n\n"
            f"Email from **{sender}** triggered the API safety filter "
            f"and was not processed.\n\n"
            f"Reason: {api_msg}"
        )
    else:
        source_label = task.conversation_token or task.source_type or "unknown"
        message = (
            f"⚠️ **Task blocked by API safety filter** (task #{task.id})\n\n"
            f"Content from {source_label} triggered a policy refusal "
            f"and was not processed.\n\n"
            f"Reason: {api_msg}"
        )

    try:
        send_notification(config, task.user_id, message, purpose="alert")
    except Exception as e:
        logger.warning(
            "Failed to post policy refusal alert for task %d (user=%s): %s",
            task.id, task.user_id, e,
        )


def _format_error_for_user(error_text: str) -> str:
    """
    Convert raw error text to a user-friendly message for Talk.

    Handles API errors, OOM, timeouts, and other common failure modes.
    Logs the full error details but returns a friendly message with personality.
    """
    from .executor import FALLBACK_EXHAUSTED_MARKER

    if FALLBACK_EXHAUSTED_MARKER in error_text:
        # Primary *and* fallback both unavailable. Say so plainly rather than
        # echoing whichever raw provider error came back last (ISSUE-212).
        logger.debug("both brains unavailable: %s", error_text[:300])
        return (
            "Both my primary and backup brains are unavailable right now — the "
            "provider is having a moment. Try again shortly."
        )

    parsed = parse_api_error(error_text)
    if parsed:
        status = parsed["status_code"]
        request_id = parsed.get("request_id")
        # Log full details for debugging
        logger.debug(
            "API error for user message: status=%d, request_id=%s, message=%s",
            status, request_id, parsed.get("message"),
        )
        if status >= 500 or status == 529:
            return "Lost contact with the mothership. Anthropic's having a moment — try again shortly."
        elif status == 429:
            return "Being throttled by the mothership. Apparently I'm too chatty. Give it a minute."
        elif status in (401, 403):
            return "Can't authenticate with Anthropic — locked out of my own brain. This needs human intervention."
        elif status == 400 and _is_policy_refusal(error_text):
            return "Content triggered the API safety filter and couldn't be processed. Check the alerts channel for details."
        else:
            return "Something went wrong talking to Anthropic. The deep stared back. Try again?"

    # Non-API errors: strip technical details, keep it friendly
    if "killed (likely out of memory)" in error_text:
        return "Ran out of memory — tried to hold too much in all eight arms at once. Try something simpler?"

    # A network-level provider failure carries no status code to parse, but it
    # is the same "the provider is unreachable" story — and it must be checked
    # before the bare "timed out" substring below, which would otherwise blame
    # the user's task for a connection timeout (ISSUE-212).
    if is_transient_api_error(error_text):
        logger.debug("transient provider error for user message: %s", error_text[:300])
        return "Lost contact with the mothership. Anthropic's having a moment — try again shortly."

    if "timed out" in error_text.lower():
        return "Drifted too deep and timed out. Maybe break this into smaller pieces?"

    # Generic fallback - don't expose raw error
    return "Something went sideways and I'm not entirely sure what. Resurfacing — try again?"


def _error_event_message(error_text: str) -> str:
    """The user-facing message for a terminal ``error`` task event.

    Stream surfaces (web chat, REPL) render this payload directly as the turn
    body — they never pass through `_format_error_for_user`, which only the Talk
    push path calls. So a raw provider error, and the internal
    `FALLBACK_EXHAUSTED_MARKER`, would reach the user verbatim there (ISSUE-212
    asks that they never do).

    Only *provider-availability* failures are reworded; every other failure
    keeps its original text, which is usually the most useful thing a developer
    watching the REPL can see. The raw text is still stored on `tasks.error`
    either way.
    """
    from .executor import FALLBACK_EXHAUSTED_MARKER

    if FALLBACK_EXHAUSTED_MARKER in error_text or is_transient_api_error(error_text):
        return _format_error_for_user(error_text)
    return error_text[:500]


def _is_shutdown_collateral(result: str) -> bool:
    """True when a failure is this daemon's own shutdown killing the task.

    Under systemd's default ``KillMode=control-group`` a ``systemctl restart``
    (the auto-update cron issues one on every new commit) SIGTERMs every
    process in the cgroup — including an in-flight task's `claude` subprocess.
    The daemon's own handler shuts down gracefully, so the worker survives long
    enough to record the corpse as a task failure and, on a final attempt,
    fail it permanently. Nothing about the task failed, so it's requeued
    instead (ISSUE-191).

    Narrow on purpose: only a signal death, and only while shutting down. A
    genuine model error that happens to land during shutdown takes the normal
    path. The reverse race — the child is signalled but the daemon hasn't set
    the flag yet — degrades to that same normal retry path.
    """
    return _shutdown_requested and is_signal_termination(result)


def _strip_action_prefix(result: str) -> tuple[bool, str]:
    """Parse ACTION:/NO_ACTION: prefixes from a silent task result.

    Returns (should_post, result_to_post). If ACTION: found, strips prefix
    and returns True. If NO_ACTION: found, returns False. If no prefix,
    returns True with original result (fail-safe: post as-is).

    Delegates to `db.scheduled_assistant_body` (single source of truth, shared
    with the transcript backfill) — None there means "don't post".
    """
    body = db.scheduled_assistant_body(True, result)
    if body is None:
        return False, result
    return True, body


def _store_room_turn(conn, task, room_token: str | None, body: str) -> int | None:
    """Store a task's delivered result as an assistant spine row in a room's
    canonical transcript — for ANY source type — when the room is web-visible.

    Any bot output delivered into a real, web-visible room belongs in that
    room's transcript, whatever ``source_type`` produced it (subtask, scheduled,
    briefing, heartbeat, an email round-trip continued from a web room, …). The
    web transcript reader renders every assistant row of a room, so storing the
    delivered body here is the whole producer half of making "what the room
    shows in Talk, it shows in web" true (ISSUE-176). This subsumes the former
    per-source-type helpers ``_store_scheduled_room_turn`` (ISSUE-133) and
    ``_store_web_room_turn`` (ISSUE-164): one producer, not one-per-type, and
    adding a new room-posting source type needs no new helper.

    ``room_token`` is the room the exchange belongs to, resolved once by
    ``transport.routing.transcript_room_for_task`` before any per-surface
    branch. It is **not** ``task.conversation_token``: on an email task that
    column is a thread hash naming no room, and reading it as one here was the
    reason an email answer reached the room as a system note instead of a turn
    (ISSUE-247). The gate is **room existence**, not source type: no room means
    nothing to mirror into and the helper no-ops. ``origin_surface`` records
    the task's real source type as provenance without gating visibility — the
    generalized ``TRANSCRIPT_SURFACE_FILTER`` admits any assistant row. Only an
    assistant row is ever written, never a ``role='user'`` row, so a
    non-conversational post can't re-pair into LLM context (the load-bearing
    "user rows are conversational-only" invariant). Idempotent across retries
    (``store_turn_message`` dedups on ``(room, role, task_id)``); returns the new
    message id, or None when it no-ops or the row already exists."""
    if not room_token:
        return None
    if db.get_room(conn, room_token) is None:
        return None
    return db.store_turn_message(
        conn, room_token, role="assistant", body=body,
        task_id=task.id, origin_surface=task.source_type,
    )


def _room_turn_belongs_here(
    conn, task: db.Task, task_id: int, room_token: str | None, *,
    delivering_into_room: bool,
) -> bool:
    """Whether this task's answer belongs in its room's canonical transcript.

    One decision, reachable two ways. It replaces three `_store_room_turn` calls
    that each hung off a different delivery branch — the Talk plan, an own-room
    web push, and the email-only plan the first two missed. Each was added when
    another routing shape turned up with no answer under its question, and that
    set was never going to close while the condition was spelled per branch.

    ``delivering_into_room`` is true when the plan actually posts this answer
    into the room on some surface: Talk, or a surface whose view of the room is
    the canonical store itself. That is a statement of intent — the answer is a
    turn in this conversation — and it is what makes an own-room web push an
    assistant bubble rather than an unsolicited `role='system'` note
    (ISSUE-164).

    Failing that, the room having the **question** in it is the evidence. This
    is the case a `thread` reply-routing policy produces: an email-only plan,
    nothing delivered into the room at all, but the exchange genuinely happened
    there and `record_inbound` mirrored the question in. Without this the
    question sits with no answer under it (ISSUE-136).

    Neither holds for a room that never received the question and is not being
    delivered into — a turn predating the mirror, or a token bound to the room
    after the fact. Storing there grows an answer-only bubble, which is
    ISSUE-136 reached from the other side.

    The evidence rung deliberately does **not** re-check ``source_type ==
    "email"`` the way the branch it replaces did. A ``role='user'`` row keyed to
    a task id only ever exists for talk, web and email (the three surfaces
    reaching ``record_inbound``), and talk/web already stored their assistant
    row on the conversational path above — so widening it only re-attempts a
    deduped insert. That equivalence leans on the conversational store staying
    where it is: narrow its `is_room_member(...)` gate and this rung
    starts writing `room_body` for those turns, which is a different body from
    the `result` that path stores.

    ``room_token`` is the resolved transcript room, not
    ``task.conversation_token`` — see `_store_room_turn`.
    """
    if not room_token:
        return False
    if delivering_into_room:
        return True
    return db.get_turn_message_id(conn, room_token, task_id, "user") is not None


def _canonical_talk_room(conn, talk_token: str) -> str:
    """The canonical room token a Talk channel belongs to, or the channel itself.

    A promoted web room's Talk ref is not its own token, so the two only compare
    equal after this.
    """
    return db.resolve_room_token(conn, "talk", talk_token) or talk_token


def _talk_result_mirror_body(
    conn, task, talk_token: str, transcript_token: str | None,
    body: str, web_push_dests,
) -> str | None:
    """The result body to mirror into the *delivered* Talk room, or None.

    `_store_room_turn` writes into the room this exchange belongs to; the Talk
    post goes to the plan's resolved ``talk_token``. Those are normally two
    names for one room, and then the canonical row already covers it. When they
    genuinely differ — a task delivered to a Talk room that is not its own — the
    web view of *that* room has nothing to show, which is the ISSUE-242 gap on
    the result rather than on a notification.

    An assistant row is not available there: the room holds no question, so the
    row would be an orphaned bubble (ISSUE-136 reached from the other side). It
    gets the ``role='system'`` treatment an alert already gets, and never
    re-pairs into LLM context.

    **This no longer fires for an email task.** It used to be the *only* thing
    reaching that room, because the writer above keyed on
    ``task.conversation_token`` and an email task's is a thread hash naming no
    room (ISSUE-247). It was also handed the transcript body while Talk was
    posted the delivered one, so the two surfaces showed different text for the
    same message; it is now handed exactly what Talk was posted, which is the
    only body a mirror of a Talk post can honestly carry.

    Two cases return None: the Talk post lands in the room the canonical row was
    already written to, and a room the plan also pushes to over ``web``, which
    would otherwise get two rows for one message.
    """
    canonical = _canonical_talk_room(conn, talk_token)
    if transcript_token and canonical == transcript_token:
        return None
    if any(d.channel == canonical for d in web_push_dests):
        return None
    return body


def download_talk_attachments(config: Config, attachments: list[str]) -> list[str]:
    """
    Get local paths for Talk attachments.

    Talk attachments arrive as Nextcloud paths (e.g., "Talk/filename.jpg").

    If using mount:
        Returns mount paths directly (no download needed).
    If using rclone:
        Downloads to temp directory before Claude Code execution.

    Returns list of local paths (or original paths as fallback on error).
    """
    if not attachments:
        return []

    local_paths = []
    for att in attachments:
        if att.startswith("Talk/"):
            if config.use_mount:
                # Use mount path directly - no download needed
                mount_path = config.nextcloud_mount_path / att
                if mount_path.exists():
                    local_paths.append(str(mount_path))
                    logger.debug(f"Talk attachment via mount: {att} -> {mount_path}")
                else:
                    # File may be in a user's Talk folder (NC stores shared files
                    # in the sender's data dir). Check NC data dir if available.
                    nc_data = Path("/mnt/nc-data")
                    found = False
                    if nc_data.is_dir():
                        filename = att.split("/", 1)[1] if "/" in att else att
                        for user_dir in nc_data.iterdir():
                            candidate = user_dir / "files" / "Talk" / filename
                            if candidate.exists():
                                local_paths.append(str(candidate))
                                logger.debug(f"Talk attachment via nc-data: {att} -> {candidate}")
                                found = True
                                break
                    if not found:
                        logger.warning(f"Talk attachment not found at mount path: {mount_path}")
                        local_paths.append(att)  # Fall back to original path
            else:
                # Download via rclone to temp directory
                config.temp_dir.mkdir(parents=True, exist_ok=True)
                remote_path = f"{config.rclone_remote}:{att}"
                result = subprocess.run(
                    ["rclone", "copy", remote_path, str(config.temp_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    # rclone copy preserves filename, so actual file is temp_dir/filename
                    actual_path = config.temp_dir / Path(att).name
                    if actual_path.exists():
                        local_paths.append(str(actual_path))
                        logger.debug(f"Downloaded Talk attachment: {att} -> {actual_path}")
                    else:
                        logger.warning(f"Downloaded file not found: {actual_path}")
                        local_paths.append(att)  # Fall back to original path
                else:
                    logger.warning(f"Failed to download {att}: {result.stderr}")
                    local_paths.append(att)  # Fall back to original path
        else:
            local_paths.append(att)

    return local_paths


def get_worker_id(user_id: str | None = None) -> str:
    """Generate a unique worker ID, optionally scoped to a user."""
    base = f"{socket.gethostname()}-{os.getpid()}"
    if user_id is not None:
        return f"{base}-{user_id}"
    return base


async def edit_talk_message(
    config: Config, task: db.Task, message_id: int, message: str,
    *, target_token: str | None = None,
) -> bool:
    """Edit a Talk message in-place. Returns True on success, False on failure.

    ``target_token`` overrides ``task.conversation_token`` for the edit, exactly
    as it does for ``post_result_to_talk`` — the message being edited lives in
    whichever room it was posted to, and on a promoted room that is the room's
    ``talk`` binding rather than its canonical token (ISSUE-400). The caller
    resolves it once and hands it down; resolving here would cost a database
    read per progress edit, which is one per tool call.

    Thin shim over ``TalkTransport.edit`` — the surface logic (and the
    ``TalkClient`` construction) lives in ``transport/talk/``."""
    token = target_token or task.conversation_token
    if not config.nextcloud.url or not token:
        return False
    from .transport.talk import TalkTransport
    try:
        await TalkTransport(config).edit(token, message_id, message)
        return True
    except Exception as e:
        logger.debug("Edit message %d failed: %s", message_id, e)
        return False


# ---------------------------------------------------------------------------
# Log channel — verbose per-user task execution log
# ---------------------------------------------------------------------------

# Per-process cache: conversation_token → displayName
_channel_name_cache: dict[str, str] = {}


async def _resolve_channel_name(config: Config, conversation_token: str) -> str:
    """Resolve a Talk conversation token to its display name.

    Thin process-cache wrapper over ``TalkTransport.resolve_channel_name`` — the
    OCS read itself lives behind the transport seam now. Only meaningful for
    Talk-origin tasks; callers gate on the origin surface before invoking.
    Results are cached for the lifetime of the process.
    """
    if conversation_token in _channel_name_cache:
        return _channel_name_cache[conversation_token]
    from .transport.talk import TalkTransport
    name = await TalkTransport(config).resolve_channel_name(conversation_token)
    _channel_name_cache[conversation_token] = name
    return name


def _log_channel_source_label(task: db.Task, channel_name: str | None) -> tuple[str, str]:
    """Return (task_id_prefix, source_suffix) for log channel messages."""
    source = channel_name if task.conversation_token and channel_name else task.source_type
    return f"**[#{task.id}]**", source


def _deduplicate_descriptions(descriptions: list[str]) -> list[str]:
    """Collapse consecutive identical descriptions with a count suffix."""
    if not descriptions:
        return []
    result = []
    prev = descriptions[0]
    count = 1
    for desc in descriptions[1:]:
        if desc == prev:
            count += 1
        else:
            result.append(f"{prev} ×{count}" if count > 1 else prev)
            prev = desc
            count = 1
    result.append(f"{prev} ×{count}" if count > 1 else prev)
    return result


def _format_log_channel_body(
    prefix: str | tuple[str, str], descriptions: list[str], *, done: bool = False,
    success: bool = True, error: str | None = None,
    skills: list[str] | None = None,
    model: str | None = None, effort: str | None = None,
) -> str:
    """Format a log channel message with accumulated tool descriptions."""
    if isinstance(prefix, tuple):
        task_prefix, source = prefix
    else:
        task_prefix, source = prefix, ""
    total = len(descriptions)
    count = f"({total} action{'s' if total != 1 else ''})" if total else "(no tool calls)"
    if done:
        status = "✅ Done" if success else "❌ Failed"
    else:
        status = "⏳ Running"
    header = f"{task_prefix} {status} {count} - {source}" if source else f"{task_prefix} {status} {count}"
    spec = " ".join(p for p in (model, effort) if p)
    if spec:
        header = f"{header} ({spec})"
    lines = [header, ""]
    if skills:
        lines.append(f"Skills: {', '.join(skills)}")
        lines.append("")
    for desc in _deduplicate_descriptions(descriptions):
        lines.append(desc)
    if done and error:
        lines.append(f"Error: {error[:200]}")
    return "\n".join(lines)


def _finalize_log_channel(
    config: Config, task: db.Task, log_dests: list[Destination], prefix: str,
    log_callback, success: bool, error: str | None = None,
    skills: list[str] | None = None,
    model: str | None = None, effort: str | None = None,
):
    """Post/edit the final summary to every resolved log destination.

    Edit-capable surfaces that streamed during the run get their in-flight
    message edited to the final state; edit-capable surfaces with no prior
    message (no tool calls) and all non-edit surfaces get a single fresh
    delivery of the footer. Each destination is delivered through its registered
    transport; one failing destination never aborts the others or the task.
    """
    descriptions = getattr(log_callback, "all_descriptions", []) if log_callback else []
    delivery_state = getattr(log_callback, "delivery_state", {}) if log_callback else {}

    body = _format_log_channel_body(
        prefix, descriptions, done=True, success=success, error=error,
        skills=skills, model=model, effort=effort,
    )

    registry = make_registry(config)
    for dest in log_dests:
        transport = registry.get(dest.surface)
        if transport is None:
            continue
        msg_id = delivery_state.get((dest.surface, dest.channel))
        try:
            if transport.capabilities.supports_edit and msg_id is not None:
                run_coro(transport.edit(dest.channel, msg_id, body))
            else:
                run_coro(transport.deliver(
                    dest.channel, body, task=task,
                    reference_id=f"istota:log:{task.id}",
                ))
        except Exception as e:
            logger.debug(
                "Log channel finalize failed for task %d dest %s: %s",
                task.id, dest.surface, e,
            )


def _count_pending(config: Config, user_id: str, queue_type: str) -> int:
    """Cheap claimable-task read for the idle pre-check (mirrors dispatch()).

    Uses the claimability-aware count so a follow-up gated behind an active task
    in the same room reads as 0 — the idle worker keeps sleeping cheaply instead
    of busy-polling claim_task until the gate clears.

    Reads with a short busy_timeout so a locked DB reads as "no work" (the worker
    keeps idling) rather than blocking; the next idle poll retries.
    """
    timeout_ms = config.scheduler.main_loop_read_timeout_ms or None
    try:
        with db.get_db(config.db_path, busy_timeout_ms=timeout_ms) as conn:
            return db.count_claimable_tasks_for_user_queue(conn, user_id, queue_type)
    except sqlite3.OperationalError as exc:
        logger.warning("idle_precheck_db_locked user=%s queue=%s err=%s", user_id, queue_type, exc)
        return 0


def _worker_idle_wait(
    user_id: str,
    queue_type: str,
    config: Config,
    stop_event: threading.Event,
    should_stop: Callable[[], bool],
    run_one: Callable[[], "tuple[int, bool] | None"],
    pending_count: Callable[[], int],
    admission_open: Callable[[], bool] = lambda: True,
) -> "tuple[int, bool] | None":
    """Park an idle worker, re-checking for work on a fine cadence.

    Polls for a pending task every ``worker_idle_poll_interval`` until
    ``worker_idle_timeout`` of *continuous* emptiness elapses, then returns
    ``None`` so the caller exits the worker. Returns the first non-None
    ``run_one()`` result as soon as a task is claimed, so the caller can loop
    back into its fast path with a fresh deadline.

    The fine-cadence path mirrors ``_dispatch_sleep``: it sleeps in
    ``time.sleep`` slices (so the fake-clock tests can drive it) and checks both
    ``stop_event`` (per-worker stop) and ``should_stop`` (global shutdown)
    before and after every slice, bounding stop/shutdown latency to one
    ``worker_idle_poll_interval``. The legacy branch instead uses a single
    interruptible ``stop_event.wait`` (instant wake on stop), exactly matching
    pre-phase-2 behaviour; its stop latency is bounded by that one coarse wait.

    The deadline tracks *continuous* emptiness: it is set once on entry and is
    only reset by the caller re-entering after a genuine task. Losing a claim
    race (``run_one`` returns ``None`` after a positive ``pending_count``) does
    not reset it, so two idle workers ping-ponging empty queues cannot keep each
    other alive forever.

    ``admission_open`` is the memory gate. On the fine-cadence path a closed
    gate skips the claim and keeps polling against the *same* deadline, so a
    worker parked on a squeezed host ages out at ``worker_idle_timeout`` and
    exits rather than holding a slot open indefinitely — under sustained
    pressure the pool drains, and ``dispatch`` declines to refill it. Checked
    ahead of ``pending_count`` because a closed gate should cost nothing at all,
    not even the indexed read. On the legacy branch below there is no polling to
    continue, so a closed gate returns ``None`` and the worker exits after that
    single recheck; that is the same exit this branch already takes when
    ``run_one`` finds nothing, so parity is preserved. Defaults to open, leaving
    every existing caller unchanged.
    """
    idle_poll = config.scheduler.worker_idle_poll_interval
    idle_timeout = config.scheduler.worker_idle_timeout
    poll_interval = config.scheduler.poll_interval

    # Legacy parity: a single coarse, interruptible wait + single recheck —
    # exactly the pre-phase-2 behaviour, including instant wake on stop (the
    # wait keys on stop_event, which pool.shutdown sets via request_stop). Opt
    # back in with worker_idle_poll_interval <= 0 or >= worker_idle_timeout.
    if idle_poll <= 0 or idle_poll >= idle_timeout:
        if stop_event.wait(timeout=min(poll_interval, idle_timeout)):
            return None  # per-worker stop / global shutdown
        if should_stop():
            return None
        if not admission_open():
            return None
        return run_one()

    deadline = time.monotonic() + idle_timeout
    while not should_stop() and not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(idle_poll, remaining))
        if should_stop() or stop_event.is_set():
            return None
        # Memory gate first: a closed gate must cost nothing, not even the
        # indexed read below. Keeps the same deadline, so a worker parked on a
        # squeezed host ages out instead of holding its slot open.
        if not admission_open():
            continue
        # Cheap pre-check before the expensive claim. pending_count is a
        # claimability-aware indexed read (the same count dispatch() uses);
        # process_one_task -> claim_task additionally executes stale-lock /
        # stuck-task maintenance UPDATEs on every call, so only pay that when
        # there is plausibly something we can actually claim. Because the count
        # mirrors claim_task's per-channel gate, a follow-up parked behind an
        # active task in this room reads as 0 here — we keep sleeping cheaply
        # instead of busy-polling claim_task until the gate clears.
        try:
            if pending_count() == 0:
                continue
        except Exception:  # noqa: BLE001
            # A transient SQLite/FUSE read failure must not kill the worker
            # mid-idle; fall through to run_one (which has its own error
            # handling) rather than skip a possibly-present task.
            pass
        result = run_one()
        if result is not None:
            return result
        # Lost the race to dispatch()/another worker, or nothing after all —
        # keep polling against the SAME deadline (the queue is still empty).
    return None


class UserWorker(threading.Thread):
    """Worker thread that processes tasks for a single user and queue serially."""

    def __init__(self, user_id: str, config: Config, pool: "WorkerPool",
                 queue_type: str = "foreground", slot: int = 0):
        super().__init__(daemon=True, name=f"worker-{user_id}-{queue_type}-{slot}")
        self.user_id = user_id
        self.queue_type = queue_type
        self.slot = slot
        self.config = config
        self.pool = pool
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("Worker started for user %s (%s)", self.user_id, self.queue_type)
        try:
            while not _shutdown_requested and not self._stop_event.is_set():
                # The memory gate covers this fast path too, not just
                # dispatch(). A worker that has already claimed one task loops
                # straight back here, so gating only the spawn would let a
                # squeezed host keep starting work in the slots it already has
                # — and the incident's trigger was one task running a test
                # suite, which is exactly what that lets through. When the gate
                # is shut we fall through to the idle wait, which parks cheaply
                # and eventually exits, so the pool drains under pressure.
                if self.pool.admission_open():
                    try:
                        result = process_one_task(
                            self.config, user_id=self.user_id, queue=self.queue_type,
                        )
                    except Exception as e:
                        logger.error("Worker %s/%s error: %s", self.user_id, self.queue_type, e)
                        result = None

                    if result is not None:
                        task_id, success = result
                        status = "completed" if success else "failed"
                        logger.info(
                            "Worker %s/%s: task %d %s",
                            self.user_id, self.queue_type, task_id, status,
                        )
                        # Processed a task — immediately check for more
                        continue

                # No tasks available — linger, re-checking on a fine cadence
                # until a new task arrives (claimed within ~one idle poll) or the
                # cumulative idle timeout elapses. dispatch() may scan the same
                # user concurrently while we linger; that overlap is harmless —
                # claim_task is atomic (UPDATE ... RETURNING), so at most one of
                # us wins and the loser simply gets None.
                idle_result = _worker_idle_wait(
                    self.user_id, self.queue_type, self.config,
                    self._stop_event, lambda: _shutdown_requested,
                    run_one=lambda: process_one_task(
                        self.config, user_id=self.user_id, queue=self.queue_type,
                    ),
                    pending_count=lambda: _count_pending(
                        self.config, self.user_id, self.queue_type,
                    ),
                    admission_open=self.pool.admission_open,
                )
                if idle_result is not None:
                    task_id, success = idle_result
                    status = "completed" if success else "failed"
                    logger.info(
                        "Worker %s/%s: task %d %s",
                        self.user_id, self.queue_type, task_id, status,
                    )
                    continue

                # Idle timeout reached — exit; dispatch() re-spawns a worker on
                # the next pending task for this user (phase-1 sub-tick cadence).
                break
        finally:
            logger.info("Worker exiting for user %s (%s/%d)", self.user_id, self.queue_type, self.slot)
            self.pool._on_worker_exit(self.user_id, self.queue_type, self.slot)

    def request_stop(self) -> None:
        self._stop_event.set()


@dataclass(frozen=True)
class ForegroundSlotPlan:
    """What one user may hold this tick, after long tasks are discounted."""

    interactive: int   # threads still counting against the interactive cap
    may_spawn: int     # new workers this user may get on this tick
    slot_range: int    # width of the slot index space to draw from


def plan_foreground_slots(
    *,
    threads: int,
    discounted: int,
    pending: int,
    user_fg_cap: int,
    user_max_long_workers: int,
) -> ForegroundSlotPlan:
    """Per-user slot arithmetic for elapsed-time reclassification (spec C1).

    The unit of accounting in the pool is a *thread*, not a task: `UserWorker`
    claims serially and lingers when idle, and `_workers` is keyed by slot
    index. With the cap at 2 and both slots held there was no free index, which
    is what blocked a short question behind a forty-minute test run.

    `discounted` is how many of this user's threads are excused from the
    interactive cap, already bounded by both the per-user and the instance-wide
    allowance (see `allocate_long_discounts`). The long allowance is additive
    per user, so the thread ceiling is `user_fg_cap + user_max_long_workers`.

    The `max(0, ...)` is required rather than defensive: the counts are read
    before `WorkerPool._lock` is taken, so a long task can complete in between
    and leave `discounted` momentarily larger than `threads`. Without it that
    reads as negative occupancy and the caller's `range` arithmetic goes with
    it.

    What it does *not* do, and what no clamp here can do: `discounted` derives
    from `running` task rows while `threads` counts pool worker threads, and
    the two come apart whenever a row outlives its worker — the window before
    the stuck-worker liveness reclaim fires (roughly the same ten minutes as
    the long threshold), or a second process draining the queue with no pool at
    all. Such a row excuses occupancy that no thread is holding, and the user
    gets one extra thread on ordinary interactive work. Clamping `discounted`
    to `threads` looks like the fix and is arithmetically inert — it changes
    `interactive` in exactly no case that `max(0, ...)` does not already cover,
    and it cannot tell a ghost row from a real one anyway. The over-grant is
    instead bounded by the two ceilings that do hold unconditionally: the
    additive per-user ceiling below, and `max_foreground_workers` in the
    caller. It clears itself when the reclaim clears the row.

    Pure. No config, no DB, no lock — the whole point is that the rule can be
    read and tested without a pool around it.
    """
    interactive = max(0, threads - discounted)
    slot_range = user_fg_cap + user_max_long_workers
    may_spawn = min(
        user_fg_cap - interactive,
        slot_range - threads,
        pending,
    )
    return ForegroundSlotPlan(
        interactive=interactive,
        may_spawn=max(0, may_spawn),
        slot_range=slot_range,
    )


def allocate_long_discounts(
    long_by_user: Mapping[str, int],
    *,
    priority: Sequence[str],
    user_cap: int,
    instance_cap: int,
) -> dict[str, int]:
    """Hand out at most `instance_cap` discounts, `user_cap` to any one user.

    Per user the long allowance is additive — 2 interactive plus 1 long — so
    one person's long job cannot consume their own interactivity. Instance-wide
    it is partitioned instead: `max_foreground_workers` stays the hard ceiling
    on foreground threads and this bounds how many of them may be discounted.
    Making both additive would raise the instance ceiling from 5 threads to 7
    and grow the box's worst-case memory exposure, which is the failure this
    whole feature exists to bound — the head-of-line block is a fairness
    problem, not a throughput one.

    `long_by_user` must already be filtered to users a discount would actually
    unblock — those at or over their interactive cap. Dispatch does that
    filtering because it is the thing holding the thread counts. Skipping it
    starves the user the feature exists for: `priority` is oldest-pending-first
    and does not correlate with "at cap", so a user with a free interactive
    slot — who was going to spawn anyway, discount or no discount — can take
    the last of the budget and leave the genuinely blocked user refused.

    `priority` is dispatch's own user order (longest-waiting first), so when the
    budget is still scarce among eligible users it goes to whoever has waited
    longest. Users holding a long task but nothing pending never reach
    dispatch's loop, yet still hold the extra thread, so they are allocated
    after the priority list rather than skipped — otherwise the budget
    under-counts exactly the threads it exists to bound. Sorted, so the
    allocation is deterministic under xdist and across ticks.

    Users granted nothing are omitted rather than mapped to 0, so the result
    reads the same as an empty allocation when the feature is off.
    """
    if user_cap <= 0 or instance_cap <= 0:
        return {}
    seen = list(priority) + sorted(set(long_by_user) - set(priority))
    granted: dict[str, int] = {}
    remaining = instance_cap
    for user_id in seen:
        if remaining <= 0:
            break
        long_tasks = long_by_user.get(user_id, 0)
        if long_tasks <= 0:
            continue
        take = min(long_tasks, user_cap, remaining)
        granted[user_id] = take
        remaining -= take
    return granted


class WorkerPool:
    """Manages per-user, per-queue worker threads with a concurrency cap.

    Each user can have multiple concurrent workers per queue type, up to their
    per-user cap. Workers are keyed by (user_id, queue_type, slot).
    """

    def __init__(self, config: Config):
        self.config = config
        self._workers: dict[tuple[str, str, int], UserWorker] = {}
        self._lock = threading.Lock()
        # Latest host memory reading, pushed in by the main loop's sampler.
        # ``None`` — the startup state, and the state on any host where
        # /proc/meminfo does not parse — means no information, which the gate
        # reads as open. See _admission_open.
        self._pressure_sample: host_pressure_mod.PressureSample | None = None
        self._pressure_lock = threading.Lock()
        # Monotonic clock of the first tick of the current closed stretch, and
        # of the last line logged about it. Both reset when the gate reopens,
        # so a fresh squeeze is reported as a new event rather than being
        # swallowed by the previous one's cooldown.
        self._gate_closed_since: float | None = None
        self._last_gate_log = 0.0

    def update_pressure(
        self, sample: "host_pressure_mod.PressureSample | None"
    ) -> None:
        """Hand the pool the latest host reading (main loop → gate).

        ``None`` clears rather than preserves. A sampler that starts failing
        must not leave the last bad reading latched: the gate would then stay
        shut on evidence that is no longer being refreshed, which is the
        indefinite-outage failure this whole spec exists to prevent.
        """
        with self._pressure_lock:
            self._pressure_sample = sample

    def gate_closed_seconds(self) -> float:
        """How long admission has been continuously closed. ``0.0`` when open.

        Read by the alerting path to decide how to word a notification: a gate
        that has been shut for longer than the cooldown while istota itself is
        running nothing means the pressure is coming from elsewhere on the box,
        and istota is the victim rather than the cause.

        Takes no ``now``. ``_gate_closed_since`` is a ``time.monotonic()``
        stamp while every other ``now`` in this file is ``time.time()``, so a
        parameter here would exist mainly to be handed the wrong clock — and
        the failure is silent, since a wall-clock value yields a difference of
        about 1.7e9 and makes every alert claim istota is a bystander.
        """
        with self._pressure_lock:
            closed_since = self._gate_closed_since
        if closed_since is None:
            return 0.0
        return max(0.0, time.monotonic() - closed_since)

    def _admission_decision(
        self,
    ) -> "tuple[bool, host_pressure_mod.PressureSample | None]":
        """Is there room on this host to *start* more work, and on what reading?

        Never about stopping work. Already-running tasks are not consulted, not
        counted and not touched; the gate refuses a *start* and the pending row
        waits, exactly as it does when a cap is full.

        Fails open in every uncertain case — feature disabled, both thresholds
        zero, no sample yet, sample unreadable. A broken sampler must not be
        able to halt work: an unexplained total outage is a far worse failure
        than one task admitted onto a busy box, and it is the failure this
        instrumentation was added to explain rather than to cause.

        Pure. The sample comes back so the dispatch-side caller can log what it
        decided on, without this having to know whether anyone wants that.
        """
        if not self.config.scheduler.host_pressure_enabled:
            return True, None

        with self._pressure_lock:
            sample = self._pressure_sample
        if sample is None:
            return True, None

        psi_threshold = self.config.scheduler.host_pressure_psi_threshold
        min_available_mb = self.config.scheduler.min_available_memory_mb
        # A zero floor with no PSI threshold would make the gate unreachable;
        # keep the switch explicit rather than implied by the arithmetic.
        if min_available_mb <= 0 and psi_threshold <= 0:
            return True, sample

        under = host_pressure_mod.is_under_pressure(
            sample,
            psi_threshold=psi_threshold if psi_threshold > 0 else float("inf"),
            min_available_mb=min_available_mb,
        )
        return (not under), sample

    def admission_open(self) -> bool:
        """The gate, with no bookkeeping. What a *worker* asks before claiming.

        Gating :meth:`dispatch` alone bounds new worker *threads*, not new task
        *starts*. A worker already alive re-claims on its own — the fast path in
        ``UserWorker.run`` and the poll inside ``_worker_idle_wait`` — so before
        this existed a squeezed host kept starting work in whatever slots it
        already had, while both this spec's C2 text and the scheduler rules said
        the task would stay ``pending``. The incident's own trigger was a single
        task running a test suite, so one claim into a lingering worker is the
        whole failure; bounding concurrency growth was never enough on its own.

        Deliberately free of the closed-since clock and the cooldown-limited log
        line that :meth:`_admission_open` maintains. Workers poll this on the
        idle cadence — several times a second, per worker — and a version that
        stamped or logged would flood the log and keep re-arming state that
        belongs to dispatch's once-per-tick view.
        """
        return self._admission_decision()[0]

    def _admission_open(self) -> bool:
        """:meth:`admission_open` plus the closed-since clock and the log line.

        Dispatch's form, called once per tick. The lock is released before
        either _note_gate_* helper, which take it themselves: ``threading.Lock``
        is not reentrant, so folding those calls into a ``with`` block — the
        obvious tidy-up — deadlocks the loop thread outright.
        """
        is_open, sample = self._admission_decision()
        if is_open or sample is None:
            self._note_gate_open()
            return True
        self._note_gate_closed(sample)
        return False

    def _note_gate_open(self) -> None:
        with self._pressure_lock:
            self._gate_closed_since = None

    def _note_gate_closed(self, sample: "host_pressure_mod.PressureSample") -> None:
        """Mark the gate shut and log at most one line per cooldown window."""
        now = time.monotonic()
        with self._pressure_lock:
            first_closed_tick = self._gate_closed_since is None
            if first_closed_tick:
                self._gate_closed_since = now
            cooldown = self.config.scheduler.host_pressure_alert_cooldown_seconds
            due = first_closed_tick or (now - self._last_gate_log >= cooldown)
            if due:
                self._last_gate_log = now
        if not due:
            return
        logger.warning(
            "dispatch_admission_closed mem_available_mb=%d psi_mem_some_avg10=%s "
            "swap_total_kb=%d — holding pending tasks, nothing running is affected",
            sample.mem_available_kb // 1024,
            "?" if sample.psi_mem_some_avg10 is None else f"{sample.psi_mem_some_avg10:.2f}",
            sample.swap_total_kb,
        )

    def dispatch(self) -> None:
        """Spawn workers for users with pending tasks, prioritizing foreground.

        Concurrency control, in the order it binds:
        1. Instance-level fg cap: max_foreground_workers — a *hard* ceiling on
           foreground threads, which the long allowance below never raises
        2. Instance-level bg cap: max_background_workers
        3. Per-user caps: effective_user_max_fg_workers / effective_user_max_bg_workers
        4. Per-user long allowance (C1, foreground only): a running task past
           `long_task_threshold_minutes` stops counting against (3), which
           raises that user's thread ceiling to `user_fg_cap +
           user_max_long_workers`. `max_long_workers` is the instance-wide
           budget of such discounts — partitioned inside (1), not added to it.
           See `plan_foreground_slots` / `allocate_long_discounts`.

        Ahead of all of them sits the memory admission gate (C2): below the
        floor, this tick spawns nothing at all and the pending rows wait. The
        check is first so a squeezed host does not even pay for the DB scan.
        """
        if not self._admission_open():
            return

        long_threshold = self.config.scheduler.long_task_threshold_minutes
        user_max_long = self.config.scheduler.user_max_long_workers
        # Any of the three at 0 is the documented off switch, and all three have
        # to be checked here rather than only in the allocator: with the
        # instance budget at 0 the query would still run every ~0.5s and have
        # its result discarded, which is a cost the "0 disables" wording does
        # not promise.
        long_enabled = (
            long_threshold > 0
            and user_max_long > 0
            and self.config.scheduler.max_long_workers > 0
        )

        # Short busy_timeout: this scan is pure reads, so a DB locked past the
        # budget means "skip this dispatch tick" (dispatch runs again in ~0.5s)
        # rather than blocking the main loop for 30s and tripping the watchdog.
        timeout_ms = self.config.scheduler.main_loop_read_timeout_ms or None
        try:
            with db.get_db(self.config.db_path, busy_timeout_ms=timeout_ms) as conn:
                fg_users = db.get_users_with_pending_fg_queue_tasks(conn)
                bg_users = db.get_users_with_pending_bg_queue_tasks(conn)
                # Pre-fetch *claimable* task counts for users that may need multiple
                # workers. Claimable (not raw pending) so a follow-up gated behind an
                # active task in the same room counts as 0 — dispatch won't spawn a
                # doomed extra worker that can only busy-poll claim_task until the
                # gate clears. A task in a different, ungated room still counts, so
                # legitimate parallelism is unaffected.
                fg_pending = {uid: db.count_claimable_tasks_for_user_queue(conn, uid, "foreground") for uid in fg_users}
                bg_pending = {uid: db.count_claimable_tasks_for_user_queue(conn, uid, "background") for uid in bg_users}
                # One extra grouped query, joining the same pre-lock scan: the
                # per-user count of foreground tasks that have demonstrated
                # they are not interactive (C1). Skipped entirely when the
                # feature is off, so a deployment that does not want it does
                # not pay for it on every ~0.5s tick.
                fg_long = (
                    db.count_long_running_tasks_by_user(
                        conn, "foreground", long_threshold
                    )
                    if long_enabled
                    else {}
                )
        except sqlite3.OperationalError as exc:
            logger.warning("dispatch_scan_db_locked err=%s (skipping tick)", exc)
            return

        fg_cap = self.config.scheduler.max_foreground_workers
        bg_cap = self.config.scheduler.max_background_workers

        with self._lock:
            # Phase 1: foreground workers
            active_fg = sum(1 for (_, qt, _) in self._workers if qt == "foreground")
            fg_threads: dict[str, int] = {}
            for (uid, qt, _) in self._workers:
                if qt == "foreground":
                    fg_threads[uid] = fg_threads.get(uid, 0) + 1
            # Only users already at or over their interactive cap can be
            # unblocked by a discount; anyone below it spawns on the ordinary
            # cap regardless. Charging them the budget anyway is how the user
            # this feature exists for — the one at cap with a question waiting
            # — gets refused while someone who needed nothing takes the last
            # of it. Allocated over every eligible user holding a long task,
            # not only those with pending work: a user with an extra thread
            # and an empty queue still occupies the exposure being bounded.
            eligible_long = {
                uid: n
                for uid, n in fg_long.items()
                if fg_threads.get(uid, 0)
                >= self.config.effective_user_max_fg_workers(uid)
            }
            discounts = allocate_long_discounts(
                eligible_long,
                priority=fg_users,
                user_cap=user_max_long,
                instance_cap=self.config.scheduler.max_long_workers,
            )
            self._retire_surplus_foreground_workers(fg_threads, discounts)
            for user_id in fg_users:
                # `fg_cap` stays the hard instance ceiling on foreground
                # threads. The long allowance is additive per user and
                # partitioned here, so a discount never buys a thread past it.
                if active_fg >= fg_cap:
                    break
                user_fg_cap = self.config.effective_user_max_fg_workers(user_id)
                existing_slots = {s for (uid, qt, s) in self._workers if uid == user_id and qt == "foreground"}
                plan = plan_foreground_slots(
                    threads=len(existing_slots),
                    discounted=discounts.get(user_id, 0),
                    pending=fg_pending.get(user_id, 0),
                    user_fg_cap=user_fg_cap,
                    user_max_long_workers=user_max_long,
                )
                to_spawn = plan.may_spawn
                user_discount = discounts.get(user_id, 0)
                threads_now = len(existing_slots)
                available = (s for s in range(plan.slot_range) if s not in existing_slots)
                for slot in available:
                    if to_spawn <= 0 or active_fg >= fg_cap:
                        break
                    key = (user_id, "foreground", slot)
                    worker = UserWorker(user_id, self.config, self, queue_type="foreground", slot=slot)
                    self._workers[key] = worker
                    worker.start()
                    threads_now += 1
                    # `threads` is the running total *after* this spawn, not the
                    # plan-time figure: two spawns in one tick would otherwise
                    # log the same number twice and a reader reconstructing
                    # occupancy from the log would undercount. This line is the
                    # only observability the allowance ships.
                    logger.info(
                        "Spawned foreground worker for user %s (slot %d, threads=%d "
                        "interactive=%d long_discount=%d)",
                        user_id, slot, threads_now,
                        max(0, threads_now - user_discount),
                        user_discount,
                    )
                    active_fg += 1
                    to_spawn -= 1

            # Phase 2: background workers
            active_bg = sum(1 for (_, qt, _) in self._workers if qt == "background")
            for user_id in bg_users:
                if active_bg >= bg_cap:
                    break
                user_bg_cap = self.config.effective_user_max_bg_workers(user_id)
                existing_slots = {s for (uid, qt, s) in self._workers if uid == user_id and qt == "background"}
                user_bg_active = len(existing_slots)
                pending = bg_pending.get(user_id, 0)
                to_spawn = min(user_bg_cap - user_bg_active, pending)
                available = (s for s in range(user_bg_cap) if s not in existing_slots)
                for slot in available:
                    if to_spawn <= 0 or active_bg >= bg_cap:
                        break
                    key = (user_id, "background", slot)
                    worker = UserWorker(user_id, self.config, self, queue_type="background", slot=slot)
                    self._workers[key] = worker
                    worker.start()
                    logger.info("Spawned background worker for user %s (slot %d)", user_id, slot)
                    active_bg += 1
                    to_spawn -= 1

    def _retire_surplus_foreground_workers(
        self, fg_threads: "dict[str, int]", discounts: "dict[str, int]"
    ) -> None:
        """Ask workers held up only by a lapsed discount to finish and exit.

        Without this the long allowance is granted once and never taken back.
        `dispatch` only ever *adds* threads, and a worker re-claims on its own
        without rechecking its slot, so the moment a long task ended while its
        user still had backlog the extra thread carried on serving ordinary
        interactive work — turning the documented "2 interactive + 1 long" into
        "3 interactive" for as long as the queue stayed non-empty. The instance
        ceiling still held, so this was never box exposure; it was the per-user
        cap quietly not meaning what it says.

        Also covers the pre-existing case of a cap lowered under a live pool,
        which stranded a worker at an index outside the new range.

        `request_stop` is graceful: `UserWorker.run` checks the stop event at
        the top of its loop, so a worker mid-task finishes that task and then
        exits. Nothing is preempted, killed or migrated — the same rule the
        long task itself is held to. Highest slot index first, so the retired
        thread is the one the allowance added rather than an original.

        Called with `self._lock` held; it only reads `_workers` and calls
        `request_stop`, which sets an event and returns.
        """
        for user_id, threads in fg_threads.items():
            user_fg_cap = self.config.effective_user_max_fg_workers(user_id)
            interactive = max(0, threads - discounts.get(user_id, 0))
            surplus = interactive - user_fg_cap
            if surplus <= 0:
                continue
            slots = sorted(
                (s for (uid, qt, s) in self._workers
                 if uid == user_id and qt == "foreground"),
                reverse=True,
            )
            for slot in slots[:surplus]:
                worker = self._workers.get((user_id, "foreground", slot))
                if worker is None:
                    continue
                # Already asked on an earlier tick — it is finishing its task.
                # Re-asking is harmless but would re-log every ~0.5s until it
                # exits, which is the wrong shape for a line an operator reads.
                stop_event = getattr(worker, "_stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    continue
                worker.request_stop()
                logger.info(
                    "Retiring surplus foreground worker for user %s (slot %d, "
                    "interactive=%d cap=%d) — long allowance lapsed",
                    user_id, slot, interactive, user_fg_cap,
                )

    def _on_worker_exit(self, user_id: str, queue_type: str, slot: int) -> None:
        """Called by a worker thread when it exits."""
        with self._lock:
            self._workers.pop((user_id, queue_type, slot), None)

    def shutdown(self) -> None:
        """Request all workers to stop and wait for them to finish."""
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.request_stop()
        for w in workers:
            w.join(timeout=10)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._workers)


def _talk_target_for_delivery(config: Config, task: db.Task) -> str | None:
    """Back-compat shim over ``routing.talk_channel_for_task``.

    The resolution moved to the transport layer, where the rest of delivery
    routing lives and where the room registry is already in scope. The name
    survives because the event consumers, the Talk transport's
    ``resolve_target`` and a good deal of introspection-shaped test coverage
    call it — see the "delivery shims stay" note in `.claude/rules/transport.md`
    for the same argument about `post_result_to_talk`.
    """
    from .transport.routing import talk_channel_for_task
    return talk_channel_for_task(config, task)


# `_notify_confirmed_email_result` is gone (ISSUE-247). It posted the bot's
# reply to a gated email task as an `Email reply sent to <sender> (task #N):`
# notification, because there was no turn in any room to attach it to — the
# helper existed only to compensate for `_store_room_turn` finding no room under
# a thread hash. The answer is now an ordinary assistant turn in the room the
# exchange was routed to, with the question above it, so the wrapper is a second
# rendering of a message the room already holds. Its other branch — announcing a
# reply the outbound gate held rather than sent — is not lost: every hold on the
# delivery leg already announces itself through
# `transport.email.outbound._announce_hold`, which names the draft id and the
# `!drafts` verbs, and does it for *every* email task rather than only a gated
# one.


def _deliver_deferred_email_output(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> None:
    """Deliver or clean up deferred email output files not handled by the normal path.

    The normal email delivery path (post_result_to_email via `post_email` flag)
    handles tasks whose output_target includes an "email" leg. This
    function handles two gap cases:

    1. source_type="email" but output_target doesn't include email (e.g. an
       emissary reply routed to Talk) — deliver via post_result_to_email,
       which will find the processed_email record and reply correctly.
    2. Non-email source (e.g. Talk user who asked the agent to email someone)
       where the agent used `email output` instead of `email send` — warn and
       delete, because there's no processed_email record and the scheduler
       would send to the wrong recipient.
    """
    from .transport import parse_output_target
    if plan_has_surface(parse_output_target(task.output_target), "email"):
        # Normal path will deliver via post_email flag — nothing to do here.
        return
    path = user_temp_dir / f"task_{task.id}_email_output.json"
    if not path.exists():
        return

    if task.source_type == "email":
        # Email-sourced task with non-email output_target (e.g. emissary reply
        # with output_target="talk"). The processed_email record exists, so
        # post_result_to_email can reply correctly.
        logger.info(
            "Delivering deferred email output for task %d (source=%s, output_target=%s)",
            task.id, task.source_type, task.output_target,
        )
        email_ok = asyncio.run(post_result_to_email(config, task, ""))
        if not email_ok:
            logger.error(
                "Failed to deliver deferred email output for task %d", task.id,
            )
    else:
        # Non-email source — agent used `email output` instead of `email send`.
        # No processed_email record, so we can't deliver to the right recipient.
        logger.warning(
            "Orphaned deferred email output file for task %d (source=%s): "
            "Claude used `email output` instead of `email send`. "
            "The email was NOT delivered. Removing file.",
            task.id, task.source_type,
        )
        path.unlink(missing_ok=True)


def _purge_obsolete_skill_jobs(conn, skill_index: dict) -> None:
    """Delete scheduled_jobs rows AND fail pending tasks rows whose
    ``skill`` field no longer exists in the skill index.

    Symmetric with cron_loader's CRON.md-orphan deletion, but for
    auto-seeded skill-task rows. The seeders re-create scheduled_jobs
    rows for skills that still exist on the next tick. Pending tasks
    rows are marked failed (not deleted) so audit / delivery state is
    preserved; rename is operator-driven and rare.
    """
    cur = conn.execute(
        "SELECT id, name, user_id, skill FROM scheduled_jobs "
        "WHERE skill IS NOT NULL"
    )
    for row in cur.fetchall():
        skill = row["skill"] if hasattr(row, "keys") else row[3]
        if skill not in skill_index:
            logger.warning(
                "Removing obsolete skill scheduled_job '%s' user=%s skill=%s "
                "(skill no longer exists in index)",
                row["name"], row["user_id"], skill,
            )
            conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (row["id"],))

    cur = conn.execute(
        "SELECT id, user_id, skill FROM tasks "
        "WHERE skill IS NOT NULL AND status IN ('pending', 'locked')"
    )
    for row in cur.fetchall():
        skill = row["skill"] if hasattr(row, "keys") else row[2]
        if skill not in skill_index:
            logger.warning(
                "Failing pending skill task #%d user=%s skill=%s "
                "(skill no longer exists in index)",
                row["id"], row["user_id"], skill,
            )
            conn.execute(
                "UPDATE tasks SET status='failed', error=?, "
                "completed_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id = ?",
                (f"unknown skill: {skill}", row["id"]),
            )
    conn.commit()


def _run_garmin_sync_inprocess(
    task: db.Task, config: Config, skill_args: list[str],
) -> tuple[bool, str]:
    """Run ``health garmin-sync`` in the daemon thread.

    The garmin engine reads + writes encrypted secrets (oauth blob,
    rotated SDK tokens, error flag, last_sync). The subprocess path
    strips ``ISTOTA_SECRET_KEY`` by design, so the engine can neither
    decrypt the stored tokens nor persist mid-run refreshes from there.
    The web ``/garmin/sync`` endpoint already runs the same engine
    in-process; this is the cron-driven equivalent.
    """
    from istota.health import garmin_sync as gs
    from istota.health import resolve_for_user
    from istota.health._loader import UserNotFoundError

    days_back = 2
    tail = skill_args[1:]
    for i, arg in enumerate(tail):
        if arg == "--days-back" and i + 1 < len(tail):
            try:
                days_back = max(1, int(tail[i + 1]))
            except (TypeError, ValueError):
                pass
            break

    if config.db_path is None:
        return False, "garmin sync: framework db_path unavailable"

    try:
        ctx = resolve_for_user(task.user_id, config)
    except UserNotFoundError as exc:
        return False, f"garmin sync: {exc}"

    # Live DB timezone so the "yesterday" window tracks travel (ISSUE-099);
    # "UTC" is the engine's effective default, same as the old None.
    user_tz = config.resolve_user_timezone(task.user_id)

    try:
        res = gs.sync_garmin(
            ctx, Path(config.db_path),
            days_back=days_back, user_tz=user_tz,
            # Daemon-side, so an auth failure can raise (and deliver) the
            # reconnect notification. This is the caller that most needs it: the
            # module job removes itself once the tokens are gone, so this run is
            # the last one that will ever notice.
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("garmin sync (in-process) raised for user=%s", task.user_id)
        return False, f"garmin sync: {exc}"

    payload = res.to_dict()
    payload["status"] = "error" if res.auth_error else "ok"
    if res.auth_error:
        payload["error"] = "token_expired"
    result_text = json.dumps(payload)
    return (not res.auth_error), result_text


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL a subprocess and every descendant in its process group."""
    # Every caller here spawns with start_new_session=True, so the child leads
    # its own group and the shared helper takes the group path; the
    # single-process fallback covers a group that is already gone.
    kill_process_group(proc.pid)


def _run_capture(
    cmd, *, timeout: float, cwd: str, env: dict,
) -> subprocess.CompletedProcess:
    """Run a subprocess capturing stdout/stderr, killing the whole process
    *group* on timeout.

    ``subprocess.run(timeout=…)`` only SIGKILLs the direct child, then calls
    ``communicate()`` again to reap it — which blocks indefinitely when an
    orphaned grandchild inherited the stdout/stderr pipe. A CRON ``command:``
    that backgrounds a child (or a skill CLI that shells out) therefore wedges
    its worker past the timeout, and because the per-task heartbeat thread keeps
    pinging, the stuck-running reaper never reclaims it — the location-alert task
    held its only background slot for 6+ hours that way. ``start_new_session``
    puts the child in its own process group so the timeout can ``os.killpg`` the
    whole tree, releasing the pipe. Re-raises ``TimeoutExpired`` so callers
    handle the deadline exactly as before; otherwise returns a CompletedProcess
    so call sites keep using ``.returncode`` / ``.stdout`` / ``.stderr``.
    """
    # Always an argv list, never `shell=True`. A caller wanting a shell builds
    # one with `shell_exec.shell_argv`, which sets `pipefail`; reintroducing
    # `shell=True` here would quietly reintroduce the status bug it fixes.
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # The group is dead now, so this drains the pipes and reaps without
        # blocking; bound it anyway so a pathological case can't hang the worker.
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _skill_error_envelope(stdout: str | None) -> str | None:
    """The error a skill CLI reported in its stdout envelope, or None.

    Skill CLIs print `{"status": "error", "error": "…"}` on stdout and write
    nothing to stderr, so both exit paths below have to read stdout to find out
    what went wrong. Shared between them so they cannot disagree about what
    counts as a reported failure.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("status") != "error":
        return None
    return str(parsed.get("error") or "skill reported status=error")


def _execute_skill_task(
    task: db.Task, config: Config,
) -> tuple[bool, str]:
    """Execute an auto-seeded skill-task in a single subprocess.

    Phase 1.3 of the unified credential resolution refactor: cron-driven
    `_module.<name>.*` jobs run as ``istota.skills.<skill>`` subprocesses
    with credentials pre-resolved via :func:`build_skill_env` on the
    trusted side. The master Fernet key never leaves the daemon.

    Skill-tasks are not arbitrary shell, so they are not admin-gated.
    """
    from .executor import build_clean_env, get_user_temp_dir
    from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
    from .skills._loader import load_skill_index

    skill_name = task.skill or ""
    try:
        skill_args = json.loads(task.skill_args or "[]")
    except (json.JSONDecodeError, ValueError):
        return False, f"invalid skill_args JSON: {task.skill_args!r}"
    if not isinstance(skill_args, list) or not all(
        isinstance(a, str) for a in skill_args
    ):
        return False, "skill_args must be a JSON list of strings"

    skill_index = load_skill_index(
        config.skills_dir, config.bundled_skills_dir,
    )
    if skill_name not in skill_index:
        return False, f"unknown skill: {skill_name}"

    # In-process dispatch for skill-tasks that need read/write access to
    # the encrypted secrets store. The subprocess path strips
    # ``ISTOTA_SECRET_KEY`` by design (executor.build_clean_env), so
    # any skill that reaches into ``secrets_store`` directly — and the
    # garmin sync engine reads + writes multiple entries (oauth blob,
    # rotated tokens, error flag, last_sync) — cannot run there. Mirrors
    # the in-process call the web ``/garmin/sync`` endpoint already makes.
    if skill_name == "health" and skill_args[:1] == ["garmin-sync"]:
        return _run_garmin_sync_inprocess(task, config, skill_args)

    timeout = config.scheduler.task_timeout_minutes * 60
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)
    user_cfg = config.get_user(task.user_id)

    ctx = EnvContext(
        config=config,
        task=task,
        user_resources=user_resources,
        user_config=user_cfg,
        user_temp_dir=user_temp_dir,
        is_admin=config.is_admin(task.user_id),
        discovered_calendars=discover_calendars_for_task(task, config),
    )

    env = build_clean_env(config)
    env["ISTOTA_TASK_ID"] = str(task.id)
    # Set wherever the id is (ISSUE-377). Neither of the scheduler's own paths
    # writes a session log, but a skill CLI keying off the id has to find the
    # attempt beside it or fail closed, and "the paths agree" is cheaper to
    # hold than "these two are the exceptions".
    env["ISTOTA_TASK_ATTEMPT"] = str(task.attempt_count + 1)
    env["ISTOTA_USER_ID"] = task.user_id
    env["ISTOTA_DEFERRED_DIR"] = str(user_temp_dir)
    env["ISTOTA_EXPERIMENTAL_FEATURES"] = ",".join(config.experimental.features)
    if config.config_path:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)
    if config.nextcloud_mount_path:
        env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path)
    if task.conversation_token:
        env["ISTOTA_CONVERSATION_TOKEN"] = task.conversation_token

    # Resolve declarative env from the full skill_index — co-declared
    # vars (e.g. NC_URL on both ``files`` and ``nextcloud``) must reach
    # the subprocess regardless of which skill the task names. No proxy
    # split: skill-tasks run a trusted CLI, not an LLM, so credentials
    # flow directly. ``build_skill_env`` warns on real value conflicts.
    env.update(build_skill_env(list(skill_index), skill_index, ctx))

    # Run setup_env hooks (C1). Without this, declarative env specs
    # marked ``from: "setup_env"`` (notably ``HEALTH_DB_PATH``) resolve
    # to None in build_skill_env — the health skill's scheduled
    # Garmin sync would fail every cron tick with "HEALTH_DB_PATH not
    # set". Hooks self-gate, so dispatching the full skill_index is
    # safe.
    env.update(dispatch_setup_env_hooks(list(skill_index), skill_index, ctx))

    # Per-user timezone (used by sync engines like Garmin that need to
    # compute the user's "yesterday" in their local TZ rather than UTC).
    # Resolved from the live user_profiles DB row so it tracks travel
    # without a daemon restart (ISSUE-099).
    env["ISTOTA_USER_TZ"] = config.resolve_user_timezone(task.user_id)

    cmd = [sys.executable, "-m", f"istota.skills.{skill_name}"] + skill_args
    try:
        proc = _run_capture(
            cmd,
            timeout=timeout,
            cwd=str(config.temp_dir),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"Skill timed out after {config.scheduler.task_timeout_minutes} minutes",
        )
    except Exception as e:
        return False, f"Skill execution error: {e}"

    err_msg = _skill_error_envelope(proc.stdout)
    if proc.returncode == 0:
        # Module-skill subprocesses (feeds, money) catch their own errors
        # and print `{"status":"error","error":"…"}` while exiting 0;
        # treat that envelope as failure (defense in depth — the facades
        # also call sys.exit(1) on the error envelope).
        if err_msg:
            return False, err_msg
        return True, proc.stdout.strip() if proc.stdout else "(no output)"

    # A non-zero exit is the *normal* way that same envelope arrives, and the
    # envelope is on stdout. Reading stderr alone recorded "Exit code 1" and
    # discarded the diagnosis the skill had just written — which is what a
    # human reads back out of `scheduled_jobs.last_error` via `!cron` and the
    # admin Cron pane.
    if err_msg:
        return False, err_msg
    error = proc.stderr.strip() if proc.stderr else f"Exit code {proc.returncode}"
    return False, error


def _execute_command_task(
    task: db.Task, config: Config,
) -> tuple[bool, str]:
    """Execute a shell command task via subprocess.

    Returns (success, result) — same interface as execute_task().
    """
    # Defense in depth — cron_loader rejects command-type CRON.md entries
    # from non-admins at sync time, but a stale row could have been inserted
    # by an earlier admin or a direct DB write. Auto-seeded module skill
    # tasks (feeds, money) now go through ``_execute_skill_task``; only
    # operator-defined CRON.md ``command:`` rows remain on this path.
    if not config.is_admin(task.user_id):
        return False, "command-type tasks are admin-only"

    timeout = config.scheduler.task_timeout_minutes * 60

    from .executor import build_stripped_env, get_user_temp_dir
    from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
    from .skills._loader import load_skill_index
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    env = build_stripped_env()
    env["ISTOTA_TASK_ID"] = str(task.id)
    # See ``_execute_skill_task``: the attempt travels with the id (ISSUE-377).
    env["ISTOTA_TASK_ATTEMPT"] = str(task.attempt_count + 1)
    env["ISTOTA_USER_ID"] = task.user_id
    # Both sibling paths (``_execute_skill_task``, ``executor.execute_task``)
    # export this, and every skill CLI keys its deferred writes off it. Without
    # it a CRON ``command:`` row invoking a skill CLI silently lost whatever
    # the CLI had no direct-write fallback for — notably the email skill's
    # ``sent_emails`` record, so a correspondent's reply had nothing to thread
    # back against (ISSUE-233).
    env["ISTOTA_DEFERRED_DIR"] = str(user_temp_dir)
    env["ISTOTA_EXPERIMENTAL_FEATURES"] = ",".join(config.experimental.features)
    # Propagate the config path so module skills (feeds, money) loading
    # istota config from a fresh subprocess find the same file the daemon
    # did — the subprocess cwd is `config.temp_dir`, which doesn't contain
    # the relative `config/config.toml` candidate.
    if config.config_path:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)
    if config.nextcloud_mount_path:
        env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path)
    if task.conversation_token:
        env["ISTOTA_CONVERSATION_TOKEN"] = task.conversation_token

    # Resolve credential / connection env vars from skill manifests
    # (NC_URL/USER/PASS, CALDAV_*, etc.) instead of hardcoding them.
    # Same trusted resolution path the skill-task dispatcher uses; the
    # operator's command may invoke any istota-skill CLI, so we expose
    # the union over the full skill_index. CalDAV vars are gated on
    # discovered calendars to mirror the LLM path.
    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)
    skill_index = load_skill_index(config.skills_dir, config.bundled_skills_dir)
    ctx = EnvContext(
        config=config,
        task=task,
        user_resources=user_resources,
        user_config=config.get_user(task.user_id),
        user_temp_dir=user_temp_dir,
        is_admin=config.is_admin(task.user_id),
        discovered_calendars=discover_calendars_for_task(task, config),
    )
    for k, v in build_skill_env(list(skill_index), skill_index, ctx).items():
        if k not in env:
            env[k] = v
    # Run setup_env hooks. Without this, declarative env specs marked
    # ``from: "setup_env"`` (notably ``LOCATION_DB_PATH``, ``HEALTH_DB_PATH``)
    # resolve to None in build_skill_env — operator-defined CRON.md
    # command rows that shell out to skill CLIs needing those vars would
    # fail silently. Mirrors the _execute_skill_task path. Hook values
    # win over the daemon's ambient env because they are computed
    # per-user; a stray LOCATION_DB_PATH inherited from systemd would
    # point at the wrong user's DB.
    env.update(dispatch_setup_env_hooks(list(skill_index), skill_index, ctx))
    try:
        # `shell_argv` rather than `shell=True`: the latter is `/bin/sh -c`,
        # which on Debian is dash and starts with `pipefail` off, so a CRON
        # `command:` row ending in a pipe reported its last stage. A job whose
        # real work failed was recorded as healthy indefinitely — the inverse of
        # what the five-consecutive-failure auto-disable exists to catch.
        proc = _run_capture(
            shell_argv(task.command),
            timeout=timeout,
            cwd=str(config.temp_dir),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {config.scheduler.task_timeout_minutes} minutes"
    except Exception as e:
        return False, f"Command execution error: {e}"

    if proc.returncode == 0:
        result = proc.stdout.strip() if proc.stdout else "(no output)"
        # Module-skill subprocesses (feeds, money, …) catch their own errors
        # and print `{"status":"error","error":"…"}` to stdout while exiting 0,
        # which would otherwise look like a successful run. Treat that envelope
        # as failure so retries / alerts kick in instead of silently rotting.
        if result.startswith("{"):
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "error":
                err_msg = parsed.get("error") or "command reported status=error"
                return False, str(err_msg)
        return True, result
    else:
        error = proc.stderr.strip() if proc.stderr else f"Exit code {proc.returncode}"
        if proc.returncode == SIGPIPE_EXIT:
            # This string becomes `scheduled_jobs.last_error`, which `!cron`,
            # the admin UI and the `cron_job` inbox row all show to a human. A
            # SIGPIPE'd producer writes nothing to stderr, so without the note
            # the operator gets a bare "Exit code 141" on a correct command.
            # It is also what `is_sigpipe_failure` keys on to keep this off the
            # retry ladder — see the failure branch in `process_one_task`.
            error = f"{error}. {SIGPIPE_NOTE}"
        return False, error


def _drain_deferred_ops(config: Config, task: db.Task, result: str) -> None:
    """Replay a completed task's deferred-op files (memory / kv / KG / health /
    subtasks / sent-emails / email-output), discard any file whose handler has
    been retired, and warn on unconsumed files. The single source of truth for
    the post-success drain — shared by ``process_one_task`` and
    ``run_task_inline`` so the two can't drift.
    """
    from .executor import get_user_temp_dir
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    # First, not merely before the warn: the handlers below run in a bare
    # sequence with no guard between them, so anything raising in one of them
    # would otherwise leave a retired file on disk until the temp sweep.
    _process_retired_deferred_files(config, task, user_temp_dir)
    _process_deferred_subtasks(config, task, user_temp_dir)
    _process_deferred_sent_emails(config, task, user_temp_dir)
    _process_deferred_kv_ops(config, task, user_temp_dir)
    _process_deferred_kg_ops(config, task, user_temp_dir)
    _process_deferred_health_ops(config, task, user_temp_dir)
    _process_deferred_garmin_import(config, task, user_temp_dir)
    _process_deferred_user_alerts(config, task, user_temp_dir)
    _deliver_deferred_email_output(config, task, user_temp_dir)
    _warn_unconsumed_deferred_files(task, user_temp_dir)


def run_task_inline(
    config: Config,
    task: db.Task,
    *,
    event_writer: "EventWriter | None" = None,
    workspace_dir: "Path | None" = None,
) -> tuple[bool, str]:
    """Execute a task to completion in-process and finalize it, with no claim,
    ack, transport push, or retry.

    Runs ``execute_task`` (streaming the brain's events through ``event_writer``
    so a subscriber can render them), emits the terminal ``result`` / ``error``
    / ``cancelled`` + ``done`` events, updates the task status, and drains the
    deferred-op files. This is the "run a task to completion locally" core the
    REPL (and a future ``istota task -x``) reuses — so the deferred-op drain and
    terminal-event emission can't drift from the daemon path.

    Returns ``(success, result_text)``.
    """
    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)

    success, result, actions_taken, execution_trace = execute_task(
        task, config, user_resources,
        event_writer=event_writer, workspace_dir=workspace_dir,
    )

    # Same success-guards the daemon applies: API errors masquerading as
    # success, and malformed (leaked tool-call XML) output.
    if success and is_api_error_banner(result):
        success = False
    if success:
        malformed = detect_malformed_result(result, output_target=task.output_target)
        if malformed:
            success = False
            result = f"Malformed output: {malformed}"

    is_cancelled = (not success) and result == "Cancelled by user"

    if event_writer is not None:
        if is_cancelled:
            event_writer.emit("cancelled")
        elif success:
            # Full answer — the `result` event is the deliverable to stream
            # surfaces (web/REPL) and must not be clipped (ISSUE-178).
            event_writer.emit("result", {"text": result, "truncated": False})
        else:
            event_writer.emit(
                "error",
                {"message": _error_event_message(result), "stop_reason": "error"},
            )
        event_writer.emit("done", {
            "stop_reason": "completed" if success else "error",
            "duration_seconds": round(event_writer.elapsed_seconds(), 1),
            **({"model": task.model_used} if task.model_used else {}),
        })
        event_writer.finish()
        # Prune ephemeral text_delta rows for stream surfaces (repl, web) once
        # the terminal event has fired — the in-process subscriber already
        # rendered them live, so retaining the rows only bloats the log.
        from .transport.registry import task_is_stream_surface
        if task_is_stream_surface(config, task):
            with db.get_db(config.db_path) as _prune_conn:
                db.delete_task_events_by_kind(_prune_conn, task.id, "text_delta")
                db.delete_task_events_by_kind(_prune_conn, task.id, "thinking")

    status = "completed" if success else ("cancelled" if is_cancelled else "failed")
    with db.get_db(config.db_path) as conn:
        db.update_task_status(
            conn, task.id, status,
            # On a failure the answer column carries whatever the model had
            # written before the stop, not the error (ISSUE-372).
            result=result if success else task.partial_result,
            error=None if success else result,
            actions_taken=actions_taken, execution_trace=execution_trace,
        )

    if success:
        _drain_deferred_ops(config, task, result)

    return success, result


def _email_task_from_the_user(config: Config, task: db.Task) -> bool:
    """Whether this email task's own sender is the user it was routed to.

    The fact the two ISSUE-255 failure paths need and that `withheld_from_room`
    can only carry half of. Recovered from `processed_emails`, which the poller
    writes for every message it ingests, and judged by
    `email_support.sender_claims_to_be_user` so this cannot drift from the
    poller's own answer.

    A *claim*, exactly as at ingest: SMTP `From:` is unauthenticated. That is the
    right strength here — the consequence is an error notice the user may not
    have needed, not a trust decision.

    Never raises and never blocks a delivery: a task whose ledger row has been
    pruned, or a lookup that fails, answers False and leaves the pre-existing
    behaviour in place.
    """
    if task.source_type != "email":
        return False
    # Imported here, as every other `email_support` use in this module is: the
    # module pulls in the email skill, which is an optional extra.
    from .email_support import sender_claims_to_be_user  # noqa: PLC0415

    try:
        with db.get_db(config.db_path) as conn:
            record = db.get_email_for_task(conn, task.id)
        if record is None:
            return False
        return sender_claims_to_be_user(config, task.user_id, record.sender_email)
    except Exception as e:  # pragma: no cover - never fail a delivery over this
        logger.warning(
            "could not resolve the sender of email task %s: %s", task.id, e,
        )
        return False


def _note_job_auto_disabled(
    conn, job_id: int, fail_count: int,
) -> RaiseResult | None:
    """The inbox row for a scheduled job the scheduler has just suspended.

    Three sites suspend a job — the policy-refusal branch, the ordinary failure
    branch, and `_record_publish_failure` — and all three previously wrote a
    `task_logs` warning and told nobody. Each now buffers the result of this and
    hands it to `deliver_pending` after its `with` block closes; the write rides
    the caller's already-open transaction, which is the whole point of the split.

    Re-reads the job rather than taking the caller's copy: `last_error` and
    `consecutive_failures` were both written by the increment a few statements
    earlier, so a `ScheduledJob` fetched before it carries the previous run's
    error. `job.user_id` is the owner of the row, not `task.user_id` — they are
    the same today and only one of them is the authority.

    **Guarded, and that is not belt-and-braces.** Everything here except
    `write_notification` itself runs in the caller's frame, inside a transaction
    that has just recorded the task as failed and charged the job a failure — so
    an exception escaping would skip `db.get_db`'s commit and roll all of that
    back, leaving the task stuck `running` and the job still firing. An inbox
    row is not worth that.
    """
    try:
        job = db.get_scheduled_job(conn, job_id)
        if job is None:
            return None
        # Answers True for every name today (ISSUE-391 retired the `_module.*`
        # exclusion). Kept as a call rather than deleted: it is the one place a
        # whole class of job can be taken out of this source, the source owns
        # that decision, and the three suspend sites reach the producer only
        # through here. Not dead code — a gate with nothing currently behind it.
        if not cron_job_source.should_notify(job.name):
            return None
        return cron_job_source.write(
            conn, job.user_id,
            job_id=job_id,
            job_name=job.name,
            fail_count=fail_count,
            cron_expression=job.cron_expression,
            # Provenance only. Nothing reads `notifications.room_token` on the
            # send path — `deliver_pending` routes by purpose through the user's
            # routing table — so a job with `output_target` set still pushes to
            # their alert destination, not into that room.
            room_token=job.conversation_token,
            last_error=job.last_error,
        )
    except Exception:
        logger.warning(
            "could not raise the auto-disable notification for job %s",
            job_id, exc_info=True,
        )
        return None


def _remove_once_job_from_cron_md(config: Config, user_id: str, job_name: str) -> None:
    """Take a completed once-job out of CRON.md, after the task transaction.

    Split out of ``process_one_task`` by ISSUE-387. The write lands on the
    rclone FUSE mount, and it used to run inside the still-open transaction
    that had just deleted the job row — so a mount that stopped answering held
    SQLite's write lock for the whole FUSE timeout and stalled every other
    framework-DB writer: the dispatch loop, the other workers, the web app,
    the pollers. The caller buffers ``(user_id, job_name)`` instead and calls
    this once the ``with`` block has closed.

    **Never raises**, and the move is what made that load-bearing rather than
    tidy. This runs *after* the commit, so an exception escaping here would
    leave the task already recorded ``completed`` with everything that
    finishes it still ahead — the result never delivered, the deferred ops
    never applied, the terminal event never emitted, and nothing retrying a
    ``completed`` row. ``remove_job_from_cron_md`` states its own never-raises
    contract, so the guard is defence in depth: it keeps a future edit to the
    writer from silently converting a stale file into a lost answer. Same
    shape as ``deliver_pending``, which contains its own exceptions for the
    buffer flushed beside this one.
    """
    try:
        from .cron_loader import remove_job_from_cron_md

        removed = remove_job_from_cron_md(config, user_id, job_name)
        if removed:
            # Deleting the row and rewriting the file are no longer one step,
            # and `_sync_cron_files` runs on the main loop every
            # `briefing_check_interval` treating CRON.md as authoritative. A
            # sync landing in between reads a file that still names this job
            # and re-inserts the row the task just deleted — the `once = true`
            # job that runs a second time, which is exactly what the warning
            # below exists to report. Before ISSUE-387 the two happened in the
            # opposite order, so a sync in the window saw a row the file did
            # not name and deleted it; hoisting the write turned a
            # self-correcting interleaving into a harmful one, and this closes
            # it again. The window is not microseconds in the case that
            # matters: under the hung mount this whole change is about, reads
            # still answer from cache while the write blocks.
            #
            # Safe to delete unconditionally because the file no longer names
            # the job, so a row under this name can only be that resurrection
            # — and if the write did *not* land we are in the `else` below,
            # where the file is still the definition and the row belongs to it.
            with db.get_db(config.db_path) as conn:
                resurrected = db.get_scheduled_job_by_name(conn, user_id, job_name)
                if resurrected is not None:
                    db.delete_scheduled_job(conn, resurrected.id)
                    logger.info(
                        "One-time job '%s' was re-inserted by a cron sync "
                        "between its deletion and the CRON.md write; deleted "
                        "again (user=%s job_id=%d)",
                        job_name, user_id, resurrected.id,
                    )
        elif config.use_mount:
            # The table row is already gone, so if the job is still in the
            # file it is now the only definition and the next sync re-inserts
            # it — a `once = true` job that runs a second time. Unchanged
            # behaviour, but until ISSUE-369 the writer could not report a
            # refused write at all, so nothing said so. Guarded on `use_mount`
            # because CRON.md is not the source of truth without one and False
            # there is the ordinary answer rather than a failure.
            logger.warning(
                "One-time job '%s' was removed from the table "
                "but not from CRON.md for user %s (no CRON.md, "
                "no such job in it, or the write was refused); "
                "if the job is still in the file the next sync "
                "will re-insert it",
                job_name, user_id,
            )
    except Exception:
        logger.warning(
            "once_job_cron_md_removal_failed user=%s job=%s",
            user_id, job_name, exc_info=True,
        )


def process_one_task(
    config: Config, dry_run: bool = False, user_id: str | None = None,
    queue: str | None = None,
) -> tuple[int, bool] | None:
    """
    Claim and process one pending task.
    Returns (task_id, success) or None if no tasks available.

    Args:
        user_id: If provided, only claim tasks for this user.
        queue: If provided, only claim tasks in this queue ('foreground' or 'background').
    """
    worker_id = get_worker_id(user_id)

    with db.get_db(config.db_path) as conn:
        # Claim a task
        task = db.claim_task(
            conn, worker_id, config.scheduler.max_retry_age_minutes,
            user_id=user_id, queue=queue,
            stuck_running_minutes=_stuck_running_minutes(config.scheduler),
            heartbeat_stuck_minutes=config.scheduler.worker_stuck_minutes,
        )
        if not task:
            return None

        task_id = task.id
        db.log_task(conn, task_id, "info", f"Task claimed by {worker_id}")

        # Update to running
        db.update_task_status(conn, task_id, "running")

        # Get user resources
        user_resources = db.get_user_resources(conn, task.user_id)

    # Log channel setup — resolve before execution starts. The verbose log is
    # opt-in and routes to any user-routable transport destination (talk default
    # / email / ntfy / comma list); effective_log_destinations returns [] when
    # the user configured neither a log route nor a legacy log_channel.
    log_dests: list[Destination] = []
    if not task.skip_log_channel:
        log_dests = effective_log_destinations(config, task.user_id)
    log_channel_prefix = ""
    log_callback = None
    if log_dests and not dry_run:
        # Resolve the *source* channel name for the log prefix — only when the
        # task originated on Talk (an OCS display-name lookup is meaningless for
        # email / repl origins, which fall back to source_type).
        channel_name = None
        if task.conversation_token and _surface_for_source_type(task.source_type) == "talk":
            try:
                channel_name = run_coro(
                    _resolve_channel_name(config, task.conversation_token),
                )
            except Exception:
                channel_name = task.conversation_token
        log_channel_prefix = _log_channel_source_label(task, channel_name)

    # A re-attempt starts with a clean slate (ISSUE-074). The retry branch
    # below purges when *it* requeues, but three other paths requeue the same
    # task.id with attempt_count + 1 and no purge — the periodic reclaim
    # (db.fail_stuck_locked_running_tasks), startup orphan recovery
    # (db.recover_orphaned_tasks), and claim_task's own inline copy of the
    # stuck-running release, which has no scheduler-side hook at all. Purging
    # here instead covers all of them at once, and has to run *before*
    # execution so it clears the previous attempt's files rather than this
    # attempt's. attempt_count == 0 is left alone — a first run has no prior
    # attempt to clean up after.
    #
    # A confirmation re-run is exempt whatever the count says. It is not a
    # re-attempt: _drain_deferred_ops is skipped when a task asks for
    # confirmation, so ops written before the question sit on disk *by design*
    # until the user answers, and db.confirm_task requeues without resetting
    # attempt_count — so a task that failed once earlier arrives here carrying
    # a charged attempt and would have its held writes discarded. The narrower
    # stale-email_output cleanup just below is what that path needs instead.
    if task.attempt_count > 0 and task.confirmation_prompt is None:
        from .executor import get_user_temp_dir
        _purge_deferred_files_for_retry(
            task, get_user_temp_dir(config, task.user_id),
        )

    # Clean up stale deferred email output from a previous execution (e.g.
    # confirmation flow: first run writes a draft via `email output`, re-run
    # sends via `email send` — the stale file would cause a double-send).
    if task.confirmation_prompt is not None:
        from .executor import get_user_temp_dir
        _stale = get_user_temp_dir(config, task.user_id) / f"task_{task.id}_email_output.json"
        if _stale.exists():
            logger.debug("Removing stale email output file from prior execution of task %d", task.id)
            _stale.unlink(missing_ok=True)

    # Command and skill tasks skip Talk ack, attachment download, and
    # resource loading (cron-driven, no live user behind a Talk session).
    # The brain path builds an EventWriter and subscribes the in-process
    # consumers (Talk / log channel / push). ack_msg_id and the subscribers
    # are referenced again during delivery, so they live at this scope.
    event_writer: EventWriter | None = None
    talk_sub: TalkEventSubscriber | None = None
    log_callback: LogChannelSubscriber | None = None
    ack_msg_id = None
    # Ping liveness for the whole execution so stuck-task reclaim can tell a
    # slow-but-alive worker from a dead one (ISSUE-112). Covers the skill,
    # command, and brain paths; stops on exit even if execution raises.
    with _task_heartbeat(config, task_id):
        if task.skill:
            success, result = _execute_skill_task(task, config)
            actions_taken = None
            execution_trace = None
        elif task.command:
            success, result = _execute_command_task(task, config)
            actions_taken = None
            execution_trace = None
        else:
            # One event writer per task; subscribers fan its stream out to
            # their surfaces. SSE / admin consumers are NOT subscribers —
            # they poll the task_events table the writer persists to.
            event_writer = EventWriter(
                task_id, str(config.db_path),
                enabled=config.scheduler.event_log_enabled,
            )

            # Send ack message + wire the progress subscriber for surfaces that
            # support a progress ack. The capability check is the transport seam;
            # the source_type == "talk" guard preserves today's behaviour (only
            # interactive Talk tasks get an editable ack — briefings, scheduled
            # jobs, and subtasks that also resolve to the Talk surface do not).
            is_rerun = task.attempt_count > 0 or task.confirmation_prompt is not None
            _delivery_transport = make_registry(config).for_task(task)
            _supports_ack = bool(
                _delivery_transport
                and _delivery_transport.capabilities.supports_progress_ack
            )
            # Where the ack goes. `conversation_token` is the room's canonical
            # token, postable only when it equals the room's Talk ref — false on
            # a promoted room, where the ack 404'd and every progress edit
            # behind it silently no-opped (ISSUE-400). Resolved once and carried
            # rather than per edit: the resolution reads the database and an
            # edit fires per tool call, so it also sits behind the free guards.
            # `conversation_token` stays in the guard so this can only redirect
            # an ack that would have been posted anyway — see the
            # `process_one_task` bullet in `.claude/rules/transport.md`.
            ack_token = (
                _talk_target_for_delivery(config, task)
                if (_supports_ack and task.source_type == "talk"
                    and task.conversation_token and not dry_run)
                else None
            )
            if ack_token:
                ack_text = f"`#{task.id}` *Retrying…*" if is_rerun else f"`#{task.id}` *{random.choice(PROGRESS_MESSAGES)}*"
                ack_msg_id = run_coro(post_result_to_talk(
                    config, task, ack_text,
                    reference_id=f"istota:task:{task.id}:ack",
                    target_token=ack_token,
                ))
                if ack_msg_id is None:
                    # Name the room. `_talk_binding_for_task` swallows a
                    # database failure and falls back to `conversation_token`,
                    # which on a promoted room is the unpostable one — so this
                    # warning has to say which token was tried, or a resolution
                    # failure and a genuine API failure look identical.
                    logger.warning(
                        "Ack message posted but no message ID returned for task %d "
                        "(room %s; progress edits will no-op)",
                        task.id, ack_token,
                    )
                if config.scheduler.progress_updates:
                    talk_sub = TalkEventSubscriber(
                        config, task, ack_msg_id, target_token=ack_token,
                    )
                    event_writer.subscribe(talk_sub)

            # Log channel subscriber (no rate limiting, streams every tool call
            # to edit-capable destinations).
            if log_dests and log_channel_prefix:
                log_callback = LogChannelSubscriber(
                    config, task, log_dests, log_channel_prefix,
                )
                event_writer.subscribe(log_callback)

            # Push notification subscriber, gated by source type.
            if task.source_type in config.scheduler.push_notification_sources and not dry_run:
                event_writer.subscribe(PushNotificationSubscriber(
                    config, task,
                    threshold_seconds=config.scheduler.push_notification_threshold_seconds,
                ))

            # Download Talk attachments to local filesystem before execution
            if task.source_type == "talk" and task.attachments:
                local_attachments = download_talk_attachments(config, task.attachments)
                # Create modified task with local paths
                task = replace(task, attachments=local_attachments)

            # Execute the task (outside the db context to avoid long locks)
            success, result, actions_taken, execution_trace = execute_task(
                task, config, user_resources, dry_run=dry_run, event_writer=event_writer,
            )

    # Duplicate-execution guard (ISSUE-112 follow-up). A slow-but-alive worker
    # whose heartbeat lapsed past the stuck threshold can have its task reclaimed
    # and re-run by a second worker while it's still executing — two answers for
    # one task id. We can't key on locked_by: get_worker_id() has no slot, so two
    # workers in the same process for the same user share an id. attempt_count is
    # the reliable token — the stuck-running release bumps it on every reclaim. If
    # the row's attempt_count has advanced past what we claimed, another worker
    # superseded us; abandon our result rather than deliver a duplicate or
    # double-apply deferred ops. The superseding worker owns delivery + the
    # terminal event frame (its EventWriter resumes seq), so we just bail.
    if not dry_run:
        with db.get_db(config.db_path) as conn:
            _current = db.get_task(conn, task_id)
        if _current is None or _current.attempt_count != task.attempt_count:
            logger.warning(
                "Task %d superseded mid-run (claimed attempt=%s, now=%s) — "
                "discarding this worker's result without delivering",
                task_id, task.attempt_count,
                _current.attempt_count if _current else "deleted",
            )
            return (task_id, False)

    # Resolve the delivery plan: the single source of truth for where this
    # task's result goes. Replaces the hardcoded output_target fan-out — the
    # plan parses task.output_target descriptors (talk/email/ntfy/istota_file/
    # stream, comma-separated, surface[:channel]), normalizes legacy both/all,
    # infers the source_type default when unset, and resolves Talk channels via
    # the synthetic-email-token fallback.
    registry = make_registry(config)
    plan = resolve_delivery_plan(config, task, registry)
    _talk_dest = next((d for d in plan if d.surface == "talk"), None)
    # talk_token: the plan's Talk channel when Talk is a destination (honours an
    # explicit talk:<token>; equals _talk_target_for_delivery for the inferred
    # case). Falls back to the unconditional resolution when Talk is NOT in the
    # plan, because the heartbeat_silent branch delivers to Talk regardless of
    # output_target (it bypasses the plan entirely, matching prior behaviour).
    talk_token = (
        _talk_dest.channel if _talk_dest else _talk_target_for_delivery(config, task)
    )
    plan_talk = _talk_dest is not None
    # The other half of "was the user themselves waiting for this answer"
    # (ISSUE-275). `tasks.withheld_from_room` records it for a self-addressed
    # thread reply and cannot record it for self-addressed first contact — the
    # column means "there is a room and this exchange is deliberately not part of
    # it", and first contact resolves no room — so this recovers it from the
    # ledger row the poller already writes. Reconstruction rather than a new
    # column, and the same reconstruction `confirmations._restore_transcript_
    # mirror` makes: `sender_claims_to_be_user` is the single definition both
    # spellings share, which is why it lives in `email_support` rather than in
    # the poller. Read once here so the two failure paths below cannot answer
    # differently about the same task.
    email_from_the_user = _email_task_from_the_user(config, task)
    # A mirror Talk leg (room fan-out from a non-Talk origin, e.g. a web-origin
    # task mirrored to its bound Talk room) carries the confirmation prompt only
    # when the task's own origin is *not* a room surface — a web-origin
    # confirmation stays on web, an email-origin one has nowhere else to go. The
    # full argument is at the branch that reads this, below.
    _talk_is_mirror = bool(_talk_dest and getattr(_talk_dest, "mirror", False))
    plan_email = plan_has_surface(plan, "email")
    plan_ntfy = plan_has_surface(plan, "ntfy")
    plan_file = plan_has_surface(plan, "istota_file")
    plan_web = plan_has_surface(plan, "web")
    # A *push* web destination is a foreign task (e.g. an email reply) routing
    # INTO a room — it must be delivered via WebTransport.deliver. An own-origin
    # web task resolves its web leg to a stream no-op (its result event already
    # covers the room over SSE), so it is absent here and never double-posts.
    # Selected by capability, not by name: any surface whose view of a room is
    # the canonical store has this property, and `web` is merely the only one
    # that does today. Naming it here is what would make a second such surface
    # fall through to the foreign lane and double-render.
    web_push_dests = [
        d for d in plan
        if d.kind == "push" and is_canonical_room_view(config, registry, d.surface)
    ]
    # The room this exchange belongs to, resolved once, before any per-surface
    # branch and before anything is written (ISSUE-247). For every surface but
    # email it is the task's own conversation token; for an email task that
    # token is a thread hash and the room comes from the routing that decided
    # where the mail surfaces. `None` means the exchange has no room — a cron
    # job mailing an external address — and stays task-only.
    with db.get_db(config.db_path) as _room_conn:
        transcript_token = transcript_room_for_task(_room_conn, config, task)

    # Split by whether the push target IS the exchange's own room. For a
    # canonical room view, a push at that room *is* the assistant row —
    # delivering it as well renders the same answer a second time as a
    # role='system' cmd-output note (ISSUE-164). A different room holds no
    # question and gets no row of its own, so there a push is the only delivery
    # and a notice is its right shape.
    #
    # An exhaustive partition on one predicate, deliberately: computing the two
    # lists from *different* tests would let a destination fall into neither and
    # be silently dropped — neither stored nor pushed.
    own_room_canonical_dests = [
        d for d in web_push_dests if transcript_token and d.channel == transcript_token
    ]
    web_foreign_dests = [
        d for d in web_push_dests
        if not (transcript_token and d.channel == transcript_token)
    ]

    # Track if we need to call istota_file handler after db connection closes.
    # The transport derives success from the task's terminal status at delivery.
    call_file_handler = False
    post_ntfy = False
    post_web = False

    # Track what to post after DB transaction closes
    post_talk_message = None
    # The delivered body, mirrored into the Talk room's web transcript when that
    # room is not the one the canonical row went to. See `_talk_result_mirror_body`.
    post_talk_mirror_body = None
    post_email = False
    is_failure_notify = False
    # A user-facing notice for a task whose plan has no room leg to carry one
    # (ISSUE-255). Buffered rather than sent inline for the reason every other
    # notification on this path is: routed by purpose, it can land on `web`,
    # whose delivery opens a second connection to the database this function is
    # holding a write transaction on.
    failure_alert = None
    # The short label the same notice carries into the inbox row and into an
    # ntfy header. Set beside every `failure_alert` assignment.
    failure_alert_title = None
    # Inbox rows raised inside the transaction below and delivered after it, for
    # the same reason `failure_alert` is buffered: `deliver_pending` sends
    # through `send_notification`, which routes by purpose and can land on the
    # web surface, which opens a second connection to this database.
    notification_results: list[RaiseResult | None] = []

    # Guard: detect API errors masquerading as successful results
    # (Claude Code may exit 0 with API error text as output).
    # The STRICT banner detector, not `parse_api_error`: this discards a
    # completed answer and fails the task permanently, so a successful reply
    # that merely *quotes* a provider error ("the earlier run died with API
    # Error: 529") must not match. The brain now classifies the same case at
    # source (`_success_frame_stop_reason`), leaving this as the backstop.
    if success and is_api_error_banner(result):
        logger.warning(
            "Task %d: result contains API error despite success flag, treating as failure",
            task_id,
        )
        success = False

    # Guard: detect malformed model output (leaked tool-call XML syntax).
    # Strict mode applies when the result will render in Talk, i.e. Talk is a
    # resolved destination (the inferred default included). Passing a "talk"
    # descriptor when Talk is in the plan keeps the prior strict/lenient split.
    if success:
        _malformed_target = "talk" if plan_talk else None
        malformed_reason = detect_malformed_result(result, output_target=_malformed_target)
        if malformed_reason:
            logger.warning(
                "Task %d: malformed result detected (%s), treating as failure",
                task_id, malformed_reason,
            )
            success = False
            result = f"Malformed output: {malformed_reason}"

    # Log result quality metrics
    if result:
        _qm_tool_count = 0
        if actions_taken:
            try:
                _qm_tool_count = len(json.loads(actions_taken))
            except (json.JSONDecodeError, TypeError):
                pass
        logger.info(
            "Task %d result metrics: success=%s, chars=%d, tools=%d",
            task_id, success, len(result), _qm_tool_count,
        )

    # A confirmation prompt is answerable on an interactive surface: a Talk
    # reply (Talk is a resolved destination, and not the "all" broadcast
    # fan-out, which pushes ntfy), or the web /chat confirm endpoint — but only
    # for a task whose OWN origin is web (source_type="web"), whose confirmation
    # rides its task_events SSE stream and is answered by POST
    # /chat/tasks/{id}/confirm. A *foreign* task merely pushed into a web room
    # (e.g. an email reply routed there) has no such SSE the room tails and no
    # answerable affordance, so it must not gate — it completes and the question
    # text is delivered to the room as a normal result (the user answers by
    # replying). Computed once and reused by the status, event-emission, and
    # deferred-op-skip branches below.
    _own_origin_web = plan_web and task.source_type == "web"
    _confirmable_surface = (plan_talk and talk_token and not plan_ntfy) or _own_origin_web
    # A no-final-answer result embeds mid-turn text the model wrote to itself,
    # not to the user, so its "should I proceed?" is not a question awaiting an
    # answer. Parking the task on it would hold it for the whole confirmation
    # timeout with a synthesized notice as the prompt (ISSUE-211).
    is_confirmation_request = bool(
        success
        and _confirmable_surface
        and not is_no_final_answer(result)
        and CONFIRMATION_PATTERN.search(result)
    )

    # The durable `messages.id` of this task's stored assistant turn, when it
    # was persisted below. Threaded into the terminal `done` event so a
    # freshly-settled web turn learns its star key without a history refetch
    # (ISSUE-172).
    stored_assistant_msg_id: int | None = None

    # The `confirmation` row written when a task parks, kept in scope past the
    # transaction because its push may still be owed at the tail (ISSUE-404):
    # the branch that writes it withholds delivery when the Talk post is
    # carrying the question instead, and that post can fail.
    held_notification: RaiseResult | None = None

    # A once-job whose table row was deleted inside the transaction below, and
    # whose CRON.md entry therefore still has to go: `(user_id, job_name)`.
    # Buffered rather than written in place, the same shape as
    # `notification_results` above. CRON.md is on the rclone mount and the
    # write followed `delete_scheduled_job`, which is what takes SQLite's
    # write lock — so a hung mount held that lock for the whole FUSE timeout
    # and stalled every other framework-DB writer: the dispatch loop, the
    # other workers, the web app, the pollers (ISSUE-387).
    # `_remove_once_job_from_cron_md` is the flush, and it owns the two things
    # the hoist introduced: it cannot raise past an already-committed task,
    # and it closes the window where a cron sync re-inserts the deleted row.
    once_job_to_remove: tuple[str, str] | None = None

    with db.get_db(config.db_path) as conn:
        if success:
            if is_confirmation_request:
                # Set task to pending confirmation instead of completing
                db.set_task_confirmation(conn, task_id, result)
                db.log_task(conn, task_id, "info", "Task awaiting user confirmation")
                # Talk confirmations post the prompt to the room; web/stream
                # confirmations surface it via the `confirmation` task event
                # (emitted below) and must not cross-post to Talk.
                #
                # A mirror Talk leg is suppressed only for a task whose OWN
                # origin is a room surface — a web-origin confirmation stays on
                # web, where its SSE stream carries it and the /chat confirm
                # endpoint answers it. An email-origin task has no such stream:
                # the mirror leg is the only push surface that can reach the
                # user at all, and the email leg must never carry the question
                # (it would mail the principal's decision to the external
                # correspondent — it stays suppressed by sitting in the `else:`
                # below). Excluding every mirror leg meant the task parked with
                # the question delivered nowhere, then died at
                # `expire_stale_confirmations` two hours later.
                #
                # **The one site asking the room-*view* question** — "does this
                # task's own origin surface show it the question itself?" — and
                # the only place the view axis and the membership axis could
                # ever diverge. They coincide for every surface that exists
                # today, because a surface owns rooms exactly when it has a
                # transcript of its own to render the prompt into; keep them
                # apart anyway, since a read-only external view would answer
                # these two differently and this gate is read negated.
                #
                # `origin_surface_for_source_type` and **never**
                # `registry._surface_for_source_type`: that one answers "where
                # do I deliver this result" and maps every non-surface source
                # type to `talk`, so through it a cron, briefing or heartbeat
                # task would read as a room surface and have its prompt
                # suppressed on the mirror leg — delivered nowhere, then killed
                # by `expire_stale_confirmations` two hours later, which is the
                # exact failure the paragraph above records fixing.
                if plan_talk and talk_token and not (
                    _talk_is_mirror
                    and is_room_view(origin_surface_for_source_type(
                        task.source_type or ""
                    ))
                ):
                    post_talk_message = result

                # The durable record of the question, written on this connection
                # inside the transaction that just parked the task — always,
                # whatever else carries it, because the row is what makes the
                # question recoverable when the push is missed.
                #
                # It is *delivered* only when nothing else is carrying the
                # question. `post_talk_message` above is that push for a Talk
                # task, and raising there too would put the same question in the
                # room twice. A web-origin confirmation has only its own SSE
                # stream, which reaches a client that happens to be watching and
                # nobody else, so there this is the first push the user gets on
                # any surface they are not currently looking at.
                held_notification = confirmation_source.write(
                    conn, task.user_id, task_id=task_id,
                    title=confirmations.describe_prompt(result),
                    body=confirmation_source.body_for(result),
                    room_token=transcript_token,
                )
                # Withheld here, and owed at the tail if that push fails —
                # see the `talk_undelivered` arm at the end of this function
                # (ISSUE-404). `held_notification` stays in scope for it.
                if post_talk_message is None:
                    notification_results.append(held_notification)
                    held_notification = None
            else:
                db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions_taken, execution_trace=execution_trace)
                db.log_task(conn, task_id, "info", "Task completed successfully")

                # Persist the assistant turn into the canonical messages store
                # for room-surface tasks (Talk/web), so the unified history
                # reader stays caught up and the web transcript renders this
                # turn from messages. Idempotent; the trace stays in `tasks`.
                #
                # The ownership question, asked of the task's *origin* surface —
                # `origin_surface_for_source_type` and never
                # `registry._surface_for_source_type`, for the reason spelled
                # out at the confirmation gate above. A scheduled, briefing or
                # subtask row originates on no surface at all and answers None,
                # which is what the tuple answered for it before.
                if (
                    is_room_member(origin_surface_for_source_type(task.source_type))
                    and task.conversation_token
                    and db.get_room(conn, task.conversation_token) is not None
                ):
                    stored_assistant_msg_id = db.store_turn_message(
                        conn, task.conversation_token, role="assistant",
                        body=result, task_id=task_id,
                        origin_surface=task.source_type,
                    )
                    # A retry that re-completes the task finds the row already
                    # stored (store returns None) — recover its id so the star
                    # key still rides the terminal event.
                    if stored_assistant_msg_id is None:
                        stored_assistant_msg_id = db.get_turn_message_id(
                            conn, task.conversation_token, task_id, "assistant",
                        )

                # Index conversation for memory search (non-critical).
                # Skip silent scheduled jobs: high-volume retrieve-and-render
                # crons whose conversations have no recall value but inflate
                # memory_chunks (and the vec/FTS indexes derived from it).
                # Skip a no-final-answer result for the same reason: it is the
                # composer's boilerplate wrapped around mid-turn narration from
                # a turn that broke, so repeated failures would seed recall with
                # near-identical text that never answered anything (ISSUE-211).
                if (
                    config.memory_search.enabled
                    and config.memory_search.auto_index_conversations
                    and not task.heartbeat_silent
                    and not is_no_final_answer(result)
                ):
                    try:
                        from .memory.search import index_conversation as _index_conv
                        from .memory.sleep_cycle import speaker_labels
                        # An indexed chunk is recalled back into a later prompt,
                        # so it must not label an external contact's mail as the
                        # user's own words (ISSUE-226).
                        _speaker = speaker_labels(conn, config, [task]).get(
                            task_id, "User",
                        )
                        _index_conv(conn, task.user_id, task_id, task.prompt, result,
                                    speaker=_speaker)
                        # Also index under channel namespace if in a channel.
                        # Skipped for an exchange deliberately kept out of that
                        # room (ISSUE-255): `_recall_memories` serves this
                        # namespace back to every later task there, so indexing
                        # it would put the withheld turn in front of the model
                        # even where the transcript is clean. The per-user index
                        # above is untouched — the exchange is the user's own and
                        # belongs in their own recall.
                        if task.conversation_token and not task.withheld_from_room:
                            channel_uid = f"channel:{task.conversation_token}"
                            _index_conv(conn, channel_uid, task_id, task.prompt, result,
                                        speaker=_speaker)
                    except Exception as e:
                        logger.debug("Memory search indexing failed for task %s: %s", task_id, e)

                if task.heartbeat_silent:
                    # Silent scheduled job — ACTION/NO_ACTION logic
                    should_post, result_to_post = _strip_action_prefix(result)
                    if should_post:
                        db.log_task(conn, task_id, "info", "Silent scheduled job: action taken")
                        if talk_token:
                            post_talk_message = result_to_post
                            _store_room_turn(
                                conn, task, transcript_token, result_to_post,
                            )
                    else:
                        db.log_task(conn, task_id, "info", "Silent scheduled job: no action needed")

                else:
                    # Non-heartbeat, non-silent task: normal delivery logic
                    if task.source_type == "briefing":
                        # Parse structured JSON output; fall back to raw text
                        parsed_briefing = parse_briefing_json(result)
                        if parsed_briefing:
                            delivery_result = parsed_briefing["body"]
                        else:
                            delivery_result = strip_briefing_preamble(result)
                    else:
                        delivery_result = result
                    # What the *room transcript* shows. Identical to the
                    # delivered body everywhere except an email task whose
                    # result is itself the structured
                    # `{"subject","body","format"}` envelope the send path
                    # unwraps — mirroring that verbatim would put a JSON blob in
                    # the room and re-pair it into LLM history as the answer.
                    if task.source_type == "email":
                        from .transport.email.outbound import email_transcript_body
                        room_body = email_transcript_body(delivery_result)
                    else:
                        room_body = delivery_result
                    # One decision, before any per-surface branch, replacing the
                    # three calls that each hung off one: the Talk plan, an
                    # own-room web push, and the email-only plan the first two
                    # missed. Each was added when another routing shape turned
                    # up with no answer under its question.
                    #
                    # Two other writers of this row remain in this function and
                    # are *not* subsumed: the conversational store above (talk /
                    # web, which runs whether or not the task succeeded far
                    # enough to reach here) and the `heartbeat_silent` branch,
                    # which stores only what it also posts. Both are idempotent
                    # against this one — `store_turn_message` dedups on
                    # `(room, role, task_id)`.
                    #
                    # The Talk rung asks whether the Talk leg lands in *this*
                    # room. Before the room was resolved separately from
                    # `conversation_token` the two questions could not come
                    # apart; now they can, and a Talk post into some other room
                    # is not evidence that the answer belongs here (ISSUE-247).
                    #
                    # The second clause keeps the *old* rule wherever the old
                    # rule applied — a task whose transcript room is its own
                    # token, i.e. everything but a routed email. A scheduled job
                    # sitting in room A with `output_target="talk:B"` stored its
                    # answer in A, and while ISSUE-164's rule arguably says it
                    # should not, taking that row away is a behaviour change
                    # this issue was not asked to make and would silently drop
                    # a job's only transcript. Tightening it is its own change.
                    _talk_lands_here = bool(
                        plan_talk and talk_token and transcript_token
                        and (
                            _canonical_talk_room(conn, talk_token)
                            == transcript_token
                            or transcript_token == task.conversation_token
                        )
                    )
                    if _room_turn_belongs_here(
                        conn, task, task_id, transcript_token,
                        delivering_into_room=bool(
                            _talk_lands_here or own_room_canonical_dests
                        ),
                    ):
                        _store_room_turn(conn, task, transcript_token, room_body)
                    if plan_talk and talk_token:
                        # `room_body`, not `delivery_result`: for an email task
                        # whose result *is* the `{"subject","body","format"}`
                        # envelope, the second is raw JSON. The room already
                        # unwrapped it, and posting the envelope to Talk would
                        # be this issue's symptom 2 — two surfaces given
                        # different text for one message — in the other
                        # direction. Identical for every non-email task, where
                        # the two are the same string. The email leg is
                        # unaffected: `deliver_email_result` parses the envelope
                        # itself from the result and the deferred file.
                        post_talk_message = room_body
                        post_talk_mirror_body = _talk_result_mirror_body(
                            conn, task, talk_token, transcript_token,
                            room_body, web_push_dests,
                        )
                    if plan_email:
                        post_email = True
                    if plan_ntfy:
                        post_ntfy = True
                    if web_foreign_dests:
                        post_web = True
                    if plan_file:
                        call_file_handler = True

                # Track scheduled job success
                if task.scheduled_job_id:
                    job = db.get_scheduled_job(conn, task.scheduled_job_id)
                    # Publish the result into shared_kv when the job opts in
                    # (admin-shared-briefing-blocks). Gated + fail-loud inside;
                    # a failed/unauthorized publish records a job failure that the
                    # success reset below must NOT wipe (observability + de-admin
                    # auto-disable), so gate the reset on the publish outcome.
                    publish_ok = True
                    if job and job.publish_shared_kv:
                        publish_ok = _publish_result_to_shared_kv(
                            conn, config, task, job, result,
                            notifications=notification_results,
                        )
                    if publish_ok:
                        db.reset_scheduled_job_failures(conn, task.scheduled_job_id)
                        # The counter is back to zero, which is the resolver's
                        # close predicate, so the row would go `stale` on the
                        # next panel read anyway. Closing it here makes it
                        # `resolved` by the thing that actually ended the
                        # condition, and does it without waiting for a read.
                        # `job.user_id`, matching `_note_job_auto_disabled`: the
                        # row was written under the job's owner, so closing it
                        # under anyone else matches nothing, silently.
                        if job is not None:
                            cron_job_source.resolve_for_job(
                                conn, job.user_id, task.scheduled_job_id,
                                by="system",
                            )
                    # Auto-remove one-time jobs after successful execution
                    if job and job.once:
                        db.delete_scheduled_job(conn, task.scheduled_job_id)
                        logger.info(
                            "One-time job '%s' completed and removed (job_id=%d)",
                            job.name, job.id,
                        )
                        # `job.user_id`, for the reason stated sixteen lines
                        # up: the job's owner is who the row was written
                        # under, and CRON.md belongs to the same person. The
                        # task's user is normally the same and is not the fact
                        # being used here.
                        #
                        # Buffered, not written here — see the declaration and
                        # `_remove_once_job_from_cron_md`. Nothing else *on
                        # this thread* reads CRON.md between here and the
                        # flush, and nothing between them can return or raise,
                        # so the warning fires on the same runs it did before.
                        # The main loop is the part that is not unchanged: it
                        # syncs CRON.md on its own cadence and can now observe
                        # the row gone while the file still names the job, so
                        # the flush deletes the row a sync may have put back.
                        once_job_to_remove = (job.user_id, job.name)

        else:
            # Check if we should retry (skip for OOM, cancellation, and policy refusals)
            is_oom = "killed (likely out of memory)" in result
            is_cancelled = result == "Cancelled by user"
            is_policy = _is_policy_refusal(result)
            is_shutdown_collateral = _is_shutdown_collateral(result)
            # A request-shaped provider failure (bad model id, expired key,
            # oversized prompt) fails identically on every attempt, so the
            # 1/4/16-minute ladder buys nothing but delay (ISSUE-212). The brain
            # already skips its own in-brain retry for these; this is the task
            # level. Banner-gated so a normal answer discussing a 400 can't
            # suppress a legitimate retry.
            is_permanent = is_api_error_banner(result) and is_permanent_api_error(result)
            if is_permanent:
                logger.warning(
                    "Task %d: permanent provider error, not retrying: %s",
                    task_id, result[:200],
                )
            # A command task killed by SIGPIPE is the second non-retryable
            # class, and the reason is stronger than "the retry cannot help".
            # `pipefail` turned `<producer> | head -N` from exit 0 into exit
            # 141, and the ladder re-runs the *whole* command string — so a
            # producer that sends mail or writes a file does it again at 1, 4
            # and 16 minutes, having already succeeded. 141 will recur anyway.
            # Gated on `task.command` because `_execute_command_task` is the
            # only thing that composes this text; an LLM answer quoting it is
            # not a reason to skip a retry the task earned.
            is_sigpipe = bool(task.command) and is_sigpipe_failure(result)
            if is_sigpipe:
                logger.warning(
                    "Task %d: command killed by SIGPIPE, not retrying (the "
                    "pipeline's producer already ran): %s",
                    task_id, result[:200],
                )
            if is_cancelled:
                # `result` here is the partial answer, not the error: the brain
                # kept what the model had written when the cancel landed and the
                # row is where it survives (ISSUE-372). Nothing is posted — the
                # user stopped this on purpose and !stop already answered them —
                # but a 29-minute investigation is no longer reduced to the
                # three words in the `error` column.
                db.update_task_status(
                    conn, task_id, "cancelled",
                    result=task.partial_result, error=result,
                    actions_taken=actions_taken, execution_trace=execution_trace,
                )
                db.log_task(conn, task_id, "info", "Task cancelled by user via !stop")
                # No Talk notification needed — !stop already acknowledged
            elif is_policy:
                # Policy refusals are non-retryable: same content will be rejected again.
                # Mark failed and post an alert so the user sees what was blocked.
                db.update_task_status(conn, task_id, "failed", error=result, actions_taken=actions_taken, execution_trace=execution_trace)
                db.log_task(
                    conn, task_id, "warn",
                    f"Task failed: API policy refusal (not retried): {result[:200]}",
                )
                _post_policy_refusal_alert(config, task, result)
                if task.scheduled_job_id:
                    fail_count = db.increment_scheduled_job_failures(
                        conn, task.scheduled_job_id, result,
                    )
                    max_failures = config.scheduler.scheduled_job_max_consecutive_failures
                    if max_failures > 0 and fail_count >= max_failures:
                        # The daemon's own column, never `enabled`: CRON.md
                        # authors that one and the sync writes it back within
                        # the tick, which is what made auto-disable a no-op for
                        # every file-defined job.
                        db.suspend_scheduled_job(conn, task.scheduled_job_id)
                        db.log_task(
                            conn, task_id, "warn",
                            f"Scheduled job auto-disabled after {fail_count} consecutive failures",
                        )
                        logger.warning(
                            "Scheduled job %d auto-disabled after %d failures",
                            task.scheduled_job_id, fail_count,
                        )
                        notification_results.append(_note_job_auto_disabled(
                            conn, task.scheduled_job_id, fail_count,
                        ))
            elif is_shutdown_collateral:
                # Not a task failure — the daemon is going away and took the
                # subprocess with it. Requeue without charging an attempt or
                # setting a backoff; the next daemon claims it immediately.
                db.release_task_for_restart(conn, task_id, result)
                db.log_task(
                    conn, task_id, "warn",
                    "Scheduler shutting down; task subprocess was terminated "
                    "— requeued without charging an attempt",
                )
                logger.warning(
                    "Task %d requeued: killed by shutdown signal (%s)",
                    task_id, result[:120],
                )
                # The next attempt re-runs from the top, so this attempt's
                # deferred-op files must not replay alongside it (ISSUE-074).
                from .executor import get_user_temp_dir
                _purge_deferred_files_for_retry(
                    task, get_user_temp_dir(config, task.user_id),
                )
            elif task.attempt_count < task.max_attempts - 1 and not is_oom \
                    and not is_permanent and not is_sigpipe:
                # Exponential backoff: 1, 4, 16 minutes
                delay = 1 << (task.attempt_count * 2)
                db.set_task_pending_retry(conn, task_id, result, delay)
                db.log_task(conn, task_id, "warn", f"Task failed, will retry in {delay} minutes: {result[:200]}")
                # ISSUE-074: clear any deferred-op files this attempt accumulated
                # so the next attempt starts with a clean slate. Producers append
                # to these files, so without this, eventual success would replay
                # the failed attempt's ops alongside the successful one's. The
                # claim-time backstop above would catch this too; purging at the
                # requeue keeps the disk clean for a task that never comes back.
                from .executor import get_user_temp_dir
                _purge_deferred_files_for_retry(
                    task, get_user_temp_dir(config, task.user_id),
                )
                # The event log is intentionally NOT wiped here: keeping it lets
                # a watching web client survive the retry (its resume cursor
                # stays valid) and see a "retrying" notice. The next attempt's
                # EventWriter resumes seq via get_max_task_event_seq, so there's
                # no UNIQUE(task_id, seq) collision. The retry notice itself is
                # emitted from the terminal-events block below (outside this DB
                # transaction, so the writer's own connection can't contend).
            else:
                db.update_task_status(
                    conn, task_id, "failed",
                    result=task.partial_result, error=result,
                    actions_taken=actions_taken, execution_trace=execution_trace,
                )
                db.log_task(conn, task_id, "error", f"Task failed permanently: {result[:500]}")

                if task.source_type in ("briefing", "scheduled"):
                    # Suppress user-facing error delivery for automated tasks.
                    # Errors are logged to DB and log_channel; no need to confuse users.
                    db.log_task(conn, task_id, "info", "Suppressed error delivery for automated task")
                elif plan_talk and talk_token:
                    # Use user-friendly error message, not raw error
                    friendly_error = _format_error_for_user(result)
                    # A run that died on the clock usually had something worth
                    # reading (ISSUE-372) — deliver it rather than sending an
                    # apology and silently dropping half an hour of work.
                    post_talk_message = _with_partial_work(
                        f"🐙 {friendly_error}", task,
                    )
                    is_failure_notify = True
                elif task.withheld_from_room or email_from_the_user:
                    # An email-only plan with no error channel at all (ISSUE-255,
                    # second arm added by ISSUE-275). The rule beside this branch
                    # — never email errors — assumes a room leg exists to carry
                    # them, and here there is none: the user mails the bot, the
                    # task fails, and nothing tells them anywhere they look.
                    # Routed by `alert` purpose so it reaches whichever surface
                    # the user actually reads (ISSUE-241), and buffered for
                    # delivery after this transaction closes, since an alert
                    # routed to `web` opens a second connection to this database.
                    #
                    # Two spellings of one question — "was the user themselves
                    # waiting for this answer" — because the poller can only
                    # record it on the task in one of the two cases.
                    # `withheld_from_room` covers a self-addressed *thread*
                    # reply. It reads False for self-addressed *first contact*,
                    # correctly and by its own rule (no room was resolved, so
                    # there is nothing for the exchange to be absent from), which
                    # left the commonest case of all — the user mailing their own
                    # bot — in exactly the silence this branch exists to end.
                    #
                    # Still scoped to that question rather than to "the plan is
                    # email-only", which is the wider gate this deliberately does
                    # not take: an external correspondent's reply under
                    # `email_reply_routing = "thread"` has the identical plan and
                    # the identical absent channel, and alerting on it is noise —
                    # a stranger is waiting for that answer, not the user. See
                    # `tests/test_email_self_reply_residue.py::TestAPermanent
                    # FailureReachesTheUser`, which pins both directions.
                    failure_alert = _with_partial_work(
                        f"⚠️ **Your emailed request failed** (task #{task.id})\n\n"
                        f"{_format_error_for_user(result)}\n\n"
                        "Nothing was sent in reply. Resend the mail to try again.",
                        task,
                    )
                    failure_alert_title = f"Your emailed request failed — task #{task.id}"
                # NOTE: We intentionally do NOT email errors to users.
                # Failed tasks routed to email/ntfy only log the error.
                # Receiving error emails is confusing; users can check Talk or retry.
                if plan_file:
                    call_file_handler = True

                # Track scheduled job failure + auto-disable
                if task.scheduled_job_id:
                    fail_count = db.increment_scheduled_job_failures(
                        conn, task.scheduled_job_id, result,
                    )
                    max_failures = config.scheduler.scheduled_job_max_consecutive_failures
                    if max_failures > 0 and fail_count >= max_failures:
                        # The daemon's own column, never `enabled`: CRON.md
                        # authors that one and the sync writes it back within
                        # the tick, which is what made auto-disable a no-op for
                        # every file-defined job.
                        db.suspend_scheduled_job(conn, task.scheduled_job_id)
                        db.log_task(
                            conn, task_id, "warn",
                            f"Scheduled job auto-disabled after {fail_count} consecutive failures",
                        )
                        logger.warning(
                            "Scheduled job %d auto-disabled after %d failures",
                            task.scheduled_job_id, fail_count,
                        )
                        notification_results.append(_note_job_auto_disabled(
                            conn, task.scheduled_job_id, fail_count,
                        ))

    # The transaction above has closed, so the inbox rows raised inside it can
    # be sent. First thing after the `with`, deliberately: everything below it
    # opens further connections of its own, and a buffer that outlives them is a
    # buffer somebody eventually forgets to flush.
    deliver_pending(config, notification_results)

    # Likewise the once-job's CRON.md removal, buffered above so the mount
    # write happens with no lock held. It goes after `deliver_pending` rather
    # than before it, which is a trade rather than an accident: a wedged Talk
    # send defers the file write (and the deletion is then re-armed by the
    # next sync until it lands), while the other order would hold every
    # buffered notice for the FUSE timeout. Neither can lose the *row*
    # deletion, which is already committed.
    if once_job_to_remove is not None:
        _remove_once_job_from_cron_md(config, *once_job_to_remove)

    # Emit terminal task events + notify subscribers (brain path only). On a
    # retry-eligible failure the task isn't done — emit nothing terminal; the
    # On a retry-eligible failure the task isn't done — emit a "retrying" notice
    # (not a terminal frame) instead, so a watching web client sees why it's
    # still working rather than a silent spinner. The log is no longer wiped, so
    # this notice and the next attempt's events (seq resumed) reach the client.
    if event_writer is not None:
        is_cancelled = (not success) and result == "Cancelled by user"
        is_policy = (not success) and _is_policy_refusal(result)
        is_oom = (not success) and "killed (likely out of memory)" in result
        is_requeued = (not success) and _is_shutdown_collateral(result)
        is_permanent_api = (not success) and is_api_error_banner(result) \
            and is_permanent_api_error(result)
        will_retry = (
            (not success)
            and not is_cancelled
            and not is_policy
            and not is_oom
            and not is_requeued
            and not is_permanent_api
            and task.attempt_count < task.max_attempts - 1
        )
        if is_requeued:
            # Same reasoning as the retry notice below: the task isn't done, so
            # no terminal frame — tell the watching client why it stalled.
            event_writer.emit("progress_text", {
                "text": "⏳ Scheduler restarting — this task will resume shortly…",
            })
        elif will_retry:
            # Mirror the backoff the retry branch set (1, 4, 16 min). Reuses the
            # progress_text kind — the frontend already renders it as the live
            # progress line; it shows during the backoff gap, then the next
            # attempt's task_started replaces it with the fresh ack verb.
            delay = 1 << (task.attempt_count * 2)
            event_writer.emit("progress_text", {
                "text": f"⏳ Attempt failed — retrying in {delay} min…",
            })
        if not will_retry and not is_requeued:
            if is_confirmation_request:
                event_writer.emit("confirmation", {"prompt": result})
            elif success:
                # Full answer — see ISSUE-178. The canonical body is stored
                # untruncated in `messages`; the live `result` event must match.
                event_writer.emit("result", {"text": result, "truncated": False})
            elif is_cancelled:
                event_writer.emit("cancelled")
            else:
                event_writer.emit("error", {
                    "message": _error_event_message(result), "stop_reason": "error",
                })
            event_writer.emit("done", {
                "stop_reason": "completed" if success else "error",
                "duration_seconds": round(event_writer.elapsed_seconds(), 1),
                **({"model": task.model_used} if task.model_used else {}),
                **({"msg_id": stored_assistant_msg_id}
                   if stored_assistant_msg_id is not None else {}),
            })
            event_writer.finish()
            # Prune the ephemeral text_delta rows now the canonical result/
            # confirmation/error has landed (web chat streaming). The deltas were
            # a cosmetic live preview; steady state retains zero. Web is the only
            # stream surface that flows through process_one_task (repl runs
            # inline), so plan_web is the gate — push tasks never wrote any.
            if plan_web:
                with db.get_db(config.db_path) as _prune_conn:
                    db.delete_task_events_by_kind(_prune_conn, task_id, "text_delta")
                    db.delete_task_events_by_kind(_prune_conn, task_id, "thinking")
            # Drop any mid-flight steers (`!steer`) that never drained — the task
            # finished or suspended (pending_confirmation) before its next loop
            # boundary. Marking them `dropped` (not deleting) keeps them out of a
            # later execution and visible in audit. Cheap no-op for the common
            # task with no steers.
            try:
                with db.get_db(config.db_path) as _steer_conn:
                    dropped = db.drop_pending_steers(_steer_conn, task_id)
                if dropped:
                    logger.info("Dropped %d undrained steer(s) for task %d", dropped, task_id)
            except Exception:
                logger.debug("drop_pending_steers failed for task %d", task_id, exc_info=True)

    # Process deferred operations (subtasks, transaction tracking) on success,
    # unless the task is awaiting confirmation (drain after the user confirms).
    if success and not is_confirmation_request:
        _drain_deferred_ops(config, task, result)

    # Save briefing digest for deduplication in the next run
    if success and task.source_type == "briefing":
        from .skills.briefing import save_briefing_digest
        parsed_briefing = parse_briefing_json(result)
        digest_text = parsed_briefing["body"] if parsed_briefing else strip_briefing_preamble(result)
        save_briefing_digest(
            task.user_id, config, digest_text,
            conversation_token=task.conversation_token,
        )
        # Archive the rendered briefing for the landing page (module path only).
        _maybe_archive_briefing(
            config, task, result, parsed_briefing,
            title=briefing_title_for_task(config, task),
        )

    # The ack message is left as-is — it shows the last tool call as a compact
    # execution summary. Error / cancelled status edits are handled live by the
    # Talk subscriber's terminal-event handling.

    # Finalize log channel message with completion status
    if log_dests and log_channel_prefix:
        error_msg = result if not success else None
        # Read selected skills from DB (set during execute_task, not on local task object)
        selected_skills = None
        if config.scheduler.log_channel_show_skills:
            try:
                with db.get_db(config.db_path) as _conn:
                    refreshed = db.get_task(_conn, task_id)
                    if refreshed and refreshed.selected_skills:
                        selected_skills = json.loads(refreshed.selected_skills)
            except Exception:
                pass
        from .brain import configured_default_model_effort, resolve_brain_kind
        from .executor import _resolve_effort
        # What actually ran, preferring the brain's own report. `config.model`
        # used to stand in here and named claude_code's model on every line
        # whatever brain produced the answer (ISSUE-418).
        #
        # `task.model_used` leads because it is the only one of the three that
        # survives an in-attempt fallback: the reroute happens inside
        # `run_with_failover` and is invisible from here, so `resolve_brain_kind`
        # below still answers with the *primary's* config and would name
        # claude_code's default for an answer native produced — the same
        # misattribution this change exists to remove. It is also the canonical
        # id rather than the unresolved alias an operator wrote.
        _brain_config = resolve_brain_kind(
            task.source_type, config.brain, override=getattr(task, "brain", None),
        )
        _default_model, _default_effort = configured_default_model_effort(_brain_config)
        _task_model = (task.model or "").strip()
        resolved_model = (
            (getattr(task, "model_used", "") or "").strip()
            or _task_model
            or _default_model
            or None
        )
        # The brain's default effort applies only where the task pinned no
        # *model*, which is the rule both `_resolve_effort` and
        # `ClaudeCodeBrain.with_defaults` implement: an effort chosen for one
        # model need not be valid on another, so a task pinning a model runs
        # with no effort at all. Reporting one here would name a setting the
        # task did not use.
        resolved_effort = _resolve_effort(task, config) or (
            None if _task_model else (_default_effort or None)
        )
        _finalize_log_channel(
            config, task, log_dests, log_channel_prefix,
            log_callback, success, error=error_msg,
            skills=selected_skills,
            model=resolved_model, effort=resolved_effort,
        )

    # Deliver results outside DB context to avoid lock conflicts. The final
    # result is always a separate Talk message (the ack carries progress); the
    # Talk subscriber never posts the result, so no dedup is needed.
    response_msg_id = None
    # A Talk post was attempted and came back with no message id (ISSUE-404).
    # Read after the email leg has had its say, since both write the one
    # buffered `failure_alert`.
    talk_undelivered = False
    if post_talk_message:
        # For a web-origin mirror leg, repost the user's question (attributed)
        # before the reply so the Talk transcript isn't an orphaned answer. Pure
        # Talk-surface post — never persisted to the canonical messages store.
        # Suppressed when the web process already posted the turn *as the user*
        # at ingest (post-as-user mirroring): the user turn's `talk` external-id
        # stamp is the signal. A framework-DB read — the scheduler never touches
        # the user token.
        #
        # An email-origin turn needs the same thing for the same reason, and did
        # not get it: the room now holds the question as a canonical row, but
        # Talk renders from Nextcloud rather than from that store, so Talk was
        # left showing an answer with nothing above it and no sign of who it was
        # answering (ISSUE-247). What used to carry that on Talk was
        # `_notify_confirmed_email_result`'s `Email reply sent to <sender>`
        # prefix, and only for a gated task.
        _repost = None
        if _talk_is_mirror and task.source_type == "web" and task.prompt:
            _repost = _format_mirror_user_repost(config, task)
        elif task.source_type == "email" and transcript_token:
            _repost = _format_email_user_repost(config, task, talk_token)
        if _repost:
            _user_posted = False
            try:
                with db.get_db(config.db_path) as conn:
                    _user_posted = db.user_turn_has_external_id(
                        conn, task_id, "talk",
                    )
            except Exception as e:
                logger.debug(
                    "user-turn external-id check failed for task %d: %s",
                    task_id, e,
                )
            if not _user_posted:
                run_coro(post_result_to_talk(
                    config, task, _repost,
                    reference_id=f"istota:task:{task.id}:prompt",
                    target_token=talk_token,
                ))
        response_msg_id = run_coro(post_result_to_talk(
            config, task, post_talk_message, use_reply_threading=True,
            reference_id=f"istota:task:{task.id}:result",
            target_token=talk_token,
        ))
        # Mirror what Talk just showed into that room's web transcript, for the
        # results `_store_room_turn` can't reach. Gated on the post having
        # landed, like `_dispatch`'s talk leg: the mirror records a Talk message,
        # so a failed post has nothing to record. Best-effort and gated on room
        # existence inside the helper — the delivery has already happened.
        if post_talk_mirror_body and response_msg_id:
            from .notifications import mirror_talk_to_room
            mirror_talk_to_room(
                config, talk_token, post_talk_mirror_body,
                talk_message_id=response_msg_id,
            )
        # `TalkTransport.deliver` catches every exception, logs one line and
        # returns None — the right contract, since a Nextcloud outage must not
        # turn a successful task into a failed one, but nothing read the value
        # it returns to say so. Every site below is `if response_msg_id:` with
        # no else, so a `ReadTimeout` on the final post was indistinguishable
        # here from a task that had no Talk leg at all and the answer was lost
        # in silence (ISSUE-404).
        #
        # `post_talk_message` is the predicate, not the `plan_talk and
        # talk_token` pair the email arm keys on: the silent-scheduled-job
        # branch sets `post_talk_message` under `if talk_token:` alone, since it
        # bypasses the delivery plan entirely, and that answer is just as lost.
        # Being inside this block is the same test, stated once.
        talk_undelivered = response_msg_id is None
    # Store bot's response message ID for reply tracking
    if response_msg_id and not is_failure_notify:
        try:
            with db.get_db(config.db_path) as conn:
                db.update_talk_response_id(conn, task_id, response_msg_id)
        except Exception as e:
            logger.debug("Failed to store talk_response_id for task %d: %s", task_id, e)

    # Record the reply's Talk post id in the assistant message's external_ids
    # ledger. Two consumers: loop-prevention (explicit no-echo invariant for a
    # future surface that doesn't self-filter bot posts the way Talk does), and
    # the Talk→web read-sync cap (`room_max_talk_synced_message_id`), which only
    # advances the web cursor to the newest *stamped* row.
    #
    # Runs for any Talk leg, mirror or native. It was mirror-only through
    # ISSUE-161, which left Talk-origin replies unstamped: the cap then stalled
    # at the user's own inbound turn and the reply they'd just read in Talk
    # stayed unread in web forever.
    #
    # Guarded on the post having gone to the room's *own* bound Talk channel: a
    # task force-routed elsewhere (`output_target="talk:<other>"`) returns a
    # post id from a foreign conversation, and writing that into this room's
    # ledger would wrongly advance its read cap.
    if response_msg_id and task.conversation_token and talk_token:
        try:
            with db.get_db(config.db_path) as conn:
                bindings = db.list_room_bindings(conn, task.conversation_token)
                talk_ref = next(
                    (b.surface_ref for b in bindings if b.surface == "talk"), None,
                )
                if talk_ref == talk_token:
                    row = conn.execute(
                        "SELECT id FROM messages WHERE room_token = ? "
                        "AND task_id = ? AND role = 'assistant' LIMIT 1",
                        (task.conversation_token, task_id),
                    ).fetchone()
                    if row:
                        db.set_message_external_id(
                            conn, row["id"], "talk", str(response_msg_id),
                        )
        except Exception as e:
            logger.debug("Failed to record talk external_id for task %d: %s", task_id, e)

    # Cache the result so it's immediately available for context building.
    # The result is always posted as its own message, so its real Talk ID is
    # the cache key. The upsert preserves :result tags so the poller won't
    # overwrite them.
    cache_msg_id = response_msg_id

    if success and talk_token and not is_failure_notify and cache_msg_id:
        try:
            with db.get_db(config.db_path) as conn:
                cache_msg = {
                    "id": cache_msg_id,
                    "actorId": config.talk.bot_username,
                    "actorDisplayName": config.talk.bot_username,
                    "actorType": "users",
                    "message": post_talk_message or result,
                    "messageType": "comment",
                    "messageParameters": {},
                    "timestamp": int(time.time()),
                    "referenceId": f"istota:task:{task.id}:result",
                    "deleted": False,
                }
                db.upsert_talk_messages(conn, talk_token, [cache_msg])
        except Exception as e:
            logger.warning("Failed to cache result message for task %d: %s", task_id, e)
    if post_email:
        email_subject = None
        if task.source_type == "briefing":
            pb = parse_briefing_json(result)
            email_result = pb["body"] if pb else strip_briefing_preamble(result)
            email_subject = briefing_title_for_task(config, task)
        else:
            email_result = result
        email_ok = asyncio.run(post_result_to_email(
            config, task, email_result, subject=email_subject,
        ))
        if not email_ok:
            with db.get_db(config.db_path) as conn:
                db.update_task_status(conn, task_id, "failed", error="Email delivery failed", actions_taken=actions_taken, execution_trace=execution_trace)
                db.log_task(conn, task_id, "error", "Task completed but email delivery failed")
            if (task.withheld_from_room or email_from_the_user) \
                    and not (plan_talk and talk_token and response_msg_id):
                # The answer exists and nothing carries it (ISSUE-255, second arm
                # added by ISSUE-275 — see the permanent-failure branch above for
                # why `withheld_from_room` alone stopped covering it). With a
                # room leg that *landed*, the assistant row is stored and the
                # answer is in front of the user, so a failed send costs the mail
                # copy alone; with an email-only plan `tasks.result` is the only
                # copy left, and nothing puts it in front of the user. Carry the
                # body itself rather than a pointer — the point is that the
                # answer survives the failure, not that its loss is announced.
                #
                # The guard is three terms and each removes a different way of
                # believing an answer landed when it did not. `plan_talk` alone
                # is not enough — a plan can carry a Talk leg whose channel
                # resolves to None, so `talk_token` is the pair the
                # permanent-failure branch also keys on. And the pair alone is
                # not enough either (ISSUE-404): `TalkTransport.deliver` returns
                # None on a `ReadTimeout` exactly as it does on a room that
                # resolved to nothing, so `response_msg_id` is what makes this a
                # question about the post rather than about the plan. With both
                # legs down the pair suppressed the last notice there was, and
                # an emailed request whose answer reached neither surface was
                # silent on both.
                # Unwrapped for the same reason the room transcript unwraps it
                # (ISSUE-247): an email task's `result` may *be* the
                # `{"subject","body","format"}` envelope the send path parses, and
                # a notice promising the answer must not hand over a JSON blob.
                # `email_transcript_body` prefers `result` over the deferred file
                # deliberately — the result is the bot's answer to its user, which
                # is exactly what this is recovering.
                from .transport.email.outbound import email_transcript_body
                failure_alert = (
                    f"⚠️ **Could not send the email reply** (task #{task.id})\n\n"
                    "The answer is below so it is not lost:\n\n"
                    f"{email_transcript_body(email_result)}"
                )
                failure_alert_title = f"Could not send the email reply — task #{task.id}"
    if post_ntfy:
        from .transport._types import DeliveryOptions
        ntfy_title = f"Task {task_id}"
        if task.source_type == "briefing":
            pb = parse_briefing_json(result)
            ntfy_result = pb["body"] if pb else strip_briefing_preamble(result)
            ntfy_title = briefing_title_for_task(config, task)
        else:
            ntfy_result = result
        ntfy_transport = registry.get("ntfy")
        if ntfy_transport is not None:
            run_coro(ntfy_transport.deliver(
                "", ntfy_result, task=task,
                options=DeliveryOptions(title=ntfy_title),
            ))
    if call_file_handler:
        # The transport re-reads the task's terminal status to derive success.
        file_transport = registry.get("istota_file")
        if file_transport is not None:
            run_coro(file_transport.deliver("", result, task=task))
    if post_web:
        # A foreign task (e.g. an alert or a reply routed into a room it is NOT
        # conversing in) pushed into a web room: deliver as an unsolicited system
        # message via WebTransport.deliver. A push into the task's own room is
        # excluded (`web_foreign_dests`) — that room's answer is the assistant
        # row written above (ISSUE-164). The own-origin web-source case never
        # reaches this branch at all (its web leg resolved to stream).
        web_transport = registry.get("web")
        if web_transport is not None:
            if task.source_type == "briefing":
                pb = parse_briefing_json(result)
                web_result = pb["body"] if pb else strip_briefing_preamble(result)
            else:
                web_result = result
            for dest in web_foreign_dests:
                run_coro(web_transport.deliver(dest.channel, web_result, task=task))

    # A confirmation prompt whose Talk push failed already has the right notice
    # written, so this is the deferred half of a decision made above rather than
    # a new one (ISSUE-404). The branch that parks the task writes the
    # `confirmation` row unconditionally and withholds its push only because
    # `post_talk_message` was going to carry the question — so a post that never
    # landed leaves an actionable, object-backed row that nothing delivered, and
    # the task dies at `expire_stale_confirmations` two hours later with the
    # question having reached nobody. `deliver_pending` re-reads the row and
    # sends only while it is still open, which is what makes a second call safe.
    #
    # This has to run ahead of the generic arm and take the case away from it: a
    # `task_alert` here would be strictly worse than nothing, since it is
    # non-actionable and auto-resolves on being seen, so the copy the user opens
    # would close itself in front of the `confirmation` row that carries the
    # `!confirm` verbs.
    #
    # Deliberately not gated on `_talk_is_mirror`. An email-origin confirmation
    # is posted on its mirror leg on purpose — that leg is the only push surface
    # that can reach the user, because the email leg must never carry the
    # question — so the carve-out below would leave exactly the shape the
    # confirmation gate's own comment records fixing.
    if talk_undelivered and held_notification is not None:
        deliver_pending(config, [held_notification])

    # A Talk leg that carried the message and posted nothing (ISSUE-404). Last,
    # after every other leg, because there is one buffered `failure_alert` and
    # the email arms above have the stronger claim on it — they also mark the
    # task failed, and they carry the same body.
    #
    # Not a mirror leg. A mirror is the room fan-out of a web- or email-origin
    # task, so Talk is the copy and the answer is somewhere else: `_store_room_
    # turn` has written the canonical `messages` row that the web room renders,
    # and for a web-origin task the stream carried it live. Alerting there would
    # tell a user their answer was lost while it sits in the room they are
    # looking at, on every web-origin task for the length of a Nextcloud outage,
    # on the one channel that has to stay worth reading. The canonical row is
    # the backstop, and not the email arm above — that one is additionally gated
    # on the answer being the *user's* own, so it says nothing about an external
    # correspondent's reply, which is the shape where the mirror is the user's
    # only view.
    #
    # It fires for every source type, including a briefing or a cron job, and
    # that is a decision rather than an omission: what this arm exists to stop is
    # content that was generated and delivered nowhere, and an automated task's
    # answer is lost exactly as thoroughly as an interactive one's. The sibling
    # suppression at the permanent-failure branch is about *errors*, which is a
    # different thing to put in front of a user. Where `alert` routes back to
    # Talk the send fails too and the row stays open, which is the outcome the
    # inbox exists for.
    #
    # Two accepted trades, both stated rather than fixed. The alert can be
    # routed to the very room that just refused the post, in which case only the
    # row survives. And a long message is posted part by part, so a failure on
    # part 3 of 5 returns None with parts 1 and 2 already in the room and this
    # delivers the whole body again — a duplicate beats a silently truncated
    # answer, and `deliver` reports no more than it knows.
    #
    # It never touches the task's status, deliberately unlike the email arm.
    # This runs on three shapes with three different statuses — a completed
    # answer, a `failed` task's apology, and (above) a parked confirmation — so
    # there is no one status to write, and on the common one the log channel has
    # already posted a completed line and the assistant row is already stored.
    # Flipping it would leave those records disagreeing about whether the task
    # ran, and would offer a re-run — another model call — for a failure that
    # was never in the model. The inbox row is what makes the loss durable.
    if talk_undelivered and held_notification is None \
            and not _talk_is_mirror and failure_alert is None:
        # "message", not "reply": on the permanent-failure path this body is the
        # apology for a task that failed, not an answer to anything.
        failure_alert = (
            f"⚠️ **Could not post to Talk** (task #{task.id})\n\n"
            "The message is below so it is not lost:\n\n"
            f"{post_talk_message}"
        )
        failure_alert_title = f"Could not post to Talk — task #{task.id}"

    if failure_alert:
        # Last, and after every DB transaction above has closed. Best-effort by
        # construction: this is the recovery path for a task that already has
        # nowhere to deliver, so a failure here leaves things exactly as they
        # were before ISSUE-255 rather than making them worse.
        #
        # The row goes first and stands whatever the send does. `alert` is the
        # last channel a task with no room leg has, and `send_notification`
        # returns False when the user configured no destination for it — which is
        # how this notice used to disappear entirely, on the one path whose whole
        # purpose is that the answer is not lost. The send itself is unchanged:
        # it carries the full unflattened body, because the point of the second
        # branch is to hand the user their answer, and the row carries the
        # flattened, capped record of it (the full text stays in `tasks.result`).
        notification_id = _write_undelivered_row(
            config, task, failure_alert_title, failure_alert,
        )
        try:
            delivered = send_notification(
                config, task.user_id, failure_alert, purpose="alert",
            )
        except Exception as e:
            delivered = False
            logger.warning(
                "Failed to alert user about task %d (user=%s): %s",
                task_id, task.user_id, e,
            )
        if delivered and notification_id is not None:
            try:
                with db.get_db(config.db_path) as conn:
                    mark_delivered(conn, [notification_id])
            except Exception:
                logger.debug(
                    "could not stamp the undelivered-result notification",
                    exc_info=True,
                )

    return task_id, success


def _format_mirror_user_repost(config: Config, task: db.Task) -> str:
    """Attributed repost of a web-origin user turn for a Talk mirror leg.

    The bot can't post as the user in Talk, so a web-origin turn mirrored into a
    bound Talk room would otherwise show an orphaned bot answer with no visible
    question. The bot reposts the question (attributed) as its own message, then
    its reply in the next post. This is a pure Talk-surface artifact — it is
    never written to the canonical `messages` store, so web history / context is
    unaffected.
    """
    uc = config.get_user(task.user_id)
    display = uc.display_name if uc and uc.display_name else task.user_id
    return f"💬 {display} (via web):\n{task.prompt}"


# A subject is an attacker-supplied header of no fixed length, and this one goes
# into a Talk post. Same cap the web transcript's external-turn header uses.
_TALK_SUBJECT_MAX_CHARS = 120


def _format_email_user_repost(
    config: Config, task: db.Task, talk_token: str | None,
) -> str | None:
    """Provenance header for an email answer posted into a Talk room.

    The room holds the question as a canonical `role='user'` row, and the web
    view renders it as a collapsed "External email" card. Talk renders from
    Nextcloud instead, so without this it shows the answer alone — a bot
    replying to nothing, with no indication that a stranger wrote in
    (ISSUE-247). Returns None when there is nothing to attribute.

    Sender and subject only, never the body. The body is the task prompt
    verbatim, wrapper and untrusted-input guard included, which is the right
    thing to re-pair into LLM context and the wrong thing to paste into a room;
    the mail itself is one click away in web chat and in the mailbox. The sender
    goes through `db.external_email_sender`, so what renders is an addr-spec or
    the fixed unattributed sentinel and never a raw `From:` with a display name
    in it.
    """
    if task.source_type != "email":
        return None
    try:
        with db.get_db(config.db_path) as conn:
            record = db.get_email_for_task(conn, task.id)
            if record is None:
                return None
            own = db.own_addresses_without_config(conn, task.user_id)
            uc = config.get_user(task.user_id)
            if uc and uc.email_addresses:
                own = list(uc.email_addresses)
            sender = db.external_email_sender(record.sender_email, own)
            subject = (record.subject or "").strip()
    except Exception as e:
        logger.debug("email repost header failed for task %d: %s", task.id, e)
        return None
    if not sender:
        # The user's own mail to their own address — not an outside voice, so
        # there is nothing to mark. Same line `resolve_author` draws.
        return None
    line = f"📧 Email from {sender}"
    if subject:
        line += f" — {subject[:_TALK_SUBJECT_MAX_CHARS]}"
    return line


async def post_result_to_talk(
    config: Config, task: db.Task, message: str,
    *, use_reply_threading: bool = False,
    reference_id: str | None = None,
    target_token: str | None = None,
) -> int | None:
    """Post a result message to Talk. Returns the Talk message ID of the last sent message.

    Long messages are split into multiple parts sent sequentially.

    `target_token` overrides `task.conversation_token` for the actual post —
    use it when the task's stored token isn't a real Talk room (see
    `_talk_target_for_delivery` for the email-source synthetic-token case).

    Thin shim over ``TalkTransport.deliver`` — splitting, group-chat threading,
    and the ``TalkClient`` construction live in ``transport/talk/``.
    """
    from .transport.talk import TalkTransport
    token = target_token or task.conversation_token
    return await TalkTransport(config).deliver(
        token, message, task=task,
        threaded=use_reply_threading, reference_id=reference_id,
    )


async def post_result_to_email(
    config: Config, task: db.Task, message: str, *, subject: str | None = None,
) -> bool:
    """Send a task result as an email reply / fresh email. Returns True on success.

    ``subject`` overrides the subject for a fresh (non-reply) send — the
    briefing path passes its deterministic title so the inbox and the web
    archive agree.

    Thin shim over the email transport — structured-output parsing, thread-reply
    routing, and sent-email recording live in ``transport/email/outbound.py``
    (mirrors ``post_result_to_talk`` / ``TalkTransport.deliver``). Calls the
    bool-returning ``deliver_email_result`` directly rather than
    ``EmailTransport.deliver`` because the scheduler's callers check the success
    flag, which the ``Transport.deliver`` protocol (``int | None``) discards for
    a surface with no message-id concept."""
    from .transport.email import deliver_email_result
    return await deliver_email_result(config, task, message, subject=subject)


def _deferred_briefing_placeholder(briefing_name: str) -> str:
    """Stand-in prompt stored on a deferred briefing task (ISSUE-143).

    The real prompt is built in the executor at worker-pickup time. This
    placeholder is only what an inspector (`istota show <id>`) sees on the
    stored row, and the fallback the executor keeps if the briefing config
    can't be resolved or its prompt build fails.
    """
    return f"Generate the '{briefing_name}' briefing."


def briefing_title_for_task(config: Config, task) -> str:
    """The deterministic display title for a finished briefing task.

    Every consumer of a briefing's output (archive entry, email subject, ntfy
    title) calls this rather than reading a model-supplied subject, so the same
    run can't be titled three different ways. Pure function of the task row +
    config, so the independent call sites agree without plumbing.

    Dated from the task's creation time (the cron fire), rendered in the user's
    timezone.
    """
    from .briefings.generate import resolve_briefing_title

    when = None
    raw = getattr(task, "created_at", None)
    if raw:
        try:
            when = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            when = None  # Fall through to "now" — a title is never worth failing over.
    return resolve_briefing_title(
        config, task.user_id, getattr(task, "briefing_name", "") or "", when,
    )


def _maybe_archive_briefing(
    config: Config, task, result: str, parsed, title: str | None = None,
) -> None:
    """Archive a rendered briefing to the module's ``briefing_archive``.

    Only module-path briefings are archived: the module must be enabled for the
    user AND the briefing must have content blocks (a legacy no-blocks briefing
    is not archived — it wouldn't appear on the landing page anyway). Reads the
    per-block provenance the executor stashed and prunes past retention. Fully
    best-effort — the task has already delivered, so a failed archive is logged
    and swallowed.
    """
    if not getattr(task, "briefing_name", None):
        return
    try:
        from . import briefings as briefings_module
        from .briefings import db as bdb
        from .briefings.generate import archive_briefing
    except Exception:  # noqa: BLE001
        return

    try:
        ctx = briefings_module.resolve_for_user(task.user_id, config)
    except briefings_module.UserNotFoundError:
        return  # module disabled
    except Exception as e:  # noqa: BLE001
        logger.warning("briefings archive: resolve failed for %s: %s", task.id, e)
        return

    # Only archive when the briefing actually has blocks (module path).
    try:
        with bdb.connect(ctx.db_path) as conn:
            if not bdb.list_blocks(conn, task.briefing_name, with_sources=False):
                return
    except Exception:  # noqa: BLE001
        return

    # The archive's `subject` is the deterministic title, not whatever the
    # model felt like calling it — the same string the email subject uses.
    subject = title or briefing_title_for_task(config, task)
    body = parsed.get("body") if parsed else strip_briefing_preamble(result)
    if not body:
        return

    # Per-block provenance the executor stashed (best-effort).
    #
    # The one consumer that outlives its task: `execute_task` has returned by
    # the time this runs, which is why nothing deletes the control directory
    # on the way out and why the unlink below is here rather than there.
    #
    # `get_task_control_dir`, not `ensure_*`: this is a read, and creating a
    # directory to discover it is empty would be the wrong shape. `None` (an
    # unresolvable user id) leaves `block_meta` empty, the same as a missing
    # file.
    #
    # The `except` swallows a wrong path silently — the archive is written
    # either way and nothing is logged — so the only thing that catches this
    # end pointing somewhere the writer does not is
    # `tests/test_briefings_generate.py::TestSchedulerArchive` asserting the
    # archived `block_meta` is non-empty.
    block_meta: dict = {}
    try:
        from .executor import get_task_control_dir
        control_dir = get_task_control_dir(config, task.user_id, task.id)
        meta_path = control_dir / "briefing_meta.json" if control_dir else None
        if meta_path is not None and meta_path.exists():
            import json as _json
            # `encoding="utf-8"` rather than the locale's, to match the
            # writer: `_write_control_file` names UTF-8 explicitly. Moot only
            # while `json.dumps` keeps its `ensure_ascii=True` default, and a
            # later `ensure_ascii=False` would otherwise lose provenance
            # silently on a non-UTF-8 daemon — swallowed by the `except`
            # below, which is the failure this whole stage is shaped around.
            block_meta = _json.loads(
                meta_path.read_text(encoding="utf-8")
            ).get("block_meta", {})
            meta_path.unlink()
    except Exception:  # noqa: BLE001
        block_meta = {}

    delivered_to = [d.surface for d in parse_output_target(task.output_target or "")]
    try:
        archive_briefing(
            ctx,
            briefing_name=task.briefing_name,
            subject=subject,
            body_md=body,
            task_id=task.id,
            block_meta=block_meta,
            delivered_to=delivered_to,
            retention_days=config.briefings.archive_retention_days,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("briefings archive write failed for %s: %s", task.id, e)


def check_briefings(db_path, app_config: Config) -> list[int]:
    """
    Check for briefings that should run and queue them as tasks.

    The slow network pre-fetch that builds the briefing prompt (news, yfinance,
    FinViz, IMAP) is NOT done here — it is deferred to the executor when a
    background worker picks the task up (ISSUE-143). This keeps the scheduler
    dispatch thread free: a slow or unreachable briefing upstream can no longer
    stall `pool.dispatch()` and starve task processing for every room. The task
    carries only the briefing identity (`briefing_name`); the worker resolves
    the live config and builds the prompt.

    Args:
        db_path: Path to the database file
        app_config: Application config with user briefings

    Returns:
        List of created task IDs
    """
    # Phase 1: Short DB read — check which briefings are due
    due_briefings: list[tuple[str, str, "BriefingConfig"]] = []

    with db.get_db(db_path) as conn:
        for user_id, user_config in app_config.users.items():
            briefings = get_briefings_for_user(app_config, user_id)
            if not briefings:
                continue

            # Resolve from the live user_profiles DB row (reusing conn) so a
            # web-UI timezone change moves the briefing schedule without a
            # daemon restart (ISSUE-099).
            user_tz_str = app_config.resolve_user_timezone(user_id, conn=conn)

            try:
                user_tz = ZoneInfo(user_tz_str)
            except Exception:
                user_tz = ZoneInfo("UTC")
                user_tz_str = "UTC"

            now = _now(user_tz)
            # Use naive wall-clock times for croniter to avoid DST bugs.
            # croniter miscomputes next fire time when a tz-aware datetime
            # crosses a DST boundary (e.g. PST→PDT), causing double-fires.
            now_naive = now.replace(tzinfo=None)

            for briefing in briefings:
                if not briefing.cron:
                    continue
                # Skip a briefing with no deliverable route: its only
                # destination(s) are bare Talk (no inline channel) and no
                # conversation_token is configured. Grammar-aware over the full
                # output_target descriptor (talk / both / all / email,talk /
                # talk:<room> / …) — an email/ntfy leg or an inline talk:<room>
                # keeps it alive (the bare Talk leg then DMs at delivery).
                _dests = parse_output_target(briefing.output)
                _talk_needs_room = any(
                    d.surface == "talk" and not d.channel for d in _dests
                )
                _has_other_route = any(
                    d.surface != "talk" or d.channel for d in _dests
                )
                if (
                    _talk_needs_room
                    and not briefing.conversation_token
                    and not _has_other_route
                ):
                    continue

                should_run = False
                last_run_at = db.get_briefing_last_run(conn, user_id, briefing.name)

                if last_run_at:
                    last_run = datetime.fromisoformat(last_run_at)
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                    base = last_run.astimezone(user_tz).replace(tzinfo=None)
                    cron = croniter(briefing.cron, base)
                    next_run = cron.get_next(datetime)
                    should_run = now_naive >= next_run
                else:
                    today_start = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
                    cron = croniter(briefing.cron, today_start)
                    next_run = cron.get_next(datetime)
                    should_run = now_naive >= next_run

                if should_run and _is_stale_fire(
                    f"briefing {user_id}/{briefing.name}",
                    next_run, now_naive,
                    app_config.scheduler.cron_max_staleness_minutes,
                ):
                    db.set_briefing_last_run(conn, user_id, briefing.name)
                    continue

                if should_run:
                    due_briefings.append((user_id, user_tz_str, briefing))

    if not due_briefings:
        return []

    # Phase 2: Short DB write — create tasks and update last_run. The prompt
    # is built later, in the executor, off the dispatch thread (ISSUE-143).
    created_tasks = []
    with db.get_db(db_path) as conn:
        for user_id, _user_tz_str, briefing in due_briefings:
            # One unusable user id costs its own briefing, not the batch. The
            # loop shares a transaction, so an escaping `ValueError` would
            # discard every earlier user's task *and* their `last_run` stamp
            # and repeat the whole thing next tick, forever (ISSUE-402).
            # `create_task` validates before it executes any statement, so
            # nothing is half-written here.
            try:
                task_id = db.create_task(
                    conn,
                    prompt=_deferred_briefing_placeholder(briefing.name),
                    user_id=user_id,
                    source_type="briefing",
                    conversation_token=briefing.conversation_token,
                    output_target=briefing.output,
                    priority=8,
                    queue="background",
                    briefing_name=briefing.name,
                )
            except ValueError as e:
                logger.error(
                    "Briefing '%s' skipped: %s", briefing.name, e,
                )
                continue
            db.set_briefing_last_run(conn, user_id, briefing.name)
            created_tasks.append(task_id)

    return created_tasks


def check_shared_blocks(config: Config, *, run_inline: bool = False) -> list[str]:
    """Generate any due module-owned shared briefing blocks.

    A shared block is generated *once globally* (no user) and its content written
    into ``shared_kv`` for per-user briefings to read (shared-kv-curated-content
    spec, Stage 5). Cron is evaluated in the configured shared-block timezone
    (``[briefings] shared_block_timezone``, default UTC) — global, no per-user
    timezone.

    Like ``check_briefings``, the slow gather (browse ~60s / IMAP) + the Brain
    call are kept off the dispatch thread: a due block is handed to a short-lived
    worker thread (the ``_send_operator_alert`` / sleep-cycle pattern). ``last_run``
    is stamped up front so a slow or failed generation does not re-fire every
    tick. ``run_inline=True`` runs generation synchronously (tests).

    Returns the list of due block names.
    """
    blocks = getattr(config, "briefing_shared_blocks", None) or []
    if not blocks:
        return []

    tz_str = (config.briefings.shared_block_timezone or "").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        logger.warning(
            "shared_block_timezone %r invalid; falling back to UTC", tz_str,
        )
        tz = ZoneInfo("UTC")
    now_naive = _now(tz).replace(tzinfo=None)
    due = []
    with db.get_db(config.db_path) as conn:
        for block in blocks:
            if not getattr(block, "enabled", True) or not block.cron:
                continue
            last_run_at = db.get_briefing_shared_block_last_run(conn, block.name)
            if last_run_at:
                last_run = datetime.fromisoformat(last_run_at)
                if last_run.tzinfo is not None:
                    last_run = last_run.astimezone(tz).replace(tzinfo=None)
                base = last_run
            else:
                base = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
            next_run = croniter(block.cron, base).get_next(datetime)
            should_run = now_naive >= next_run
            if should_run and _is_stale_fire(
                f"shared block {block.name}", next_run, now_naive,
                config.scheduler.cron_max_staleness_minutes,
            ):
                db.set_briefing_shared_block_last_run(
                    conn, block.name, now_naive.isoformat(),
                )
                continue
            if should_run:
                due.append(block)
        # Stamp last_run up front (mirrors check_briefings' phase-2 stamp) so a
        # slow/failed generation doesn't re-fire on the next tick.
        for block in due:
            db.set_briefing_shared_block_last_run(
                conn, block.name, now_naive.isoformat(),
            )

    for block in due:
        if run_inline:
            _generate_shared_block(config, block)
        else:
            threading.Thread(
                target=_generate_shared_block, args=(config, block),
                name=f"shared-block-{block.name}", daemon=True,
            ).start()
    return [b.name for b in due]


def _generate_shared_block(config: Config, block) -> None:
    """Gather + synthesize one shared block and write it to shared_kv.

    A ``None`` result (no usable sources / all-empty gather / failed Brain call)
    skips the write and leaves the prior value intact — a transient upstream
    outage degrades to last-known-good rather than blanking the section. Runs on
    a short-lived worker thread; never raises into the caller.

    ISSUE-181: a ``synthesis`` block needs the primary brain, so when it's
    degraded (breaker open) we skip the whole gather+synthesize — the expensive
    browse/IMAP fetch would be wasted and the brain call would fail anyway. The
    prior shared_kv value is kept (last-known-good); one operator alert already
    fired when the breaker opened. ``structured`` blocks never touch the brain
    (verbatim source concatenation), so they still generate when degraded.
    """
    from istota.brain import primary_brain_unavailable
    from istota.briefings.shared_blocks import SYSTEM_IDENTITY, run_shared_block
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE

    if getattr(block, "render_mode", "synthesis") != "structured":
        available, _reason = primary_brain_unavailable(config.brain)
        if not available:
            logger.info(
                "shared block %s skipped — primary brain unavailable "
                "(keeping prior content)",
                block.name,
            )
            return

    try:
        result = run_shared_block(block, config)
    except Exception as e:  # noqa: BLE001
        logger.error("shared block %s generation failed: %s", block.name, e)
        return
    if result is None:
        logger.warning(
            "shared block %s: nothing generated, keeping prior content", block.name,
        )
        return
    try:
        with db.get_db(config.db_path) as conn:
            db.shared_kv_set(
                conn, SHARED_BLOCK_NAMESPACE, block.name,
                json.dumps(result), SYSTEM_IDENTITY,
            )
        logger.info("shared block %s regenerated", block.name)
    except Exception as e:  # noqa: BLE001
        logger.error("shared block %s write failed: %s", block.name, e)


def _parse_shared_kv_target(target: str) -> tuple[str, str]:
    """Parse a ``publish_shared_kv`` target into ``(namespace, key)``.

    ``"<ns>/<key>"`` → split on the first slash; a bare ``"<key>"`` →
    (``briefing_shared_blocks``, key) so a publish job populates a shared block
    by name with no ceremony.
    """
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE

    target = (target or "").strip()
    if "/" in target:
        ns, _, key = target.partition("/")
        return ns.strip(), key.strip()
    return SHARED_BLOCK_NAMESPACE, target


def _publish_result_to_shared_kv(
    conn, config: Config, task, job, result_text: str,
    *, notifications: list[RaiseResult | None] | None = None,
) -> bool:
    """Publish a completed job's result text into ``shared_kv`` (gated).

    A post-success gated write in the ``_process_deferred_*`` genre — NOT an
    ``output_target`` surface (a publish job's own delivery is orthogonal). The
    identity is always the task's ``user_id`` (never anything from job payload).

    Returns ``True`` when the publish succeeded or was cleanly skipped
    (empty-result → keep last-known-good), ``False`` when it failed and recorded
    a job failure — the caller then withholds the success reset so the failure
    survives (observability + de-admin auto-disable).

    * Unauthorized (``not is_shared_kv_writer``) → **fail loud**: ERROR log,
      operator alert, job-failure increment → ``False``.
    * Empty/whitespace result → skip (preserve last-known-good), INFO → ``True``.
    * Never raises out of ``process_one_task``.
    """
    ns, key = _parse_shared_kv_target(job.publish_shared_kv)
    if not key:
        logger.error(
            "publish_shared_kv job=%s has no key in target %r; skipping",
            job.name, job.publish_shared_kv,
        )
        _record_publish_failure(
            conn, config, task, job, "publish_shared_kv missing key",
            notifications=notifications,
        )
        return False

    if not config.is_shared_kv_writer(task.user_id):
        logger.error(
            "publish_shared_kv unauthorized user=%s job=%s key=%s/%s",
            task.user_id, job.name, ns, key,
        )
        _record_publish_failure(
            conn, config, task, job,
            f"publish_shared_kv unauthorized for user {task.user_id}",
            notifications=notifications,
        )
        try:
            user = _operator_alert_user(config)
            if user:
                _send_operator_alert(
                    config, user,
                    f"⚠️ Scheduled job '{job.name}' tried to publish shared content "
                    f"as non-writer {task.user_id} (key {ns}/{key}). Nothing was "
                    f"written.",
                )
        except Exception as e:  # noqa: BLE001
            logger.error("publish_shared_kv alert failed: %s", e)
        return False

    if not (result_text or "").strip():
        logger.info(
            "publish_shared_kv job=%s: empty result, keeping prior value at %s/%s",
            job.name, ns, key,
        )
        return True

    value = json.dumps({
        "text": result_text,
        "trusted": bool(job.publish_shared_kv_trusted),
    })
    try:
        db.shared_kv_set(conn, ns, key, value, task.user_id)
        logger.info(
            "publish_shared_kv job=%s wrote %s/%s (%d bytes, trusted=%s)",
            job.name, ns, key, len(result_text), bool(job.publish_shared_kv_trusted),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("publish_shared_kv write failed job=%s key=%s/%s: %s",
                     job.name, ns, key, e)
        try:
            _record_publish_failure(
                conn, config, task, job, f"shared_kv write failed: {e}",
                notifications=notifications,
            )
        except Exception:  # noqa: BLE001
            pass
        return False


def _record_publish_failure(
    conn, config: Config, task, job, error: str,
    *, notifications: list[RaiseResult | None] | None = None,
) -> None:
    """Increment a job's failure counter for an unauthorized/failed publish, and
    auto-disable after the configured threshold (mirrors the normal failure path).

    ``notifications`` is `process_one_task`'s buffer. Threaded down rather than
    delivered here because this runs inside that function's write transaction —
    the third of the three auto-disable sites, and the one whose distance from
    the `with` block makes it easiest to forget. A caller that passes nothing
    still disables the job; it just tells nobody, which is the behaviour this
    parameter exists to end.
    """
    fail_count = db.increment_scheduled_job_failures(conn, job.id, error)
    max_failures = config.scheduler.scheduled_job_max_consecutive_failures
    if max_failures > 0 and fail_count >= max_failures:
        db.suspend_scheduled_job(conn, job.id)
        logger.warning(
            "Scheduled job %d auto-disabled after %d consecutive publish failures",
            job.id, fail_count,
        )
        if notifications is not None:
            notifications.append(
                _note_job_auto_disabled(conn, job.id, fail_count),
            )


def check_briefing_triggers(db_path, config: Config) -> list[int]:
    """Check for briefing trigger files from the NC app and create tasks.

    Trigger files are written by the NC app to request an immediate briefing
    run. Each file is ``{triggers_dir}/briefing_{user_id}_{briefing_name}.json``
    containing ``{"user_id": "...", "briefing_name": "..."}``.

    Returns list of created task IDs.
    """
    if config.config_path is None:
        return []

    triggers_dir = config.config_path.parent / "triggers"
    if not triggers_dir.is_dir():
        return []

    created_tasks = []
    for trigger_file in triggers_dir.glob("briefing_*.json"):
        try:
            trigger = json.loads(trigger_file.read_text())
            user_id = trigger.get("user_id", "")
            briefing_name = trigger.get("briefing_name", "")

            if not user_id or not briefing_name:
                logger.warning("Invalid trigger file %s: missing user_id or briefing_name", trigger_file)
                trigger_file.unlink()
                continue

            user_config = config.get_user(user_id)
            if not user_config:
                logger.warning("Trigger for unknown user %s, skipping", user_id)
                trigger_file.unlink()
                continue

            # Find the matching briefing
            briefings = get_briefings_for_user(config, user_id)
            briefing = next((b for b in briefings if b.name == briefing_name), None)
            if not briefing:
                logger.warning("Trigger for unknown briefing %s/%s, skipping", user_id, briefing_name)
                trigger_file.unlink()
                continue

            # Queue the briefing task; the prompt is built in the executor off
            # the dispatch thread (ISSUE-143), same as the cron path.
            with db.get_db(db_path) as conn:
                task_id = db.create_task(
                    conn,
                    prompt=_deferred_briefing_placeholder(briefing.name),
                    user_id=user_id,
                    source_type="briefing",
                    conversation_token=briefing.conversation_token,
                    output_target=briefing.output,
                    priority=8,
                    queue="background",
                    briefing_name=briefing.name,
                )
            created_tasks.append(task_id)
            logger.info("Triggered briefing %s for %s (task %d)", briefing_name, user_id, task_id)
        except Exception as e:
            logger.error("Error processing trigger %s: %s", trigger_file, e)
        finally:
            # Always delete the trigger file after processing
            try:
                trigger_file.unlink(missing_ok=True)
            except Exception:
                pass

    return created_tasks


def cleanup_old_temp_files(config: Config, retention_days: int) -> int:
    """
    Delete temp files older than retention_days.

    Iterates into per-user subdirectories under temp_dir.
    All permanent storage should be in Nextcloud, so temp files
    are safe to clean up periodically.

    Returns:
        Number of files deleted.
    """
    if not config.temp_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 24 * 60 * 60)
    deleted = 0

    def _cleanup_dir(directory: Path) -> int:
        count = 0
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    count += 1
                elif path.is_dir():
                    # Recurse into user subdirectories
                    count += _cleanup_dir(path)
                    # Remove empty directories, but only once the directory
                    # itself has gone untouched past the retention window.
                    # execute_task creates an empty per-user temp dir and writes
                    # its prompt file a few seconds later; without this age gate
                    # a concurrent cleanup tick would rmdir that still-empty dir
                    # mid-task and break the write (the temp-dir race).
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.rmdir()  # only succeeds if empty
                    except OSError:
                        pass
            except Exception as e:
                logger.debug(f"Could not process temp path {path}: {e}")
        return count

    deleted = _cleanup_dir(config.temp_dir)
    return deleted


def check_db_health(config: Config) -> list[CheckReport]:
    """Sweep ``PRAGMA quick_check`` + ``REINDEX`` across all known SQLite DBs.

    Covers the framework DB and every configured user's module DBs
    (feeds, health, location, money). Each check is independent —
    one failed open or one disabled module doesn't stop the sweep.

    Returns the per-DB :class:`CheckReport` list so callers (tests,
    operator tooling) can inspect outcomes. The scheduler tick ignores
    the return value; results are already logged.
    """
    reports: list[CheckReport] = []

    # 1. Framework DB (local disk, but cheap to check and worth confirming).
    reports.append(check_and_repair(config.db_path, label="framework"))

    # 2. Per-user module DBs. These now live on LOCAL disk at
    #    config.module_db_path(user_id, module) (off the Nextcloud mount so WAL
    #    is safe). Probe each resolved path directly rather than calling the
    #    module resolvers: resolvers raise for disabled-module / missing-user /
    #    missing-mount, and we don't want any of those to skip a *file* that is
    #    actually on disk and might be corrupt.
    for user_id in config.users:
        # From the registry, not a copy of it: a list written out by hand here
        # would silently stop covering the next module added (ISSUE-262).
        for module in sorted(MODULE_NAMES):
            try:
                db_path = config.module_db_path(user_id, module)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "db_health_module_path_failed user=%s module=%s err=%s",
                    user_id, module, exc,
                )
                continue
            reports.append(
                check_and_repair(db_path, label=f"{module}:{user_id}")
            )

    return reports


def _doctor_alert_recipients(config: Config) -> list[str]:
    """Who a doctor alert goes to: the admin allowlist, and nobody else.

    Deliberately not :func:`_operator_alert_user`, which falls back to the first
    configured user, and deliberately not ``Config.is_admin``, which reads an
    empty allowlist as "everyone is an admin". A doctor ``FAIL`` names install
    paths, binary locations and remedies; broadcasting that to every user on a
    multi-user deployment leaks the operator's layout to non-admins.

    Failing closed and saying so is the safer default, and it matches
    ``_user_is_web_admin``'s posture.
    """
    return sorted(config.admin_users)


def _alert_doctor_failures(config: Config, failures: list, *, context: str) -> None:
    """Send one alert per admin naming every failing check. Never raises.

    One message, not one per check: a boot that fails five checks is one
    problem, and five notifications is how an operator learns to mute them.
    """
    if not failures:
        return
    recipients = _doctor_alert_recipients(config)
    if not recipients:
        logger.warning(
            "doctor_alert_undeliverable checks=%d reason=no admin users configured "
            "(set ISTOTA_ADMINS_FILE); the alert names install paths and remedies, "
            "so it is not broadcast to every user",
            len(failures),
        )
        return

    lines = [f"{config.bot_name} runtime check failed ({context}):", ""]
    for result in failures:
        lines.append(f"- {result.name}: {result.detail}")
        if result.remedy:
            lines.append(f"  {result.remedy}")
    message = "\n".join(lines)

    for user_id in recipients:
        try:
            _send_operator_alert(config, user_id, message)
        except Exception as exc:  # noqa: BLE001 - the alert path must not take the daemon down
            logger.error("doctor_alert_send_failed user=%s err=%s", user_id, exc)


def run_startup_checks(config: Config) -> list:
    """Run the doctor registry once at boot, log it, and alert on any failure.

    **Never aborts, whatever it finds.** Both deployment shapes restart
    automatically — ``restart: unless-stopped`` in compose, systemd on bare
    metal — so a daemon that exited on a ``FAIL`` would not fail loudly, it
    would crash-loop, and in the container shape that locks the operator out of
    the box they need to exec into. Degraded-but-running is strictly more
    diagnosable. It also matches ``_validate_forge_clis``'s existing posture and
    does not preempt ``check_db_health``'s self-repair, which already handles the
    one condition that looked like a candidate for fatal.

    Deep checks are not run: spawning a bubblewrap namespace is not something a
    boot path should do unattended.
    """
    from . import doctor

    try:
        results = doctor.run_checks(config)
        # Redact before anything is logged or sent. `detail` carries observed
        # paths and raw exception text, and the log file and the Talk room are
        # boundaries in exactly the way the admin dashboard is.
        results = doctor.redact(results, config)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not become an outage
        logger.error("doctor_startup_failed err=%s", exc, exc_info=True)
        return []

    for result in results:
        if result.status == doctor.FAIL:
            logger.error("doctor %s: %s %s", result.name, result.detail, result.remedy)
        elif result.status == doctor.WARN:
            logger.warning("doctor %s: %s %s", result.name, result.detail, result.remedy)

    counts = doctor.summarize(results)
    logger.info(
        "STARTUP doctor: %d ok, %d warn, %d fail, %d skip",
        counts[doctor.OK], counts[doctor.WARN], counts[doctor.FAIL], counts[doctor.SKIP],
    )
    _alert_doctor_failures(config, doctor.failing(results), context="start-up")
    return results


# Checks the interval sweep leaves alone, and why. `runtime.framework_db` runs
# `PRAGMA quick_check`, which reads the whole database — `check_db_health` owns
# that job and does it once a day. Running it hourly here would be the same
# full-DB scan 24 times over, for a second opinion nobody asked for. It still
# runs at boot and whenever an operator types `istota doctor`.
SWEEP_SKIPPED_CHECKS = ("runtime.framework_db",)


def check_doctor(config: Config, state: dict) -> list:
    """Re-run the doctor registry on the scheduler's interval.

    This matters more than the boot run. The drift we actually saw happens
    *after* boot: the production auto-update cron pulls code and restarts
    services every two minutes without running Ansible, so what is installed
    changes under a config the daemon already loaded. A boot-only check is blind
    to exactly that, and ``developer.forge_config_drift`` is the check that sees
    it.

    Alerts on the *transition* into failure rather than on every sweep, with the
    previously-failing set held in ``state`` — the caller's dict, so there is no
    process-global. A check that is still failing produces no second alert; one
    that is newly failing does.
    """
    from . import doctor

    try:
        results = doctor.run_checks(config, skip=SWEEP_SKIPPED_CHECKS)
        results = doctor.redact(results, config)
    except Exception as exc:  # noqa: BLE001 - a periodic diagnostic must not kill the loop
        logger.error("doctor_sweep_failed err=%s", exc, exc_info=True)
        return []

    failures = doctor.failing(results)
    failing_names = {r.name for r in failures}
    previously_failing = state.get("failing", set())

    for result in failures:
        logger.error("doctor %s: %s %s", result.name, result.detail, result.remedy)

    newly_failing = failing_names - previously_failing
    if newly_failing:
        _alert_doctor_failures(
            config,
            [r for r in failures if r.name in newly_failing],
            context="periodic check",
        )
    state["failing"] = failing_names
    return results


#: `executor.SANDBOX_CACHE_ROOT_NAME`, restated. Held equal by
#: `tests/test_worktree_reaper.py`.
_SANDBOX_CACHE_ROOT_NAME = ".package-caches"


def _package_cache_dirs(repos_dir: str) -> list[Path]:
    """Every derived package cache under `repos_dir` — `{root}/*/.package-caches`.

    The layout is `executor.SANDBOX_CACHE_ROOT_NAME`, restated here rather than
    imported for the reason the developer skill restates it: this is the
    scheduler, and it should not pull the executor in for one string.
    `tests/test_worktree_reaper.py` holds the spelling to the executor's.

    Enumerated from disk rather than from `user_profiles`, because the caches
    are created lazily on a user's first task and it is precisely the directory
    that *exists* that costs the walk. Never raises and never returns a partial
    guess: an unreadable root yields an empty list, which costs a noisy sweep
    rather than a wrong one — `skip` only prunes the walk, so getting it wrong
    in this direction loses performance, not safety.
    """
    try:
        return [
            entry / _SANDBOX_CACHE_ROOT_NAME
            for entry in Path(repos_dir).iterdir()
            if entry.is_dir() and not entry.is_symlink()
        ]
    except OSError:
        return []


def check_worktree_reap(config: Config) -> list:
    """Sweep `developer.repos_dir` for worktrees whose work has landed.

    Here rather than on the developer skill's setup path (ISSUE-288, and the
    review of it): `dispatch_setup_env_hooks` calls every skill's hook whatever
    the task selected, so a sweep there ran before every Talk reply, every cron
    job and every heartbeat tick — and the heartbeat builds a task with `id=0`,
    so it ran with no notion of whose worktree was whose. A delete path belongs
    on a cadence somebody chose.

    Nothing is protected by name here, and nothing needs to be: the retention
    window is the guard, and it is floored well above any task's lifetime.

    **The global root, not a per-user one, and that is deliberate.** Everything
    that scopes a *task* takes `{repos_dir}/{user_id}` — the bwrap bind, the
    container mount, `DEVELOPER_REPOS_DIR`, the credential scrub — because those
    decide what one user's model can reach. This sweep decides nothing of the
    kind: it runs in the daemon, on a cadence, for the whole deployment, and it
    has no user to scope to. Handed a per-user root it would need a list of
    users, and a user missing from that list would keep every worktree they ever
    made, silently — which is the ISSUE-288 state this exists to end. The
    per-user directories sit one level below the root and `find_git_dirs`'
    depth budget covers them.

    Returns the outcomes so a caller can assert on them; the sweep logs its own
    removals and a count of what it kept.
    """
    from .worktree_reaper import reap_and_report

    dev = config.developer
    # Re-checked here rather than left to the loop's gate. The gate exists to
    # skip the thread spawn cheaply; this makes the function safe to call on
    # its own, which is how the tests and any future operator command reach it.
    # A delete path should not depend on its caller having read the flag.
    if not (dev.enabled and dev.repos_dir and dev.worktree_reap_enabled):
        return []

    try:
        return reap_and_report(
            Path(dev.repos_dir),
            retention_hours=dev.worktree_retention_hours,
            # The package caches live inside repos_dir and hold one directory
            # per unpacked wheel. None of them is a repository, and walking
            # them logs a per-sweep line claiming thousands of directories went
            # unswept for credentials (ISSUE-319).
            #
            # One entry per user, because the caches are derived per user now —
            # `{repos_dir}/{user_id}/.package-caches`, `find_git_dirs` matches
            # `skip` on the resolved path, and this used to name
            # `security.sandbox_cache_dir`, which is blank on a deployment that
            # has a repos tree to derive from. An empty skip is not a cosmetic
            # loss: the cache sits at depth 2, so `uv/archive-v0` lands exactly
            # on `_MAX_DEPTH` and every unpacked wheel is listed and lstatted,
            # and a git directory inside the cache at depth 4 or less would be
            # picked up as a reap candidate — which means `git fetch` against a
            # model-written `remote.origin.url`, from the unsandboxed
            # scheduler, outside the CONNECT allowlist, on every sweep.
            skip=_package_cache_dirs(dev.repos_dir),
        )
    except Exception as exc:  # noqa: BLE001 - a periodic sweep must not kill the loop
        logger.error("worktree_reap_failed err=%s", exc, exc_info=True)
        return []


def sandbox_cache_sweep_root(config: Config) -> tuple[Path, list[str] | None] | None:
    """Where the per-user package caches are, and whose they are. None if nowhere.

    The two shapes `executor.resolve_sandbox_cache_dir` produces, asked the
    other way round. **It reproduces that function's branch selection, not its
    refusals**, and the difference is worth being exact about rather than
    claiming an agreement that does not hold. The branch gate is the same pair,
    `enabled and repos_dir`, and that is the part the sweep's worth rests on: a
    root the resolver does not write into is a sweep that finds nothing while
    the real caches grow, silently and in the direction of the disk leak
    ISSUE-317 exists to close.

    The resolver then refuses five further things (a relative root, one the
    daemon cannot write, one at or above a sandbox mount, one under a database
    directory, and `_validate_workspace_dir`'s blocklist) and this does not
    re-derive them — duplicating that chain is the drift the whole finding
    would be about. Only the one refusal that changes *where this walks* is
    repeated: an absolute root. A relative `developer.repos_dir` makes the
    resolver return None while this would hand `sweep_and_report` a path
    resolved against the daemon's working directory, so the sweep would run
    package-manager reclaim verbs somewhere no cache was ever created. The
    others all leave the sweep walking a directory the resolver simply declined
    to populate, which finds nothing and costs a log line.

    * developer skill on with a repos dir — the caches are derived per user at
      `{repos_dir}/{user_id}/.package-caches`, so the root is `repos_dir` and
      the user list is the daemon's own (`config.users`, which the config
      loader has already overlaid `user_profiles` onto). **The list is passed
      rather than enumerated**, because `repos_dir` is bound read-write into
      every admin developer task and a directory name found there is not
      evidence of a user — see `sandbox_cache_sweeper._candidates_for_users`.
      A cache belonging to nobody on that list is reported by
      `report_orphan_caches` and acted on by nothing.
    * otherwise, `security.sandbox_cache_dir` and its one-level layout, which
      the sweeper enumerates itself. Unchanged.

    Returning `None` rather than a root with nothing at it keeps "there is no
    cache to bound" distinguishable from "the cache is empty", which is what the
    caller's own gate needs.

    """
    dev, sec = config.developer, config.security
    if dev.enabled and dev.repos_dir:
        root = Path(dev.repos_dir)
        if not root.is_absolute():
            logger.warning(
                "sandbox_cache_sweep_skipped reason=repos_dir_not_absolute path=%r "
                "— it would resolve against the daemon's working directory, and "
                "the resolver refuses it too, so no cache was ever created there.",
                dev.repos_dir,
            )
            return None
        return root, sorted(config.users)
    if sec.sandbox_cache_dir:
        root = Path(sec.sandbox_cache_dir)
        if not root.is_absolute():
            logger.warning(
                "sandbox_cache_sweep_skipped reason=sandbox_cache_dir_not_absolute "
                "path=%r — same reason.", sec.sandbox_cache_dir,
            )
            return None
        return root, None
    return None


def check_skill_overlay_reindex(config: Config) -> list:
    """Refresh the memory-search index for every user's per-skill overlays.

    Returns the user ids whose overlays produced rows, for the caller's log
    line. Never raises: one user's failure must not cost the rest the pass.

    **Why this is a scheduler tick and not part of the sleep cycle** (ISSUE-343).
    An overlay is a document the *user* authors, so there is no write path to
    hang indexing off — the memory CLI used to reindex after each of its own
    overlay writes, and that went with the write verbs. It was the wrong seam
    regardless: a text-editor edit over Nextcloud, which is how a file the user
    owns is normally changed, called no CLI and was never indexed. A full
    directory pass is the only thing that covers every authoring route, and
    `search.reindex_skill_overlays` already reaps rows for a file that no longer
    binds, so a pass is also what expires a deleted overlay.

    It went into `process_user_sleep_cycle` first, above that function's early
    returns so a user with no interactions still got a pass. Review found the
    gate one level up: `check_sleep_cycles` returns before calling it at all
    when `sleep_cycle.enabled` is false, and again when `primary_brain_unavailable`
    is open. `memory_search.enabled` and `sleep_cycle.enabled` are independent
    settings, so that left a supported deployment — search on, sleep cycle off —
    on which *nothing* indexed an overlay, where index-on-write had. And a
    usage-limit cooldown cost a night's indexing for work that makes no brain
    call. Neither gate has anything to do with this, so it sits on a cadence of
    its own beside the other two maintenance passes.

    Its own connection, not the sleep cycle's. That pass holds one write
    transaction for its whole per-user run, and `index_file` deletes and
    re-inserts unconditionally — there is no content hash — so every binding
    overlay is re-embedded on each pass. That work belongs outside a
    transaction spanning an LLM call, which is the same reason the retired
    per-write reindex ran outside the CLI's own `memory_md_lock`.
    """
    if not (
        config.memory_search.enabled and config.memory_search.auto_index_memory_files
    ):
        return []
    if not config.use_mount:
        return []

    touched: list[str] = []
    try:
        from .memory.search import reindex_skill_overlays
    except Exception as e:  # noqa: BLE001 - optional extra; a reindex is best-effort
        logger.debug("skill overlay reindex unavailable: %s", e)
        return []

    try:
        with db.get_db(config.db_path) as conn:
            for user_id in config.users:
                try:
                    files, chunks = reindex_skill_overlays(conn, config, user_id)
                except Exception as e:  # noqa: BLE001 - one user must not cost the rest
                    logger.debug(
                        "skill overlay reindex failed for %s: %s", user_id, e
                    )
                    continue
                if files:
                    touched.append(user_id)
                    logger.info(
                        "Reindexed %d skill overlays (%d chunks) for %s",
                        files, chunks, user_id,
                    )
    except Exception as e:  # noqa: BLE001 - never take the loop down
        logger.debug("skill overlay reindex pass failed: %s", e)
    return touched


def check_sandbox_cache_sweep(config: Config) -> list:
    """Bound the per-user package caches, wherever this deployment puts them.

    Here rather than on a task's setup path, for the reason `check_worktree_reap`
    above gives: `dispatch_setup_env_hooks` calls every skill's hook whatever the
    task selected, so a sweep there runs before every Talk reply and every
    heartbeat tick. A delete path belongs on a cadence somebody chose.

    **The busy set is fail-closed and this function owns that.** The sweeper is
    a leaf that reads no database, so the set of users with a task in flight has
    to arrive as an argument — and an unreadable task table would arrive as an
    empty set, which reads as "nobody is working" and is the one wrong answer
    that costs a running task its cache. So a failed read returns without
    sweeping at all. Disk is what the sweep protects and one more interval of it
    is cheap; a `uv sync` losing its cache mid-resolution is not.

    The *user* list is not fail-closed in the same way and does not need to be:
    it comes from the already-loaded config rather than from a query, and being
    wrong about it costs an unswept cache rather than a live task's.

    Re-checks its own gate rather than trusting the loop's, matching the reaper:
    the loop's gate exists to skip the thread spawn cheaply, and a delete path
    should be safe to call on its own.
    """
    from .sandbox_cache_sweeper import sweep_and_report

    sec = config.security
    if not sec.sandbox_cache_sweep_enabled:
        return []
    target = sandbox_cache_sweep_root(config)
    if target is None:
        return []
    root, user_ids = target

    try:
        with db.get_db(config.db_path) as conn:
            busy_users = db.get_users_with_live_tasks(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sandbox_cache_sweep_skipped reason=task_table_unreadable err=%s "
            "— the set of users with work in flight is unknown, so nothing is swept.",
            exc,
        )
        return []

    try:
        return sweep_and_report(
            root,
            max_bytes=int(sec.sandbox_cache_max_gb * 1024 ** 3),
            busy_users=busy_users,
            user_ids=user_ids,
        )
    except Exception as exc:  # noqa: BLE001 - a periodic sweep must not kill the loop
        logger.error("sandbox_cache_sweep_failed err=%s", exc, exc_info=True)
        return []


AVATAR_IMPORT_IMPORTED = "imported"
AVATAR_IMPORT_NO_CUSTOM = "no-custom"
AVATAR_IMPORT_UNCHANGED = "unchanged"
AVATAR_IMPORT_FAILED = "failed"

# The byte ceiling `normalize` is given for an imported picture. It follows
# `web.max_avatar_kb` so an operator who lowered the cap lowers this too, but
# **not through its zero**: `0` there means "switch the upload route off", a
# statement about a route this job does not use, and reading it as a byte
# ceiling would silently refuse every import on a deployment that had only meant
# to stop users uploading. `avatar_import_from_nextcloud` is the switch for
# this, and it is a different one on purpose.
_AVATAR_IMPORT_FALLBACK_KB = 4096


@dataclass(frozen=True)
class AvatarImportOutcome:
    """What one tick did about one user. Returned so a caller can assert on it."""

    user_id: str
    action: str
    detail: str = ""


def check_avatar_import(config: Config) -> list[AvatarImportOutcome]:
    """Import each user's custom Nextcloud avatar, on a cadence.

    **The user set is `config.users`, and that is the whole point of this
    function.** An earlier draft of the spec derived it from the `user_avatars`
    table, which is dead on arrival: a user with no `nextcloud` row is exactly
    the user who needs the first import, so reading the table for the set
    excludes precisely them and nothing is ever imported, on any deployment.
    The table supplies the ETags and nothing else.

    **No transaction is held across a fetch.** The loop is fetch (network, no
    DB), then one short write for that user, then the next user. This is the
    first backgrounded scheduler check that writes to the framework DB in a
    per-user loop interleaved with HTTP — the neighbours are readers, or touch
    other files — so one `with db.get_db(...)` around the loop would hold the
    write lock for the length of a Nextcloud timeout and stall every writer in
    the daemon and in the web process.

    Each returned image runs through the same `avatars.normalize` an upload
    takes: an image from Nextcloud is not more trusted than one from a browser.
    The byte ceiling goes to `fetch_avatar` as well as to `normalize`, so it is
    enforced on the stream before the body exists in memory rather than on
    `len()` after — the rule the spec states as D15 for the upload route, which
    nothing about the sender being Nextcloud changes. Every user is wrapped, so
    one unreachable account does not end the tick.

    Re-checks its own gates rather than trusting the loop's, matching the
    reaper and the cache sweep: the loop's gate exists to skip the thread spawn
    cheaply, and this has to be safe to call on its own — which is how the tests
    and any future operator command reach it.

    `web.enabled` is one of those gates and is easy to leave out, since the
    other three are about the import itself. An avatar renders in the web UI and
    nowhere else — Talk draws its own, email and ntfy have no identity gutter —
    so with the surface off this would spend one Nextcloud request per
    configured user every six hours on bytes nothing will ever serve. It is also
    what keeps `doctor` honest: `web.avatar_import` SKIPs with "web interface
    disabled" the way every other `web.*` check does, and a check reporting SKIP
    for work the daemon is doing anyway is worse than no check at all.
    """
    if not (
        config.web.enabled
        and config.storage_is_nextcloud
        and config.web.avatar_import_from_nextcloud
        and config.scheduler.avatar_import_interval
    ):
        return []

    # `storage_is_nextcloud` is `bool(url)` and `nc_configured` is
    # `bool(url and username)`, so a config carrying a URL and no username
    # passes every gate above and then raises "Nextcloud is not configured" for
    # every user, once per tick, forever. Refusing here makes that one skip
    # rather than N failures, and leaves the failure counter meaning what it
    # says: a credential the server rejected, not one nobody supplied.
    if not nc_configured(config):
        logger.debug("avatar_import_skipped reason=nextcloud_not_configured")
        return []

    users = sorted(config.users)
    if not users:
        return []

    try:
        with db.get_db(config.db_path) as conn:
            etags = avatars.import_probe_state(conn)
    except Exception as exc:  # noqa: BLE001 - a periodic check must not kill the loop
        logger.error(
            "avatar_import_skipped reason=probe_state_unreadable err=%s", exc,
            exc_info=True,
        )
        return []

    max_bytes = (
        config.web.max_avatar_kb or _AVATAR_IMPORT_FALLBACK_KB
    ) * 1024
    outcomes: list[AvatarImportOutcome] = []
    header_seen = False
    header_absent = False

    for user_id in users:
        try:
            answer = nc_avatars.fetch_avatar(
                config, user_id, etag=etags.get(user_id, ""),
                max_bytes=max_bytes,
            )
            if answer is None:
                # A 304, or a user Nextcloud does not know. Nothing was learned,
                # so the stored row and its ETag are left exactly as they are.
                outcomes.append(
                    AvatarImportOutcome(user_id, AVATAR_IMPORT_UNCHANGED)
                )
                continue

            if isinstance(answer, nc_avatars.NoCustomAvatar):
                if answer.header_seen:
                    header_seen = True
                    probe_etag = answer.etag
                else:
                    header_absent = True
                    # **No validator for an answer nothing could classify**, and
                    # this line is the whole of a defect both reviewers found
                    # independently. An ETag lets the next tick skip a body it
                    # has already seen; storing one here made the next tick send
                    # `If-None-Match`, take a 304, record `unobserved` — and
                    # `unobserved` is an OK. So the `absent` verdict, the single
                    # finding the recorded state exists to carry, erased itself
                    # one interval after it was written, on the only deployment
                    # it was built for, and a restart did not help because the
                    # first tick after one takes the same 304 path. Asking
                    # unconditionally costs one GET per user per tick on a
                    # deployment that can import nothing anyway, and it is what
                    # keeps the answer observable for as long as it is true.
                    probe_etag = ""
                with db.get_db(config.db_path) as conn:
                    avatars.touch_import_probe(
                        conn, user_id, remote_etag=probe_etag,
                    )
                outcomes.append(
                    AvatarImportOutcome(user_id, AVATAR_IMPORT_NO_CUSTOM)
                )
                continue

            header_seen = True
            image, digest = avatars.normalize(
                answer.image, declared_format=None, max_bytes=max_bytes,
            )
            with db.get_db(config.db_path) as conn:
                avatars.put_user_avatar(
                    conn, user_id,
                    source=avatars.SOURCE_NEXTCLOUD,
                    image=image,
                    content_hash=digest,
                    remote_etag=answer.etag,
                )
            outcomes.append(AvatarImportOutcome(user_id, AVATAR_IMPORT_IMPORTED))
        except avatars.AvatarError as exc:
            # **A picture this deployment will never accept, parked rather than
            # retried.** `normalize` refuses an image over `AVATAR_MAX_PIXELS`
            # or in a format not on the list, and both are reachable from a
            # Nextcloud avatar well inside the byte ceiling the fetch already
            # enforced — so the ceiling cannot prevent them and the refusal is
            # permanent for as long as that user keeps that picture. Falling
            # into the generic branch below stored no ETag, which meant the next
            # tick sent no `If-None-Match`, pulled the whole body down again and
            # logged another traceback, every interval, forever. Recording the
            # validator turns that into a 304. INFO, not WARNING with a
            # traceback: nothing here is broken, and a user can fix it by
            # changing their Nextcloud picture.
            logger.info(
                "avatar_import_unusable user=%s err=%s", user_id, exc,
            )
            try:
                with db.get_db(config.db_path) as conn:
                    avatars.touch_import_probe(
                        conn, user_id, remote_etag=getattr(answer, "etag", ""),
                    )
            except Exception as write_exc:  # noqa: BLE001 - best effort
                logger.warning(
                    "avatar_import_probe_write_failed user=%s err=%s",
                    user_id, write_exc,
                )
            outcomes.append(
                AvatarImportOutcome(user_id, AVATAR_IMPORT_FAILED, str(exc))
            )
        except Exception as exc:  # noqa: BLE001 - one account must not end the tick
            logger.warning(
                "avatar_import_failed user=%s err=%s", user_id, exc, exc_info=True,
            )
            outcomes.append(
                AvatarImportOutcome(user_id, AVATAR_IMPORT_FAILED, str(exc))
            )

    _record_avatar_import_tick(config, outcomes, header_seen, header_absent)
    return outcomes


def _record_avatar_import_tick(
    config: Config,
    outcomes: list[AvatarImportOutcome],
    header_seen: bool,
    header_absent: bool,
) -> None:
    """Write down what this tick did, for `doctor`'s `web.avatar_import` check.

    That check opens no socket, so this row is the only thing telling it whether
    the import is working — in particular whether the custom-avatar header
    arrived at all, which is the difference between "nobody has set a Nextcloud
    avatar" and "nothing will ever be imported here".

    `header_seen` wins over `header_absent`: one response carrying the header is
    proof the server can send it, and the finding doctor warns on is that
    nothing can ever be imported.
    """
    if header_seen:
        header = avatars.HEADER_SEEN
    elif header_absent:
        header = avatars.HEADER_ABSENT
    else:
        # Every user answered 304, or 404, or failed. No response this tick
        # could have carried the header, which is not the same as its absence.
        header = avatars.HEADER_UNOBSERVED

    counted = {action: 0 for action in (
        AVATAR_IMPORT_IMPORTED, AVATAR_IMPORT_NO_CUSTOM,
        AVATAR_IMPORT_UNCHANGED, AVATAR_IMPORT_FAILED,
    )}
    for outcome in outcomes:
        counted[outcome.action] = counted.get(outcome.action, 0) + 1

    state = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "users": len(outcomes),
        "imported": counted[AVATAR_IMPORT_IMPORTED],
        "no_custom": counted[AVATAR_IMPORT_NO_CUSTOM],
        "unchanged": counted[AVATAR_IMPORT_UNCHANGED],
        "failed": counted[AVATAR_IMPORT_FAILED],
        "header": header,
    }
    try:
        with db.get_db(config.db_path) as conn:
            avatars.write_import_state(conn, state)
    except Exception as exc:  # noqa: BLE001 - a record, not the work
        logger.warning("avatar_import_state_unwritten err=%s", exc)


def _record_session_log_sweep(config: Config, result: SweepResult) -> None:
    """Write down what this sweep did, for `doctor`'s `runtime.session_log_dir`.

    That check runs in three processes that never see the sweep — daemon
    start-up, `istota doctor` and the web process behind the admin Health pane —
    so the one fact it needs cannot be module state: whether the *size ceiling*
    is what reclaimed. When it is, `retention_days` is not the retention
    actually in force and the effective window is a function of load, which is a
    condition to surface rather than absorb.

    Written on every sweep, not only on an eviction, so a ceiling that stopped
    binding stops warning. Failure costs the row and never the tick: this is a
    record of the cleanup, not part of it.

    The write is bounded rather than taking ``get_db``'s 30-second default,
    because it happens on the dispatch thread once a cleanup tick: losing a
    diagnostic row is strictly cheaper than holding the loop for half a minute
    behind a busy worker, which is the trade ``main_loop_read_timeout_ms``
    already states for the loop's own reads.
    """
    try:
        with db.get_db(
            config.db_path,
            busy_timeout_ms=config.scheduler.main_loop_read_timeout_ms,
        ) as conn:
            db.shared_kv_set(
                conn,
                SWEEP_STATE_NAMESPACE,
                SWEEP_STATE_KEY,
                encode_sweep_state(result, now=time.time()),
                "scheduler:session_log_sweep",
            )
    except Exception as exc:  # noqa: BLE001 - a record, not the work
        logger.warning("session_log_sweep_state_unwritten err=%s", exc)


def _operator_alert_user(config: Config) -> str | None:
    """Pick a user to receive operator-level scheduler alerts.

    Thin delegate to :func:`istota.notifications.operator_alert_user` (the
canonical home) so scheduler-internal callers and tests keep their import
path. Prefers the first admin user (sorted for determinism); falls back to
    the first configured user. ``None`` when no users are configured.
    """
    from .notifications import operator_alert_user

    return operator_alert_user(config)


def _send_operator_alert(config: Config, user_id: str, message: str, *, timeout: float = 30.0) -> None:
    """Send an operator alert without letting a hung Talk delivery stall the
    caller. `send_notification` ultimately runs on the persistent asyncio loop
    with no timeout, so on the single-threaded main loop a wedged Nextcloud would
    block dispatch indefinitely (ISSUE-143 class) — and a backup-destination
    outage is exactly when Talk is likely also degraded. Run the send on a
    short-lived daemon thread and only wait `timeout`; if it's still going we
    return and let it finish (or die) in the background."""
    def _do() -> None:
        try:
            send_notification(config, user_id, message, purpose="alert")
        except Exception as exc:  # noqa: BLE001
            logger.error("operator_alert_failed err=%s", exc)

    t = threading.Thread(target=_do, name="operator-alert", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.error("operator_alert_timed_out after %ss — send still running in background", timeout)


def _db_backup_lookback_days() -> int:
    """The 'vanished' window, read from where it's defined rather than restated.
    Imported here so the alert text can't drift away from the actual window."""
    from .db_backup import _VANISHED_LOOKBACK_DAYS

    return _VANISHED_LOOKBACK_DAYS


def _alert_backup_problems(config: Config, results: list[dict]) -> None:
    """Fire one operator alert when a backup run reports any errored, suspect
    (row-count collapse) or vanished DB. Best-effort — never raises into the loop.

    ``skip_missing`` is deliberately not a problem: a user who never opened a
    module has no DB for it and never will. ``vanished`` is the same absence
    with history behind it — the DB was being snapshotted days ago — which is
    coverage shrinking rather than a module that was never used (ISSUE-262).
    """
    problems = [r for r in results if r.get("status") in ("error", "suspect", "vanished")]
    if not problems:
        return
    user = _operator_alert_user(config)
    if not user:
        return
    lines = "\n".join(f"• {r['label']}: {r['status']}" for r in problems)
    message = (
        f"⚠️ DB backup problem — {len(problems)} database(s) failed, were "
        f"quarantined, or dropped out of coverage on the latest snapshot:\n{lines}\n"
        "A 'suspect' DB was empty/unreadable vs. the prior good snapshot and was "
        f"kept aside as .suspect; the prior good copy is preserved. A 'vanished' DB "
        f"had a snapshot within the last {_db_backup_lookback_days()} days and its "
        "source file is now gone. Check the live DB."
    )
    _send_operator_alert(config, user, message)


def _maybe_alert_backup_stale(
    config: Config, now: float, persisted: float, already_alerted: bool
) -> bool:
    """Alert once when the persisted backup clock is older than 2x the interval
    (backups have silently stopped). Re-arms on recovery. Returns the new
    already_alerted state. Gated on a prior successful run (`persisted > 0`) so a
    fresh deploy that simply hasn't backed up yet doesn't false-alarm."""
    if not (config.scheduler.db_backup_enabled and config.scheduler.db_backup_interval):
        return already_alerted
    stale_after = 2 * config.scheduler.db_backup_interval
    if persisted > 0 and now - persisted >= stale_after:
        if already_alerted:
            return True
        age_h = int((now - persisted) / 3600)
        interval_h = config.scheduler.db_backup_interval // 3600
        logger.error(
            "db_backup_stale last_run=%.0f age_s=%.0f — backups appear stopped",
            persisted, now - persisted,
        )
        user = _operator_alert_user(config)
        if user:
            _send_operator_alert(
                config, user,
                f"⚠️ DB backups appear to have stopped — no successful snapshot in "
                f"{age_h}h (interval is {interval_h}h). Check the scheduler and the "
                "backup destination.",
            )
        return True
    return False


def _run_db_backup(config: Config) -> None:
    """Snapshot every local DB, then alert on any errored/suspect/vanished result.

    The snapshot and its alert are one unit so the whole thing can be handed to
    a background thread — the alert has to see the results of the run that
    produced them, and ``_alert_backup_problems`` is best-effort anyway.
    """
    from .db_backup import backup_databases

    results = backup_databases(config)
    _alert_backup_problems(config, results)


def _run_email_poll(config: Config) -> None:
    """Poll inbound mail and log what it queued.

    A thin wrapper so the loop can hand one callable to
    ``_spawn_background_check``. Exceptions are contained there and logged as
    ``background_check_failed``; the try/except is kept so the log names the
    email poll specifically, which is what the operator greps for.
    """
    from .transport.email import poll_emails

    try:
        email_tasks = poll_emails(config)
        if email_tasks:
            logger.info("Queued %d email task(s)", len(email_tasks))
    except Exception as e:
        logger.error("Error polling emails: %s", e)


def check_travel_timezone(
    config: Config, *, now: "datetime | None" = None,
) -> list[tuple[str, str]]:
    """Follow each opted-in user's timezone when they travel (ISSUE-096).

    `user_profiles.timezone` feeds the `User timezone:` prompt header, and
    through it every briefing and calendar read. Crossing a border used to leave
    it on the home zone until the user fixed it by hand.

    Opt-in per user (`timezone_follow_location`, default off) and never silent:
    this rewrites a value the user chose, so it is a setting they turn on and an
    event they are told about, not an inference made behind their back. Returns
    the `(user_id, new_zone)` pairs actually written.

    Runs here rather than in the ingest path: the receiver is a different
    process with no notification plumbing, and a timezone that settles within a
    poll interval is soon enough for something measured in hours.
    """
    from . import user_profiles
    from .location import resolve_for_user
    from .location import db as location_db
    from .location import timezone as location_timezone

    changed: list[tuple[str, str]] = []
    now = now or datetime.now(timezone.utc)

    with db.get_db(config.db_path) as fw_conn:
        for user_id in list(config.users):
            detected = ""
            current_tz = ""
            try:
                if not config.is_module_enabled(user_id, "location", conn=fw_conn):
                    continue

                profile = user_profiles.get_profile(
                    config.db_path, user_id, conn=fw_conn,
                )
                if profile is None or not profile.timezone_follow_location:
                    continue

                current_tz = profile.timezone or "UTC"
                ctx = resolve_for_user(user_id, config, conn=fw_conn)
                if not Path(ctx.db_path).exists():
                    continue

                with location_db.connect(ctx.db_path) as loc_conn:
                    detected = location_timezone.detect_travel_timezone(
                        loc_conn, current_tz, now=now,
                        accuracy_threshold_m=config.location.accuracy_threshold_m,
                    ) or ""
                if not detected:
                    continue

                if _travel_timezone_on_cooldown(fw_conn, user_id, detected, now):
                    continue

                user_profiles.update_profile(
                    config.db_path, user_id, timezone=detected,
                )
                _record_travel_timezone(fw_conn, user_id, detected, now)
                fw_conn.commit()
                changed.append((user_id, detected))
                logger.info(
                    "travel timezone user=%s %s -> %s", user_id, current_tz, detected,
                )
            except Exception as e:
                logger.warning("Travel timezone check failed for %s: %s", user_id, e)
                continue

            # Outside the try above on purpose: the write has already landed and
            # is what makes the user's clocks right. A dead Talk room must not
            # look like the change failed, nor stop the next user being checked.
            try:
                sent = send_notification(
                    config, user_id,
                    f"Timezone updated to {detected} — you've been in it for a "
                    f"while (was {current_tz}). Briefings and calendar times "
                    f"follow this. Turn this off under Settings if you'd rather "
                    f"set it yourself.",
                    purpose="notification",
                )
            except Exception as e:
                sent = False
                logger.warning("Travel timezone notice failed for %s: %s", user_id, e)
            if not sent:
                # The feature promises it is never silent, so a change the user
                # was not told about is an operator-visible problem rather than
                # a quiet success.
                logger.warning(
                    "travel timezone changed for %s but no destination accepted "
                    "the notice", user_id,
                )

    return changed


# The user's position moves over hours, so there is nothing to gain from
# looking every minute — and plenty to lose, since each pass opens every user's
# location DB.
TRAVEL_TZ_CHECK_INTERVAL = 900  # 15 minutes

# Long enough that a border commute or a manual revert cannot produce a
# per-tick rewrite loop, short enough that a genuine second trip to the same
# place next week still lands.
_TRAVEL_TZ_COOLDOWN_HOURS = 24
_TRAVEL_TZ_NAMESPACE = "location"
_TRAVEL_TZ_KEY = "auto_timezone"


def _travel_timezone_on_cooldown(
    conn: "db.sqlite3.Connection", user_id: str, zone: str, now: datetime,
) -> bool:
    """Whether this exact zone was auto-written for this user recently.

    Detection is memoryless — it compares where you are against what is stored
    — so on its own it re-fires every tick against anything that puts the
    stored value back: a user who prefers home time abroad and sets it manually,
    or a commute that crosses a border with an hour on each side. The record of
    what we last set is what makes the change an event rather than a loop.
    """
    row = db.kv_get(conn, user_id, _TRAVEL_TZ_NAMESPACE, _TRAVEL_TZ_KEY)
    if not row:
        return False
    try:
        stored = json.loads(row["value"])
        if stored.get("zone") != zone:
            return False
        at = datetime.fromisoformat(stored["at"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return now - at < timedelta(hours=_TRAVEL_TZ_COOLDOWN_HOURS)


def _record_travel_timezone(
    conn: "db.sqlite3.Connection", user_id: str, zone: str, now: datetime,
) -> None:
    db.kv_set(
        conn, user_id, _TRAVEL_TZ_NAMESPACE, _TRAVEL_TZ_KEY,
        json.dumps({"zone": zone, "at": now.isoformat()}),
    )


def _run_sleep_cycles(config: Config) -> None:
    """Run the nightly per-user and per-channel sleep cycles (ISSUE-144 Tier 2).

    Bundled as one off-thread unit: both are halves of the same nightly pass and
    came due on the same interval, so running them sequentially here preserves
    the order they had on the dispatch thread and keeps them to one background
    thread rather than two concurrent brain-calling passes.

    Each half opens its own short-lived connection instead of borrowing the
    loop-owned one they used to share. Nothing inside the sleep cycle commits
    mid-pass, so one connection spanning both halves would hold a single write
    transaction for the whole nightly run; two shorten that window, and the
    caller thread has no connection to lend anyway.

    Each half is independently guarded so a failure in the per-user pass still
    lets the channel pass run.
    """
    from .memory.sleep_cycle import check_channel_sleep_cycles, check_sleep_cycles

    try:
        with db.get_db(config.db_path) as conn:
            sleep_users = check_sleep_cycles(conn, config)
        if sleep_users:
            logger.info(
                "Ran sleep cycle for %d user(s): %s",
                len(sleep_users), ", ".join(sleep_users),
            )
    except Exception as e:
        logger.error("Error running sleep cycles: %s", e)

    try:
        with db.get_db(config.db_path) as conn:
            channel_tokens = check_channel_sleep_cycles(conn, config)
        if channel_tokens:
            logger.info(
                "Ran channel sleep cycle for %d channel(s): %s",
                len(channel_tokens), ", ".join(channel_tokens),
            )
    except Exception as e:
        logger.error("Error running channel sleep cycles: %s", e)


def _run_heartbeat_checks(config: Config) -> None:
    """One heartbeat sweep, on a background thread with its own connection.

    Called from both the daemon loop (spawned) and the one-shot
    `run_scheduler` path (directly), so the two cannot drift — the precedent
    `_run_email_poll` and `_run_sleep_cycles` set for the same pair.

    Exceptions are contained by `_spawn_background_check` and logged as
    `background_check_failed`; the try/except here is kept so the log names
    heartbeats specifically, which is what an operator greps for, and so the
    one-shot caller — which has no `_spawn_background_check` around it — is
    covered too.
    """
    from .heartbeat import check_heartbeats

    try:
        with db.get_db(config.db_path) as conn:
            checked_users = check_heartbeats(conn, config)
            if checked_users:
                logger.debug("Checked heartbeats for %d user(s)", len(checked_users))
    except Exception as e:
        logger.error("Error checking heartbeats: %s", e)


def _spawn_background_check(
    name: str,
    fn: Callable[[], object],
    inflight: dict[str, threading.Thread],
    *,
    overlap_expected: bool = False,
) -> bool:
    """Run a known-slow periodic check on a short-lived daemon thread.

    The DB-health sweep, the DB-backup snapshot, the nightly sleep cycles and
    the inbound email poll can all run long: the first two walk every per-user
    DB (the backup writing to the rclone FUSE mount, where latency is
    unbounded), the third makes synchronous per-user LLM calls, and the fourth
    makes one IMAP connection per message it reads plus a WebDAV upload per
    attachment — network I/O whose duration an outside sender can influence.
    Run synchronously they blocked ``pool.dispatch()`` for their whole duration, and the
    ``LoopWatchdog.suspended()`` wrapper needed to stop them false-paging left
    the watchdog blind to *real* stalls in the same window (ISSUE-144).

    ``inflight`` is the caller's own thread registry (``run_daemon`` owns it, so
    there's no process-global state): a check whose previous run is still going
    is skipped rather than overlapped, so a wedged sweep can't stack one thread
    per tick — and, for the sleep cycles, so a pass outliving its poll interval
    can't re-fire against state it hasn't stamped yet. Exceptions are contained
    — a crashed run frees the slot for the next tick. Returns True when a thread
    was spawned.

    ``overlap_expected`` demotes the skip log to DEBUG, for a check polled far
    more often than it runs: the sleep cycles are polled every
    ``briefing_check_interval`` but gated by their own cron, so a nightly pass
    spanning several ticks is normal rather than a warning-worthy overrun.

    Daemon threads by design: an in-flight snapshot dies with the process at
    shutdown rather than delaying it. Backups write dated dirs and the restore
    path sanity-checks them, so a torn snapshot can't clobber the last good one.
    A sleep cycle killed mid-pass leaves its ``last_run`` unstamped and re-runs
    next cycle — the same outcome as any daemon restart during a nightly run.
    """
    prev = inflight.get(name)
    if prev is not None and prev.is_alive():
        log = logger.debug if overlap_expected else logger.warning
        log("background_check_still_running name=%s — skipping this tick", name)
        return False

    def _run() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.error("background_check_failed name=%s err=%s", name, exc)

    thread = threading.Thread(
        target=_run, name=f"bgcheck-{name}", daemon=True,
    )
    inflight[name] = thread
    thread.start()
    return True


class LoopWatchdog:
    """Defense-in-depth monitor for a stalled scheduler main loop (ISSUE-143).

    The dispatch loop is single-threaded: if `pool.dispatch()` or an
    unanticipated per-cycle check blocks (a slow network call that slipped onto
    the loop thread, a wedged DB sweep), task dispatch stops for every room with
    no other signal — the failure mode ISSUE-143 describes. This watchdog runs on
    its own daemon thread, watches a last-tick timestamp the loop bumps each
    iteration, and logs an ERROR plus fires one operator alert when the loop has
    gone silent for longer than ``stall_seconds``. It re-arms once the loop
    recovers, so a transient stall pages once rather than on every check.

    The three checks ISSUE-144 moved — the DB-health sweep, the DB-backup
    snapshot (Tier 1) and the nightly sleep cycles (Tier 2) — all run on
    background threads via ``_spawn_background_check``, and none of them
    suspends the watchdog. There are no ``suspended()`` call sites in
    ``run_daemon``.

    One synchronous network check remains on the loop thread:
    ``run_cleanup_checks`` step 5, the IMAP retention sweep. Its cost is bounded
    (``skills.email._MAX_DELETES_PER_SWEEP``, ~20 round trips) rather than
    proportional to the mailbox, so it does not normally approach
    ``stall_seconds`` — but a pathologically slow IMAP host can still page here,
    and moving it to ``_spawn_background_check`` is the fix if it ever does.

    ``suspended()`` is kept as the escape hatch for any future check that must
    run on the loop thread and is *known* to block for minutes — without it such
    a check would page every time it ran, drowning out the unexpected stalls
    this is meant to catch. Prefer ``_spawn_background_check``.
    """

    def __init__(self, config: Config, stall_seconds: int):
        self._config = config
        self._stall_seconds = stall_seconds
        self._last_tick = time.time()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._alerted = False
        self._suspended = False

    def tick(self) -> None:
        """Record a live loop iteration; re-arm after a recovery."""
        self._last_tick = time.time()
        if self._alerted:
            self._alerted = False
            logger.info("scheduler main loop recovered from stall")

    @contextlib.contextmanager
    def suspended(self):
        """Pause stall detection around a known-long synchronous check.

        Resets the tick on both entry and exit so the long operation is not
        counted as a stall and the first post-resume iteration starts clean.
        """
        self._suspended = True
        self._last_tick = time.time()
        try:
            yield
        finally:
            self._suspended = False
            self._last_tick = time.time()

    def start(self) -> None:
        if self._stall_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="loop-watchdog",
        )
        self._thread.start()
        logger.info(
            "STARTUP Started loop-stall watchdog (threshold %ds)",
            self._stall_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Poll at ~1/4 the stall window, clamped to a sane [5, 30]s band.
        interval = max(5.0, min(30.0, self._stall_seconds / 4))
        while not self._stop.wait(interval):
            if self._suspended:
                continue
            stalled_for = time.time() - self._last_tick
            if stalled_for >= self._stall_seconds and not self._alerted:
                self._alerted = True
                logger.error(
                    "scheduler main loop stalled: no dispatch tick for %.0fs "
                    "(threshold %ds) — task processing is blocked for all rooms",
                    stalled_for, self._stall_seconds,
                )
                self._fire_alert(stalled_for)

    def _fire_alert(self, stalled_for: float) -> None:
        user_id = _operator_alert_user(self._config)
        if not user_id:
            return

        # Deliver off the watchdog thread: the alert path itself goes through the
        # persistent asyncio loop (run_coro, timeout=None), and if *that* is what
        # is wedged a synchronous send would block the watchdog forever. A daemon
        # thread bounds the watchdog's exposure.
        def _deliver():
            try:
                send_notification(
                    self._config, user_id,
                    f"⚠️ Scheduler main loop stalled — no dispatch tick for "
                    f"{stalled_for:.0f}s. Task processing is blocked for all rooms.",
                    purpose="alert",
                )
            except Exception:  # noqa: BLE001
                logger.debug("loop-stall alert delivery failed", exc_info=True)

        threading.Thread(target=_deliver, daemon=True, name="loop-watchdog-alert").start()


def _emit_scheduler_stats(config: Config, pool: "WorkerPool | None") -> None:
    """Emit one ``scheduler_stats`` health line for the long-running daemon.

    Process-wide signal designed to surface resource leaks (the kind that ate
    the host in ISSUE-101) within single-digit minutes instead of days. Shape
    is space-separated ``key=value`` pairs, matching the devbox-proxy audit
    format, on the dedicated ``istota.scheduler.stats`` logger::

        scheduler_stats threads=42 fds=87 rss_mb=312 tasks_running=2 workers_active=3

    Cheap (sub-millisecond) and defensive: every collector degrades rather than
    raising. psutil-derived fields (fds, rss_mb) are omitted when psutil is
    unavailable (a single startup-style WARN, not one per emit); a DB hiccup
    yields ``tasks_running=?``; a missing pool yields ``workers_active=0``. The
    whole body is wrapped so a stats failure can never kill the daemon loop.
    """
    global _psutil_unavailable_warned
    try:
        parts = [f"threads={threading.active_count()}"]

        # fds + rss_mb via psutil. Each field is collected independently and
        # omitted on *any* failure (not just ImportError) — the line must still
        # emit. This matters most under fd exhaustion (the ISSUE-101 class):
        # psutil.num_fds() does os.listdir("/proc/self/fd"), which raises
        # OSError(EMFILE) precisely when the leak this line exists to catch is
        # at its worst. Dropping the whole line there would blind the operator
        # exactly when threads= is the signal they need.
        proc = None
        try:
            import psutil  # noqa: PLC0415  -- optional dep, lazy by design

            proc = psutil.Process()
        except ImportError:
            if not _psutil_unavailable_warned:
                logger.warning(
                    "scheduler_stats: psutil unavailable — "
                    "omitting fds/rss_mb from the health line",
                )
                _psutil_unavailable_warned = True
        except Exception:  # noqa: BLE001  -- AccessDenied / NoSuchProcess etc.
            pass

        if proc is not None:
            try:
                parts.append(f"fds={proc.num_fds()}")
            except Exception:  # noqa: BLE001  -- e.g. EMFILE under fd exhaustion
                pass
            try:
                parts.append(f"rss_mb={int(proc.memory_info().rss / 1024 / 1024)}")
            except Exception:  # noqa: BLE001
                pass

        # Running-task denominator. Never let a locked / mid-repair DB abort
        # the emit — degrade to '?' instead.
        try:
            with db.get_db(config.db_path) as conn:
                running = db.count_running_tasks(conn)
            parts.append(f"tasks_running={running}")
        except Exception:  # noqa: BLE001
            parts.append("tasks_running=?")

        workers_active = pool.active_count if pool is not None else 0
        parts.append(f"workers_active={workers_active}")

        # How long admission has been shut, 0 when open. Without this the health
        # line cannot tell a quiet daemon from a held one: gating the claim
        # paths means a shut gate drains the pool, so a squeeze with a full
        # backlog now reports workers_active=0 and tasks_running=0 — identical
        # to an idle night. The one WARNING per cooldown from dispatch() is the
        # only other signal, and it is far too sparse to read a squeeze off.
        if pool is not None:
            parts.append(f"admission_closed_s={int(pool.gate_closed_seconds())}")

        _SCHEDULER_STATS_LOGGER.info("scheduler_stats " + " ".join(parts))
    except Exception as exc:  # noqa: BLE001  -- stats must never crash the loop
        # Route to the stats logger so a consumer filtering by logger name (not
        # just grepping the message) sees the gap rather than silent absence.
        _SCHEDULER_STATS_LOGGER.warning(
            "scheduler_stats emit failed: %s", exc, exc_info=True,
        )


def _emit_host_pressure_breadcrumb() -> None:
    """Write one ``host_pressure`` line: the fixed-cadence memory breadcrumb.

    The record that makes the *next* memory incident attributable. On
    2026-08-20 the host died carrying 4.64 GB of unreclaimable shmem with no
    swap, and what created it could never be established — the tmpfs cleared on
    reboot, and no process in the OOM dump had the memory mapped. The only
    samples that existed came from the kernel's own OOM records, which left a
    five-day hole either side of the accumulation.

    So this runs unconditionally, on its interval, whether or not the box is
    under pressure: a slow leak never crosses a threshold until the day it is
    fatal. The field that does the work is ``shmem_unaccounted_kb`` — ``Shmem``
    minus the summed tmpfs usage — which separates memory some mount can be
    ``du``'d for from memory that lives in no filesystem at all.

    Costs six small file reads plus one ``statvfs`` and one ``stat`` per tmpfs
    mount, so it stays on the loop thread rather than paying for a thread every
    interval. Wrapped whole: an instrumentation failure must never take the
    daemon down, which would be the instrument causing the outage it exists to
    explain.

    The failure line is deliberately prefixed ``host_pressure_error`` and not
    ``host_pressure``. The documented way to retrieve the series is
    ``journalctl … | grep host_pressure``, so a failure notice sharing the
    record's prefix would land inside a parsed series as a row with no fields.
    """
    global _host_pressure_unavailable_warned
    try:
        from . import host_pressure  # noqa: PLC0415  -- leaf module, imported where used

        sample = host_pressure.read_sample()
        if sample is None:
            if not _host_pressure_unavailable_warned:
                logger.info(
                    "host_pressure: /proc/meminfo unreadable — no memory breadcrumb "
                    "on this host (not Linux, or not a procfs). PSI being absent is "
                    "not enough to reach here; the breadcrumb records what it can.",
                )
                _host_pressure_unavailable_warned = True
            return

        tmpfs = host_pressure.read_tmpfs_usage()
        # memory.events for the daemon's own cgroup. Stage 2 shipped
        # MemoryHigh=5G but nothing read the counter it moves, so a throttled
        # daemon looked exactly like a hung one: memory.high does not kill, it
        # applies an allocation-time sleep to every process in the cgroup —
        # the dispatch loop included. `high` rising across the series is what
        # separates "we are being slowed by our own limit" from "the host is
        # thrashing", and no other field on this line can tell them apart.
        events = host_pressure.read_memory_events()
        _HOST_PRESSURE_LOGGER.info(host_pressure.breadcrumb(sample, tmpfs, events))
    except Exception as exc:  # noqa: BLE001  -- instrumentation must never crash the loop
        _HOST_PRESSURE_LOGGER.warning(
            "host_pressure_error breadcrumb failed: %s", exc, exc_info=True,
        )


def _claimable_backlog(config: Config) -> int:
    """Total claimable tasks across both queues, or 0 if it cannot be read.

    The signal that survives the admission gate. `active_count` does not: a shut
    gate drains the pool within `worker_idle_timeout`, so "no workers" stops
    meaning "no work to do" the moment the gate closes. Whether anything is
    *queued* is independent of the gate, which is what makes it usable for
    telling "istota is waiting behind someone else" from "istota is the cause".

    Best-effort, like everything on this path: a locked DB reads as no backlog
    rather than raising into the alert.
    """
    timeout_ms = config.scheduler.main_loop_read_timeout_ms or None
    try:
        with db.get_db(config.db_path, busy_timeout_ms=timeout_ms) as conn:
            total = 0
            for users, queue in (
                (db.get_users_with_pending_fg_queue_tasks(conn), "foreground"),
                (db.get_users_with_pending_bg_queue_tasks(conn), "background"),
            ):
                for user_id in users:
                    total += db.count_claimable_tasks_for_user_queue(
                        conn, user_id, queue
                    )
            return total
    except Exception as exc:  # noqa: BLE001  -- alerting must never raise
        logger.warning("host_pressure_error backlog read failed: %s", exc)
        return 0


def _check_host_pressure(
    config: Config,
    pool: "WorkerPool",
    *,
    last_alert: float,
    alert_clocks: dict[str, float],
    background_checks: dict[str, threading.Thread],
    now: float,
) -> float:
    """Sample host memory, feed the admission gate, and snapshot on a crossing.

    Two jobs off one reading, and they are deliberately not the same test.
    The gate asks "is there room to start more work" and consults
    :func:`host_pressure.is_under_pressure`. The snapshot asks "is something
    happening we will want the evidence for" and consults
    :func:`host_pressure.snapshot_trigger`, which additionally fires on a large
    unaccounted-shmem residue. The production series is what forced the split:
    on 2026-08-21 the host took 1.52 GB of shmem in under five minutes with PSI
    at 0.07 and 2.9 GB still available. zram handled it, so refusing work would
    have been wrong — but that burst is the one event in 24 hours whose
    attribution anyone would want, and a single shared predicate can only get
    one of those two answers right.

    Returns the new ``last_alert`` clock and mutates ``alert_clocks``, which
    holds one cooldown window per trigger class. Both are unchanged on a tick
    that fired nothing, so a quiet stretch cannot push a window forward and
    silence a squeeze that starts later.

    Wrapped whole and never raises — the body lives in
    :func:`_check_host_pressure_inner` and this function is the net around it.
    That matters because it runs on the main loop thread and the call site is
    bare: an instrument that can take the daemon down is worse than no
    instrument, since it would produce exactly the unexplained outage this spec
    was written to prevent. The sample is handed to the pool before anything
    else is attempted, so a failure in the attribution half never costs the
    admission half its input.
    """
    try:
        return _check_host_pressure_inner(
            config,
            pool,
            last_alert=last_alert,
            alert_clocks=alert_clocks,
            background_checks=background_checks,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001  -- instrumentation must never crash the loop
        # The outer net. The inner blocks catch each reader at its own boundary
        # so one failure does not cost the others their result; this catches
        # everything between them — the config reads, the cooldown arithmetic,
        # anything a later edit adds outside a try. The main loop calls this
        # bare, on the strength of the promise above.
        logger.warning("host_pressure_error check failed: %s", exc, exc_info=True)
        return last_alert


def _check_host_pressure_inner(
    config: Config,
    pool: "WorkerPool",
    *,
    last_alert: float,
    alert_clocks: dict[str, float],
    background_checks: dict[str, threading.Thread],
    now: float,
) -> float:
    """The body of :func:`_check_host_pressure`. May raise; its caller catches."""
    if not config.scheduler.host_pressure_enabled:
        return last_alert

    try:
        sample = host_pressure_mod.read_sample()
    except Exception as exc:  # noqa: BLE001
        # Clear the reading before returning. Returning early here without
        # doing so would leave the *last* sample latched in the pool, and if
        # that sample was a starved one the gate stays shut for the life of
        # the process — dispatch spawning nothing, one WARNING per cooldown as
        # the only symptom. A sampler that has started throwing is exactly the
        # case the fail-open rule exists for, so it must clear like a `None`
        # return does rather than freeze the last thing it managed to read.
        logger.warning("host_pressure_error sample failed: %s", exc, exc_info=True)
        pool.update_pressure(None)
        return last_alert

    # Push the reading to the gate first, and push ``None`` through as well,
    # on the same reasoning as the except branch above.
    pool.update_pressure(sample)
    if sample is None:
        return last_alert

    tmpfs_read_ok = True
    try:
        tmpfs = host_pressure_mod.read_tmpfs_usage()
    except Exception as exc:  # noqa: BLE001
        logger.warning("host_pressure_error tmpfs read failed: %s", exc, exc_info=True)
        tmpfs = []
        tmpfs_read_ok = False

    try:
        # The residue arm is disarmed when the tmpfs read failed. Its
        # arithmetic is `Shmem - Σ tmpfs`, so the empty list the failure path
        # leaves behind makes the subtrahend zero and reports the *whole* of
        # Shmem as unaccounted — a false burst on any host with more than the
        # threshold in perfectly ordinary, perfectly accounted tmpfs. An empty
        # list here means "the read failed", not "there are none", and this
        # module is careful about that distinction everywhere else.
        reason = host_pressure_mod.snapshot_trigger(
            sample,
            tmpfs,
            psi_threshold=config.scheduler.host_pressure_psi_threshold,
            min_available_mb=config.scheduler.min_available_memory_mb,
            shmem_unaccounted_mb=(
                config.scheduler.host_pressure_shmem_unaccounted_alert_mb
                if tmpfs_read_ok
                else 0
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("host_pressure_error trigger failed: %s", exc, exc_info=True)
        return last_alert

    if reason is None:
        return last_alert

    # One cooldown per trigger class, not one for all three. The residue arm is
    # the one that fires while the host is healthy, so a single shared window
    # would let the cheapest alert suppress the most urgent: a residue notice
    # at t=0 would silence a MemAvailable collapse at t=60 for the rest of the
    # window, and that collapse is the one that halts dispatch.
    trigger_class = reason.split("=", 1)[0]
    cooldown = config.scheduler.host_pressure_alert_cooldown_seconds
    previous = alert_clocks.get(trigger_class, 0.0)
    # `now` is wall clock, so a backward NTP step can make this negative. Clamp
    # rather than compare raw: a negative delta is not "inside the window", and
    # reading it as one would mute every alert for the size of the step.
    if previous and 0 <= now - previous < cooldown:
        return last_alert

    alert_clocks[trigger_class] = now
    # Off the loop thread. The snapshot is the most expensive thing the daemon
    # does — a Docker round-trip per container at 2s apiece, the whole process
    # table, and a walk of every /proc/*/fd when the residue is large — and it
    # runs at exactly the moment I/O is slowest. Then the operator alert joins
    # for up to 30s on top. Inline, that is a minute of dispatch starvation
    # during an incident, and the spec's own Track B constraint says this must
    # never block the main loop. `_spawn_background_check` is the established
    # answer here and its in-flight guard also stops two snapshots stacking.
    # The sampling above stays inline: it is three file reads, and the gate
    # needs the reading this tick rather than whenever a thread gets to it.
    _spawn_background_check(
        "host_pressure_snapshot",
        lambda: _emit_host_pressure_snapshot(config, pool, sample, tmpfs, reason),
        background_checks,
    )
    return now


def _emit_host_pressure_snapshot(
    config: Config,
    pool: "WorkerPool",
    sample: "host_pressure_mod.PressureSample",
    tmpfs: "list[host_pressure_mod.TmpfsUsage]",
    reason: str,
) -> None:
    """Write the structured snapshot and send one operator alert. Never raises.

    The snapshot goes to the log at WARNING because it is the artefact someone
    will go looking for after the fact, and the log is the only surface that
    survives the host it describes. The notification is the operator's live
    signal — learning about this from a user asking "are we back?" is the
    failure mode being closed — and is best-effort on top: a wedged Talk must
    not stop the evidence being recorded.
    """
    # The running tasks' sandbox pids, so the snapshot can attribute the tmpfs
    # inside each bwrap namespace — the daemon's own mount table cannot see it,
    # and it is the most common holder of the residue that triggers the
    # snapshot in the first place (ISSUE-286). Read before the block so a
    # database that will not open costs the sandbox section rather than the
    # whole snapshot; `None` renders `sandbox not-queried`, which is what
    # actually happened, rather than `none-running`, which would be a claim.
    #
    # The dispatch loop's read timeout, not `get_db`'s 30s default. This runs
    # while the host is thrashing and the daemon may itself be `memory.high`
    # throttled, which is exactly when a lock is real — and a 30s wait would
    # put that much drift between the `sample=` figures captured earlier and
    # the block printed beside them, the drift `snapshot`'s own docstring says
    # makes a block read as a bug in the trigger. Timing out here degrades to
    # `not-queried` through the handler below, which is the honest answer.
    timeout_ms = config.scheduler.main_loop_read_timeout_ms
    try:
        with db.get_db(config.db_path, busy_timeout_ms=timeout_ms) as conn:
            task_pids = db.get_running_task_pids(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "host_pressure_error running-task pids unreadable: %s", exc, exc_info=True
        )
        task_pids = None

    snapshot_written = False
    try:
        # An empty socket path is the operator switching container lookup off,
        # so pass an explicit empty container list rather than a path that
        # cannot connect: the two produce the same snapshot but only one of
        # them says which it meant.
        socket_path = config.scheduler.host_pressure_docker_socket
        extra = (
            {"docker_socket": Path(socket_path)} if socket_path else {"containers": []}
        )
        block = host_pressure_mod.snapshot(
            sample=sample, tmpfs=tmpfs, task_pids=task_pids, **extra
        )
        logger.warning("%s\n  trigger=%s", block, reason)
        snapshot_written = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("host_pressure_error snapshot failed: %s", exc, exc_info=True)

    try:
        user = _operator_alert_user(config)
        if not user:
            return

        # Say what is actually happening to the queue, not what usually
        # happens when a snapshot fires. The residue arm of snapshot_trigger
        # deliberately leaves admission open, so on the 2026-08-21 burst — the
        # event that arm exists for — an unconditional "workers are held" would
        # tell the operator the queue is stalled when it is running normally.
        # The pure predicate, not `_admission_open()`. The bookkeeping variant
        # stamps `_gate_closed_since` when it is None, so asking it here would
        # reset the clock that `gate_closed_seconds()` is read from a few lines
        # below — the alert would suppress its own escalation on every pass
        # where the snapshot happened to ask first. It also runs off the loop
        # thread, where it would spend dispatch's once-per-cooldown log budget.
        gate_shut = not pool.admission_open()
        if gate_shut:
            queue_state = (
                "No new task starts while this lasts; running tasks are untouched."
            )
        else:
            queue_state = (
                "Admission is still open and the queue is unaffected — this is a "
                "record for attribution, not a stall."
            )

        # istota as victim rather than cause: the memory is being taken by
        # something else on the box, which is a different remedy and one the
        # operator cannot guess from a generic pressure alert.
        #
        # The discriminator is the *backlog*, not the worker count. Worker count
        # used to carry that signal — a squeeze with queued work kept lingering
        # workers claiming, so `active_count > 0` meant "istota is busy". Gating
        # the claim paths ended that: a shut gate now drains the pool in
        # `worker_idle_timeout` (10s by default), far inside the cooldown window
        # this compares against, so `active_count` is 0 by then whatever the
        # cause. Keying on it would print "istota is a bystander" during exactly
        # the 2026-08-20 shape, where istota's own task left the residue behind,
        # and send the operator looking off-box.
        held = _claimable_backlog(config)
        if (
            gate_shut
            and pool.active_count == 0
            and held == 0
            and pool.gate_closed_seconds()
            >= config.scheduler.host_pressure_alert_cooldown_seconds
        ):
            queue_state += (
                "\nNo istota worker is running and nothing is queued, so the "
                "memory is being held by something else on the host — istota is "
                "waiting behind it, not causing it."
            )
        elif gate_shut and held:
            queue_state += f"\n{held} task(s) are queued and waiting for the gate."

        evidence = (
            "A host_pressure_snapshot naming the holders is in the log."
            if snapshot_written
            else "The snapshot could not be gathered; see host_pressure_error in the log."
        )
        message = (
            f"⚠️ Host memory pressure — {reason}\n"
            f"MemAvailable {sample.mem_available_kb // 1024} MB, "
            f"Shmem {sample.shmem_kb // 1024} MB, "
            f"swap {(sample.swap_total_kb - sample.swap_free_kb) // 1024}"
            f"/{sample.swap_total_kb // 1024} MB used.\n"
            f"{queue_state} {evidence}"
        )
        _send_operator_alert(config, user, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("host_pressure_error alert failed: %s", exc, exc_info=True)


def _effective_processed_email_retention(sched: SchedulerConfig) -> int:
    """How long a ``processed_emails`` row is kept, in days. 0 = never prune.

    The configured window, floored by the mail's own lifetime. The two are
    coupled: the row is the only thing stopping a message that is still
    physically in ``poll_folder`` from being re-ingested as a fresh task, and
    ``email_id`` is a bare IMAP UID with no ``UIDVALIDITY`` and no folder
    qualifier, so a re-ingest is indistinguishable from new mail. Pruning
    inside the mail's own lifetime would trade unbounded growth for duplicate
    tasks and duplicate replies to strangers — a worse failure.

    ``email_retention_days = 0`` means mail is *never* deleted from IMAP, so
    the message's lifetime is unbounded and the row's has to be too: the prune
    is disabled outright rather than left unfloored.

    The floor is ``email_retention_days + 1``, not equal to it. The IMAP sweep
    searches ``BEFORE <date>``, which is date-granular and evaluated in the
    mail server's zone, while ``processed_at`` is an exact UTC timestamp — so a
    message on the boundary outlives an exactly-equal window by up to a day.

    A floor only, never a cap: an operator who wants the ledger kept far longer
    than the mailbox gets exactly that.

    One residual the floor does *not* model: the IMAP sweep is bounded per run
    (``skills.email._MAX_DELETES_PER_SWEEP``), so while a backlog is draining a
    message's real lifetime is ``email_retention_days`` plus however many ticks
    it waits its turn, not exactly ``email_retention_days``. The default 90-day
    window sits far above the 8-day floor, so this only bites an operator who
    has tuned the ledger window down to the floor *and* is draining a backlog.
    Worth knowing before treating the +1 as a tight bound.
    """
    configured = sched.processed_email_retention_days
    if configured <= 0:
        return 0
    if sched.email_retention_days <= 0:
        _warn_once(
            "processed_email_retention_floor_disabled",
            "processed_email_retention_days is set but email_retention_days is 0, "
            "so mail is never deleted from IMAP and a pruned dedup row could let "
            "an old message be re-ingested as a new task; skipping the prune",
        )
        return 0

    floor = sched.email_retention_days + 1
    if floor > configured:
        _warn_once(
            "processed_email_retention_floored",
            "processed_email_retention_days (%d) is below email_retention_days "
            "(%d); using %d so a message still in the mailbox can't lose its "
            "dedup row and be re-ingested as a new task"
            % (configured, sched.email_retention_days, floor),
        )
        return floor
    return configured


def _confirmation_notice_token(task_info: dict) -> str | None:
    """The Talk room an expiry notice may fall back to, or None.

    The old code passed ``conversation_token`` verbatim, which for an email gate
    is the synthetic `compute_thread_id` hash — not a room token at all, so the
    notice posted into nothing and the user was never told their mail had been
    dropped (ISSUE-241). Same shape as `_talk_target_for_delivery`: a synthetic
    thread id or a stream-surface token is not a Talk channel. Returning None
    lets the routing ladder (alerts_channel → briefing → DM) resolve one.
    """
    from .email_support import is_synthetic_email_thread_token

    token = task_info.get("conversation_token")
    if not token:
        return None
    if token.startswith(("web-", "repl-")):
        return None
    if is_synthetic_email_thread_token(token):
        return None
    return token


def _write_undelivered_row(
    config: Config, task: db.Task, title: str | None, message: str,
) -> int | None:
    """Record a task result that reached nobody, on a connection of its own.

    Safe to open one here: this runs at the tail of `process_one_task`, after
    every write transaction the function held has closed — the same invariant
    the `send_notification` call beside it already depends on.

    Fire-and-forget by construction. Nothing will ever change to close this row,
    so `task_alert` closes it on being seen, and `sweep_expired_alerts` catches
    the ones nobody looks at.
    """
    try:
        with db.get_db(config.db_path) as conn:
            result = task_alert_source.write(
                conn, task.user_id,
                dedup_key=task_alert_source.undelivered_key(task.id),
                title=title or f"A task result could not be delivered — task #{task.id}",
                body=message,
                severity="warning",
                # There is no in-app action: the answer is in the row and in
                # `tasks.result`, and retrying the delivery — resending the mail,
                # or asking again once Talk is back — is the user's move.
                actionable=False,
                params={"task_id": task.id, "source_type": task.source_type},
                room_token=task.conversation_token,
            )
        return result.notification_id if result is not None else None
    except Exception:
        logger.warning(
            "could not record the undelivered-result notification for task %s",
            task.id, exc_info=True,
        )
        return None


def _expired_confirmation_notice(conn, task_info: dict) -> str:
    """Say *which* request expired, not just that one did.

    A bare "your pending confirmation timed out" is unactionable: the user has
    no way to tell which message was dropped, and for inbound mail the message
    was marked processed when it was gated, so nothing re-polls it. Naming the
    sender and subject is what makes going back to the mailbox possible.
    """
    from . import confirmations

    lead = "A request that was waiting for your confirmation timed out and was cancelled."
    if task_info.get("source_type") == "email":
        try:
            task = db.get_task(conn, task_info["id"])
            # Through the shared describer, so the sender and subject are
            # truncated and stripped of markup here too — this notice can land
            # in Talk (markdown), in an email, or in an ntfy header, and the
            # subject is a stranger's text.
            label = confirmations.describe(conn, task) if task else None
        except Exception:
            label = None
        if label:
            return (
                f"{lead}\n\nIt was {label}. It is still in the mailbox; forward "
                "it again, or add the sender with `!trust` if you want it "
                "processed without asking."
            )
    return f"{lead}\n\nSubmit it again if you still need it."


def run_cleanup_checks(config: Config) -> None:
    """
    Run all cleanup checks for scheduler robustness.
    Call periodically from daemon loop.

    Synchronous: the rare notices (expired-confirmation / failed-ancient)
    go through the sync ``send_notification`` dispatcher, which routes Talk
    through the persistent loop. The body otherwise does blocking DB / IMAP /
    filesystem cleanup that must NOT run on the persistent asyncio loop.
    """
    sched = config.scheduler

    # Composed inside the transaction below, delivered after it closes. The
    # notice routes by purpose now, so it can land on the web surface, whose
    # delivery opens a second connection to this database — inline it would
    # block on the write lock `expire_stale_confirmations` takes, for the full
    # busy timeout, per expired task, on the dispatch loop.
    # `(user_id, message, conversation_token, notification_id)`. The id is the
    # inbox row raised in the same transaction that expired the task; the send
    # below stamps `last_delivered_at` on it only where a destination accepted.
    # The send is kept here rather than moved onto `deliver_pending` because it
    # carries a `conversation_token` override the store deliberately does not
    # model — `room_token` on a row is provenance, not routing.
    expiry_notices: list[tuple[str, str, str | None, int | None]] = []
    # The ancient-pending notice, buffered for the same reason. It used to be
    # sent inline because a bare `talk` route touched no database; the ISSUE-242
    # transcript mirror made that untrue.
    ancient_notices: list[tuple[str, str, str | None]] = []

    with db.get_db(config.db_path) as conn:
        # 1. Expire stale confirmations; the user is told after the transaction.
        expired = db.expire_stale_confirmations(conn, sched.confirmation_timeout_minutes)
        for task_info in expired:
            logger.info(
                f"Expired stale confirmation: task {task_info['id']} "
                f"(user: {task_info['user_id']})"
            )
            notice = _expired_confirmation_notice(conn, task_info)
            # The question is gone, so the inbox item is answered — by the
            # clock, which is what `resolved_by='system'` records. On this
            # connection, inside the transaction the expiry itself ran in.
            # Closing it is what stops the panel offering a Confirm button for a
            # task that has already been cancelled.
            confirmation_source.resolve_for_task(
                conn, task_info["user_id"], task_info["id"], by="system",
            )
            # And a fire-and-forget row takes its place, saying what was
            # dropped. A separate row rather than a reuse of the confirmation
            # one: they are different items with different lifecycles — the
            # confirmation closed, this one closes when the user reads it — and
            # reopening a resolved row under the old key would put a Confirm
            # button back on a cancelled task the moment a resolver lagged.
            expired_row = task_alert_source.write(
                conn, task_info["user_id"],
                dedup_key=task_alert_source.expired_key(task_info["id"]),
                title=f"A request timed out — task #{task_info['id']}",
                body=notice,
                severity="warning",
                # Nothing to press. Resubmitting the request happens wherever it
                # came from, which the notice text names.
                actionable=False,
                params={"task_id": task_info["id"],
                        "source_type": task_info.get("source_type")},
                room_token=_confirmation_notice_token(task_info),
            )
            expiry_notices.append((
                task_info["user_id"],
                notice,
                _confirmation_notice_token(task_info),
                expired_row.notification_id if expired_row is not None else None,
            ))

        # 1b. Recover stuck locked/running tasks (mirrors claim_task recovery
        # but runs even when no tasks are being claimed)
        stuck = db.fail_stuck_locked_running_tasks(
            conn, sched.max_retry_age_minutes,
            stuck_running_minutes=_stuck_running_minutes(sched),
            heartbeat_stuck_minutes=sched.worker_stuck_minutes,
        )
        for task_info in stuck:
            logger.warning(
                "Recovered stuck task: task %d (user: %s, status: %s)",
                task_info["id"], task_info["user_id"],
                task_info.get("source_type", "unknown"),
            )

        # 2. Log warnings for stale pending tasks
        stale_tasks = db.get_stale_pending_tasks(conn, sched.stale_pending_warn_minutes)
        for task in stale_tasks:
            logger.warning(
                f"Stale pending task detected: task {task.id} "
                f"(user: {task.user_id}, source: {task.source_type}, "
                f"created: {task.created_at})"
            )

        # 3. Fail ancient pending tasks and notify users
        failed = db.fail_ancient_pending_tasks(conn, sched.stale_pending_fail_hours)
        for task_info in failed:
            logger.warning(
                f"Auto-failed ancient pending task: task {task_info['id']} "
                f"(user: {task_info['user_id']}, source: {task_info['source_type']})"
            )
            # Notify only for tasks the user actually submitted. Automated tasks
            # (scheduled jobs, briefings, heartbeats, subtasks) pile up on their
            # own when the queue wedges; notifying their output channel turns one
            # stuck worker into a per-minute "task cancelled" flood — the message
            # ("A task you submitted…") isn't even true for them.
            if task_info["source_type"] in _AUTOMATED_SOURCE_TYPES:
                continue
            # Buffered, not sent here — same rule as `expiry_notices` above, and
            # now for the same reason on *both* legs of the route. A `web`
            # destination always opened a second connection to this database;
            # since ISSUE-242 a `talk` one does too (the transcript mirror), so
            # sending inside this write transaction would block the second
            # connection until the busy timeout on the dispatch thread.
            if task_info["conversation_token"] and config.nextcloud.url:
                ancient_notices.append((
                    task_info["user_id"],
                    "A task you submitted was cancelled because it was pending too long "
                    "without being processed. Please try again or contact support if this "
                    "keeps happening.",
                    task_info["conversation_token"],
                ))

        # 4. Clean up old completed tasks
        deleted_count = db.cleanup_old_tasks(conn, sched.task_retention_days)
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old task(s)")

        # 4a. Prune the processed-email dedup ledger (ISSUE-231). One row per
        # polled message, including the ones that produce nothing, and until
        # now nothing ever deleted from it.
        email_rows = db.cleanup_old_processed_emails(
            conn, _effective_processed_email_retention(sched),
        )
        if email_rows > 0:
            logger.info(f"Pruned {email_rows} processed-email row(s)")

        # 4b. Age out the per-message deletion ledger. It exists only to tell a
        # live client what vanished; one further behind than this reloaded from
        # scratch long ago. Fixed 30 days rather than a knob — the row is tiny
        # and nothing about it is worth tuning.
        pruned = db.prune_message_deletions(conn, _MESSAGE_DELETION_RETENTION_DAYS)
        if pruned > 0:
            logger.info(f"Pruned {pruned} message-deletion ledger row(s)")

    # 4c. Prune token/cost rows, in a transaction of its own. The block above is
    # one long write transaction and this retention window is 180 days against
    # the task table's 7, so the delete it issues on the day it finally bites is
    # far larger than anything up there. Holding the write lock for it would
    # stall the dispatch loop's readers on their busy timeout.
    try:
        with db.get_db(config.db_path) as conn:
            usage_rows = db.prune_old_usage(conn, sched.usage_retention_days)
        if usage_rows > 0:
            logger.info(f"Pruned {usage_rows} usage row(s)")
    except Exception as e:
        # Swallowed — telemetry retention is never worth failing a cleanup pass
        # over. But logged at warning, not debug: this is the only thing bounding
        # a table that gains a row per brain attempt, and a prune that has been
        # silently failing for weeks looks exactly like one that is working.
        logger.warning(f"Usage prune skipped: {e}")

    # 4e. The two notification-inbox sweeps, in a transaction of their own and
    # for the same reason 4c is: the block above is one long write transaction,
    # and the retention delete on the day it first bites clears a whole backlog.
    #
    # `sweep_expired_alerts` is the backstop for the fire-and-forget class.
    # `task_alert` rows have no object to watch, so the only thing that closes
    # one is being seen — and a row below the render limit, or one belonging to a
    # user who never opens the bell, is never seen. Without this pass, "open rows
    # are never swept" plus "only rendered rows auto-resolve" means the badge
    # climbs monotonically forever. Open rows of the *object-backed* sources are
    # untouched at any age: their close condition is the object.
    #
    # `sweep_retention` is the other end — a closed row is kept for reopen and
    # for post-hoc debugging, then deleted.
    #
    # A transaction each, not one shared between them. `db.get_db` commits once
    # on exit, so a shared block would hold the write lock from the first row
    # the age sweep closes right through the retention delete — and the
    # retention delete is the larger of the two by far on the day it first
    # bites. Splitting them is the same move 4c makes for the usage prune.
    try:
        with db.get_db(config.db_path) as conn:
            closed_alerts = sweep_expired_alerts(conn)
        with db.get_db(config.db_path) as conn:
            deleted_notifications = sweep_retention(conn)
        if closed_alerts or deleted_notifications:
            logger.info(
                "Notification sweeps: closed %d aged alert(s), deleted %d "
                "retired row(s)",
                closed_alerts, deleted_notifications,
            )
    except Exception as e:
        # Both sweeps swallow their own failures already; this catches a failure
        # to open the connection at all. Warning rather than debug for the reason
        # the usage prune is: these are the only things bounding a table that
        # gains a row per alert, and one silently failing for weeks looks exactly
        # like one that is working.
        logger.warning(f"Notification sweeps skipped: {e}")

    # 4d. Tell users about the confirmations that just expired. Outside the
    # transaction on purpose — see `expiry_notices` above.
    expiry_delivered: list[int] = []
    for user_id, message, token, notification_id in expiry_notices:
        try:
            delivered = send_notification(
                config, user_id, message,
                purpose="alert", conversation_token=token,
            )
        except Exception as e:
            logger.error(f"Failed to notify user about expired confirmation: {e}")
            continue
        if delivered and notification_id is not None:
            expiry_delivered.append(notification_id)
    if expiry_delivered:
        try:
            with db.get_db(config.db_path) as conn:
                mark_delivered(conn, expiry_delivered)
        except Exception:
            logger.debug(
                "could not stamp expired-confirmation notification delivery",
                exc_info=True,
            )

    # 4d. And about the ancient pending tasks that were just auto-failed.
    for user_id, message, token in ancient_notices:
        try:
            send_notification(config, user_id, message, conversation_token=token)
        except Exception as e:
            logger.error(f"Failed to notify user about failed task: {e}")

    # 5. Clean up old emails from IMAP (outside db context)
    if config.email.enabled and sched.email_retention_days > 0:
        try:
            from .email_support import cleanup_old_emails
            deleted_emails = cleanup_old_emails(config, sched.email_retention_days)
            if deleted_emails > 0:
                logger.info(f"Deleted {deleted_emails} old email(s) from IMAP inbox")
        except Exception as e:
            logger.error(f"Error cleaning up old emails: {e}")

    # 6. Clean up talk message cache
    with db.get_db(config.db_path) as conn:
        deleted_msgs = db.cleanup_old_talk_messages(conn, sched.talk_cache_max_per_conversation)
        if deleted_msgs > 0:
            logger.info(f"Cleaned up {deleted_msgs} old talk message(s)")

    # 7. Clean up old temp files
    if sched.temp_file_retention_days > 0:
        try:
            deleted_files = cleanup_old_temp_files(config, sched.temp_file_retention_days)
            if deleted_files > 0:
                logger.info(f"Deleted {deleted_files} old temp file(s)")
        except Exception as e:
            logger.error(f"Error cleaning up temp files: {e}")

    # 7b. Clean up old native-brain session logs
    #
    # The **only** caller of the sweep. The feature ships enabled and the writer
    # appends for every native task attempt, onto the filesystem the framework
    # database is writing to, so without this step nothing on the deployment
    # ever deletes one.
    #
    # `enabled` controls the writer, not this sweep. A deployment that switches
    # writing off still needs to retire the transcripts already on disk.
    #
    # The gate is `or`, not `and`: the age rule and the disk ceiling bound
    # different things and neither implies the other. An operator who sets
    # `retention_days = 0` to keep everything indefinitely still wants the
    # ceiling enforced, and wiring this as `and` would silently disable it —
    # which is the exact failure the ceiling exists to prevent.
    #
    # Reads `config.brain.native` directly rather than going through
    # `resolve_brain_kind`, because this is about a directory on disk and not
    # about which brain a given task would route to. A deployment that switched
    # away from native still sweeps the logs native left behind.
    _slog = config.brain.native.session_log
    if _slog.retention_days > 0 or _slog.max_total_gb > 0:
        try:
            _sweep = sweep_session_logs(
                resolve_session_log_dir(config.db_path, _slog.dir),
                retention_days=_slog.retention_days,
                max_total_gb=_slog.max_total_gb,
                now=time.time(),
            )
            if _sweep.deleted_age or _sweep.deleted_size:
                logger.info(
                    "Session logs: deleted %d aged, %d over ceiling, %d dir(s); "
                    "%.1f MB remain",
                    _sweep.deleted_age, _sweep.deleted_size, _sweep.dirs_removed,
                    _sweep.bytes_after / 1_048_576,
                )
            if _sweep.errors:
                logger.warning(
                    "Session log sweep: %d path(s) could not be processed", _sweep.errors,
                )
            _record_session_log_sweep(config, _sweep)
        except Exception as e:
            logger.error(f"Error cleaning up session logs: {e}")

    # 8. Clean up old location pings (per-user location.db)
    if sched.location_ping_retention_days > 0:
        from . import location as _location  # noqa: PLC0415

        total_pings = 0
        # Reuse one framework-DB conn for the per-user is_module_enabled
        # checks to avoid opening N+ short-lived sqlite connections per
        # cleanup tick (one of the FD-churn paths that produced EMFILE).
        with db.get_db(config.db_path) as fw_conn:
            enabled_users = _location.list_users(config, conn=fw_conn)
            for uid in enabled_users:
                try:
                    ctx = _location.resolve_for_user(uid, config, conn=fw_conn)
                except _location.UserNotFoundError:
                    continue
                # No location.db yet (user never ingested a ping) → nothing
                # to clean up. The file + parent dir are created on first
                # webhook write; connecting before then raises "unable to
                # open database file".
                if not ctx.db_path.exists():
                    continue
                try:
                    with _location.connect(ctx.db_path) as conn:
                        deleted = _location.db.cleanup_old_pings(
                            conn, sched.location_ping_retention_days,
                        )
                        conn.commit()
                    total_pings += deleted
                except Exception:
                    logger.exception(
                        "Failed to clean up location pings for user=%s", uid,
                    )
        if total_pings > 0:
            logger.info(f"Cleaned up {total_pings} old location ping(s)")

    # 8b. Reconcile visits from pings (batch cleanup of state-machine drift)
    if config.location.reconcile_enabled:
        try:
            _reconcile_visits_for_all_users(config)
        except Exception as e:
            logger.error(f"Error reconciling visits: {e}")

    # 9. Clean up old Claude session logs
    if sched.temp_file_retention_days > 0:
        try:
            deleted_logs = cleanup_old_claude_logs(sched.temp_file_retention_days)
            if deleted_logs > 0:
                logger.info(f"Deleted {deleted_logs} old Claude session log(s)")
        except Exception as e:
            logger.error(f"Error cleaning up Claude logs: {e}")

    # 10. Tell users about outbound drafts they have left unanswered.
    try:
        nag_stale_outbound_drafts(config)
    except Exception as e:
        logger.error(f"Error notifying about stale outbound drafts: {e}")


# How long a held outbound draft sits before it raises a notification of its
# own. Fixed rather than a knob: it governs one reminder about the user's own
# unfinished decision, and there is nothing to tune between "long enough that a
# same-day answer is never nagged" and "soon enough that a reply the user
# believes went out is not discovered days later".
STALE_DRAFT_HOURS = 24


def nag_stale_outbound_drafts(config: Config) -> int:
    """One notification per outbound draft left pending for a day. Returns the
    count delivered.

    Not a briefing item and not an expiry. Held drafts never expire — binning
    the user's own intended reply after a timeout loses work with no trace,
    which is why `expire_stale_confirmations` does not touch this table — so the
    only thing that surfaces a forgotten draft is this. It matters most for a
    draft with no room (a cron job mailing an external address), where there is
    no card anywhere to notice.

    `purpose="alert"` routes through the user's own routing table, so it lands
    wherever they have alerts pointed rather than on a surface they may not
    read.

    Stamped **after** delivery: `send_notification` returns False for "no
    destination configured" rather than raising, and stamping on the decision
    rather than the delivery would let one silent failure swallow the reminder
    permanently. A failed send leaves `nagged_at` NULL and the next sweep
    retries.
    """
    from . import outbound_drafts as drafts

    # Read and deliver in separate transactions, and deliver outside both: an
    # alert routed to the web surface opens a second connection to this
    # database, and sending inside a write transaction would block it for the
    # full busy timeout on the dispatch thread. Same rule as `expiry_notices`.
    with db.get_db(config.db_path) as conn:
        stale = drafts.stale_unnagged(conn, older_than_hours=STALE_DRAFT_HOURS)
    if not stale:
        return 0

    delivered: list[int] = []
    for draft in stale:
        recipients = ", ".join(draft.all_recipients) or "an unnamed recipient"
        subject = (draft.subject or "(no subject)").replace("\n", " ")
        # No imperative aimed at *this* notification: it routes by the user's
        # alert purpose, which may be ntfy or email — surfaces with no composer,
        # where "reply with !drafts send" is an instruction that cannot be
        # followed. Name the commands and where they work instead.
        message = (
            f"An email to {recipients} has been waiting for your approval "
            f"since {draft.created_at} and has not gone out.\n\n"
            f"Subject: {subject}\n\n"
            f"In Talk or web chat: `!drafts` lists it, `!drafts send {draft.id}` "
            f"releases it, `!drafts discard {draft.id}` bins it."
        )
        try:
            sent = send_notification(
                config, draft.user_id, message, purpose="alert",
            )
        except Exception as e:
            logger.error(
                "Failed to notify user %s about stale draft %d: %s",
                draft.user_id, draft.id, e,
            )
            continue
        if sent:
            delivered.append(draft.id)
        else:
            logger.warning(
                "No destination for the stale-draft notice for user %s "
                "(draft %d); will retry next sweep", draft.user_id, draft.id,
            )

    if delivered:
        with db.get_db(config.db_path) as conn:
            for draft_id in delivered:
                drafts.mark_nagged(conn, draft_id)
        logger.info("Notified about %d stale outbound draft(s)", len(delivered))
    return len(delivered)


def _reconcile_visits_for_all_users(config: Config) -> None:
    """Re-derive the visits table for each user with the location module
    enabled.

    Operates on a window ending ``reconcile_buffer_minutes`` before now
    so the currently-open visit is never rewritten. Only closed visits
    in the window are replaced.

    Per-user file scope: every user with an enabled location module is
    a candidate. The legacy ``[[resources]]`` overland filter was dead
    code post-modules-refactor (the overland token moved into the
    secrets table) and is gone.
    """
    from . import location as _location  # noqa: PLC0415

    loc = config.location
    now = datetime.now(timezone.utc)
    until = now - timedelta(minutes=loc.reconcile_buffer_minutes)
    since = until - timedelta(hours=loc.reconcile_lookback_hours)
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_s = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    # One framework-DB conn for the module-enabled lookups across users —
    # avoids the FD churn that triggered EMFILE on prod.
    with db.get_db(config.db_path) as fw_conn:
        enabled_users = _location.list_users(config, conn=fw_conn)
        for uid in enabled_users:
            try:
                ctx = _location.resolve_for_user(uid, config, conn=fw_conn)
            except _location.UserNotFoundError:
                continue
            # No location.db yet (user never ingested a ping) → no visits
            # to reconcile. See the matching guard in run_cleanup_checks.
            if not ctx.db_path.exists():
                continue
            try:
                with _location.connect(ctx.db_path) as conn:
                    n = _location.db.reconcile_visits(
                        conn, since_s, until_s,
                        grace_minutes=loc.reconcile_grace_minutes,
                        min_pings=loc.reconcile_min_pings,
                        min_dwell_sec=loc.reconcile_min_dwell_sec,
                        accuracy_threshold_m=loc.accuracy_threshold_m,
                    )
                    conn.commit()
                if n:
                    logger.info(
                        "Reconciled %d visit(s) for user=%s window=[%s,%s)",
                        n, uid, since_s, until_s,
                    )
            except Exception:
                logger.exception(
                    "Visit reconciliation failed for user=%s", uid,
                )


def cleanup_old_claude_logs(retention_days: int) -> int:
    """
    Delete old Claude session logs from ~/.claude/{projects,debug,todos}.

    Returns count of deleted files.
    """
    home = Path(os.environ.get("HOME", "/tmp"))
    claude_dir = home / ".claude"
    if not claude_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 24 * 60 * 60)
    deleted = 0

    cleanup_specs = [
        (claude_dir / "projects", "*.jsonl"),
        (claude_dir / "debug", "*.txt"),
        (claude_dir / "todos", "*.json"),
    ]

    for base_dir, pattern in cleanup_specs:
        if not base_dir.exists():
            continue
        for path in base_dir.rglob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except Exception as e:
                logger.debug("Could not delete claude log %s: %s", path, e)

        # Clean up empty subdirectories (walk bottom-up)
        for dirpath in sorted(base_dir.rglob("*"), reverse=True):
            if dirpath.is_dir():
                try:
                    dirpath.rmdir()  # only succeeds if empty
                except OSError:
                    pass

    return deleted


def _sync_cron_files(conn, app_config: Config) -> None:
    """Sync CRON.md files to DB for all configured users."""
    from .cron_loader import (
        _MODULE_JOB_PREFIX,
        load_cron_document,
        migrate_db_jobs_to_file,
        sync_cron_jobs_to_db,
    )

    for user_id in app_config.users:
        try:
            doc = load_cron_document(app_config, user_id)
            if doc is not None:
                # Count only user-defined DB jobs when deciding whether to
                # migrate-to-file; module-managed jobs don't belong in CRON.md.
                user_db_jobs = [
                    j for j in db.get_user_scheduled_jobs(conn, user_id)
                    if not j.name.startswith(_MODULE_JOB_PREFIX)
                ]
                # A file nobody has authored a job list into, with rows in
                # the table: the seeded template this branch was written for,
                # so write the rows into the file rather than wiping them.
                # `is_template` is no fence at all *or* a fence holding only
                # comments, which is what `storage.CRON_TEMPLATE` seeds; the
                # docstring on it has the reasoning.
                #
                # An **empty** fence is not that, and used to reach here too:
                # both read as zero jobs, so deleting your last job from
                # CRON.md restored it from the table within the minute and
                # rewrote the document doing it (ISSUE-369 defect 2). It now
                # takes the sync path below, where the orphan sweep deletes
                # the row and the file is left alone.
                if doc.is_template and user_db_jobs:
                    if not migrate_db_jobs_to_file(
                        conn, app_config, user_id, overwrite=True
                    ):
                        # Every reason for a False here is a failure: the file
                        # was read a moment ago, so the mount is configured,
                        # and `user_db_jobs` is non-empty. The rows are intact
                        # and the next tick tries again; say so rather than
                        # leaving a user with a file that never fills in.
                        logger.warning(
                            "Could not restore %d scheduled job(s) into CRON.md "
                            "for %s; the rows are unchanged and the next sync "
                            "will retry", len(user_db_jobs), user_id,
                        )
                elif not doc.jobs and not doc.states_no_jobs:
                    # The file lists jobs and the parser could use none of
                    # them — an unreadable `prompt_file`, a typo in a key.
                    # Syncing that would orphan-delete every row while the
                    # definitions sit in the file, and nothing would bring
                    # them back: the next tick reads the same file the same
                    # way. Hold, the way an unparseable fence is held.
                    logger.warning(
                        "CRON.md for %s lists jobs and none of them could be "
                        "read; leaving the %d existing scheduled job(s) alone "
                        "until the file is fixed", user_id, len(user_db_jobs),
                    )
                else:
                    sync_cron_jobs_to_db(
                        conn, user_id, doc.jobs,
                        is_admin=app_config.is_admin(user_id),
                    )
            else:
                # No CRON.md — try one-time migration from DB. False here is
                # the ordinary case (nothing to migrate, or the file already
                # exists), not a failure, so it is not logged; the writer logs
                # a refused write itself.
                migrate_db_jobs_to_file(conn, app_config, user_id)
        except Exception as e:
            logger.error("Error syncing CRON.md for %s: %s", user_id, e)

    # Sync module-managed jobs (e.g. money's run_scheduled).
    # These are not user-editable via CRON.md; their definitions come from
    # the module's jobs.py and the user's istota resource entry.
    try:
        _sync_money_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing money module jobs: %s", e)

    try:
        _sync_feeds_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing feeds module jobs: %s", e)

    try:
        _sync_health_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing health module jobs: %s", e)


def _sync_module_jobs(
    conn,
    app_config: Config,
    *,
    module_name: str,
    module_prefix: str,
    resolve_for_user,
    jobs_for_user,
    user_not_found_exc: type[Exception],
    on_first_seed=None,
) -> None:
    """Seed/refresh ``{module_prefix}*`` scheduled jobs from each user's module config.

    Shared engine for ``_sync_money_module_jobs`` and
    ``_sync_feeds_module_jobs``. Idempotent: existing rows are updated when
    cron or command differs; obsolete rows are deleted; users with the
    module disabled have all their ``{module_prefix}*`` rows cleaned up.

    ``on_first_seed(conn, user_id, job_dict)`` — optional callback invoked
    once after each freshly-inserted row, before commit. Feeds uses this to
    queue an immediate one-shot poll for the ``run_scheduled`` row so newly
    provisioned users don't wait up to 5 minutes for the first tick.
    """
    for user_id in app_config.users:
        uc = app_config.users.get(user_id)
        if uc is None:
            continue

        if not app_config.is_module_enabled(user_id, module_name, conn=conn):
            # Drop any stale module rows
            conn.execute(
                "DELETE FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
                (user_id, f"{module_prefix}%"),
            )
            continue

        try:
            module_ctx = resolve_for_user(user_id, app_config, conn=conn)
        except user_not_found_exc as e:
            logger.warning(
                "Could not resolve %s config for %s: %s", module_name, user_id, e,
            )
            continue

        wanted = jobs_for_user(module_ctx, user_id)
        wanted_by_name = {j["name"]: j for j in wanted}

        # Rescue suspended module rows. A module job has no `!cron enable`
        # to run against it — it is not in anybody's CRON.md — so if the
        # daemon gives up on one, only this retries it.
        #
        # ``auto_disabled_at IS NOT NULL`` says the *scheduler* stopped this
        # row, directly. The predicate used to be ``enabled = 0 AND
        # consecutive_failures > 0``, where the second term was a structural
        # inference standing in for exactly that ("``_module.*`` rows have no
        # operator-pause UI, so a failure count means the daemon did it") —
        # there is now a column that states it, so the inference goes. An
        # operator who ran ``!cron disable`` on a module row sets `enabled = 0`
        # with `auto_disabled_at` still NULL and is excluded by construction
        # rather than by proxy.
        #
        # The cooldown moves to `auto_disabled_at` for the same reason: it is
        # the timestamp the rule was always about. It caps the rescue rate for
        # a genuinely broken row, which loops through suspend and rescue at
        # roughly hourly rather than every */5 tick.
        rescued = conn.execute(
            "UPDATE scheduled_jobs "
            "SET auto_disabled_at = NULL, consecutive_failures = 0, "
            "    last_error = NULL "
            "WHERE user_id = ? AND name LIKE ? "
            "AND auto_disabled_at IS NOT NULL "
            "AND auto_disabled_at < datetime('now', '-1 hour')",
            (user_id, f"{module_prefix}%"),
        ).rowcount
        if rescued:
            logger.info(
                "Lifted the suspension on %d module job(s) for user %s",
                rescued, user_id,
            )

        # The legacy arm: a row the *pre-split* code stopped, which wrote
        # `enabled = 0` and left `auto_disabled_at` NULL because the column did
        # not exist yet. The migration deliberately backfills nothing — nothing
        # on a CRON.md row separates an operator disable from an auto-disable —
        # but that reasoning does not carry here, and without this arm every
        # `_module.*` row stopped by the running deployment is dead for good:
        # the arm above cannot see it (it keys on `auto_disabled_at`, which this
        # shape has NULL), the drift branch only rescues the `command`-shaped
        # rows, and the CRON.md sync skips `_module.*` entirely. The list used
        # to carry two more clauses and both have since gone — `!cron enable`
        # takes a `_module.` name (ISSUE-392) and a module suspension does now
        # raise a notification (ISSUE-391) — but neither reaches this shape,
        # which is stopped with `auto_disabled_at` NULL and so was never
        # suspended by anything that would have raised one.
        #
        # So this is today's predicate, scoped to that shape by
        # `auto_disabled_at IS NULL` and `disabled_at IS NULL`. The
        # `consecutive_failures > 0` term is the inference the old rescue always
        # made — a `_module.*` row has no operator-pause UI, so a failure count
        # means the daemon stopped it — and `disabled_at` is what makes it sound
        # again rather than merely plausible: `!cron disable` accepts a
        # `_module.` name and gives the user exactly that UI, so on a row that
        # had already failed the inference read the user's own off switch as the
        # daemon's and reverted it on the next tick past the cooldown,
        # indefinitely (ISSUE-392). The claim this comment used to make — that
        # the arm cannot fire twice for one row — held only for the case it was
        # written for; the user can disable again, and it fired again.
        #
        # Not scoped by `command IS NOT NULL` instead, which looks like it names
        # the pre-split shape and does not: module rows moved from command-tasks
        # to skill-tasks in May 2026 and the column split landed at the end of
        # August, so every release in between produced skill-shaped rows with
        # pre-split `enabled` semantics — exactly the set this arm exists for.
        #
        # `disabled_at` is not backfilled, so a row that predates the upgrade
        # has it NULL and is still reached. The residual is bounded and
        # deliberate: a module row a user had disabled by hand *before* the
        # upgrade is rescued once, which is what the old code did to it on every
        # tick. Delete this arm once no deployment predates the split.
        legacy = conn.execute(
            "UPDATE scheduled_jobs "
            "SET enabled = 1, consecutive_failures = 0, last_error = NULL "
            "WHERE user_id = ? AND name LIKE ? "
            "AND enabled = 0 AND auto_disabled_at IS NULL "
            "AND disabled_at IS NULL "
            "AND consecutive_failures > 0 "
            "AND (last_run_at IS NULL "
            "     OR last_run_at < datetime('now', '-1 hour'))",
            (user_id, f"{module_prefix}%"),
        ).rowcount
        if legacy:
            logger.info(
                "Re-enabled %d module job(s) for user %s stopped by pre-split "
                "code", legacy, user_id,
            )

        # Read by name, not by position. Two of these columns decide whether
        # the branch below writes `enabled = 1` over the row, and a column
        # inserted anywhere but the end of this list would silently re-point
        # them — the failure mode is not an exception but a wrong boolean
        # feeding that write. Every caller reaches this through `db.get_db`,
        # which sets `sqlite3.Row`.
        existing_rows = list(conn.execute(
            "SELECT id, name, cron_expression, command, skill, skill_args, "
            "skip_log_channel, disabled_at "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            (user_id, f"{module_prefix}%"),
        ))
        existing_by_name = {row["name"]: row for row in existing_rows}

        for name, j in wanted_by_name.items():
            row = existing_by_name.get(name)
            if row is None:
                conn.execute(
                    "INSERT INTO scheduled_jobs "
                    "(user_id, name, cron_expression, prompt, command, "
                    "skill, skill_args, enabled, skip_log_channel) "
                    "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1)",
                    (user_id, name, j["cron"], j["skill"], j["skill_args"]),
                )
                logger.info(
                    "Seeded module job '%s' for user %s", name, user_id,
                )
                if on_first_seed is not None:
                    on_first_seed(conn, user_id, j)
            else:
                legacy_command = row["command"] is not None
                drift = (
                    row["cron_expression"] != j["cron"]
                    or legacy_command  # legacy command shape — migrate
                    or row["skill"] != j["skill"]
                    or row["skill_args"] != j["skill_args"]
                    or not bool(row["skip_log_channel"])
                )
                if drift:
                    # Don't bump last_run_at here — backfilling skip_log_channel
                    # on existing rows would otherwise defer the next scheduled
                    # run by one full cron interval (up to 24h for daily jobs).
                    extra_sql = ""
                    if legacy_command and row["disabled_at"] is None:
                        # Non-admin users hit the admin gate on the old
                        # command-task path and got auto-disabled. The migration
                        # to skill-task shape removes that failure mode, so
                        # rescue the row's enabled/failure state in the same
                        # update. `enabled = 1` stays: a row stopped by the old
                        # code is off in the user's column and nothing else will
                        # turn it back on.
                        #
                        # Gated on `disabled_at`, which is the same rule the
                        # legacy arm above takes and for the same reason: this
                        # is the second path that writes `enabled = 1` over a
                        # `_module.*` row, so leaving it ungated would revert a
                        # user's `!cron disable` on the command-shaped subset
                        # and reopen ISSUE-392 through a narrower door. Gating
                        # rather than clearing the stamp, because the shape
                        # migration in the same UPDATE is wanted either way —
                        # only the enabled/failure rescue is the user's to
                        # refuse. It also keeps the invariant the column is
                        # documented under: nothing ever leaves a row
                        # `enabled = 1` with `disabled_at` still set.
                        extra_sql = (
                            ", enabled = 1, consecutive_failures = 0, "
                            "last_error = NULL, auto_disabled_at = NULL"
                        )
                    conn.execute(
                        "UPDATE scheduled_jobs "
                        "SET cron_expression = ?, command = NULL, "
                        "skill = ?, skill_args = ?, skip_log_channel = 1"
                        f"{extra_sql} "
                        "WHERE id = ?",
                        (j["cron"], j["skill"], j["skill_args"], row["id"]),
                    )
                    logger.info(
                        "Updated module job '%s' for user %s%s",
                        name, user_id,
                        # `extra_sql`, not `legacy_command`: the rescue is now
                        # gated, so a migrated row the user had switched off
                        # keeps its off switch and must not be logged as rescued.
                        " (rescued from auto-disable)" if extra_sql else "",
                    )

        for name, row in existing_by_name.items():
            if name not in wanted_by_name:
                conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (row["id"],))
                logger.info(
                    "Removed obsolete module job '%s' for user %s",
                    name, user_id,
                )

    conn.commit()


def _sync_money_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.money.*`` scheduled jobs from each user's money config."""
    try:
        from istota.money import UserNotFoundError, resolve_for_user
        from istota.money.jobs import MODULE_PREFIX, jobs_for_user
    except ImportError:
        # money extra not installed
        return

    _sync_module_jobs(
        conn, app_config,
        module_name="money",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
    )


def _sync_feeds_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.feeds.*`` scheduled jobs for users with feeds enabled."""
    try:
        from istota.feeds import UserNotFoundError, resolve_for_user
        from istota.feeds.jobs import MODULE_PREFIX, jobs_for_user
    except ImportError:
        # feeds extra not installed
        return

    run_scheduled_name = f"{MODULE_PREFIX}run_scheduled"

    def _queue_initial_poll(conn, user_id: str, j: dict) -> None:
        # First-time seed: queue an immediate one-shot poll so newly
        # provisioned users see their seeded subscriptions populate
        # without waiting up to 5 minutes for the first cron tick.
        if j["name"] != run_scheduled_name:
            return
        task_id = db.create_task(
            conn,
            prompt="",
            user_id=user_id,
            source_type="scheduled",
            priority=5,
            skip_log_channel=True,
            skill=j["skill"],
            skill_args=j["skill_args"],
            queue="background",
        )
        logger.info(
            "Queued initial feeds poll for user %s as task %d",
            user_id, task_id,
        )

    _sync_module_jobs(
        conn, app_config,
        module_name="feeds",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
        on_first_seed=_queue_initial_poll,
    )


def _sync_health_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.health.*`` scheduled jobs for users with the
    health module enabled and a Garmin connection. ``jobs_for_user``
    returns an empty list for users without stored Garmin tokens, so
    those users have their stale ``_module.health.*`` rows removed.

    On first seed (a freshly-inserted job row) we also queue a one-shot
    30-day backfill task so the user sees their history populate without
    waiting up to 6h for the first scheduled tick.
    """
    try:
        from istota.health import UserNotFoundError, resolve_for_user
        from istota.health.jobs import (
            GARMIN_SYNC_JOB,
            MODULE_PREFIX,
            jobs_for_user,
        )
    except ImportError:
        return

    backfill_name = GARMIN_SYNC_JOB.name

    def _queue_initial_backfill(conn, user_id: str, j: dict) -> None:
        if j["name"] != backfill_name:
            return
        # Resolve the freshly-inserted job row's id so the backfill task
        # is linked back to it via ``scheduled_job_id`` (M2). Without
        # this the failure-tracking path doesn't fire on the backfill,
        # so a permanently-broken Garmin connection would silently fail
        # the initial 30-day pull and never show up in the operator's
        # auto-disable counter.
        row = conn.execute(
            "SELECT id FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            (user_id, j["name"]),
        ).fetchone()
        scheduled_job_id = row[0] if row else None

        task_id = db.create_task(
            conn,
            prompt="",
            user_id=user_id,
            source_type="scheduled",
            priority=5,
            skip_log_channel=True,
            skill="health",
            skill_args=json.dumps(["garmin-sync", "--days-back", "30"]),
            queue="background",
            scheduled_job_id=scheduled_job_id,
        )
        logger.info(
            "Queued initial Garmin backfill for user %s as task %d (job_id=%s)",
            user_id, task_id, scheduled_job_id,
        )

    _sync_module_jobs(
        conn, app_config,
        module_name="health",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
        on_first_seed=_queue_initial_backfill,
    )


def _resolve_job_model_effort(job, app_config, pinned_brain, job_effort):
    """A scheduled job's ``(model, effort)``, resolved by the brain that runs it.

    Two rules, both matching an established site. The brain is the one
    ``resolve_brain_kind`` answers with, which is what the *executor* will
    compute for this task — the rule ``commands.brain_for_room`` states for the
    room path. The old line asked ``app_config.brain``, the base ``[brain]
    kind``, which ignores both the job's pin and ``[brain.source_type_overrides]``,
    so a portable ``smart`` on a routed deployment was resolved into a namespace
    the task never ran in and ``_resolve_crossing_model_effort`` dropped it as a
    crossing — at INFO, deliberately unsurfaced (ISSUE-419). The *resolved* kind
    rather than the raw pin, so a job whose brain the operator has since dropped
    from ``room_selectable`` keeps a model the brain that runs it can use.

    And ``resolve_alias`` rather than ``resolve_model_name``, which returns
    ``resolved[0]`` and discards the effort half, so ``model = "opus:high"`` ran
    at default effort with nothing said. The effort is taken off the pair before
    the model is, because ``resolve_alias`` may answer ``(None, "high")`` — a
    built-in role on a native brain with no configured model — and reading it
    only in the model branch loses the modifier in exactly the case the caller
    wrote one. Precedence is the job's own ``effort`` field over the alias's,
    which is ``with_defaults``' rule with the job row standing in for the block;
    an explicit ``haiku:high`` therefore reaches the wire, since
    ``_resolve_effort``'s protection is against an effort *inherited* from a
    default chosen for another model, not against one the operator wrote on this
    line.

    Never raises. ``make_brain`` constructs a brain the deployment may never
    otherwise build — a pinned kind's whole config block — and that is not
    confined to ``ValueError``: a malformed ``[brain.native] base_url`` reaches
    ``httpx.InvalidURL``, which derives from ``Exception`` directly. The
    enclosing ``except ValueError`` around ``create_task`` would not catch it,
    and ``get_db`` skips its commit on an exception, so one such row would roll
    back every job already queued in the tick — and ``run_scheduler``, which has
    no handler at all, would abandon the rest of its single pass. The raw values
    are the fallback because the executor resolves again as defence in depth.
    """
    raw_base, raw_effort = split_effort(job.model)
    try:
        job_brain = make_brain(
            resolve_brain_kind(
                "scheduled", app_config.brain, override=(pinned_brain or None),
            )
        )
        pair = job_brain.resolve_alias(job.model)
        if pair:
            job_effort = job_effort or (pair[1] or "")
        if pair and pair[0]:
            return pair[0], job_effort
        job_model = job_brain.resolve_model_name(job.model)
    except Exception as e:
        logger.error(
            "Scheduled job '%s' (user: %s): could not resolve model %r against "
            "its own brain (%s); passing it through unresolved",
            job.name, job.user_id, job.model, e,
        )
        return job.model, job_effort
    if job_model == raw_base:
        # Nothing in the alias table recognised the name, so the base passes
        # through — legitimate for a brain that does no aliasing, which is why
        # the wording says what will happen rather than calling it an error. A
        # `:effort` modifier on such a name is dropped, matching
        # `_resolve_crossing_model_effort`'s fallback, and saying so is the
        # whole reason this warns. Keyed on the row id: `_warned_keys` is a
        # process-global set with no eviction and `job.name` / `job.model` are
        # both model-writable through the `schedules` skill, so keying on those
        # is one durable entry per value anyone cares to write.
        _warn_once(
            f"cron_model:{job.id}",
            f"Scheduled job '{job.name}' (user: {job.user_id}): model "
            f"{job.model!r} resolved to nothing on this job's brain; using "
            f"{job_model!r} as written"
            + (f" and dropping the ':{raw_effort}' modifier" if raw_effort else ""),
        )
    return job_model, job_effort


def check_scheduled_jobs(conn, app_config: Config) -> list[int]:
    """
    Check for scheduled jobs that should run and queue them as tasks.

    Syncs CRON.md files to DB, then reads job definitions from the
    scheduled_jobs table and evaluates cron expressions in each user's timezone.

    Returns:
        List of created task IDs.
    """
    created_tasks = []

    # Sync file-based definitions to DB before evaluating
    _sync_cron_files(conn, app_config)

    jobs = db.get_enabled_scheduled_jobs(conn)
    if not jobs:
        logger.debug("No enabled scheduled jobs found")
        return created_tasks
    logger.debug("Found %d enabled scheduled job(s)", len(jobs))

    # Group by user_id to look up timezone once per user
    jobs_by_user: dict[str, list[db.ScheduledJob]] = {}
    for job in jobs:
        jobs_by_user.setdefault(job.user_id, []).append(job)

    for user_id, user_jobs in jobs_by_user.items():
        # Live DB timezone (reusing conn) so a web-UI change moves the job
        # schedule without a daemon restart (ISSUE-099); falls back to UTC.
        user_tz_str = app_config.resolve_user_timezone(user_id, conn=conn)
        try:
            user_tz = ZoneInfo(user_tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = _now(user_tz)
        # Use naive wall-clock times for croniter to avoid DST bugs.
        # croniter miscomputes next fire time when a tz-aware datetime
        # crosses a DST boundary (e.g. PST→PDT), causing double-fires.
        now_naive = now.replace(tzinfo=None)

        for job in user_jobs:
            should_run = False

            if job.last_run_at:
                last_run = datetime.fromisoformat(job.last_run_at)
                if last_run.tzinfo is None:
                    # DB stores UTC via datetime('now')
                    last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                base = last_run.astimezone(user_tz).replace(tzinfo=None)
                cron = croniter(job.cron_expression, base)
                next_run = cron.get_next(datetime)
                should_run = now_naive >= next_run
                logger.debug(
                    "Job '%s': last_run=%s next_run=%s now=%s should_run=%s",
                    job.name, last_run, next_run, now_naive, should_run,
                )
            else:
                # Use created_at as base so jobs don't fire immediately
                # when the cron time has already passed today
                if job.created_at:
                    base = datetime.fromisoformat(job.created_at)
                    if base.tzinfo is None:
                        # DB stores UTC via datetime('now')
                        base = base.replace(tzinfo=ZoneInfo("UTC"))
                    base = base.astimezone(user_tz).replace(tzinfo=None)
                else:
                    base = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
                cron = croniter(job.cron_expression, base)
                next_run = cron.get_next(datetime)
                should_run = now_naive >= next_run
                logger.debug(
                    "Job '%s' (never run): base=%s next_run=%s now=%s should_run=%s",
                    job.name, base, next_run, now_naive, should_run,
                )

            if should_run and _is_stale_fire(
                f"job {job.user_id}/{job.name}",
                next_run, now_naive,
                app_config.scheduler.cron_max_staleness_minutes,
            ):
                db.set_scheduled_job_last_run(conn, job.id)
                continue

            if should_run:
                # Overlap guard: don't stack a new run while a prior run of the
                # same job is still in flight. A `* * * * *` job behind a wedged
                # single background worker would otherwise grow the queue one
                # row/minute (the location-alert incident). last_run_at is left
                # untouched so the job fires the next tick once the in-flight run
                # clears — correct for sparse jobs too (advancing it would push
                # the next fire out by a full interval).
                inflight = db.count_inflight_tasks_for_scheduled_job(conn, job.id)
                if inflight:
                    logger.warning(
                        "Scheduled job '%s' (user: %s) skipped: %d prior run(s) "
                        "still in flight",
                        job.name, job.user_id, inflight,
                    )
                    continue
                # `TEXT` in a dynamically typed store, so coerce once and use
                # the same value for the resolution and the column:
                # `resolve_brain_kind` opens with `(override or "").strip()`,
                # which raises `AttributeError` on a non-string from a call that
                # looks total. `commands.brain_for_room` guards the identical
                # call for the identical reason.
                pinned_brain = str(job.brain).strip() if job.brain else ""
                # Aliases resolve at task-creation time so the DB stays
                # canonical, matching the talk poller's `!model` prefix path.
                job_model, job_effort = "", (job.effort or "")
                if job.model:
                    job_model, job_effort = _resolve_job_model_effort(
                        job, app_config, pinned_brain, job_effort,
                    )
                # Only an *admitted* pin reaches the column, and the reason is
                # the model beside it. This job's model was resolved against
                # the kind `resolve_brain_kind` admits, so a refused pin left
                # the row naming one namespace while `tasks.model` held a name
                # from another — which `executor._pin_origin_namespace` reads
                # as a crossing and `_request_model` then drops, at INFO, on
                # every fire. `room_selectable` ships as `[]`, so that was the
                # *default* outcome of the feature: the operator's `model`
                # silently replaced by the running brain's own default while
                # the only log line said their `brain` had been ignored.
                #
                # NULL rather than the fallthrough kind, because the two differ
                # where the fallthrough kind is itself allowlisted: writing it
                # would have `resolve_brain_kind` admit a pin nobody asked for
                # and clear `fallback`, taking availability failover off a job
                # whose pin was refused. NULL is what the row means — no pin is
                # in effect — and it puts the origin read on the unpinned
                # branch, which resolves the same lane this resolution used.
                #
                # `room_selectable_kinds` is the predicate `resolve_brain_kind`
                # itself applies (it already intersects `KNOWN_BRAIN_KINDS`), so
                # this reuses the one enforcement point rather than restating
                # it. A room keeps the opposite rule deliberately: `rooms.model`
                # was written when its pin *was* admitted, so the dropped kind's
                # namespace is the true origin there. A job re-resolves every
                # fire, so its row records the kind it just resolved against.
                admitted_brain = (
                    pinned_brain
                    if pinned_brain in room_selectable_kinds(app_config.brain)
                    else ""
                )
                # As in `check_briefings`: one unusable `job.user_id` costs its
                # own row, not every job after it in the same transaction
                # (ISSUE-402). `job.user_id` comes off the `scheduled_jobs`
                # table rather than from `config.users`, so a legacy row can
                # carry one this guard refuses.
                try:
                    task_id = db.create_task(
                        conn,
                        prompt=job.prompt,
                        user_id=job.user_id,
                        source_type="scheduled",
                        conversation_token=job.conversation_token,
                        output_target=job.output_target,
                        priority=5,
                        heartbeat_silent=job.silent_unless_action,
                        skip_log_channel=job.skip_log_channel,
                        scheduled_job_id=job.id,
                        command=job.command,
                        skill=job.skill,
                        skill_args=job.skill_args,
                        queue="background",
                        model=job_model or None,
                        effort=job_effort or None,
                        brain=admitted_brain or None,
                    )
                except ValueError as e:
                    logger.error(
                        "Scheduled job '%s' (user: %s) skipped: %s",
                        job.name, job.user_id, e,
                    )
                    continue
                db.set_scheduled_job_last_run(conn, job.id)
                created_tasks.append(task_id)
                logger.info(
                    "Scheduled job '%s' (user: %s) queued as task %d",
                    job.name, job.user_id, task_id,
                )

    return created_tasks


# ---------------------------------------------------------------------------
# The periodic gates, as one table (F33)
# ---------------------------------------------------------------------------


def _gate_always(config: Config) -> bool:
    """Default ``IntervalGate.enabled``: no condition beyond the clock."""
    return True


def _gate_epoch(config: Config) -> float:
    """Default ``IntervalGate.seed``: due on the first tick."""
    return 0.0


@dataclass(frozen=True)
class IntervalGate:
    """One periodic check in the scheduler: when it is due, and what it runs.

    The daemon loop used to state each of these three times — a clock variable
    seeded ~130 lines above the loop, an ``if now - clock >= interval`` gate
    with its own ``try/except`` inside the loop, and (for nine of them) a
    re-inlined copy of the same body in ``run_scheduler``. This table is the one
    statement; ``_tick_interval_gates`` and ``_run_interval_gates_once`` are the
    two readers.

    Fields:

    ``name``
        The gate's identity, and for a background gate the label
        ``_spawn_background_check`` logs and keys its in-flight registry on.
        Operators grep these, so a background gate's name is not free to change.
    ``run``
        The body, taking the tick's ``now`` (most ignore it; the two that read
        it feed a cooldown clock). Unguarded — error policy is ``on_error``.
    ``field``
        The ``config.scheduler`` attribute the interval is read from, or None
        where the interval is not a config field at all. Authoritative: the
        interval is *derived* from it, so the name a test reads and the value
        the loop uses cannot drift.
    ``fixed_interval``
        The interval for a gate with no config field.
    ``enabled``
        Everything in the gate condition that is not the clock — including a
        ``bool(interval)`` term where the current code has one, since an
        interval of 0 otherwise reads as "due every tick" rather than "off".
    ``seed``
        The clock's initial value. Three shapes are in use and each has a reason
        recorded at its row.
    ``background`` / ``overlap_expected``
        Handed to ``_spawn_background_check``. The clock advances at *spawn*
        time; the in-flight guard, not the clock, is what prevents overlap.
    ``on_error`` / ``one_shot_on_error``
        The ``logger.error`` format string for the daemon loop and for
        ``run_scheduler`` respectively. ``None`` means the gate propagates on
        that path, which is what the code being replaced does — the background
        spawns are bare in the loop, and two of the one-shot bodies are bare in
        ``run_scheduler``.
    ``one_shot``
        Whether ``run_scheduler`` runs this gate. That path has no clocks: it
        runs every one-shot gate whose ``enabled`` holds, once.
    """

    name: str
    run: Callable[[float], None]
    field: str | None = None
    fixed_interval: float | None = None
    enabled: Callable[[Config], bool] = _gate_always
    seed: Callable[[Config], float] = _gate_epoch
    background: bool = False
    overlap_expected: bool = False
    on_error: str | None = None
    one_shot: bool = False
    one_shot_on_error: str | None = None

    def __post_init__(self) -> None:
        """A row must state exactly one interval, and state it explicitly.

        Both fields absent would otherwise resolve to 0.0, which is
        ``backup-stale-alert``'s *deliberate* every-tick shape — so a row that
        simply forgot to say would land on the loop thread every 0.5s and read
        as a decision. And both fields present states two intervals of which
        only one is ever read. Refused at construction rather than left to a
        test, because the point of the table is that a row is the one
        statement.
        """
        if (self.field is None) == (self.fixed_interval is None):
            raise ValueError(
                f"interval gate {self.name!r} must state exactly one of "
                "`field` (a config.scheduler attribute) or `fixed_interval`"
            )

    def interval(self, config: Config) -> float:
        """Seconds between runs, read from ``field`` where there is one."""
        if self.field is not None:
            return getattr(config.scheduler, self.field)
        return self.fixed_interval


def _db_backup_last_time(config: Config) -> float:
    """The persisted backup clock, for the ``db-backup`` gate's seed."""
    from . import db_backup as _db_backup

    return _db_backup.last_backup_time(config)


def build_interval_gates(
    config: Config,
    *,
    pool: "WorkerPool | None" = None,
    background_checks: "dict[str, threading.Thread] | None" = None,
    doctor_state: dict | None = None,
    pressure_state: dict | None = None,
    backup_state: dict | None = None,
) -> list[IntervalGate]:
    """The scheduler's periodic checks, in the order the daemon loop runs them.

    **The order is load-bearing and this list is where it is stated.** Shared
    files are organized before TASKS.md is polled so the files are in place when
    the poller walks them; the backup staleness alert reads the persisted clock
    immediately after the snapshot gate that would have advanced it; the
    heartbeat sweep is last. ``run_scheduler`` iterates the same list and so
    inherits the same relative order for the nine gates it runs.

    ``pool``, ``doctor_state``, ``pressure_state`` and ``backup_state`` are the
    loop-local state the daemon owns. ``run_scheduler`` passes none of them: no
    gate it runs reads any, and building the closures costs nothing since they
    are never called.
    """
    keeper: dict[str, threading.Thread] = (
        background_checks if background_checks is not None else {}
    )
    doctor_seen: dict = doctor_state if doctor_state is not None else {}
    pressure: dict = (
        pressure_state
        if pressure_state is not None
        else {"last_alert": 0.0, "clocks": {}}
    )
    backup: dict = backup_state if backup_state is not None else {"alerted": False}

    def _briefings(now: float) -> None:
        # Manages its own DB connections so no lock is held during the slow
        # network pre-fetching.
        briefing_tasks = check_briefings(config.db_path, config)
        if briefing_tasks:
            logger.info("Queued %d briefing(s)", len(briefing_tasks))

    def _shared_blocks(now: float) -> None:
        # Generation runs off the dispatch thread on worker threads, so the slow
        # gather can't stall dispatch.
        shared_names = check_shared_blocks(config)
        if shared_names:
            logger.info(
                "Generating %d shared block(s): %s",
                len(shared_names), ", ".join(shared_names),
            )

    def _briefing_triggers(now: float) -> None:
        triggered = check_briefing_triggers(config.db_path, config)
        if triggered:
            logger.info("Processed %d briefing trigger(s)", len(triggered))

    def _scheduled_jobs(now: float) -> None:
        with db.get_db(config.db_path) as conn:
            scheduled_tasks = check_scheduled_jobs(conn, config)
            if scheduled_tasks:
                logger.info("Queued %d scheduled job(s)", len(scheduled_tasks))

    def _sleep_cycles(now: float) -> None:
        _run_sleep_cycles(config)

    def _travel_timezone(now: float) -> None:
        check_travel_timezone(config)

    def _email_poll(now: float) -> None:
        _run_email_poll(config)

    def _shared_files(now: float) -> None:
        from .shared_file_organizer import discover_and_organize_shared_files
        organized = discover_and_organize_shared_files(config)
        if organized:
            logger.info("Organized %d shared file(s)", len(organized))

    def _tasks_files(now: float) -> None:
        from .tasks_file_poller import poll_all_tasks_files
        tasks_file_tasks = poll_all_tasks_files(config)
        if tasks_file_tasks:
            logger.info("Queued %d TASKS.md task(s)", len(tasks_file_tasks))

    def _cleanup(now: float) -> None:
        run_cleanup_checks(config)

    def _status_write(now: float) -> None:
        from .status_writer import write_status

        with db.get_db(config.db_path) as conn:
            fg_pending = sum(
                db.count_pending_tasks_for_user_queue(conn, uid, "foreground")
                for uid in db.get_users_with_pending_fg_queue_tasks(conn)
            )
            bg_pending = sum(
                db.count_pending_tasks_for_user_queue(conn, uid, "background")
                for uid in db.get_users_with_pending_bg_queue_tasks(conn)
            )
        write_status(config, pool.active_count, fg_pending, bg_pending)

    def _db_health(now: float) -> None:
        check_db_health(config)

    def _doctor(now: float) -> None:
        check_doctor(config, doctor_seen)

    def _worktree_reap(now: float) -> None:
        check_worktree_reap(config)

    def _cache_sweep(now: float) -> None:
        check_sandbox_cache_sweep(config)

    def _avatar_import(now: float) -> None:
        check_avatar_import(config)

    def _overlay_reindex(now: float) -> None:
        check_skill_overlay_reindex(config)

    def _db_backup_run(now: float) -> None:
        _run_db_backup(config)

    def _backup_stale(now: float) -> None:
        # Reads the *persisted* clock, not the loop's — the loop clock advances
        # at spawn time, this one only on a durable OK run, which is what makes
        # "backups have silently stopped" detectable at all.
        from . import db_backup as _db_backup

        persisted = _db_backup.last_backup_time(config)
        backup["alerted"] = _maybe_alert_backup_stale(
            config, now, persisted, backup["alerted"]
        )

    def _scheduler_stats(now: float) -> None:
        _emit_scheduler_stats(config, pool)

    def _pressure_breadcrumb(now: float) -> None:
        _emit_host_pressure_breadcrumb()

    def _pressure_sample(now: float) -> None:
        pressure["last_alert"] = _check_host_pressure(
            config,
            pool,
            last_alert=pressure["last_alert"],
            alert_clocks=pressure["clocks"],
            background_checks=keeper,
            now=now,
        )

    def _heartbeats(now: float) -> None:
        _run_heartbeat_checks(config)

    return [
        IntervalGate(
            name="briefings",
            run=_briefings,
            field="briefing_check_interval",
            on_error="Error checking briefings: %s",
            one_shot=True,
            # Bare in `run_scheduler` today: a briefing sweep that raises there
            # aborts the one-shot run rather than being logged past.
            one_shot_on_error=None,
        ),
        # ------------------------------------------------------------------
        # KNOWN MISMATCH, preserved deliberately. These four read
        # `briefing_check_interval` and none of them is a briefing: shared
        # blocks, scheduled jobs, the sleep-cycle poll and the cleanup sweep.
        # Giving each its own key is an operator-visible config change across
        # `config.py`, `config.example.toml`, the Ansible template and the
        # Docker render, so it is its own piece of work. The point of the table
        # is that the mismatch is four visible rows instead of four `if`
        # statements 300 lines apart. Do not "fix" it here.
        # ------------------------------------------------------------------
        IntervalGate(
            name="shared-blocks",
            run=_shared_blocks,
            field="briefing_check_interval",  # mismatch: not a briefing
            on_error="Error checking shared blocks: %s",
            one_shot=True,
            one_shot_on_error="Error checking shared blocks: %s",
        ),
        IntervalGate(
            name="briefing-triggers",
            run=_briefing_triggers,
            field="tasks_file_poll_interval",
            on_error="Error checking briefing triggers: %s",
        ),
        IntervalGate(
            name="scheduled-jobs",
            run=_scheduled_jobs,
            field="briefing_check_interval",  # mismatch: not a briefing
            on_error="Error checking scheduled jobs: %s",
            one_shot=True,
            # Bare in `run_scheduler` today, like the briefing sweep above.
            one_shot_on_error=None,
        ),
        # Off the dispatch thread (ISSUE-144 Tier 2): extraction makes
        # synchronous per-user LLM calls and can take minutes. This interval is
        # only the *poll* cadence — each check's own cron decides whether to do
        # any work — so a pass outliving it is normal and the in-flight guard,
        # not the clock, is what stops it re-firing against unstamped state.
        IntervalGate(
            name="sleep-cycles",
            run=_sleep_cycles,
            field="briefing_check_interval",  # mismatch: not a briefing
            background=True,
            overlap_expected=True,
            one_shot=True,
            # `_run_sleep_cycles` guards each half itself.
            one_shot_on_error=None,
        ),
        # ISSUE-096. Its own interval rather than the briefing one: the signal
        # it watches moves over hours, so a minute-by-minute sweep of every
        # user's location DB would be pure churn. Off the dispatch thread — it
        # opens a per-user location.db and may send a notification.
        IntervalGate(
            name="travel-timezone",
            run=_travel_timezone,
            fixed_interval=TRAVEL_TZ_CHECK_INTERVAL,
            enabled=lambda c: c.location.enabled,
            background=True,
            overlap_expected=True,
            one_shot=True,
            one_shot_on_error="Error checking travel timezones: %s",
        ),
        # ISSUE-250. One IMAP connection per message read and another per
        # message with attachments, each attachment uploaded to Nextcloud over
        # WebDAV — unbounded network I/O whose duration an outside sender can
        # influence. `overlap_expected` because a batch draining a backlog
        # legitimately outlives the poll interval.
        IntervalGate(
            name="email-poll",
            run=_email_poll,
            field="email_poll_interval",
            enabled=lambda c: c.email.enabled,
            background=True,
            overlap_expected=True,
            one_shot=True,
            # `_run_email_poll` guards itself.
            one_shot_on_error=None,
        ),
        # Before the TASKS.md poll below, so the files are in place when it
        # walks them.
        IntervalGate(
            name="shared-files",
            run=_shared_files,
            field="shared_file_check_interval",
            on_error="Error organizing shared files: %s",
            one_shot=True,
            one_shot_on_error="Error organizing shared files: %s",
        ),
        IntervalGate(
            name="tasks-file-poll",
            run=_tasks_files,
            field="tasks_file_poll_interval",
            on_error="Error polling TASKS.md files: %s",
            one_shot=True,
            one_shot_on_error="Error polling TASKS.md files: %s",
        ),
        IntervalGate(
            name="cleanup",
            run=_cleanup,
            field="briefing_check_interval",  # mismatch: not a briefing
            on_error="Error running cleanup checks: %s",
        ),
        # The status file's 60s is a literal, not a config field.
        IntervalGate(
            name="status-write",
            run=_status_write,
            fixed_interval=60,
            on_error="Error writing status: %s",
        ),
        # `PRAGMA quick_check` + self-healing REINDEX over the framework DB and
        # every per-user module DB. Seeded to 0 so a fresh daemon sweeps on its
        # first tick rather than waiting out a 24h interval after a deploy.
        IntervalGate(
            name="db-health",
            run=_db_health,
            field="db_health_check_interval",
            background=True,
        ),
        # The drift this catches happens *after* boot — the auto-update cron
        # changes what is installed under a config the daemon already loaded —
        # so a boot-only check is blind to it. Seeded to now, not 0: the boot
        # run already swept, and a 0 here would re-run the whole registry
        # seconds later.
        IntervalGate(
            name="doctor",
            run=_doctor,
            field="doctor_check_interval",
            enabled=lambda c: bool(c.scheduler.doctor_check_interval),
            seed=lambda c: time.time(),
            background=True,
        ),
        # ISSUE-288. Seeded to 0 for the reason the sweeps below share: the
        # accumulation it clears is the state a restart usually arrives into,
        # and there is no boot run to double up with.
        IntervalGate(
            name="worktree-reap",
            run=_worktree_reap,
            field="worktree_reap_interval",
            enabled=lambda c: bool(
                c.developer.enabled
                and c.developer.repos_dir
                and c.developer.worktree_reap_enabled
                and c.scheduler.worktree_reap_interval
            ),
            background=True,
        ),
        # ISSUE-317. Moving the caches off bwrap's root tmpfs is what makes them
        # persist, and nothing pruned them, so the fix for a RAM leak was a disk
        # leak on the volume the reap above is already fighting for.
        IntervalGate(
            name="sandbox-cache-sweep",
            run=_cache_sweep,
            field="sandbox_cache_sweep_interval",
            enabled=lambda c: bool(
                c.security.sandbox_cache_sweep_enabled
                and sandbox_cache_sweep_root(c) is not None
                and c.scheduler.sandbox_cache_sweep_interval
            ),
            background=True,
        ),
        # On a cadence rather than at login or on render: a fetch at login puts
        # a 10-second Nextcloud timeout in front of authentication, and a fetch
        # on render is the live-proxy coupling the Nextcloud decoupling is
        # unwinding. Gated on `web.enabled` too — an avatar renders in the web
        # UI and nowhere else. Seeded to 0: a user who first signed in while the
        # daemon was down has no imported picture and nothing else will fetch
        # one, so a restart should ask rather than wait out six hours.
        IntervalGate(
            name="avatar-import",
            run=_avatar_import,
            field="avatar_import_interval",
            enabled=lambda c: bool(
                c.web.enabled
                and c.storage_is_nextcloud
                and c.web.avatar_import_from_nextcloud
                and c.scheduler.avatar_import_interval
            ),
            background=True,
        ),
        # ISSUE-343. An overlay is a user-written file with no CLI write path,
        # so a periodic full directory pass is the only seam that sees an edit.
        # Seeded to 0: a restart is the one moment an overlay edited while the
        # daemon was down is guaranteed to be unindexed.
        IntervalGate(
            name="skill-overlay-reindex",
            run=_overlay_reindex,
            field="skill_overlay_reindex_interval",
            enabled=lambda c: bool(
                c.memory_search.enabled
                and c.memory_search.auto_index_memory_files
                and c.use_mount
                and c.scheduler.skill_overlay_reindex_interval
            ),
            background=True,
        ),
        # Off-host durability for the local DBs. Off the loop thread because
        # this one writes to the rclone FUSE mount, where a degraded mount makes
        # the write time unbounded. Seeded from the *persisted* last-run stamp
        # so the cadence survives a restart: an overdue backup fires promptly, a
        # recent one waits out the remainder. Without it the clock reset every
        # boot and a host deploying more than once a day never backed up.
        IntervalGate(
            name="db-backup",
            run=_db_backup_run,
            field="db_backup_interval",
            enabled=lambda c: bool(
                c.scheduler.db_backup_enabled and c.scheduler.db_backup_interval
            ),
            seed=_db_backup_last_time,
            background=True,
        ),
        # Not an interval gate: `fixed_interval=0` runs it on every tick, which
        # is what the code it replaces did. It is in the table rather than
        # beside it so the ordering — immediately after the snapshot gate whose
        # clock it is *not* reading — stays stated in one place.
        IntervalGate(
            name="backup-stale-alert",
            run=_backup_stale,
            fixed_interval=0,
            enabled=lambda c: bool(
                c.scheduler.db_backup_enabled and c.scheduler.db_backup_interval
            ),
        ),
        # Seeded to now, not 0, so the first stats line fires after one full
        # interval — no noisy emit during startup while state is hydrating.
        IntervalGate(
            name="scheduler-stats",
            run=_scheduler_stats,
            field="scheduler_stats_interval",
            enabled=lambda c: bool(c.scheduler.scheduler_stats_interval),
            seed=lambda c: time.time(),
        ),
        # Seeded to 0, unlike the stats line above: a daemon restart is
        # precisely when a memory datum is worth having, since it establishes
        # the post-restart baseline the series is read against.
        IntervalGate(
            name="host-pressure-breadcrumb",
            run=_pressure_breadcrumb,
            field="host_pressure_breadcrumb_interval_seconds",
            enabled=lambda c: bool(
                c.scheduler.host_pressure_enabled
                and c.scheduler.host_pressure_breadcrumb_interval_seconds
            ),
        ),
        # Its own, faster cadence and a different purpose from the breadcrumb
        # above: this one feeds a decision (the admission gate and the threshold
        # snapshot), that one feeds a series. Seeded to 0 so the gate has a real
        # reading on the first tick rather than failing open for an interval
        # after every restart. Never raises — see `_check_host_pressure`.
        IntervalGate(
            name="host-pressure-sample",
            run=_pressure_sample,
            field="host_pressure_sample_interval_seconds",
            enabled=lambda c: bool(
                c.scheduler.host_pressure_enabled
                and c.scheduler.host_pressure_sample_interval_seconds
            ),
        ),
        # Backgrounded for the reason `_spawn_background_check` documents: it
        # blocked `pool.dispatch()` for its whole duration, which was fair while
        # the six check types were cheap and stopped being fair when
        # `self-check` grew into the whole doctor registry. Taking it off the
        # loop was only half the fix — the sweep also held one write transaction
        # for its whole length, so `check_heartbeats` commits per check.
        IntervalGate(
            name="heartbeats",
            run=_heartbeats,
            field="heartbeat_check_interval",
            background=True,
            overlap_expected=True,
            one_shot=True,
            # `_run_heartbeat_checks` guards itself.
            one_shot_on_error=None,
        ),
    ]


def seed_interval_clocks(
    gates: list[IntervalGate], config: Config
) -> dict[str, float]:
    """Each gate's initial clock, keyed by name."""
    return {gate.name: gate.seed(config) for gate in gates}


def _tick_interval_gates(
    gates: list[IntervalGate],
    clocks: dict[str, float],
    config: Config,
    now: float,
    inflight: dict[str, threading.Thread],
) -> None:
    """Run every gate that is due, in table order, and advance its clock.

    The clock advances whether or not the body succeeded, and for a background
    gate it advances at *spawn* time — both are what the inline gates did. A
    gate with no ``on_error`` propagates, which is what the bare
    ``_spawn_background_check`` call sites did.
    """
    for gate in gates:
        if not gate.enabled(config):
            continue
        # A non-positive interval means *every tick*, bypassing the clock
        # rather than comparing against it. That is `backup-stale-alert`, which
        # had no clock at all before this table and must not acquire one by
        # accident: `now` is wall-clock, so a backwards NTP step leaves the
        # stored clock ahead of it and `now - clock >= 0` would skip the check
        # for the length of the step.
        interval = gate.interval(config)
        if interval > 0 and now - clocks[gate.name] < interval:
            continue
        try:
            if gate.background:
                _spawn_background_check(
                    gate.name,
                    lambda g=gate, t=now: g.run(t),
                    inflight,
                    overlap_expected=gate.overlap_expected,
                )
            else:
                gate.run(now)
        except Exception as e:
            if gate.on_error is None:
                raise
            logger.error(gate.on_error, e)
        clocks[gate.name] = now


def _run_interval_gates_once(gates: list[IntervalGate], config: Config) -> None:
    """Run every one-shot gate once, synchronously, in table order.

    No clocks and no interval test: ``run_scheduler`` is a single pass, so a
    gate it runs at all it runs unconditionally. Nothing is backgrounded —
    one-shot mode has no dispatch loop to starve, and running the same bodies
    the daemon runs is what keeps the two paths from drifting.
    """
    now = time.time()
    for gate in gates:
        if not gate.one_shot:
            continue
        if not gate.enabled(config):
            continue
        try:
            gate.run(now)
        except Exception as e:
            if gate.one_shot_on_error is None:
                raise
            logger.error(gate.one_shot_on_error, e)


def run_scheduler(config: Config, max_tasks: int | None = None, dry_run: bool = False) -> int:
    """
    Run the scheduler once (for cron-style invocation).
    Returns number of tasks processed.
    """
    processed = 0

    # Hydrate user configs from Nextcloud API
    try:
        hydrate_user_configs(config)
    except Exception as e:
        logger.warning("User config hydration failed: %s", e)

    # Poll Talk conversations.
    #
    # Gated on signaling the same way `run_daemon` is, and for a sharper reason
    # than tidiness: this is a *second process*, so the supervisor's in-flight
    # set and `inbound.py`'s module globals do not reach it. A one-shot run
    # against a signaling-enabled deployment would read the same cursor the
    # daemon's drain just read, fetch the same messages and run the filter
    # chain over them again — and `dispatch_command`, `handle_confirmation_reply`
    # with its ack post, the `!model` usage reply and the channel-gate notice
    # are none of them idempotent. On such a deployment the daemon owns Talk
    # inbound; a one-shot has no supervisor and is not a substitute for one.
    if config.talk.enabled and config.talk.signaling.enabled:
        logger.info(
            "Skipping the Talk poll: [talk.signaling] enabled = true, so "
            "inbound belongs to the signaling supervisor in the daemon",
        )
    elif config.talk.enabled:
        try:
            from .transport.talk import poll_talk_conversations
            # Single-pass mode still lazily uses the persistent loop here:
            # poll_talk_conversations pulls the shared get_talk_client singleton
            # (whose httpx pool is bound to the persistent loop), and the shared
            # process_one_task delivery path already submits via run_coro, so all
            # Talk I/O must stay on one loop. The persistent runtime is a single
            # daemon thread that exits with the one-shot process.
            talk_tasks = run_coro(poll_talk_conversations(config))
            if talk_tasks:
                logger.info("Queued %d Talk task(s)", len(talk_tasks))
        except Exception as e:
            logger.error("Error polling Talk: %s", e)

    # Every periodic check the daemon loop runs that a single pass also wants,
    # through the same table so the two paths cannot drift. Nine rows are marked
    # `one_shot`; they run synchronously here, in the loop's own order and each
    # with the error policy its row states. The rest belong to a long-lived
    # process — the sweeps, the backups, the status file, the health lines — and
    # a one-shot run neither runs nor clocks them.
    _run_interval_gates_once(build_interval_gates(config), config)

    # Process tasks
    while True:
        result = process_one_task(config, dry_run=dry_run)
        if result is None:
            break

        task_id, success = result
        processed += 1

        if max_tasks and processed >= max_tasks:
            break

    # Single-pass mode lazily started the persistent runtime via run_coro
    # (poller / delivery). Stop it so the shared httpx client's aclose hook
    # runs a clean TLS shutdown instead of the connections being dropped on
    # process exit. No-op if the runtime was never started (Talk disabled).
    reset_async_runtime()

    return processed


def _start_talk_signaling(config: Config) -> bool:
    """Refuse, or start the signaling supervisor. True when it is driving inbound.

    **The two startup refusals are called from here**, which is the only place
    they can be: `signaling.py` has held `require_websockets` and `require_hpb`
    since the protocol landed, and until this function existed a deployment
    with `enabled = true` and no library booted normally with only `doctor`
    saying otherwise.

    Neither degrades to the poller. The poll path is a *capability floor* for a
    deployment with no high-performance backend, not a redundant branch, so a
    daemon that quietly took it would report every signaling counter healthy
    for want of any watchers to be unhealthy — while the operator who asked for
    push believes messages arrive within a second and they arrive within a poll
    cycle.

    **A settings call that could not be made is not one of those refusals, and
    the distinction is deliberate.** The two refusals are misconfigurations:
    a library that is not installed, and a Talk that reports `internal` mode
    because no server is registered with it. Both are settled facts that a
    retry cannot change. A Nextcloud that did not answer is neither — it is a
    fact about this second — and refusing there would make a transient blip on
    one service take down a daemon that also runs cron jobs, briefings, email
    and the web scheduler, which is a single point of failure the poll path
    never had. So a failed settings call starts the supervisor anyway: its own
    `settings()` raises a watcher fault and backs off, the reconciliation pass
    carries inbound meanwhile at `room_sync_interval`, and
    `doctor`'s `talk.signaling_reachable` and `talk.signaling_auth` are what
    report it. A Talk that *answers* and names no backend still refuses.
    """
    from .transport.talk import signaling as sig

    # Refusal 1: no WebSocket client, so no connection can be opened at all.
    sig.require_websockets()

    # Refusal 2 needs an answer from Talk, and only an answer can refuse.
    from .async_runtime import get_talk_client, run_coro, spawn_task

    try:
        payload = run_coro(
            get_talk_client(config).get_signaling_settings(), timeout=30.0,
        )
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.error(
            "STARTUP Could not read Talk's signaling settings (%s: %s). "
            "Starting the supervisor anyway: an unreachable Nextcloud is not "
            "the misconfiguration the startup refusal is for, and the watchers "
            "retry on their own backoff. Check doctor's talk.signaling_* "
            "checks if inbound stays on the reconciliation interval.",
            type(e).__name__, e,
        )
    else:
        settings = sig.parse_settings(
            payload, nextcloud_url=config.nextcloud.url,
        )
        if not settings.signaling_mode:
            # `require_hpb` refuses on *three* states, and only two of them are
            # the settled misconfigurations this gate is for. The third —
            # "Talk reported no signaling mode" — is what `parse_settings`
            # yields for any payload it could not read: a 200 carrying a proxy
            # error page, an OCS envelope shaped differently, an upstream field
            # rename. Refusing there is the same single point of failure the
            # docstring above rejects, wearing a different hat, and
            # `require_hpb`'s own remedy for it says "check that Nextcloud is
            # reachable" — a transient-fault remedy attached to a hard refusal.
            logger.error(
                "STARTUP Talk answered the signaling settings call with "
                "nothing this client could read, so whether a "
                "high-performance backend is registered is unknown. Starting "
                "the supervisor anyway; see doctor's talk.signaling_auth and "
                "talk.signaling_reachable.",
            )
        else:
            sig.require_hpb(settings)

    from .transport.talk.supervisor import SignalingSupervisor

    supervisor = SignalingSupervisor(config)
    # `spawn`, not `submit`: this runs for the life of the daemon and must not
    # block the boot sequence. The handle is held by the runtime's own
    # registry, which cancels it by name during `stop()`; the supervisor's
    # `finally` then cancels and awaits its watchers.
    spawn_task(supervisor.run(), name="talk-signaling-supervisor")
    return True


def _talk_poll_loop(config: Config) -> None:
    """Background thread: continuously polls Talk conversations."""
    from .transport.talk import poll_talk_conversations

    while not _shutdown_requested:
        try:
            # Runs on the shared persistent loop. The long-poll awaits yield the
            # loop, so delivery coroutines (acks, results) submitted via run_coro
            # interleave normally; the poll's own FIRST_COMPLETED + cancel only
            # touches the tasks it created. submit timeout=None matches the old
            # asyncio.run-forever semantics — httpx-level timeouts bound each poll.
            talk_tasks = run_coro(poll_talk_conversations(config))
            if talk_tasks:
                logger.info("Queued %d Talk task(s)", len(talk_tasks))
        except Exception as e:
            logger.error("Talk poll error: %s", e)
        time.sleep(config.scheduler.talk_poll_interval)


def _dispatch_sleep(
    pool: "WorkerPool", config: Config, should_stop: Callable[[], bool]
) -> None:
    """Sleep out one base poll tick, re-dispatching workers in sub-tick slices.

    Every periodic check in the daemon loop is self-throttled by its own
    interval timer, so the only work that needs to happen on every base tick is
    ``pool.dispatch()`` (pending-task discovery). Sleeping the whole
    ``poll_interval`` in one shot means a freshly-enqueued task waits up to a
    full ``poll_interval`` before a worker claims it. Instead we sleep in
    ``dispatch_interval`` slices and dispatch after each, so cold pickup latency
    is bounded by ``dispatch_interval`` — without re-running any of the heavy
    interval-gated checks (they stay on ``poll_interval`` granularity because the
    slice loop consumes one full base tick before the outer loop iterates).

    ``dispatch_interval`` <= 0 or >= ``poll_interval`` restores the legacy
    single-sleep-per-tick behaviour. ``should_stop`` is polled before and after
    each slice so shutdown is honoured within one ``dispatch_interval``.
    """
    base = config.scheduler.poll_interval
    slice_s = config.scheduler.dispatch_interval
    if slice_s <= 0 or slice_s >= base:
        time.sleep(base)
        return
    deadline = time.monotonic() + base
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(slice_s, remaining))
        if should_stop():
            return
        try:
            pool.dispatch()
        except Exception as e:  # noqa: BLE001
            logger.error("Error dispatching workers: %s", e)


def _report_task_cgroups(config: Config) -> None:
    """Sweep cgroups a previous run left behind, and say at startup what A6 will do.

    The sweep is here rather than in the executor because it is a statement
    about the *previous* process: a daemon killed by the OOM killer or a reboot
    — exactly the events this spec exists to survive — leaves its ``task-*``
    directories on disk, and they accumulate across restarts until the next
    incident, when ``systemd-cgls`` is what an operator reaches for first.

    The startup line is the other half. ``task_cgroup`` fails open by design, so
    a deployment without ``Delegate=`` runs every task uncontained; without a
    line saying so at startup, the *only* difference between "containment on"
    and "containment silently absent" is a warning that fires once, whenever the
    first task happens to run.

    For that line to be worth anything it has to be a *measurement*, which is
    what ``probe`` makes it. Resolving the root is not enough on its own — it
    succeeds on any systemd host — and a report that cannot fail is a report
    that says nothing. The negative case is logged at warning rather than info,
    since "your containment is not running" is not an informational fact.
    """
    if not config.scheduler.task_cgroup_enabled:
        logger.info("STARTUP Per-task cgroups: disabled")
        return
    try:
        root = task_cgroup.resolve_root()
        if root is None:
            logger.warning(
                "STARTUP Per-task cgroups: enabled but INERT "
                "(no delegated unit cgroup — tasks run uncontained)"
            )
            return
        # Resolving the root is not evidence, which is the trap the first cut of
        # this function fell into: `resolve_root` answers on every systemd host,
        # `Delegate=` applied or not, so the affirmative line printed on hosts
        # where every `create` would go on to fail. Probe instead.
        reason = task_cgroup.probe(root)
        if reason is not None:
            logger.warning(
                "STARTUP Per-task cgroups: enabled but INERT (%s); tasks run "
                "uncontained. Check Delegate= and DelegateSubgroup= on the unit",
                reason,
            )
            return
        removed, surviving = task_cgroup.sweep_stale(root)
        logger.info(
            "STARTUP Per-task cgroups: %s (memory.max=%s, pids.max=%d, cpu.max=%s)%s%s",
            root,
            "max" if config.scheduler.task_memory_max_mb <= 0
            else f"{config.scheduler.task_memory_max_mb}M",
            config.scheduler.task_pids_max,
            "unset" if config.scheduler.task_cpu_max_percent <= 0
            else f"{config.scheduler.task_cpu_max_percent}%",
            f"; swept {removed} stale" if removed else "",
            f"; {surviving} still holding live processes" if surviving else "",
        )
    except Exception as e:  # noqa: BLE001
        # A startup report must never be what stops the daemon from starting.
        logger.warning("STARTUP Per-task cgroups: check failed: %s", e)


def run_daemon(
    config: Config,
    *,
    install_signal_handlers: bool = True,
    ready_event: "threading.Event | None" = None,
) -> None:
    """
    Run the scheduler as a daemon (continuous loop).
    Handles graceful shutdown via SIGTERM/SIGINT.

    ``install_signal_handlers`` (default True) preserves the standalone-daemon
    behaviour. The combined ``istota serve`` launcher runs this on a worker
    thread with ``install_signal_handlers=False`` and owns SIGINT/SIGTERM
    itself (signal handlers can only be installed from the main thread), driving
    shutdown via ``request_shutdown()``.

    ``ready_event`` (when supplied) is set once the worker pool and pollers are
    initialized and the loop is about to start — the launcher waits on it before
    starting uvicorn so the two subsystems come up in order. On an early return
    (lock contention) the event is still set so a waiter isn't left blocked.
    """
    global _shutdown_requested
    # Clear any stale flag from a prior in-process run (serve restart / tests).
    _shutdown_requested = False

    # Acquire exclusive lock to prevent multiple daemon instances
    lock_path = DAEMON_LOCK_PATH
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another scheduler daemon is already running. Exiting.")
        lock_file.close()
        if ready_event is not None:
            ready_event.set()
        raise _DaemonAlreadyRunning(
            "Another istota scheduler is already running (lock held at "
            f"{lock_path})."
        )

    # The availability breaker is process-local and starts clear. Remove its
    # observational cross-process state too, or the web unit can keep showing
    # a cooldown that this fresh scheduler will no longer honor. This must be
    # after the daemon lock: a rejected second instance cannot clear the live
    # scheduler's state.
    from .brain_availability import clear_all as clear_brain_availability
    clear_brain_availability(config)

    # Write PID to lock file for debugging
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    # Set up signal handlers (main-thread only; the serve launcher opts out).
    if install_signal_handlers:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

    logger.info("STARTUP Scheduler daemon starting (pid: %d)", os.getpid())
    # The revision this process imported, not the one the checkout holds now.
    # Every deploy path moves the checkout before it restarts anything, so
    # these disagree for as long as the restart lags — see `build_info`.
    logger.info("STARTUP Running %s", build_description())
    logger.info("STARTUP Task poll interval: %ds", config.scheduler.poll_interval)
    logger.info("STARTUP Max fg/bg workers: %d/%d", config.scheduler.max_foreground_workers, config.scheduler.max_background_workers)
    logger.info("STARTUP Worker idle timeout: %ds", config.scheduler.worker_idle_timeout)
    logger.info("STARTUP Talk poll interval: %ds", config.scheduler.talk_poll_interval)
    logger.info("STARTUP Talk poll timeout: %ds", config.scheduler.talk_poll_timeout)
    logger.info("STARTUP Email poll interval: %ds", config.scheduler.email_poll_interval)
    logger.info("STARTUP Briefing check interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP TASKS.md poll interval: %ds", config.scheduler.tasks_file_poll_interval)
    logger.info("STARTUP Shared file check interval: %ds", config.scheduler.shared_file_check_interval)
    logger.info("STARTUP Heartbeat check interval: %ds", config.scheduler.heartbeat_check_interval)
    logger.info("STARTUP DB health check interval: %ds", config.scheduler.db_health_check_interval)
    logger.info(
        "STARTUP Doctor check interval: %s",
        f"{config.scheduler.doctor_check_interval}s"
        if config.scheduler.doctor_check_interval
        else "disabled",
    )
    logger.info(
        "STARTUP Host pressure breadcrumb: %s",
        f"{config.scheduler.host_pressure_breadcrumb_interval_seconds}s"
        if config.scheduler.host_pressure_enabled
        and config.scheduler.host_pressure_breadcrumb_interval_seconds
        else "disabled",
    )
    logger.info(
        "STARTUP Memory admission gate: %s",
        f"below {config.scheduler.min_available_memory_mb} MB available "
        f"or PSI some avg10 > {config.scheduler.host_pressure_psi_threshold:g} "
        f"(sampled every {config.scheduler.host_pressure_sample_interval_seconds}s)"
        if config.scheduler.host_pressure_enabled
        and config.scheduler.host_pressure_sample_interval_seconds
        else "disabled",
    )
    _report_task_cgroups(config)
    logger.info("STARTUP Scheduled job check interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Cleanup interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Confirmation timeout: %d min", config.scheduler.confirmation_timeout_minutes)
    logger.info("STARTUP Task retention: %d days", config.scheduler.task_retention_days)
    logger.info("STARTUP Email retention: %d days", config.scheduler.email_retention_days)
    logger.info("STARTUP Temp file retention: %d days", config.scheduler.temp_file_retention_days)

    # Security status checks
    # Linux + bubblewrap is the only supported deployment configuration.
    # Other configurations are for development only and provide no isolation guarantees.
    from .executor import _bwrap_available
    multi_user = len(config.users) > 1
    if config.security.sandbox_enabled and not _bwrap_available():
        if multi_user:
            logger.warning(
                "SECURITY UNSUPPORTED CONFIGURATION: sandbox_enabled but bubblewrap unavailable "
                "with %d users configured — no filesystem isolation between users. "
                "Linux + bubblewrap is the only supported multi-user deployment.",
                len(config.users),
            )
        else:
            logger.warning(
                "SECURITY Sandbox enabled but bubblewrap unavailable (single-user, dev-only configuration)"
            )
    elif config.security.sandbox_enabled:
        logger.info("SECURITY Sandbox enabled with bubblewrap")
    else:
        if multi_user:
            logger.warning(
                "SECURITY UNSUPPORTED CONFIGURATION: sandbox_enabled=false with %d users configured — "
                "no isolation between users. Linux + bubblewrap is the only supported multi-user deployment.",
                len(config.users),
            )
        elif config.is_standalone:
            # Intended single-user local posture (istota serve / setup), not a
            # misconfiguration. Still visible so the trust model isn't hidden.
            logger.info(
                "SECURITY Standalone local install — no sandbox isolation. The "
                "agent runs with your user account's full privileges (trusted "
                "single-user posture). Only give it content and instructions you "
                "trust."
            )
        else:
            logger.warning(
                "SECURITY Sandbox explicitly disabled — no isolation guarantees (dev-only configuration)"
            )
    logger.info("SECURITY Skill proxy: %s", "enabled" if config.security.skill_proxy_enabled else "disabled")
    logger.info("SECURITY Network proxy: %s", "enabled" if config.security.network.enabled else "disabled")
    # Beside the sandbox lines rather than in a corner of its own: this is the
    # other thing that decides where a task's commands actually run. A feature
    # that silently no-ops is how the compose devbox reached the state
    # `312f8fd5` found it in, so the backend is named at start-up and checked by
    # `doctor` — a log line an operator can grep for, and a check that says
    # whether the rendered config and this process agree.
    _container_backend = istota_config.container_backend(config)
    if istota_config.devbox_container_backend(config):
        logger.info(
            "SECURITY Development container: %s — %s command(s) route into the "
            "user's devbox over the exec transport",
            _container_backend,
            len(config.developer.container.shim_commands),
        )
    else:
        logger.info(
            "SECURITY Development container: %s — project code builds and runs "
            "on the host",
            _container_backend,
        )

    # The runtime self-check. Logs every non-OK result and alerts the admin
    # allowlist on any failure; never aborts, whatever it finds (see the
    # function's own docstring for why a crash-loop is worse than degraded).
    #
    # On a thread, and its results carried to the loop rather than discarded.
    # Two reasons for the thread: `runtime.writable_dirs` and
    # `runtime.mount_liveness` stat the rclone FUSE mount with no timeout, where
    # a *hung* (as opposed to dropped) mount blocks uninterruptibly — the same
    # reason every other mount-touching sweep in this file is backgrounded — and
    # the alert fans out one 30s-timeout send per admin behind it. Neither
    # belongs between here and the loop starting.
    startup_doctor_results: list = []
    startup_doctor_thread = threading.Thread(
        target=lambda: startup_doctor_results.extend(run_startup_checks(config)),
        name="doctor-startup",
        daemon=True,
    )
    startup_doctor_thread.start()

    # Hydrate user configs from Nextcloud API (display name, email, timezone)
    try:
        hydrate_user_configs(config)
    except Exception as e:
        logger.warning("User config hydration failed: %s", e)

    # Ensure user directories exist for all configured users (runs migration + README seeding)
    for user_id in config.users:
        try:
            ensure_user_directories_v2(config, user_id)
        except Exception as e:
            logger.warning("Failed to ensure directories for %s: %s", user_id, e)

    # One-time / idempotent: copy tier-2 credentials declared in TOML resource
    # extras (monarch_email/password, karakeep api_key, …) into the encrypted
    # secrets table so the web UI can read them. Skipped when ISTOTA_SECRET_KEY
    # is unset; later starts skip rows that already exist.
    try:
        from . import secrets_store  # noqa: PLC0415

        secrets_store.import_from_user_configs(config.db_path, config.users)
    except Exception as e:  # noqa: BLE001
        logger.warning("Secrets import skipped: %s", e)

    # Phase 6: migrate per-user TOML profile fields into the user_profiles
    # table on first run. Idempotent — only writes rows that don't exist.
    try:
        from . import user_profiles as _up  # noqa: PLC0415

        _up.import_from_user_configs(config.db_path, config.users)
        # Re-apply DB rows onto config.users so the in-memory Config reflects
        # the freshly-imported values (the load_config call earlier in the
        # process saw an empty table). Cheap; only touches users that already
        # have rows.
        from .config import _apply_user_profiles  # noqa: PLC0415

        _apply_user_profiles(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("user_profiles migration skipped: %s", e)

    # Phase 7b: migrate per-user TOML briefings into the briefing_configs
    # table on first run. Idempotent — only writes rows whose
    # (user_id, name) pair doesn't already exist. Re-applies the overlay
    # so the in-memory config reflects DB-managed briefings.
    #
    # The workspace pass follows it and carries each user's retired
    # BRIEFINGS.md into the same table, once. Order matters: the file was the
    # live authority until this release, so it must land after the TOML seed
    # and overwrite it where the two name the same briefing — otherwise a user
    # with both silently changes schedule at the upgrade.
    try:
        from . import user_briefings as _ub  # noqa: PLC0415

        _ub.import_from_user_configs(config.db_path, config.users)
        _ub.import_from_workspace_files(config.db_path, config)
        from .config import _apply_user_briefings  # noqa: PLC0415

        _apply_user_briefings(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("user_briefings migration skipped: %s", e)

    # admin-shared-briefing-blocks: seed shared_block_configs from config
    # (DEFAULT_SHARED_BLOCKS / [[briefing_shared_blocks]]) on first run — seed-once,
    # DB-wins thereafter — then re-apply the overlay so in-memory config reflects
    # any DB edits.
    try:
        from . import shared_blocks_store as _sbs  # noqa: PLC0415

        _sbs.import_from_config(config.db_path, config.briefing_shared_blocks)
        from .config import _apply_shared_blocks  # noqa: PLC0415

        _apply_shared_blocks(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("shared_blocks seed skipped: %s", e)

    # Phase 1.3: purge orphan skill scheduled_jobs / pending skill tasks
    # whose skill name no longer exists in the index (e.g. operator
    # renamed `feeds` → `feed_reader`). Runs once per startup; the
    # seeders re-populate fresh rows on the next sync tick.
    try:
        from .skills._loader import load_skill_index as _lsi  # noqa: PLC0415

        _idx = _lsi(config.skills_dir, config.bundled_skills_dir)
        with db.get_db(config.db_path) as conn:
            _purge_obsolete_skill_jobs(conn, _idx)
    except Exception as e:  # noqa: BLE001
        logger.warning("Skill-job purge skipped: %s", e)

    # Reclaim tasks abandoned mid-execution by a dead prior instance before any
    # worker runs. Under the flock every running/locked row is an orphan, so
    # this recovers a restart in seconds instead of waiting out
    # worker_stuck_minutes (DB-only; doesn't need the asyncio runtime).
    try:
        recover_orphaned_tasks_on_startup(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("Startup orphan recovery skipped: %s", e)

    # Start the persistent asyncio runtime that hosts all Talk (and other) I/O
    # on one loop with one pooled httpx client. Explicit start here surfaces a
    # loop-creation failure before the daemon goes live rather than lazily on the
    # first run_coro call; every run_coro site (poller, delivery, consumers,
    # notifications) shares it.
    from .async_runtime import get_async_runtime
    get_async_runtime()
    logger.info("STARTUP Started persistent asyncio runtime")

    # Talk inbound has exactly one driver. With signaling enabled it is the
    # event stream, and `_talk_poll_loop` is **not started at all** — two
    # drivers at different cadences would double every fetch, race the module
    # globals in `inbound.py` across their awaits, and leave the sweep clock
    # owned by nobody. Without it the poller is the capability floor, unchanged.
    if config.talk.enabled and config.talk.signaling.enabled:
        _start_talk_signaling(config)
        logger.info("STARTUP Started Talk signaling supervisor (poller not started)")
    elif config.talk.enabled:
        talk_thread = threading.Thread(
            target=_talk_poll_loop, args=(config,), daemon=True, name="talk-poller",
        )
        talk_thread.start()
        logger.info("STARTUP Started Talk polling thread")

    # Create worker pool for per-user concurrent task processing
    pool = WorkerPool(config)

    # Defense-in-depth: a separate thread alerts if the single-threaded main
    # loop stops ticking (ISSUE-143). The loop bumps watchdog.tick() each
    # iteration below.
    watchdog = LoopWatchdog(config, config.scheduler.loop_stall_alert_seconds)
    watchdog.start()

    # Initialize status writer (the `status-write` gate imports `write_status`
    # itself, where it is used).
    from .status_writer import init_status_writer
    init_status_writer()

    # Which checks were failing at the last sweep, so the alert fires on the
    # transition into failure rather than once per interval forever. Owned by
    # the loop rather than by the module, matching `background_checks`.
    #
    # Seeded from the boot run: without it every failure the boot alert already
    # named counts as "newly failing" at the first sweep and alerts a second
    # time an hour later. Joined with a short deadline because the boot check
    # can be stuck on a hung mount, and the loop must start regardless — an
    # unseeded state costs one duplicate alert, which is much cheaper than
    # delaying dispatch.
    doctor_state: dict = {}
    startup_doctor_thread.join(timeout=30.0)
    if not startup_doctor_thread.is_alive():
        from . import doctor as _doctor

        doctor_state["failing"] = {r.name for r in _doctor.failing(startup_doctor_results)}
    else:
        logger.warning(
            "doctor_startup_slow: boot check still running after 30s; the first "
            "sweep may repeat its alert"
        )
    # Cooldown clocks for the threshold snapshot + admin notification, one per
    # trigger class so a residue notice cannot mute a MemAvailable collapse,
    # plus the re-armable flag that makes a silently-stopped backup page once
    # rather than every tick. Loop-local so a re-entered daemon and each test
    # start clean, which is also why they are dicts: the gate bodies below are
    # closures and a rebound local would not reach the caller.
    pressure_state: dict = {"last_alert": 0.0, "clocks": {}}
    backup_state: dict = {"alerted": False}
    # In-flight registry for the slow periodic checks that run off this thread
    # (ISSUE-144). Loop-local rather than process-global so tests and a
    # re-entered daemon each get a clean slate.
    background_checks: dict[str, threading.Thread] = {}
    # The periodic checks, and one clock per gate seeded from its own row.
    # Everything about when each runs — its interval's config field, its
    # enabling conditions, whether it goes on a background thread, and how its
    # clock starts — is stated once in `build_interval_gates`.
    interval_gates = build_interval_gates(
        config,
        pool=pool,
        background_checks=background_checks,
        doctor_state=doctor_state,
        pressure_state=pressure_state,
        backup_state=backup_state,
    )
    gate_clocks = seed_interval_clocks(interval_gates, config)

    # Signal the launcher (if any) that the pool + pollers are up and the loop
    # is about to start — it starts uvicorn only after this fires.
    if ready_event is not None:
        ready_event.set()

    while not _shutdown_requested:
        # Mark the loop alive for the stall watchdog before doing any work.
        watchdog.tick()

        # Dispatch worker threads first — minimizes latency for pending tasks
        try:
            pool.dispatch()
        except Exception as e:
            logger.error("Error dispatching workers: %s", e)

        now = time.time()

        # Run every periodic check that is due, in table order. What used to be
        # twenty-two `if now - last_x >= interval:` blocks — each with its own
        # clock seeded a hundred lines above and its own try/except — is one
        # table in `build_interval_gates` and one runner. Ordering is part of
        # the table: shared files before the TASKS.md poll, the backup
        # staleness alert straight after the snapshot gate, heartbeats last.
        _tick_interval_gates(
            interval_gates, gate_clocks, config, now, background_checks
        )

        # Sleep out the rest of the base tick, re-dispatching in sub-tick slices
        # so a freshly-enqueued task is claimed within dispatch_interval instead
        # of waiting a full poll_interval (the gated checks above stay on
        # poll_interval granularity — see _dispatch_sleep).
        _dispatch_sleep(pool, config, lambda: _shutdown_requested)

    # Stop the stall watchdog before tearing the rest down.
    watchdog.stop()

    # Shutdown workers before releasing lock
    pool.shutdown()

    # Stop the persistent asyncio runtime after the worker pool: cancels any
    # in-flight coroutine (the talk-poller daemon thread may be mid long-poll),
    # then runs cleanup hooks (closes the shared TalkClient httpx pool) and stops
    # the loop. reset_async_runtime also clears the process globals so an
    # in-process restart rebuilds from a clean slate. Best-effort — a hung
    # network coro can't block daemon shutdown (stop has its own timeout).
    try:
        reset_async_runtime()
        logger.info("Stopped persistent asyncio runtime")
    except Exception as e:  # noqa: BLE001
        logger.warning("Error stopping persistent asyncio runtime: %s", e)

    # Release lock on shutdown
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

    logger.info("Shutdown complete.")


def main():
    """Entry point for scheduler script."""
    import argparse

    from .logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Istota task scheduler")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run as daemon (continuous loop)")
    parser.add_argument("--max-tasks", type=int, help="Maximum tasks to process (single run mode)")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually execute tasks")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    config = load_config(Path(args.config) if args.config else None)

    # Configure logging based on config and flags
    setup_logging(config, verbose=args.verbose, daemon_mode=args.daemon)

    if args.daemon:
        if args.dry_run:
            logger.warning("--dry-run is ignored in daemon mode")
        try:
            run_daemon(config)
        except _DaemonAlreadyRunning:
            # Already logged an error inside run_daemon; exit cleanly.
            raise SystemExit(1)
    else:
        processed = run_scheduler(config, max_tasks=args.max_tasks, dry_run=args.dry_run)
        logger.info("Processed %d task(s)", processed)


if __name__ == "__main__":
    main()
