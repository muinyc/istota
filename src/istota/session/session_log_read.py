"""Reading a session log back: one set of parsing rules, two consumers.

:mod:`istota.session.session_log` writes an append-only JSONL transcript of one
``NativeBrain`` task attempt. Two things read it — the operator's ``istota
session list|show|tail|stats`` and the ``tasks transcript`` skill verb a running
task uses to inspect one of its user's *finished* runs — and they ask the same
questions of the same bytes. This module is where those questions are answered,
rather than inline in either caller, because a second copy of the parsing is how
the two start disagreeing about what a transcript says: the CLI would render a
file the skill calls unavailable, or vice versa, and neither would be wrong
about anything a test could see.

**The rules are what this module is, and three of them are the difference
between a reader and a ``json.loads`` loop.**

*A file whose first line is not a ``session`` record is unreadable, and is
reported that way rather than rendered.* A loop renders whatever parses, which
is how a truncated file, somebody else's JSONL, or a log whose header write
failed gets presented as a transcript. pi draws the same line, and the writer's
contract makes it safe to: ``open()`` writes the header before any other record
can reach the handle, so line 1 is the header on every file that has anything in
it at all.

*A malformed line in the middle is skipped and **counted**.* Skipping silently
is worse than refusing the file, because the caller then believes it saw the
whole run. Every entry point that reads a body returns the count.

*A trailing line with no newline is a live write, not damage.* The writer
appends and flushes whole lines, so a partial tail means a run happening right
now — ``session tail`` on a live file is the ordinary case. It is counted apart
from a malformed line for exactly that reason.

**Nothing here raises, and that holds outside the annotated types as well as
inside them.** One caller is a CLI a script runs and the other answers a task,
where a traceback is a failed task rather than a rendering glitch. A path that is
a directory, a file that vanished between the listing and the read, an unreadable
mode, a byte sequence that is not UTF-8, a root that is ``None`` because the
manifest variable carrying it was never set — all answers rather than tracebacks.
The entry points return a ``dict`` carrying ``ok`` and a ``reason``, or an empty
list, or ``None``.

The half of that which is easy to lose is the **record contents**. Every field
read out of a line comes from ``json.loads``, so its type is whatever was on the
line rather than whatever the writer meant — the premise of the whole module is
that the file may be damaged, half-written, or somebody else's JSONL opening
with a plausible header. ``len(record["summary"])`` on a number is a
``TypeError``, and it escaped ``digest`` and reached the CLI as a traceback until
``_as_str`` / ``_as_list`` were put in front of every such read.

**Two finders, and the split is a boundary rather than a convenience.**
:func:`find_logs` takes a ``user_id`` and refuses anything that is not a single
path component — including the empty string, which must never quietly widen to
"every user", since the skill verb's whole scoping story is that it passes
``ISTOTA_USER_ID`` and gets that user's files. :func:`find_all_logs` enumerates
every user and exists for the operator CLI, which is already running as the
daemon user with the whole tree in front of it. A caller that means one user
cannot reach the second function by passing a falsy value to the first.

**Rendering is not here.** ``digest`` and ``excerpt`` return data. The CLI
renders it for a human; the skill verb renders it for a model, wrapped in the
untrusted-content delimiters its own module owns — a tool result in a session
log is raw web page, email body and feed item, and framing it is the consumer's
job because only the consumer knows who is about to read it.

stdlib-only apart from :mod:`istota.session.session_log`, which it imports for
the file-name convention and the ``user_id`` component test, and :mod:`istota.du`
for the first-level directory scan ``find_all_logs`` shares with the sweep. The
first two are shared with the writer by definition, and restating them here
would be the same two copies this module exists to prevent; ``du`` is a leaf
that imports nothing from the package. Roots and paths are parameters.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from istota import du
from istota.session.session_log import LOG_SUFFIX, is_one_component

# How much of the end of a file `read_last_record` reads before giving up on a
# cheap answer and falling back to a full scan. One record is bounded at roughly
# `max_content_chars` plus overhead, so 256 KiB covers the shipped policy several
# times over; `result_text` is deliberately uncapped by the writer, which is why
# there is a fallback at all rather than a promise.
_TAIL_WINDOW = 256 * 1024

# What a `_malformed` placeholder carries of the line it could not parse, under
# `skip_malformed=False`. Enough to recognise, far short of a record.
_RAW_PREVIEW_CHARS = 500

# How much of `result_text` the digest's preview carries. The digest is read at
# a glance; the deliverable itself is what `show` and `--turn` are for.
_RESULT_PREVIEW_CHARS = 400

# How much of a tool call's arguments the digest renders. Enough to see which
# command ran.
_ARGS_PREVIEW_CHARS = 240

# Bounds on `--grep`'s *input*, and it is worth being exact about what they buy,
# because the first version of this comment claimed a safety property these
# numbers do not have. They cap the work an ordinary pattern does, which is
# linear in the subject: 64 KiB of one record's text is far past what "find
# where it said X" needs, and it keeps a scan over a multi-megabyte transcript
# from being paced by its largest record.
#
# **They do not bound a catastrophically backtracking pattern, and nothing here
# does.** Backtracking is exponential in the *subject*, not a product of the two
# numbers, so these ceilings sit orders of magnitude past where it matters —
# measured in this tree, `(a+)+b` against 28 characters of one text block takes
# 19 seconds, and 28 is 2340 times under the subject ceiling. `re` has no step
# limit and no stdlib mechanism interrupts a scan already running, so a real
# bound means a different engine or a structural refusal — and a structural one
# with false negatives would be this same overclaim wearing a check.
#
# So the residual is stated rather than closed, and its size depends entirely on
# who writes the pattern. Here it is an operator at a terminal, who stalls their
# own shell and ends it with Ctrl-C. That stops being the exposure the moment a
# *model* writes the pattern and the scan runs host-side in the daemon's
# namespace with a task waiting on it: the consumer that does that owns closing
# this, and must not inherit these two numbers believing it is already closed.
_MAX_GREP_PATTERN_CHARS = 200
_MAX_GREP_SUBJECT_CHARS = 65536

# What "the conversation" is, for a selector that does not narrow it. The three
# kinds left out are the ones that are not turns: the `session` header and the
# `context` block, which `digest` reports on their own, and `result`, which every
# consumer renders as the terminal line rather than as part of the run.
#
# **A steer and a nudge are in it, and that is the reason this is a named list
# rather than `kind == "message"`.** Both are mid-run injections, and a
# transcript that drops them is unexplainable exactly where it matters most: a
# user turn appears in the middle of an agent loop with nothing saying where it
# came from, so a reader attributes to the user something the framework said, or
# reads a steered run as one the model wandered into. `serialization_error` is
# here for the same reason a malformed line is counted — it marks a record that
# was lost, and its value is entirely positional.
_CONVERSATION_KINDS = (
    "message", "compaction", "steer", "nudge", "error", "serialization_error",
)

# `{stamp}_task-{id}-{attempt}` plus an optional collision suffix, which
# `session_log_path` appends as `-{4 alnum}` and then `-{n}` on a second
# collision. The task id and the attempt are the two fields the name exists to
# carry, so they are anchored and everything after them is opaque.
#
# Two details that are hardening rather than live defects, since the writer
# produces neither shape and NAME_MAX stops the first from existing on disk at
# all. The digit runs are bounded because `int()` raises `ValueError` past
# CPython's 4300-digit conversion limit, and this function is in a module whose
# contract is that it does not raise. `\A`/`\Z` rather than `^`/`$` because `$`
# also matches before a trailing newline, so a name ending in one would parse.
_NAME_RE = re.compile(
    r"\A(?P<stamp>[^_]+)_task-(?P<task_id>\d{1,18})-(?P<attempt>\d{1,9})"
    r"(?:-(?P<suffix>.+))?\Z"
)


@dataclass
class ReadStats:
    """What a pass over a file cost, beside the records it produced.

    ``malformed`` and ``partial_tail`` are separate because they mean different
    things: one is damage and the other is a run in progress. Collapsing them
    would make ``session tail`` on a live file report corruption on every poll.
    """

    records: int = 0
    malformed: int = 0
    partial_tail: int = 0
    unreadable: str = ""
    lines: int = 0


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------

def read_records(
    path: Path | str,
    *,
    skip_malformed: bool = True,
    stats: ReadStats | None = None,
) -> Iterator[dict]:
    """Yield each record in file order. Never raises.

    *stats* is how the counts get out of a generator: the caller owns the object
    and reads it once the iterator is exhausted. The alternative — returning a
    tuple — would make the common case (walk the records) carry the uncommon one.

    With ``skip_malformed=False`` a line that will not parse is yielded as
    ``{"type": "malformed", "line": n, "error": …, "raw": …}`` rather than
    raising, so a caller that wants to *show* the damage can. Nothing in this
    module raises on a bad line, at any setting; the flag chooses between
    dropping it and surfacing it.
    """
    st = stats if stats is not None else ReadStats()
    if _as_path(path) is None:
        st.unreadable = f"TypeError: not a path: {type(path).__name__}"
        return
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        # `ValueError` and not only `OSError`: a path carrying a NUL byte raises
        # `ValueError: embedded null byte` out of `open`, `stat` and `scandir`
        # alike, and that is not an `OSError`, so every plain `except OSError`
        # in this module would miss it. The one caller that hands these
        # functions a name from outside the daemon is the skill verb, whose
        # target the model writes. `session_log.resolve_session_log_dir` names
        # the same hazard for the same reason.
        st.unreadable = f"{type(exc).__name__}: {exc}"
        return
    try:
        with handle:
            for number, raw in enumerate(handle, start=1):
                st.lines = number
                if not raw.endswith("\n"):
                    # No terminator: the writer had not finished this line when
                    # we read, or the process died mid-append. Either way it is
                    # not a record, and it is the last thing in the file.
                    if raw.strip():
                        st.partial_tail += 1
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    # `RecursionError` is the deep-nesting case the writer
                    # already paid for in `_count_truncations`: a line of
                    # 200,000 open brackets exhausts the interpreter's frame
                    # limit inside the decoder, and it is not a `ValueError`.
                    # A line that cannot be decoded is malformed whichever way
                    # the decoder gave up.
                    st.malformed += 1
                    if not skip_malformed:
                        yield {
                            "type": "malformed",
                            "line": number,
                            "error": str(exc),
                            "raw": line[:_RAW_PREVIEW_CHARS],
                        }
                    continue
                if not isinstance(record, dict):
                    # Valid JSON, not a record. Counted as damage rather than
                    # passed on: every consumer below does `record.get(...)`.
                    st.malformed += 1
                    if not skip_malformed:
                        yield {
                            "type": "malformed",
                            "line": number,
                            "error": "not a JSON object",
                            "raw": line[:_RAW_PREVIEW_CHARS],
                        }
                    continue
                st.records += 1
                yield record
    except (OSError, ValueError) as exc:  # a read that fails partway through
        st.unreadable = f"{type(exc).__name__}: {exc}"


def read_header(path: Path | str) -> dict | None:
    """The ``session`` record on line 1, or ``None`` if there is not one.

    ``None`` is the whole "this file is not a transcript" answer, and it is
    deliberately not distinguished from "this file does not exist": both mean
    there is nothing here to read, and the callers that need the difference have
    already stat'ed the path. Reads one line, so it stays cheap on a listing of
    files that run to megabytes.
    """
    if _as_path(path) is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except (OSError, ValueError):
        return None
    if not first.strip():
        return None
    try:
        record = json.loads(first)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session":
        return None
    return record


def read_last_record(path: Path | str) -> dict | None:
    """The last complete record, or ``None``.

    Read backward from the end rather than forward from the start: the listing
    wants a finished run's ``stop_reason`` from every file it shows, and reading
    each one whole to find its last line would make ``session list`` cost the
    size of the tree. A trailing line with no newline is a live write and is not
    the last record.

    Falls back to a forward scan when the window holds no complete line, which
    is what makes an uncapped ``result_text`` safe: the writer bounds every
    record except that one.
    """
    if _as_path(path) is None:
        return None
    try:
        size = os.path.getsize(path)
    except (OSError, ValueError):
        return None
    if size <= 0:
        return None
    try:
        with open(path, "rb") as handle:
            window = min(size, _TAIL_WINDOW)
            handle.seek(size - window)
            chunk = handle.read(window)
    except (OSError, ValueError):
        return None

    text = chunk.decode("utf-8", "replace")
    if window < size:
        # The first line in the window is almost certainly a fragment of a
        # record that started before it.
        _, _, text = text.partition("\n")
    lines = text.split("\n")
    if lines and lines[-1].strip():
        # No terminator on the final line: a live write, not a record.
        lines.pop()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            return record

    if window >= size:
        return None
    # Nothing complete in the window: one record is larger than it. Pay for the
    # forward scan rather than report a file's last line missing.
    last = None
    for record in read_records(path):
        last = record
    return last


# --------------------------------------------------------------------------
# Finding files
# --------------------------------------------------------------------------

def parse_log_name(name: str) -> dict | None:
    """``{stamp, task_id, attempt, suffix}`` from a log's file name, or ``None``.

    The name is what makes ``ls | grep task-4471`` enough to find every attempt
    of a task without opening a file, and it is also the only identity a file
    whose header is unreadable still has — which is why a listing can show a
    damaged file's task and attempt at all.
    """
    if not isinstance(name, str) or not name.endswith(LOG_SUFFIX):
        return None
    match = _NAME_RE.match(name[: -len(LOG_SUFFIX)])
    if not match:
        return None
    return {
        "stamp": match.group("stamp"),
        "task_id": int(match.group("task_id")),
        "attempt": int(match.group("attempt")),
        "suffix": match.group("suffix") or "",
    }


def _logs_in(directory: Path, task_id: int | None) -> list[Path]:
    """Every log directly in *directory*, newest first, symlinks excluded.

    Neither the directory itself nor any entry in it may be a symlink. Both
    halves are needed and only one of them is obvious: a symlinked *file* would
    read a log from outside the root, and a symlinked *user directory* would
    hand back every file under whatever it points at, filed under the user whose
    name is on the link. The tree is bound into no sandbox, so a link in it
    should not exist at all — this is defence in depth behind that, and it is
    the finder the skill verb's ``ISTOTA_USER_ID`` scoping calls.
    """
    try:
        if directory.is_symlink():
            return []
    except (OSError, ValueError):
        return []
    try:
        entries = list(os.scandir(directory))
    except (OSError, ValueError):
        return []
    found: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except (OSError, ValueError):
            continue
        parsed = parse_log_name(entry.name)
        if parsed is None:
            continue
        if task_id is not None and parsed["task_id"] != int(task_id):
            continue
        found.append(Path(entry.path))
    # The stamp leads the name and is ISO 8601, so lexical order is
    # chronological order — reversed, newest first, which is what every caller
    # wants first.
    return sorted(found, key=lambda p: p.name, reverse=True)


def find_logs(
    root: Path | str, user_id: str, *, task_id: int | None = None
) -> list[Path]:
    """One user's logs, newest first. Never crosses into another user's.

    The ``user_id`` is held to :func:`~istota.session.session_log.is_one_component`,
    so ``""``, ``"."``, ``".."`` and anything with a separator find nothing
    rather than resolving somewhere else. The empty string mattering is the point:
    the skill verb's scoping is its whole boundary, and a falsy id that quietly
    meant "every user" would turn one missing environment variable into a
    cross-user read. :func:`find_all_logs` is the deliberate, separately named
    way to span users.

    A ``{root}/{user_id}`` that is itself a symlink finds nothing — see
    :func:`_logs_in`. Name containment alone does not give scoping; the entry
    the name resolves to has to be the directory it appears to be.
    """
    base = _as_path(root)
    if base is None or not is_one_component(user_id):
        return []
    return _logs_in(base / user_id, task_id)


def find_all_logs(root: Path | str, *, task_id: int | None = None) -> list[Path]:
    """Every user's logs, newest first. For the operator CLI only.

    Enumerates the per-user directories from disk, which is safe for the same
    reason the sweep's enumeration is: the tree is bound into no sandbox at any
    path, so a directory in it can only have been created by the writer.
    """
    root = _as_path(root)
    if root is None:
        return []
    found: list[Path] = []
    for entry in du.first_level_dirs(root):
        found.extend(_logs_in(entry, task_id))
    return sorted(found, key=lambda p: (p.name, str(p)), reverse=True)


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def record_text(record: Any) -> str:
    """The human text in a record, which is what ``--grep`` runs against.

    Deliberately not the record's JSON. Matching the serialized form means a
    pattern hits field names and ids — ``tool_call_id`` appears in every tool
    result, so grepping it would return the whole file for a word nobody wrote —
    and it would report a match inside a base64-free image descriptor's hash.
    What a reader means by "find where it said X" is the content: text and
    thinking blocks, a tool call's arguments, a tool result's output, a steer, a
    compaction summary, an error message and the final answer.
    """
    if not isinstance(record, dict):
        return ""
    parts: list[str] = []

    def _blocks(blocks: Any) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for key in ("text", "thinking"):
                value = block.get(key)
                if isinstance(value, str):
                    parts.append(value)
            if block.get("type") == "tool_call":
                name = block.get("name")
                if isinstance(name, str):
                    parts.append(name)
                arguments = block.get("arguments")
                if arguments is not None:
                    parts.append(_dumps(arguments))

    kind = record.get("type")
    if kind == "message":
        message = record.get("message")
        if isinstance(message, dict):
            _blocks(message.get("content"))
    elif kind == "context":
        for key in ("system_prompt",):
            value = record.get(key)
            if isinstance(value, str):
                parts.append(value)
    else:
        for key in (
            "text", "summary", "message", "traceback", "result_text", "kind",
            # A `serialization_error`'s own two fields. The writer emits that
            # record when one record would not serialize, so it is the marker
            # for a lost turn — a grep for what went wrong has to be able to
            # reach it.
            "error", "record_type",
        ):
            value = record.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _clip(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        text = _dumps(text)
    return text if len(text) <= limit else text[:limit] + "…"


# Every field below comes out of `json.loads`, so its type is whatever was on
# the line and not whatever the writer meant to put there. A file can be damaged,
# half-written, or somebody else's JSONL that happens to open with a `session`
# record — the module's premise is exactly that — so `len(record.get("summary"))`
# on a number is a `TypeError` out of a function documented never to raise, and
# in the second consumer that is a failed task rather than a rendering glitch.
# `_output_size` already guarded its own `content` this way; these two are the
# same guard for the fields that did not have one.

def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_bool_or_none(value: Any) -> bool | None:
    """A header flag as ``True``/``False``, or ``None`` where the file is silent.

    Three-valued on purpose. A file written before the field existed carries no
    key at all, and collapsing that to ``False`` would state something the run
    never said — for ``is_fallback`` it would label every old fallback
    transcript as the primary and send a reader to the wrong ``task_usage``
    row. A value of the wrong type is the same silence: the header is file
    content, and `json.loads` hands back whatever was on the line.

    ``bool`` is checked rather than truthiness because `isinstance(True, int)`
    is also true, and a header claiming ``1`` is claiming a number.
    """
    return value if isinstance(value, bool) else None


def _as_path(value: Any) -> Path | None:
    """*value* as a ``Path``, or ``None`` where it is not one to begin with.

    The never-raises contract has to hold outside the annotated types as well as
    inside them, which is the same position `session_log.resolve_session_log_dir`
    takes and for a concrete reason: the second consumer reads its root out of a
    ``proxy_only`` manifest variable, so an unwired or unset ``ISTOTA_SESSION_LOG_DIR``
    arrives here as ``None``. `Path(None)` is a `TypeError`, and the spec
    requires that path to answer "no transcript" rather than fail.
    """
    if isinstance(value, Path):
        return value
    try:
        return Path(value)
    except TypeError:
        return None


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------

def summarize(path: Path | str) -> dict:
    """One listing row, from two line reads rather than a whole file.

    A row is produced for a file whose header will not parse, marked
    ``readable: False`` — the identity still comes off the name, and omitting
    the file from the listing would hide the one thing an operator needs to see
    about it. ``complete`` is False when the last record is not a ``result``:
    an interrupted run, which is a different fact from a failed one and is the
    reading a naive "the last line is the result" would get wrong.

    ``user_id``, ``task_id`` and ``attempt`` are the *name and directory's*,
    never the header's, because they are what every lookup filters on. A header
    disagreeing with any of the three is reported as ``header_user_id`` /
    ``header_task_id`` / ``header_attempt`` rather than believed.

    ``is_fallback`` is the exception that proves that rule and comes off the
    header, because the name cannot carry it: both runs of a rerouted attempt
    are ``task-{id}-{attempt}``, so there is nothing to check the claim against.
    It is tri-state — ``None`` where the file is silent, which is every log
    written before the field existed (ISSUE-378).
    """
    path = _as_path(path) or Path("")
    parsed = parse_log_name(path.name) or {}
    row: dict = {
        "path": str(path),
        "name": path.name,
        "user_id": path.parent.name,
        "task_id": parsed.get("task_id"),
        "attempt": parsed.get("attempt"),
        "size": 0,
        "mtime": 0.0,
        "readable": False,
        "reason": "",
        "complete": False,
        "stop_reason": "",
        "success": None,
        "turns": None,
        "model": "",
        "brain": "",
        # Tri-state, never a bare False — see `_as_bool_or_none`.
        "is_fallback": None,
        "header_user_id": "",
        "header_task_id": None,
        "header_attempt": None,
    }
    try:
        info = os.stat(path)
        row["size"] = info.st_size
        row["mtime"] = info.st_mtime
    except (OSError, ValueError) as exc:
        row["reason"] = f"{type(exc).__name__}: {exc}"
        return row

    header = read_header(path)
    if header is None:
        if not path.exists():
            # The sweep unlinks `*.jsonl` under this root on the scheduler's
            # interval, and `read_header` deliberately conflates "gone" with
            # "not a transcript" on the grounds that a caller needing the
            # difference has already stat'ed the path. This is that caller, and
            # the stat above is now stale — so without this check an operator
            # listing a tree during a sweep gets false corruption reports.
            row["reason"] = "file no longer exists"
        else:
            row["reason"] = "no session header on line 1"
        return row

    row["readable"] = True
    for key in ("model", "brain"):
        value = header.get(key)
        if value is not None:
            row[key] = value
    # Which run of a rerouted attempt this file is (ISSUE-378). Not an identity
    # field in the sense the block below means — the name cannot carry it, since
    # both runs of one reroute are `task-{id}-{attempt}` — so unlike `task_id`,
    # `attempt` and `user_id` there is nothing to check the header against and
    # the header is the only source there is.
    row["is_fallback"] = _as_bool_or_none(header.get("is_fallback"))

    # **Three identity fields come off the name and the directory, never off the
    # header, and the rule is one rule.** The name and the directory are what
    # every lookup here filters on — `_logs_in` matches `task_id` against
    # `parse_log_name`, `find_logs` scopes on the directory, and the CLI's
    # `--task` and `--attempt` do the same — so they are the identity that
    # carries the boundary. The header is file content. Taking `task_id` from
    # the header made `session list --task 5000` print a row labelled "task
    # 4471", after which `session show 4471` found nothing: the listing named a
    # task the finder could not resolve. Where the two disagree it is the
    # disagreement that is worth reporting, not the header's answer.
    for key, claimed_key in (("user_id", "user_id"), ("task_id", "task_id"),
                             ("attempt", "attempt")):
        claimed = header.get(claimed_key)
        if claimed is not None and claimed != "" and claimed != row[key]:
            row[f"header_{key}"] = claimed

    last = read_last_record(path)
    if isinstance(last, dict) and last.get("type") == "result":
        row["complete"] = True
        row["stop_reason"] = str(last.get("stop_reason") or "")
        row["success"] = bool(last.get("success"))
        turns = last.get("turns")
        row["turns"] = turns if isinstance(turns, int) else None
    return row


def _empty_digest(path: Path, reason: str) -> dict:
    """A `digest` that could not be produced, carrying every key one that could.

    Same key set on both paths, deliberately. A consumer that checks ``ok``
    reads the reason; one that forgets gets an empty answer rather than a
    ``KeyError``, and there is a real window in which it matters — `digest` and
    `excerpt` are two reads of the same path, and the retention sweep unlinks
    ``*.jsonl`` under that root from the scheduler on an interval, so a file can
    stop existing between them.
    """
    return {
        "ok": False,
        "reason": reason,
        "path": str(path),
        "header": None,
        "context": None,
        "turns": 0,
        "tools": [],
        "compactions": [],
        "steers": 0,
        "nudges": 0,
        "errors": [],
        "serialization_errors": 0,
        "result": None,
        "complete": False,
        "records": 0,
        "malformed": 0,
        "partial_tail": 0,
        "unreadable": reason,
        "size": 0,
        "mtime": 0.0,
    }


def _empty_excerpt(path: Path, reason: str) -> dict:
    """An `excerpt` that could not be produced. See :func:`_empty_digest`."""
    return {
        "ok": False,
        "reason": reason,
        "path": str(path),
        "header": None,
        "records": [],
        "records_total": 0,
        "records_returned": 0,
        "chars": 0,
        "truncated": False,
        "turn": None,
        "turn_count": 0,
        "malformed": 0,
        "partial_tail": 0,
        "unreadable": reason,
    }


def _header_or_reason(path: Path) -> tuple[dict | None, str]:
    if not os.path.exists(path):
        return None, "not found"
    header = read_header(path)
    if header is None:
        return None, "no session header on line 1"
    return header, ""


def digest(path: Path | str) -> dict:
    """What happened on this run, at a glance.

    The tool calls in order with their status and their output *sizes*, the
    compactions with their trigger, any error, and the terminal record — which
    is the shape troubleshooting actually asks for, and the shape that fits in a
    response the ``tasks`` skill caps at a few thousand characters. The
    deliverable itself is not in it: ``result_text`` is uncapped in the file by
    design, so the digest carries its length and a short preview and leaves the
    body to ``excerpt``.

    Tool calls are paired to their results by ``tool_call_id``. A call with no
    result is reported ``answered: False`` rather than dropped — a run that
    timed out mid-tool is exactly the run somebody is reading this for.
    """
    path = _as_path(path) or Path("")
    header, reason = _header_or_reason(path)
    if header is None:
        return _empty_digest(path, reason)

    stats = ReadStats()
    tools: list[dict] = []
    by_call: dict[str, dict] = {}
    compactions: list[dict] = []
    errors: list[dict] = []
    context: dict | None = None
    result: dict | None = None
    turns = 0
    steers = 0
    nudges = 0
    serialization_errors = 0
    last_type = ""

    for record in read_records(path, stats=stats):
        kind = record.get("type")
        last_type = kind if isinstance(kind, str) else ""
        if kind == "context":
            context = {
                "tools": _as_list(record.get("tools")),
                "system_prompt_chars": len(_as_str(record.get("system_prompt"))),
                "system_prompt_source": _as_str(record.get("system_prompt_source")),
                "tools_schema_sha256": _as_str(record.get("tools_schema_sha256")),
            }
        elif kind == "message":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                turns += 1
                for block in _as_list(message.get("content")):
                    if not isinstance(block, dict) or block.get("type") != "tool_call":
                        continue
                    entry = {
                        "seq": len(tools) + 1,
                        "turn": turns,
                        "id": block.get("id") or "",
                        "name": block.get("name") or "",
                        "arguments": _clip(block.get("arguments"), _ARGS_PREVIEW_CHARS),
                        "answered": False,
                        "is_error": None,
                        "output_chars": 0,
                        "output_chars_total": 0,
                        "truncated": False,
                        "images": 0,
                    }
                    tools.append(entry)
                    if entry["id"]:
                        by_call[str(entry["id"])] = entry
            elif role == "tool_result":
                entry = by_call.get(str(message.get("tool_call_id") or ""))
                if entry is None:
                    # A result whose call is not in the file (a compaction cut
                    # the assistant turn, or the log starts mid-run). Recorded
                    # on its own rather than dropped.
                    entry = {
                        "seq": len(tools) + 1,
                        "turn": turns,
                        "id": message.get("tool_call_id") or "",
                        "name": message.get("tool_name") or "",
                        "arguments": "",
                        "answered": False,
                        "is_error": None,
                        "output_chars": 0,
                        "output_chars_total": 0,
                        "truncated": False,
                        "images": 0,
                    }
                    tools.append(entry)
                shown, real, truncated, images = _output_size(message.get("content"))
                entry["answered"] = True
                entry["is_error"] = bool(message.get("is_error"))
                entry["output_chars"] = shown
                entry["output_chars_total"] = real
                entry["truncated"] = truncated
                entry["images"] = images
        elif kind == "compaction":
            compactions.append({
                "trigger": _as_str(record.get("trigger")),
                "recovery_index": record.get("recovery_index"),
                "messages_dropped": record.get("messages_dropped"),
                "tokens_before": record.get("tokens_before"),
                "summary_chars": len(_as_str(record.get("summary"))),
            })
        elif kind == "steer":
            steers += 1
        elif kind == "nudge":
            nudges += 1
        elif kind == "serialization_error":
            # The writer's own marker for a record it could not serialize. It is
            # the same loss a malformed line is, arriving by a different route,
            # so it gets the same treatment: counted and reported. Leaving it
            # uncounted would let a reader believe they saw the whole run, which
            # is the failure the malformed count exists to prevent.
            serialization_errors += 1
        elif kind == "error":
            errors.append({
                "kind": _as_str(record.get("kind")),
                "message": _clip(_as_str(record.get("message")), _RESULT_PREVIEW_CHARS),
            })
        elif kind == "result":
            text = _as_str(record.get("result_text"))
            result = {
                "success": bool(record.get("success")),
                "stop_reason": _as_str(record.get("stop_reason")),
                "model_used": _as_str(record.get("model_used")),
                "duration_ms": record.get("duration_ms"),
                "usage": record.get("usage"),
                "turns": record.get("turns"),
                "compactions": record.get("compactions"),
                "truncated_records": record.get("truncated_records"),
                "result_text_chars": len(text),
                "result_text_preview": _clip(text, _RESULT_PREVIEW_CHARS),
            }

    try:
        info = os.stat(path)
        size, mtime = info.st_size, info.st_mtime
    except OSError:
        size, mtime = 0, 0.0

    return {
        "ok": True,
        "reason": "",
        "path": str(path),
        "header": header,
        "context": context,
        "turns": turns,
        "tools": tools,
        "compactions": compactions,
        "steers": steers,
        "nudges": nudges,
        "errors": errors,
        "serialization_errors": serialization_errors,
        "result": result,
        "complete": last_type == "result",
        "records": stats.records,
        "malformed": stats.malformed,
        "partial_tail": stats.partial_tail,
        # A read that died partway through — an EIO, a network mount going away,
        # the sweep unlinking underneath. `read_records` has always recorded it
        # and nothing read it back, so a truncated record set came out under
        # `ok: True` with an empty reason: the caller believed it saw the whole
        # run, which is the one failure the malformed count exists to prevent,
        # arriving by the route that is counted and then discarded.
        "unreadable": stats.unreadable,
        "size": size,
        "mtime": mtime,
    }


def _output_size(content: Any) -> tuple[int, int, bool, int]:
    """``(chars in the log, chars before the writer's cap, truncated, images)``.

    The first two differ exactly where the writer capped a block, and reporting
    both is what lets a reader tell "the model saw a short result" from "the log
    is short" — the same distinction ``truncated_records`` draws on the terminal
    record.

    **An image is counted as an image and never as characters.** The writer
    stores it as a descriptor of about 150 characters carrying the *decoded byte
    length* of the picture, so folding that number into a character count made a
    one-megabyte screenshot report ``output_chars = 1048576`` for a record that
    occupies a line and a half. Two different units under one name is a figure
    nobody can use.
    """
    shown = 0
    real = 0
    images = 0
    truncated = False
    if not isinstance(content, list):
        return 0, 0, False, 0
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            shown += len(text)
            total = block.get("chars_total")
            real += total if isinstance(total, int) else len(text)
        elif block.get("type") == "image":
            images += 1
        if block.get("truncated") is True:
            truncated = True
    return shown, real, truncated, images


# --------------------------------------------------------------------------
# Excerpts
# --------------------------------------------------------------------------

def excerpt(
    path: Path | str,
    *,
    turn: int | None = None,
    tools: bool = False,
    grep: str | None = None,
    thinking: bool = False,
    max_chars: int = 0,
) -> dict:
    """A selected slice of the conversation, as records.

    The selectors are checked in the order ``turn`` > ``tools`` > ``grep``, and
    with none of them the whole conversation is returned — which is what
    ``session show`` renders. "The conversation" is ``_CONVERSATION_KINDS``, so a
    steer and a nudge come back in their place in the run rather than being
    dropped as not-a-message; see that constant for why. Records come back as
    they are in the file, minus
    thinking blocks unless *thinking* is set: thinking is the bulkiest part of a
    transcript and the least useful for "what went wrong", and handing a model
    its own prior reasoning anchors it on a path it already abandoned. The
    default is therefore off, and the operator CLI turns it on for a human.

    A **turn** is one assistant message plus everything the loop produced before
    the next assistant message — its tool results, and any steer, nudge or
    compaction that landed in between. That is the unit somebody means by "show
    me turn 4", and it is why the selection is not simply "the fourth message".

    *max_chars* bounds the returned records by their serialized size, stopping
    at a whole record rather than clipping one, and always returns at least one
    record: an empty answer reads as "the run had no conversation", which is a
    different and wrong thing to say. ``truncated`` plus ``records_returned`` vs
    ``records_total`` is how the caller reports the cut instead of hiding it.
    ``0`` means no cap.
    """
    path = _as_path(path) or Path("")
    header, reason = _header_or_reason(path)
    if header is None:
        return _empty_excerpt(path, reason)

    if turn is not None and turn < 1:
        # Turns are 1-based because the first assistant message is turn 1. A 0
        # or a negative used to fall through the selector and hand back the
        # assembled prompt, which is the one record the docstring says belongs
        # to no turn — a wrong answer where a refusal was meant.
        return _empty_excerpt(path, f"turn must be 1 or greater, got {turn}")

    pattern = None
    if grep:
        if len(grep) > _MAX_GREP_PATTERN_CHARS:
            return _empty_excerpt(
                path,
                f"grep pattern longer than {_MAX_GREP_PATTERN_CHARS} characters",
            )
        try:
            pattern = re.compile(grep)
        except re.error as exc:
            return _empty_excerpt(path, f"invalid grep pattern: {exc}")

    stats = ReadStats()
    selected: list[dict] = []
    turn_count = 0
    current_turn = 0

    for record in read_records(path, stats=stats):
        kind = record.get("type")
        message = record.get("message") if kind == "message" else None
        role = message.get("role") if isinstance(message, dict) else None
        if role == "assistant":
            turn_count += 1
            current_turn = turn_count

        if turn is not None:
            # A turn is bounded by its assistant message: everything after it up
            # to the next one belongs to it, and the assembled prompt ahead of
            # turn 1 belongs to no turn. `current_turn` is 0 until the first
            # assistant message, and `turn` is refused below 1 above, so this
            # one comparison excludes the pre-turn records on its own.
            if current_turn != turn:
                continue
            if kind not in _CONVERSATION_KINDS:
                continue
        elif tools:
            if role != "tool_result":
                continue
        elif pattern is not None:
            # Grep searches the conversation, the same set every other selector
            # takes, rather than every record in the file. It was the one
            # selector without this line, and the two records it could reach
            # past it are both wrong to return. The `context` record carries the
            # daemon's own system prompt, which is configuration and identical
            # across nearly every task. The `result` record carries
            # `result_text`, which the *writer* deliberately leaves uncapped
            # because it is the deliverable — and since the size cap below
            # always keeps its first record whatever it costs, a one-character
            # pattern matching a result returned a record of unbounded size with
            # `max_chars` in force. The consumer that supplies the pattern is
            # model-facing and its own response budget is 8000 characters.
            if kind not in _CONVERSATION_KINDS:
                continue
            if not pattern.search(record_text(record)[:_MAX_GREP_SUBJECT_CHARS]):
                continue
        else:
            if kind not in _CONVERSATION_KINDS:
                continue

        selected.append(_without_thinking(record) if not thinking else record)

    total = len(selected)
    kept: list[dict] = []
    used = 0
    truncated = False
    for index, record in enumerate(selected):
        cost = len(_dumps(record))
        if max_chars > 0 and index > 0 and used + cost > max_chars:
            truncated = True
            break
        kept.append(record)
        used += cost
    if max_chars > 0 and not truncated and used > max_chars:
        # The single record we always keep was itself over the cap.
        truncated = True

    return {
        "ok": True,
        "reason": "",
        "path": str(path),
        "header": header,
        "records": kept,
        "records_total": total,
        "records_returned": len(kept),
        "chars": used,
        "truncated": truncated,
        "turn": turn,
        "turn_count": turn_count,
        "malformed": stats.malformed,
        "partial_tail": stats.partial_tail,
        "unreadable": stats.unreadable,
    }


def _without_thinking(record: dict) -> dict:
    """A copy with the thinking blocks gone, leaving the original untouched.

    A shallow rebuild of only the two levels that change: the caller may hold
    the record, and mutating it in place would make a second pass over the same
    file disagree with the first.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return record
    content = message.get("content")
    if not isinstance(content, list):
        return record
    kept = [
        block
        for block in content
        if not (isinstance(block, dict) and block.get("type") == "thinking")
    ]
    if len(kept) == len(content):
        return record
    new_message = dict(message)
    new_message["content"] = kept
    new_record = dict(record)
    new_record["message"] = new_message
    return new_record


# --------------------------------------------------------------------------
# Whole-tree numbers
# --------------------------------------------------------------------------

@dataclass
class TreeStats:
    """What ``istota session stats`` reports. Bytes are ``st_size``, not blocks.

    Deliberately different from the sweep's du-style measurement, and there are
    **two** differences, not one. The unit: the sweep bounds what a *volume*
    holds, so it counts blocks, while this answers "how much transcript is
    there", which is content. And the set: this counts only what
    :func:`find_all_logs` yields, which is files whose name `parse_log_name`
    recognises, one level under the root — while the sweep's own scan totals
    every file under a user directory at any depth and treats anything ending
    ``.jsonl`` as an eviction candidate. So an operator's stray file counts
    toward the ceiling, and can be deleted by the sweep, while never appearing
    here.

    The consequence for a reader is that these two numbers agree in order of
    magnitude and never exactly, and that this one is a floor on what the
    ceiling sees rather than an estimate of it. ``doctor``'s
    ``runtime.session_log_dir`` is the check that reports against the ceiling on
    the sweep's own terms.
    """

    files: int = 0
    bytes: int = 0
    per_user: dict = field(default_factory=dict)
    oldest: float = 0.0
    newest: float = 0.0


def tree_stats(root: Path | str, *, since: float | None = None) -> TreeStats:
    """Count and size every log under *root*, split by user. Never raises.

    *since* is a POSIX timestamp floor on mtime, which is what ``--days``
    resolves to. A file that cannot be stat'ed is skipped rather than counted at
    zero, so the total is of what was measurable rather than of what was found.
    """
    out = TreeStats()
    for path in find_all_logs(root):
        try:
            info = os.stat(path)
        except (OSError, ValueError):
            continue
        if since is not None and info.st_mtime < since:
            continue
        user = path.parent.name
        out.files += 1
        out.bytes += info.st_size
        entry = out.per_user.setdefault(user, {"files": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += info.st_size
        if out.oldest == 0.0 or info.st_mtime < out.oldest:
            out.oldest = info.st_mtime
        if info.st_mtime > out.newest:
            out.newest = info.st_mtime
    return out
