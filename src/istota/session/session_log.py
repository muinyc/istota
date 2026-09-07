"""Append-only JSONL transcript of one ``NativeBrain`` task attempt.

Nothing persisted the conversation the native brain holds with the model. When
a task finished, the message list, every tool result, every thinking block and
every compaction summary were garbage-collected, and what survived was built
for delivery rather than for inspection: ``tasks.execution_trace`` carries tool
*labels* and no tool output at all, ``task_events`` carries a capped payload and
only when streaming is on, and ``task_N_prompt.txt`` is the input rather than
the run. So a native task that produced a wrong answer could not be
reconstructed. ``ClaudeCodeBrain`` never had this problem — the ``claude`` CLI
writes its own session JSONL and ``build_bwrap_cmd`` binds it out of the sandbox
— so the asymmetry was accidental. The format here adapts pi's session store
(the same prior art ``agent/types.py`` already cites for ``prepareNextTurn``) to
istota's unit of work, which is a task *attempt* rather than an interactive
session.

**One file per attempt, not per task.** A retry re-executes the prompt from
scratch with a fresh message list, so two attempts are two runs and merging them
would produce a transcript that never existed. ``task_usage`` already draws the
line at ``(task_id, attempt_seq)``.

**Linear records, no ``id``/``parentId``.** pi's tree exists for ``/fork`` and
``/tree``, which are interactive affordances. A task attempt has no user at the
keyboard and no way to rewind, so order in the file is the order of the run.
Adding the two fields later is a format-version bump the reader can absorb,
which is why they are left out rather than added speculatively.

**Two rules keep the artifact bounded, and they are the difference between an
observability feature and a disk-filling one.** Images are never written as
bytes — a single screenshot is megabytes of base64, so an ``ImageContent``
serializes as a descriptor whose ``sha256`` still identifies two records as the
same image. Text is capped per content block, **head and tail** rather than head
alone, because a truncated build log's tail is where the error is and a
head-only cut reliably discards the one part anybody opens the file for. Tool
call arguments are capped separately into an honest marker object, since a
truncated *fragment* of a JSON object is worse than a marker saying so.
``result_text`` is deliberately uncapped: it is the deliverable, the same
reasoning that put ``result`` in ``events._UNCAPPED_EVENT_KINDS``.

**The writer never raises, and it never nags.** A task must not fail because a
log could not be written, so every public method is wrapped; the first failure
logs one warning, disables the writer and closes the handle, and every later
call is a no-op — a disk that filled up must not produce one warning per tool
call for the rest of the day. A single record that will not serialize costs that
record and not the session: it becomes a ``serialization_error`` line and the
run carries on. Writes are ``flush``ed and never ``fsync``ed; a daemon that dies
loses the records the OS had buffered, which is a cost worth paying to keep an
``fsync`` off the agent loop's hot path. pi makes the same trade.

**Permissions are the only content control.** Files are ``0600`` behind a
``0700`` directory, set through the open flags. There is no group-readable case
and no content-based redaction: a regex sweep for credentials would miss the
shapes it does not know and mangle legitimate content that resembles a token.
The residual risk is stated rather than denied — a ``Bash`` call that cats a
credential file puts that credential in the log, and the retention window is how
long it stays there. That is already true of ``task_N_prompt.txt`` and of the
app log; an operator who cannot accept it sets the feature off.

What this module does **not** do is decide what goes in the ``session``
header: :meth:`SessionLogWriter.open` copies the caller's mapping through,
minus the three fields that are the record's own. Keeping ``api_key`` and
``extra_headers`` out of it, and reducing ``base_url`` to its host — an
operator can put a token in a URL path — belongs to whoever builds that
mapping, which is ``brain/native.py``. Stated here because a reader looking
for the rule should find out where it lives rather than assume it is enforced
below.

**The caps reach slightly wider than the content blocks.** ``TextContent`` and
``ThinkingContent`` are the ones the format is written around, and
``ToolCallContent.arguments`` has its own. ``max_content_chars`` is also
applied to the ``context`` record's system prompt, to a ``steer``'s text and to
both halves of an ``error`` record, because each is an unbounded string reaching
the file from somewhere this module does not control. ``result_text`` is the one
deliberate exemption.

:func:`sweep_session_logs` lives here rather than in the scheduler, on the
:mod:`istota.worktree_reaper` precedent: the delete rule and the write rule
belong in one file. It enforces **two independent rules**, and neither implies
the other. Age bounds how long a transcript is retrievable, which is a privacy
question. Bytes bound how much disk a burst of long agentic tasks can take from
the filesystem the framework database is writing to, which is an availability
one — on the reference deployment ``data/`` holds ``istota.db``, every module DB
and these logs, so a logging artifact that can fill it takes SQLite writes down
with it. :mod:`istota.sandbox_cache_sweeper` wrote the reasoning down first: a
rule phrased in days either keeps everything or throws away something minutes
old, because the growth arrives in bursts rather than at a rate.

**The ceiling is deployment-wide, not per user**, because the thing being
protected is a filesystem and a filesystem has no per-user quota. Under a
per-user ceiling the real limit is ``users x ceiling`` — a number that changes
whenever a user is added and that appears nowhere in the config. The cost is a
fairness question per-user eviction never faced, and the answer is
**largest-user-first, then oldest-within-that-user**. Plain global oldest-first
is the obvious rule and it inverts the outcome: the globally oldest files belong
to the *quietest* users precisely because they are quiet, so one user producing
a flood would evict everyone else's history to make room for their own fresh
output. Water-filling from the largest tree down trims the heaviest producer
toward the others before anybody else loses anything, and the two rules are
identical on a single-user deployment.

**Never a file being written.** A file whose mtime is inside
:data:`LIVE_WINDOW_SECONDS` is never evicted by the ceiling — the same guard
shape ``sandbox_cache_sweeper`` uses for a live writer, and cheaper here because
there is no task table to consult. A tree that cannot be brought under the
ceiling without touching those files reports ``still_over`` and stops, rather
than deleting a live session or looping.

Measurement is du-style (``du.iter_tree``), because a volume is filled by
blocks. Directory inodes are deliberately *not* counted, which is where this
diverges from ``sandbox_cache_sweeper.measure_cache``: a per-user directory is
overhead the sweep can never reclaim, so counting it would let a many-user
deployment sit permanently over a ceiling no eviction could clear. There are no
hardlinks here, so no inode deduplication is needed.

Enumerating user directories from disk is safe here in a way it is not in
``sandbox_cache_sweeper``, which takes its user list from ``config.users``
because its tree is bound read-write into a sandbox and an entry there is
model-plantable. This tree is bound into no sandbox at any path, so a directory
in it can only have been created by the writer.

stdlib-only apart from :mod:`istota.llm.types`, which the serializer needs for
its ``isinstance`` dispatch, and :mod:`istota.du`, which holds the du-style walk
and the first-level directory scan the sweep shares with
``sandbox_cache_sweeper`` and ``doctor``. Both are leaves that import nothing
from the package — ``tests/native/test_session_log.py`` asserts that
transitively rather than taking it on trust, because a permitted leaf that later
grows a ``config`` import would bring the whole graph in through a name the
guard already approved. No config, no brain, no database, roots and policy are
parameters, and it never raises.
"""

import base64
import hashlib
import json
import logging
import math
import os
import stat
import traceback
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from istota import du
from istota.llm.types import (
    AssistantMessage,
    Content,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

logger = logging.getLogger("istota.session.session_log")

# Bumped when a record's shape changes in a way a reader cannot absorb. Line 1
# carries it so an old file stays readable after the format moves on.
FORMAT_VERSION = 1

LOG_SUFFIX = ".jsonl"

# The directory `resolve_session_log_dir` hangs off `db_path.parent` when the
# operator configured none. `logs` rather than `sessions` because that is what
# these are, and a generic name leaves room for a second log kind without a
# rename — the sweep is scoped to `*.jsonl` under `logs/{user_id}/`, so a
# future sibling is not in its path.
LOG_DIR_NAME = "logs"

_GIB = 1024 ** 3

# The shipped policy, so a caller with no config — a test, a one-off script —
# gets it. `SessionLogConfig` will restate them in `config.py`; nothing reads
# this module yet.
DEFAULT_MAX_CONTENT_CHARS = 32768
DEFAULT_MAX_ARGS_CHARS = 8192
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_GB = 2.0

# The ceiling is clamped to this. Below half a gigabyte the bound is under a
# single busy day's worth of transcripts, so every sweep would evict a file
# somebody is about to read.
MIN_MAX_TOTAL_GB = 0.5

# A file stamped inside this window is assumed to belong to a run happening now
# and is never evicted by the ceiling.
LIVE_WINDOW_SECONDS = 3600.0

# How much of an over-cap arguments object survives in the marker. Enough to
# recognise the call, far short of the cap it is standing in for.
_ARGS_PREVIEW_CHARS = 512

# How deep `_count_truncations` walks a record looking for this module's own
# cap markers. The deepest one it writes is at 5.
_MAX_SCAN_DEPTH = 6

_SECONDS_PER_DAY = 86400.0


# --------------------------------------------------------------------------
# Policy and identity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionLogPolicy:
    """What gets written and how much of it."""

    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS
    max_args_chars: int = DEFAULT_MAX_ARGS_CHARS
    include_thinking: bool = True


@dataclass(frozen=True)
class SessionLogIdentity:
    """Which run a file belongs to. Every field comes from the task, never from
    anything the model wrote."""

    task_id: int
    attempt: int
    user_id: str
    source_type: str = ""
    conversation_token: str = ""
    is_group_chat: bool = False


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Indirection so a test can freeze the clock without patching stdlib."""
    return datetime.now(timezone.utc)


def _iso_ms(dt: datetime) -> str:
    """ISO 8601 UTC at millisecond precision: ``2026-08-31T14:22:01.993Z``.

    A naive datetime is read as UTC rather than as local time. Reading it as
    local would put a record's timestamp an hour or more away from the file name
    built from the same value.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ts() -> str:
    return _iso_ms(_utcnow())


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def resolve_session_log_dir(db_path: Path | str | None, configured: str = "") -> Path:
    """Where the logs go, from the only two things that decide it.

    A pure function of ``config.db_path`` and
    ``config.brain.native.session_log.dir`` rather than a method on ``Config``,
    so the writer, the scheduler's sweep, ``doctor`` and the skill proxy all ask
    one question and this module stays a stdlib-only leaf that imports no
    config. The resolved directory is derived on every read and stored nowhere:
    a second copy is how a checker starts passing while the real thing is
    wrong.

    Blank — the shipped default — is ``{db_path.parent}/logs``. That is local
    disk on every shipped shape (appending JSONL all day to the rclone mount
    would be worse than a WAL database there), it is the state directory that
    already holds ``modules/``, ``backups/`` and ``subscription_usage.json``,
    and on the Ansible shape it is behind the read-only tmpfs
    ``build_bwrap_cmd`` masks ``db_path.parent`` with. On the standalone shape
    that mask is refused — ``db_path.parent`` *is* the workspace there — and on
    the shipped Docker stack the path is under what the mask would cover but no
    mask is ever emitted, because that compose file grants neither
    ``seccomp:unconfined`` nor ``systempaths=unconfined`` and the bwrap probe
    fails. So on two of the three shapes the logs are merely unbound rather than
    masked, which the ``doctor`` check reports rather than papers over — both
    counts of it, since a deployment can fail on availability and on path at
    once (ISSUE-381). The boundary in every case is that nothing binds the
    path.

    A configured value is used **as given**. Nothing expands ``~`` or resolves
    against a base, matching every other path in ``config.py``; only
    surrounding whitespace is dropped, since a rendered config is where a stray
    space comes from and a directory named with one is not. **A relative value
    therefore follows each process's own working directory**, so the daemon,
    the scheduler's sweep and an ``istota`` command run from a shell can
    address three different places from one config file. That is a reason to
    write an absolute path rather than a property of this function, and it is
    stated here because the opposite claim — that resolving would be the thing
    to cause disagreement — is the wrong way round and was in an earlier draft
    of this docstring. An operator who points the value outside
    ``db_path.parent`` gets a ``WARN`` from ``doctor`` naming the exposure, not
    a refusal.

    **A value that names no directory of its own is refused** and falls back to
    the default: ``/``, ``//``, ``.``, ``..``, ``../..`` and ``a/..``. The point
    is not tidiness. The resolved directory is handed to
    :func:`sweep_session_logs`, which treats every subdirectory of it as a user
    and unlinks ``*.jsonl`` under each, so ``dir = "/"`` is a whole-filesystem
    delete and ``dir = ".."`` is the parent of whatever directory the daemon
    happens to be in. A null byte is refused for the same reason: it survives
    ``Path`` untouched and surfaces as a ``ValueError`` from somewhere much
    further down, where an ``except OSError`` will not catch it.

    **This is not general containment and must not be read as it.** ``dir =
    "/var/log"`` still resolves to ``/var/log`` and the sweep would still walk
    it. Bounding an operator-set root against an ancestor is a rule the
    delete path has to carry — the shape ``sandbox_cache_sweeper`` states as an
    equality and ``worktree_reaper`` as containment — and it belongs with the
    sweep rather than here, where refusing more would contradict the specified
    behaviour that a relative directory is honoured as written.

    Never raises, for the annotated types and outside them: a ``db_path`` or a
    ``configured`` of some other type takes the default rather than a
    ``TypeError``, because the callers are the task path, a scheduler tick and
    ``doctor``, and none of the three has anywhere to put an exception. A
    missing ``db_path`` yields the relative ``logs``.

    **A relative ``db_path`` yields a relative log directory, and the dataclass
    default is relative.** ``Config.db_path`` defaults to ``data/istota.db``, so
    a config that sets neither key resolves to ``data/logs``, followed against
    each process's own working directory — the same property a configured
    relative ``dir`` has, arrived at without anybody configuring anything. Every
    shipped installer writes an absolute ``db_path`` (the Ansible template, the
    Docker render, and ``setup_wizard``'s workspace-derived path), so this is
    reachable from a hand-written config rather than from a deployment. It costs
    consistency rather than data: the writer and the sweep both run in the
    daemon and agree, while ``istota doctor`` from a shell inspects whatever
    ``data/logs`` means there.
    """
    text = configured.strip() if isinstance(configured, str) else ""
    if text and "\x00" not in text:
        candidate = Path(text)
        # `.name` is empty for `/`, `//` and `.`. It is **not** empty for `..`
        # — pathlib keeps that as an ordinary name — so the second test is
        # needed and is not belt-and-braces: without it `dir = ".."` reaches
        # the sweep as the parent of the daemon's working directory.
        if candidate.name and candidate.name != "..":
            return candidate
        logger.warning(
            "[session_log] dir=%r names no directory of its own; using the "
            "default beside db_path instead", configured,
        )
    elif text:
        logger.warning("[session_log] dir contains a null byte; using the default instead")

    if not isinstance(db_path, (str, Path)):
        db_path = None
    return Path(db_path or "istota.db").parent / LOG_DIR_NAME


def is_one_component(value: str) -> bool:
    """Whether *value* is a single, ordinary path component.

    ``user_id`` reaches the writer from the task row rather than from anything
    the model wrote, so this is defence in depth rather than the boundary. It is
    cheap, and it is the difference between a directory under the log root and
    an append somewhere else on the disk.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        return False
    if "\x00" in value:
        return False
    if os.sep in value or (os.altsep and os.altsep in value):
        return False
    return value == os.path.basename(value)


def _short_suffix(session_id: str) -> str:
    """Four alphanumerics off the session id, for a colliding file name."""
    cleaned = "".join(ch for ch in str(session_id) if ch.isalnum()).lower()
    return cleaned[:4] if len(cleaned) >= 4 else uuid.uuid4().hex[:4]


def session_log_path(
    root: Path | str,
    ident: SessionLogIdentity,
    now: datetime,
    *,
    session_id: str = "",
) -> Path:
    """``{root}/{user_id}/{timestamp}_task-{id}-{attempt}.jsonl``.

    The timestamp is ISO 8601 UTC with ``:`` and ``.`` replaced by ``-``, as pi
    does, so lexical order is chronological order and the name is safe on any
    filesystem. ``task-{id}-{attempt}`` is what makes ``ls | grep task-4471``
    enough to find every attempt of a task without opening a file.

    A path that already exists gets a short suffix off *session_id* rather than
    being reused: a ``usage_limit`` reroute to a fallback brain can produce a
    second native run for the same ``(task_id, attempt)``, and overwriting the
    first would destroy the record of the run that failed.

    Raises ``ValueError`` for a ``user_id`` that is not a single path component.
    The writer catches it, since the writer never raises.
    """
    if not is_one_component(ident.user_id):
        raise ValueError(f"user_id is not a single path component: {ident.user_id!r}")
    stamp = _iso_ms(now).replace(":", "-").replace(".", "-")
    base = f"{stamp}_task-{int(ident.task_id)}-{int(ident.attempt)}"
    directory = Path(root) / ident.user_id

    # `lexists`, not `exists`: the writer opens with `O_EXCL`, which refuses a
    # dangling symlink where `exists()` follows it and reports nothing there.
    # The two disagreeing meant the retry recomputed the same name eight times
    # and disabled the log for the whole attempt rather than renaming past it.
    candidate = directory / f"{base}{LOG_SUFFIX}"
    if not os.path.lexists(candidate):
        return candidate

    suffix = _short_suffix(session_id)
    candidate = directory / f"{base}-{suffix}{LOG_SUFFIX}"
    n = 2
    while os.path.lexists(candidate) and n < 1000:
        candidate = directory / f"{base}-{suffix}-{n}{LOG_SUFFIX}"
        n += 1
    return candidate


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _cap_text(text: str, limit: int) -> tuple[str, bool, int]:
    """Head-and-tail truncation. Returns ``(text, truncated, chars_total)``.

    Head *and* tail: a long tool result's tail is usually where the error is,
    so a head-only cut discards the one part anybody opens the file for.

    ``limit`` bounds the *result*, note included, rather than the two halves.
    Charging the note to the caller instead let the "cap" make the text longer
    than the input: at ``limit=1`` a two-character string came back with every
    original character plus twenty-five injected ones, flagged ``truncated``
    and reporting ``[truncated 0 chars]``. Below the width where a head, a tail
    and the note between them all fit, there is no head-and-tail to write and
    the text is clipped instead — a degenerate setting, kept honest rather than
    made to work.
    """
    total = len(text)
    if limit <= 0 or total <= limit:
        return text, False, total
    # `total` is the widest the count can print, so this bounds the real note.
    widest_note = len(f"\n… [truncated {total} chars] …\n")
    if limit <= 2 * widest_note:
        return text[:limit], True, total
    half = (limit - widest_note) // 2
    dropped = total - 2 * half
    note = f"\n… [truncated {dropped} chars] …\n"
    return text[:half] + note + text[-half:], True, total


def _image_descriptor(block: ImageContent) -> dict:
    """An image as identity and size, never as bytes.

    ``bytes`` is the *decoded* length, so it is what the image costs rather than
    what its base64 costs. The hash makes two records identifiable as the same
    image without either containing it.
    """
    data = block.data or ""
    try:
        raw = base64.b64decode(data, validate=False)
        decoded = True
    except Exception:
        # Bad padding. Report the encoded form's size rather than nothing, and
        # say that is what happened.
        raw = data.encode("utf-8", "backslashreplace")
        decoded = False
    descriptor = {
        "type": "image",
        "media_type": block.media_type,
        "display_name": block.display_name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not decoded:
        descriptor["decode_error"] = True
    return descriptor


def _cap_arguments(arguments: Any, limit: int) -> Any:
    """The arguments dict, or an honest marker where it is over the cap.

    A marker rather than a clipped string, because a truncated fragment of a
    JSON object is worse than a record that says it dropped one.
    """
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except Exception as exc:
        return {
            "_truncated": True,
            "_unserializable": True,
            "chars_total": 0,
            "preview": f"<{type(exc).__name__}>",
        }
    if limit <= 0 or len(encoded) <= limit:
        return arguments
    preview_chars = max(1, min(_ARGS_PREVIEW_CHARS, limit))
    return {
        "_truncated": True,
        "chars_total": len(encoded),
        "preview": encoded[:preview_chars],
    }


def serialize_content(block: Content, policy: SessionLogPolicy) -> dict | None:
    """One content block as a record fragment.

    ``None`` means the block is deliberately absent: today that is thinking
    under ``include_thinking = False``, which is a drop rather than a cap.
    """
    if isinstance(block, ThinkingContent):
        if not policy.include_thinking:
            return None
        text, truncated, total = _cap_text(block.thinking, policy.max_content_chars)
        record: dict = {"type": "thinking", "thinking": text}
        if truncated:
            record["truncated"] = True
            record["chars_total"] = total
        return record

    if isinstance(block, TextContent):
        text, truncated, total = _cap_text(block.text, policy.max_content_chars)
        record = {"type": "text", "text": text}
        if truncated:
            record["truncated"] = True
            record["chars_total"] = total
        return record

    if isinstance(block, ImageContent):
        return _image_descriptor(block)

    if isinstance(block, ToolCallContent):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "arguments": _cap_arguments(block.arguments, policy.max_args_chars),
        }

    # A block type this module has not learned yet. Recording something the
    # reader can act on beats a serialization_error line that loses the turn.
    return {
        "type": str(getattr(block, "type", "unknown")),
        "unrecognized": True,
        "repr": _cap_text(repr(block), policy.max_content_chars)[0],
    }


def _serialize_blocks(blocks: Iterable[Content], policy: SessionLogPolicy) -> list[dict]:
    out = []
    for block in blocks or ():
        serialized = serialize_content(block, policy)
        if serialized is not None:
            out.append(serialized)
    return out


def _serialize_usage(usage: Usage | None) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": usage.cost_usd,
    }


def serialize_message(msg: Message, policy: SessionLogPolicy) -> dict:
    """One ``istota.llm.types`` message as a record fragment.

    Field names stay ``snake_case``, as they are in Python. Deliberately not
    renamed to pi's ``camelCase``: this is istota's format, and a reader that
    half-matches pi's is worse than one that plainly does not.
    """
    if isinstance(msg, UserMessage):
        return {"role": "user", "content": _serialize_blocks(msg.content, policy)}

    if isinstance(msg, AssistantMessage):
        return {
            "role": "assistant",
            "model": msg.model,
            "stop_reason": msg.stop_reason,
            "error_message": msg.error_message,
            "usage": _serialize_usage(msg.usage),
            "content": _serialize_blocks(msg.content, policy),
        }

    if isinstance(msg, ToolResultMessage):
        return {
            "role": "tool_result",
            "tool_call_id": msg.tool_call_id,
            "tool_name": msg.tool_name,
            "is_error": bool(msg.is_error),
            "content": _serialize_blocks(msg.content, policy),
        }

    return {
        "role": str(getattr(msg, "role", "unknown")),
        "unrecognized": True,
        "content": [],
        "repr": _cap_text(repr(msg), policy.max_content_chars)[0],
    }


def _count_truncations(obj: Any, depth: int = 0) -> int:
    """Whether anything in a record was capped, so ``result`` can report it.

    Two bounds, and both are about the same thing: a record contains one blob
    this module did not build, and that blob is JSON the *model* emitted.

    ``arguments`` is never descended into beyond its own marker key. Recursing
    freely there let a 600-deep nested tool argument — 1200 bytes, so far under
    ``max_args_chars`` that the marker never fired — blow the interpreter's
    frame limit and raise out of ``message()``, on the one path Stage 3 calls
    once per turn. It also let a model that wrote a literal ``{"truncated":
    true}`` inflate a counter whose whole job is to tell "the model saw a short
    result" from "the log is short".

    :data:`_MAX_SCAN_DEPTH` is the belt to that pair of braces: the deepest
    marker this module writes sits at depth 5 (record, message, content, block,
    arguments), so nothing below is ours to find anyway.
    """
    if depth > _MAX_SCAN_DEPTH:
        return 0
    if isinstance(obj, dict):
        found = 1 if (obj.get("truncated") is True or obj.get("_truncated") is True) else 0
        for key, value in obj.items():
            if key == "arguments":
                # This module's own marker, and nothing below it.
                if isinstance(value, dict) and value.get("_truncated") is True:
                    found += 1
                continue
            found += _count_truncations(value, depth + 1)
        return found
    if isinstance(obj, list):
        return sum(_count_truncations(value, depth + 1) for value in obj)
    return 0


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------

class SessionLogWriter:
    """One file, one task attempt, append-only, and it never raises.

    ``root=None`` (or ``enabled=False``) is the disabled writer: every method is
    a no-op and :attr:`path` is ``None``. That is how the feature switches off,
    so the caller has no ``if self._log is not None`` at eight call sites.
    """

    def __init__(
        self,
        root: Path | str | None,
        ident: SessionLogIdentity,
        policy: SessionLogPolicy,
        *,
        enabled: bool = True,
    ) -> None:
        self._ident = ident
        self._policy = policy
        self._root = Path(root) if root is not None else None
        self._disabled = self._root is None or not enabled
        self._fh = None
        self._path: Path | None = None
        self._truncated = 0
        self._warned = False
        self._opened = False
        self.session_id = "" if self._disabled else str(uuid.uuid4())

    # -- state -------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def truncated_records(self) -> int:
        return self._truncated

    @property
    def active(self) -> bool:
        """Whether a record written now would land in a file."""
        return not self._disabled and self._fh is not None

    # -- lifecycle ---------------------------------------------------------

    def open(self, header: dict | None = None) -> None:
        """Create the file and write the ``session`` record.

        *header* carries what the caller knows and this module must not import
        to find out: the brain, the provider, the model, the effort. ``type``,
        ``v`` and ``ts`` are this module's and cannot be overridden.
        """
        # `_opened` rather than `_fh is not None`: `close()` clears the handle,
        # so the latter let a second `open()` after a close start a *second*
        # file and orphan the first — silently, with `path` now naming the new
        # one. One attempt is one file.
        if self._disabled or self._opened:
            return
        try:
            # Before any mkdir, not just before the open: `{root}/../escape`
            # creates a directory outside the root whether or not a file ever
            # lands in it.
            if not is_one_component(self._ident.user_id):
                raise ValueError(
                    f"user_id is not a single path component: {self._ident.user_id!r}"
                )
            directory = Path(self._root) / self._ident.user_id
            os.makedirs(self._root, mode=0o700, exist_ok=True)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            self._fh, self._path = self._open_exclusive()
            self._opened = True

            record = {
                "type": "session",
                "v": FORMAT_VERSION,
                "ts": _ts(),
                "session_id": self.session_id,
                "task_id": self._ident.task_id,
                "attempt": self._ident.attempt,
                "user_id": self._ident.user_id,
                "source_type": self._ident.source_type,
                "conversation_token": self._ident.conversation_token,
                "is_group_chat": bool(self._ident.is_group_chat),
            }
            if isinstance(header, dict):
                record.update(
                    {k: v for k, v in header.items() if k not in ("type", "v", "ts")}
                )
        except Exception as exc:
            # The header build is inside the `try` too: it reads a
            # caller-supplied mapping, and "every public method is wrapped" has
            # to mean the whole method.
            self._fail("could not open", exc)
            return
        self._write(record)

    def _open_exclusive(self):
        """``O_EXCL`` so a colliding name is never overwritten, only renamed."""
        now = _utcnow()
        last: Exception | None = None
        for _ in range(8):
            path = session_log_path(self._root, self._ident, now, session_id=self.session_id)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:  # lost the race; take the next name
                last = exc
                continue
            # `utf-8` is load-bearing rather than a default, and it is what
            # makes `backslashreplace` safe here. That handler emits `\xNN` for
            # a codepoint below 0x100, which is *not* valid JSON escape syntax
            # — but under UTF-8 the only unencodable codepoints are the
            # surrogates, which always render as `\uXXXX`, which is. Change the
            # encoding and lines start coming out unparseable.
            handle = os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace")
            return handle, path
        raise last if last is not None else OSError("could not pick a session log name")

    def close(self) -> None:
        handle, self._fh = self._fh, None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception as exc:
            # Nothing a caller can do, and the records are already on their way.
            logger.debug("session log: close failed for %s (%s)", self._path, exc)

    # -- records -----------------------------------------------------------

    def context(
        self,
        system_prompt: str,
        tools: Sequence[str],
        schema_sha: str,
        **extra: Any,
    ) -> None:
        """The system prompt and the tool surface, recorded once.

        Tool *names* plus a hash over the sorted schema JSON: the full schemas
        are large, identical across nearly every task, and their drift is what
        the hash is for.
        """

        def build() -> dict:
            text, truncated, total = _cap_text(system_prompt or "", self._policy.max_content_chars)
            body: dict = {
                "system_prompt": text,
                "tools": list(tools or ()),
                "tools_schema_sha256": schema_sha or "",
            }
            if truncated:
                body["truncated"] = True
                body["chars_total"] = total
            body.update({k: v for k, v in extra.items() if k not in ("type", "ts")})
            return body

        self._record("context", build)

    def message(self, msg: Message) -> None:
        self._record("message", lambda: {"message": serialize_message(msg, self._policy)})

    def compaction(self, **fields: Any) -> None:
        self._record("compaction", lambda: dict(fields))

    def steer(self, text: str) -> None:
        def build() -> dict:
            capped, truncated, total = _cap_text(str(text), self._policy.max_content_chars)
            body: dict = {"text": capped}
            if truncated:
                body["truncated"] = True
                body["chars_total"] = total
            return body

        self._record("steer", build)

    def nudge(self, **fields: Any) -> None:
        self._record("nudge", lambda: dict(fields))

    def error(self, exc: BaseException) -> None:
        def build() -> dict:
            formatted = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            message, _, _ = _cap_text(str(exc), self._policy.max_content_chars)
            tb, _, _ = _cap_text(formatted, self._policy.max_content_chars)
            return {"kind": type(exc).__name__, "message": message, "traceback": tb}

        self._record("error", build)

    def result(self, **fields: Any) -> None:
        """The terminal record. ``result_text`` is deliberately not capped."""
        self._record("result", lambda: dict(fields))

    # -- plumbing ----------------------------------------------------------

    def _record(self, kind: str, build: Callable[[], dict]) -> None:
        if self._disabled or self._fh is None:
            return
        try:
            body = build()
        except Exception as exc:
            # One unserializable object must not end the session log.
            self._write(
                {
                    "type": "serialization_error",
                    "ts": _ts(),
                    "record_type": kind,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        record = {"type": kind, "ts": _ts()}
        # `type` and `ts` are this module's, at every record kind rather than
        # only at `session` and `context`. The reader's contract is stated in
        # terms of them — "`result` is always the last line", "line 1 is a
        # `session` header or the file is unreadable" — so a caller's stray
        # field must not be able to rename a record.
        record.update({k: v for k, v in body.items() if k not in ("type", "ts")})
        self._write(record)

    def _write(self, record: dict) -> None:
        if self._disabled or self._fh is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False)
        except Exception as exc:
            record = {
                "type": "serialization_error",
                "ts": _ts(),
                "record_type": record.get("type") if isinstance(record, dict) else None,
                "error": f"{type(exc).__name__}: {exc}",
            }
            line = json.dumps(record, ensure_ascii=False)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception as exc:
            self._fail("could not write to", exc)
            return
        try:
            if _count_truncations(record):
                self._truncated += 1
        except Exception as exc:
            # The record is already on disk. A statistic must not be the thing
            # that raises out of a writer whose contract is that it does not.
            logger.debug("session log: could not count truncations (%s)", exc)

    def _fail(self, what: str, exc: Exception) -> None:
        """One warning, then silence for the rest of the run."""
        self._disabled = True
        if not self._warned:
            self._warned = True
            logger.warning(
                "Session log disabled for task %s attempt %s: %s %s (%s: %s)",
                self._ident.task_id,
                self._ident.attempt,
                what,
                self._path or self._root,
                type(exc).__name__,
                exc,
            )
        handle, self._fh = self._fh, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SweepResult:
    """What one sweep did.

    ``deleted_size`` is kept apart from ``deleted_age`` rather than summed
    because the two mean different things to an operator: a non-zero one says
    the ceiling is what reclaimed, so ``retention_days`` is not the retention
    actually in force and the effective window is a function of load. That is
    the condition ``doctor`` is meant to surface; no reader exists yet.

    The ceiling pass removes files and never a directory. A user directory it
    empties is collected by the age pass instead, a full ``retention_days``
    after its last write — the cost is inodes rather than bytes, which is the
    same reason directory inodes are left out of the measurement.
    """

    deleted_age: int = 0
    deleted_size: int = 0
    dirs_removed: int = 0
    bytes_after: int = 0
    still_over: bool = False
    errors: int = 0


# Where the scheduler writes down what its last sweep did, and where `doctor`
# reads it back. The two processes are different — the sweep runs on the
# scheduler's cleanup tick, and the check runs at daemon start-up, from `istota
# doctor` and from the web process behind the admin Health pane — so "did the
# ceiling reclaim anything" cannot be module state.
#
# `shared_kv` rather than a table: it is one small JSON row of deployment-wide
# daemon state, on the `avatars.IMPORT_STATE_NAMESPACE` precedent. The leading
# underscore is what keeps it away from the model — `kv_namespaces` reserves the
# prefix, so the `kv` skill refuses the namespace on every verb and the deferred
# op replay refuses it again for a sandboxed task.
#
# The constants and the JSON shape live here, beside `SweepResult`, so the
# writer and the reader cannot disagree about either. The two `shared_kv` calls
# themselves stay with their callers: this module imports nothing from the
# package and `db` is not about to be the exception.
SWEEP_STATE_NAMESPACE = "_session_log_sweep"
SWEEP_STATE_KEY = "last_sweep"


def encode_sweep_state(result: SweepResult, *, now: float) -> str:
    """The row body for the sweep that just finished.

    Every counter, not only ``deleted_size``: the field doctor keys its warning
    on is the interesting one, and an operator reading the row by hand wants the
    rest of the picture beside it.
    """
    return json.dumps(
        {
            "at": _iso_ms(datetime.fromtimestamp(now, tz=timezone.utc)),
            "deleted_age": result.deleted_age,
            "deleted_size": result.deleted_size,
            "dirs_removed": result.dirs_removed,
            "bytes_after": result.bytes_after,
            "still_over": result.still_over,
            "errors": result.errors,
        },
        sort_keys=True,
    )


def decode_sweep_state(raw: object) -> dict | None:
    """The last sweep's record, or ``None`` when there is not a usable one.

    ``None`` rather than a raise on anything unparseable, because the one reader
    is a doctor check on the daemon's start-up path: a row nobody can read is,
    for the operator, indistinguishable from no row at all, and it is certainly
    not worth a traceback.
    """
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass
class _Candidate:
    """One evictable file: where it is, what it costs, when it was last written."""

    path: Path
    size: int
    mtime: float


def _scan_user_dir(directory: Path) -> tuple[int, list[_Candidate], int]:
    """``(bytes, jsonl candidates, errors)`` for one user's tree.

    Everything under the directory counts toward the bytes — an operator's
    stray file fills the volume like any other — but only ``*.jsonl`` is a
    candidate for eviction, because the sweep deletes only what it wrote.
    Directory inodes are not counted; see the module docstring.
    """
    total = 0
    candidates: list[_Candidate] = []
    errors = 0

    def _on_error(exc: OSError) -> None:
        nonlocal errors
        errors += 1
        logger.debug(
            "session log sweep: cannot read %s (%s)", getattr(exc, "filename", "?"), exc
        )

    # Files only: a per-user directory is overhead no eviction can reclaim, so
    # counting directory inodes would leave a many-user deployment permanently
    # over a ceiling nothing could clear.
    for full, info in du.iter_tree(directory, on_error=_on_error):
        size = du.entry_bytes(info)
        total += size
        if full.endswith(LOG_SUFFIX) and not stat.S_ISLNK(info.st_mode):
            candidates.append(_Candidate(Path(full), size, info.st_mtime))
    return total, candidates, errors


def _user_dirs(root: Path) -> tuple[list[Path], int]:
    """The per-user directories under *root*, and how many entries could not be
    read. A symlink is never followed: nothing outside the root is swept."""
    errors = 0

    def _on_error(exc: OSError) -> None:
        nonlocal errors
        # A root that does not exist is a deployment that has run no native
        # task, not an unreadable entry: nothing went unmeasured.
        if isinstance(exc, FileNotFoundError):
            return
        errors += 1
        logger.debug(
            "session log sweep: cannot read %s (%s)", getattr(exc, "filename", "?"), exc
        )

    return du.first_level_dirs(root, on_error=_on_error), errors


def sweep_session_logs(
    root: Path | str,
    *,
    retention_days: int,
    max_total_gb: float,
    now: float,
    floor_gb: float = MIN_MAX_TOTAL_GB,
) -> SweepResult:
    """Apply the age rule, then the deployment-wide ceiling. Never raises.

    **One walk of the tree, not one per rule** (ISSUE-379). Sizes and mtimes are
    collected once and both rules are applied to that result; see the comment
    on the walk for what the second pass used to buy and what replaces it.

    The two rules are independent and the caller's gate must be ``or``:
    ``retention_days = 0`` keeps everything indefinitely by age and still wants
    the disk bound in force, and ``max_total_gb = 0`` drops the bound while the
    age rule carries on.

    *floor_gb* is the clamp :data:`MIN_MAX_TOTAL_GB` describes, exposed so a
    test can exceed a ceiling without writing half a gigabyte. Production
    callers take the default.

    **There is no containment rule on the root, and that is a decision rather
    than an omission.** Every other delete path in this codebase carries one —
    ``sandbox_cache_sweeper`` as an equality against a derived layout,
    ``worktree_reaper`` as containment under ``developer.repos_dir``,
    ``skill_host_paths`` as an allowlist of roots — and each exists because a
    *model-supplied or model-plantable* name is being resolved against a trusted
    base. There is no such name here: the root is the whole input, it comes from
    ``[brain.native.session_log] dir`` in the operator's config file, the tree is
    bound into no sandbox at any path, and a directory inside it can only have
    been created by the writer. So this is trusted the way
    ``security.sandbox_cache_dir`` is trusted, and ``config.example.toml`` says
    so beside the setting rather than leaving an operator to find out here.

    There is also no ancestor to bound it against that would not contradict the
    specified behaviour. ``db_path.parent`` is the only candidate, and requiring
    it would refuse both a perfectly reasonable ``dir = "/var/log/istota"`` and
    the relative value :func:`resolve_session_log_dir` is required to honour as
    given. What bounds a mis-set root instead is the shape of the walk, and it is
    worth stating exactly rather than generously, since it is the whole of what
    the operator is being asked to accept: only ``*.jsonl``, only beneath a
    **first-level subdirectory** of the root and never a file at the root's own
    level, at **any depth** within such a subdirectory — :func:`_scan_user_dir`
    walks a user's tree recursively — never through a symlinked entry at either
    level, and ``rmdir`` only on a directory this sweep found empty and older
    than the window. So ``dir = "/var/log"`` reaches every ``*.jsonl`` under
    every subdirectory of ``/var/log``, however deep, and nothing directly in
    ``/var/log`` itself.
    """
    root = Path(root)
    deleted_age = deleted_size = dirs_removed = 0

    directories, errors = _user_dirs(root)

    # -- one walk, both rules ---------------------------------------------
    # The two rules used to walk separately, so a tick with `retention_days > 0`
    # — the shipped default — read the whole tree twice (ISSUE-379). Nobody
    # notices tens of milliseconds on local disk, but `dir` is a free-form
    # operator setting with no containment rule, so the cost is a function of a
    # directory that can be a network mount — paid synchronously on the
    # scheduler's cleanup tick, which is `briefing_check_interval` and so about
    # once a minute by default rather than once a night.
    #
    # What the second walk bought was accounting: it re-stat'ed, so the byte
    # totals were the post-age truth for free. Those totals are now kept by
    # hand as the age rule deletes, which is the whole of what this collapse
    # costs in complexity, and `TestTheSweepWalksOnce` is what holds the two
    # halves equal.
    #
    # One consequence had to be closed rather than documented, and the first
    # draft of this comment got it backwards by calling it nested. The ceiling
    # now reasons about mtimes read *before* the age deletions, so the stale
    # window is the age pass **prepended to** the eviction loop's own, not
    # inside it — and `LIVE_WINDOW_SECONDS` bounds which files are at risk, not
    # how stale the reading is. A native task quiet at scan time and writing
    # again during the age pass was measured as idle and evicted, where the
    # second walk had re-stat'ed and spared it. So the eviction loop re-reads
    # each victim immediately before unlinking it, which costs one `lstat` per
    # eviction rather than one per file and closes the loop's own pre-existing
    # half of the same gap.
    #
    # What is left is one-directional and safe: a file that *grew* between the
    # walk and the eviction is still measured at its earlier size, so the tree
    # is under-counted and the ceiling evicts less rather than more.
    sizes: dict[Path, int] = {}
    files: dict[Path, list[_Candidate]] = {}
    dir_mtimes: dict[Path, float] = {}

    for directory in directories:
        if retention_days > 0:
            try:
                # Read before the deletions: unlinking a file stamps its parent
                # `now`, and the empty-directory gate below would then never
                # fire for a directory this sweep emptied. Only the age rule
                # asks the question, so only the age rule pays the `lstat`.
                dir_mtimes[directory] = directory.lstat().st_mtime
            except OSError:
                errors += 1
        size, candidates, scan_errors = _scan_user_dir(directory)
        errors += scan_errors
        sizes[directory] = size
        files[directory] = candidates

    # -- age, for privacy --------------------------------------------------
    if retention_days > 0:
        cutoff = now - retention_days * _SECONDS_PER_DAY
        for directory in list(files):
            if directory not in dir_mtimes:
                # Its own mtime could not be read, which is the state the
                # two-walk version skipped the whole age pass in. Its bytes are
                # still measured, exactly as they were then.
                continue

            kept: list[_Candidate] = []
            for candidate in files[directory]:
                if candidate.mtime >= cutoff:
                    kept.append(candidate)
                    continue
                try:
                    candidate.path.unlink()
                except FileNotFoundError:
                    # Gone before we got to it. Its bytes are off the volume, so
                    # the running total has to come down and it must not go back
                    # on the eviction list — there is nothing there to evict.
                    # The second walk got this free by simply not finding the
                    # file; keeping the bytes here overstated the tree, and
                    # because the phantom belongs to one user while
                    # largest-user-first picks another, it evicted a live file
                    # of somebody else's to reclaim space that was already free.
                    # The ceiling loop below has argued this since it was
                    # written: on a delete path an accounting error that rounds
                    # toward more deletion is the wrong direction.
                    #
                    # Not an error either, and that is a deliberate change from
                    # the old `except OSError` that swallowed this case. The
                    # count is rendered as "N path(s) could not be processed",
                    # and a file already off the volume was processed. The
                    # ceiling loop already declines to count it; the two rules
                    # now agree about one event.
                    sizes[directory] -= candidate.size
                    continue
                except OSError as exc:
                    errors += 1
                    logger.debug("session log sweep: cannot remove %s (%s)", candidate.path, exc)
                    # Back on the list, not off it. The rescan used to put a
                    # refused unlink in front of the ceiling on its own, and
                    # dropping it here would exempt the one file the sweep
                    # already knows it is having trouble with.
                    kept.append(candidate)
                    continue
                deleted_age += 1
                sizes[directory] -= candidate.size
            files[directory] = kept

            # Only once the directory itself has gone untouched past the
            # window: `open` creates it and writes its first record a moment
            # later, and a tick in between must not rmdir it out from under an
            # in-flight task. Same gate `cleanup_old_temp_files` carries.
            if dir_mtimes[directory] < cutoff:
                try:
                    directory.rmdir()  # only succeeds if empty
                    dirs_removed += 1
                except OSError:
                    pass

    # -- bytes, for the disk ----------------------------------------------
    # The `is_dir()` probe survives the collapse even though the walk it used to
    # guard is gone. It is one stat per user directory against a whole rescan,
    # and it is what keeps a directory that stopped existing during the age pass
    # — this sweep's own `rmdir`, or anything else removing one — from carrying
    # its measured bytes into the ceiling as pressure no longer on the volume.
    # Doing this by bookkeeping off the `rmdir` alone covered the first cause
    # and not the second.
    total = 0
    per_user: dict[Path, int] = {}
    evictable: dict[Path, list[_Candidate]] = {}
    live_cutoff = now - LIVE_WINDOW_SECONDS

    for directory, candidates in files.items():
        if not directory.is_dir():
            continue
        per_user[directory] = sizes[directory]
        total += sizes[directory]
        # Oldest first within a user, and never a file a run may be writing now.
        evictable[directory] = sorted(
            (c for c in candidates if c.mtime <= live_cutoff),
            key=lambda c: (c.mtime, str(c.path)),
        )

    still_over = False
    # `isfinite` before the comparison: TOML accepts `inf` as a float, so
    # `max_total_gb = inf` is a plausible operator spelling of "no ceiling" —
    # and `int(inf * _GIB)` raises `OverflowError` out of a module whose whole
    # contract is that it does not. Non-finite reads as no ceiling, which is
    # what the operator meant.
    if math.isfinite(max_total_gb) and math.isfinite(floor_gb) and max_total_gb > 0:
        ceiling = int(max(max_total_gb, floor_gb) * _GIB)
        while total > ceiling:
            # Largest-user-first: water-filling trims the heaviest producer
            # toward the others rather than evicting a quiet user's whole
            # history — which is what plain global oldest-first does, since the
            # globally oldest files belong to the quietest users.
            heaviest = None
            for directory in sorted(per_user, key=lambda d: (-per_user[d], str(d))):
                if evictable.get(directory):
                    heaviest = directory
                    break
            if heaviest is None:
                # Everything left is live, or is not ours to delete.
                still_over = True
                break

            victim = evictable[heaviest].pop(0)

            # Re-read it immediately before removing it. This candidate's mtime
            # comes from the single walk, which ran before the age pass, so it
            # is as stale as the whole sweep — and `LIVE_WINDOW_SECONDS` only
            # says which files are at risk of that, not how stale the reading
            # is. A task that was quiet at scan time and has written since is
            # using this file now. One stat per eviction, not per file.
            try:
                fresh_mtime = victim.path.lstat().st_mtime
            except FileNotFoundError:
                total -= victim.size
                per_user[heaviest] -= victim.size
                continue
            except OSError as exc:
                errors += 1
                logger.debug("session log sweep: cannot stat %s (%s)", victim.path, exc)
                continue
            if fresh_mtime > live_cutoff:
                # Written to since the walk. Off the candidate list and its
                # bytes stay on the total, so if everything left is live the
                # loop runs out of candidates and reports `still_over` — which
                # is the honest answer rather than taking a file in use.
                continue

            try:
                victim.path.unlink()
            except FileNotFoundError:
                # Somebody else removed it between the walk and here. Its bytes
                # are off the volume either way, so the running total has to
                # come down: leaving it up made the loop evict a second file to
                # reclaim space that was already free, and reported a
                # `bytes_after` describing a tree that no longer existed. On a
                # delete path an accounting error that rounds toward more
                # deletion is the wrong direction.
                #
                # `victim.size` and never the fresh stat's: the running total was
                # built from the walk's numbers, so it has to be drained with the
                # same ones or the two desync.
                total -= victim.size
                per_user[heaviest] -= victim.size
                continue
            except OSError as exc:
                errors += 1
                logger.debug("session log sweep: cannot remove %s (%s)", victim.path, exc)
                continue
            deleted_size += 1
            total -= victim.size
            per_user[heaviest] -= victim.size

    return SweepResult(
        deleted_age=deleted_age,
        deleted_size=deleted_size,
        dirs_removed=dirs_removed,
        bytes_after=total,
        still_over=still_over,
        errors=errors,
    )
