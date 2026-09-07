"""Claude Code execution wrapper."""

import contextlib
import errno
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading  # noqa: F401  — kept for `mock.patch("istota.executor.threading.Timer")` compat
import time  # noqa: F401  — kept for `mock.patch("istota.executor.time.sleep")` compat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import sqlite3

    from .brain import BrainResult

from . import db
from . import email_support
from . import task_cgroup
from . import task_env
from .claude_runtime_env import (
    CLAUDE_RUNTIME_ENV_VARS,  # used by `_PROXY_LOOKUP_BLOCKED` below, and
    # re-exported: the drift guard reads it beside `build_clean_env`.
    without_claude_runtime_env,  # noqa: F401  — re-exported; `task_env` owns
    # the only remaining call, and `tests/test_security.py` imports both names
    # from here beside `build_clean_env`.
)
from .config import Config
from .sandbox_plan import (
    Mount,
    SandboxProfile,  # noqa: F401  — re-exported; heartbeat, commands and doctor import it from here
    build_mount_plan,
    project_fs_roots,
    render_bwrap_argv,
)
from .context import (
    build_talk_context,
    format_context_for_prompt,
    format_talk_context_for_prompt,
    select_relevant_context,
    select_relevant_talk_context,
)
from .storage import (
    ensure_channel_directories,
    ensure_user_directories_v2,
    get_user_scripts_path,
    open_user_skill_overlays,
    read_channel_memory,
    read_dated_memories,
    read_user_config_file,
    read_user_memory_v2,
)
from .brain import (
    CANONICAL_ROLES,
    is_portable_alias,
    make_brain,
    model_namespace_for_kind,
    resolve_brain_kind,
)
from .brain._roles import PORTABLE_KEY
from .brain._fallback import (
    COOLDOWN_STOP_REASONS,
    TRIGGER_STOP_REASONS,
    effective_fallback_kind,
    get_availability_breaker,
    open_primary_breaker,
)
from .agent.events import READ_DESCRIPTION_PREFIX
from .events import EventWriter, random_progress_message
from .executor_stream import TaskStreamAdapter
from .image_attachments import (
    KIND_OMITTED,
    ImagePreparation,
    ocr_query_text,
    prepare_image_attachments,
    render_ocr_context,
)
from .shell_exec import pipefail_env
from .user_scope import scoped_user_dir
from .skills.calendar import get_caldav_client, get_calendars_for_user
from .skills.whisper.out_of_process import transcribe_audio_out_of_process

logger = logging.getLogger("istota.executor")

# Source types treated as interactive (live user behind the turn): they load
# conversation context, sticky skills, the skills changelog, and personal
# memory. The REPL and web chat are full-stack interactive surfaces like
# talk/email.
_INTERACTIVE_SOURCE_TYPES = ("talk", "email", "repl", "web")


# Distinct (user_id, tz_str) pairs already warned about, so a persistently
# invalid timezone config warns once per process rather than on every task.
_INVALID_TZ_WARNED: set[tuple[str, str]] = set()


def _resolve_user_tz(
    config: Config,
    user_id: str,
    *,
    conn: "sqlite3.Connection | None" = None,
) -> tuple[ZoneInfo, str]:
    """Return (ZoneInfo, tz_str) for a user, falling back to UTC.

    Delegates the DB-vs-in-memory timezone resolution to
    ``Config.resolve_user_timezone`` (so web-UI edits take effect without a
    scheduler restart — ISSUE-099) and wraps the result in a ``ZoneInfo``,
    falling back to UTC if the resolved name is not a valid zone. Pass
    ``conn`` to reuse an existing framework-DB connection on the hot path.

    An invalid zone name (e.g. the abbreviation ``PDT`` instead of the IANA
    name ``America/Los_Angeles``) logs one WARNING per distinct
    ``(user_id, name)`` so a misconfigured timezone is self-diagnosing rather
    than silently rendering every clock in UTC.
    """
    tz_str = config.resolve_user_timezone(user_id, conn=conn)
    try:
        return ZoneInfo(tz_str), tz_str
    except Exception:
        key = (user_id, tz_str)
        if key not in _INVALID_TZ_WARNED:
            _INVALID_TZ_WARNED.add(key)
            logger.warning(
                "Invalid timezone %r for user %s — falling back to UTC. Use an "
                "IANA name like 'America/Los_Angeles' (abbreviations such as "
                "'PDT'/'PST' are not valid).",
                tz_str, user_id,
            )
        return ZoneInfo("UTC"), "UTC"

# API error detection / retry policy moved into brain.claude_code; re-exported
# here for backward compatibility with callers (scheduler.py) and tests that
# import these symbols from istota.executor. Unused *here* by construction —
# that is what a re-export is — so F401 is silenced rather than obeyed.
from .brain.claude_code import (  # noqa: E402,F401  (kept after module docstring)
    API_ERROR_PATTERN,
    API_RETRY_DELAY_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    TRANSIENT_STATUS_CODES,
    is_signal_termination,
    is_transient_api_error,
    is_usage_limit_error,
    parse_api_error,
)

# Audio extensions eligible for pre-transcription (matches whisper skill file_types)
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"})

# Wall clock for pre-transcribing *all* of one send's audio, not each file.
# `_pre_transcribe_attachments` runs on a worker thread before the brain is
# called, so `scheduler.task_timeout_minutes` does not cover it and this is the
# only bound there is.
_PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS = 900.0

# Inbound image preparation — extension screening, the pre-decode gates,
# normalization, the two renditions and automatic OCR — lives in
# `image_attachments`, which owns the limits and the model-facing notices and
# imports no brain. `_preshrink_image_attachments` was its ancestor.


# Result composition + malformed-output detection moved to session.result in
# Phase 0 of the agent-loop migration. Re-exported here for backward
# compatibility with callers (scheduler.py) and tests that import these
# symbols from istota.executor.
from .session.result import (  # noqa: E402,F401
    _AUTOMATED_SOURCE_TYPES,
    _CM_SEGMENT_MIN_CHARS,
    _CODE_FENCE_PATTERN,
    _NO_FINAL_ANSWER_NOTICE,
    _TERSE_REFERENCE_RE,
    _TERSE_RESULT_MAX_CHARS,
    _TOOL_SYNTAX_PATTERN,
    _TRAILING_REGION_MIN_CHARS,
    _compose_full_result,
    _ensure_final_answer,
    _is_automated_task,
    _is_back_reference,
    _is_terse,
    _last_substantial_region,
    _log_compose_override,
    _text_similarity,
    detect_malformed_result,
    is_no_final_answer,
)


def _pre_transcribe_attachments(
    attachments: list[str] | None,
    prompt: str,
    cancel_check: "Callable[[], bool] | None" = None,
) -> str:
    """Pre-transcribe audio attachments so skill selection sees real text.

    Returns an enriched prompt with transcribed text, or the original prompt
    if no audio attachments or transcription fails.

    The transcript is *appended* to whatever the sender typed rather than
    replacing it: a voice memo can arrive alongside a written message ("have a
    listen and summarize this"), and dropping that half loses the instruction
    the audio was sent under. A send with no typed text (the composer's
    record-and-send) carries only the transcript.

    Each file is transcribed in its own child process. This used to call
    `transcribe_audio` directly, which imported faster-whisper into the daemon
    and left roughly 450 MB per transcription on glibc's free lists that
    nothing ever returned — five voice messages walked the scheduler's RSS from
    820 MB to 2894 MB in four steps, with no sign of stopping (ISSUE-273). See
    `skills/whisper/out_of_process.py` for the measurements.

    The whole loop shares one wall-clock budget rather than giving each file
    its own. This runs on a worker thread *before* the brain call, so nothing
    else bounds it — and a per-file timeout would mean a send carrying five
    audio files could hold the worker for five times the limit, which is the
    stall the timeout exists to prevent rather than a smaller version of it.

    `cancel_check` is polled between files, for the same reason
    `prepare_image_attachments` polls it: this window sits *before* the brain
    call, so `scheduler.task_timeout_minutes` does not cover it and
    `BrainRequest.cancel_check` is not yet in play. Without the poll `!stop` and
    the web cancel button are inert for the whole budget — 900 s here, and one
    send can put normalization and OCR behind it on the same worker. What is
    already transcribed is kept: the prompt is still better with a partial
    transcript than with none.
    """
    if not attachments:
        return prompt

    audio_paths = []
    for att in attachments:
        ext = Path(att).suffix.lstrip(".").lower()
        if ext in _AUDIO_EXTENSIONS:
            audio_paths.append(att)

    if not audio_paths:
        return prompt

    deadline = time.monotonic() + _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS
    transcribed_parts = []
    for audio_path in audio_paths:
        if _cancelled(cancel_check):
            logger.info("Audio pre-transcription cancelled, stopping the pass")
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Keep what earlier files produced; the prompt is still better with
            # a partial transcript than with none.
            logger.warning(
                "Pre-transcription budget exhausted, skipping %s and any files after it",
                Path(audio_path).name,
            )
            break
        try:
            result = transcribe_audio_out_of_process(audio_path, timeout=remaining)
            if result.get("status") == "ok" and result.get("text", "").strip():
                text = result["text"].strip()
                transcribed_parts.append(text)
                logger.debug(
                    "Pre-transcribed %s: %s",
                    Path(audio_path).name,
                    text[:100] + ("..." if len(text) > 100 else ""),
                )
            else:
                error = result.get("error", "unknown error")
                logger.debug("Pre-transcription failed for %s: %s", audio_path, error)
        except Exception:
            logger.debug("Pre-transcription error for %s", audio_path, exc_info=True)

    if not transcribed_parts:
        return prompt

    transcribed_text = " ".join(transcribed_parts)
    filenames = ", ".join(Path(p).name for p in audio_paths)
    block = (
        f"Transcribed voice message: {transcribed_text}\n\n"
        f"(Original audio: {filenames})"
    )
    return block if not prompt.strip() else f"{prompt}\n\n{block}"


def _cancelled(cancel_check: "Callable[[], bool] | None") -> bool:
    """Poll a cancellation channel without letting it break the pass.

    A channel that raises is not a reason to abandon a task's attachments, so
    a failure reads as "not cancelled" — the same posture
    `image_attachments._cancelled` takes for the same callable.
    """
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


def _make_cancel_check(config: Config, task_id: int) -> "Callable[[], bool]":
    """The task's cancellation channel: one predicate, three consumers.

    Audio pre-transcription, image preparation and the brain all poll the same
    thing. It opens its own short-lived connection rather than borrowing the
    caller's, because the caller may be holding a write transaction open and
    this is read-only.
    """
    def _check() -> bool:
        try:
            with db.get_db(config.db_path) as cancel_conn:
                return db.is_task_cancelled(cancel_conn, task_id)
        except Exception:
            return False

    return _check


def image_bind_roots(
    config: Config, task: db.Task, user_temp_dir: Path,
    control_dir: Path | None = None,
) -> list[Path]:
    """The roots an image attachment can live under and still be openable.

    `build_bwrap_cmd` binds the user temp dir plus `{mount}/Users/{user}`,
    `{mount}/Talk` and `{mount}/Channels/{token}`, and re-binds the task
    control directory read-only after all of them — and nothing else. The
    scheduler's nc-data fallback hands out `/mnt/nc-data/<user>/files/Talk/...`,
    which is under none of them, so a small in-limits screenshot arriving that
    way would be named in the Claude Code directive and be unreadable. Naming
    the roots here is what lets `prepare_image_attachments` copy such a file in
    even when it needs no resize and no conversion.

    `control_dir` is where the prepared renditions are *written*, and it is in
    this list to keep an invariant rather than to decide a copy: `_within_binds`
    tests the source, so nothing today reaches it. The output directory has
    always been inside `bind_roots` — it used to be, via `user_temp_dir` — and a
    destination outside the roots the same call is told about is the shape of a
    later copy loop or a second-pass rendition landing somewhere unreadable.
    `user_temp_dir` stays: source attachments still arrive there.

    Resolved, because `_bind` resolves its source and uses the *resolved* path
    as the in-namespace destination: on a deployment where `temp_dir` sits
    behind a symlink an unresolved path names a file that does not exist inside
    the namespace, and every `Read` fails.
    """
    roots: list[Path] = []
    # `user_temp_dir` is `{temp_dir}/{user_id}` and arrives already joined, so
    # an id that collapses makes this root `config.temp_dir` — every user's
    # scratch space and the whole `.control` tree, as a legal copy source
    # (ISSUE-402). `build_mount_plan` refuses such a task outright and
    # `ensure_task_control_dir` refuses it before that, so nothing reaches here
    # on the real path; the entry is skipped rather than trusted because this
    # list is an allowlist and a boundary should not rest on a caller two
    # frames up having already refused.
    if scoped_user_dir(config.temp_dir, task.user_id) is not None:
        roots.append(user_temp_dir)
    if control_dir is not None:
        roots.append(control_dir)
    mount = config.nextcloud_mount_path
    if mount:
        mount = Path(mount)
        # Scoped, not joined: an unscoped id collapses to `{mount}/Users` and
        # every user's attachments become a legal copy source (ISSUE-402). No
        # user root at all is the right fallback — the bind is dropped there
        # too, so a file under one would be unreadable inside the namespace.
        own = scoped_user_dir(mount / "Users", task.user_id)
        if own is not None:
            roots.append(own)
        roots.append(mount / "Talk")
        if task.conversation_token:
            roots.append(mount / "Channels" / task.conversation_token)
    resolved = []
    for root in roots:
        try:
            resolved.append(Path(root).resolve())
        except OSError:
            continue
    return resolved


def get_user_temp_dir(config: Config, user_id: str) -> Path:
    """Get the per-user temp directory path."""
    return config.temp_dir / user_id


CONTROL_DIR_NAME = ".control"


def get_task_control_dir(
    config: Config, user_id: str, task_id: int
) -> Path | None:
    """The daemon-owned directory for one task's framework-authored files.

    ``{config.temp_dir}/.control/{user_id}/task_{task_id}``, resolved.

    Framework-authored per-task files — the two prompt halves, the briefing
    metadata, the prepared image attachments — are written by the daemon and
    must not be modified by the model. They live here rather than in
    :func:`get_user_temp_dir` because that directory is the sandbox's
    ``--chdir`` target, bound read-write, and it is per *user* rather than per
    task: a concurrent task of the same user can create an entry in it, task
    ids are sequential and exported as ``ISTOTA_TASK_ID``, so the entry can be
    a dangling symlink named after a task that has not started yet.

    **A sibling of the per-user directories, not a child of one.** That is the
    whole of it: a directory inside a model-writable parent can be replaced
    with a symlink between the daemon's ``mkdir`` and bwrap's ``mount``, which
    is ISSUE-320 exactly. ``.developer`` survives that only because a later
    bind buries the swap; this directory would have no bind behind it. The
    shared temp root is bound at no path, so being one level up removes the
    window rather than racing it.

    **Resolved, unlike :func:`get_user_repos_dir`**, which returns its
    candidate as written. ``_ro_bind(src)`` with no explicit ``dest`` uses the
    *unresolved* string it was handed as the in-namespace destination, and
    ``BrainRequest.composed_system_prompt_path`` is a single value read by
    ``NativeBrain`` in the daemon and by the ``claude`` CLI inside the
    namespace — so host path and namespace path must be one string, which
    means the caller must hand over an already-resolved one. The last
    component is deliberately *not* resolved: :func:`ensure_task_control_dir`
    opens it ``O_NOFOLLOW``, and resolving here would follow a planted symlink
    and leave that open inspecting an ordinary directory.

    Returns None when ``user_id`` is empty or would escape the root — the same
    containment equality :func:`get_user_repos_dir` and :func:`daemon_work_dir`
    use, both halves of it, because truthiness alone lets ``.``, ``..`` and an
    absolute component through and the lexical half alone lets a symlink
    through. Also refuses a ``user_id`` that *casefolds* to
    :data:`CONTROL_DIR_NAME`: :func:`get_user_temp_dir` is a plain join, so a
    user of that name would put its model-writable scratch directory exactly
    where the control root goes, and on a case-insensitive filesystem
    ``.Control`` is that same directory — the equality has to be case-folded
    or the guard is defeated by the shift key on every developer host. A
    leading dot is not a legal user id anywhere one is produced; the refusal
    is explicit rather than relying on that.

    **The refusal covers a corrupt control root too, and that is why the two
    reasons are logged apart.** ``root`` is deliberately *not* resolved, so a
    symlink planted at ``.control`` makes ``candidate.resolve()`` resolve
    through it and the equality fails — the right answer, since resolving the
    root would instead accept the planted symlink and hand back a path outside
    the tree altogether. But the fault there is a symlink the daemon owns the
    parent of, not a bad user id, and a message blaming the user id sends an
    operator looking in the wrong place.

    ``task_id`` is coerced with ``int()`` before it is interpolated, and that
    is a containment check rather than tidiness: the equality above covers the
    ``user_id`` component and stops there, ``PurePath`` does not collapse
    ``..``, and the kernel resolves it at ``mkdir``. A ``task_id`` of
    ``"1/../../.."`` would otherwise name a directory outside the user's
    subtree, which is the one thing this function exists to prevent. Today's
    callers pass ``task.id`` off a SQLite row and the heartbeat's synthetic
    ``0``, so it is unreachable now and would be reached the first time a
    caller takes the id from a deferred-op JSON or a CLI argument — the same
    reason the notification resolvers coerce ``object_id``. ``task_0`` is a
    legal directory name and needs no special case.

    Does not create anything. Never raises — not for a hostile ``user_id`` and
    not for one of the wrong type, which is why the join is inside the ``try``
    and ``TypeError`` is caught beside ``OSError`` and ``ValueError``.
    """
    temp_dir = getattr(config, "temp_dir", None)
    if not temp_dir or not user_id:
        return None
    if isinstance(user_id, str) and user_id.casefold() == CONTROL_DIR_NAME.casefold():
        return None
    try:
        root = Path(temp_dir).resolve() / CONTROL_DIR_NAME
        candidate = root / user_id
        # Two checks, because neither catches the other's cases. The lexical
        # one refuses a component that never became a child (`.` is dropped by
        # `PurePath`, an absolute one replaces the root, a nested one goes
        # deeper); the resolved one refuses `..` and every symlink, which are
        # children by name and somewhere else on disk — including a symlink at
        # the control root itself, which is a different fault and is reported
        # as one below.
        contained = candidate.parent == root and candidate.resolve() == root / user_id
        leaf = f"task_{int(task_id)}"
    except (OSError, ValueError, TypeError):
        logger.warning(
            "task control dir: cannot name a directory under %s for user id "
            "%r, task id %r; not using it.",
            temp_dir, user_id, task_id,
        )
        return None
    if not contained:
        # Two reasons, one refusal. Which one it is decides where an operator
        # looks, so they are not collapsed into a single sentence.
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            logger.warning(
                "task control dir: %s is not a directory the daemon owns "
                "(a symlink or a non-directory is in its place); refusing to "
                "use it for user id %r.",
                root, user_id,
            )
        else:
            logger.warning(
                "task control dir: %s does not resolve to the subtree named "
                "by user id %r; not using it. A symlink or a path component "
                "in the user id would reach outside that user's own control "
                "directory.",
                candidate, user_id,
            )
        return None
    return candidate / leaf


def _ensure_control_level(path: Path, *, parents: bool) -> None:
    """One level of the control directory: a real directory, ours, at 0700.

    ``Path.mkdir(exist_ok=True)`` swallows ``FileExistsError`` whenever
    ``is_dir()`` says yes, and ``is_dir()`` follows a symlink — so a symlink
    pointing at a directory sails straight through the create, and a plain
    ``os.chmod`` afterwards would follow it too. ``O_NOFOLLOW | O_DIRECTORY``
    is what refuses both that and a regular file (and a FIFO, which would
    otherwise block the open), and ``fchmod`` then acts on the descriptor
    rather than on the name, so the mode lands on the inode that was opened or
    on nothing at all.

    **The uid is checked on the same descriptor**, before the mode is set. A
    directory type check alone says the level is a directory, not that it is
    *ours*: ``Config.temp_dir`` defaults to ``/tmp/istota``, whose parent is
    world-writable and sticky, so on a host where the daemon has not run yet
    any local account can pre-create a level and the tree meant to be
    unreadable by the model lands somewhere another user owns. The fd is
    already in hand, so ``fstat`` costs nothing, and it fails closed. What
    this cannot cover is a symlink at ``temp_dir`` *itself*: that is resolved
    before any of this runs, and the operator's configured root is trusted as
    given here exactly as ``security.sandbox_cache_dir`` is. The shipped
    deployment shapes put it under a directory the daemon owns
    (``{istota_home}/tmp`` under Ansible, ``/data/tmp`` under Docker); the
    ``/tmp`` default is a single-host convenience and not a shape this
    directory's guarantee is claimed on.

    The mode is re-asserted on every call because ``mkdir(exist_ok=True)``
    leaves an existing directory's mode alone: a directory created under a
    different umask, or widened by hand, would otherwise stay widened for the
    life of the deployment.

    Raises ``OSError`` — the caller turns it into the ``RuntimeError`` that
    fails the task.
    """
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700, parents=parents)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        owner = os.fstat(fd).st_uid
        if owner != os.geteuid():
            raise PermissionError(
                errno.EPERM,
                f"directory is owned by uid {owner}, not by the daemon",
            )
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def ensure_task_control_dir(config: Config, user_id: str, task_id: int) -> Path:
    """Create the directory :func:`get_task_control_dir` names, 0700 the whole
    way down, and return the resolved path.

    Raises ``RuntimeError`` when the path cannot be resolved or created. The
    caller is ``execute_task``, and a control directory that does not exist is
    a task that must fail rather than run with its standing instructions
    reachable by nothing — the same fail-closed argument ISSUE-375 made for
    ``composed_system_prompt_path``.

    All three levels are created and mode-asserted, not just the last: the
    control root and the per-user level are what keep one user's control files
    out of another's reach, and ``mkdir(parents=True)`` applies its ``mode``
    to the leaf only, creating every intermediate with the ambient umask.

    **Two mechanisms refuse a corrupt level, and which one covers which case
    is not what it looks like.** A *symlink* at the control root or at the
    per-user level is refused by :func:`get_task_control_dir`, whose
    containment equality resolves through both — this function is never
    reached. A *regular file* at those same levels is not, because
    ``Path.resolve()`` is non-strict and returns the lexical path even when an
    ancestor is not a directory, so the equality holds and it arrives here for
    ``O_DIRECTORY`` to refuse with ENOTDIR. And the whole ``task_{id}`` level
    arrives here whatever is at it, since the resolver deliberately leaves the
    last component unresolved — which makes ``O_NOFOLLOW`` the sole guard for
    a symlink there, and nowhere else. Removing it turns exactly that one case
    red, which is what the negative control in
    ``tests/test_task_control_dir.py`` records. The mode and ownership
    assertions apply at all three levels.

    Idempotent: a retry of the same task reuses the same directory, which is
    what makes it safe for ``execute_task`` to call unconditionally and for
    ``_build_module_briefing_prompt`` to call again underneath it.

    **One retry, and it is for the sweep rather than for flakiness.**
    ``cleanup_old_temp_files`` recurses into every subdirectory of
    ``temp_dir`` — ``.control`` included, since ``iterdir`` yields dotted
    entries — and ``rmdir``s any empty directory past
    ``temp_file_retention_days``. Neither ``mkdir`` on an existing directory
    nor ``fchmod`` updates an mtime, so a per-user level left empty and idle
    stays a valid candidate *while* this function is walking down through it,
    and the second level's ``mkdir(parents=False)`` then raises
    ``FileNotFoundError`` and fails a task for a directory that was collected
    underneath it. The retry restarts from the top so every level is
    re-created and re-asserted; ``parents=True`` on the lower levels would
    have papered over it while recreating the levels above with the ambient
    umask instead of 0700.

    The message names the path and never the contents of anything found there.
    """
    control_dir = get_task_control_dir(config, user_id, task_id)
    if control_dir is None:
        raise RuntimeError(
            f"cannot name a task control directory for user {user_id!r}, "
            f"task {task_id!r}: the user id is empty, is not a child of "
            f"{getattr(config, 'temp_dir', None)}/{CONTROL_DIR_NAME}, or that "
            f"directory is not one the daemon owns"
        )
    user_level = control_dir.parent
    root = user_level.parent
    # The shared temp root may not exist yet, so the control root takes
    # `parents=True` — the same posture `daemon_work_dir` takes for the
    # per-user directories beside it. `mkdir` applies its mode to the leaf
    # only, so this creates `temp_dir` with the ambient umask and `.control`
    # itself at 0700.
    levels = ((root, True), (user_level, False), (control_dir, False))
    for attempt in (1, 2):
        try:
            for level, parents in levels:
                _ensure_control_level(level, parents=parents)
        except FileNotFoundError as exc:
            # A level was collected under us between two iterations. Start
            # again once; a second occurrence is not a sweep race.
            if attempt == 2:
                raise RuntimeError(
                    f"task control directory {control_dir} kept disappearing "
                    f"while it was being created: {exc.strerror or exc}"
                ) from exc
            continue
        except OSError as exc:
            raise RuntimeError(
                f"task control directory {level} is unusable: "
                f"{exc.strerror or exc}"
            ) from exc
        return control_dir
    return control_dir


def _write_control_file(path: Path, text: str) -> None:
    """Write one framework-authored file: `O_NOFOLLOW`, `O_TRUNC`, mode 0600.

    Both prompt halves go through here so the rule is stated once. `O_TRUNC`
    is what makes a retry of the same task overwrite rather than append; 0600
    because these hold the persona, the user's own overlays, their email
    address, their retrieved memory and their request, and no other local
    account has any reason to read them.

    ``O_NOFOLLOW`` is belt-and-braces since the files moved out of the
    model-writable directory — see the call sites — and it is kept because a
    guard dropped on the strength of a property held somewhere else is the one
    nobody notices the loss of.

    The descriptor is closed by hand only on the path where ``os.fdopen``
    itself raises: it takes ownership when it returns, so closing after a
    successful call would be a double close, and a double close on a
    long-running daemon closes whatever fd the number was reused for.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(text)


class DaemonSandbox(NamedTuple):
    """What a task-less daemon-side model call needs to run confined.

    ``wrap`` goes on the request's ``sandbox_wrap`` and ``work_dir`` on its
    ``cwd``; the two travel together because bwrap chdirs into ``work_dir``
    inside the namespace, so a request naming a different directory would
    disagree with its own wrap.

    ``wrap`` is ``None`` where there is no namespace to build, and
    ``refused`` is what tells the two reasons apart. ``False`` means nothing
    was asked for: the operator set ``sandbox_enabled = false``, and an
    ordinary task is unconfined on that deployment too. ``True`` means a
    namespace was wanted and could not be built, which is the ISSUE-397
    exposure exactly — a caller granting a file tool must treat it as a
    refusal to run rather than as permission to run unconfined.
    """

    wrap: Callable[[list[str]], list[str]] | None
    work_dir: Path
    refused: bool = False


def daemon_work_dir(config: Config | None, user_id: str) -> Path:
    """The scratch directory a task-less daemon-side model call runs in.

    ``{config.temp_dir}/{user_id}`` — one level below the shared root, because
    that is what :func:`build_daemon_sandbox` binds into the namespace
    (ISSUE-397). The shared root is bound by nothing, so a caller that writes
    an upload's temp copy there hands the model a path it cannot open.

    Returns the shared root itself when there is no per-user directory to name:
    an empty ``user_id``, or one whose join escapes the root. That return value
    is also the signal :func:`build_daemon_sandbox` refuses on — binding the
    shared root would put every user's scratch space in one user's namespace,
    which is worse than not wrapping at all. The containment test is the same
    equality :func:`get_user_repos_dir` uses, and for the same reason:
    truthiness alone lets ``.``, ``..`` and an absolute component through.

    It names the same directory as :func:`get_user_temp_dir` and is not a
    second rule for it: that one is the task path's plain join, where
    ``execute_task`` does the ``mkdir`` and the sandbox plan carries the
    containment. There is no executor around a daemon-side call, so the
    resolve, the ``mkdir`` and the refusal are folded in here.

    Creates the directory it names. Never raises.
    """
    return _daemon_dirs(config, user_id)[1]


def _daemon_dirs(config: Config | None, user_id: str) -> tuple[Path, Path]:
    """``(shared root, per-user work dir)``, equal when the id could not scope.

    Returning both is what lets :func:`build_daemon_sandbox` tell a scoped
    directory from the fallback without re-deriving the rule — inferring it
    from the returned path alone is the kind of second copy that goes quietly
    wrong.

    The containment test is :func:`get_user_repos_dir`'s, both halves of it,
    because neither half catches the other's cases: the lexical one refuses a
    component that never became a child (``.`` is dropped by ``PurePath``, an
    absolute one replaces the root, a nested one goes deeper), and the resolved
    one refuses ``..`` and every symlink, which are children by name and
    somewhere else on disk. A ``resolved.parent == root`` test looks like it
    covers the second and does not: ``{temp_dir}/alice -> {temp_dir}/bob``
    resolves to another child of the root, passes, and would put bob's scratch
    directory in alice's namespace read-write.

    The resolved path is what is returned, since it is what goes on to bwrap
    and into the prompt, and those two must name one directory.
    """
    root = Path(getattr(config, "temp_dir", None) or tempfile.gettempdir())
    with contextlib.suppress(OSError):
        root = root.resolve()
    work_dir = root
    if user_id:
        candidate = root / user_id
        try:
            contained = (
                candidate.parent == root and candidate.resolve() == root / user_id
            )
        except (OSError, ValueError):
            contained = False
        if contained:
            work_dir = root / user_id
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Not fatal here — `build_daemon_sandbox` refuses to wrap a directory
        # that is not there, and an unwrapped caller fails at its own open with
        # a real errno. Logged because the previous code in the two upload
        # routes called `mkdir` directly, so a permission problem used to
        # surface at the route rather than as a later missing directory.
        logger.warning("daemon_work_dir_mkdir_failed path=%s error=%s", work_dir, e)
    return root, work_dir


def build_daemon_sandbox(
    config: Config,
    user_id: str,
    *,
    extra_ro_binds: Iterable[Path] | None = None,
) -> DaemonSandbox:
    """Bubblewrap for a model call that has no task behind it (ISSUE-397).

    The OCR extractors build their own ``BrainRequest`` rather than going
    through :func:`execute_task`, so none of the per-task plumbing above runs
    for them — including the sandbox. That mattered more than it looks:
    ``build_claude_cli_flags`` reads a non-empty ``allowed_tools`` as the signal
    to add ``--dangerously-skip-permissions`` and emit no ``--allowedTools``
    allowlist at all, so a request asking for ``Read`` gets the CLI's whole
    default toolset, ``Bash`` and ``Write`` included. The two Claude brains take
    their filesystem boundary from bubblewrap and ignore ``fs_read_roots``
    entirely, so without a wrap that toolset ran host-side as the daemon user.

    ``SandboxProfile.CLAUDE``, following ``heartbeat.py``'s task-less
    ``claude -p``: the process being wrapped is that CLI, which needs its own
    binary, its credential and its state directory. The synthetic ``db.Task``
    is what the per-user binds key on; ``conversation_token`` stays empty, so no
    channel directory is bound.

    No ``net_proxy_sock``, so no ``--unshare-net``: these calls run in the
    daemon's own network namespace and have to reach the provider. That is the
    same posture ``heartbeat.py`` takes, and it is why
    :func:`build_model_cli_env` carries the proxy and CA-bundle names forward.

    ``extra_ro_binds`` is how the caller names the document. The
    ``{mount}/Users/{user_id}`` bind covers a bloodwork panel's upload but not
    every caller: the encounter and immunization routes hand the extractor a
    temp copy, and ``python -m istota.health.ocr`` an arbitrary local file. A
    wrap that hides the document is an outage rather than a boundary, so the
    file is bound by name. Read-only even where it falls inside the read-write
    ``work_dir`` bind — the extractor reads it and nothing more, and a later
    ``--ro-bind`` on a path under an earlier ``--bind`` is what takes the write
    away. That "later" is why ``build_bwrap_cmd`` emits ``extra_ro_binds``
    after every other bind rather than in the middle, where it used to sit.

    **A document under a masked database directory would be hidden, and that is
    left as is.** The masks are last and shadow whatever is beneath them, and
    the document is not in ``mask_protected_paths``, so such a wrap would be an
    outage. Protecting it would refuse the mask instead, which trades a
    boundary for the outage rather than removing it — the worse of the two. No
    shipped caller lands there: a panel's upload is on the Nextcloud mount, the
    two routes' temp copies are under ``work_dir``, and
    ``python -m istota.health.ocr`` uses a system temp dir.

    ``wrap`` is ``None`` in two cases and they are **not** the same answer, so
    see ``refused`` on :class:`DaemonSandbox`. ``security.sandbox_enabled =
    false`` is a deployment that confines no task at all, and this one runs
    with it; a ``user_id`` that names no directory under ``config.temp_dir``
    is a namespace that was wanted and could not be built, and a caller
    granting a file tool must decline to run rather than run unconfined.

    That the wrap is non-``None`` does not mean a namespace exists. Where
    bubblewrap is unavailable — macOS, the shipped Docker stack, which grants
    neither `seccomp:unconfined` nor `systempaths=unconfined` (ISSUE-381) —
    the flag still reads true, `build_bwrap_cmd` returns its argument
    unchanged, and the closure is inert. Same posture as an ordinary task on
    those hosts, which is the whole claim: equal to the rest of the
    deployment, not better than it.

    **`is_admin` is the caller's real one and stays that way**, which is worth
    stating because narrowing it looks like a free win and is not. For an
    admin on a `developer.enabled` deployment the namespace therefore also
    carries `{repos_dir}/{user_id}` read-write and the derived package cache —
    reached, on this path, by a prompt whose input is an uploaded document. It
    stays because `sandbox_cache_is_derived` reads `config.is_admin(user_id)`
    itself rather than this argument: passing `False` would derive the cache
    inside the repos subtree and then skip the repos bind that buries a symlink
    swapped in under it, which is ISSUE-320 reopened. The two gates are one on
    purpose, and narrowing the namespace here means narrowing them together.

    Never raises; a failure to read the user's resources costs the resource
    binds, not the wrap.
    """
    root, work_dir = _daemon_dirs(config, user_id)
    if not config.security.sandbox_enabled:
        return DaemonSandbox(None, work_dir)
    if work_dir == root or not work_dir.is_dir():
        logger.warning(
            "daemon_sandbox_refused user_id=%r work_dir=%s — no per-user "
            "directory to build a namespace around; a tool-granting caller "
            "must not run here",
            user_id, work_dir,
        )
        return DaemonSandbox(None, work_dir, refused=True)

    task = db.Task(
        id=0,
        status="running",
        source_type="cli",
        user_id=user_id,
        prompt="",
        conversation_token="",
    )
    try:
        with db.get_db(config.db_path) as conn:
            user_resources = db.get_user_resources(conn, user_id)
    except Exception as e:  # noqa: BLE001 — a missing DB costs binds, not the wrap
        logger.debug(
            "daemon_sandbox_resources_unavailable user_id=%r error=%s", user_id, e
        )
        user_resources = []
    is_admin = config.is_admin(user_id)
    binds: list[Path] = []
    for path in extra_ro_binds or []:
        try:
            binds.append(Path(path).resolve())
        except OSError:
            continue

    def _wrap(raw_cmd: list[str]) -> list[str]:
        return build_bwrap_cmd(
            raw_cmd, config, task, is_admin, user_resources, work_dir,
            extra_ro_binds=binds, profile=SandboxProfile.CLAUDE,
        )

    return DaemonSandbox(_wrap, work_dir)


def get_user_repos_dir(config: Config, user_id: str) -> Path | None:
    """The task's own subtree of ``developer.repos_dir``, or None.

    ``developer.repos_dir`` is a root of per-user subtrees rather than one
    shared tree: ``{repos_dir}/{user_id}/{namespace}/{project}.git``. An admin
    developer task binds only its own, so one admin cannot read or write
    another's clones, worktrees, model-written git configs or package caches.
    That is structural — there is no mask to emit and no argv ordering to
    preserve, which is the whole of what ISSUE-319 had to get right.

    The three places that need this path — the bwrap bind, the native brain's
    write roots, and the developer skill's ``setup_env`` — must not disagree
    about it, and the last of those cannot import this module (it is a skill
    module; ``executor`` imports the skill package). So the rule is stated here
    and repeated there against this docstring, with
    ``tests/test_sandbox.py::TestPerUserReposDir`` holding the two equal.

    ``user_id`` is joined plainly, exactly as :func:`get_user_temp_dir` joins
    it. Deliberately one rule and not two: user ids already reach the
    filesystem through that function, and a second, stricter spelling here
    would mean a user whose task directory exists and whose repos directory
    does not, silently.

    What *is* checked is that the join did what it says —
    :func:`~istota.user_scope.scoped_user_dir`, the same equality rule
    ``sandbox_cache_sweeper`` uses and for the same reason. Truthiness alone
    lets three values through that resolve outside one user's subtree: ``.``
    collapses to the shared root, ``..`` to its parent, and an absolute
    component replaces the root outright. The rule was written here and had no
    counterpart at the Nextcloud mount join next door, which is ISSUE-402; it
    lives in that leaf now and this function is one of its four callers. And
    the entry is model-plantable:
    every deployment running the shared bind gave a task read-write access to
    this root, so ``{repos_dir}/{user_id}`` may already be a symlink someone
    left there, which ``_bind`` and ``_add`` both resolve and which ``chmod``
    would follow. That is not a stricter rule about user ids; it is the check
    that the path named is the one the layout describes.

    Validated resolved, returned **as written**, like
    :func:`resolve_sandbox_cache_dir`: ``_bind`` uses the string it is handed
    as the sandbox destination, so returning the resolved path would put a
    symlinked deployment root at a different name inside the namespace from
    everything else bound under it, hence on another mount.

    None when the layout cannot be named — no configured root, no user id, or
    a join that lands somewhere else. The fallback in each case would be the
    shared root, which is the exposure this split exists to remove, so it fails
    closed instead.
    """
    root = config.developer.repos_dir
    if not root or not user_id:
        return None
    # `scoped_user_dir` is where the two checks live now — this function is
    # where they were written, and the Nextcloud mount join next door had no
    # equivalent until ISSUE-402 gave the rule one home.
    candidate = scoped_user_dir(root, user_id)
    if candidate is None:
        logger.warning(
            "developer.repos_dir: %s does not resolve to the subtree named "
            "by user id %r; not using it. A symlink or a path component in "
            "the user id would reach outside that user's own tree.",
            f"{root}/{user_id}", user_id,
        )
    return candidate


def discover_calendars_for_task(
    task, config: Config,
) -> list[tuple[str, str, bool]]:
    """Best-effort CalDAV discovery for the task's user.

    Returns ``[]`` when CalDAV is not configured, the server is
    unreachable, or the user owns no calendars. Used by the LLM,
    skill-task, and command-task code paths so manifest specs gated on
    ``gate_has_discovered_calendars`` resolve consistently across all
    three.
    """
    if not (config.caldav_url and config.caldav_username and config.caldav_password):
        return []
    try:
        # ISSUE-101: DAVClient owns a requests.Session whose urllib3 pool
        # spawns a daemon watchdog thread on first connection. Without
        # close() the thread and the open socket leak per call — over
        # days the scheduler accumulated 6000+ of each.
        with get_caldav_client(
            config.caldav_url, config.caldav_username, config.caldav_password,
        ) as client:
            return get_calendars_for_user(client, task.user_id) or []
    except Exception:
        return []


def _resolve_effort(task, config: Config) -> str:
    """Resolve the effort flag for a task: the task's own pin, or nothing.

    Why the empty return rather than a deployment default: `config.effort` was
    the *claude_code* brain's default living at the root, and returning it here
    put it on every request whatever brain was about to run — which is one half
    of ISSUE-418, and is why a room pinned to `native` with
    `[brain.native] effort = "high"` ran at the top-level `medium`. Each brain
    now applies its own, so a request carries an effort only when something
    actually chose one for this task.

    The `task_model and not task_effort` branch survives the change and is not
    made redundant by it. It says a per-task *model* pin drops an effort that
    was chosen for a different model — a cron job pinned to Haiku must not
    inherit `high` from anywhere, since Haiku rejects the flag outright. That
    now bites on the brain's own default rather than on the top-level one, and
    the brains implement it: `ClaudeCodeBrain._with_defaults` fills its default
    effort only when the request pins no model.

    `config` is still taken, and still read by nothing here. Kept because every
    caller has it and the signature is the seam ISSUE-417 widens.
    """
    task_model = (task.model or "").strip()
    task_effort = (task.effort or "").strip()
    if task_model and not task_effort:
        return ""
    return task_effort


def _resolve_advisor(task, config: Config) -> str:
    """Resolve the ``advisor_model`` for a task, unresolved (alias/raw form).

    A per-task model pin — whatever set it: ``!model``, ``!room model``, a
    ``[[jobs]] model``, an API caller — drops the configured advisor. The CLI's
    advisor gate has two independent checks, and only one is fatal: a *main*
    model that doesn't support the advisor tool at all exits non-zero with no
    result (pin-dependent — this is what a stale pin risks); a capability
    mismatch between two otherwise-advisor-capable models only warns and the
    task still completes. Dropping on any pin sidesteps the fatal case without
    Istota needing to track which models support the advisor tool at all —
    that's the CLI's own catalog. Only the unpinned default path gets the
    configured advisor.
    """
    task_model = (task.model or "").strip()
    if task_model:
        return ""
    return (config.advisor_model or "").strip()


def persist_brain_usage(
    config: Config,
    conn,
    *,
    usage,
    origin: str,
    user_id: str,
    brain_kind: str = "",
    task_id: int | None = None,
    source_type: str = "",
    is_fallback: bool = False,
    model: str = "",
    effort: str = "",
    stop_reason: str = "",
    success: bool = False,
) -> None:
    """Record one brain attempt's token/cost usage. Best-effort throughout.

    ``usage`` is a ``BrainUsage`` or None (``TmuxClaudeBrain`` leaves it None —
    it reconstructs events from a transcript and has no result frame, so a row
    would be a synthetic zero dragging every average).

    ``model`` is the model the attempt actually ran, and it wins over
    ``usage.model``. The two differ where it matters: ``usage.model`` is the
    CLI's cost-weighted dominant model, and it is empty outright for a native
    row, which reports one total with no per-model split. Without this every
    native row would land with no model and Stage 5's per-model grouping would
    bucket the whole native fleet as unknown.

    ``origin`` names the caller: ``task`` for the executor's own path, or the
    daemon call site for the model invocations that have no task at all
    (``sleep_cycle``, ``shared_blocks``, ``health_ocr``, …). Those pass
    ``task_id=None``; without the column they would be invisible in both
    directions — absent from the usage table and absent from any unmeasured-task
    count, because they were never tasks.

    The ``logger.info`` breadcrumb is kept deliberately: it is what leaves a
    figure greppable in the journal when the DB write is the thing that failed.

    Never raises. Telemetry must not turn a completed task into a failed one,
    and the writer's SAVEPOINT means the swallowed case is always a *complete*
    failure rather than a parent with a partial per-model split.
    """
    if usage is None:
        return
    logger.info(
        "brain_usage origin=%s task_id=%s brain=%s model=%s billed_input=%d "
        "cache_read=%d cache_write=%d output=%d cost=%s basis=%s",
        origin, task_id, brain_kind, model or usage.model,
        usage.billed_input_tokens, usage.cache_read_tokens,
        usage.cache_write_tokens, usage.output_tokens,
        round(usage.cost_usd, 6), usage.cost_basis,
    )
    try:
        if conn is not None:
            _insert_usage_row(
                conn, usage=usage, origin=origin, user_id=user_id,
                brain_kind=brain_kind, task_id=task_id, source_type=source_type,
                is_fallback=is_fallback, model=model, effort=effort,
                stop_reason=stop_reason, success=success,
            )
        else:
            with db.get_db(config.db_path) as usage_conn:
                _insert_usage_row(
                    usage_conn, usage=usage, origin=origin, user_id=user_id,
                    brain_kind=brain_kind, task_id=task_id,
                    source_type=source_type, is_fallback=is_fallback,
                    model=model, effort=effort, stop_reason=stop_reason,
                    success=success,
                )
    except Exception:
        logger.warning(
            "failed to persist usage (origin=%s task=%s) — spend not recorded",
            origin, task_id, exc_info=True,
        )


def _insert_usage_row(conn, **kwargs) -> None:
    db.insert_task_usage(conn, **kwargs)


def _persist_task_usage(
    config: Config,
    conn,
    task_id: int,
    usage,
    *,
    user_id: str = "",
    source_type: str = "",
    brain_kind: str = "",
    is_fallback: bool = False,
    model: str = "",
    effort: str = "",
    stop_reason: str = "",
    success: bool = False,
) -> None:
    """The task-shaped wrapper over `persist_brain_usage`."""
    persist_brain_usage(
        config, conn, usage=usage, origin="task", user_id=user_id,
        brain_kind=brain_kind, task_id=task_id, source_type=source_type,
        is_fallback=is_fallback, model=model, effort=effort,
        stop_reason=stop_reason, success=success,
    )


def _native_with_user_key(native_config, config: Config, user_id: str):
    """Overlay the user's per-user native-brain API key onto the native config.

    Looks up the encrypted ``native_brain``/``api_key`` secret for ``user_id``;
    when present it replaces the instance-wide key (`[brain.native] api_key` /
    `ISTOTA_BRAIN_NATIVE_API_KEY`), enabling per-user provider credentials in a
    multi-user deployment. Falls back to the instance key on absence/error so a
    missing secret never blocks the task. Returns a copy — never mutates input.
    """
    import dataclasses

    try:
        from . import secrets_store

        key = secrets_store.get_secret(
            config.db_path, user_id, "native_brain", "api_key"
        )
    except Exception:
        logger.debug(
            "native api key secret lookup failed for user=%s", user_id, exc_info=True
        )
        key = None
    if key:
        return dataclasses.replace(native_config, api_key=key)
    return native_config


# --- Brain fallback (availability failover) --------------------------------
# Generalizes the old tmux→claude_code in-attempt fallback so an operator can
# configure any brain as a fallback for any primary, triggered when the primary
# is unavailable (usage limit / missing binary / tmux launch failure). Stays at
# the executor level: brains have no Config (needed for the operator alert), and
# the same-attempt/no-increment rerun already lives here.


def config_alias_portable_names(config) -> set[str]:
    """The portable alias names for the cross-brain fallback check.

    The canonical tiers (``fast``/``general``/``smart``) plus any custom alias an
    operator flagged ``portable = true`` in ``[models.aliases]``. A shortcut
    (``opus``) or canonical id is deliberately absent — it pins one provider and
    can't cross the boundary. Derived from the config's raw alias mapping so it's
    independent of global load-order state.
    """
    def _truthy(raw):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"true", "1", "yes", "on"}
        return False

    names = set(CANONICAL_ROLES)
    aliases = getattr(getattr(config, "models", None), "aliases", None) or {}
    for name, value in aliases.items():
        if isinstance(value, Mapping):
            for key, raw in value.items():
                if str(key).lower() == PORTABLE_KEY and _truthy(raw):
                    names.add(str(name).strip().lower())
    return names


def _resolve_crossing_model_effort(
    task, config, target_brain, effort, *, origin_namespace,
):
    """Resolve (model, effort, dropped_pin) for a pin meeting another brain.

    The question is whether the model name **crossed into a brain that speaks a
    different vocabulary**, and until ISSUE-417 nothing asked it. This function
    crossed unconditionally, so a `claude_code -> tmux_claude` fallback — the
    same `claude` binary, the same ``anthropic`` namespace, the same valid
    ``claude-opus-5`` — dropped the pin anyway *and* put a "your pin was
    dropped" note in front of the user, for no reason. Measured both ways
    before the fix::

        claude_code -> native       ns=openai_compat  dropped_pin='claude-opus-5'
        claude_code -> tmux_claude  ns=anthropic      dropped_pin='claude-opus-5'

    Where the namespace is **unchanged** the name is used exactly as it arrived,
    which is what the primary path does and must stay byte-identical to. Where
    it changed, the rule is the one this function always had: a portable intent
    (tier fast/general/smart, plus operator ``portable = true`` aliases) is
    re-resolved in the target's namespace, and a non-portable pin (provider
    shortcut, canonical id) cannot carry — the target uses its own default and
    the requested name comes back as ``dropped_pin`` for the visible note and
    the INFO log.

    ``origin_namespace`` is where the *name* was written, not where the task
    ran: on the fallback path it is the primary brain's. ``None`` means it could
    not be established, and is deliberately **not** treated as "matches" — a
    pin whose portability is unknown is dropped, the same direction
    ``commands._clear_pin_across_namespaces`` takes for the same reason.

    Reads the task's pin alone. It used to fall back to `config.model`, which
    made an *unpinned* task on a `claude_code` deployment carry a non-portable
    anthropic id into the fallback, have it dropped, and put that same note in
    front of a user who had pinned nothing (ISSUE-418). With the deployment
    default gone the empty branch below is the ordinary case, and it already
    does the right thing: the target brain applies its own.
    """
    raw = (task.model or "").strip()
    if not raw:
        return ("", effort, None)
    target_namespace = getattr(target_brain, "model_namespace", None)
    # `None` is "not established" and must never compare equal — an origin we
    # could not resolve is treated as a crossing, which drops a pin whose
    # portability is unknown rather than sending it to a wire that may not
    # take it. Same direction `commands._clear_pin_across_namespaces` takes.
    crossing = origin_namespace is None or origin_namespace != target_namespace
    if not crossing or is_portable_alias(raw, config_alias_portable_names(config)):
        # Resolve through the target brain's own alias table. Two cases share
        # this arm, and folding them is what keeps the non-crossing branch
        # honest: a portable intent crossing a boundary must land on a valid
        # slug and effort in the new namespace (a customized ``smart`` falling
        # back claude_code→native must not carry the anthropic value), and a
        # pin that crossed nothing must come out exactly as the *primary* path
        # would have produced it — which is `resolve_model_name`, not the raw
        # string. Returning `raw` here was a regression on a previously correct
        # path: the alias tables are namespace-keyed, so within one namespace
        # this is by construction the same answer the primary got, while the
        # raw string hands the CLI an alias it does not accept and drops the
        # effort an alias like ``smart`` carries. Fall back to the id-only path
        # defensively if the pair is empty.
        pair = target_brain.resolve_alias(raw)
        if pair and pair[0]:
            return (pair[0], pair[1] or effort, None)
        return (target_brain.resolve_model_name(raw), effort, None)
    logger.info(
        "fallback_model: non-portable %r dropped across a namespace change "
        "(%s -> %s); using the target brain's default",
        raw, origin_namespace, target_namespace,
    )
    return ("", effort, raw)


def _pin_origin_namespace(task, config) -> str | None:
    """The namespace a task's own model pin was written in (ISSUE-417).

    Not where the task *runs* — where the name came from, which is the only
    thing that says whether it can carry.

    **``tasks.model_namespace`` is the answer wherever it is set, and it is a
    recorded fact rather than an inference** (ISSUE-420). The producer writes it
    beside the model, from the brain it actually resolved the alias against, so
    nothing here has to reconstruct it. The two branches below are what answers
    a row that predates the column, and they are kept because that is a real
    population rather than a migration nobody ran: NULL means "not recorded",
    and the inference is exactly what this function did before, so no existing
    row's outcome moved when the column arrived.

    The inference cannot be made correct on its own, which is why the column
    exists. Both branches read a *current* fact to describe a *past* write:

    - ``tasks.brain`` set means the pin came from the room, and every writer of
      ``rooms.model`` resolves through that room's own brain
      (``commands.brain_for_room``, ``web_app._brain_for_room_token``), so the
      pin is in *that* kind's namespace — **provided the pin was admitted when
      the model was written**. `brain_for_room` hands the pin to
      ``resolve_brain_kind``, which refuses a kind absent from
      ``[brain] room_selectable`` and falls through to the lane, so a model
      written after the operator shortened that list is in the lane's namespace
      while the column still names the refused kind. The two cases leave
      identical rows and want opposite answers, so no rule over this row can
      separate them; that is ISSUE-420, and the recorded namespace above is what
      settles it.
    - Otherwise the pin was written against the brain this task's *lane*
      resolves to, which is ``resolve_brain_kind`` with no override — not
      ``[brain] kind`` (ISSUE-419). The two largest producers ask exactly that
      question before they write: ``check_scheduled_jobs`` resolves a
      ``[[jobs]] model`` through ``resolve_brain_kind("scheduled", …)``, and a
      room's ``!model`` goes through ``commands.brain_for_room``. Reading the
      base kind made every ``[brain.source_type_overrides]`` deployment answer
      with a namespace nothing had written in, so a cron ``model = "smart"``
      routed to native was resolved as native's model, compared against
      anthropic, read as a crossing and dropped — the operator's pin silently
      replaced by the brain's own default, at INFO.

    **Read what that second bullet does to this function's one caller before
    changing anything here.** ``_request_model`` passes the routed brain as the
    target, and for an unpinned task this now returns that brain's own
    namespace by construction, so the crossing rule can never fire on the
    primary path for such a task. The check survives there only for a *pinned*
    task, and on the fallback path, where ``_run_fallback`` compares against a
    different brain than the one that ran. That is the intended shape and not an
    oversight: the premise is that nobody writes a name into ``tasks.model``
    except through the lane's own brain.

    **Three producers do not meet that premise** (ISSUE-421), and each ends
    with a foreign-namespace id passed to ``resolve_model_name``, which hands an
    unknown id straight through. None was introduced here — before ISSUE-419
    each was dropped instead, which was the right outcome reached by a rule
    that was wrong about why. Only the third is fixed; the other two are
    outstanding and belong at the producer:

    - ``repl/session`` resolves its own ``!model`` through
      ``make_brain(config.brain)`` — the base kind — the same expression
      ISSUE-419 removed from the scheduler, and it writes no namespace, so a
      ``[brain.source_type_overrides] repl`` deployment still infers the lane's
      for a name resolved in the base kind's. Outstanding.
    - ``scheduler_deferred`` copies a parent's ``model`` onto a ``subtask``
      row, so a deployment routing the two lanes to different namespaces hands
      the child a name resolved in the parent's. The recorded namespace is
      carried across with it where the parent *has* one, which covers a subtask
      of a room turn; a parent that bypassed ``record_inbound`` — cron, email,
      briefing, heartbeat — records nothing, and the child still infers its own
      lane. Partially covered, and the producer fix is still outstanding.
    - ``rooms.model`` is one column shared by every surface bound to a room,
      written against the *writing* surface's lane and read here against the
      *inbound* one, so a room whose model was set from web and whose next
      message arrives over Talk disagreed wherever those two lanes route
      differently. **Fixed by ``rooms.model_namespace``**: the writing surface
      records the namespace it resolved in and ``record_inbound`` freezes it
      onto the task, so the inbound surface reads the fact instead of guessing
      from its own lane.

    The routing read is guarded because ``tasks.source_type`` is TEXT with no
    ``CHECK`` and SQLite is dynamically typed, so ``resolve_brain_kind``'s
    ``(source_type or "").strip()`` is reachable with a number on the row. It is
    a guard for direct callers and tests rather than for ``execute_task``, which
    calls ``resolve_brain_kind`` on the same value unguarded long before the
    request is built — so a task that would trip this has already failed. The
    residue is the base kind, the answer this returned before routing was
    consulted.

    ``None`` where the kind does not resolve, which the crossing rule reads as
    "not established" and therefore treats as a crossing.
    """
    recorded = (getattr(task, "model_namespace", None) or "").strip()
    if recorded:
        return recorded
    pinned = (getattr(task, "brain", None) or "").strip()
    if pinned:
        return model_namespace_for_kind(pinned)
    try:
        routed = resolve_brain_kind(getattr(task, "source_type", ""), config.brain)
    except Exception:  # noqa: BLE001 — a routing read must not raise into a request build
        routed = config.brain
    return model_namespace_for_kind(getattr(routed, "kind", None))


def _request_model(task, config, brain) -> str:
    """The model name a task's request carries: its own pin, resolved, or "".

    Wraps the crossing rule so `execute_task`'s request build reads as one
    expression. Within one namespace this is exactly
    ``brain.resolve_model_name(task.model)``, which is what it replaced, so the
    ordinary path is unchanged; across one it re-resolves a portable intent and
    drops a pin that cannot carry, letting the brain apply its own default.

    The dropped pin is deliberately **not** reported to the user here, unlike
    the fallback path's `dropped_pin`. A fallback is a substitution the user did
    not ask for and is worth a note; this is a pin that was never runnable on
    the brain the room or the routing selected, and the note has no action
    behind it — the model surfaces already refuse an id the room's brain cannot
    run (`_known_room_models`), so this is the residue of a room whose brain
    moved out from under a stored pin. It is logged by the rule itself.
    """
    raw = (task.model or "").strip()
    if not raw:
        return ""
    model, _effort, _dropped = _resolve_crossing_model_effort(
        task, config, brain, "", origin_namespace=_pin_origin_namespace(task, config),
    )
    return model


# Prefixed onto a fallback failure text when the fallback was *also* unavailable,
# so the delivery layer can say "both brains are down" instead of echoing a raw
# provider error at the user (ISSUE-212). A marker rather than a formatted
# sentence because the executor's return contract is a plain string — the
# scheduler owns the user-facing wording, and the underlying cause stays in the
# text for the logs and the friendly formatter.
FALLBACK_EXHAUSTED_MARKER = "[brain-fallback-exhausted]"

# The fallback's own stop_reasons that mean "I was unavailable too", as opposed
# to a task-level outcome (timeout / oom / cancelled) where "both unavailable"
# would be the wrong thing to tell the user.
# `not_found` is deliberately absent: it means the fallback brain's binary isn't
# installed. That is an operator misconfiguration, and telling the user to "try
# again shortly" would be false — it will never resolve on its own. It flows
# through the ordinary failure path so the real cause stays visible.
_FALLBACK_UNAVAILABLE_REASONS = frozenset(
    {"usage_limit", "fallback", "transient_api_error"}
)


def _run_fallback(config, brain_config, fallback_kind, task, req, *, on_start=None):
    """Construct the fallback brain and run the same attempt through it.

    Returns ``(BrainResult | None, dropped_pin, effort_used)``. A ``None``
    result means the fallback brain couldn't be constructed (misconfig) — the
    caller keeps the primary's result and flows through the normal path. Never
    raises: an unexpected exception in the fallback brain becomes a failed
    BrainResult.

    ``effort_used`` is returned because the fallback re-resolves model *and*
    effort in its own namespace, so the request's original effort does not
    describe the attempt that ran — recording it on the usage row would name a
    setting the fallback never used.

    ``on_start(model, dropped_pin)`` fires once the request is resolved and
    immediately before the fallback brain runs (ISSUE-278). It exists so the
    user-facing notice lands *in* the silence rather than after it: the fallback
    run is the long part, and a notice emitted on the way out would arrive at
    the end of the same wait it is there to explain. It is called only on the
    path where a fallback actually runs — a construction failure returns above
    it, since no substitution took place to report.
    """
    import dataclasses as _dc

    from .brain import BrainResult

    try:
        fb_config = _dc.replace(brain_config, kind=fallback_kind)
        if fallback_kind == "native":
            fb_config = _dc.replace(
                fb_config,
                native=_native_with_user_key(fb_config.native, config, task.user_id),
            )
        fb_brain = make_brain(fb_config)
    except Exception as e:  # noqa: BLE001 — misconfigured nested block
        logger.warning("brain fallback: could not construct %s: %s", fallback_kind, e)
        return None, None, ""

    # The origin is where the *pin* was written, which on this path is the
    # primary brain — `brain_config.kind` is the routed kind this attempt ran,
    # not `[brain] kind` (ISSUE-417). A move within one namespace is not a
    # crossing and the pin is kept; `claude_code -> tmux_claude` is the case
    # that was dropping a valid id and reporting it to the user.
    fb_model, fb_effort, dropped_pin = _resolve_crossing_model_effort(
        task, config, fb_brain, req.effort,
        origin_namespace=model_namespace_for_kind(brain_config.kind),
    )
    # An advisor pairing can only be right for the model it was resolved
    # against. anthropic->native drops it (mirrors the non-portable-pin drop
    # above — NativeBrain has no wire for it anyway); a dropped_pin also
    # drops it — a non-portable config.model pin means the fallback runs on
    # its own default model instead, and the advisor was never evaluated
    # against that. anthropic->anthropic with the pin intact keeps it, since
    # the same pairing carries over to the fallback too.
    fb_advisor = (
        req.advisor
        if fb_brain.model_namespace == "anthropic" and dropped_pin is None
        else ""
    )
    # `is_fallback` marks the run rather than describing the reroute's cause,
    # which is why it is set here and not passed in: every path that reaches
    # this function is a fallback run, including the breaker-cooldown one where
    # no primary was called at all. `_ran_fallback` at the call site is set for
    # the same reason and draws the same line (ISSUE-378).
    fb_req = _dc.replace(
        req, model=fb_model, effort=fb_effort, advisor=fb_advisor, is_fallback=True,
    )
    if on_start is not None:
        try:
            on_start(fb_model, dropped_pin)
        except Exception:
            # The notice is cosmetic; the reroute is not. A surface that throws
            # must never cost the user the answer.
            logger.debug("brain fallback notice failed", exc_info=True)
    try:
        return _mark_if_exhausted(fb_brain.execute(fb_req)), dropped_pin, fb_effort
    except Exception as e:  # noqa: BLE001 — brains shouldn't raise, but be safe
        logger.exception("brain fallback: fallback brain %s raised", fallback_kind)
        return (
            BrainResult(
                success=False,
                result_text=f"Fallback execution error: {e}",
                stop_reason="error",
            ),
            dropped_pin,
            fb_effort,
        )


def _mark_if_exhausted(fb_result):
    """Tag a fallback result that failed for *availability* reasons.

    Both brains being unavailable is the one case the user must be told about
    plainly — the alternative is delivering whatever raw provider error the
    fallback produced. A task-level failure (timeout / oom / cancelled) is left
    alone: it isn't an availability problem and the normal wording applies.
    """
    import dataclasses as _dc

    if fb_result.success or fb_result.stop_reason not in _FALLBACK_UNAVAILABLE_REASONS:
        return fb_result
    logger.error(
        "brain fallback: fallback brain also unavailable (reason=%s)",
        fb_result.stop_reason,
    )
    return _dc.replace(
        fb_result,
        result_text=f"{FALLBACK_EXHAUSTED_MARKER} {fb_result.result_text}".strip(),
    )


def _fire_fallback_alert(config, task, primary_kind, fallback_kind, reason, window):
    """One operator alert when the availability breaker opens for a primary.

    ``fallback_kind`` is None on a deployment with no ``[brain] fallback``
    configured, which is a legitimate shape since ISSUE-362. The breaker still
    opens there (the sleep cycle and the shared-block generator read it), so the
    alert still fires — it just can't promise a reroute.

    ``window`` is the seconds actually armed, from the breaker itself, not
    ``fallback_cooldown_seconds``. The two stopped agreeing in ISSUE-374: the
    setting is now a ceiling and a usage limit ends at the quota's reset, so
    reading the config here would page the operator "for 3600s" about a window
    that ends in eleven minutes. This is the one message whose whole job is to
    say when the primary comes back.
    """
    try:
        from . import notifications

        cooldown = int(round(window))
        if fallback_kind is not None:
            what_happens = (
                f"falling back to {fallback_kind} for {cooldown}s. "
                "The primary will be probed again after the cooldown."
            )
        else:
            # Not "tasks pause for the cooldown". `_skip_primary` is gated on a
            # fallback existing, so with none every task still calls the primary
            # and the first one after the provider recovers succeeds. What the
            # cooldown holds back is the direct callers — the sleep cycle and
            # shared-block generation — through `primary_brain_unavailable`.
            what_happens = (
                "no fallback configured, so tasks keep failing until it "
                f"recovers. Background memory and briefing work pauses for "
                f"{cooldown}s."
            )
        notifications.send_notification(
            config,
            task.user_id,
            f"⚠️ {primary_kind} brain unavailable ({reason}) — {what_happens}",
            purpose="alert",
        )
    except Exception:
        logger.debug("brain fallback alert failed", exc_info=True)


@dataclass
class FailoverOutcome:
    """What one availability-failover run produced, for the persist step.

    Every field is read by ``execute_task`` after the call. ``ran_fallback`` is
    not derivable from ``primary_usage_result``: on the breaker-cooldown path
    the fallback runs with no primary call at all, so there is nothing to hold
    and the flag would read false for every task in the window.
    """

    result: "BrainResult"
    # The primary's result, held only when a fallback replaced it, so both
    # attempts' usage can be written from the one call site that has a `conn`.
    primary_usage_result: "BrainResult | None"
    ran_fallback: bool
    # The effort the attempt actually ran at. The fallback re-resolves it in
    # its own namespace, so `req.effort` describes the primary only.
    usage_effort: str
    dropped_pin: "str | None"
    primary_kind: str
    fallback_kind: "str | None"


def _failover_notice(stream, event_writer, reason, *, primary_kind, fallback_kind):
    """A `brain_fallback` emitter bound to `reason`, for `on_start`.

    Both reroute paths hand the same notice to the stream; only the
    reason differs (a fresh primary failure vs. the breaker already
    being open). Returns None when there is no **event writer**, so
    `_run_fallback` skips the hook entirely — the gate is the writer
    rather than the stream, which matters now that the two arrive as
    separate arguments instead of one adapter wrapping one writer. A
    stream with no writer has buffered nothing either (`on_event`
    returns at its own `event_writer is None`), so the skipped settle
    costs nothing.
    """
    if event_writer is None:
        return None

    def _emit(model, dropped_pin):
        # A reroute is a stream boundary exactly like a tool call: what
        # streamed before it came from the brain that just failed, and
        # the fallback streams into these same buffers. Settle them
        # first, or an unflushed primary tail is emitted as the opening
        # of the fallback's answer — one paragraph, under a notice
        # saying the primary failed — and the fallback's own narration
        # gate starts pre-credited with the primary's characters.
        # The settle runs whether or not the notice does, because it is
        # about the daemon's own buffers rather than the sentence: a
        # retry that called the primary and watched it fail again has a
        # tail held here that must not open the fallback's answer.
        if stream is not None and stream.is_stream_surface:
            stream.flush_thinking()
            stream.settle_at_tool_boundary()
        # ISSUE-361: once per turn, not once per failover attempt. The
        # retry ladder re-runs this same task id, and every attempt
        # after the breaker opens takes the cooldown path, so the
        # per-attempt emit stacked the banner three deep under one user
        # message. The first is what survives; `emit_once` is where the
        # cases that costs something are written down.
        event_writer.emit_once("brain_fallback", {
            "primary": primary_kind,
            "reason": reason,
            "fallback": fallback_kind,
            "model": model,
            "dropped_pin": dropped_pin or "",
            "text": fallback_notice_text(
                primary_kind, reason, fallback_kind, model, dropped_pin,
            ),
        })

    return _emit


def run_with_failover(
    brain,
    req,
    *,
    config,
    brain_config,
    task,
    stream,
    event_writer,
) -> FailoverOutcome:
    """Run one attempt through the primary brain, rerouting when it is down.

    Generalizes the old tmux->claude_code in-attempt fallback: when the primary
    brain is unavailable (usage limit / missing binary / tmux launch failure)
    and a fallback is configured, re-run this same attempt through the fallback
    brain — no new DB row, no attempt increment. Stickiness: once the primary
    reports a persistent unavailability, subsequent tasks skip it for a
    cooldown. All of it collapses to the plain primary call when no fallback is
    configured.

    The caller owns the ``ExitStack`` around this call: the skill and network
    proxies must be live for the primary call, the reroute and the fallback
    call alike.

    ``brain_config`` is the *identity* source (which brain ran, which one
    catches it) and ``config.brain`` is the *policy* source: the cooldown and
    ``fallback_on_transient`` are read off the latter, as they were when this
    was inline. The two agree today because ``execute_task`` derives
    ``brain_config`` from ``config.brain`` with ``dataclasses.replace``, which
    carries both fields across. A future per-source-type override that varied
    either one would have to change this function, not just its caller.
    """
    _primary_kind = brain_config.kind
    _fallback_kind = effective_fallback_kind(brain_config)
    _cooldown = config.brain.fallback_cooldown_seconds
    _breaker = get_availability_breaker()
    _dropped_pin = None
    _primary_usage_result = None
    _ran_fallback = False
    _usage_effort = req.effort
    _primary_started_at = None
    _primary_started_monotonic = None

    def _notice(reason):
        return _failover_notice(
            stream, event_writer, reason,
            primary_kind=_primary_kind, fallback_kind=_fallback_kind,
        )

    _skip_primary = (
        _fallback_kind is not None
        and _cooldown > 0
        and _breaker.should_skip(_primary_kind, _cooldown)
    )
    if _skip_primary:
        # Cooling down — go straight to the fallback, no primary call.
        logger.info(
            "brain fallback: skipping primary %s (cooling down) "
            "-> %s task=%d",
            _primary_kind, _fallback_kind, task.id,
        )
        _fb, _dropped_pin, _fb_effort = _run_fallback(
            config, brain_config, _fallback_kind, task, req,
            on_start=_notice("cooldown"),
        )
        if _fb is not None:
            # This branch is the steady state once the breaker
            # opens — every task for the cooldown window takes it —
            # so flagging the row here is what keeps the *majority*
            # of genuinely-fallback rows from being labelled
            # otherwise. There is no primary row: the primary was
            # never called. When construction failed instead, the
            # primary really did run below and the flag stays off.
            _ran_fallback = True
            _usage_effort = _fb_effort
        brain_result = _fb if _fb is not None else brain.execute(req)
    else:
        _primary_started_at = time.time()
        _primary_started_monotonic = time.monotonic()
        brain_result = brain.execute(req)
        _triggers = set(TRIGGER_STOP_REASONS)
        if config.brain.fallback_on_transient:
            _triggers.add("transient_api_error")
        if brain_result.stop_reason in _triggers:
            # Open the availability breaker only for persistent
            # conditions (usage_limit / not_found). "fallback" is
            # excluded so tmux keeps being probed per-task (its own
            # launch _CircuitBreaker governs when to stop).
            #
            # Deliberately not gated on a fallback being configured
            # (ISSUE-362). The breaker is a shared signal: the
            # direct callers (sleep cycle, shared blocks) read it
            # through `primary_brain_unavailable`, and
            # `report_brain_result` already opens it for them with
            # no regard to a fallback. Gating it here left a
            # deployment with no fallback without the availability
            # record and without either operator alert, so the only
            # notice that a primary had gone down was the failed
            # task itself. Safe for the task path: `_skip_primary`
            # is separately gated on a fallback existing, so an open
            # breaker never skips a primary there is nothing to
            # replace.
            #
            # The window ends at the quota's reset where one is
            # known, not a flat `_cooldown` from the failure
            # (ISSUE-374). `open_primary_breaker` owns that and
            # publishes the same deadline it armed, so the
            # scheduler's breaker and the record the web process
            # reads describe one window.
            _armed_window = (
                open_primary_breaker(
                    _primary_kind,
                    _cooldown,
                    brain_result.stop_reason,
                    config=config,
                )
                if brain_result.stop_reason in COOLDOWN_STOP_REASONS
                else None
            )
            if _armed_window is not None:
                _fire_fallback_alert(
                    config, task, _primary_kind, _fallback_kind,
                    brain_result.stop_reason, _armed_window,
                )
            if _fallback_kind is not None:
                logger.error(
                    "brain fallback: task=%d primary=%s reason=%s "
                    "-> %s",
                    task.id, _primary_kind,
                    brain_result.stop_reason, _fallback_kind,
                )
            else:
                logger.error(
                    "brain unavailable: task=%d primary=%s "
                    "reason=%s, no [brain] fallback configured",
                    task.id, _primary_kind,
                    brain_result.stop_reason,
                )
            # Preserve tmux's own launch alert: its _CircuitBreaker
            # governs fallback/not_found (which are NOT in the
            # availability breaker's cooldown set), so its
            # 5-consecutive-launch-failure alert still routes here.
            if _primary_kind == "tmux_claude":
                try:
                    from .brain.tmux_claude import (
                        consume_circuit_open_alert,
                    )
                    if consume_circuit_open_alert():
                        from . import notifications
                        _tail = (
                            "falling back."
                            if _fallback_kind is not None
                            else "no fallback configured, so tasks "
                                 "keep failing."
                        )
                        notifications.send_notification(
                            config, task.user_id,
                            "⚠️ tmux_claude brain circuit opened — "
                            f"{_tail} Check the claude CLI "
                            "version / readiness markers.",
                            purpose="alert",
                        )
                except Exception:
                    logger.debug(
                        "tmux circuit-open alert failed", exc_info=True
                    )
            _fb = None
            if _fallback_kind is not None:
                _fb, _dropped_pin, _fb_effort = _run_fallback(
                    config, brain_config, _fallback_kind, task,
                    req, on_start=_notice(brain_result.stop_reason),
                )
            if _fb is not None:
                # The fallback *replaces* brain_result, so without
                # this the single persist call below would record the
                # fallback's numbers under the primary's identity and
                # the primary's own spend would be unrecoverable. It
                # is captured rather than written here because
                # `_run_fallback` takes no `conn`: opening a second
                # one would block on the write lock for the full 30s
                # busy timeout whenever `execute_task` was entered
                # with an open write transaction, as the interactive
                # path does.
                _primary_usage_result = brain_result
                _ran_fallback = True
                _usage_effort = _fb_effort
                brain_result = _fb
        elif brain_result.success and _cooldown > 0:
            # Primary healthy again → close the breaker.
            _breaker.record_success(
                _primary_kind, started_at=_primary_started_monotonic,
            )
            from .brain_availability import clear_unavailable

            clear_unavailable(
                config, _primary_kind, started_at=_primary_started_at,
            )

    return FailoverOutcome(
        result=brain_result,
        primary_usage_result=_primary_usage_result,
        ran_fallback=_ran_fallback,
        usage_effort=_usage_effort,
        dropped_pin=_dropped_pin,
        primary_kind=_primary_kind,
        fallback_kind=_fallback_kind,
    )


def _append_model_note(result_text, dropped_pin, primary_kind, actual_model):
    """Append a single italic note when a non-portable pin was dropped on a
    successful fallback, so the user isn't silently given a different model.

    Pure string→string (no I/O); part of ``result_text`` so it delivers
    uniformly across every surface and persists with the result.
    """
    model_str = actual_model or "a different model"
    # Italicize only the prose runs — the emoji and the `code` spans stay
    # outside emphasis so they render upright (a single wrapping `_…_` would
    # inherit italics onto the emoji and the model IDs). Asterisk emphasis
    # (not underscore) because `primary_kind`/model IDs can contain `_`
    # (e.g. `claude_code`), which would confuse underscore delimiters.
    note = f"⚠️ *Ran on* `{model_str}` *(*`{dropped_pin}` *unavailable).*"
    return f"{result_text}\n\n{note}"


def unread_images(images, trace_json: "str | None") -> list[str]:
    """Prepared images with no recorded `Read` call, by basename.

    The Claude Code brains deliver an image by telling the model to open it,
    which means the vision claim otherwise rests entirely on the model
    complying with a prompt instruction — the failure shape ISSUE-366 records,
    moved one layer up. This reads a *recorded tool call* instead. It is not
    the post-hoc answer grading rejected in the spec: a `ToolUseEvent` is a fact
    about what the model did, not an inference from what it wrote.

    Matched on `READ_DESCRIPTION_PREFIX`, which only `Read` produces — a Bash
    call carries its own emoji, so the model cannot forge this label by naming a
    command.

    **What it detects is a `Read` that was never attempted, and nothing more.**
    Two limits, both in the same direction, so a name returned here is reliable
    and an empty result is not proof of sight:

    * the trace entry is written from a `ToolUseEvent`, which is emitted from
      the assistant message's `tool_use` block — at call time, before any tool
      result. A `Read` that *failed* (a size ceiling, a path outside the
      namespace) is recorded exactly like one that succeeded. The spec settles
      that case elsewhere: the tool result reaches the same model and the
      directive requires it to report the failure rather than guess around it.
    * the trace records the basename rather than the path, so a `Read` of any
      unrelated file sharing a basename with a prepared image — a `screenshot.png`
      in a repository the task is working in, say — satisfies the check.

    An unparseable or absent trace yields no names for the same reason: silence
    is not evidence that an image went unopened.
    """
    if not images or not trace_json:
        return []
    try:
        entries = json.loads(trace_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    read = {
        str(entry.get("text", ""))[len(READ_DESCRIPTION_PREFIX):]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "tool"
        and str(entry.get("text", "")).startswith(READ_DESCRIPTION_PREFIX)
    }
    missing = []
    for image in images:
        if Path(image.path).name not in read:
            missing.append(image.display_name or Path(image.path).name)
    return missing


def _append_unread_images_note(result_text: str, names: list[str]) -> str:
    """Say plainly which images the model never opened.

    Same mechanism as `_append_model_note`: part of ``result_text``, so it
    delivers uniformly across every surface and persists with the result. Pure
    string→string.
    """
    listed = ", ".join(f"`{n}`" for n in names)
    note = (
        f"⚠️ *The image* {listed} *was never opened during this task, so the "
        f"answer above was not informed by it.*"
        if len(names) == 1
        else f"⚠️ *These images were never opened during this task, so the "
             f"answer above was not informed by them:* {listed}*.*"
    )
    return f"{result_text}\n\n{note}"


def _append_vision_dropped_note(
    result_text: str, names: list[str], model: str, *, rerouted: bool = False
) -> str:
    """Say that the answer came from a model that could not see the images.

    A prompt-side notice tells the *model*; only the user reads the result.
    Without this, an answer written blind arrives with nothing anywhere saying
    so — which is the same confident-about-an-unseen-image failure the change
    exists to remove, delivered to the person rather than by the model.

    `rerouted` only changes the wording. A fallback is worth naming because the
    user did not choose the model that answered; a primary is the configured
    one and saying "rerouted" there would be false.
    """
    listed = ", ".join(f"`{n}`" for n in names)
    model_str = model or "the model that answered"
    how = "*rerouted to*" if rerouted else "*ran on*"
    return (
        f"{result_text}\n\n⚠️ *Answered without seeing* {listed} — "
        f"{how} `{model_str}`*, which declares no vision support.*"
    )


def brain_delivers_vision(kind: str, model: str) -> bool | None:
    """Whether a brain of `kind` would give `model` the pixels, or None.

    `None` is "cannot tell", and the callers treat it as no note rather than as
    a negative — claiming an answer was written blind when it may not have been
    is the same class of false statement this whole change removes.
    """
    if kind in ("claude_code", "tmux_claude"):
        # Delivery is Claude Code's own `Read`, available on any model the CLI
        # runs. Whether the model actually called it is what `unread_images`
        # answers, and that is a different question.
        return True
    if kind == "native":
        try:
            from .llm.catalog import get_model_info

            return bool(get_model_info(model).supports_vision)
        except Exception:
            return None
    return None


# Plain-language readings of the stop_reasons that open a fallback, for the
# user-facing notice. `cooldown` is not a brain stop_reason — it's the executor's
# own name for the breaker-open path, where no primary call was made at all.
_FALLBACK_REASON_PHRASES = {
    "usage_limit": "overheated when it hit its usage limit",
    "not_found": "is unavailable because I can't find its CLI",
    "transient_api_error": "is unavailable because its provider returned an error",
    "fallback": "is unavailable because it couldn't start",
}


def fallback_notice_text(primary_kind, reason, fallback_kind, model, dropped_pin) -> str:
    """The sentence every stream surface shows when a fallback is taken.

    Composed here, not per surface: the web transcript and the REPL render
    ``payload["text"]`` verbatim, so the wording lives in one place and two
    surfaces can't drift apart. Pure string→string, no I/O.

    ``model`` is what the fallback was *asked* for, which is empty whenever it
    runs on its own default — the case ``dropped_pin`` describes. The notice
    names the pin then rather than inventing a model name, because the model
    that actually ran is not known until the run returns (the terminal ``done``
    event carries it).
    """
    if reason == "cooldown":
        lead = f"My `{primary_kind}` brain is still cooling down after a recent failure."
    else:
        phrase = _FALLBACK_REASON_PHRASES.get(reason)
        if phrase:
            lead = f"My `{primary_kind}` brain {phrase}."
        else:
            lead = f"My `{primary_kind}` brain is unavailable — {reason}."
    if dropped_pin:
        backup = (
            f"I'm using my `{fallback_kind}` backup. It can't use the pinned "
            f"`{dropped_pin}`, so its default model is running instead."
        )
    elif model:
        backup = f"I'm using my `{fallback_kind}` backup with `{model}`."
    else:
        backup = f"I'm using my `{fallback_kind}` backup."
    return f"{lead} {backup} I might say weird stuff, but I'm doing my best."


def _build_native_completer(native_config, timeout: float, *, on_usage=None):
    """A `prompt -> raw_output | None` one-shot completer over the native provider.

    Conversation-context triage on a native deployment, so the native brain runs
    it through its own provider/model instead of shelling out to the `claude`
    CLI it isn't using.

    Returns None if the provider can't be built (e.g. missing key / bad config),
    so the caller skips the brain-aware path rather than mis-routing to the CLI.

    ``on_usage`` (ISSUE-272) receives what the turn spent, in the same shared
    vocabulary the CLI path reports. Without it this path returned only
    ``AssistantMessage.text`` and dropped the ``usage`` sitting on the same
    object — so a native deployment's triage was as unmeasured as a
    claude_code one, for a different reason. Reported on a failed turn too:
    a turn that reached the provider and then errored still spent tokens.
    """
    try:
        from istota.llm import make_provider
        from istota.llm.oneshot import make_message_completer

        provider = make_provider(native_config)
        # Generous output budget: a JSON id array is short, but reasoning
        # models burn tokens thinking first and would otherwise return empty.
        completer = make_message_completer(
            provider, native_config.model, max_tokens=4096
        )
    except Exception:
        logger.warning(
            "native triage completer setup failed; skipping brain-aware triage",
            exc_info=True,
        )
        return None

    def _classify(prompt: str) -> str | None:
        message = completer(prompt, timeout=timeout)
        if message is None:
            return None
        if on_usage is not None:
            _report_native_usage(on_usage, message, native_config.model)
        # An `error` turn carries an error message where the answer would be;
        # returning it would feed prose to a JSON parser. None is the fail-open
        # signal the callers already handle.
        if message.stop_reason == "error":
            return None
        return message.text

    return _classify


def _report_native_usage(on_usage, message, requested_model: str) -> None:
    """Convert one native turn's usage to the shared vocabulary and report it.

    Never raises: telemetry must not turn a working triage into a fail-open one.

    ``cost_reported`` follows the same conservative reading the native brain
    uses — True only when the provider returned a cost of its own. The catalog
    prices an unknown model at zero, so without the distinction a
    direct-Anthropic or local deployment would write a fabricated `0.0` labelled
    as real spend.

    **A turn that measured nothing writes no row.** Every ``StreamError`` site in
    ``llm/openai_compat.py`` builds a fresh ``AssistantMessage`` with a default
    ``Usage()``, so a failed native turn carries zeros rather than what it spent.
    Reporting those would write ``has_totals=1`` rows of pure zero — and every
    token aggregate filters on ``has_totals``, so during a provider outage they
    would arrive in bulk and drag this origin's per-call averages toward zero
    while inflating its measured-call count. Zeros here mean "not measured", not
    "free". This mirrors the CLI half, where `_parse_simple_json_output` returns
    no usage for an unparseable attempt and `_report_triage_usage` returns
    early; without the check the two halves disagree, invisibly, behind one sink.

    A provider-reported cost is kept even at zero tokens: that is the provider
    saying the turn was free, which is a measurement.
    """
    try:
        from istota import usage as usage_types
        from istota.llm.catalog import get_model_info
        from istota.session.usage import TaskUsage

        if message.usage.total_tokens == 0 and message.usage.cost_usd is None:
            return

        model = message.model or requested_model
        accumulated = TaskUsage()
        accumulated.add(message.usage, get_model_info(model))
        on_usage(
            usage_types.from_task_usage(
                accumulated, cost_reported=message.usage.cost_usd is not None
            ),
            model=model,
            brain_kind="native",
            stop_reason=message.stop_reason,
            success=message.stop_reason != "error",
        )
    except Exception:
        logger.warning("native triage usage sink failed", exc_info=True)


def _native_web_fetch_enabled(
    task: "db.Task", config: Config, is_admin: bool = True,
) -> bool:
    """True when this task will actually have the native WebFetch tool.

    Used to fold `untrusted_input` into the eager skill set — the native
    WebFetch tool ingests untrusted web content but, as a core tool, doesn't
    trigger the companion-skill machinery that surfaces that guidance for
    ingest *skills*.

    ``is_admin`` is here because `build_allowed_tools` may scope the tool by it
    and `NativeBrain._build_tools` filters on that list, so where the tool is
    not built the guidance would be prompt weight with nothing behind it. It
    defaults to True — the permissive answer — so a caller that only wants the
    routing question keeps getting it, and the one caller that decides a
    *prompt* passes the task's real value.

    It is read against ``admin_only`` rather than on its own (ISSUE-449). The
    short-circuit that used to lead this function was correct only because the
    tool was withheld from every non-admin; with the gate off by default a
    non-admin's native task carries the tool, and the untrusted-content
    guardrails have to follow it there. The flag is read off ``routed.native``
    beside ``enabled`` — the same object as ``config.brain.native``, since
    ``resolve_brain_kind`` replaces only ``kind`` and ``fallback`` — so the two
    reads cannot drift, and neither can this answer and the one
    `build_allowed_tools` gives from the unrouted config.
    """
    from .brain import resolve_brain_kind

    try:
        routed = resolve_brain_kind(
            task.source_type, config.brain, override=task.brain,
        )
    except Exception:  # noqa: BLE001 — never let routing lookup fail selection
        return False
    if routed.kind != "native":
        return False
    wf = getattr(routed.native, "web_fetch", None)
    if not (wf and wf.enabled):
        return False
    if getattr(wf, "admin_only", False) and not is_admin:
        return False
    return True


def _build_triage_completer(task: "db.Task", config: Config):
    """Conversation-context triage completer, routed through the task's brain.

    Per-source-type brain routing decides the transport:
    - claude_code (and tmux) → None, so context triage uses the `claude` CLI.
    - native → a native provider completer. If it can't be built (missing key /
      bad config), returns a completer that always yields None so triage fails
      open (includes all older messages) instead of shelling out to the `claude`
      CLI the native brain isn't using.

    The completer carries its own usage sink, because it is the object that
    performs the inference on this path (ISSUE-272). The CLI path's sink is
    passed separately — see ``_build_triage_usage_sink``.
    """
    from .brain import resolve_brain_kind

    routed = resolve_brain_kind(task.source_type, config.brain, override=task.brain)
    if routed.kind != "native":
        return None

    native = _native_with_user_key(routed.native, config, task.user_id)
    completer = _build_native_completer(
        native,
        config.conversation.selection_timeout,
        on_usage=_build_triage_usage_sink(task, config),
    )
    if completer is None:
        return lambda _prompt: None
    return completer


def _build_triage_usage_sink(task: "db.Task", config: Config):
    """Record one conversation-context triage inference as a `task_usage` row.

    ``origin="context_triage"``, and **no ``task_id``** — the same shape the
    other task-less origins use. A triage inference is not one of the task's own
    attempts, and a row carrying the id would take an ``attempt_seq`` in that
    task's sequence, which is meant to count brain attempts. ``user_id`` and
    ``source_type`` are available here (unlike the ownerless sleep-cycle pass),
    so the row is still attributable.

    Opens its own short connection (``conn=None``): prompt assembly holds no
    write transaction, so there is no caller connection to reuse.
    """
    def _sink(usage, *, model="", brain_kind="", stop_reason="", success=False):
        persist_brain_usage(
            config, None, usage=usage, origin="context_triage",
            user_id=task.user_id or "", source_type=task.source_type or "",
            brain_kind=brain_kind, model=model,
            stop_reason=stop_reason, success=success,
        )

    return _sink


# Credential-related env var patterns to strip from subprocess environments
_CREDENTIAL_ENV_PATTERNS = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "APP_PASSWORD", "NC_PASS", "PRIVATE_KEY",
})

#: Shell startup controls, stripped by exact name rather than by substring.
#:
#: Cron ``command:`` rows and heartbeat shell commands run under bash now
#: (``shell_exec.shell_argv``) where they used to run under ``/bin/sh``. Bash
#: sources ``$BASH_ENV`` for a *non-interactive* shell and dash sources nothing,
#: so a value inherited from the daemon's own environment would newly execute
#: before every one of those commands. Not reachable by a task or by the model —
#: it needs control of the daemon's environment — but it is a capability the
#: previous interpreter did not have, so it goes.
#:
#: ``SHELLOPTS`` and ``BASHOPTS`` are the same mechanism one step along: bash
#: imports them at startup, before any startup file, and they are readonly
#: thereafter. They name options rather than a file, so neither is an exec
#: inlet — but an inherited ``SHELLOPTS=xtrace`` would echo every expanded
#: command line, credential values the manifest injected included, into the
#: captured output of every cron job, and ``noexec`` would have each one parse
#: its script and run nothing while exiting 0. ``shell_argv``'s ``-o pipefail``
#: cannot undo either. Reachability is low, since bash only exports
#: ``SHELLOPTS`` if it imported it in the first place, so this closes a gap
#: rather than a live bug.
#:
#: **``build_clean_env`` filters by this set and then re-adds ``SHELLOPTS``
#: deliberately**, to a fixed value it owns (``shell_exec.pipefail_env``,
#: ISSUE-321). Strip first, set second: the point is that no *inherited* value
#: survives, not that the variable is absent.
#:
#: ``ENV`` is deliberately **not** here. POSIX shells read it only for
#: *interactive* shells, and bash invoked as ``bash -c`` is not in POSIX mode
#: and reads ``BASH_ENV`` instead — so stripping it would buy nothing and would
#: break an operator whose command reads ``$ENV`` as a deployment name.
#:
#: Exact match, because these go through a substring test above: ``ENV`` as a
#: substring would strip most of the environment.
_SHELL_STARTUP_ENV_VARS = frozenset({"BASH_ENV", "SHELLOPTS", "BASHOPTS"})

_bwrap_checked: bool | None = None

#: Whether this host's bwrap needs ``--unshare-user`` spelled out.
#:
#: Set by ``_bwrap_available`` and read through ``_bwrap_requires_unshare_user``.
#: False until the probe has run and found otherwise, so a host where the plain
#: probe already succeeds is left exactly as it was.
_bwrap_needs_unshare_user: bool = False

#: The mount operations the availability probe performs, which are the ones
#: ``build_bwrap_cmd`` performs unconditionally on every sandbox it builds.
#:
#: **A probe that answers for less than the command it gates is not a probe.**
#: This used to be `--ro-bind / /` alone, which asks only whether the kernel
#: will hand out a mount namespace — and a container can answer yes to that and
#: still refuse `mount("proc")` inside it, because Docker's masked `/proc`
#: entries and read-only `/proc/sys` make the container's procfs not "fully
#: visible" and the kernel then blocks a fresh procfs in a nested user
#: namespace. Measured on the shipped image: with `seccomp:unconfined` alone,
#: `bwrap --unshare-user --ro-bind / / -- true` exits 0 and the same command
#: with these mounts exits 1 at "Can't mount proc on /newroot/proc". A daemon
#: that trusted the narrow probe there would report a working sandbox, set
#: ``ISTOTA_SANDBOXED``, and then fail every task — which is worse than the
#: silent fallback it replaced.
#:
#: `tests/test_sandbox_db_isolation.py` holds this against the real argv, so a
#: mount added to `build_bwrap_cmd`'s unconditional set and not to this one
#: fails in the default suite rather than on somebody's host.
_BWRAP_PROBE_MOUNTS = [
    "--unshare-pid", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
]

#: The whole probe argv: something to be a root, plus the mounts above.
#:
#: The root bind is the probe's own scaffolding and is deliberately *not* in
#: the list above. `build_bwrap_cmd` binds selectively — `/usr`, a handful of
#: `/etc` entries, the task's own workspace — and never `--ro-bind / /`, so a
#: guard that required the real argv to contain the probe's root would be
#: asserting the sandbox is broader than it is.
_BWRAP_PROBE_ARGS = ["--ro-bind", "/", "/", *_BWRAP_PROBE_MOUNTS]


def _release_task_cgroup(task_id: int, path: Path) -> None:
    """Give a task's cgroup back, naming an OOM kill on the way out (A6).

    ``memory.events`` has to be read *before* ``rmdir``, because the counters
    go with the directory. Without this the feature's first visible effect on a
    real host is that builds and test suites which used to pass start failing,
    reported as an opaque killed child with nothing anywhere mentioning a cap —
    a task told it was killed, and an operator with no way to find out why.

    Both limits that can end a task are named, not just memory. A task that
    hits ``pids.max`` gets ``fork: Resource temporarily unavailable`` out of
    whatever it was running, which mentions no cgroup and reads like a broken
    toolchain; ``pids.events``'s ``max`` counter is the only place that says
    otherwise. Neither counter moved before placement worked (ISSUE-285) — the
    cgroup held one sleeping process, so nothing in it ever reached a limit.

    Never raises: this runs from an ``ExitStack`` callback on the task's exit
    path, where an exception would replace the task's real result with this
    one's.
    """
    try:
        events = task_cgroup.read_events(path)
        if events.get("oom_kill"):
            logger.warning(
                "task %d: %d process(es) OOM-killed inside the task's own cgroup "
                "(scheduler.task_memory_max_mb) — the task exceeded its memory "
                "cap; the rest of the host was unaffected",
                task_id, events["oom_kill"],
            )
        pids_events = task_cgroup.read_events(path, "pids.events")
        if pids_events.get("max"):
            logger.warning(
                "task %d: %d fork(s) refused by the task's own cgroup "
                "(scheduler.task_pids_max) — the task hit its process limit and "
                "will have reported it as a fork failure",
                task_id, pids_events["max"],
            )
        task_cgroup.destroy(path)
    except Exception:  # noqa: BLE001
        logger.debug("task %d: cgroup cleanup failed", task_id, exc_info=True)


def _bwrap_available() -> bool:
    """Check if bwrap can create namespaces (cached after first call).

    Returns False on non-Linux, when bwrap is not installed, or where the
    kernel refuses the namespaces bwrap needs.

    **Probed twice, and the second probe is the one that matters as root.**
    bwrap only forces ``CLONE_NEWUSER`` on itself when it is neither setuid nor
    running as uid 0 — so an unprivileged daemon gets a user namespace whether
    or not anybody asked, and a daemon running as *root without CAP_SYS_ADMIN*
    does not. The plain probe then fails at ``unshare(CLONE_NEWNS)`` with
    "Creating new namespace failed: Operation not permitted", the whole sandbox
    is disabled for the process, and every task runs unconfined behind one
    warning line. That is what every task in a Docker deployment was doing:
    measured inside the shipped image, ``bwrap --ro-bind / / -- true`` exits 1
    and ``bwrap --unshare-user --ro-bind / / -- true`` exits 0.

    So a failed plain probe is retried with ``--unshare-user`` spelled out, and
    the answer is remembered — `build_bwrap_cmd` and `_bwrap_supports` both
    have to pass the same flag, or they would build an argv the probe never
    tested. Order matters: the plain probe runs first, so a host where the
    sandbox already worked is answered by exactly the command it was answered
    by before and nothing about it changes.

    Both probes carry `_BWRAP_PROBE_ARGS`, which performs the unconditional
    mount set `build_bwrap_cmd` emits rather than the bare root bind this
    used to ask about. See `_BWRAP_PROBE_MOUNTS` for why the narrower
    question has a wrong answer on a container.
    """
    global _bwrap_checked, _bwrap_needs_unshare_user
    if _bwrap_checked is not None:
        return _bwrap_checked

    import shutil
    import subprocess
    import sys

    if sys.platform != "linux":
        _bwrap_checked = False
        return False

    if shutil.which("bwrap") is None:
        _bwrap_checked = False
        return False

    def _probe(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, timeout=5)

    try:
        plain = _probe(["bwrap", *_BWRAP_PROBE_ARGS, "--", "true"])
        if plain.returncode == 0:
            _bwrap_checked = True
            return True

        explicit = _probe(
            ["bwrap", "--unshare-user", *_BWRAP_PROBE_ARGS, "--", "true"]
        )
        if explicit.returncode == 0:
            _bwrap_needs_unshare_user = True
            _bwrap_checked = True
            logger.info(
                "bwrap needs --unshare-user spelled out on this host (it "
                "declines to unshare the user namespace on its own as uid 0); "
                "adding it. The plain probe said: %s",
                plain.stderr.decode(errors="replace").strip(),
            )
            return True

        _bwrap_checked = False
        logger.warning(
            "Sandbox skipped: bwrap namespace creation failed both without and "
            "with --unshare-user (kernel without unprivileged user namespaces, "
            "or a container blocking the syscall): %s / %s",
            plain.stderr.decode(errors="replace").strip(),
            explicit.stderr.decode(errors="replace").strip(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Sandbox skipped: bwrap probe failed: %s", exc)
        _bwrap_checked = False
    return _bwrap_checked


def _bwrap_requires_unshare_user() -> bool:
    """Whether every bwrap argv on this host has to carry ``--unshare-user``.

    Calls `_bwrap_available` rather than reading the global directly, because
    the global is only meaningful once the probe has run and callers reach this
    from several places that may each be first.
    """
    if not _bwrap_available():
        return False
    return _bwrap_needs_unshare_user


_bwrap_flag_support: dict[str, bool] = {}
_bwrap_probe_lock = threading.Lock()


def effective_sandboxing(config: Config) -> bool:
    """Whether the filesystem sandbox is actually in place for this deployment.

    ``sandbox_enabled`` is what the operator asked for; this is what they got.
    Four shapes run the model with the daemon's own filesystem access: the
    standalone install, which ships ``sandbox_enabled = false``; and — despite
    the flag being set — a container where the bwrap probe fails (no
    CAP_SYS_ADMIN, and that one is multi-user), a Linux host with no bwrap
    installed, and any non-Linux host.

    Named because four call sites need the same answer and three of them used
    to spell it out inline, two under comments calling it "*effective*
    sandboxing" without there being anything of that name to point at. One of
    those sites decides how the prompt describes the database boundary to the
    model, so a definition that drifted between them would have the daemon
    telling the model the databases are masked on a deployment where they are
    not — and the code there is explicit that a false boundary claim is worse
    than no claim at all.

    Consults the bwrap capability probe, which shells out once per process and
    caches. That is why prompt assembly reaches `subprocess` at all (ISSUE-308).

    Not every site that mentions both halves wants this predicate. The
    scheduler's startup warning (`scheduler.py`) reads ``sandbox_enabled and
    not _bwrap_available()`` — "asked for it, didn't get it", which is
    ``sandbox_enabled and not effective_sandboxing(config)`` and not the plain
    negation. Collapsing it to ``not effective_sandboxing(config)`` would fire
    an unsupported-configuration warning on every standalone install, which
    ships the flag off deliberately.
    """
    return bool(config.security.sandbox_enabled and _bwrap_available())


def effective_sandboxing_if_known(config: Config) -> bool | None:
    """`effective_sandboxing`, but only where the answer costs nothing.

    ``None`` means "not established here": the flag is on and the bwrap
    capability probe has not run in this process, so answering would mean
    spawning. Every other case is the same answer `effective_sandboxing`
    gives.

    For a caller that must not spawn — today `doctor`'s
    ``runtime.session_log_dir`` under ``run_checks(probe=False)``, whose whole
    subject is a boundary and which therefore must not report a protection it
    did not verify. It reads the memo rather than declaring ignorance because
    the memo is usually warm where it matters: the daemon probes at start-up in
    `_log_startup_status`, so inside that process the answer is free, and
    saying "not probed" while `_bwrap_checked` holds it would be a statement
    about the world that is wrong.

    Reads the module global directly rather than calling `_bwrap_available`,
    which is the point — that function's contract is to probe when it has no
    cached answer.
    """
    if not config.security.sandbox_enabled:
        # No probe needed and none was ever wanted: the flag alone settles it,
        # exactly as `effective_sandboxing`'s own short-circuit does.
        return False
    if _bwrap_checked is None:
        return None
    return bool(_bwrap_checked)


def _bwrap_supports(flag: str, probe_args: list[str]) -> bool:
    """Whether this bwrap accepts *flag*, probed once per process.

    Probed rather than assumed: passing an unsupported flag makes bwrap exit
    non-zero *before* it runs anything, which would fail every task on an older
    host. ``probe_args`` is the whole argv the flag needs, companion flags
    included — bwrap rejects `--disable-userns` without `--unshare-user`, and a
    probe missing one reports "unsupported" on a host that supports it fine.

    A probe that could not run is logged loudly and a rejection quietly: the
    first is an unexplained loss of hardening, the second is an old bwrap saying
    what it is. Either way the answer is cached, so a failure to probe silently
    turns the flag off for the process — the reason the result is not trusted
    for anything but hardening. Locked because scheduler workers build their
    first sandbox concurrently, and an unlocked probe is N subprocesses and N
    log lines for one question.

    ``--unshare-user`` is prepended where `_bwrap_available` found the host
    needs it, for the same reason that function retries with it: without the
    flag *every* probe here fails at namespace creation rather than at the flag
    under test, so a supported flag reports unsupported. Fixing
    `_bwrap_available` alone would have left that true — the sandbox would have
    started and ``--remount-ro`` would have reported unsupported, leaving the
    database masks writable, which is the one thing the read-only mask exists
    to prevent.

    Through `_bwrap_requires_unshare_user` rather than the global, so the two
    places that have to agree about this flag agree through one accessor.
    """
    with _bwrap_probe_lock:
        cached = _bwrap_flag_support.get(flag)
        if cached is not None:
            return cached
        if not _bwrap_available():
            _bwrap_flag_support[flag] = False
            return False

        if _bwrap_requires_unshare_user() and "--unshare-user" not in probe_args:
            probe_args = ["--unshare-user", *probe_args]

        supported = False
        try:
            result = subprocess.run(
                ["bwrap", *probe_args, "--", "true"],
                capture_output=True, timeout=5,
            )
            supported = result.returncode == 0
            if not supported:
                logger.info(
                    "bwrap rejected %s: %s", flag,
                    result.stderr.decode(errors="replace").strip(),
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "bwrap probe for %s could not run (%s); treating it as "
                "unsupported for the rest of this process", flag, exc,
            )
        _bwrap_flag_support[flag] = supported
        return supported


def _bwrap_supports_disable_userns() -> bool:
    """Whether this bwrap accepts ``--disable-userns`` (added in 0.8).

    Matters because the deployment enables ``kernel.unprivileged_userns_clone``
    — bwrap needs it — which also lets the sandboxed process `unshare -Urm`
    into a nested namespace where it holds CAP_SYS_ADMIN and can `umount` one
    of our masks, revealing whatever was bound underneath. With nothing bound
    under the database directories that reveals nothing, but the masks exist
    precisely to survive a future broad ``sandbox_ro_paths`` entry, and that is
    the case where lifting them would matter. ``--disable-userns`` blocks the
    nested namespace outright.

    Probed rather than assumed: passing an unsupported flag makes bwrap exit
    non-zero, which would fail every task on an older host.

    The probe carries ``--unshare-user`` because bwrap refuses the pair without
    it ("--disable-userns requires --unshare-user", exit 1) — so a probe without
    it answers "unsupported" on every host, which is what it did from the flag's
    introduction until this was found. `build_bwrap_cmd` emits the two together
    for the same reason. Unprivileged bwrap unshares the user namespace anyway,
    so on the supported deployment the companion flag changes nothing on its own.
    """
    already_probed = "--disable-userns" in _bwrap_flag_support
    supported = _bwrap_supports(
        "--disable-userns",
        ["--unshare-user", "--ro-bind", "/", "/", "--disable-userns"],
    )
    if not supported and not already_probed and _bwrap_available():
        logger.info(
            "bwrap does not support --disable-userns; sandbox masks can be "
            "lifted from a nested user namespace. Keep sandbox_ro_paths narrow."
        )
    return supported


def _bwrap_supports_remount_ro() -> bool:
    """Whether this bwrap accepts ``--remount-ro`` (added in 0.2).

    The database masks are read-only so that a probe against a path that is no
    longer in the namespace fails at open time instead of quietly creating a
    zero-byte file on the mask's tmpfs — which then answers `no such table` and
    reads as a corrupt database rather than as a boundary, and litters the
    directory for the rest of the task.

    Old enough that every supported host has it, but probed all the same: the
    cost of being wrong is every task failing, against a cosmetic gain.
    """
    already_probed = "--remount-ro" in _bwrap_flag_support
    supported = _bwrap_supports(
        "--remount-ro",
        ["--ro-bind", "/", "/", "--tmpfs", "/tmp", "--remount-ro", "/tmp"],
    )
    if not supported and not already_probed and _bwrap_available():
        logger.info(
            "bwrap does not support --remount-ro; the database masks stay "
            "writable and a stray file written there will look like a database."
        )
    return supported


def build_clean_env(config: Config) -> dict[str, str]:
    """Build minimal environment for Claude subprocess.

    Returns a restricted env (PATH, HOME, PYTHONUNBUFFERED) plus any
    configured passthrough vars. Credentials are injected per-task by
    execute_task() and optionally routed through the skill proxy.
    """
    # Ensure the active Python venv bin dir is on PATH so skills can run
    # as `python -m istota.skills.*` inside the sandbox. Use sys.prefix
    # (not sys.executable) to get the venv root — sys.executable resolves
    # through symlinks to the system python binary.
    venv_bin = str(Path(sys.prefix).resolve() / "bin")
    base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if venv_bin not in base_path.split(os.pathsep):
        base_path = f"{venv_bin}{os.pathsep}{base_path}"
    env = {
        "PATH": base_path,
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONUNBUFFERED": "1",
    }
    # USER/LOGNAME are process-identity basics, not secrets. The macOS login
    # Keychain lookup the `claude` CLI uses to find its OAuth credential needs
    # them — without them a stripped-env `claude -p` reports "Not logged in"
    # even though the interactive CLI is authenticated (the standalone/local
    # install's default brain). Harmless on Linux, where the credential is a
    # file under HOME.
    for identity_key in ("USER", "LOGNAME"):
        identity_val = os.environ.get(identity_key)
        if identity_val is not None:
            env[identity_key] = identity_val
    for key in config.security.passthrough_env_vars:
        # A shell-startup control is never passed through, whatever the
        # operator listed. This env is otherwise an allowlist built from
        # scratch, so the passthrough loop was the one way an inherited
        # `BASH_ENV` could reach a model subprocess — and that is a file bash
        # sources before every command the model runs. `build_stripped_env`
        # has filtered the same set since the interpreter swap; this is the
        # sibling path, which never did (found in review of ISSUE-321, whose
        # own comment below claimed the protection this line supplies).
        if key.upper() in _SHELL_STARTUP_ENV_VARS:
            logger.warning(
                "passthrough_env_vars names %s, which is a shell-startup "
                "control — ignoring it. Remove it from the config.", key,
            )
            continue
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    # Pass through Claude Code auth token if present.
    #
    # Set for every task whatever brain will run it, because two of the three
    # brains authenticate with it. The one that does not takes it back out —
    # `claude_runtime_env.CLAUDE_RUNTIME_ENV_VARS` names it, `NativeBrain`
    # applies that on the way into its tools, and `execute_task` applies it to
    # `proxy_base_env` (ISSUE-390). A second variable hand-set here belongs in
    # that set too, unless a model-facing subprocess has business reading it.
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    # Propagate the admins-file path (a path, not a secret) so subprocess
    # config loads — the feeds/money skill facades call load_config() —
    # resolve the namespace-correct admins file instead of the hardcoded
    # /etc/istota default. Unset on non-namespace deploys; harmless to omit.
    admins_file = os.environ.get("ISTOTA_ADMINS_FILE")
    if admins_file:
        env["ISTOTA_ADMINS_FILE"] = admins_file
    # Propagate the config file actually loaded so subprocess load_config()
    # calls — the on-demand `skills` loader re-applies disabled/admin/
    # experimental guards from a fresh load_config(), and the feeds/money
    # facades load config too — resolve the SAME file the daemon used rather
    # than falling back to the default search order. Without this a daemon
    # started with `-c /custom/path` would re-apply guards from a different
    # config than the one that built the catalogue. Mirrors the scheduler's
    # command/skill-task env builders.
    if config.config_path is not None:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    # `pipefail` on for every bash below this process (ISSUE-321).
    #
    # Here rather than in the brains because this is the env every model
    # subprocess is built from, and the defect is not one brain's: a
    # `ClaudeCodeBrain` or `TmuxClaudeBrain` task runs its commands through the
    # CLI's own Bash tool, which istota launches and does not instrument, so
    # `shell_exec.shell_argv` — which fixes the shells istota spawns itself —
    # cannot reach it. `NativeBrain` already passes `-o pipefail` on its own
    # argv and gains only depth here, which is what keeps the two brains
    # answering an identical command string identically. That divergence was
    # the stated reason ISSUE-307 left this site alone; it is what closes now.
    #
    # Applied last, so it wins over a `passthrough_env_vars` entry naming
    # SHELLOPTS. That is deliberate: the alternative hands the model's shell
    # whatever the daemon's environment carried, which is usually nothing and
    # silently restores the bug. `set +o pipefail` inside a command remains the
    # per-command escape hatch, and there is no deployment-wide one — a switch
    # to turn a correctness fix back off is not worth the shape it would give
    # the config, and neither ISSUE-307 nor `shell_argv` shipped one either.
    #
    # This env is an allowlist built from scratch and the passthrough loop
    # above refuses the whole shell-startup family, so nothing inherited can
    # arrive carrying `xtrace` and the value here is the only one there is.
    # That loop is what makes the claim true; it did not filter until ISSUE-321
    # was reviewed, and this comment asserted the protection anyway.
    #
    # Reaches the host-side skill CLIs too, via the `proxy_base_env` snapshot
    # in `execute_task` — those run unsandboxed as the daemon user, so anything
    # set here is worth a second thought. This one is a fixed option name with
    # no interpolation and no file behind it, and the CLIs spawn no shell at
    # all (nothing under `src/` passes `shell=True`), so it is inert there.
    env.update(pipefail_env())
    return env


#: What a daemon-side model call needs from the daemon's own environment in
#: order to *reach* the provider, beyond what `build_clean_env` allowlists.
#:
#: Split into three groups because they fail differently, not for tidiness.
#: The proxy triple is tested for **presence**, never truthiness: `NO_PROXY=`
#: is meaningfully empty (it blanks an inherited exemption list), the same
#: reading `tool_server._PROXY_ENV_VARS` takes of the same three names.
_MODEL_CLI_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                         "https_proxy", "http_proxy", "no_proxy")
#: Where the CLI's TLS trust store is, on a deployment terminating TLS at its
#: own proxy. Without these the request fails at the handshake.
#:
#: `CURL_CA_BUNDLE` is here because `forge_cli._CARRY_EXACT` already answers
#: this same question that way, and it is the one of the five that `curl` reads
#: and the only one `requests` falls back to — so an operator who set just that
#: name, which is the common single-variable choice, otherwise got nothing and
#: a handshake error pointing nowhere near here. Widening a list of trust-store
#: *paths* is safe in the one direction that matters: a CA bundle can only add
#: trust, never redirect a request or carry a credential.
_MODEL_CLI_TLS_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
                       "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
#: Where the provider *is*, on a gateway deployment, and how to authenticate
#: to it. `ANTHROPIC_API_KEY` is the long-standing member of this group.
_MODEL_CLI_ENDPOINT_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                            "ANTHROPIC_BASE_URL")


#: The reachability half of ISSUE-410, and the axis it is split on.
#:
#: `build_model_cli_env` tops its allowlist up from `os.environ`, which is the
#: daemon's own for every daemon-side caller and is `proxy_base_env` for the
#: one that is not: `code_review` runs as a subprocess the skill proxy
#: spawned, so the top-up loop was reading the wrong environment and finding
#: nothing. ISSUE-409 fixed the credential names by injecting them into the
#: proxy's per-skill map; these are what was left.
#:
#: They do **not** all go to the same place, and the axis is not "secret or
#: not". It is **whether handing the value to a CLI that did not ask for it
#: can do harm**, which splits the eight names cleanly in two.
#:
#: A **trust store path** cannot. It only ever adds a CA, so a skill that
#: needed it works and a skill that did not is unaffected, and the value is a
#: path rather than a credential. Those are shared with every host-side skill
#: CLI, below.
#:
#: A **proxy URL redirects traffic**, and that is the part the obvious reading
#: misses. It is not merely inert for a CLI with no use for it — it captures
#: that CLI's requests, including the ones aimed at this deployment's own
#: services. `browse` is the live case: its only outbound call is to
#: `BROWSER_API_URL`, which defaults to `http://localhost:9223`, over `httpx`
#: with `trust_env=True` — which honours `HTTP_PROXY` and does not special-case
#: loopback. Handing it the daemon's egress proxy would send that at the
#: proxy, which answers 403 or 405, on a deployment where it works today.
#: `tests/support/env_isolation.py` records exactly this failure already
#: happening here, to nineteen loopback stub servers. And a proxy URL is not
#: reliably a non-secret either: `http://user:pass@egress:3128` is an ordinary
#: shape, `skill_proxy` returns a skill CLI's stderr verbatim to the model,
#: and a connect failure is exactly what echoes the URL.
#:
#: So the proxy triple goes to the skills that call a model and nothing else,
#: through the same per-skill map ISSUE-409 used — which is the structure for
#: a value that must reach exactly one subprocess and stay out of the
#: `credential-fetch` union, and that is what these need. `ANTHROPIC_BASE_URL`
#: travels with them because no other skill reads it and a gateway URL can
#: carry a key in its path.
#:
#: What this leaves undone is deliberate and is **not** what ISSUE-410 filed:
#: `feeds` fetches external URLs and would want an egress proxy. Giving it one
#: means exempting each deployment's own internal endpoints from it, which is
#: a design question about per-skill network policy rather than about the
#: reviewer's environment.
#:
#: Neither half collides with a sandboxed task's CONNECT bridge. That value is
#: set by `sandbox_plan` as `exec env HTTPS_PROXY=…` inside the namespace at
#: exec time, so it enters no env dict — and a host-side skill CLI is outside
#: the namespace, where the daemon's own proxy is the correct answer.
SKILL_CLI_TLS_VARS = _MODEL_CLI_TLS_VARS

#: The half that is scoped rather than shared — see above.
SKILL_MODEL_REACHABILITY_VARS = frozenset(
    (*_MODEL_CLI_PROXY_VARS, "ANTHROPIC_BASE_URL")
)


def _reachability_env(
    names: Iterable[str], *present: Mapping[str, str],
) -> dict[str, str]:
    """Those of ``names`` the daemon's environment has and ``present`` does not.

    A gap-filler, never an override, which is what keeps it from undoing a
    decision made elsewhere. A name an operator listed in
    ``passthrough_env_vars`` is already in the task env and keeps that value,
    matching :func:`build_model_cli_env`'s rule for the same names. A name a
    skill manifest declared ``sensitive`` has been *moved* into the per-skill
    credential map by ``_split_credential_env``, so re-reading it from the
    daemon here would put it back in front of every host-side CLI instead of
    the one that declared it.

    That second case has a cost worth naming, because it is not the scoping it
    looks like: ``derive_credential_set`` is index-wide, so **one** manifest
    declaring ``SSL_CERT_FILE`` sensitive takes it out of `browse`, `feeds` and
    `code_review` at once and leaves it only for the skills that declared it.
    Declining to re-add it is right — the alternative silently overrules the
    manifest — but a reader adding such an entry should expect that reach. No
    shipped manifest names one of these today.

    Presence, not truthiness, for the reason :data:`_MODEL_CLI_PROXY_VARS`
    gives: ``NO_PROXY=`` is meaningfully empty, and dropping it restores an
    inherited exemption list rather than changing nothing.
    """
    return {
        name: os.environ[name]
        for name in names
        if name in os.environ
        and not any(name in mapping for mapping in present)
    }


def skill_cli_tls_env(*present: Mapping[str, str]) -> dict[str, str]:
    """:data:`SKILL_CLI_TLS_VARS` for the shared ``proxy_base_env`` snapshot."""
    return _reachability_env(SKILL_CLI_TLS_VARS, *present)


def skill_model_reachability(*present: Mapping[str, str]) -> dict[str, str]:
    """:data:`SKILL_MODEL_REACHABILITY_VARS`, for :data:`SKILL_MODEL_CALLERS`.

    The counterpart to :func:`skill_model_credentials`, and separate from it
    for two reasons rather than one. That function drops an empty value,
    because an empty credential is something a CLI would try to authenticate
    with and cannot, whereas here an empty value is meaningful — ``NO_PROXY=``
    blanks an inherited exemption list — so this reads presence. And it reads
    the daemon's own environment rather than a list of sources, because
    ``build_clean_env`` is an allowlist that carries none of these names, so
    the daemon's is the only environment they are ever in.
    """
    return _reachability_env(SKILL_MODEL_REACHABILITY_VARS, *present)


#: Skills whose CLI is itself a model caller.
#:
#: `code_review` spawns the `claude` binary per reviewer — `make_brain` inside
#: a skill CLI, a shape no other skill has (nothing else under `src/istota/
#: skills/` imports a brain). It is the exception `proxy_base_env`'s strip has
#: to make: ISSUE-390 reasoned that "no skill invokes the `claude` binary and
#: none reads the variable" and took `CLAUDE_CODE_OAUTH_TOKEN` out of the
#: snapshot every host-side skill CLI runs with, and from that change every
#: review on a subscription-authenticated deployment came back `skipped /
#: review_failed` about a second after it started — the shape of a `claude -p`
#: that exits on "Not logged in" before opening a socket (ISSUE-409).
#:
#: A name list rather than a manifest flag, because both manifest routes are
#: worse. Declaring the token `sensitive` in `skill.md` puts it in
#: `derive_credential_set`, which is index-wide and drives the split that
#: builds the *model's* env — so the task's own `ClaudeCodeBrain` would lose
#: the credential in order to give it to the skill. A `daemon_env` manifest
#: source would let any skill name any variable in the daemon's environment as
#: its own credential, which is a much wider door than one reader needs.
SKILL_MODEL_CALLERS = frozenset({"code_review"})

#: What such a CLI needs in order to *authenticate*, as opposed to reach.
#:
#: `build_model_cli_env` runs inside the skill subprocess, so the `os.environ`
#: it reads is `proxy_base_env` plus whatever the proxy injected — not the
#: daemon's own. The two API-key names are in that env by no route at all
#: (`build_clean_env` is an allowlist and does not carry them), so this was
#: broken on an API-key deployment before ISSUE-390 broke it on a subscription
#: one; same failure, one shape earlier (ISSUE-409). Naming all three keeps
#: the fix from being true only of the deployment it was found on.
#:
#: Deliberately not the rest of `_MODEL_CLI_*`: a base URL, a CA bundle and an
#: outbound proxy are *reachability*, which ISSUE-410 handled separately —
#: :data:`SKILL_CLI_TLS_VARS` shared with every host-side CLI, and
#: :data:`SKILL_MODEL_REACHABILITY_VARS` scoped here beside these. This set is
#: what a CLI needs to *authenticate*, and it stays a set of its own because
#: the empty-value rule differs: an empty credential is not a credential,
#: while an empty `NO_PROXY` is meaningful.
SKILL_MODEL_CREDENTIAL_VARS = CLAUDE_RUNTIME_ENV_VARS | frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
)


def skill_model_credentials(*sources: Mapping[str, str]) -> dict[str, str]:
    """:data:`SKILL_MODEL_CREDENTIAL_VARS`, read from the first source that has
    each one.

    A *copy* out of its sources, never a split, and that is the shape of the
    whole fix. Everything else in the proxy's `credential_env` got there by
    `_split_credential_env`, which moves a name out of the model's environment
    and into the proxy's; the Claude token has to end up in both, because
    `ClaudeCodeBrain` and `TmuxClaudeBrain` authenticate the task's own brain
    with it. Taking it out of the model's env here would unauthenticate the
    task in order to fix the skill.

    An absent or empty value yields no entry, so a deployment configured with
    one of these injects only that one, and a deployment with none injects
    nothing rather than an empty string a CLI would read as a credential.
    """
    out: dict[str, str] = {}
    for name in SKILL_MODEL_CREDENTIAL_VARS:
        for source in sources:
            if source.get(name):
                out[name] = source[name]
                break
    return out


def build_model_cli_env(config: Config) -> dict[str, str]:
    """Build the env for a daemon-side model call that is not a task.

    Used by every call that reaches a model outside `execute_task` — the
    `!check` / self-check execution test and conversation-context triage,
    which spawn or wrap the `claude` CLI directly, and the modules that build a
    `BrainRequest` of their own (the three OCR extractors, the biomarker
    explainer, the nightly sleep cycle, shared briefing blocks and the
    code-review agents — seven, where this sentence said six until the roster
    was counted). The rule rather than the roster: if it sends a prompt to a
    model and there is no task behind it, its env comes from here.

    It is `build_clean_env`'s allowlist plus the names below. Everything else
    in the daemon environment — the master Fernet key, the Nextcloud app
    password, every configured service token — stays out.

    **The extra names are a regression fix, not a widening** (ISSUE-395).
    These calls all run in the daemon's own network namespace: they build no
    `--unshare-net` and no CONNECT bridge, so unlike a task they get no
    `HTTPS_PROXY` pointing at one. They used to pass `dict(os.environ)` and so
    inherited the daemon's proxy, CA-bundle and gateway settings by accident.
    Narrowing the env without carrying those forward would have left a
    proxy-only or gateway deployment unable to reach the provider at all, from
    six call sites at once, with the failure surfacing as a connect error
    nowhere near this function.

    An operator's own `security.passthrough_env_vars` entry wins: those are
    applied by `build_clean_env` and are only filled in here when absent.
    """
    env = build_clean_env(config)
    for name in (*_MODEL_CLI_PROXY_VARS, *_MODEL_CLI_TLS_VARS,
                 *_MODEL_CLI_ENDPOINT_VARS):
        # Presence, not truthiness — see `_MODEL_CLI_PROXY_VARS`.
        if name not in env and name in os.environ:
            env[name] = os.environ[name]
    return env


def build_stripped_env() -> dict[str, str]:
    """Build os.environ minus credential vars. For heartbeat/cron commands.

    Phase 1.4 of the unified credential resolution refactor: the master
    Fernet key (``ISTOTA_SECRET_KEY``) is no longer preserved here. Skill
    subprocesses that need per-user encrypted secrets get them
    pre-resolved via the manifest ``env:`` blocks.

    ``PRECOMMIT_SCANS_REQUIRED`` is added rather than filtered (ISSUE-291).
    The repository's pre-commit hook refuses a commit whose secret scan could
    not run, but only where it can tell nobody is watching, and the markers it
    reads for that — ``ISTOTA_SANDBOXED``, ``DEVELOPER_REPOS_DIR`` — are built
    per task by ``build_claude_env``. This env is the daemon's own, so a cron
    ``command`` job or a heartbeat shell command carries neither and would be
    read as a human at a terminal. It is as unattended as any of them, so it
    says so.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not any(p in k.upper() for p in _CREDENTIAL_ENV_PATTERNS)
        and k.upper() not in _SHELL_STARTUP_ENV_VARS
    }
    env["PRECOMMIT_SCANS_REQUIRED"] = "1"
    return env


# Defense-in-depth: instance-wide credentials that must never be returned
# by the proxy's credential-lookup endpoint, even if a buggy or hostile
# setup_env hook accidentally injects them into the credential env.
#
# After Phase 1.4 the master Fernet key never enters any subprocess env;
# manifests can only declare per-user secrets, and the trusted-side
# resolver returns plaintext values. This frozenset closes the residual
# hole of a setup_env hook doing
# ``env["ISTOTA_SECRET_KEY"] = os.environ["ISTOTA_SECRET_KEY"]``.
# ``derive_lookup_allowlist`` subtracts this set from its return value so
# ``credential-fetch ISTOTA_SECRET_KEY`` is rejected by the proxy even if
# the var sneaks into ``credential_env``.
#
# ``SKILL_MODEL_CREDENTIAL_VARS`` joins it for the same reason from the other
# direction: `task_env` puts those in ``credential_env`` on purpose, so the
# proxy can inject them into the one skill CLI that calls a model. Injection
# is scoped to that skill; the lookup endpoint is not scoped to anything — its
# allowlist is a union, and the socket is bound into the sandbox, so a name in
# it is a name the model can ask for by hand. That would put the credential
# back on the route ISSUE-390 closed, through a different door.
_PROXY_LOOKUP_BLOCKED = (
    frozenset({"ISTOTA_SECRET_KEY"})
    | SKILL_MODEL_CREDENTIAL_VARS
    # The proxy triple and the gateway URL are injected per skill for the same
    # reason the credentials are, so they need the same second door shut: an
    # outbound proxy URL can carry basic-auth userinfo, and the lookup
    # allowlist is a union anything holding the socket can fetch by name.
    | SKILL_MODEL_REACHABILITY_VARS
)


# Reserved key a setup_env hook may return to prepend entries to the *model's*
# PATH. os.pathsep-separated. Consumed and dropped by execute_task; never
# merged into ``env`` and never handed to the skill proxy — see the
# application site for why that distinction is load-bearing.
HOOK_PATH_PREPEND_KEY = "ISTOTA_PATH_PREPEND"


# --- Network proxy allowlist ---

_DEFAULT_NETWORK_HOSTS = frozenset({
    "api.anthropic.com:443",
    "mcp-proxy.anthropic.com:443",
})

_PYPI_HOSTS = frozenset({
    "pypi.org:443",
    "files.pythonhosted.org:443",
})

# Package registries reached by an install inside a developer worktree
# (ISSUE-304). Gated on the developer skill rather than a config flag of their
# own: the registries arrive with the skill, which is already opt-in through
# ``developer.enabled``.
#
# **Authorized, not selected**, and the difference is worth stating because the
# two read alike. ``authorized_skills`` is the union of the selected skills and
# the ones ``derive_authorized_skills`` auto-authorizes on credential presence,
# and ``developer`` auto-authorizes as soon as *either* forge token resolves.
# So on a deployment where the user has configured a GitLab or GitHub token,
# these hosts are on the allowlist for every one of that user's tasks — a Talk
# reply, a cron job, a briefing — and not only for tasks that chose the skill.
# That is the same gate the forge hosts have always ridden, deliberately (the
# symmetry `derive_authorized_skills` exists for), so this adds reach to an
# existing door rather than opening a new one. It is a door onto a registry
# anyone may publish to, which is the argument this file uses below to refuse
# ``*.blob.core.windows.net`` — the difference is that a package registry is
# what an install *is*, and ``allow_pypi`` already concedes the same property
# deployment-wide and on by default.
#
# Every hostname here was measured through a logging CONNECT proxy that
# permitted everything and recorded each target, the same method that
# established the GitHub Actions log host above. A guessed name fails silently
# at the boundary and reads as a broken install, because the proxy matches
# ``host:port`` exactly and supports no wildcards.
#
# npm: a complete `npm ci` of this repo's own web/package-lock.json — 213
# packages — made 15 CONNECTs, all to this one host. Metadata and tarballs
# share it.
#
# cargo: `cargo fetch` on serde and its transitive dependencies contacted the
# sparse index and the download host. `crates.io` itself is the API — publish,
# search, yank — and was never contacted, so it is not here.
#
# PyPI is absent deliberately: it is global (``allow_pypi``) rather than
# developer-gated, because ad-hoc Python runs in every task and not only in
# development ones.
_REGISTRY_HOSTS = frozenset({
    "registry.npmjs.org:443",
    "index.crates.io:443",
    "static.crates.io:443",
})


def _build_network_allowlist(
    config: Config,
    authorized_skills: list[str],
) -> set[str]:
    """Build per-task network allowlist from config and authorized skills.

    Phase 3: keyed on ``authorized_skills`` (the union of selected skills
    and skills auto-authorized via credential presence) so a user with
    GitLab tokens configured can reach gitlab.com even when ``developer``
    wasn't selected — symmetric with credential authorization.
    """
    hosts: set[str] = set(_DEFAULT_NETWORK_HOSTS)

    if config.security.network.allow_pypi:
        hosts |= _PYPI_HOSTS

    hosts.update(config.security.network.extra_hosts)

    # Developer skill: add git remote hosts from config
    if "developer" in authorized_skills and config.developer.enabled:
        from urllib.parse import urlparse

        # Package registries. Independent of the forge URLs below — a
        # deployment with neither configured still installs dependencies.
        hosts |= _REGISTRY_HOSTS

        for url in [config.developer.gitlab_url, config.developer.github_url]:
            if url:
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or 443
                if host:
                    hosts.add(f"{host}:{port}")

        # GitHub API lives on a separate host from github.com
        if config.developer.github_url:
            parsed = urlparse(config.developer.github_url)
            if parsed.hostname and "github.com" in parsed.hostname:
                hosts.add("api.github.com:443")
                # `gh run view --log-failed` — the CI feedback loop — fetches
                # job logs from a second host. Measured against gh 2.98 through
                # a logging CONNECT proxy: one stable hostname, the same across
                # independent runs, so an exact entry is enough and the proxy
                # needs no wildcard support.
                #
                # `gh run download` is deliberately NOT covered. Artifacts come
                # from productionresultssa<N>.blob.core.windows.net, where the
                # shard varies (4 and 7 observed for one repository), and the
                # only entry that would cover it is *.blob.core.windows.net —
                # all of Azure Blob Storage, a general-purpose exfiltration
                # channel. Logs are what the feedback loop needs; artifacts
                # are not worth that.
                hosts.add("results-receiver.actions.githubusercontent.com:443")
            elif parsed.hostname:
                # GitHub Enterprise Server: the API is a path on the same host
                # (<host>/api/v3), so no separate entry — but the web host
                # itself was already added above and is what gh talks to.
                pass

    # Nextcloud skill: the instance host. Only reachable when the skill proxy
    # is off — with it on, the skill CLI runs server-side in the daemon's netns
    # and never meets this allowlist.
    if "nextcloud" in authorized_skills and config.nextcloud.url:
        from urllib.parse import urlparse

        parsed = urlparse(config.nextcloud.url)
        if parsed.hostname:
            hosts.add(f"{parsed.hostname}:{parsed.port or 443}")

    # Google Workspace skill: Google API hosts
    if "google_workspace" in authorized_skills:
        hosts.update({
            "oauth2.googleapis.com:443",
            "www.googleapis.com:443",
            "sheets.googleapis.com:443",
            "docs.googleapis.com:443",
            "drive.googleapis.com:443",
            "calendar-json.googleapis.com:443",
            "chat.googleapis.com:443",
            "gmail.googleapis.com:443",
            "people.googleapis.com:443",
            "admin.googleapis.com:443",
        })

    return hosts


# --- Manifest-derived credential / authorization helpers (Phase 3) ---


def derive_credential_set(skill_index: dict) -> frozenset[str]:
    """All sensitive env-var names declared by any skill manifest.

    Replaces the hand-maintained ``_PROXY_CREDENTIAL_VARS`` constant.
    Includes vars whose source is ``setup_env`` (the manifest declares
    the var name and ``sensitive: true``; the actual value comes from the
    skill's setup_env hook) so the var is split out of Claude's clean env
    and routed through the proxy.
    """
    return frozenset(
        spec.var
        for meta in skill_index.values()
        for spec in meta.env_specs
        if spec.sensitive and spec.var
    )


# Non-secret env vars the executor itself withholds from Claude, on top of the
# manifest-declared ``proxy_only`` ones. Neither is in any skill.md — both are
# set imperatively for every task — so neither has a manifest to carry the flag.
#
# ISTOTA_TASK_ATTEMPT is here rather than in the model's env because the model
# has no use for it and `tasks transcript` treats it as authority: it is the
# floor that withholds the transcript the calling run is still writing, so a
# value the model could replace is a floor the model could raise above every
# file. The proxy path was never exposed (`SkillProxy` spawns the CLI from the
# daemon's own snapshot, taking nothing from the request), but
# `skill_client._run_direct` re-execs with the *inherited* environment on a
# proxy-off deployment. Withholding it keeps that shape failing closed, which
# is exactly what it did before ISSUE-377 by way of ISTOTA_DB_PATH — the floor
# read the database then, and that path is stripped here too.
_EXECUTOR_PROXY_ONLY_VARS = frozenset({"ISTOTA_DB_PATH", "ISTOTA_TASK_ATTEMPT"})


def derive_proxy_only_set(skill_index: dict) -> frozenset[str]:
    """Env-var names to route through the proxy without treating as credentials.

    The third bucket alongside credentials and the clean env: values the
    host-side skill CLI needs but the model must not hold. Today that is the
    database paths — ``ISTOTA_DB_PATH`` plus the ``proxy_only`` vars the health
    and location manifests declare for their per-user module DBs — and
    ``ISTOTA_TASK_ATTEMPT``, which is authority rather than a path.

    Unlike ``derive_credential_set`` this is not per-skill-scoped. These aren't
    secrets, so there is nothing to leak between skills, and scoping them would
    mean re-deriving each skill's DB path in the proxy for no gain.
    """
    return frozenset(
        spec.var
        for meta in skill_index.values()
        for spec in meta.env_specs
        if spec.proxy_only and spec.var
    ) | _EXECUTOR_PROXY_ONLY_VARS


def derive_authorized_skills(
    selected_skills: list[str],
    skill_index: dict,
    ctx: object,
    hook_env: dict[str, str] | None = None,
) -> list[str]:
    """Skills authorized for credential access this task.

    A skill is authorized if EITHER:
      (a) it was selected (Pass 1 / Pass 2 picked it), OR
      (b) ANY of its sensitive EnvSpecs resolves successfully — the user
          has at least one of its credentials configured.

    Replaces ``_authorized_skills_from_credentials``. The auto-auth signal
    is now manifest-derived: adding a credential to a skill's ``env:``
    block is the only step needed to enroll it; no hand-maintained map.

    Three design choices:

    - ``any``, not ``all``. Multi-provider skills (e.g. ``developer`` —
      GitLab token OR GitHub token) auto-authorize when one provider is
      configured.
    - No ``meta.cli`` gate. The ``developer`` skill is doc-only but
      consumes its tokens via ``credential-fetch`` from helper scripts;
      gating on ``cli=true`` would lock it out (regression of e675ed9).
    - ``fallback_var`` does NOT contribute to authorization. An
      operator-set EnvironmentFile fallback is an instance-wide signal
      and would otherwise auto-authorize every user, defeating the
      per-user privacy posture. Resolution passes
      ``fallbacks_disabled=True``.

    ``hook_env`` is the merged output of ``dispatch_setup_env_hooks``. It
    is the auto-auth signal for a ``source="setup_env"`` credential, which
    ``_resolve_env_spec`` deliberately resolves to ``None`` — the manifest
    declares only the var name and the hook owns the value. Without it
    such a skill can never auto-authorize, so its credential is stripped
    from Claude's env (it is sensitive) and then never injected back by
    the proxy (it is in no authorized skill's credential map), leaving the
    CLI to run unauthenticated. ``google_workspace`` is the live case: it
    has no eager selector, so it is only ever reached via the on-demand
    menu, and selection is the only other route to authorization. Unlike
    an EnvironmentFile fallback, a hook value is per-user (here, derived
    from that user's stored OAuth token), so it is a sound auto-auth
    signal.
    """
    from .skills._env import _resolve_env_spec  # noqa: PLC0415

    authorized: set[str] = set(selected_skills)
    for name, meta in skill_index.items():
        if name in authorized:
            continue
        sensitive_specs = [s for s in meta.env_specs if s.sensitive]
        if not sensitive_specs:
            continue
        for spec in sensitive_specs:
            if spec.source == "setup_env":
                # The hook self-gates; a produced value means the user has
                # this credential configured.
                resolved = (hook_env or {}).get(spec.var)
            else:
                resolved = _resolve_env_spec(spec, ctx, fallbacks_disabled=True)
            if resolved:
                authorized.add(name)
                break
    return sorted(authorized)


def derive_skill_credential_map(
    authorized_skills: list[str],
    skill_index: dict,
) -> dict[str, set[str]]:
    """Per-skill: which sensitive env vars its manifest declares.

    Replaces ``_build_skill_credential_map``. Used by the proxy to scope
    credential injection: a skill CLI invocation only sees credentials
    its own manifest declared.
    """
    result: dict[str, set[str]] = {}
    for skill in authorized_skills:
        meta = skill_index.get(skill)
        if not meta:
            continue
        creds = {s.var for s in meta.env_specs if s.sensitive and s.var}
        if creds:
            result[skill] = creds
    return result


def derive_lookup_allowlist(
    authorized_skills: list[str],
    skill_index: dict,
) -> set[str]:
    """Union of credentials any authorized skill may fetch via credential-fetch.

    Replaces ``_allowed_credentials_for_skills``. Subtracts
    ``_PROXY_LOOKUP_BLOCKED`` as a defense-in-depth hard-reject list
    (today: ``ISTOTA_SECRET_KEY`` and the model credentials).
    """
    allowed: set[str] = set()
    for creds in derive_skill_credential_map(authorized_skills, skill_index).values():
        allowed |= creds
    return allowed - _PROXY_LOOKUP_BLOCKED


def _split_credential_env(
    env: dict[str, str],
    credential_set: frozenset[str] | set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split env into (credential_env, clean_env) using ``credential_set``.

    Phase 3: ``credential_set`` is derived per-task from the loaded skill
    index (``derive_credential_set(skill_index)``) instead of a
    module-level constant. The credential dict is passed to the skill
    proxy; the clean dict goes to Claude's subprocess.
    """
    credential_env: dict[str, str] = {}
    clean_env: dict[str, str] = {}
    for k, v in env.items():
        if k in credential_set:
            credential_env[k] = v
        else:
            clean_env[k] = v
    return credential_env, clean_env


def build_allowed_tools(
    is_admin: bool,
    skill_names: list[str],
    *,
    web_fetch_admin_only: bool = False,
) -> list[str]:
    """Build the per-task tool list.

    The CLI brains (ClaudeCodeBrain / TmuxClaudeBrain) no longer pass this as an
    ``--allowedTools`` allowlist — they run with ``--dangerously-skip-permissions``
    and the model gets its full default toolset. The security boundary is the
    bwrap sandbox + network proxy + clean env (credential stripping), not an
    interactive permission prompt; Bash is permitted anyway, which is effectively
    unrestricted inside the sandbox. ``Agent`` + ``Workflow`` (the harness's
    multi-agent fan-out) are denied separately via ``--disallowedTools`` so
    Istota orchestrates through its own skills.

    The returned list still matters in two places: NativeBrain filters its
    in-process tool set by these names, and a non-empty list is the signal that
    distinguishes a tool-bearing task from a text-only one (empty => sleep cycle
    / OCR / explainer, which get no tools and no skip-permissions).

    WebSearch is included for everyone; it runs server-side at the provider and
    returns result titles + URLs rather than page bodies, so it grants this host
    no egress, and page reading is steered to the `browse` skill in the prompt's
    Tools section.

    **`WebFetch` goes to everyone, and ``is_admin`` decides nothing here unless
    an operator asks it to** (ISSUE-449). On the native path the tool makes a
    GET from the *daemon's* own network namespace, outside the CONNECT
    allowlist — where the same user's task under a CLI brain has
    ``--unshare-net`` plus that allowlist and can reach only what the operator
    listed. That asymmetry is real and it used to be answered by withholding
    the tool from a non-admin.

    It is answered by an egress policy instead, because that is the axis the
    asymmetry is on. ``[brain.native.web_fetch]`` already carries one —
    ``allow_hosts``, ``block_hosts``, ``extra_blocked_cidrs``,
    ``allowed_ports``, ``allow_http``, the built-in private/reserved IP
    blocklist and ``require_url_provenance`` — and it binds every caller
    identically, which is what an egress policy is for. The identity gate bound
    nobody's destinations: an admin reached anything the policy allowed, and a
    non-admin asking to read a web page got a tool that was not there and a
    prompt that said nothing about why.

    ``web_fetch_admin_only`` is that gate, kept as an operator setting and off
    by default. Read unconditionally rather than only for a room-selected
    brain, matching what the removed rule did: the asymmetry exists on a
    native-default deployment as well as in a room an admin pinned to native
    (the fill is unconditional on sender, so a non-admin's turns run under
    whatever the room chose), and a rule scoped to pinned rooms would leave the
    same user with *more* egress on the deployment default than in a pinned
    room.

    **On the two CLI brains this changes nothing**, and that is not an oversight
    in either direction. They run with ``--dangerously-skip-permissions`` and
    never receive this list as an allowlist, so a non-admin there had the CLI's
    own `WebFetch` throughout — which is the tool none of this has a quarrel
    with, since it runs inside the namespace behind ``--unshare-net`` and the
    CONNECT allowlist. The tool being scoped is the daemon-side one, and native
    is the only brain that builds it.

    Two callers read this list and only one of them is the brain.
    `build_prompt`'s Tools section names `WebFetch` under the same condition, or
    a non-admin native task is told to reach for a tool that is not registered.
    """
    tools = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch"]
    if is_admin or not web_fetch_admin_only:
        tools.append("WebFetch")
    return tools


def _validate_workspace_dir(config: Config, workspace_dir: Path) -> Path:
    """Resolve and bounds-check a REPL workspace directory (blocklist posture).

    An arbitrary RW bind expands the sandbox's writable surface, so reject paths
    that overlap sensitive roots: the database directories, other users'
    Nextcloud mounts, the istota source tree, the credential/secret dirs, and
    $HOME dotfile config dirs (~/.ssh, ~/.config, ~/.claude, ~/.developer). The
    bwrap-host ``--workspace cwd`` case is the security-relevant one; Mac/Docker
    have no bwrap and degrade to running in cwd directly.

    The database entries are not what stops the *bwrap* path — the masks run
    last there, so a tmpfs at or under the workspace shadows the bind whatever
    it was. They are load-bearing for ``native_fs_roots``, which threads this
    same validated workspace into the native brain's in-process file tools and
    has no masks at all. In the default layout the source-tree entry covers the
    databases incidentally; a relocated ``db_path`` or ``module_data_dir`` is
    the case that needs naming.

    Raises ValueError when the path is forbidden. Returns the resolved path.
    """
    resolved = Path(workspace_dir).resolve()
    home = Path.home().resolve()

    forbidden: list[Path] = []
    # The istota source tree (don't let a workspace shadow our own code).
    try:
        forbidden.append(Path(__file__).resolve().parents[2])
    except IndexError:
        pass
    # Nextcloud mount root (other users' data live under here).
    if config.nextcloud_mount_path:
        forbidden.append(Path(config.nextcloud_mount_path).resolve())
    # The framework DB directory and the per-user module-DB root. Skipped when
    # db_path is relative (the `data/istota.db` default): it would resolve
    # against the *current* cwd, so `istota repl --workspace ~/proj` launched
    # from inside ~/proj would be refused for overlapping a `<cwd>/data` that
    # need not even exist.
    if config.db_path and Path(config.db_path).is_absolute():
        forbidden.append(Path(config.db_path).parent.resolve())
    try:
        forbidden.append(config.module_db_root())
    except ValueError:
        # Misconfigured module_data_dir (under the mount). The mount root is
        # already forbidden above, so the path is covered either way.
        pass
    # Credential / secret dirs + $HOME dotfile config dirs.
    for rel in (".ssh", ".config", ".claude", ".developer", ".aws", ".gnupg"):
        forbidden.append(home / rel)
    secret_key_path = os.environ.get("ISTOTA_SECRET_KEY_FILE")
    if secret_key_path:
        forbidden.append(Path(secret_key_path).resolve().parent)

    def _overlaps(a: Path, b: Path) -> bool:
        # True if a == b, a is under b, or b is under a.
        return a == b or _is_relative_to(a, b) or _is_relative_to(b, a)

    for bad in forbidden:
        try:
            bad_resolved = bad.resolve()
        except OSError:
            continue
        if _overlaps(resolved, bad_resolved):
            raise ValueError(
                f"workspace {resolved} overlaps a protected path ({bad_resolved})"
            )
    return resolved


_cache_dir_refusals: set[str] = set()

# Cache subdirectory names, per tool. uv and npm each treat their cache
# directory as theirs alone and prune it, so they do not share one root.
SANDBOX_CACHE_UV = "uv"
SANDBOX_CACHE_NPM = "npm"

#: The cache directory's name inside a user's own repos subtree.
#:
#: ``{developer.repos_dir}/{user_id}/{this}``, derived rather than configured.
#: The subtree is bound read-write and the cache sits inside it, which is what
#: puts the cache and a worktree's venv on one mount — ``link(2)`` compares
#: mounts rather than devices, so a cache anywhere else makes uv copy every
#: wheel instead of hardlinking it. Dotted so it does not read as a namespace
#: directory in a listing of the user's clones.
SANDBOX_CACHE_ROOT_NAME = ".package-caches"


def _sandbox_bind_targets(config: Config) -> list[Path]:
    """Paths ``build_bwrap_cmd`` mounts, that a cache must not be mounted above.

    bwrap applies argv in order, so a later ``--bind`` whose destination is an
    *ancestor* of an earlier mount covers it — the same mechanism the
    ``.developer`` read-only re-bind and the database masks rely on, used the
    wrong way round. The cache bind is emitted late, so without this list a
    supported config value silently revokes boundaries the sandbox is built on:
    ``sandbox_cache_dir = $HOME/.cache`` overmounts the read-only huggingface
    bind, ``= config.temp_dir`` hands every user's deferred-op directory to
    every task and makes the credential-fetch helpers under ``.developer``
    writable again, and ``= $HOME/.local`` gives the model write access to the
    ``claude`` binary the daemon spawns host-side.

    ``_validate_workspace_dir`` does not cover this and should not be made to:
    the REPL workspace it was written for is bound *before* all of those, so
    ordering protects it and its blocklist never had to name them. This list is
    the same idea as ``_mask_protected`` — what a late mount operation must not
    swallow — for the one other late mount operation.

    Equal-or-ancestor is the rule, not overlap, and this list answers **one
    direction only**: what the cache can swallow. It does not answer what can
    swallow the cache, and for a long time this docstring claimed it did — that
    a cache *inside* one of these "covers nothing", therefore is safe. The first
    half is true and the conclusion does not follow. ``developer.repos_dir``
    was both the documented home for the cache *and* an entry on this list,
    bound in full seven lines after it, so the documented shape put every
    user's cache directory inside an ancestor bind emitted later, read-write,
    for every admin developer task (ISSUE-319).

    That whole family is gone: the bind is one user's subtree and the cache is
    derived inside it, so there is no other user's cache in the namespace to
    reach. The ``repos_dir`` entry below survives the demolition and is worth a
    word, because with the derivation in place nothing can currently produce a
    configured cache root while ``repos_dir`` is set —
    the derived branch is gated on the ``sandbox_cache_is_derived`` triple —
    ``is_admin and developer.enabled and developer.repos_dir`` — while this
    entry is appended on ``repos_dir`` alone. So it fires on exactly one shape:
    a deployment with the path still set and the skill switched off, or a
    non-admin, either of which takes the fallback branch and reads
    ``security.sandbox_cache_dir`` *and* carries this entry. That is the
    sandbox-without-developer deployment the fallback exists for, and a
    ``sandbox_cache_dir`` at or above ``repos_dir`` there would cover every
    user's subtree at once. It stays because this list is the answer to one
    question — what must a cache never be mounted above — and
    ``repos_dir`` is on that list on the merits: a cache mounted over the root
    would cover every user's subtree beneath it. A future second reader of
    ``sandbox_cache_dir`` would want the entry already here rather than
    discover its absence the way ISSUE-319 was discovered.

    **It stays hand-written, and a test is what the extraction bought
    instead.** Making it a projection over the mount plan is an import cycle:
    it is called from inside :func:`resolve_sandbox_cache_dir`, which
    :func:`~istota.sandbox_plan.build_mount_plan` itself calls. So
    ``tests/test_sandbox_plan_parity.py::TestTheCacheAncestorList`` asserts the
    half that is available — every path named here is either a destination the
    plan actually mounts, or is deliberately broader than any one bind and says
    so there with its reason. An entry naming a path nothing mounts is
    ISSUE-319's shape from the other side: a list entry that reads like a
    boundary and refuses nothing.
    """
    home = Path(os.environ.get("HOME", "/tmp"))
    targets: list[Path] = [
        Path("/"), Path("/usr"), Path("/etc"), Path("/tmp"),
        # Every user's task workspace, and with it `.developer`.
        config.temp_dir,
        # The Claude CLI binary, its state, and its credentials.
        home / ".local",
        home / ".claude",
        # The read-only model cache.
        home / ".cache" / "huggingface",
    ]
    if config.developer.repos_dir:
        # The *global* root, not `repos_root(config, user_id)`, and deliberately
        # so — this function has no user and the global entry is the stricter
        # test. Equal-or-ancestor is the rule, so naming the root refuses a
        # cache at or above `{repos_dir}` while leaving one at
        # `{repos_dir}/{user_id}` permitted; that one lands *inside* the repos
        # bind for that user, at the same host directory, which covers nothing.
        targets.append(Path(config.developer.repos_dir))
    if config.nextcloud_mount_path:
        targets.append(Path(config.nextcloud_mount_path))
    for ro_path in config.security.sandbox_ro_paths:
        targets.append(Path(ro_path))
    sp_path = custom_system_prompt_path(config)
    if sp_path is not None:
        targets.append(sp_path)
    return targets


def _source_and_venv_paths() -> tuple[Path, Path]:
    """``(src/, venv)`` for the running install, both of them read-only binds.

    Two callers derive these — the binds themselves and
    :func:`mask_protected_paths` — and a second copy of the deployed-layout
    fallback is how they would come to disagree about which directory a mask
    must not swallow.

    **The venv comes from the running interpreter, not from the source layout.**
    ``sys.prefix`` *is* the venv root inside a virtual environment, which is
    what :func:`build_clean_env` already builds ``PATH`` from a few hundred
    lines above. The two used to disagree: ``PATH`` named ``{sys.prefix}/bin``
    while the bind covered ``{istota_home}/.venv``, and on any install where
    those are not the same directory the bind covered nothing — ``_ro_bind``
    skips a path that does not exist, silently, so the namespace simply had no
    Python interpreter in it.

    That cost nothing while the only things executed inside the sandbox were
    ``claude`` and ``bash``, both under the unconditional ``/usr`` bind.
    ``istota.tool_server`` is the first thing istota runs in there with its own
    interpreter (ISSUE-389), so the disagreement became "the tool server cannot
    start" — ``bwrap: execvp /venv/bin/python3: No such file or directory``,
    every native task, on any layout the convention does not describe. The
    conventional paths remain as a fallback for a non-venv install, where
    ``sys.prefix`` is ``/usr`` and already bound.
    """
    istota_src = Path(__file__).resolve().parent.parent  # src/
    if sys.prefix != sys.base_prefix:
        # In a venv: sys.prefix is its root, whatever the source tree looks like.
        return istota_src, Path(sys.prefix).resolve()
    istota_home = istota_src.parent  # project root or install root
    venv_path = istota_home / ".venv"
    if not venv_path.exists():
        # Deployed layout: {istota_home}/src/.venv
        venv_path = istota_src / ".venv"
    return istota_src, venv_path


def _reroot_interpreter(exe: Path, spelled_root: Path, bound_root: Path) -> Path:
    """``exe``, renamed from the root it was spelled under to the bound one.

    The one rule this module exists for, in one place because it is needed on
    both branches below and getting it on only one is how this bug is written.

    **Re-rooted, never resolved.** ``resolve()`` would follow
    ``{venv}/bin/python`` on through to ``/usr/bin/python3`` — a path that does
    exist in the namespace, and that is not in a venv, so the server would
    start and fail to import istota. Only the root is rewritten, so the
    interpreter keeps the ``bin/`` whose parent holds the ``pyvenv.cfg`` that
    makes it a venv at all.
    """
    if spelled_root == bound_root:
        return exe
    try:
        rel = exe.relative_to(spelled_root)
    except ValueError:
        # Nothing to re-root against. Better the path we were started with
        # than one assembled out of two unrelated trees — but this is the
        # pre-fix behaviour, so on a layout that needed the rewrite it is the
        # `exit 127` all over again, reported as a bare connection reset.
        # CPython derives both prefixes by walking up from `sys.executable`, so
        # reaching here at all means something upstream is unusual; say so
        # rather than leaving an operator with only the reset.
        logger.warning(
            "the running interpreter (%s) is not under %s, so it cannot be "
            "named against the %s the sandbox binds; a sandboxed tool server "
            "may fail to start",
            exe, spelled_root, bound_root,
        )
        return exe
    return bound_root / rel


def sandbox_interpreter() -> Path:
    """The running interpreter, named the way it exists *inside* the namespace.

    The sandbox binds Python at a **resolved** path — the venv at
    :func:`_source_and_venv_paths`'s answer, a standalone base interpreter at
    :func:`python_base_prefix_binds`'s — while ``sys.executable`` is spelled
    however the process was started. Where a symlink stands between the two (the
    Ansible role's ``/srv/app/{ns}/.venv -> /srv/app/{ns}/istota/.venv``) the
    argv names a path that exists nowhere in the namespace, since bwrap
    materializes a bind's parents as empty mount points and a symlink is not
    among them. The tool server then dies at exec: ``exit 127: env:
    '.../python': No such file or directory``, every native task. This is the
    other half of 88c36d54, which moved the bind onto ``sys.prefix`` and left
    the argv alone; the two agree wherever no symlink stands between them, which
    is every test host and no deployed one.

    **Both branches re-root, and that is not symmetry for its own sake.** The
    first draft returned ``sys.executable`` unchanged whenever
    ``sys.prefix == sys.base_prefix``, on the reasoning that a non-venv
    interpreter is under ``/usr``. That is true of a distro Python and false of
    a pyenv- or uv-managed one, which
    :func:`python_base_prefix_binds` now binds — resolved — so the branch
    reproduced the very bug above the moment it acquired a bind to disagree
    with. Found in review, measured, not reasoned about.
    """
    exe = Path(sys.executable)
    if sys.prefix == sys.base_prefix:
        binds = python_base_prefix_binds()
        if not binds:
            # Under /usr, which is bound unconditionally and as written.
            return exe
        return _reroot_interpreter(exe, Path(sys.base_prefix), binds[0])
    _, venv_path = _source_and_venv_paths()
    return _reroot_interpreter(exe, Path(sys.prefix), venv_path)


def python_base_prefix_binds() -> list[Path]:
    """The interpreter installation a ``bin/python`` symlink points at, if it
    needs binds of its own. Empty where something already covers it.

    Binding a venv carries its ``bin/python`` in as a *symlink*, not as an
    interpreter: every venv builder — ``python -m venv`` without ``--copies``,
    and uv — writes a link to the Python it was built from. Where that is the
    distro's, the target is under the unconditional ``/usr`` bind and there is
    nothing to do. Where it is a uv- or pyenv-managed standalone build under
    ``$HOME``, nothing binds it, and the argv then resolves to a link that
    dangles inside the namespace — the same ``exit 127``, one level down.

    Which one a host has is not a property of this deployment: the Ansible role
    runs a bare ``uv sync``, so uv downloads an interpreter wherever it does not
    find a suitable system one. The host that reported the symlinked-venv bug
    happened to have used ``/usr``, which is the only reason that surfaced as
    the outage it did rather than as this one.

    **Two spellings, not one, when they differ.** A symlink stores the literal
    string it was written with, and the kernel walks that string component by
    component *inside* the namespace — so binding only the resolved path leaves
    a venv whose ``bin/python`` names the unresolved one pointing at nothing.
    Binding both costs a second read-only mount of the same content and makes
    the traversal work whichever spelling the link carries. `[0]` is the
    resolved one, which is what :func:`sandbox_interpreter` re-roots onto.

    **It refuses rather than widening.** A resolved value that is not
    recognisably a Python installation — no ``bin/`` under it — drops the bind
    instead of binding whatever it names, and ``/`` is refused outright: the
    containment test below is an ancestor check, which ``/`` passes on neither
    arm, so without this it would ro-bind the entire host filesystem into every
    namespace. That is `user_scope`'s posture and `sandbox_cache_sweeper`'s, for
    their reason — the fallback would be the shared root, which is the exposure.

    Read-only, and what it exposes is a Python installation, which is what
    ``/usr`` already puts in every namespace. Empty for the ``/usr`` case, so
    the mount plan is byte-identical on a deployment with a distro Python.
    """
    spelled = Path(sys.base_prefix)
    base = spelled.resolve()
    usr = Path("/usr")
    if base == usr or usr in base.parents:
        return []
    if base == Path(base.root) or not base.parent.name:
        logger.warning(
            "sys.base_prefix resolves to %s, which is not a Python "
            "installation; not binding it into the sandbox", base,
        )
        return []
    if not (base / "bin").is_dir():
        logger.warning(
            "sys.base_prefix (%s) has no bin/ under it, so it does not look "
            "like a Python installation; not binding it into the sandbox", base,
        )
        return []
    binds = [base]
    if spelled != base:
        binds.append(spelled)
    return binds


def mask_protected_paths(
    config: Config,
    *,
    user_temp_dir: Path | None = None,
    workspace_dir: Path | None = None,
    plan_mounts: "tuple[Mount, ...] | None" = None,
) -> list[Path]:
    """Paths a late tmpfs mask must not shadow, for this config.

    A mask at or above any of these would take away something the task needs —
    its own workspace, the source tree it runs from, the venv, the mount — and
    turn a security measure into an outage. ``build_mount_plan`` builds this
    list per task; ``doctor`` builds it without one, to answer whether the
    database mask covers a directory on this deployment at all.

    **``plan_mounts`` is the per-task branch, and it is a projection rather
    than a second derivation.** Four of the five paths below — the task
    workspace, the source tree, the venv and the REPL workspace — are binds the
    plan already carries, and naming them again here is exactly the two-copies
    shape ISSUE-319 and ISSUE-320 each cost a filed bug. So
    :func:`~istota.sandbox_plan.build_mount_plan` passes its own accumulated
    mounts and this returns their ``Mount.protected`` entries. The Nextcloud
    mount **root** is the fifth and is added from the config either way,
    because it is the one protected path that is not a bind: the plan mounts
    ``{mount}/Users/{user_id}``, ``{mount}/Talk`` and
    ``{mount}/Channels/{token}`` and never the root itself, so projecting alone
    would stop refusing a mask over the mount whenever none of those exists.

    The mounts, not a whole ``MountPlan``, because the plan does not exist yet
    at the point the builder needs this: the masks are part of it. Passing the
    finished object would be a cycle wearing a nicer type.

    A caller with no task omits all three keyword arguments, and each omission
    is a documented divergence rather than an equivalence.

    ``user_temp_dir`` is the per-task workspace and is what the sandbox actually
    protects. Omitting it substitutes ``config.temp_dir``, the *parent* of every
    per-user workspace, which gives the same answer everywhere the two shapes
    are distinguishable: ``{temp_dir}/{user}`` is inside a candidate whenever
    ``{temp_dir}`` is, and the one arrangement where they differ is a
    ``db_path.parent`` sitting at ``{temp_dir}/{user}`` exactly — a framework
    database inside one user's workspace, which the sandbox has larger problems
    with than a mask. ``tests/test_sandbox.py`` pins that case, so a change
    widening the divergence goes red rather than quietly.

    ``workspace_dir`` is the REPL workspace, and omitting it drops an entry
    outright. The answers coincide only because ``_validate_workspace_dir``
    already refuses a workspace overlapping ``db_path.parent`` or
    ``module_db_root()`` — the two candidates any caller compares against — so a
    validated workspace is never among the paths a database mask would shadow.
    That is a dependency on another function's blocklist rather than a property
    of this one, which is why it is written down here: narrowing that blocklist
    would reopen the two-consumers gap the extraction exists to close.
    """
    if plan_mounts is not None:
        if not plan_mounts:
            # Raised rather than falling back, for the reason
            # `render_bwrap_argv` raises on a sourceless mount: an empty plan
            # would quietly yield a protected list of the mount alone, and
            # `plan_masks` would then emit a database mask over the venv and the
            # source tree, so every task on that host would die at `execvp
            # .../python3: No such file or directory`. Unreachable from the one
            # caller — `/usr` is on the list before this runs — and fail-closed
            # rather than fail-open if that ever stops being true.
            raise ValueError(
                "mask_protected_paths: plan_mounts is empty; a plan with no "
                "mounts cannot have produced the paths a mask must not shadow"
            )
        protected = [m.source for m in plan_mounts if m.protected and m.source]
    else:
        src, venv = _source_and_venv_paths()
        temp = (
            Path(user_temp_dir) if user_temp_dir is not None else Path(config.temp_dir)
        )
        protected = [temp.resolve(), src, venv]
        # The interpreter symlink target, where it needs binds of its own.
        # `build_mount_plan` marks those mounts `protected`, so this branch has
        # to name them too or the two derivations disagree — which is exactly
        # what `test_sandbox_plan_parity` holds them to.
        protected.extend(python_base_prefix_binds())
        if workspace_dir is not None:
            protected.append(Path(workspace_dir))
    mount = config.nextcloud_mount_path
    if mount:
        protected.append(Path(mount).resolve())
    return protected


def mask_shadowed_by(candidate: Path, protected: Iterable[Path]) -> list[Path]:
    """Which of ``protected`` a tmpfs mask at ``candidate`` would shadow.

    Empty means the mask is safe to emit; anything else is the refusal
    ``_mask_dir`` logs. Module level and shared rather than a closure, because
    ``doctor``'s ``runtime.session_log_dir`` check has to ask this exact
    question. "Is the directory under ``db_path.parent``" is the tempting
    second copy and it is wrong in the one case that matters: it answers True on
    the standalone install, where ``db_path.parent`` *is* the workspace and the
    mask is refused — so the checker would report the property holding while the
    directory sat outside every mask, which is the ``map_basemap`` two-consumers
    failure exactly.
    """
    return [p for p in protected if p == candidate or p.is_relative_to(candidate)]


def sandbox_cache_is_derived(config: Config, user_id: str) -> bool:
    """Whether this user's cache is derived inside their repos subtree.

    **The derivation is only safe where the repos bind covers it, so this is
    that bind's own gate and not a subset of it** — `is_admin and
    developer.enabled and developer.repos_dir`, byte for byte what
    `build_bwrap_cmd` emits the repos bind on and what `native_fs_roots` adds
    the repos write root on.

    Gating on less than that was ISSUE-320. The derived cache's parent,
    ``{repos_dir}/{user_id}``, is model-writable — through the repos bind for an
    admin, and through the devbox's read-write mount of the same directory for
    anyone the operator lists in ``istota_devbox_users``, which has no admin gate
    and reaches a task via the exec socket, itself bound on ``"developer" in
    authorized_skills`` with no admin gate either. So a *non-admin* took a cache
    bind inside a directory they could write, with no covering bind over it,
    which is the one shape where a symlink swap between
    :func:`resolve_sandbox_cache_dir`'s check and bwrap's ``mount`` binds the
    link's target read-write into the sandbox. Measured, both halves:
    ``tests/linux/test_sandbox_cache_dir.py::TestTheCacheBindSymlinkRace``.

    ``Config.is_admin`` is a pure function of the loaded config, so two
    concurrent tasks for one user cannot disagree about it — which is what lets
    the covering argument be about a user rather than about a task. Its
    empty-``admin_users`` branch returns True for everyone, and that fails in
    the safe direction here: everyone derives, and everyone gets the covering
    bind too.
    """
    return bool(
        config.developer.enabled
        and config.developer.repos_dir
        and config.is_admin(user_id)
    )


def resolve_sandbox_cache_dir(config: Config, user_id: str) -> Path | None:
    """This user's package-cache directory, or None.

    One predicate for two decisions — the RW bind in ``build_bwrap_cmd`` and the
    ``UV_CACHE_DIR`` / ``XDG_CACHE_HOME`` group in ``execute_task``. They must
    not disagree: naming a cache the sandbox did not bind points uv at a path
    that exists inside the namespace only on bwrap's root tmpfs, which is the
    RAM-backed cache ISSUE-305 is about, at a new name.

    **Two shapes, and the first one is derived rather than configured.**

    * :func:`sandbox_cache_is_derived` — an admin, with the developer skill on
      and a ``repos_dir``: ``{repos_dir}/{user_id}/.package-caches``.
      The repos bind is ``{repos_dir}/{user_id}`` and is emitted after the cache
      bind, so it is an ancestor and covers it — one mount, which is the only
      shape where uv hardlinks a wheel into a venv instead of copying it
      (``link(2)`` compares mounts rather than devices, measured four ways on
      the reference deployment). What that covering used to also expose was
      every *other* user's cache, because the root was shared; it is one user's
      own subtree now, so there is nothing beside the cache to reach and no
      mask to emit. ``security.sandbox_cache_dir`` is not consulted at all on
      this branch — the derivation is the layout, not a default for a key.
    * otherwise: ``{security.sandbox_cache_dir}/{user_id}``, unchanged. That
      serves a deployment running the sandbox without the developer skill,
      where ISSUE-305 still applies and there is no repos tree to put a cache
      in — and, since ISSUE-320, a non-admin on a deployment that does have
      one. Nothing binds an ancestor of it, so it is its own mount and a venv
      in the task workspace pays the copy — the same cost it paid before, not a
      regression introduced here. The key is blank on a deployed developer
      install, so in practice a non-admin there gets no disk cache at all and
      falls back to the pre-ISSUE-305 tmpfs. That is the intended trade: the
      hardlink win the derivation exists for is between one user's *worktrees*,
      and a non-admin has none, so the derivation was buying them nothing and
      costing them the ISSUE-320 window.

    **Per user in both shapes.** A single shared directory would be the first RW
    surface a non-admin task and an admin task hold in common, and it persists
    across tasks by construction — and uv's unpacked-wheel cache is trusted on
    read, never re-verified against a hash, so a planted archive is executed by
    the next ``uv sync`` that hardlinks out of it. Per-user costs nothing the
    placement argument was about: hardlink sharing is between one user's
    worktrees, which stay inside one subtree.

    **The containment assertion is the whole layout in one line.** The
    directory this returns must be the one the layout names, resolved: a child
    of the root, at the expected name. The cache's parent is bound read-write
    into the task's own sandbox on the derived branch, so the entry is
    model-plantable — a symlink at ``.package-caches`` pointing at another
    user's subtree would otherwise be created, ``chmod 0700``-ed and bound RW by
    the daemon, which is ISSUE-319 back through a name. Same equality rule
    ``get_user_repos_dir`` and ``sandbox_cache_sweeper`` use, applied on both
    branches because the configured root was model-adjacent too under the old
    default.

    **The protection checks run against the cache's parent, not the cache**, on
    both branches — the bind-target list, the database directories and
    ``_validate_workspace_dir``. Conservative in the direction that matters,
    since a broader path can only refuse more. One consequence is worth stating
    because it has no escape hatch any more: ``_validate_workspace_dir``
    overlaps in *both* directions, so a ``developer.repos_dir`` that overlaps
    the source tree, the Nextcloud mount, a database directory or a ``$HOME``
    dotfile directory loses its disk cache on every task, and
    ``security.sandbox_cache_dir`` cannot be used to put it elsewhere because
    that branch is not taken. Under the pre-derivation shape the cache root was
    independently configurable and could sidestep the overlap. The refusal is
    now a fact about ``repos_dir``, and the fix is to move ``repos_dir``.

    Returned **as written**, not resolved, though every check below runs against
    the resolved path. ``_bind`` uses the string it was handed as the sandbox
    destination, and the repos bind passes ``{repos_dir}/{user_id}`` unresolved
    — so resolving here would put a symlinked ``repos_dir`` and the cache under
    it at two different names inside the namespace, hence on two mounts, and
    ``link(2)`` returns EXDEV between them. That is the exact cost the
    derivation exists to avoid, failing silently.

    Never raises. Every rejection falls open to the pre-ISSUE-305 behaviour,
    because both callers run on the task path — for NativeBrain, per Bash call —
    and the alternative to failing open is a config typo that fails every task.
    """
    def _refuse(message: str) -> None:
        # Called from `build_bwrap_cmd` and from `execute_task`, on every task,
        # for what is a fact about the config file. Warn once per process per
        # distinct problem instead of twice per task forever.
        if message not in _cache_dir_refusals:
            _cache_dir_refusals.add(message)
            logger.warning("%s", message)

    # Inside the `try`, deliberately, and this is not a style choice. The
    # never-raises contract is what both callers rest on, and one of them is
    # `build_bwrap_cmd` under NativeBrain, which reaches this per Bash call. The
    # branch selection touches paths: `get_user_repos_dir` guards only `OSError`
    # while `Path.resolve()` raises `ValueError` on an embedded null byte and
    # `Path(root) / user_id` raises `TypeError` on a non-str user id. The
    # pre-derivation code read a plain string attribute and entered the `try`
    # immediately, so leaving the selection above it opened a hole that had not
    # been there.
    try:
        # Which shape, and with it the three things that differ: the root the
        # leaf is created under, the leaf's name, and which directory the
        # *operator* is responsible for having created. On the derived branch
        # that last one is `repos_dir` itself — `{repos_dir}/{user_id}` is made
        # here (with parents) for a non-admin, whose subtree the developer
        # skill's `setup_env` deliberately does not create, since no sandbox
        # binds it.
        #
        # **Gated on the repos bind's whole condition**, via
        # `sandbox_cache_is_derived` — `is_admin` included, which this branch
        # used to leave out while the comment here named it. The derivation's
        # whole justification is that the repos bind covers the cache, and that
        # bind is `is_admin and config.developer.enabled`; with either half
        # missing there is no covering bind, so deriving would take the
        # operator's explicit `security.sandbox_cache_dir` away and give nothing
        # back — and, for a non-admin, would put the cache inside a directory
        # the devbox lets them write with nothing on top of it (ISSUE-320).
        # A sandbox running without the developer skill is exactly the
        # deployment that key is kept for; so is a non-admin on one that has it.
        if sandbox_cache_is_derived(config, user_id):
            repos_root = get_user_repos_dir(config, user_id)
            if repos_root is None:
                # `get_user_repos_dir` has already said why. No fall back to
                # `security.sandbox_cache_dir`: with the developer skill on,
                # that key is not this deployment's cache location, and the
                # reasons the join fails — no user id, a planted symlink — are
                # reasons to give this task no disk-backed cache rather than to
                # reach for another path.
                _refuse(
                    f"sandbox cache: {config.developer.repos_dir} has no usable "
                    f"subtree for user {user_id!r}; not binding a cache. Package "
                    "caches stay on the sandbox's root tmpfs, in RAM."
                )
                return None
            raw = str(repos_root)
            leaf = SANDBOX_CACHE_ROOT_NAME
            must_exist = Path(config.developer.repos_dir)
            key = "developer.repos_dir"
        else:
            raw = config.security.sandbox_cache_dir
            if not raw:
                return None
            leaf = user_id
            must_exist = Path(raw)
            key = "security.sandbox_cache_dir"

        root = Path(raw)
        if not root.is_absolute():
            # Names the branch's own key. `get_user_repos_dir` runs no
            # absoluteness check of its own, so a relative `developer.repos_dir`
            # surfaces here and nowhere else — and sending that operator to
            # `security.sandbox_cache_dir`, which is blank on their deployment,
            # is worse than saying nothing.
            _refuse(
                f"sandbox cache root {raw!r} (from {key}) is not an absolute path; "
                "not binding a cache. A relative path would resolve against the "
                "daemon's working directory."
            )
            return None

        resolved_root = root.resolve()
        # `must_exist` is `root` on the configured branch and the operator's
        # `repos_dir` on the derived one, where `{repos_dir}/{user_id}` may not
        # exist yet and is created below with its parents. The daemon creating
        # a whole tree of directories out of a value it was configured with is
        # what this check refuses in both shapes: a typo should read as a
        # warning and a cache in RAM, not as a new directory tree.
        resolved_must_exist = must_exist.resolve()
        if not (
            resolved_must_exist.is_dir()
            and os.access(resolved_must_exist, os.W_OK | os.X_OK)
        ):
            _refuse(
                f"sandbox cache root {resolved_must_exist} is not a directory the "
                "daemon can write; not binding a cache. Package caches stay on the "
                "sandbox's root tmpfs, in RAM."
            )
            return None

        # Not above anything the sandbox already mounts — see _sandbox_bind_targets.
        for target in _sandbox_bind_targets(config):
            try:
                resolved_target = target.resolve()
            except OSError:
                continue
            if resolved_target == resolved_root or _is_relative_to(resolved_target, resolved_root):
                _refuse(
                    f"sandbox cache root {resolved_root} is at or above "
                    f"{resolved_target}, which the sandbox mounts; not binding a "
                    "cache, because the cache bind would cover that mount. Set "
                    "developer.repos_dir and let the cache derive from it, or point "
                    "security.sandbox_cache_dir inside a directory instead of above one."
                )
                return None

        # The database directories, checked here rather than left to
        # `_validate_workspace_dir`: that function skips a relative `db_path`
        # (the shipped default) because a REPL workspace would resolve it
        # against the wrong cwd, and the daemon has no such problem. The masks
        # are the last mount operations and are read-only, so a cache under one
        # is a dead end uv cannot write. The cache loses that argument, never
        # the mask.
        db_dirs: list[Path] = []
        if config.db_path:
            db_dirs.append(Path(config.db_path).parent.resolve())
        try:
            db_dirs.append(config.module_db_root())
        except ValueError:
            pass
        for db_dir in db_dirs:
            if resolved_root == db_dir or _is_relative_to(resolved_root, db_dir):
                _refuse(
                    f"sandbox cache root {resolved_root} is under the database "
                    f"directory {db_dir}, which the sandbox masks read-only; not "
                    "binding a cache."
                )
                return None

        # The remaining protected roots — the source tree, the mount, the
        # credential and dotfile directories — via the blocklist the REPL
        # workspace already uses. Same posture: an operator-named RW bind.
        _validate_workspace_dir(config, resolved_root)

        # The containment assertion. Two checks, because neither catches the
        # other's cases — the same pair `get_user_repos_dir` runs, for the same
        # reason. The lexical one refuses a leaf that never became a child (an
        # empty `user_id` collapses `root / ""` to the root itself, which is
        # the shared cache the per-user split exists to prevent, and which the
        # old code produced silently); the resolved one refuses a symlink,
        # which is a child by name and somewhere else on disk.
        #
        # This is the invariant the whole layout rests on. On the derived
        # branch the parent is bound read-write into this very task's sandbox,
        # so a task can plant `.package-caches` as a symlink into another
        # user's subtree — and without this the daemon would `mkdir` through
        # it, `chmod 0700` its target and bind that target RW on the next task.
        cache_dir = root / leaf
        if cache_dir.parent != root or cache_dir.resolve() != resolved_root / leaf:
            _refuse(
                f"sandbox cache {cache_dir} does not resolve to the directory named "
                f"by {leaf!r} inside {resolved_root}; not binding it. A symlink there "
                "would put the cache in another user's tree. Package caches stay on "
                "the sandbox's root tmpfs, in RAM."
            )
            return None

        # `parents=True` matters on the derived branch: the developer skill's
        # `setup_env` creates `{repos_dir}/{user_id}` for an *admin* only,
        # matching the bind's gate, and a non-admin still gets a cache here —
        # ISSUE-305 applies to any task that runs a package manager.
        #
        # The intermediate directory is mode-set separately and best-effort. A
        # parent created by `parents=True` takes the umask, and this one holds a
        # user's clones on the admin path; but `mkdir(exist_ok=True)` succeeds
        # on a directory another uid owns and `chmod` then raises EPERM, and
        # losing the whole cache to that would be trading the thing this
        # function exists for against a mode it does not own. The cache
        # directory's own 0700 below is not best-effort, because that one is
        # this function's to get right.
        parent_missing = not root.exists()
        cache_dir.mkdir(parents=True, exist_ok=True)
        if parent_missing:
            try:
                os.chmod(root, 0o700)
            except OSError as exc:
                logger.warning(
                    "sandbox cache: could not set 0700 on %s (%s); the cache "
                    "inside it is still 0700", root, exc,
                )

        # `mkdir` and `chmod` re-traverse the path *by name*, so the containment
        # check above is a check on one inode and these are operations on
        # whatever the name means now. `Path.mkdir(exist_ok=True)` swallows
        # `FileExistsError` whenever `is_dir()` says yes, and `is_dir()` follows
        # a symlink; plain `os.chmod` follows one too. On the derived branch the
        # parent is bound read-write into a live task's sandbox and this
        # function runs on the task path, so the writer and the checker are
        # concurrent by construction — a symlink landing at `.package-caches`
        # between the two would get another user's subtree `chmod 0700`-ed.
        #
        # `O_NOFOLLOW` refuses the symlink at the last component (ELOOP), and
        # `fchmod` then acts on the descriptor rather than on the name, so the
        # mode lands on the inode that was opened or on nothing. The equality is
        # re-asserted through `/proc/self/fd` where that is readable, which is
        # the only way to ask "what did I actually open" rather than "what does
        # this name mean now".
        #
        # **The residual is stated rather than implied, and it has been
        # measured.** `_bind` resolves in Python and the *kernel* walks the name
        # again at bwrap's `mount`, so a swap after this returns still reaches
        # the bind — confirmed following a planted symlink into another user's
        # subtree (ISSUE-320). What stops it being an exposure is not anything
        # in this function: it is that `sandbox_cache_is_derived` now takes this
        # branch only where `build_bwrap_cmd` emits the covering repos bind, so
        # a cache whose parent is model-writable always has that bind landing on
        # top of it. Closing the window here instead means binding through
        # `/proc/self/fd/N`, which changes the sandbox *destination* name — and
        # the destination name is load-bearing here (see the "as written"
        # paragraph above: two names means two mounts means EXDEV). That is a
        # trade this function cannot make alone.
        #
        # The native brain has no mounts and therefore no covering anything, so
        # it does not add this path as a write root on the derived branch at
        # all — see `native_fs_roots`.
        fd = os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
            try:
                opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                # No procfs (darwin, and the test suite runs there). The
                # `O_NOFOLLOW` above is still the guard that matters; this is
                # the confirmation, not the check.
                opened = None
            if opened is not None and opened != resolved_root / leaf:
                _refuse(
                    f"sandbox cache {cache_dir} changed under us: it is {opened}, "
                    f"not {resolved_root / leaf}; not binding it."
                )
                return None
        finally:
            os.close(fd)
        return cache_dir
    except Exception as exc:  # never raise: both callers are on the task path
        # `raw` deliberately via `locals()`: the branch selection is inside the
        # `try` now, so it is the one thing that can raise *before* `raw` is
        # bound, and a bare reference here would turn the never-raises guard
        # into an `UnboundLocalError` on exactly the path it was widened to
        # cover. Found by the test for that widening.
        _refuse(
            f"sandbox cache {locals().get('raw', '<unresolved>')} rejected "
            f"({exc!r}); not binding it."
        )
        return None


def custom_system_prompt_path(config: Config) -> Path | None:
    """Absolute path of the operator's ``config/system-prompt.md``, or None.

    Absolute via ``abspath`` (not ``resolve``) so a relative ``skills_dir``
    still yields a path a child process with its own ``--chdir`` can open,
    without rewriting a symlinked deployment root to a name nothing else uses.
    """
    if not config.custom_system_prompt:
        return None
    path = config.skills_dir.parent / "system-prompt.md"
    if path.is_absolute():
        return path
    return Path(os.path.abspath(path))


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def build_bwrap_cmd(
    cmd: list[str],
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
    proxy_sock: Path | None = None,
    net_proxy_sock: Path | None = None,
    extra_ro_binds: list[Path] | None = None,
    authorized_skills: "frozenset[str] | set[str] | list[str] | None" = None,
    workspace_dir: Path | None = None,
    *,
    profile: SandboxProfile,
) -> list[str]:
    """Wrap a command in bubblewrap for per-user filesystem isolation.

    Returns the original cmd unchanged if sandbox is not available
    (non-Linux, bwrap not installed, or namespace creation denied).

    Three steps, and the middle one is where the policy lives.
    :func:`~istota.sandbox_plan.build_mount_plan` decides every bind, mask and
    namespace flag — it holds the whole of what this function used to be down
    to the argv formatting, and its docstring documents ``profile``,
    ``authorized_skills`` and ``workspace_dir``.
    :func:`~istota.sandbox_plan.render_bwrap_argv` turns that answer into argv
    and decides nothing. The split exists so the native brain's file-tool
    roots, the cache bind's ancestor check and the mask's protected paths can
    project one answer instead of each restating a slice of it; the argv is
    unchanged by it, and ``tests/test_sandbox_argv_golden.py`` is what says so.
    """
    if not _bwrap_available():
        return cmd

    plan = build_mount_plan(
        config,
        task,
        is_admin,
        user_resources,
        user_temp_dir,
        profile=profile,
        proxy_sock=proxy_sock,
        net_proxy_sock=net_proxy_sock,
        extra_ro_binds=extra_ro_binds,
        authorized_skills=authorized_skills,
        workspace_dir=workspace_dir,
    )
    return render_bwrap_argv(
        plan, cmd, net_proxy_sock=net_proxy_sock, user_temp_dir=user_temp_dir,
    )


def native_fs_confinement_active(config: Config) -> bool:
    """Whether NativeBrain's in-process file tools should be path-confined.

    Keyed off *effective* sandboxing, exactly like the executor's cwd choice:
    on a real multi-user deployment (Linux + bwrap) the claude_code path
    confines the filesystem via bwrap, so the native file tools confine
    themselves to the same roots (NB-1). Where bwrap is unavailable (Mac / dev),
    claude_code runs unconfined too, so native stays unconfined for parity
    rather than surprising the developer with a boundary the CLI path doesn't
    have.

    **"which run in-process, outside any bwrap" is what this used to say, and
    it stopped being true in ISSUE-389.** The six core tools now run in a
    per-attempt namespace of their own (`tool_server`, `SandboxProfile.NATIVE`),
    so these roots are the error-message layer above that namespace — and the
    only confinement there is on the shapes where nothing is confined, which is
    the branch this predicate already returns False for. The stale sentence is
    named rather than deleted because two documents cited it as the authority
    for a native-versus-CLI difference that no longer exists.
    """
    return effective_sandboxing(config)


def native_fs_roots(
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
    workspace_dir: Path | None = None,
    control_dir: Path | None = None,
) -> tuple[list[Path], list[Path], list[Path]]:
    """File-access roots for a native-brain task.

    Returns ``(read_roots, write_roots, write_denied_roots)``.

    **These are no longer the boundary, and that changes what this function is
    for.** It was written when the native file tools ran on daemon worker
    threads with no namespace around them, so it was a second filesystem policy
    written in Python — one that had to be kept in step with every bind
    ``build_bwrap_cmd`` emits, and one whose check and open were separate
    syscalls, so an ancestor could be swapped between them. The tools now run
    inside ``istota.tool_server``, in the one bwrap namespace the attempt gets
    (ISSUE-389), where a path outside the binds is *absent* rather than
    refused.

    What these roots still do is produce the error the model reads. "Cannot
    read /etc/shadow: path is outside the allowed workspace" is a better answer
    than ENOENT, and it is the answer on the unsandboxed shapes too — macOS,
    the standalone install, a Docker stack without the two container settings —
    where ``build_bwrap_cmd`` hands the command back unwrapped and this list is
    once again the only confinement there is. So it still names
    ``build_bwrap_cmd``'s user-data binds exactly (not the system/venv binds,
    which are irrelevant to the file tools): writable roots are the RW binds,
    read roots additionally the RO binds (Talk attachments, read-only
    resources) — with one qualification the projection added: a read-only bind
    nested inside an earlier read-write one is a *write-deny* root instead,
    since that is what bwrap's ordering already makes it, so it is reachable
    for reading through the root containing it rather than named on its own
    (rule 2 in ``project_fs_roots``). No database root of any kind — the
    sandbox masks those, and these tools have no masks.

    **It no longer restates them.** This function builds the same
    :class:`~istota.sandbox_plan.MountPlan` ``build_bwrap_cmd`` renders and
    hands it to :func:`~istota.sandbox_plan.project_fs_roots`, which is where
    the four derivation rules live. A bind added to the plan reaches both
    consumers or neither, which is what ISSUE-319 and ISSUE-320 each cost a
    filed bug to discover. The plan is built with ``profile=NATIVE`` — the
    profile decides only the Claude runtime block and the custom system prompt
    bind, none of which is user data, so the roots would be the same either
    way; naming the right one keeps the plan honest for anything that reads it
    later.

    The third element carries the RO carve-outs bwrap gets by re-binding a
    path read-only *after* the RW bind that would otherwise cover it.
    Containment alone cannot express those, so they are returned separately and
    threaded onto ``ToolEnv.write_denied_roots``. Two *named* entries, plus
    every read-only user-data mount nested inside an earlier read-write one,
    which ``project_fs_roots`` derives rather than naming (rule 2 there).
    The two named ones:
    ``.developer`` — the credential-fetch helper and the git credential helpers
    — which the claude_code path has protected since the RO re-bind was added
    and which this function silently left writable until it grew this return
    value. It is carried at the path *as written*, matching the bind, which is
    the same value this function has always returned; note that it is not the
    value enforcement compares against, since ``ToolEnv`` realpaths every deny
    root, so a symlink planted at that name relocates the denial. That gap
    predates the projection and is not closed here. And ``control_dir``, the task's own
    ``{temp_dir}/.control/{user_id}/task_{id}``: every file the daemon authors
    for this task, which is to say both prompt halves, the briefing metadata
    and the prepared image renditions.

    **``control_dir`` goes in two lists, and neither covers the other's case.**
    It is also appended to ``read_only``, and the reason is the whole shape of
    ``ToolEnv``:

    - ``read_roots`` is ``None`` when confinement is off, and ``None`` means
      *unconfined* — both root lists are then inert. So a ``read_only`` entry
      alone protects nothing on the standalone install or the shipped Docker
      stack.
    - ``write_denied_roots`` is checked *ahead of* that unconfined return, so
      it is enforced whether or not confinement is active; its empty value is
      ``()`` rather than ``None``, because a deny set has no unconfined meaning
      to signal.
    - Under confinement the control directory is inside no write root — that is
      the design, it is a sibling of ``user_temp_dir`` rather than a child — so
      ``read_only`` is what makes it *readable* while leaving it unwritable.
      Without it a confined task could not open its own prepared image
      attachment, on a path its own prompt named.

    **Only this task's own directory, and the routes that could widen that.**
    ``{temp_dir}/.control`` and ``{temp_dir}/.control/{user_id}`` are named
    here by nothing, so a task reaches no other task's control files. Two
    things could put a wider path back in, and only one of them is guarded.
    ``security.sandbox_ro_paths`` is bound verbatim and now warns at config
    load (``config._warn_ro_paths_over_control_tree``). The **per-resource
    mounts** below are not: a ``user_resources`` row is ``mount /
    resource_path``, bounded by the Nextcloud mount root and nothing else, so
    on a layout where ``config.temp_dir`` sits *under*
    ``nextcloud_mount_path`` a row naming the control tree would bind it
    read-write and neither entry here would cover a sibling task's directory.
    That is the same residual ``.claude/rules/brain.md`` records for the
    session-log directory, and it is out of scope on the same grounds: no
    shipped shape produces the layout (Ansible and Docker both put
    ``temp_dir`` outside the mount; on the standalone install the two coincide
    but ``sandbox_enabled`` is false, so this function is never called), and
    refusing a resource path is a decision about resources rather than about
    this guard. Stated so it is a known gap rather than an assumed absence.

    **The entry is a directory, and that is what retired the per-file one.**
    It used to be ``composed_system_prompt_path``, one exact path, because
    neither this list nor a bwrap bind can express a filename pattern — so
    ``task_<id>_prompt.txt``, the briefing metadata and every rendition sat
    beside it unguarded, and a *second concurrent task of the same user* could
    overwrite another task's file, ``user_temp_dir`` being per user rather than
    per task. ``ToolEnv._in_denied`` compares realpaths with ``is_relative_to``,
    so one directory entry covers every file nested under it and a framework
    file added later needs no new entry here.

    **This function is not the only producer of that deny entry, and must not
    become it.** ``execute_task`` calls this only under
    ``native_fs_confinement_active``, so on an unsandboxed shape — macOS, the
    standalone install, the shipped Docker stack — nothing here runs at all.
    The control directory is therefore seeded onto ``fs_write_denied_roots``
    *outside* that branch, which is where it does the most work: those are the
    deployments with no bwrap re-bind behind it. ``ToolEnv`` enforces a deny
    root whether or not confinement is on, precisely so that seeding means
    something. This function returns it too, so the confined path has one list
    and no duplicate.

    Carve-outs here deny *writes* only. bwrap's other nested override, the
    tmpfs masks over ``db_path.parent`` and ``module_db_root()``, is a total
    mask this cannot express — what holds that property on the native side is
    that neither path is under a returned root, which in turn rests on
    ``Config.module_db_root`` refusing a module dir under the Nextcloud mount
    and on ``_validate_workspace_dir`` refusing a workspace that would bind one
    back in. Those two guards, not this function, are what to check if a
    deployment ever puts a database under ``temp_dir`` or ``repos_dir``.

    **A rejected REPL workspace costs the workspace and nothing else, which is
    the deliberate asymmetry with ``build_bwrap_cmd``.** There a workspace the
    blocklist refuses fails the task, because the alternative is a namespace
    that silently lacks the directory the user asked to work in. Here the roots
    are an error-message layer over the same plan, so the answer is to log and
    carry on without it. The validation therefore runs *before* the build
    rather than as a ``try`` around it: catching there would either lose every
    other root or mean building the plan a second time, and the plan build is
    not free — ``resolve_sandbox_cache_dir`` creates a directory and
    ``plan_masks`` logs a refused mask, both of which would then happen twice
    per task. It is also the narrower catch, which matters more: a ``ValueError``
    from anywhere else in the build (``Path.resolve`` raises one on an embedded
    NUL, and a ``user_resources`` row is row data) still propagates instead of
    being reported as a blocklist rejection that never happened.
    """
    if workspace_dir is not None:
        try:
            _validate_workspace_dir(config, workspace_dir)
        except ValueError:
            logger.warning(
                "native_fs_roots: workspace %s rejected by blocklist", workspace_dir,
            )
            workspace_dir = None

    plan = build_mount_plan(
        config,
        task,
        is_admin,
        user_resources,
        user_temp_dir,
        profile=SandboxProfile.NATIVE,
        workspace_dir=workspace_dir,
    )
    return project_fs_roots(plan, control_dir)


def _detect_notification_reply(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None" = None,
) -> db.Task | None:
    """
    Check if this task is a reply to a scheduled/briefing notification.

    Returns the parent task if the user is replying to a scheduled or briefing
    notification, so context can be scoped narrowly. Returns None otherwise.
    """
    if not task.reply_to_talk_id or not task.conversation_token or not conn:
        return None
    parent = db.get_reply_parent_task(conn, task.conversation_token, task.reply_to_talk_id)
    if parent and parent.source_type in ("scheduled", "briefing"):
        return parent
    return None


def _user_email_address_map(config: Config) -> dict[str, list[str]]:
    """Every configured user's own email addresses, keyed by user id.

    Drives the ISSUE-226 sender attribution in the history readers. A user with
    no configured addresses maps to `[]`, which reads as "nothing is theirs" —
    every email turn of theirs is then attributed to its envelope sender. That
    is the safe direction, but it also means their *own* mail reads as
    third-party, so a missing `email_addresses` is worth a log line rather than
    silent degradation.
    """
    address_map: dict[str, list[str]] = {}
    for user_id, user_config in config.users.items():
        addresses = list(user_config.email_addresses or [])
        if not addresses:
            logger.debug(
                "User %s has no configured email_addresses; their own email "
                "turns will be attributed to the sending address",
                user_id,
            )
        address_map[user_id] = addresses
    return address_map


def _ensure_reply_parent_in_history(
    task: db.Task,
    history: list[db.ConversationMessage],
    config: Config,
    conn: "db.sqlite3.Connection | None" = None,
) -> tuple[list[db.ConversationMessage], db.ConversationMessage | None]:
    """
    Ensure the replied-to message's task is included in conversation history.

    If the user replied to a specific message, look up the task associated with
    it and prepend that whole turn to history if not already present — which is
    what saves a long parent from being reduced to the 1000-char snapshot.
    Falls back to injecting reply_to_content as a synthetic message if the
    parent task isn't found in the DB.

    The parent is addressed in one of two namespaces, resolved by two distinct
    lookups: `reply_to_message_id` is a canonical `messages.id` (web) and
    `reply_to_talk_id` is a Talk message id. They are never interchangeable —
    in a Talk-bound room both are small integers, so crossing them resolves to
    an unrelated turn with no signal that it went wrong. The canonical id is
    tried first: a task carrying both is one whose canonical citation was
    derived from the Talk one (Stage 6), where the canonical lookup is the
    more precise of the two.

    Returns (updated_history, reply_parent_msg) where reply_parent_msg is the
    message that must survive triage (or None if not applicable).
    """
    if not task.conversation_token:
        return history, None
    if not task.reply_to_message_id and not task.reply_to_talk_id:
        return history, None

    history_ids = {msg.id for msg in history}

    # `get_reply_parent_task` also matches on `talk_response_id`, which an email
    # task carries once its confirmation prompt was posted — so this path can
    # surface an email turn and needs the same sender attribution as the bulk
    # readers (ISSUE-226).
    address_map = _user_email_address_map(config)

    def _lookup(c: db.sqlite3.Connection) -> tuple[db.Task | None, str | None]:
        parent = None
        if task.reply_to_message_id:
            parent = db.get_reply_parent_task_by_message_id(
                c, task.conversation_token, task.reply_to_message_id,
            )
        if parent is None and task.reply_to_talk_id:
            parent = db.get_reply_parent_task(
                c, task.conversation_token, task.reply_to_talk_id,
            )
        if parent is None:
            return None, None
        return parent, db.email_sender_for_task(c, parent.id)

    if conn is not None:
        parent_task, parent_sender = _lookup(conn)
    else:
        with db.get_db(config.db_path) as temp_conn:
            parent_task, parent_sender = _lookup(temp_conn)

    if parent_task:
        parent_msg = db.ConversationMessage(
            id=parent_task.id,
            prompt=parent_task.prompt,
            result=parent_task.result or "",
            created_at=parent_task.created_at or "",
            actions_taken=parent_task.actions_taken,
            source_type=parent_task.source_type,
            user_id=parent_task.user_id,
            external_sender=db.external_email_sender(
                parent_sender, address_map.get(parent_task.user_id or "", []),
            ),
        )
        if parent_task.id not in history_ids:
            logger.info(
                "Force-including reply parent task %d in context for task %d",
                parent_task.id,
                task.id,
            )
            return [parent_msg] + history, parent_msg
        else:
            logger.debug(
                "Reply parent task %d already in history for task %d",
                parent_task.id,
                task.id,
            )
            return history, parent_msg

    # An unresolvable parent — a `role='system'` row, a turn retention deleted,
    # a turn that failed or is still running — falls through to the snapshot
    # alone, which `build_prompt` already quotes into the request section
    # unconditionally. The synthetic `(replied-to message)` context row that
    # used to be injected here is gone with the `(In reply to: …)` fallbacks it
    # was a sibling of: both put the same 1000 characters in the prompt a
    # second time, once as context and once as the frame.
    if task.reply_to_content:
        logger.info(
            "Reply parent not resolvable for task %d "
            "(canonical=%s talk=%s); the request-section quote stands alone",
            task.id,
            task.reply_to_message_id,
            task.reply_to_talk_id,
        )

    return history, None


def _apply_recency_window_talk(
    messages: list[db.TalkMessage],
    config: Config,
) -> list[db.TalkMessage]:
    """Trim Talk messages to recency window, keeping a guaranteed minimum.

    Always includes the most recent `context_min_messages`. Beyond that,
    includes older messages only if they fall within `context_recency_hours`
    of the newest message. Disabled when context_recency_hours == 0.

    Messages must be in chronological order (oldest first).
    """
    recency_hours = config.conversation.context_recency_hours
    if recency_hours <= 0 or not messages:
        return messages

    min_count = config.conversation.context_min_messages
    if len(messages) <= min_count:
        return messages

    # Cutoff based on the newest message's timestamp
    newest_ts = messages[-1].timestamp
    cutoff_ts = newest_ts - (recency_hours * 3600)

    # Walk backwards: guaranteed minimum, then include if within window
    guaranteed = messages[-min_count:]
    older = messages[:-min_count]
    within_window = [m for m in older if m.timestamp >= cutoff_ts]

    result = within_window + guaranteed
    if len(result) < len(messages):
        logger.info(
            "Recency window trimmed Talk context from %d to %d messages "
            "(min=%d, window=%.1fh, dropped=%d older)",
            len(messages), len(result), min_count, recency_hours,
            len(messages) - len(result),
        )
    return result


def _apply_recency_window_db(
    history: list[db.ConversationMessage],
    config: Config,
) -> list[db.ConversationMessage]:
    """Trim DB conversation messages to recency window, keeping a guaranteed minimum.

    Same logic as _apply_recency_window_talk but for ConversationMessage
    (uses created_at datetime string instead of unix timestamp).

    Messages must be in chronological order (oldest first).
    """
    recency_hours = config.conversation.context_recency_hours
    if recency_hours <= 0 or not history:
        return history

    min_count = config.conversation.context_min_messages
    if len(history) <= min_count:
        return history

    # Parse the newest message's created_at to get cutoff
    newest = history[-1]
    try:
        newest_dt = datetime.fromisoformat(newest.created_at)
    except (ValueError, TypeError):
        return history  # Can't parse, skip filtering

    cutoff_seconds = recency_hours * 3600
    guaranteed = history[-min_count:]
    older = history[:-min_count]

    within_window = []
    for msg in older:
        try:
            msg_dt = datetime.fromisoformat(msg.created_at)
            if (newest_dt - msg_dt).total_seconds() <= cutoff_seconds:
                within_window.append(msg)
        except (ValueError, TypeError):
            within_window.append(msg)  # Keep if unparseable

    result = within_window + guaranteed
    if len(result) < len(history):
        logger.info(
            "Recency window trimmed DB context from %d to %d messages "
            "(min=%d, window=%.1fh, dropped=%d older)",
            len(history), len(result), min_count, recency_hours,
            len(history) - len(result),
        )
    return result


def _build_talk_api_context(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None",
    user_tz: ZoneInfo | None = None,
) -> tuple[str | None, set[int]]:
    """Build conversation context from the local Talk message cache.

    Reads cached messages (populated by the poller), enriches bot messages with
    task metadata from the DB, and formats for the prompt.

    Returns (formatted_context, task_ids_included). task_ids_included is the
    set of DB task IDs whose results appear in the returned context — callers
    use it to deduplicate against memory recall.
    """
    from .context import _parse_reference_id

    limit = config.conversation.talk_context_limit
    if conn is not None:
        raw_messages = db.get_cached_talk_messages(conn, task.conversation_token, limit=limit)
    else:
        with db.get_db(config.db_path) as temp_conn:
            raw_messages = db.get_cached_talk_messages(temp_conn, task.conversation_token, limit=limit)

    if not raw_messages:
        logger.info("No messages from Talk API for token %s", task.conversation_token)
        # No reply-to fallback here any more: `build_prompt` renders the
        # citation into the request section unconditionally, so emitting it as
        # the whole conversation context would quote the same snapshot twice.
        return None, set()

    # Collect task IDs from referenceIds for batch metadata lookup
    task_ids = []
    for msg in raw_messages:
        ref_id = msg.get("referenceId") or None
        tid, tag = _parse_reference_id(ref_id)
        if tid is not None and tag == "result":
            task_ids.append(tid)

    # Batch lookup task metadata
    task_metadata: dict[int, dict] = {}
    if task_ids:
        if conn is not None:
            task_metadata = db.get_task_metadata_for_context(conn, task_ids)
        else:
            with db.get_db(config.db_path) as temp_conn:
                task_metadata = db.get_task_metadata_for_context(temp_conn, task_ids)

    # Build filtered TalkMessage list
    talk_messages = build_talk_context(
        raw_messages, config.talk.bot_username, task_metadata,
    )

    if not talk_messages:
        logger.info("No relevant Talk messages after filtering for task %d", task.id)
        return None, set()

    # Cap at lookback_count, then apply recency window
    lookback = config.conversation.lookback_count
    if len(talk_messages) > lookback:
        talk_messages = talk_messages[-lookback:]
    talk_messages = _apply_recency_window_talk(talk_messages, config)

    # Reply parent handling: check if replied-to message is in the fetched history
    reply_parent_talk_msg = None
    if task.reply_to_talk_id:
        for tm in talk_messages:
            if tm.message_id == task.reply_to_talk_id:
                reply_parent_talk_msg = tm
                break
        # No synthetic stand-in when the parent isn't in the fetched window.
        # `build_prompt` quotes the snapshot into the request section
        # unconditionally, so synthesizing a context message from the same
        # string puts it in the prompt twice — and this is the *common* Talk
        # case, since a reply to anything more than a few turns back falls
        # outside the window. The last of the five fallbacks that did this;
        # the other four are gone for the same reason.

    # Select relevant messages (triage routed through the task's brain)
    relevant = select_relevant_talk_context(
        task.prompt, talk_messages, config,
        completer=_build_triage_completer(task, config),
        on_usage=_build_triage_usage_sink(task, config),
    )

    # Ensure reply parent survives triage
    if reply_parent_talk_msg:
        relevant_ids = {m.message_id for m in relevant}
        if reply_parent_talk_msg.message_id not in relevant_ids:
            relevant = [reply_parent_talk_msg] + relevant
            logger.info(
                "Re-added reply parent (talk msg %d) after triage for task %d",
                reply_parent_talk_msg.message_id, task.id,
            )

    if not relevant:
        logger.info("No relevant Talk context selected from %d messages", len(talk_messages))
        return None, set()

    conversation_context = format_talk_context_for_prompt(
        relevant, truncation=config.conversation.context_truncation,
        user_tz=user_tz,
    )
    logger.info(
        "Loaded %d Talk API context messages (%d chars) for task %d",
        len(relevant), len(conversation_context), task.id,
    )
    included_task_ids = {m.task_id for m in relevant if m.task_id}
    return conversation_context, included_task_ids


def _build_db_context(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None",
    user_tz: ZoneInfo | None = None,
) -> tuple[str | None, set[int]]:
    """Build conversation context from the DB (original approach).

    Used for email tasks and as fallback when Talk API is unavailable.

    Returns (formatted_context, task_ids_included). task_ids_included is the
    set of DB task IDs whose results appear in the returned context — callers
    use it to deduplicate against memory recall.
    """
    # Exclude background / non-conversational task types from conversation
    # context. subtask/heartbeat are the forward guard (defense-in-depth on top
    # of the nonconversational_transcript_cleanup_v1 migration) against any
    # future path that stores a non-conversational user row — the model must
    # never read a cron/subtask post back as prior user conversation.
    _exclude_types = ["scheduled", "briefing", "subtask", "heartbeat"]

    # Who each turn is attributed to (ISSUE-226). Every user, not just the task's
    # own: a shared room's history carries co-members' turns, and checking theirs
    # against this user's addresses would mark them external for no reason.
    own_email_addresses = _user_email_address_map(config)

    if conn is not None:
        history = db.get_conversation_history(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.lookback_count,
            exclude_source_types=_exclude_types,
            user_email_addresses=own_email_addresses,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            history = db.get_conversation_history(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.lookback_count,
                exclude_source_types=_exclude_types,
                user_email_addresses=own_email_addresses,
            )

    # Inject recent scheduled/briefing tasks in the same channel — these are
    # deliberately re-surfaced (cron/briefing output the user may reference)
    # even though get_conversation_history excludes them. But subtask/heartbeat
    # must stay excluded here too: a subtask's synthetic orchestration prompt is
    # not a user utterance, and re-injecting it would read back as prior user
    # conversation (the LLM-context isolation invariant — canonical-room-
    # transcript spec). So hard-exclude them from this re-surfacing path as well.
    _prev_exclude = ["subtask", "heartbeat"]
    if conn is not None:
        prev_tasks = db.get_previous_tasks(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.previous_tasks_count,
            exclude_source_types=_prev_exclude,
            user_email_addresses=own_email_addresses,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            prev_tasks = db.get_previous_tasks(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.previous_tasks_count,
                exclude_source_types=_prev_exclude,
                user_email_addresses=own_email_addresses,
            )

    if prev_tasks:
        history_ids = {msg.id for msg in history}
        injected = 0
        for prev in prev_tasks:
            if prev.id not in history_ids:
                history.append(prev)
                injected += 1
        if injected:
            history.sort(key=lambda m: (m.created_at, m.id))
            logger.info(
                "Included %d previous tasks (excluded source_type) in context for task %d",
                injected, task.id,
            )

    logger.debug("Context lookup: token=%s, history_count=%d", task.conversation_token, len(history))

    # Apply recency window before selection
    history = _apply_recency_window_db(history, config)

    if history:
        reply_parent_msg = None
        if task.reply_to_talk_id and task.conversation_token:
            history, reply_parent_msg = _ensure_reply_parent_in_history(
                task, history, config, conn if conn is not None else None,
            )

        relevant = select_relevant_context(
            task.prompt, history, config,
            completer=_build_triage_completer(task, config),
            on_usage=_build_triage_usage_sink(task, config),
        )

        if reply_parent_msg:
            relevant_ids = {msg.id for msg in relevant}
            if reply_parent_msg.id not in relevant_ids:
                relevant = [reply_parent_msg] + relevant
                logger.info(
                    "Re-added reply parent (task %d) after triage dropped it for task %d",
                    reply_parent_msg.id, task.id,
                )

        if relevant:
            conversation_context = format_context_for_prompt(
                relevant, truncation=config.conversation.context_truncation,
                user_tz=user_tz,
            )
            logger.info(
                "Loaded %d context messages (%d chars) for task %d",
                len(relevant), len(conversation_context), task.id,
            )
            included_task_ids = {msg.id for msg in relevant}
            return conversation_context, included_task_ids
        else:
            logger.info("No relevant context selected from %d messages", len(history))
    else:
        # The citation is no longer stood up as the whole context here — the
        # request section carries it either way. See `_build_talk_api_context`.
        logger.info("No conversation history found for token %s", task.conversation_token)

    return None, set()


def _apply_bot_name(content: str, config: Config) -> str:
    """Replace {BOT_NAME} placeholder with config.bot_name in loaded content."""
    return content.replace("{BOT_NAME}", config.bot_name).replace("{BOT_DIR}", config.bot_dir_name)


def load_emissaries(config: Config) -> str | None:
    """Load the emissaries constitutional document (global only, not user-overridable)."""
    if not config.emissaries_enabled:
        return None
    config_dir = config.skills_dir.parent
    emissaries_path = config_dir / "emissaries.md"
    if emissaries_path.exists():
        return emissaries_path.read_text().strip()
    return None


def load_persona(config: Config, user_id: str | None = None) -> str | None:
    """Load persona file, checking user workspace first, then global.

    User workspace PERSONA.md (in their Nextcloud config dir) takes precedence
    over the global config/istota.md file.

    The user's copy is read through ``read_user_config_file``, not a plain
    ``read_text``: it sits in a directory bound read-write into that user's own
    sandbox, this runs host-side with the daemon's filesystem view, and what it
    returns becomes prompt text on the next task (ISSUE-339). A refused read
    falls through to the global persona, which is the same outcome as having no
    per-user file — and the global one lives in ``config/``, which is bound
    into no sandbox at any path.
    """
    # Try user workspace persona first
    if user_id and config.use_mount:
        content = read_user_config_file(config, user_id, "PERSONA.md")
        if content and content.strip():
            return _apply_bot_name(content.strip(), config)

    # Fall back to global persona
    config_dir = config.skills_dir.parent
    persona_path = config_dir / "persona.md"
    if persona_path.exists():
        return _apply_bot_name(persona_path.read_text().strip(), config)
    return None


def load_channel_guidelines(
    config: Config, source_type: str, user_id: str | None = None,
) -> str | None:
    """Load channel-specific guidelines, substituting the doc placeholders.

    ``{user_id}`` joins ``{BOT_NAME}``/``{BOT_DIR}`` here so a guideline can
    name a concrete workspace path — web.md's file-handover link needs one, and
    a literal ``{user_id}`` reaching the model is worse than no example. Skill
    bodies already substitute it; this brings guidelines in line with the set
    AGENTS.md documents.

    The substituted value is flattened. The *file* is an instruction block and
    stays multiline — its structure is lines — but a scalar interpolated into
    it is not part of that structure, and the result is now `build_prompt`'s
    system half: a `user_id` carrying `\\n\\n## Important rules\\n\\n1. …`
    renders a second rules heading inside the message compaction never
    touches. Same rule, and the same `_one_line`, as the header scalars there.
    """
    config_dir = config.skills_dir.parent
    guidelines_path = config_dir / "guidelines" / f"{source_type}.md"
    if guidelines_path.exists():
        text = _apply_bot_name(guidelines_path.read_text().strip(), config)
        if user_id:
            text = text.replace("{user_id}", _one_line(user_id))
        return text
    return None


def _recall_memories(
    config: Config,
    conn: "db.sqlite3.Connection | None",
    task: db.Task,
    prompt: str,
    skip_memory: bool = False,
    exclude_task_ids: set[int] | None = None,
) -> str | None:
    """BM25 search using the task's *effective* prompt. Independent of triage.

    `prompt` is passed explicitly rather than read off `task` because the query
    is the enriched string — typed request plus audio transcript plus OCR
    context — and `task.prompt` carries only the first two. Reading the field
    would make this the one retrieval pass blind to an attachment's text.

    `exclude_task_ids` is the set of task IDs already included as conversation
    history; recall drops conversation chunks for those tasks so the same
    content doesn't appear twice in the prompt.
    """
    if not config.memory_search.enabled or not config.memory_search.auto_recall:
        return None
    if skip_memory:
        return None

    try:
        from .memory.search import search
    except ImportError:
        return None

    include_ids: list[str] = []
    source_types = ["memory_file", "conversation"]
    if task.conversation_token:
        include_ids.append(f"channel:{task.conversation_token}")
        # Channel namespace also has dated channel_memory and durable
        # channel_memory_durable (from CHANNEL.md). Include both.
        source_types += ["channel_memory", "channel_memory_durable"]

    try:
        if conn is not None:
            results = search(
                conn, task.user_id, prompt,
                limit=config.memory_search.auto_recall_limit,
                source_types=source_types,
                include_user_ids=include_ids or None,
                exclude_conversation_task_ids=exclude_task_ids or None,
                recency_half_life_days=config.memory_search.recency_half_life_days,
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                results = search(
                    temp_conn, task.user_id, prompt,
                    limit=config.memory_search.auto_recall_limit,
                    source_types=source_types,
                    include_user_ids=include_ids or None,
                    exclude_conversation_task_ids=exclude_task_ids or None,
                    recency_half_life_days=config.memory_search.recency_half_life_days,
                )
    except Exception:
        logger.debug("Memory recall search failed", exc_info=True)
        return None

    if not results:
        return None

    parts = []
    for r in results:
        snippet = r.content[:300].strip()
        parts.append(f"- [{r.source_type}] {snippet}")
    return "\n".join(parts)


def _recall_playbooks(
    config: Config,
    conn: "db.sqlite3.Connection | None",
    task: db.Task,
    prompt: str,
    skip_memory: bool = False,
) -> str | None:
    """Recall learned playbooks relevant to the task's effective prompt (Part B).

    Mirrors `_recall_memories` but queries only `source_type="playbook"`,
    user-scoped, top-`playbooks.recall_limit`. Gated on `playbooks.enabled`,
    skipped for automated tasks (briefings/scheduled, like all personal memory)
    and when a selected skill set `skip_memory`.
    """
    if not config.playbooks.enabled:
        return None
    if skip_memory or _is_automated_task(task):
        return None

    try:
        from .memory.search import search
    except ImportError:
        return None

    try:
        if conn is not None:
            results = search(
                conn, task.user_id, prompt,
                limit=config.playbooks.recall_limit,
                source_types=["playbook"],
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                results = search(
                    temp_conn, task.user_id, prompt,
                    limit=config.playbooks.recall_limit,
                    source_types=["playbook"],
                )
    except Exception:
        logger.debug("Playbook recall search failed", exc_info=True)
        return None

    if not results:
        return None

    # Stamp use-recency onto each recalled playbook file so the sleep cycle's
    # retention prune keys on last-use, not last-write (ISSUE-174 Concern 3).
    now = time.time()
    for r in results:
        source_id = getattr(r, "source_id", None)
        if not source_id:
            continue
        try:
            os.utime(source_id, (now, now))
        except OSError as e:
            # A no-op utime (e.g. an rclone FUSE mount that rejects utimens)
            # silently reverts Concern 3 to write-based aging — log it so the
            # degradation is visible rather than invisible.
            logger.debug("playbook mtime stamp failed for %s: %s", source_id, e)
            continue

    parts = []
    for r in results:
        snippet = r.content.strip()
        parts.append(f"- {snippet}")
    return "\n\n".join(parts)


def _apply_memory_cap(
    config: Config,
    user_memory: str | None,
    dated_memories: str | None,
    channel_memory: str | None,
    recalled_memories: str | None,
    knowledge_facts: str | None = None,
    playbooks: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Truncate memory components if total exceeds max_memory_chars.

    Truncation order: recalled → knowledge facts → dated → playbooks →
    (warn about user/channel). Playbooks are truncated late because an
    actionable procedure is higher-value than recalled snippets, dated context,
    or KG triples (cap-ladder open question resolved in favour of protecting
    playbooks). Returns the updated components.
    """
    cap = config.max_memory_chars
    if cap <= 0:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks

    total = (
        len(user_memory or "")
        + len(dated_memories or "")
        + len(channel_memory or "")
        + len(recalled_memories or "")
        + len(knowledge_facts or "")
        + len(playbooks or "")
    )
    if total <= cap:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks

    over = total - cap

    # Truncate recalled first
    if recalled_memories and over > 0:
        if over >= len(recalled_memories):
            over -= len(recalled_memories)
            recalled_memories = None
        else:
            recalled_memories = recalled_memories[:len(recalled_memories) - over] + "\n...[truncated]"
            over = 0

    # Then knowledge facts
    if knowledge_facts and over > 0:
        if over >= len(knowledge_facts):
            over -= len(knowledge_facts)
            knowledge_facts = None
        else:
            knowledge_facts = knowledge_facts[:len(knowledge_facts) - over] + "\n...[truncated]"
            over = 0

    # Then dated
    if dated_memories and over > 0:
        if over >= len(dated_memories):
            over -= len(dated_memories)
            dated_memories = None
        else:
            dated_memories = dated_memories[:len(dated_memories) - over] + "\n...[truncated]"
            over = 0

    # Then playbooks (most protected of the recall-tier sources)
    if playbooks and over > 0:
        if over >= len(playbooks):
            over -= len(playbooks)
            playbooks = None
        else:
            playbooks = playbooks[:len(playbooks) - over] + "\n...[truncated]"
            over = 0

    if over > 0:
        logger.warning(
            "Memory cap (%d) exceeded by %d chars after truncating recalled/dated/playbooks; "
            "user_memory=%d, channel_memory=%d chars remain",
            cap, over, len(user_memory or ""), len(channel_memory or ""),
        )

    return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks


# ---------------------------------------------------------------- rules block

# The `## Important rules` list, assembled once for everybody.
#
# It used to be two f-strings, one per privilege level, and they drifted the way
# two copies of a list always do: a rule added to one reached half the
# deployment with nothing anywhere to say so. Rule 3c (ISSUE-345) was written
# into both by hand, and the goldens could not have caught a miss — each is
# regenerated from whatever its own block holds, so both would still have
# matched.
#
# Two things had to be decided to merge them, and both are visible in the list
# rather than inferred.
#
# **The labels stay hand-written.** Generating `1.` … `N.` would flatten the
# 3/3a/3b/3c family, and that family is deliberate: a lettered rule is an
# insertion under its parent, which is what says 3a-3c are all "when a step
# cannot be done here". So a rule carries its own label and the list carries
# only the order.
#
# **The admin-only rule takes a letter too**, `6a.`, where it used to be a plain
# `7.` that pushed the whole admin tail one ahead of the standard user's. That
# offset was the only reason the two lists could not share a numbering. Reusing
# the existing convention keeps the rule where it was — beside `6.`, which is
# also about what the user ends up reading — and leaves 1 through 10 meaning the
# same thing at both privilege levels.
#
# Three entries still differ by privilege (the access rule, the database rule,
# and the skill-CLI rule, which names subtasks only where they exist) and one is
# admin-only. Everything else is written once.


def _db_rule(*, is_admin: bool, db_masked: bool) -> str:
    """Rule 3, over two axes: whether the databases are masked, and privilege.

    Where the masks are in place the rule can state a fact — there is nothing
    to open. Where they are not (`effective_sandboxing` is False) it has to fall
    back to a prohibition, because telling the model there is nothing there
    would be false, and a false boundary claim is worse than no claim. That is
    what ISSUE-237 corrected.
    """
    if db_masked and is_admin:
        return (
            "Istota's databases are not on your filesystem — the directories "
            "that hold them are empty here, so there is nothing for `sqlite3` or "
            "Python's `sqlite3` to open and no path worth hunting for. Every "
            "read goes through a skill CLI (e.g. `istota-skill kv get`, "
            "`istota-skill tasks status`), which runs outside this sandbox and "
            "returns only your own data; every write goes through one, or via "
            "deferred JSON files in $ISTOTA_DEFERRED_DIR."
        )
    if db_masked:
        return (
            "Istota's databases are not on your filesystem — the directories "
            "that hold them are empty here, so there is nothing for `sqlite3` or "
            "Python's `sqlite3` to open. All database access, read and write, "
            "goes through the skill CLI commands, which run outside this "
            "sandbox and return only your own data, or through the bot's "
            "scheduler."
        )
    if is_admin:
        return (
            "Never open a database file directly — not to write, and not to "
            "read. This deployment has no filesystem sandbox, so an attempt may "
            "well succeed and hand you every user's rows; that it works is not "
            "permission. Every read goes through a skill CLI (e.g. "
            "`istota-skill kv get`, `istota-skill tasks status`), which returns "
            "only your own data; every write goes through one, or via deferred "
            "JSON files in $ISTOTA_DEFERRED_DIR."
        )
    return (
        "Never open a database file directly — not to write, and not to "
        "read. This deployment has no filesystem sandbox, so an attempt may "
        "well succeed; those files hold every user's data and none of it is "
        "yours to read this way. All database access, read and write, goes "
        "through the skill CLI commands, which return only your own data, "
        "or through the bot's scheduler."
    )


def _one_line(value: str) -> str:
    """No interpolated value may contain a line break.

    The whole block's structure is carried by line prefixes — the joiner puts
    one rule per line and the label at the front of it — so a value carrying
    `\n1. ` forges a rule in the model-facing prompt. Rules 1 and 2 interpolate
    a user id, a filesystem path and stored email addresses, none of which is
    validated for this anywhere upstream. The exposure predates the merge (the
    two f-strings had it identically); it is closed here because this is now the
    one place that knows the structure depends on it.

    Collapsed to a space rather than refused: this runs on the prompt-assembly
    path, where raising means no task at all, and a mangled user id in a rule is
    a far better failure than a forged rule or a dead deployment.
    """
    return value.replace("\r", " ").replace("\n", " ")


def build_rules_section(
    *,
    is_admin: bool,
    user_id: str,
    scoped_path: str,
    user_email_addresses: list[str] | None,
    db_masked: bool,
) -> str:
    """The `## Important rules` block, for either privilege level.

    `scoped_path` is read only for a standard user, and rule 1 is where the two
    blocks used to disagree about what the rule even means: an admin is told
    whose resources are theirs, a standard user is told which directory is.
    """
    user_id = _one_line(user_id)
    scoped_path = _one_line(scoped_path)
    addresses = (
        ", ".join(_one_line(a) for a in user_email_addresses)
        if user_email_addresses
        else "none configured"
    )

    rules: list[tuple[str, str]] = [
        (
            "1.",
            f"Only access resources that belong to user '{user_id}' as listed above."
            if is_admin
            else f"You can ONLY access files under {scoped_path}. You do NOT have access to the task database or other users' data.",
        ),
        (
            "2.",
            "For sensitive actions, ask for confirmation EXCEPT:\n"
            f"   - Emails to the user's own addresses ({addresses}) do NOT need confirmation\n"
            "   - Emails to external addresses DO need confirmation\n"
            "   - Modifying calendars, deleting files, sharing externally need confirmation",
        ),
        ("3.", _db_rule(is_admin=is_admin, db_masked=db_masked)),
        (
            "3a.",
            "When you need something your environment can't do — a credentialed request, a network call the allowlist blocks, a read of system state — the answer is a skill CLI subcommand. `istota-skill` runs with credentials and network access this task does not have, and hands you the value synchronously. Check `istota-skill <name> --help` for one before building a workaround out of scheduled jobs, subtasks or file polling; subtasks and jobs are handoffs and never return a value to you. If nothing covers it, say what is missing instead of improvising."
            if is_admin
            else "When you need something your environment can't do — a credentialed request, a network call the allowlist blocks, a read of system state — the answer is a skill CLI subcommand. `istota-skill` runs with credentials and network access this task does not have, and hands you the value synchronously. Check `istota-skill <name> --help` for one before building a workaround out of scheduled jobs or file polling; a scheduled job is a handoff and never returns a value to you. If nothing covers it, say what is missing instead of improvising.",
        ),
        (
            "3b.",
            "Only wait on out-of-band work when it plausibly finishes within about two minutes — you hold a worker slot for the whole wait, and a scheduled job cannot start before the next minute boundary. When you do wait, never redirect the probe's stderr: `2>/dev/null` makes a broken command indistinguishable from \"not ready yet\" and runs the loop to its full length. Abort after two consecutive non-zero exits, and cap the total wait. If the work might take longer, hand off and answer in a later turn.",
        ),
        (
            "3c.",
            "A step that failed goes in the deliverable, not only in your reasoning. On unattended work — scheduled jobs, briefings, digests, reminders — never reconstruct a broken script's work by hand: do not monkeypatch it, import its functions and drive them yourself, or feed it values recovered from memory search. The artifact you deliver is the only surface anyone sees there, so producing the expected one anyway hides the breakage for as long as someone keeps repairing it. Where the result was asked for in this task, standing in for a broken step by hand is allowed only if the reply says plainly what failed and what you put in its place. Being resourceful means finding the answer, never quietly replacing a step that is broken.",
        ),
        (
            "4.",
            "After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.",
        ),
        ("5.", "Never edit or create files in your own source directory."),
        (
            "6.",
            "Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.",
        ),
    ]

    if is_admin:
        rules.append((
            "6a.",
            "Your execution JSONL logs (full conversation traces including subagent output) are stored under ~/.claude/projects/. If a user reports missing or truncated output from a previous task, search these logs for the full assistant message content.",
        ))

    rules += [
        (
            "7.",
            "Ignore the `currentDate` value in any auto-memory block — it is rendered in the host's UTC clock and may be off by one day from local time. Use the `Today's date`, `Current time`, and `User timezone` lines at the top of this prompt as the authoritative source for \"today\".",
        ),
        (
            "8.",
            "Dates that appear in fetched content (RSS/feed items, web pages, emails, file contents) are publication or authorship dates — never infer the current date from them. The `Today's date` and `Current time` lines above are the only authoritative source for \"today\", even when fetched content shows a later date (e.g. a feed item already stamped tomorrow in another timezone).",
        ),
        (
            "9.",
            "When computing elapsed time between two timestamps (\"X ago\", \"merged N hours ago\", etc.), normalize both to ISO 8601 UTC first and subtract the full timestamps. Do not subtract clock-face hours/minutes by hand — that gives the wrong answer when the timestamps straddle a UTC midnight, end-of-month, or DST boundary. The `Current UTC` line above is your reference for \"now\".",
        ),
        (
            "10.",
            "Before invoking a skill CLI subcommand, confirm the subcommand exists — do not guess subcommand names from memory. If the skill's documentation is not included in this prompt, run `istota-skill <name> --help` first and use only a subcommand it lists. A failed guess wastes a turn; checking once is cheaper.",
        ),
    ]

    body = "\n".join(f"{label} {text}" for label, text in rules)
    return f"## Important rules\n\n{body}"


# What the executor can truthfully say about a prepared image, and no more.
# "Attached" alone is not evidence of sight, so a prepared image is marked as
# distinct from an omitted one — but the line stops short of asserting *how*
# the pixels arrive, or that they arrived at all.
#
# The spec asks this line for `vision supplied` / `vision requires Claude Code
# Read`, and neither is a fact this layer holds. Prompt assembly runs several
# hundred lines before the brain is constructed, so the only thing available
# here is the *configured* kind — and three ordinary states make it wrong:
# a native model that declares no vision support (the brain sends a named
# omission instead of the image), an availability-breaker skip-primary that
# routes to the fallback before the primary is ever called, and an in-attempt
# reroute, both of which run a brain the prompt did not name. Each of those
# turns the line into a claim of sight for an image the model cannot see, which
# is precisely the failure the whole change exists to remove.
#
# The layer that *can* be right says it instead, in the same prompt: the CLI
# brains prepend the `Read` directive naming every path, and the native brain
# emits one image block per image or one named omission per image. So this line
# distinguishes prepared from omitted, and delivery is stated by whoever
# delivers.
VISION_PREPARED = "prepared for vision"


def image_attachment_status(prep: ImagePreparation) -> "dict[str, str]":
    """Map each recognized image attachment to its one-phrase status.

    An omitted image carries its own reason, which `prepare_image_attachments`
    already wrote as a bounded model-facing notice.
    """
    status: dict[str, str] = {}
    for block in prep.ocr_blocks:
        if block.kind == KIND_OMITTED and block.path:
            status[block.path] = f"not sent to the model: {block.detail}"
    for image in prep.images:
        status[str(image.path)] = VISION_PREPARED
    return status


def _attachment_line(
    attachment: str, config: Config, status: "dict[str, str] | None"
) -> str:
    """One `Attached files` line: the path, where it is, and its vision status.

    The location label is per line rather than per section. It used to be one
    `any(att.startswith("/") …)` predicate over the whole list, which relabels
    the entire section "local paths" the moment a single entry becomes
    absolute — presenting a workspace-relative PDF as a local path. Normalizing
    strictly more images than before makes that mixed list the common case
    rather than the odd one.
    """
    if attachment.startswith("/"):
        where = "local path"
    elif config.storage_is_nextcloud:
        where = "in Nextcloud, access via rclone"
    else:
        where = "workspace-relative"
    note = (status or {}).get(attachment, "")
    return f"  - {attachment} [{where}]" + (f" — {note}" if note else "")


@dataclass(frozen=True)
class ComposedPrompt:
    """The two halves of a task prompt, which are not interchangeable.

    ``system`` is Istota's standing instructions — identity, execution
    constraints, emissaries, persona, the workspace vocabulary, the callable
    tool surface, the rules, the response guidelines and the skill bodies. It
    reaches the brain *outside* the compactable message history, so it is still
    there verbatim after the model's context has been summarized (ISSUE-375).

    ``user`` is the task material a compaction summary is meant to carry
    forward instead: retrieved memory, conversation and confirmation history,
    and the request itself.

    A frozen dataclass rather than a tuple, because the two strings have
    different authority and swapping them must be visible at the call site
    rather than being a silent argument-order mistake.
    """

    system: str
    user: str


#: The dry-run rendering, in fixed labels rather than prose, so nothing has to
#: guess where one half ends. Read by `tests/test_prompt_golden.py`, which
#: snapshots both halves of one assembly into one file.
DRY_RUN_PROMPT_HEADER = "[DRY RUN] Would execute with prompts:"
PROMPT_SYSTEM_LABEL = "===== SYSTEM ====="
PROMPT_USER_LABEL = "===== USER ====="


def render_composed_prompt(prompt: ComposedPrompt) -> str:
    """Both halves, labelled, for a dry run and for the goldens.

    Takes the value type. It never parses a joined string back into parts —
    that would make the delimiters load-bearing in the other direction, and a
    prompt containing one of them would then re-split wrongly.
    """
    return (
        f"{PROMPT_SYSTEM_LABEL}\n{prompt.system}\n\n"
        f"{PROMPT_USER_LABEL}\n{prompt.user}"
    )


def build_prompt(
    task: db.Task,
    user_resources: list[db.UserResource],
    config: Config,
    skills_doc: str | None = None,
    conversation_context: str | None = None,
    user_memory: str | None = None,
    discovered_calendars: list[tuple[str, str, bool]] | None = None,
    user_email_addresses: list[str] | None = None,
    dated_memories: str | None = None,
    channel_memory: str | None = None,
    skills_changelog: str | None = None,
    is_admin: bool = True,
    emissaries: str | None = None,
    source_type: str | None = None,
    output_target: str | None = None,
    recalled_memories: str | None = None,
    playbooks: str | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    skills_index: str | None = None,
    confirmation_context: str | None = None,
    knowledge_facts: str | None = None,
    conn: "db.sqlite3.Connection | None" = None,
    effective_prompt: str | None = None,
    attachment_status: "dict[str, str] | None" = None,
) -> ComposedPrompt:
    """Build a task's prompt, split by authority rather than by size.

    Returns a `ComposedPrompt`: standing instructions in ``system``, task
    material in ``user``. The split is not about how often a value changes —
    it is about whether the value has to remain verbatim for the life of the
    task. Anything a rule still names after the model's history has been
    compacted away belongs in ``system``, which is why the four time lines and
    the accessible-resources section are there despite reading as task facts:
    rules 1, 7, 8 and 9 point at them, those rules survive compaction, and a
    surviving instruction pointing at deleted material is ISSUE-375 again.

    **No line in the system half may point at material in the user half.** The
    single string this replaced was written as one document and referred to
    itself throughout. Four references were live at the split; three are
    answered by putting the referent in the system half, and the fourth — the
    group-conversation line — is answered by dropping the word "below", since
    its referent is conversation context and belongs in the user half.

    The split also raises several interpolated scalars from a user message to a
    system one, so every one of them goes through `_one_line` before it is
    rendered into a header. That is structural sanitation and not instruction
    sanitation: the multiline instruction blocks (persona, emissaries,
    guidelines, changelog, skill overlays) are untouched, because their
    structure *is* lines.

    This is the only production assembly function. There are deliberately no
    parallel ``build_system_prompt`` / ``build_user_prompt`` entry points: they
    would have to repeat every conditional in here, and the two copies would
    drift into a layer rendered twice or not at all.

    Pass ``conn`` to let the per-task timezone lookup reuse an existing
    framework-DB connection instead of opening a throwaway one.

    ``effective_prompt`` is the enriched request — typed text plus audio
    transcript plus rendered OCR context — and it is what ``## User's request``
    renders. It is a parameter rather than a mutation of ``task.prompt``
    because the same string has to reach skill selection and the three
    retrieval passes, and a field read in five places is how those drift apart.
    ``None`` falls back to ``task.prompt``, which is what every caller outside
    ``execute_task`` passes.

    ``attachment_status`` maps an attachment path to the one-phrase status
    shown after it: ``VISION_PREPARED``, or the reason it was left out.
    "Attached" alone is not evidence of sight, so a prepared image is marked as
    distinct from an omitted one here — and no further, since *how* the pixels
    arrive is not a fact assembly holds. See ``VISION_PREPARED`` for why, and
    for which layer says the rest.
    """
    # Stage 3a (Resources sunset): resources are no longer a per-task prompt
    # surface. The enumerated Nextcloud Folders / TODO Files / Notes /
    # Reminders / Calendar-fallback sections are replaced by a single static
    # workspace-layout line; the model finds files by convention + the tools
    # it already has (Glob/Read over the bound workspace). The folder
    # bind-mount loop (build_sandbox_command / native_fs_roots) still mounts
    # out-of-workspace paths into the sandbox; CalDAV discovery still drives
    # the Calendar section; the web root stays config-driven.

    # Every scalar interpolated into the system half is flattened first. The
    # split raised this whole document from a user message to a system one, so
    # a value carrying a line break no longer forges a line in task material —
    # it forges a standing instruction, in the one message compaction never
    # touches. `_one_line` collapses rather than refuses, for the reason it
    # gives: this runs on the assembly path, where raising means no task at all.
    # Nothing upstream validates any of these — the user id and conversation
    # token come off a task row, the source and output target off routing, the
    # timezone label off a per-user profile.
    display_bot_name = _one_line(config.bot_name)
    display_user_id = _one_line(task.user_id)

    resource_sections = []

    if config.use_mount:
        resource_sections.append(
            f"Your workspace is at Users/{display_user_id}/, containing "
            f"shared/, inbox/, memories/, and your bot dir "
            f"({config.bot_dir_name}/). Notes live in {config.bot_dir_name}/notes/."
        )
    else:
        resource_sections.append(
            f"Your workspace is at Users/{display_user_id}/."
        )

    # Calendars stay discovery-driven (CalDAV); the resource-typed fallback
    # is gone.
    if discovered_calendars:
        # The calendar name and URL are the least trusted scalars in this
        # block and the only ones that come off a remote server: a *shared*
        # calendar's display name is set by whoever shared it, and CalDAV puts
        # no constraint on it. One list item per line, so a newline in a name
        # forges a line inside `## User's accessible resources` — the section
        # rule 1 names, in the half that survives compaction.
        cal_list = "\n".join(
            f"  - {_one_line(name)}: {_one_line(url)} "
            f"({'read/write' if writable else 'read-only'})"
            for name, url, writable in discovered_calendars
        )
        resource_sections.append(
            f"Calendars (shared by {display_user_id}):\n{cal_list}"
        )

    resources_text = "\n\n".join(resource_sections)

    # Load emissaries and persona (skipped for neutral output like briefings)
    emissaries_section = ""
    if emissaries and not skip_persona:
        emissaries_section = f"\n\n{emissaries}\n"

    persona_section = ""
    if not skip_persona:
        persona = load_persona(config, user_id=task.user_id)
        if persona:
            persona_section = f"\n\n{persona}\n"

    # Load channel-specific guidelines
    channel_guidelines = load_channel_guidelines(config, task.source_type, task.user_id)
    channel_section = ""
    if channel_guidelines:
        channel_section = f"\n\n## Response format ({task.source_type})\n\n{channel_guidelines}\n"

    # Build attachments section if present
    attachments_text = ""
    if task.attachments:
        att_list = "\n".join(
            _attachment_line(att, config, attachment_status)
            for att in task.attachments
        )
        attachments_text = f"\n\nAttached files:\n{att_list}"

    # Build user memory section
    memory_section = ""
    if user_memory:
        memory_section = f"""
## User memory

The following information has been remembered about this user:

{user_memory}

"""

    # Build knowledge facts section
    knowledge_facts_section = ""
    if knowledge_facts:
        knowledge_facts_section = f"""
## Known facts

Current facts about entities relevant to this user:

{knowledge_facts}

"""

    # Build channel memory section
    channel_memory_section = ""
    if channel_memory:
        channel_memory_section = f"""
## Channel memory

The following information has been remembered about this channel/room:

{channel_memory}

"""

    # Build dated memories section
    dated_memories_section = ""
    if dated_memories:
        dated_memories_section = f"""
## Recent context (from previous days)

{dated_memories}

"""

    # Build recalled memories section
    recalled_section = ""
    if recalled_memories:
        recalled_section = f"""
## Recalled memories (from search)

The following past context was automatically retrieved based on relevance to the current request:

{recalled_memories}

"""

    # Build learned-playbooks section (Part B). Procedures distilled from past
    # successful tasks — guidance, not gospel; verify before acting.
    playbooks_section = ""
    if playbooks:
        playbooks_section = f"""
## Learned Playbooks

Previously-successful approaches to similar tasks, distilled from past work.
Treat these as guidance — verify each step still applies before acting:

{playbooks}

"""

    # Build conversation context section
    context_section = ""
    if conversation_context:
        context_section = f"""
## Conversation context

The following are relevant previous messages from this conversation:

{conversation_context}

"""

    # Build confirmation context section (for re-executed confirmed tasks)
    confirmation_section = ""
    if confirmation_context:
        confirmation_section = f"""## Confirmed action

The user reviewed and approved your previous response. Your previous output:

{confirmation_context}

Execute the action you proposed. If you drafted an email, send it now via `istota-skill email send`. Do not re-draft or ask for confirmation again.

"""

    # Build file access tools section based on the storage backend. Three modes:
    # local folder, Nextcloud via mount, Nextcloud via rclone. Non-admin users
    # get a scoped path restricted to their own directory (server shape only).
    if config.storage_backend == "local":
        # `_one_line` here as well as on the bare user id above: these paths are
        # built from it, so a line break survives `Path` joining into the
        # system half.
        ws_root = _one_line(
            str(config.workspace_root(task.user_id) or config.nextcloud_mount_path)
        )
        file_tools = f"""- Your files live in your workspace at '{ws_root}'. Use standard file tools (Read, Write, Edit, ls, cat).
  - The workspace is the area you manage for the user (memory, notes, inbox, shared files). It is a normal local folder.
  - This install runs locally without a sandbox, so you also have ordinary access to the rest of the machine's filesystem (the user's home, Downloads, etc.). The workspace is your managed area, not the limit of what you can read — stay within what the user asked for."""
    elif config.use_mount:
        if is_admin:
            mount_display = _one_line(str(config.nextcloud_mount_path))
        else:
            mount_display = _one_line(
                str(config.nextcloud_mount_path / "Users" / task.user_id)
            )
        file_tools = f"""- Nextcloud files are mounted at '{mount_display}'
  - List: ls {mount_display}/path/
  - Read: cat {mount_display}/path/file.txt
  - Write: Use standard file operations (Python, bash, etc.)
  - All Nextcloud paths are accessible as local filesystem paths"""
    else:
        remote = _one_line(config.rclone_remote)
        file_tools = f"""- rclone for Nextcloud files: remote name is '{remote}'
  - List: rclone ls {remote}:/path/
  - Copy from NC: rclone copy {remote}:/path/file.txt /tmp/
  - Copy to NC: rclone copy /tmp/file.txt {remote}:/path/"""

    # Browser tool line (only when enabled)
    browser_tool = ""
    if config.browser.enabled:
        browser_tool = "\n- Web browser for JS-rendered pages: istota-skill browse (see browse skill for details)"

    # Web tools line. WebSearch is always allowed; WebFetch goes to everyone
    # unless the operator set `[brain.native.web_fetch] admin_only`, matching
    # `build_allowed_tools`, which is the list `NativeBrain` filters its
    # in-process tool set by. The two have to agree or a task is told to reach
    # for a tool that is not registered.
    #
    # WebSearch only returns result titles + URLs, so reading a page needs a
    # fetch tool — steer that to the browse skill when the browser service is up
    # (it renders JS and reaches arbitrary sites); WebFetch is the lightweight
    # fallback where the caller has it.
    #
    # Where it is withheld and there is no browser service either, the line
    # *says so* rather than being dropped (ISSUE-449). Silence here is what made
    # the old gate hard to live with: the tool was unregistered, the prompt
    # named no page-reading route, and a user asking for a web page got a model
    # that neither read the page nor knew there was anything to explain. The
    # browse skill is not offered as the remedy there because that skill *is*
    # the browser service, so naming it would be a second unavailable route.
    #
    # **The withheld predicate asks the routing question and the tool list does
    # not**, which is a disagreement on purpose rather than drift. `admin_only`
    # only ever removes the *daemon-side* tool, since native is the only brain
    # that builds one; a `claude_code` or `tmux_claude` task keeps the CLI's own
    # `WebFetch` whatever this list says, because that list never reaches the
    # CLI as an allowlist. So a prompt that told a non-admin on a CLI-brain
    # deployment "you have no fetch tool" would be stating an absence that is
    # not there, which is the same defect as naming a tool that is not
    # registered, pointing the other way. `_native_web_fetch_enabled` with its
    # permissive `is_admin` default is exactly the "would this task have had the
    # daemon-side tool" question, and it is asked last so a deployment leaving
    # `admin_only` off never resolves a brain here at all.
    web_fetch_withheld = (
        not is_admin
        and config.brain.native.web_fetch.admin_only
        and _native_web_fetch_enabled(task, config)
    )
    web_search_line = (
        "\n- Web search: WebSearch — finds result titles and URLs; "
        "it does not fetch page content."
    )
    if config.browser.enabled:
        read_line = (
            "\n- Reading web pages: prefer the browse skill (istota-skill browse) — "
            "it renders JavaScript and follows links."
        )
        if not web_fetch_withheld:
            read_line += (
                " Use WebFetch only as a lightweight fallback for simple static pages."
            )
    elif not web_fetch_withheld:
        read_line = (
            "\n- Reading web pages: WebFetch fetches a URL and extracts content "
            "against your prompt."
        )
    else:
        read_line = (
            "\n- Reading web pages: you have no fetch tool — WebFetch is "
            "restricted to administrators on this deployment. Say that plainly "
            "when you are asked to read a page, rather than guessing at what it "
            "says."
        )
    web_tools = web_search_line + read_line

    # Bash runs with `pipefail` on (ISSUE-321), which the model has to be told
    # once because it changes what an exit status means.
    #
    # Eager, here, rather than only in `developer/skill.md` where the rest of
    # the rule lives: `developer` is a menu skill with no `always_include` and
    # no `source_types`, so it reaches the prompt through sticky skills — the
    # *second* turn of a conversation. A first-turn `… | head` therefore
    # returned an unexplained 141 to a model with no rule for it, on the
    # surface where most tool calls happen. Nothing in istota annotates that
    # number on this path either: `shell_exec.SIGPIPE_NOTE` covers the shells
    # istota builds, and the CLI's own Bash tool appends a bare
    # `[exit code: 141]` that nothing here touches.
    bash_tool = (
        "\n- Bash: pipelines run with `pipefail` on, so a pipeline's status is the "
        "first stage that failed rather than the last command — `<runner> … | tail` "
        "reports a failure the run actually had. Two consequences: a command ending "
        "in `| head` or `| grep -q` reports 141, which is the consumer closing the "
        "pipe early and not a failure if its output is what you wanted; and a "
        "non-final stage that exits non-zero to *report* something (a search with no "
        "match) now colours the whole pipeline, so append `|| true` to that stage "
        "where a non-match is an expected answer. `set +o pipefail` opts one command out."
    )

    # CLI skills list (generated from skill index metadata)
    cli_skills_section = cli_skills_text or ""

    # Menu index (eligible skills the model can load on demand via skills show).
    # Appended after the CLI-tools list; empty when the menu is empty.
    if skills_index:
        cli_skills_section = (
            (cli_skills_section + "\n" + skills_index)
            if cli_skills_section else skills_index
        )

    # Compute user's local time. The label is flattened *before* it reaches the
    # three rendered lines rather than only at the `User timezone:` header:
    # rules 7, 8 and 9 name `Current time` and `Today's date` too, and both
    # carry the same label in parentheses.
    user_tz, _raw_tz_str = _resolve_user_tz(config, task.user_id, conn=conn)
    user_tz_str = _one_line(_raw_tz_str)
    user_now = datetime.now(user_tz)
    user_time_str = user_now.strftime("%A, %B %-d, %Y at %-I:%M %p") + f" ({user_tz_str})"
    user_date_str = user_now.strftime("%Y-%m-%d") + f" ({user_tz_str})"
    # UTC anchor for unambiguous elapsed-time arithmetic (ISSUE-091).
    utc_now_str = user_now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # No database path, for anyone. It used to be stated for admins because
    # operator tooling refers to it, hedged with "skill CLIs only" because an
    # unqualified "Database path: …" three lines above the rules reads as an
    # affordance (that hedge was ISSUE-237's fix). The file is masked out of
    # the sandbox, so a path would name something that isn't there — worse than
    # useless, since a failed open reads as a broken command rather than as a
    # boundary. What replaces it is the rule below.
    db_path_line = "Database: reachable only through skill CLIs (no file access)"

    # Whether the masks are actually in place — see `effective_sandboxing` for
    # the shapes where they are not, and `_db_rule` for what the rule says in
    # each case.
    db_masked = effective_sandboxing(config)

    # Explicit privileges line so admin-gated capabilities (subtasks, shared-KV
    # writes, DB access) don't have to be inferred from indirect signals or
    # discovered by hunting through config/source.
    privileges_line = "Privileges: admin" if is_admin else "Privileges: standard user"

    db_tool_line = ""  # DB writes handled via deferred JSON files

    scoped_path = str(config.nextcloud_mount_path / "Users" / task.user_id) if config.use_mount else f"{config.rclone_remote}:/Users/{task.user_id}"
    rules_section = build_rules_section(
        is_admin=is_admin,
        user_id=task.user_id,
        scoped_path=scoped_path,
        user_email_addresses=user_email_addresses,
        db_masked=db_masked,
    )

    # The citation frame. Unconditional whenever a snapshot exists, because the
    # user's message is a response *to* that text and routinely depends on it
    # ("yes, do that"). It lives in the request section rather than in
    # `## Conversation context`, which is an ordered record — "this specific
    # one" is not a record entry. Independent of triage, unlike the parent turn
    # `_ensure_reply_parent_in_history` force-includes: the two overlap by at
    # most the snapshot's 1000 characters, which is the price of the frame
    # always being there.
    reply_quote_section = ""
    if task.reply_to_content:
        quoted = "\n".join(
            f"> {line}" for line in task.reply_to_content.splitlines() or [""]
        )
        reply_quote_section = f"> Replying to:\n{quoted}\n\n"

    # The remaining header scalars, flattened for the reason given where
    # `display_bot_name` and `display_user_id` are bound.
    display_source = _one_line(source_type or task.source_type or "unknown")
    display_output_target = _one_line(output_target or "text")
    display_token = _one_line(task.conversation_token or "none")

    group_chat_line = ""
    if task.is_group_chat:
        # No "below": the conversation context this names is in the user half,
        # which native compaction may replace with a summary. A system line
        # pointing there would become a false statement in a message that
        # survives for the life of the task.
        group_chat_line = f"\nThis is a group conversation. You were @mentioned by '{display_user_id}'. Other participants' messages are visible in conversation context."

    # Per-user plus-addressed email line
    per_user_email_line = ""
    _per_user_email = email_support.per_user_address(config, task.user_id)
    if _per_user_email:
        per_user_email_line = f"\nPer-user email: {_one_line(_per_user_email)}"

    # ---- the system half: standing instructions, verbatim for the whole task
    system = f"""You are {display_bot_name}, a helpful assistant bot. You are responding to a request from user '{display_user_id}'.

Current time: {user_time_str}
Today's date: {user_date_str}
User timezone: {user_tz_str}
Current UTC: {utc_now_str}
Current task ID: {task.id}
Conversation token: {display_token}{group_chat_line}
Source: {display_source}
Output target: {display_output_target}{per_user_email_line}
{db_path_line}
{privileges_line}
{emissaries_section}{persona_section}
## User's accessible resources

{resources_text}

## Available tools

You have access to:
{file_tools}{browser_tool}{web_tools}{bash_tool}
{cli_skills_section}{db_tool_line}
- Email: two commands exist — `istota-skill email send` sends immediately via SMTP, `istota-skill email output` writes a deferred reply file. Use `send` when the user asks you to email someone (this is the common case). Only use `output` when this task arrived as an incoming email (Source: email) and you are composing the reply. See the email skill for details.

{rules_section}
{channel_section}"""

    if skills_changelog:
        system += f"\n\n## What's New in Skills\n\n{skills_changelog}"

    if skills_doc:
        system += f"\n\n{skills_doc}"

    # ---- the user half: task material the compaction summary carries forward
    #
    # Joined block by block rather than by one f-string skeleton: each of these
    # carries its own leading and trailing newlines from the days when they sat
    # between fixed neighbours, and concatenating them raw now leaves a dropped
    # block's separators behind. One blank line between whatever is present.
    user_blocks = [
        memory_section,
        knowledge_facts_section,
        channel_memory_section,
        dated_memories_section,
        recalled_section,
        playbooks_section,
        context_section,
        confirmation_section,
    ]
    user = "".join(
        block.strip("\n") + "\n\n" for block in user_blocks if block.strip()
    )
    user += (
        "## User's request\n\n"
        f"{reply_quote_section}{effective_prompt or task.prompt}{attachments_text}\n"
    )

    return ComposedPrompt(system=system, user=user)


def build_deferred_briefing_prompt(task: db.Task, config: Config) -> str | None:
    """Build a briefing task's full prompt at execution time (ISSUE-143).

    The scheduler creates briefing tasks carrying only the briefing identity
    (``task.briefing_name``) and a placeholder prompt, deferring the slow
    network pre-fetch (news, yfinance, FinViz, IMAP) off the dispatch thread.
    This resolves the live briefing config and timezone and builds the real
    prompt.

    Returns the built prompt, or ``None`` if the briefing can't be resolved or
    the build raises. The caller (``execute_task``) treats ``None`` as a task
    failure so the normal retry/backoff applies, rather than running the model on
    the bare placeholder.
    """
    if not task.briefing_name:
        return None

    # Blocks are the sole content model (retire-legacy-briefing-components).
    # The module path runs the components→blocks migration on first touch, so a
    # legacy components-only briefing is seeded to blocks before assembly. A
    # ``None`` here (module disabled for the user, or a briefing with no blocks
    # after migration) is a misconfiguration: the task fails with the existing
    # quiet retry rather than falling back to a dead legacy generator.
    return _build_module_briefing_prompt(task, config)


def _build_module_briefing_prompt(task: db.Task, config: Config) -> str | None:
    """Assemble a block-grouped briefing prompt from the briefings module.

    Returns the prompt when the module is enabled and the briefing has blocks
    (running the one-time components→blocks migration first); ``None`` when the
    module is disabled for the user, the briefing has no blocks, or any error
    occurs — the caller treats ``None`` as a task failure. Also stashes
    per-block provenance in the task's control directory, which the scheduler
    reads when archiving the rendered briefing.

    Not "a deferred file", which it used to be and which means something
    specific here: a deferred op is model-authored, lives in the writable
    per-user directory, is named in ``_KNOWN_DEFERRED_SUFFIXES`` and is purged
    on retry. This one is none of those.
    """
    try:
        from . import briefings as briefings_module
        from .briefings import ensure_initialised
        from .briefings.generate import assemble_briefing_input
    except Exception:  # noqa: BLE001
        return None

    try:
        ctx = briefings_module.resolve_for_user(task.user_id, config)
    except briefings_module.UserNotFoundError:
        return None  # module disabled for the user → task fails (quiet retry)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings module resolve failed for task %s: %s", task.id, e,
        )
        return None

    try:
        ensure_initialised(ctx, app_config=config)
        with db.get_db(config.db_path) as conn:
            assembled = assemble_briefing_input(
                ctx, task.briefing_name, config, conn=conn,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings module prompt build failed for task %s (%s): %s",
            task.id, task.briefing_name, e,
        )
        return None

    if assembled is None:
        return None  # no blocks after migration → task fails (quiet retry)

    # Stash per-block provenance for the scheduler's archive write.
    #
    # In the control directory rather than the model's working directory, for
    # the reason every framework-authored per-task file is there: the daemon
    # writes it and the daemon reads it back. The old location was per *user*
    # and bound read-write into the sandbox, so a concurrent task of the same
    # user could plant a dangling symlink at a not-yet-started task's filename
    # and redirect this write, which `write_text` would have followed.
    #
    # `ensure_task_control_dir` rather than threading the path through
    # `build_deferred_briefing_prompt`: it is idempotent, and `execute_task`
    # created this exact directory a few dozen lines above the call that
    # reaches here. A second call is one `mkdir(exist_ok=True)` and three mode
    # assertions per briefing task, against a parameter on two signatures with
    # one caller between them.
    #
    # Still best-effort, and the `RuntimeError` this can now raise is caught
    # by the same `except`: the archive works with empty `block_meta`. The
    # *reader* swallows its own failure identically
    # (`scheduler._maybe_archive_briefing`), which is why both ends moved in
    # one commit and why the test asserts the archived provenance is
    # non-empty rather than that the archive ran.
    try:
        import json as _json

        control_dir = ensure_task_control_dir(config, task.user_id, task.id)
        _write_control_file(control_dir / "briefing_meta.json", _json.dumps({
            "briefing_name": task.briefing_name,
            "block_meta": assembled.block_meta,
        }))
    except Exception as e:  # noqa: BLE001
        # Best-effort — the archive still works with empty `block_meta` — but
        # not silent. `execute_task` has already created this directory for
        # this same `(user_id, task_id)` and failed the task if it could not,
        # so the only way the `ensure_` call here raises is that a level was
        # removed, replaced or re-owned *during* the task: the conditions the
        # resolver's guards exist to detect. Swallowing that without a word
        # made it indistinguishable from a briefings-module hiccup, and since
        # the reader swallows its own miss too, the whole operator-visible
        # trace was an archive row with no provenance in it.
        logger.warning(
            "briefing provenance not stashed for task %s (%s): %s",
            task.id, task.briefing_name, e,
        )

    return assembled.prompt


def execute_task(
    task: db.Task,
    config: Config,
    user_resources: list[db.UserResource],
    dry_run: bool = False,
    use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    event_writer: EventWriter | None = None,
    workspace_dir: "Path | None" = None,
) -> tuple[bool, str, str | None, str | None]:
    """
    Execute a task using the configured brain.

    Returns (success, result_text, actions_taken_json, execution_trace_json).

    Args:
        event_writer: Optional task-event sink. When provided, the executor
            adapts the brain's StreamEvent stream into TaskEvents and persists
            them; consumers (Talk, log channel, push, SSE, admin) read those.
            None for dry runs and CLI paths with no observability surface.

    Returns (success, result_or_error).
    """
    # Ensure per-user temp directory exists
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    # And the daemon-owned directory beside it, for the files the framework
    # authors and the model must not touch: both prompt halves and the prepared
    # image attachments. Created here, before anything is written into it.
    #
    # Nothing deletes it. `cleanup_old_temp_files` already recurses into every
    # subdirectory of `temp_dir` — `.control` included — unlinks files past
    # `temp_file_retention_days` and only `rmdir`s a directory that is empty
    # *and* itself past that window, which is the age gate an in-flight task
    # needs. The accepted cost is that unlinking the last file updates the
    # directory's own mtime, so a leaf survives about two retention windows
    # rather than one and the tree carries an inode per task in between —
    # bounded, self-clearing, and on the volume that sweep already manages.
    # A cleanup callback here would also be wrong rather than merely
    # redundant: the briefing metadata is read by the *scheduler*, after this
    # function has returned, inside a bare `except Exception`, so deleting it
    # on the way out would lose every briefing's per-block provenance silently.
    #
    # Fail-closed. A task with no resolvable control directory has nowhere to
    # put its standing instructions, and falling back to the model-writable
    # directory is the exposure this exists to remove.
    #
    # Failed rather than raised, though. Nothing between here and
    # `UserWorker.run`'s catch-all handles an exception — `process_one_task`
    # has no handler of its own — so a raise leaves the row `running` with its
    # heartbeat stopped, recovered only by the stuck-worker sweep
    # `scheduler.worker_stuck_minutes` later, three times over, with the
    # reason nowhere but the daemon log. The conditions are deterministic
    # (a level that is not a directory, one owned by another account, an
    # unresolvable user id), so retrying at all is generous; doing it slowly
    # and anonymously is not. Returning the failure keeps the normal
    # accounting and puts the path and the reason in front of whoever asked.
    try:
        control_dir = ensure_task_control_dir(config, task.user_id, task.id)
    except RuntimeError as exc:
        logger.error("Task %s: %s", task.id, exc)
        return False, str(exc), None, None

    # Build resources: merge config-defined resources with dynamic DB resources
    user_config = config.get_user(task.user_id)
    all_resources = list(user_resources)  # start with passed resources (e.g. shared_file from DB)
    if user_config:
        for rc in user_config.resources:
            all_resources.append(db.UserResource(
                id=0, user_id=task.user_id,
                resource_type=rc.type, resource_path=rc.path,
                display_name=rc.name or None, permissions=rc.permissions,
            ))
    user_resources = all_resources

    # Briefing tasks defer their prompt build to here (ISSUE-143): building a
    # briefing prompt does slow network I/O (news, yfinance, FinViz, IMAP).
    # Running it in the worker instead of the scheduler dispatch loop keeps a
    # slow or unreachable upstream from stalling task dispatch for every room.
    if task.source_type == "briefing" and task.briefing_name:
        built = build_deferred_briefing_prompt(task, config)
        if built:
            task.prompt = built
        else:
            # The prompt couldn't be built (briefing config gone, or the build
            # raised). Fail the task so the normal retry/backoff applies instead
            # of running the model on the bare placeholder and delivering a
            # contentless briefing with no re-run. Briefing failures don't notify
            # the user, so this is a quiet retry.
            msg = f"briefing prompt build failed for {task.briefing_name!r}"
            logger.error("Task %s: %s", task.id, msg)
            return False, msg, None, None

    # The cancellation channel, built once. Both pre-brain passes below poll it
    # and the brain gets the same callable: `scheduler.task_timeout_minutes`
    # covers neither of them, and `BrainRequest.cancel_check` is not in play
    # until the brain runs, so without this `!stop` and the web cancel button
    # are inert for the whole pre-brain window — 900 s of audio plus 180 s of
    # normalization plus the OCR deadline, in sequence on one worker.
    _cancel_check = _make_cancel_check(config, task.id)

    # Pre-transcribe audio attachments so skill selection sees real text.
    #
    # This one *does* still land on `task.prompt`, and deliberately: the
    # scheduler indexes `task.prompt` into conversation memory after
    # `execute_task` returns (`scheduler.py`, `index_conversation`), and an
    # audio-only send arrives as the transport's stand-in "Process the attached
    # file(s)". Dropping the assignment would index that stand-in instead of
    # what the user actually said, for every voice message. Nothing *inside*
    # this function reads the mutated field any more — every consumer below
    # takes `effective_prompt` explicitly — so the implicit contract the
    # mutation used to carry is gone even though the assignment stays.
    enriched_prompt = _pre_transcribe_attachments(
        task.attachments, task.prompt, cancel_check=_cancel_check,
    )
    if enriched_prompt != task.prompt:
        logger.info("Pre-transcribed audio for task %s, enriched prompt for skill selection", task.id)
        task.prompt = enriched_prompt

    # Normalize and OCR the image attachments. Before skill selection and
    # before prompt assembly, so both see the same orientation, the same paths
    # and the same OCR context the model will.
    image_prep = prepare_image_attachments(
        task.attachments,
        control_dir / "attachments",
        task.id,
        cancel_check=_cancel_check,
        # Only where there is a namespace to be outside of. Without effective
        # sandboxing the model reads with the daemon's own filesystem view, so
        # every path is already openable and a copy would buy nothing while
        # replacing the user's own file path with a temp one in the prompt —
        # on the standalone single-user shape, for every image.
        bind_roots=(
            image_bind_roots(config, task, user_temp_dir, control_dir)
            if effective_sandboxing(config)
            else None
        ),
    )
    if image_prep.attachments is not task.attachments:
        # In memory only. Nothing writes this back, so a retry regenerates the
        # renditions and the OCR block from the original attachment rather than
        # stacking a second copy of either.
        task.attachments = image_prep.attachments

    # `effective_prompt` is the one string every consumer below sees: the typed
    # request, plus the audio transcript, plus the rendered OCR context. An
    # attachment is deliberately supplied input, so its text benefits from
    # memory, playbook and knowledge-graph recall the way any other input does
    # — the audio transcript already reached those passes, and holding OCR out
    # of them alone would be a special case with no principle behind it. The
    # residual is recorded rather than designed against: retrieval runs before
    # `untrusted_input` frames anything, so text painted into an image can
    # influence which of the *user's own* stored facts are pulled in. It selects
    # among existing memories rather than introducing new ones, and it is bound
    # by the same per-image and per-task character budgets as the rest.
    effective_prompt = task.prompt
    _ocr_context = render_ocr_context(image_prep.ocr_blocks)
    if _ocr_context:
        effective_prompt = (
            f"{effective_prompt}\n\n{_ocr_context}".strip()
            if effective_prompt.strip()
            else _ocr_context
        )

    # The same content, unframed, for the three retrieval passes. They get the
    # OCR *text* rather than the rendered section, and the difference is not
    # cosmetic: the two BM25 passes join every whitespace token of their query
    # with an implicit AND and pass no `allow_or_fallback`, so the rendered
    # section's ~60-word untrusted-content preamble and its per-image headings
    # would add sixty-odd terms present in no stored chunk and return zero rows
    # for every task carrying an image — silently, because an AND miss is not an
    # error. That is the inverse of the reason OCR reaches retrieval. Framing is
    # for the model, which must see it; a search index has no use for it.
    _ocr_query = ocr_query_text(image_prep.ocr_blocks)
    retrieval_query = (
        f"{task.prompt}\n\n{_ocr_query}".strip() if _ocr_query else task.prompt
    )

    # Select and load relevant skills
    from .skills._loader import (
        load_skill_index, select_skills, load_skills,
        compute_skills_fingerprint, load_skills_changelog,
        effective_disabled_skills,
    )

    is_admin = config.is_admin(task.user_id)

    _bundled_dir = config.bundled_skills_dir
    skill_index = load_skill_index(config.skills_dir, bundled_dir=_bundled_dir)
    user_resource_types = {r.resource_type for r in user_resources}
    # Instance-wide + per-user disabled skills, plus the capability gate: a
    # skill whose `requires_capability` (e.g. browse→browser, devbox→devbox)
    # isn't available in this deployment is folded into the disabled set so it
    # drops from both selection and the on-demand menu (no wasted pull /
    # confusing CLI failure). See config.available_capabilities().
    _disabled = effective_disabled_skills(config, task.user_id, skill_index)

    # Build sticky skills from recent conversation + explicit reply parent
    sticky_skills: set[str] | None = None
    if task.conversation_token and task.source_type in _INTERACTIVE_SOURCE_TYPES:
        def _get_sticky(c: "db.sqlite3.Connection") -> set[str]:
            skills = db.get_recent_conversation_skills(
                c, task.conversation_token,
                exclude_task_id=task.id,
                max_age_minutes=30,
                limit=2,
            )
            # Also include skills from explicit reply parent (no time limit)
            if task.reply_to_talk_id:
                parent = db.get_reply_parent_task(c, task.conversation_token, task.reply_to_talk_id)
                if parent and parent.selected_skills:
                    try:
                        skills |= set(json.loads(parent.selected_skills))
                    except (json.JSONDecodeError, TypeError):
                        pass
            return skills
        try:
            if conn is not None:
                sticky_skills = _get_sticky(conn)
            else:
                with db.get_db(config.db_path) as temp_conn:
                    sticky_skills = _get_sticky(temp_conn)
            if sticky_skills:
                logger.debug("Sticky skills from conversation: %s", ", ".join(sorted(sticky_skills)))
        except Exception:
            logger.debug("Failed to get sticky skills for task %d", task.id, exc_info=True)

    # `prompt` no longer drives selection — `skills/_loader.select_skills` says
    # so in its own docstring, and the eager selectors are the attachment list
    # and the source type. It is passed the enriched string anyway so the two
    # cannot fall out of step, and the inertness is worth naming here rather
    # than being rediscovered: it is also what stops attacker-painted OCR text
    # steering which skills load, so re-introducing keyword selection would be
    # a security change and not a feature.
    selected_skills = select_skills(
        prompt=effective_prompt,
        source_type=task.source_type,
        user_resource_types=user_resource_types,
        skill_index=skill_index,
        is_admin=is_admin,
        attachments=task.attachments,
        disabled_skills=_disabled if _disabled else None,
        sticky_skills=sticky_skills or None,
        enabled_experimental_features=frozenset(config.experimental.features),
    )

    # The native WebFetch tool ingests untrusted external content, but as a
    # *core tool* it doesn't drive companion-skill selection the way ingest
    # *skills* do. So when this task routes to the native brain with WebFetch
    # enabled, fold `untrusted_input` into the eager set explicitly — mirroring
    # how the ingest skills pull it in via companion expansion — so its
    # inbound-handling guardrails reach the prompt whenever the tool is present.
    if (
        _native_web_fetch_enabled(task, config, is_admin)
        and "untrusted_input" in skill_index
    ):
        if "untrusted_input" not in selected_skills and (
            not _disabled or "untrusted_input" not in _disabled
        ):
            selected_skills = [*selected_skills, "untrusted_input"]

    # Same pattern, same reason, for image attachments. File-type selection
    # already picks `transcribe` for an image and `transcribe` expands its
    # `untrusted_input` companion, so this is normally redundant — which is
    # exactly why it is here explicitly. The OCR block is text read off pixels
    # an attacker chose, and it reaches the prompt whatever skill metadata says;
    # a future change to `transcribe`'s companions or file types must not
    # silently take the guardrails with it. Keyed on the omission notices too,
    # since those name attacker-supplied filenames even when no image was
    # prepared.
    if (image_prep.images or image_prep.ocr_blocks) and "untrusted_input" in skill_index:
        if "untrusted_input" not in selected_skills and (
            not _disabled or "untrusted_input" not in _disabled
        ):
            selected_skills = [*selected_skills, "untrusted_input"]

    # Persist selected skills for conversation stickiness
    if task.id and selected_skills:
        def _save_skills(c: "db.sqlite3.Connection") -> None:
            db.save_task_selected_skills(c, task.id, selected_skills)
        try:
            if conn is not None:
                _save_skills(conn)
            else:
                with db.get_db(config.db_path) as temp_conn:
                    _save_skills(temp_conn)
            logger.debug("Saved %d selected skills for task %d", len(selected_skills), task.id)
        except Exception:
            logger.warning("Failed to save selected_skills for task %d", task.id, exc_info=True)

    # Skills (Part A — single-axis model). A skill is either *eager* (full body
    # inline, because a deterministic rule in select_skills picked it) or in the
    # *menu* (a one-line entry the model pulls in full via
    # `istota-skill skills show <name>`, which also delivers that skill's
    # companions). The menu is the full eligible catalogue — every loadable skill
    # not already eager — so the capable main model self-selects from it (this
    # replaced the removed Pass-2 LLM pre-router). Selection == the eager set.
    from .skills._loader import build_disclosure_index, eligible_skill_names

    eager_skills = selected_skills
    # Menu = eligible skills not already eager-selected or excluded by one.
    menu_exclude = set(selected_skills)
    for n in selected_skills:
        m = skill_index.get(n)
        if m:
            menu_exclude.update(m.exclude_skills)
    menu = eligible_skill_names(
        skill_index,
        exclude=menu_exclude,
        disabled_skills=_disabled if _disabled else None,
        is_admin=is_admin,
        enabled_experimental_features=frozenset(config.experimental.features),
    )
    skills_index = build_disclosure_index(menu, skill_index)
    logger.info("skills: eager=%d menu=%d", len(eager_skills), len(menu))

    # Per-skill user overlays. Derived by the shared helper rather than here, so
    # this path and `skills show` cannot resolve different directories.
    # `{user_id}` in the injected label is substituted below with the rest.
    #
    # The directory and an open descriptor on it, together. Every component
    # under `{mount}/Users/{user_id}` is model-writable, so resolving the path
    # again for each overlay read is a window a task can land a symlink in —
    # and this is the surface where that win was worst, since what it steals
    # goes straight into the next prompt (ISSUE-344). `(None, None)` covers the
    # refusal as well as the ordinary absence, which degrades to exactly the
    # prompt this task would have had with no overlay at all; `doctor`'s
    # `config.skill_overlays` is what reports the refusal, once, on a cadence.
    _overlay_dir, _overlay_fd = open_user_skill_overlays(config, task.user_id)

    try:
        skills_doc = load_skills(
            config.skills_dir, eager_skills, config.bot_name, config.bot_dir_name,
            skill_index=skill_index, bundled_dir=_bundled_dir,
            user_overlay_dir=_overlay_dir, user_overlay_dir_fd=_overlay_fd,
        )
    finally:
        if _overlay_fd is not None:
            os.close(_overlay_fd)
    if skills_doc:
        # Resolve per-user scripts directory
        scripts_nc_path = get_user_scripts_path(task.user_id, config.bot_dir_name)
        if config.use_mount:
            scripts_dir = str(config.nextcloud_mount_path / scripts_nc_path.lstrip("/"))
        else:
            scripts_dir = f"{config.rclone_remote}:{scripts_nc_path}"
        skills_doc = skills_doc.replace("{scripts_dir}", scripts_dir)
        skills_doc = skills_doc.replace("{user_id}", task.user_id)
        # Storage-neutral workspace root + product noun (backend-derived).
        # NOTE: this is a *display string* for the {workspace} placeholder only.
        # It must NOT clobber the `workspace_dir` parameter — that one is the
        # REPL `--workspace cwd` bind path (None for normal tasks) and gets
        # blocklist-validated by build_bwrap_cmd (`_validate_workspace_dir`),
        # which forbids anything under the Nextcloud mount root. The per-user
        # workspace lives under the mount, so reusing the variable made every
        # sandboxed task fail with "overlaps a protected path".
        ws_root = config.workspace_root(task.user_id)
        workspace_display = str(ws_root) if ws_root is not None else f"{config.rclone_remote}:/Users/{task.user_id}"
        skills_doc = skills_doc.replace("{workspace}", workspace_display)
        skills_doc = skills_doc.replace("{storage}", config.storage_label)
    if selected_skills:
        logger.debug("Selected skills: %s", ", ".join(selected_skills))

    # Compute behavior flags from selected skills
    _selected_metas = [skill_index[n] for n in selected_skills if n in skill_index]
    _skip_memory = any(m.exclude_memory for m in _selected_metas)
    _skip_persona = any(m.exclude_persona for m in _selected_metas)

    # Skills changelog: detect changes for interactive tasks
    skills_changelog = None
    _is_interactive = task.source_type in _INTERACTIVE_SOURCE_TYPES
    current_fingerprint = compute_skills_fingerprint(config.skills_dir, bundled_dir=_bundled_dir)
    if _is_interactive:
        try:
            def _check_fingerprint(c):
                return db.get_user_skills_fingerprint(c, task.user_id)
            if conn is not None:
                stored_fingerprint = _check_fingerprint(conn)
            else:
                with db.get_db(config.db_path) as fp_conn:
                    stored_fingerprint = _check_fingerprint(fp_conn)
            if stored_fingerprint != current_fingerprint:
                skills_changelog = load_skills_changelog(config.skills_dir, bundled_dir=_bundled_dir)
                if skills_changelog:
                    logger.info(
                        "Skills changed for user %s (%s -> %s), including changelog",
                        task.user_id, stored_fingerprint or "none", current_fingerprint,
                    )
        except Exception:
            pass  # Graceful degradation

    # Get conversation context if enabled
    conversation_context = None
    context_task_ids: set[int] = set()
    notification_parent = _detect_notification_reply(task, config, conn)
    context_skip_reason = None
    if not use_context:
        context_skip_reason = "use_context=False"
    elif not config.conversation.enabled:
        context_skip_reason = "conversation.enabled=False in config"
    elif task.source_type not in _INTERACTIVE_SOURCE_TYPES:
        context_skip_reason = f"source_type={task.source_type!r} (not interactive)"
    elif not task.conversation_token:
        context_skip_reason = "no conversation_token"

    if context_skip_reason:
        logger.info("Skipping context lookup: %s", context_skip_reason)
    elif notification_parent is not None:
        # Reply to a scheduled/briefing notification — scope context narrowly
        parent_result = notification_parent.result or ""
        if parent_result:
            conversation_context = (
                "[Note: The user is replying to a scheduled notification. "
                "If they are simply acknowledging it, respond very briefly (1 sentence or less). "
                "Do not investigate or bring up unrelated topics.]\n\n"
                f"[Scheduled notification (task {notification_parent.id})]:\n"
                f"{parent_result[:2000]}"
            )
        logger.info(
            "Notification reply detected for task %d (parent task %d, source_type=%s)",
            task.id, notification_parent.id, notification_parent.source_type,
        )
    else:
        # Resolve user TZ once for context formatting (mirrors prompt header).
        _ctx_user_tz, _ = _resolve_user_tz(config, task.user_id, conn=conn)

        # Try Talk API-based context for Talk tasks, fall back to DB on failure
        _used_talk_api = False
        if task.source_type == "talk":
            try:
                conversation_context, context_task_ids = _build_talk_api_context(
                    task, config, conn, user_tz=_ctx_user_tz,
                )
                _used_talk_api = conversation_context is not None
            except Exception as e:
                logger.warning(
                    "Talk API context fetch failed for task %d, falling back to DB: %s",
                    task.id, e,
                )

        # DB-based context fallback (always used for email, fallback for Talk)
        if not _used_talk_api:
            conversation_context, context_task_ids = _build_db_context(
                task, config, conn, user_tz=_ctx_user_tz,
            )

    # Load user memory (auto-create directories if missing)
    # Skills with exclude_memory=true (e.g. briefing) skip personal memory
    # to avoid leaking private context into newsletter-style output.
    user_memory = None
    if not _skip_memory:
        try:
            user_memory = read_user_memory_v2(config, task.user_id)
            if user_memory is None:
                # Try to create directories (memory file may just not exist yet)
                ensure_user_directories_v2(config, task.user_id)
        except Exception:
            # Graceful degradation if storage unavailable
            pass

    # Load channel memory if in a conversation
    channel_memory = None
    if task.conversation_token:
        try:
            channel_memory = read_channel_memory(config, task.conversation_token)
            if channel_memory is None:
                ensure_channel_directories(config, task.conversation_token)
        except Exception:
            pass  # Graceful degradation

    # Auto-discover calendars for user
    discovered_calendars = discover_calendars_for_task(task, config)

    # Auto-load recent dated memories if enabled
    dated_memories = None
    if (config.sleep_cycle.enabled
            and config.sleep_cycle.auto_load_dated_days > 0
            and not _skip_memory):
        try:
            dated_memories = read_dated_memories(
                config, task.user_id,
                max_days=config.sleep_cycle.auto_load_dated_days,
            )
        except Exception:
            pass  # Graceful degradation
    user_config = config.get_user(task.user_id)

    # Auto-recall memories via BM25 search. Exclude task IDs already included
    # as conversation history so the same chunk doesn't appear twice.
    recalled_memories = _recall_memories(
        config, conn, task, retrieval_query,
        skip_memory=_skip_memory,
        exclude_task_ids=context_task_ids or None,
    )

    # Recall learned playbooks (Part B). Independent of _recall_memories;
    # gated on config.playbooks.enabled inside the helper.
    playbooks_text = _recall_playbooks(
        config, conn, task, retrieval_query, skip_memory=_skip_memory,
    )

    # Load knowledge graph facts (filtered by relevance to prompt)
    knowledge_facts_text = None
    if not _skip_memory:
        try:
            from .memory.knowledge_graph import (
                ensure_table, get_current_facts, select_relevant_facts,
                format_facts_for_prompt,
            )
            max_kf = config.max_knowledge_facts
            if conn is not None:
                ensure_table(conn)
                kg_facts = get_current_facts(conn, task.user_id)
                if kg_facts:
                    kg_facts = select_relevant_facts(
                        kg_facts, retrieval_query, task.user_id, max_facts=max_kf,
                    )
                    if kg_facts:
                        knowledge_facts_text = format_facts_for_prompt(kg_facts)
            else:
                with db.get_db(config.db_path) as _kg_conn:
                    ensure_table(_kg_conn)
                    kg_facts = get_current_facts(_kg_conn, task.user_id)
                    if kg_facts:
                        kg_facts = select_relevant_facts(
                            kg_facts, retrieval_query, task.user_id, max_facts=max_kf,
                        )
                        if kg_facts:
                            knowledge_facts_text = format_facts_for_prompt(kg_facts)
        except Exception:
            pass  # Graceful degradation

    # Apply memory size cap
    user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text, playbooks_text = _apply_memory_cap(
        config, user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text, playbooks_text,
    )

    # Get user's email addresses for confirmation policy
    user_email_addresses = []
    if user_config:
        user_email_addresses = user_config.email_addresses

    # Load emissaries (constitutional principles)
    emissaries = load_emissaries(config)

    # Compute effective output target (same logic as scheduler.process_one_task)
    effective_output_target = task.output_target
    if not effective_output_target:
        if task.source_type in ("talk", "briefing"):
            effective_output_target = "talk"
        elif task.source_type == "email":
            effective_output_target = "email"
        elif task.source_type == "istota_file":
            effective_output_target = "istota_file"

    # Build CLI skills list from skill index
    from .skills._loader import format_cli_skills
    cli_skills_text = format_cli_skills(skill_index, is_admin=is_admin)

    # Build prompt
    # Detect confirmed tasks — pass their previous output as confirmation context
    _confirmation_context = None
    if task.confirmed_at and task.confirmation_prompt:
        _confirmation_context = task.confirmation_prompt

    composed = build_prompt(
        task, user_resources, config, skills_doc, conversation_context, user_memory,
        discovered_calendars, user_email_addresses, dated_memories, channel_memory,
        skills_changelog, is_admin, emissaries,
        source_type=task.source_type,
        output_target=effective_output_target,
        recalled_memories=recalled_memories,
        playbooks=playbooks_text,
        skip_persona=_skip_persona,
        cli_skills_text=cli_skills_text,
        skills_index=skills_index,
        confirmation_context=_confirmation_context,
        knowledge_facts=knowledge_facts_text,
        conn=conn,
        effective_prompt=effective_prompt,
        attachment_status=image_attachment_status(image_prep),
    )

    # The two halves travel apart from here. `req.prompt` is the user half; the
    # system half reaches the brain as a *path*
    # (`BrainRequest.composed_system_prompt_path`), so it lands with system
    # authority, outside anything native compaction can reach (ISSUE-375).
    #
    # Two consumers downstream read `req.prompt` as "everything the model was
    # shown", and both now read the user half by decision rather than by
    # accident. `NativeBrain`'s `_extract_urls(req.prompt)` builds the
    # `require_url_provenance` corpus: a URL named only in a persona, a skill
    # body or a tool description is not user-provided provenance, so the
    # narrower corpus is the intended one. `build_image_prompt` prepends the
    # image `Read` directive to it, which keeps that directive leading the user
    # message instead of trailing eight kilobytes of tool documentation.
    prompt = composed.user

    # Log prompt size breakdown. Each component is attributed to the half it
    # is actually in, with a residual per half — a single `other` across both
    # would describe a string nothing sends, while dropping the residual
    # altogether loses the one figure that moves when the persona, the
    # emissaries, the rules or the tool section grow. That is the number to
    # watch on a context-pressure bug, which is what ISSUE-375 is.
    context_chars = len(conversation_context) if conversation_context else 0
    memory_chars = len(user_memory or "") + len(dated_memories or "") + len(channel_memory or "") + len(recalled_memories or "")
    skills_chars = len(skills_doc or "")
    system_chars = len(composed.system)
    user_chars = len(composed.user)
    logger.info(
        "Prompt for task %d: %d chars total "
        "(system: %d [skills %d, other %d], "
        "user: %d [context %d, memory %d, other %d])",
        task.id, system_chars + user_chars,
        system_chars, skills_chars, system_chars - skills_chars,
        user_chars, context_chars, memory_chars,
        user_chars - context_chars - memory_chars,
    )

    if dry_run:
        rendered = render_composed_prompt(composed)
        return True, f"{DRY_RUN_PROMPT_HEADER}\n\n{rendered}", None, None

    # Both halves on disk before anything is built from them, in the
    # daemon-owned control directory rather than in the model's own working
    # directory.
    #
    # The user half is a debugging artifact and nothing in the product reads it
    # back — the prompt reaches ClaudeCodeBrain on stdin, TmuxClaudeBrain
    # through its own `workdir/prompt.txt`, and NativeBrain as the initial user
    # message. It is kept as one, and it is the half that had to move: it
    # carries retrieved memory, knowledge facts, playbooks, conversation
    # history and the request itself, it differs per task, and where it used to
    # sit every later task of that user could read it for the length of the
    # retention window.
    prompt_file = control_dir / "prompt.txt"
    _write_control_file(prompt_file, prompt)

    # The system half, and the file the brain is handed by path.
    #
    # **The directory is resolved and the filename is not**, and the split is
    # the whole point rather than an oversight.
    #
    # `control_dir` arrives resolved from `ensure_task_control_dir`, and that
    # is what makes the path absolute — `BrainRequest` requires that, since
    # NativeBrain opens it in the daemon process while the Claude CLI opens it
    # inside the sandbox, against two different working directories, and a
    # reroute carries one value between them — and it is also what makes it the
    # *in-namespace* path: `_ro_bind` uses the string it is handed as the
    # destination, so an unresolved ancestor on a deployment whose `temp_dir`
    # sits behind a symlink would land the read-only bind somewhere the CLI
    # never looks. `ImageInput.path` records that half of the rule for the same
    # reason.
    #
    # Resolving the *last* component would undo the guard below. `.resolve()`
    # follows a symlink, so a planted one would silently become its target and
    # `O_NOFOLLOW` would then inspect a perfectly ordinary file — leaving the
    # write to land wherever the link pointed, with the symlink hidden rather
    # than caught. Measured: the first draft of this did exactly that.
    #
    # Written before the request is built and never conditionally: a request
    # naming a file that was not written is the fail-closed contract firing on
    # our own bug.
    #
    # `O_NOFOLLOW` on this half and on the one above, and it is belt-and-braces
    # now rather than load-bearing. What it answered was that `user_temp_dir` is
    # per *user*, bound read-write into every one of that user's sandboxes and
    # exported as `ISTOTA_DEFERRED_DIR`, so a concurrent task of the same user
    # could create entries in it — and task ids are sequential and in the
    # environment, so the entry could be a *dangling* symlink named after a
    # task that had not started yet, which a plain `write_text` follows on open.
    # `control_dir` is 0700 under a 0700 root the daemon owns, is a sibling of
    # the per-user directories rather than a child of one, and is bound into no
    # sandbox read-write, so there is no task that can plant anything here. The flag stays
    # because a guard dropped on the strength of a property held somewhere else
    # is the one nobody notices the loss of. The deferred-op files still carry
    # the original exposure and are model-authored by design.
    #
    # `0o600` because the file holds the persona, the user's own overlays and
    # the per-user email address, and there is no reason for it to be readable
    # by other local accounts.
    system_prompt_file = control_dir / "system_prompt.txt"
    _write_control_file(system_prompt_file, composed.system)

    # Result file path
    result_file = user_temp_dir / f"task_{task.id}_result.txt"

    # Clean up any previous result file
    if result_file.exists():
        result_file.unlink()

    # Bound here rather than at its assignment below, so the handler at the
    # bottom of this function can release it. The cgroup is created roughly 200
    # lines before the ExitStack that registers its cleanup, and everything in
    # between — brain construction, model resolution, the BrainRequest itself —
    # is inside this try. An exception there returns without the stack ever
    # being entered, leaking the directory until the next daemon start.
    _task_cg = None

    try:
        if event_writer is not None:
            # Stamp a generic progress verb so stream surfaces (web chat) show a
            # real "working on it" line instead of a hardcoded placeholder until
            # the first tool/text event arrives. Talk ignores this payload and
            # picks its own verb at ack time; both draw from the same list.
            event_writer.emit("task_started", {"text": random_progress_message()})
        use_streaming = event_writer is not None
        allowed = build_allowed_tools(
            is_admin,
            selected_skills,
            web_fetch_admin_only=config.brain.native.web_fetch.admin_only,
        )

        # Which attempt of this task is running, bound once and read twice: it
        # goes into the environment here and names the session log's file on
        # the brain request some six hundred lines below. The exclusion that
        # keeps a task out of the transcript it is writing is an *equality*
        # between those two, so they are one expression rather than two copies
        # of `attempt_count + 1` far enough apart to drift unnoticed — and the
        # direction they would drift in is the permissive one, a floor above
        # the live file. `+ 1` because the counter records *prior* attempts, so
        # a first run is attempt 1, as `task_usage.attempt_seq` is.
        task_attempt = task.attempt_count + 1

        # The model's environment, the three-way credential split behind it,
        # the two proxy objects and the sandbox's read-only bind list — all in
        # `task_env`. Three orderings inside it are load-bearing (the proxy
        # snapshot before `ISTOTA_SANDBOXED`, proxy-only before credentials,
        # the PATH-prepend key consumed after the hook merge) and its docstring
        # is where they are written down.
        #
        # Both proxies come back *constructed and not entered*. They are
        # entered in the `ExitStack` below, which has to wrap the primary call,
        # the reroute and the fallback call alike.
        _runtime = task_env.build_task_runtime(
            config,
            task,
            user_temp_dir=Path(user_temp_dir),
            control_dir=control_dir,
            task_attempt=task_attempt,
            selected_skills=selected_skills,
            skill_index=skill_index,
            is_admin=is_admin,
            user_resources=user_resources,
            user_config=user_config,
            discovered_calendars=discovered_calendars,
        )
        env = _runtime.env
        _proxy_ctx = _runtime.proxy_ctx
        _proxy_sock = _runtime.proxy_sock
        _net_proxy_ctx = _runtime.net_proxy_ctx
        _net_proxy_sock = _runtime.net_proxy_sock
        _extra_ro_binds = _runtime.extra_ro_binds
        authorized_skills = _runtime.authorized_skills

        # Sandbox wrapper closures — capture the per-task bind config so the
        # brain can wrap its raw cmd without knowing anything about bwrap.
        #
        # Two closures rather than one taking a profile, because the request
        # carries them as two fields (see `BrainRequest.native_sandbox_wrap`):
        # `_run_fallback` copies the request with `dataclasses.replace`, so a
        # single field whose value encoded a profile would hand the Claude
        # mounts to NativeBrain on the shipped `claude_code -> native` reroute.
        # Everything else about the two is identical, which is why the plan is
        # built once and the profile is the only argument that differs.
        def _build_wrap(sandbox_profile: SandboxProfile):
            def _wrap(raw_cmd: list[str]) -> list[str]:
                if not config.security.sandbox_enabled:
                    return raw_cmd
                return build_bwrap_cmd(
                    raw_cmd, config, task, is_admin, user_resources,
                    Path(user_temp_dir), proxy_sock=_proxy_sock,
                    net_proxy_sock=_net_proxy_sock,
                    extra_ro_binds=_extra_ro_binds,
                    # `TaskRuntime.authorized_skills` — the union credential
                    # presence widened, not `selected_skills`. See
                    # `build_bwrap_cmd`'s own docstring for why the distinction
                    # decides whether the exec transport routes on the first
                    # turn of a conversation.
                    authorized_skills=authorized_skills,
                    workspace_dir=workspace_dir,
                    profile=sandbox_profile,
                )

            return _wrap

        _sandbox_wrap = _build_wrap(SandboxProfile.CLAUDE)
        _native_sandbox_wrap = _build_wrap(SandboxProfile.NATIVE)

        # Adapts the brain's (widened) StreamEvent stream to TaskEvents:
        # the coalescing buffers for answer text and reasoning, the
        # narration gate and the delta-vs-whole-turn dedupe all live in
        # `executor_stream`. `stream.on_event` is what goes on
        # `BrainRequest.on_progress`.
        stream = TaskStreamAdapter(config, task, event_writer)

        # Per-task cgroup (A6). Created before the brain is asked for anything,
        # because the pid it hands back has already been spawned and every
        # microsecond between spawn and placement is time the tree runs
        # unbounded. `None` on any deployment without `Delegate=` — the module
        # logs why once and everything below carries on as it did before.
        if config.scheduler.task_cgroup_enabled:
            _task_cg = task_cgroup.create(
                task.id,
                task_cgroup.CgroupLimits(
                    memory_max_mb=config.scheduler.task_memory_max_mb,
                    pids_max=config.scheduler.task_pids_max,
                    cpu_max_percent=config.scheduler.task_cpu_max_percent,
                ),
                # A retry reuses the task row, so the id alone would put this
                # attempt in the directory the previous one left behind —
                # together with whatever of its tree escaped the kill.
                attempt=task.attempt_count,
            )

        def _on_pid(pid: int) -> None:
            # Placement first, DB second. `update_task_pid` can block on the
            # SQLite write lock, and the whole value of the cgroup is in the
            # window before the child's own work starts.
            #
            # This is the after-the-fact form, and it only reaches the pid's own
            # thread group — not the children it already forked (ISSUE-285). The
            # brains that spawn their own subprocess place it from `preexec_fn`
            # instead, off `req.task_cgroup`, and by the time they call back
            # here the pid is already a member and this write is a no-op. What
            # it is still load-bearing for is TmuxClaudeBrain, which reports a
            # pane pid the tmux server spawned: there is no `preexec_fn` to
            # reach, so containing the group leader is all that path can do.
            if _task_cg is not None:
                task_cgroup.place(pid, _task_cg)
            try:
                with db.get_db(config.db_path) as pid_conn:
                    db.update_task_pid(pid_conn, task.id, pid)
            except Exception:
                pass  # non-critical

        def _poll_steers() -> "list[str]":
            # Claim any pending mid-flight steers (`!steer`) for this task,
            # marking them consumed, and hand the raw texts to the brain. The
            # brain frames + injects them as user turns. Wired onto the request
            # only for a steering-capable brain (below). Best-effort — a DB
            # hiccup returns no steers, never aborts the run.
            try:
                with db.get_db(config.db_path) as steer_conn:
                    steers = db.claim_pending_steers(steer_conn, task.id)
                return [s.text for s in steers]
            except Exception:
                return []

        # Custom system prompt path (claude_code-only knob; brain ignores
        # if the file is missing). `build_bwrap_cmd` binds this one file into
        # the sandbox — the CLI opens it there.
        sp_path = custom_system_prompt_path(config)

        from .brain import BrainRequest, resolve_brain_kind
        # Brain routing. `tasks.brain` is the room's standing pick, frozen
        # onto this row at creation; below it, an operator can map this task's
        # source_type to a different kind via [brain.source_type_overrides].
        # No-op for the common case, where both are unset. An admitted room pin
        # also clears `fallback`, so the failover machinery below collapses to a
        # plain primary call for that task.
        _brain_config = resolve_brain_kind(
            task.source_type, config.brain, override=task.brain,
        )
        if _brain_config.kind != config.brain.kind:
            logger.info(
                "brain routing: task %d source_type=%s -> kind=%s (default %s)",
                task.id, task.source_type, _brain_config.kind, config.brain.kind,
            )
        # Overlay the per-user native-brain API key (encrypted secrets) so a
        # multi-user deployment can give each user their own provider credential.
        if _brain_config.kind == "native":
            import dataclasses as _dc
            _brain_config = _dc.replace(
                _brain_config,
                native=_native_with_user_key(
                    _brain_config.native, config, task.user_id
                ),
            )
        brain = make_brain(_brain_config)

        # Filesystem confinement roots for NativeBrain's in-process file tools
        # (NB-1). Only when effective sandboxing is active — same predicate the
        # cwd choice below uses. Other brains ignore these fields.
        _fs_read_roots: "list[Path] | None" = None
        _fs_write_roots: "list[Path] | None" = None
        # The task control directory is denied **whether or not confinement is
        # active**, which is why it is seeded here rather than only inside the
        # branch. The two root lists are an allowlist and mean nothing without
        # confinement, but a deny root is a statement about a path: `ToolEnv`
        # resolves `write_denied_roots` unconditionally and checks them ahead
        # of its unconfined early return, exactly so a caller who sets one
        # without `read_roots` gets the refusal it looks like
        # (`session/tools/env.py`, and `test_denied_even_when_unconfined`).
        #
        # That matters because the unconfined shapes are the ones with nothing
        # else: `build_bwrap_cmd` hands the command back unwrapped on macOS, on
        # the standalone install and on the shipped Docker stack (which grants
        # neither `seccomp:unconfined` nor `systempaths=unconfined`, so the
        # bwrap probe fails and every task runs uncontained). Gating this on
        # sandboxing would leave the directory with no guard at all on
        # precisely those deployments. The confined branch below returns a list
        # that already contains it, so there is no duplicate.
        #
        # No matching *read* seed, and that is not an omission: `read_roots`
        # left as None is what `ToolEnv` reads as unconfined, so adding one
        # here would turn an unconfined shape into a confined one whose only
        # readable path is this directory.
        _fs_write_denied_roots: "list[Path]" = [control_dir]
        if native_fs_confinement_active(config):
            _fs_read_roots, _fs_write_roots, _fs_write_denied_roots = native_fs_roots(
                config,
                task,
                is_admin,
                user_resources,
                Path(user_temp_dir),
                workspace_dir,
                control_dir=control_dir,
            )

        # Resolve aliases (role, provider) to a canonical model ID. Talk-poller
        # tasks already arrive resolved via the !model prefix path; cron jobs,
        # briefings, email, and operator istota_model defaults can still carry
        # an alias string here, which the brain CLI doesn't accept directly.
        # `resolve_model_name` is a no-op for canonical IDs and unknown strings.
        req = BrainRequest(
            prompt=prompt,
            allowed_tools=allowed,
            # Non-sandbox path (Mac/dev/Docker): the REPL points the brain's
            # working directory at the launch dir directly. No blocklist here —
            # without bwrap the process already runs with the user's own FS
            # access, so the bind-shadowing threat the blocklist guards doesn't
            # apply (it fires in build_bwrap_cmd, the sandboxed path). Keyed off
            # *effective* sandboxing: when sandbox_enabled is set but bwrap is
            # absent (Mac/dev), build_bwrap_cmd returns the cmd unwrapped with no
            # --chdir, so this cwd is what actually takes effect for --workspace.
            cwd=(
                Path(workspace_dir).resolve()
                if workspace_dir is not None
                and not effective_sandboxing(config)
                else Path(config.temp_dir)
            ),
            env=env,
            db_path=config.db_path,
            # Which attempt this is, for the brain's own per-attempt artifacts
            # (NativeBrain's session log names its file after them). Read off
            # the task row rather than out of `env`, which is the sandbox
            # environment.
            #
            # `+ 1` because `attempt_count` counts *prior* attempts — the claim
            # path never touches it, and only a release/retry increments it, so
            # a first run carries 0 (`scheduler.py`: "attempt_count == 0 is left
            # alone — a first run has no prior attempt"). The session log's
            # numbering is 1-based, as `task_usage.attempt_seq` is, so a first
            # run has to be attempt 1 and a retry attempt 2. This deliberately
            # disagrees with the task cgroup directory and the tmux session
            # label, which are named from the raw counter: those are within-run
            # identifiers with no reader outside the process, while an operator
            # reads a log file name against the usage table.
            #
            # The same binding as `ISTOTA_TASK_ATTEMPT` above, deliberately: the
            # floor that withholds a live transcript is an equality between the
            # two, so re-deriving it here would be a second copy to keep in step.
            task_id=task.id,
            attempt=task_attempt,
            user_id=task.user_id or "",
            source_type=task.source_type or "",
            conversation_token=task.conversation_token or "",
            is_group_chat=bool(task.is_group_chat),
            timeout_seconds=config.scheduler.task_timeout_minutes * 60,
            # The task's own pin, or nothing. Never a deployment default: the
            # top-level `config.model` was claude_code's own, and substituting
            # it here shadowed every other brain's (ISSUE-418). An empty value
            # reaches the brain, which fills in its own configured default —
            # which is what `NativeBrain`'s long-dead `req.model or
            # self._config.model` was always waiting to do.
            #
            # Resolved through the crossing rule rather than by
            # `resolve_model_name` alone, because a *pin* can still meet a brain
            # of another namespace here (ISSUE-417) — ISSUE-418 removed the
            # deployment default, not the pin. The route that still reaches it
            # here is a *pinned* task — a room, or a cron job, whose `brain` the
            # operator has since dropped from `room_selectable`, where
            # `resolve_brain_kind` warns and falls through while the stored
            # model belongs to another namespace. For an *unpinned* task this is
            # now inert by construction: `_pin_origin_namespace` answers with
            # the lane's own brain, which is the brain being passed in, so
            # nothing crosses. See that function for the three producers that do
            # not meet the premise behind it. Within one namespace the rule
            # resolves exactly as `resolve_model_name` did, so the ordinary path
            # is unchanged.
            model=_request_model(task, config, brain),
            effort=_resolve_effort(task, config),
            # Anthropic-namespace brains only — the advisor tool has no wire
            # over NativeBrain's openai_compat endpoint.
            advisor=(
                brain.resolve_model_name(_resolve_advisor(task, config))
                if brain.model_namespace == "anthropic"
                else ""
            ),
            custom_system_prompt_path=sp_path,
            # Istota's standing instructions, by path, with system authority.
            # Required input from here on: a brain that cannot read it fails
            # the attempt rather than running the user half alone, which would
            # be ISSUE-375 recreated by a filesystem race. Absolute by
            # construction — see the `.resolve()` at the write above.
            composed_system_prompt_path=system_prompt_file,
            # The prepared images, as paths and media types — never bytes. Each
            # brain converts at the last moment, so nothing large reaches a task
            # row or a log line and the executor learns no provider wire format.
            # `_run_fallback` copies the request with `dataclasses.replace`,
            # which does not name this field, so a reroute carries these across
            # and the fallback brain makes its own capability decision.
            images=image_prep.images,
            streaming=use_streaming,
            on_progress=stream.on_event if use_streaming else None,
            cancel_check=_cancel_check,
            # Steering channel — only for a brain that can act on it mid-run
            # (`!steer`). A non-steerable brain leaves this None (no extra DB
            # polling) and any steer written to the channel is dropped at
            # finalization. The command layer refuses to write for such brains
            # anyway, so this is defense-in-depth.
            poll_steers=_poll_steers if getattr(brain, "supports_steering", False) else None,
            on_pid=_on_pid,
            # NativeBrain has no single subprocess and so never calls `on_pid`
            # — its Bash tool spawns one child per execution. It places each of
            # those itself, from this path. Other brains ignore the field.
            task_cgroup=_task_cg,
            sandbox_wrap=_sandbox_wrap,
            # The same plan under the NATIVE profile — no Claude runtime block,
            # no credential, no system-prompt bind. Read only by NativeBrain;
            # the two Claude brains read `sandbox_wrap` and ignore this.
            native_sandbox_wrap=_native_sandbox_wrap,
            # Filesystem confinement for NativeBrain's in-process file tools
            # (NB-1). Populated only when effective sandboxing is on; other
            # brains ignore these (bwrap already confines their tools).
            fs_read_roots=_fs_read_roots,
            fs_write_roots=_fs_write_roots,
            fs_write_denied_roots=_fs_write_denied_roots,
            result_file=result_file,
            # Task-derived tmux session label (no-op for other brains): threads
            # the task id into the session name, structured log line, and
            # on_pid/!stop correlation.
            session_label=f"istota-{task.id}-{task.attempt_count}",
        )
        if req.advisor:
            logger.info(
                "task %d: model=%s advisor=%s", task.id, req.model, req.advisor,
            )

        # Availability failover (brain-fallback spec) — see
        # `run_with_failover`. The ExitStack stays here: the proxies must be
        # live across the primary call, the reroute and the fallback call.
        try:
            with contextlib.ExitStack() as stack:
                if _proxy_ctx is not None:
                    stack.enter_context(_proxy_ctx)
                if _net_proxy_ctx is not None:
                    stack.enter_context(_net_proxy_ctx)
                # Every exit path — success, failure, timeout, cancellation,
                # a fallback brain replacing the primary — gives the directory
                # back and kills anything still in it.
                if _task_cg is not None:
                    stack.callback(_release_task_cgroup, task.id, _task_cg)

                _failover = run_with_failover(
                    brain, req,
                    config=config,
                    brain_config=_brain_config,
                    task=task,
                    stream=stream,
                    event_writer=event_writer,
                )
                brain_result = _failover.result
                _primary_usage_result = _failover.primary_usage_result
                _ran_fallback = _failover.ran_fallback
                _usage_effort = _failover.usage_effort
                _dropped_pin = _failover.dropped_pin
                _primary_kind = _failover.primary_kind
                _fallback_kind = _failover.fallback_kind
        finally:
            # Final flush: emit any buffered streamed thinking + text before the
            # scheduler emits the terminal event. Thinking first so its rows keep
            # a lower seq than any trailing answer text. On success this precedes
            # the canonical ``result`` (which replaces the answer in the UI); on
            # an exception the finally still drains both buffers.
            stream.finish()

        success = brain_result.success
        result = brain_result.result_text
        actions = brain_result.actions_taken
        trace = brain_result.execution_trace

        # What the model had written when a cancel or a timeout ended the run
        # (ISSUE-372). It rides on the task rather than in the return tuple:
        # `result_text` is what the scheduler dispatches on — by exact equality
        # for a cancel — so the partial answer cannot travel in it, and widening
        # the four-tuple would touch every caller for a value only the scheduler
        # reads. Same hand-off `model_used` uses two blocks down. Only on a
        # failure: a successful run's answer *is* `result`, and setting this too
        # would give the scheduler two candidates for one column.
        if not success and brain_result.partial_text:
            task.partial_result = brain_result.partial_text

        # Record the model the brain actually used. Prefer the brain's reported
        # value (accurate even when the model was the brain/CLI default); fall
        # back to the resolved request model. Set it on the task object so the
        # scheduler can include it in the terminal `done` event, and persist it
        # to the dedicated `model_used` column so the web-chat history endpoint
        # surfaces it across reloads. `task.model` (the override) is left alone
        # so a retry of a default-model task re-resolves the current default.
        actual_model = (brain_result.model_used or "").strip() or req.model
        if actual_model:
            task.model_used = actual_model
            try:
                if conn is not None:
                    db.set_task_model_used(conn, task.id, actual_model)
                else:
                    with db.get_db(config.db_path) as _model_conn:
                        db.set_task_model_used(_model_conn, task.id, actual_model)
            except Exception:
                logger.debug("persisting task model_used failed", exc_info=True)

        # Persist this attempt's token/cost telemetry. Both rows are written
        # here, from the one place that already holds a `conn`. On an in-attempt
        # brain fallback there are two: `attempt_seq` 1 and 2, each with its own
        # `brain_kind` and `is_fallback`, which summed is the task's real cost.
        if _primary_usage_result is not None:
            _persist_task_usage(
                config, conn, task.id, _primary_usage_result.usage,
                user_id=task.user_id, source_type=task.source_type,
                brain_kind=_primary_usage_result.brain_kind,
                # `effort_used` first, for the reason `model_used` is read
                # rather than `req.model`: the brain fills its own configured
                # default onto a copy, so this request no longer describes the
                # attempt (ISSUE-418). `req.effort` remains the fallback for a
                # brain that reports none.
                model=_primary_usage_result.model_used,
                effort=_primary_usage_result.effort_used or req.effort,
                stop_reason=_primary_usage_result.stop_reason,
                success=_primary_usage_result.success,
            )
        _persist_task_usage(
            config, conn, task.id, brain_result.usage,
            user_id=task.user_id, source_type=task.source_type,
            brain_kind=brain_result.brain_kind,
            is_fallback=_ran_fallback,
            model=brain_result.model_used,
            effort=brain_result.effort_used or _usage_effort,
            stop_reason=brain_result.stop_reason, success=brain_result.success,
        )

        # CM-aware / terse-result composition: reconcile result_text with
        # the trace so substantial intermediate text isn't lost when the
        # final ResultEvent is terse. Same logic both brains will need.
        # Runs whenever the task succeeded, trace or not — its other job is the
        # empty-result guard, and a successful turn that produced no trace at
        # all still must not deliver a blank reply (ISSUE-211).
        if success:
            trace_list: list = []
            if trace:
                try:
                    parsed_trace = json.loads(trace)
                except (json.JSONDecodeError, TypeError):
                    parsed_trace = None
                if isinstance(parsed_trace, list):
                    # Element-wise, not just list-ness: the walker calls
                    # .get() on every entry, so one stray non-dict would
                    # turn a completed run into an execution error.
                    trace_list = [e for e in parsed_trace if isinstance(e, dict)]
            result = _compose_full_result(result, trace_list, task=task)

        # Visible fallback note: a non-portable model pin that couldn't cross the
        # provider boundary was dropped, so the fallback ran on its own default.
        # Append the note *after* composition (so composition operates on the real
        # answer) and only on success — a failed fallback flows through the normal
        # error path with no cosmetic note.
        if success and _dropped_pin:
            result = _append_model_note(
                result, _dropped_pin, _primary_kind, actual_model
            )

        # Two image notes, both about the same thing: the user must never read
        # an answer produced without the picture and have nothing say so.
        if success and req.images:
            _ran_kind = _fallback_kind if _ran_fallback else _primary_kind
            _can_see = brain_delivers_vision(_ran_kind, actual_model)
            if _can_see is False:
                # A model that declares no vision support. Not gated on a
                # fallback having run: the fact that matters is that the answer
                # was written without the picture, and a *primary* native brain
                # on a non-vision model produces exactly that with nothing
                # telling the user so. The brain already told the model it was
                # missing the images; this is the half the user reads.
                result = _append_vision_dropped_note(
                    result,
                    [i.display_name or Path(i.path).name for i in req.images],
                    actual_model,
                    rerouted=_ran_fallback,
                )
            elif _ran_kind in ("claude_code", "tmux_claude"):
                # The directive required one `Read` per image. Check the trace
                # rather than trusting it.
                _unread = unread_images(req.images, trace)
                if _unread:
                    logger.warning(
                        "task %d: %d of %d prepared image(s) were never read "
                        "by %s", task.id, len(_unread), len(req.images),
                        _ran_kind,
                    )
                    result = _append_unread_images_note(result, _unread)

        # Update skills fingerprint after successful interactive execution
        if success and _is_interactive:
            try:
                def _update_fp(c):
                    db.set_user_skills_fingerprint(c, task.user_id, current_fingerprint)
                if conn is not None:
                    _update_fp(conn)
                else:
                    with db.get_db(config.db_path) as fp_conn:
                        _update_fp(fp_conn)
            except Exception:
                pass  # Non-critical

        return success, result, actions, trace

    except Exception as e:
        # Reached when the failure happened before the ExitStack was entered;
        # once it has been, the callback already ran and this is a no-op on a
        # directory that is gone.
        if _task_cg is not None:
            _release_task_cgroup(task.id, _task_cg)
        return False, f"Execution error: {e}", None, None


def execute_task_interactive(
    prompt: str,
    user_id: str,
    config: Config,
) -> tuple[bool, str]:
    """
    Execute a prompt interactively (for CLI testing).
    Creates a temporary task and executes it.
    """
    with db.get_db(config.db_path) as conn:
        # Create temporary task
        task_id = db.create_task(
            conn,
            prompt=prompt,
            user_id=user_id,
            source_type="cli",
        )
        task = db.get_task(conn, task_id)
        if not task:
            return False, "Failed to create task"

        # Get dynamic resources from DB (shared_file entries from auto-organizer)
        user_resources = db.get_user_resources(conn, user_id)

        # Execute (config resources are merged internally by execute_task)
        success, result, actions, trace = execute_task(task, config, user_resources)

        # Update task status
        if success:
            db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions, execution_trace=trace)
        else:
            db.update_task_status(
                conn, task_id, "failed", result=task.partial_result, error=result,
                actions_taken=actions, execution_trace=trace,
            )

        return success, result
