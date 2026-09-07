"""ClaudeCodeBrain — wraps the `claude` CLI subprocess.

Owns:
- Building the `claude -p - --allowedTools ...` command.
- Wrapping the command with bubblewrap (via the caller-supplied sandbox_wrap).
- Spawning the subprocess, writing the prompt over stdin.
- Parsing --output-format stream-json into StreamEvents and forwarding them.
- Auto-retry on transient Anthropic API errors (5xx/429).

Result reconciliation (CM-aware composition, malformed-output detection)
stays in the executor — both brains will produce result_text + execution_trace
and need the same downstream cleanup.
"""

import dataclasses
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from ._events import (
    ContextManagementEvent,
    RateLimitEvent,
    RequestUsageEvent,
    ResultEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolUseEvent,
    make_stream_parser,
)
from istota import task_cgroup
from istota import usage as usage_types
from ..process_group import kill_process_group
from ._aliases import CANONICAL_ROLES, split_effort
from ._roles import get_alias_override_target, get_alias_overrides
from ._types import BrainRequest, BrainResult

logger = logging.getLogger("istota.brain.claude_code")


# Pattern to detect Anthropic API errors carrying a JSON body.
API_ERROR_PATTERN = re.compile(r"API Error: (\d{3}) (\{.*\})", re.DOTALL)

# The CLI does not always attach a JSON body — `API Error: 529 Overloaded`,
# `API Error: 500` and `API Error: 400 Bad Request` are all real shapes. Matching
# only the JSON form meant a bare 529 parsed as *nothing*: not transient, so not
# retried, classified as a generic error, and therefore not a fallback trigger
# (ISSUE-212). The tail stops at the newline so a stack trace below the banner
# doesn't become the "message".
_API_ERROR_PLAIN_PATTERN = re.compile(r"API Error:?\s+(\d{3})\b[ \t]*([^\n]*)")

# "API Error" in any of its punctuations — `API Error: …`, `API Error (…)`. Used
# as a *gate* on the text-shaped predicates below so ordinary prose that happens
# to discuss a connection reset is never dragged onto the retry path.
_API_ERROR_MARKER = re.compile(r"API Error\b", re.IGNORECASE)

# Transient HTTP status codes that warrant retry. Documentation of the common
# cases — the live rule is `_status_is_transient`, which treats *every* 5xx as
# transient. Enumerating was a latent version of the bug this fixes: a
# Cloudflare-fronted provider emits 520-526 ("Web Server Returned an Unknown
# Error" / "Connection Timed Out"), none of which were listed, so each would
# dead-end exactly as the 529 did.
TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}  # 529 = overloaded

# Non-5xx statuses that are still capacity/timing signals rather than a problem
# with the request: 408 Request Timeout, 425 Too Early, 429 Too Many Requests.
_TRANSIENT_4XX = frozenset({408, 425, 429})


def _status_is_transient(status: int) -> bool:
    """Whether an HTTP status is a capacity/availability signal worth retrying."""
    return status >= 500 or status in _TRANSIENT_4XX

# Request-shaped statuses: retrying or switching brains cannot help, and doing
# either wastes a call (and on a paid fallback, money). The complement of
# TRANSIENT_STATUS_CODES for the codes we actually see.
PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 405, 413, 414, 422})

# Network/transport failures the CLI reports as an API error. Capacity-shaped in
# the same sense as a 529: the request was fine, the path to the provider wasn't.
_NETWORK_TRANSIENT_RE = re.compile(
    r"connection (?:error|reset|refused|closed|aborted)|"
    r"(?:request|read|socket|connect)?\s*time[d]?\s?out|"
    r"socket hang ?up|network error|fetch failed|premature close|"
    r"getaddrinfo|dns (?:lookup )?fail|"
    # The CLI's own documented capacity-throttle banner: "Server is
    # temporarily limiting requests (not your usage limit)". Explicitly a
    # server-side throttle, so it belongs in the fallback trigger set.
    r"temporarily limiting requests|limiting requests",
    re.IGNORECASE,
)

# Node/libc errno strings are diagnostic enough to stand without the API Error
# marker — they don't occur in prose, and the CLI doesn't always wrap them.
_NET_ERRNO_RE = re.compile(
    r"\b(ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b"
)

# Request-shaped bodies: the *content* of the request is the problem, so no
# amount of retrying or brain-switching changes the outcome.
_REQUEST_SHAPED_RE = re.compile(
    r"invalid_request_error|authentication_error|permission_error|not_found_error|"
    r"content[_ ]filter|prompt is too long|"
    r"context[ _-]?(?:length|window|size)|maximum context",
    re.IGNORECASE,
)

# `retry-after: 30`, `"retry_after": 30`, `Retry-After 30`. `[ \t]*` rather than
# `\s*`: the pair `\s*[:=]?\s*` is ambiguous and backtracks quadratically on a
# long whitespace run, and the input here is unbounded provider/stderr text.
_RETRY_AFTER_RE = re.compile(
    r"retry[-_ ]?after[\"']?[ \t]*[:=]?[ \t]*[\"']?(\d+(?:\.\d+)?)", re.IGNORECASE
)

# Retry configuration for transient API errors
# What a usage row records as the brain that ran. One of KNOWN_BRAIN_KINDS.
BRAIN_KIND = "claude_code"

API_RETRY_MAX_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 5
# Ceiling on a provider-supplied Retry-After. A worker parked on the provider's
# word for an hour is worse than failing the attempt and letting the task's own
# retry ladder (1/4/16 min) or the fallback brain take over.
RETRY_AFTER_MAX_SECONDS = 60.0
# Slice length for the retry backoff, so `!stop` lands within a slice
# instead of waiting out a (now potentially 60s) provider-requested delay.
_RETRY_SLEEP_SLICE_SECONDS = 0.5
# Floor for the image re-issue's remaining budget. The first attempt can have
# consumed the whole timeout, and handing a subprocess a zero or negative one
# turns "degrade to text" into an instant timeout.
_MIN_REISSUE_SECONDS = 30.0


# Frame `type` values the CLI itself emits, and keys only its own envelope
# carries. Together these separate "the CLI changed its envelope" from "the
# model answered with JSON of its own" — a distinction worth drawing carefully,
# because a warning that fires on every structured answer is the false alarm
# that trains an operator to ignore the real one. A model may well use `type`
# in its own schema; it will not report its own token spend.
_CLI_FRAME_TYPES = frozenset({"result", "system", "assistant", "user", "stream_event"})
_CLI_ENVELOPE_KEYS = ("modelUsage", "total_cost_usd", "session_id")


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------
#
# Neither CLI brain can be handed image bytes: `claude -p` reads text on stdin
# and the tmux brain submits a bracketed paste. Their provider-supported image
# path is Claude Code's own `Read` tool, which returns visual content to the
# model rather than raw bytes and does its own final recompression — so
# delivery here is a directive requiring one `Read` per prepared image, and the
# executor's post-run trace audit is what stops the vision claim resting on the
# model's compliance with a prompt instruction (which is the failure shape
# ISSUE-366 records, moved one layer up).
#
# The wording follows `health/ocr._build_vision_prompt`, the shipped
# Read-the-absolute-path directive, rather than inventing a second phrasing for
# the same instruction.

IMAGE_DIRECTIVE_HEADER = "## Image attachments — open these before answering"
IMAGE_OMITTED_HEADER = "## Image attachments — not available on this task"
IMAGE_WITHDRAWN_HEADER = "## Image attachments — withdrawn by the provider"

_IMAGE_DIRECTIVE_BODY = (
    "Read each of the following absolute paths with the Read tool before you "
    "answer or take any other action. Reading an image returns the picture "
    "itself; the path text below is not visual access, and neither is the OCR "
    "section, which is a separate and fallible source. If a Read fails, say so "
    "in your answer and do not guess at what the image shows."
)

_IMAGE_OMITTED_BODY = (
    "The following image attachments were prepared for this task, but it runs "
    "without tools, so there is no way to open them. Answer from the text and "
    "any OCR context, and do not state or imply that you have seen them."
)


def _image_paths(req: BrainRequest) -> list[str]:
    """The resolved absolute path of each prepared image, in sender order."""
    return [str(Path(image.path).resolve()) for image in req.images]


def _image_names(req: BrainRequest) -> list[str]:
    """Basenames only — a directive names paths, a notice never does."""
    return [
        image.display_name or Path(image.path).name for image in req.images
    ]


def build_image_prompt(req: BrainRequest) -> str:
    """`req.prompt` with the image section prepended, or unchanged.

    A request carrying no images is returned byte-identical, which is the whole
    shipped population of this brain's traffic.

    `allowed_tools=[]` is a policy decision by the caller (the sleep cycle, the
    health OCR paths), never a gap to fill: the tool set is not enabled
    implicitly. Those requests get a named omission instead, the same split
    `health/ocr.py` already settled with `allowed_tools=["Read"] if read_path`
    (where the same value also supplies `fs_read_roots`, so the grant and its
    confinement cannot be separated).
    """
    if not req.images:
        return req.prompt

    if not req.allowed_tools:
        listing = "\n".join(f"- {name}" for name in _image_names(req))
        section = f"{IMAGE_OMITTED_HEADER}\n\n{_IMAGE_OMITTED_BODY}\n\n{listing}"
    else:
        listing = "\n".join(f"- {path}" for path in _image_paths(req))
        section = f"{IMAGE_DIRECTIVE_HEADER}\n\n{_IMAGE_DIRECTIVE_BODY}\n\n{listing}"
    return f"{section}\n\n{req.prompt}"


def build_withdrawn_image_prompt(req: BrainRequest, images, reason: str) -> str:
    """The re-issue's prompt: the same request, with the images named as gone.

    Not a silent strip. A blind retry lets the model answer confidently without
    knowing it lost sight, which is the defect this whole change exists to
    prevent, reached by a different code path — the notice is exactly what
    removes that objection.
    """
    listing = "\n".join(
        f"- {image.display_name or Path(image.path).name}" for image in images
    )
    body = (
        "The provider rejected this request's image payload "
        f"({reason}), so it has been re-sent without the images below. "
        "Answer from the text and any OCR context, and do not state or imply "
        "that you have seen them."
    )
    return f"{IMAGE_WITHDRAWN_HEADER}\n\n{body}\n\n{listing}\n\n{req.prompt}"


# A 400 is the status the provider uses for every request-shaped complaint, so
# it only counts here when the diagnostic actually names an image. Size words
# alone are not enough and the size arm is 413's: `exceeds`, `too large` and
# `maximum` are the vocabulary of a context-length or `max_tokens` complaint
# too, and matching those buys a second paid full run plus a notice telling the
# user their images were the problem when they were not. 413 needs no such test
# — the request was too large and this request's images are the largest thing
# in it.
_IMAGE_REJECTION_RE = re.compile(r"image|media_?type|attachment", re.IGNORECASE)


def is_image_payload_rejection(text: str, has_images: bool) -> bool:
    """Whether `text` is the provider refusing this request's images.

    Deferring to the existing classification is what makes this necessary
    rather than defensive: `PERMANENT_STATUS_CODES` holds both 400 and 413,
    `is_permanent_api_error` maps them to `stop_reason="error"`, and `error` is
    in the never-fallback set — so an oversized image payload would kill an
    otherwise valid text task with no answer and no fallback attempt.
    """
    if not has_images or not text:
        return False
    parsed = parse_api_error(text)
    if not parsed:
        return False
    status = parsed.get("status_code")
    if status == 413:
        return True
    if status == 400:
        return bool(_IMAGE_REJECTION_RE.search(parsed.get("message") or ""))
    return False


def _rejection_reason(text: str) -> str:
    """A short, bounded quote of the provider's own diagnostic."""
    parsed = parse_api_error(text) or {}
    status = parsed.get("status_code") or "?"
    message = (parsed.get("message") or "").strip()
    if len(message) > 200:
        message = message[:199] + "…"
    return f"HTTP {status}: {message}" if message else f"HTTP {status}"


def _looks_like_cli_envelope(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    if any(key in obj for key in _CLI_ENVELOPE_KEYS):
        return True
    kind = obj.get("type")
    return isinstance(kind, str) and kind in _CLI_FRAME_TYPES


def _warn_if_cli_envelope(frames) -> None:
    """Warn when output that came from the CLI could not be read as an envelope.

    The silent fallback is how ISSUE-271 stayed invisible for three weeks: it
    reads as success at every layer above, so the answer is a JSON blob and the
    usage row is simply absent. One line here is what turns the next envelope
    change into a log entry on the first sleep cycle rather than a source dive
    three weeks later.
    """
    if not any(_looks_like_cli_envelope(frame) for frame in frames):
        return
    logger.warning(
        "claude_code: --output-format json returned CLI-shaped output with no "
        "terminal result frame; treating stdout as the answer and recording no "
        "usage. The CLI's envelope has probably changed again (ISSUE-271)."
    )


def _parse_simple_json_output(stdout: str):
    """Read `--output-format json` stdout into `(answer_text, BrainUsage | None)`.

    **Two shapes are real and both must be read** (ISSUE-271). The CLI's help
    has always described the flag as "json (single result)", but 2.1.227 emits
    a JSON *array* of the same frames the streaming path produces, while
    2.1.238 emits the bare terminal `result` frame as one object. Accepting
    only the array meant every daemon-side model call on a newer CLI got the
    JSON envelope back as its answer and recorded no usage row — seven origins,
    none of them `task`, including the code reviewer (whose findings parser
    then reported `malformed_output` for every diff).

    Returns `(None, None)` for anything that is neither shape, and that
    fallback is load-bearing rather than defensive: roughly ninety tests across
    six files patch `subprocess.run` with plain-text stdout, and a deployment
    running a CLI that ignores the flag behaves the same way. A `(None, None)`
    answer means the caller treats stdout as the answer exactly as it did
    before.

    A bare object only counts as the terminal frame when its `type` is
    `result`. Several daemon callers ask the model for a JSON answer, so a
    `{`-leading stdout is not on its own evidence of an envelope.

    Non-streaming output carries no `message_delta` frames, so these runs get
    totals and NULL context columns. The single-object shape additionally
    carries no `system` init frame, which costs two fields sniffed off it, and
    `cost_basis` then lands on `unknown` — deliberately, since totals and cost
    come from `modelUsage`, which is present either way, and inferring the
    basis from config would be exactly the guess
    `cost_basis_from_api_key_source` refuses to make.

    `_build_command` now passes `--verbose`, which makes both deployed CLI
    versions emit the array shape with the init frame, so the single-object
    branch should no longer be reached in production. It stays because it costs
    nothing and the flag's effect is the CLI's behaviour rather than ours: a
    version that ignores `--verbose` degrades to an honest `unknown` instead of
    losing the answer.
    `model_hint` is likewise empty, so `usage.model` falls to whichever model
    `modelUsage` says carried the cost; a costed frame with no `modelUsage`
    children lands model-less rather than mislabelled. A blank `model` on one
    of these rows is that, not corruption.
    """
    if not stdout:
        return None, None
    head = stdout.lstrip()[:1]
    if head not in ("[", "{"):
        return None, None
    try:
        decoded = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, None

    if isinstance(decoded, list):
        frames = decoded
    elif isinstance(decoded, dict):
        if decoded.get("type") == "result":
            frames = [decoded]
        else:
            # The array→object move is exactly what ISSUE-271 was, so an
            # unreadable *object* is the shape most likely to regress next.
            # Warning only from the list branch below would leave that one as
            # silent as the original.
            _warn_if_cli_envelope([decoded])
            return None, None
    else:
        return None, None

    result_frame = None
    model_seen = ""
    api_key_source = None
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        if kind == "result":
            result_frame = frame
        elif kind == "system":
            if not model_seen and frame.get("model"):
                model_seen = str(frame["model"])
            if api_key_source is None and frame.get("apiKeySource"):
                api_key_source = str(frame["apiKeySource"])
    if result_frame is None:
        _warn_if_cli_envelope(frames)
        return None, None

    usage = usage_types.from_cli_result(
        result_frame, [], api_key_source, model_hint=model_seen,
    )
    answer = result_frame.get("result")
    error_text = result_frame.get("error")
    if not (isinstance(answer, str) and answer):
        # An `error_during_execution`-shaped frame carries its text in `error`,
        # with `result` either absent *or present and empty* — the empty case is
        # why this tests truthiness rather than type. Leaving the answer blank
        # would make the caller's `if returncode == 0 and output:` guard skip
        # the classifiers entirely, so a provider failure that used to be read
        # off stdout and classified would come back as "produced no output"
        # with a generic `error`, which is in neither the fallback trigger set
        # nor the breaker's cooldown set.
        if isinstance(error_text, str) and error_text:
            answer = error_text
    if not isinstance(answer, str):
        # Nothing textual in the frame at all. `None` tells the caller to keep
        # raw stdout, so the classifiers still have something to read. A frame
        # whose `result` is genuinely the empty string keeps it: that is a
        # degenerate answer, not a provider failure, and handing the caller the
        # raw envelope instead would put JSON where the answer goes.
        return None, usage
    return answer, usage


def parse_api_error(text: str) -> dict | None:
    """Parse API error string into structured data.

    Returns dict with status_code, message, request_id on match, or None.
    Prefers the JSON-bodied form; falls back to the bodyless
    ``API Error: NNN <text>`` shape the CLI also emits.
    """
    if not text:
        return None
    match = API_ERROR_PATTERN.search(text)
    if match:
        status_code = int(match.group(1))
        try:
            payload = json.loads(match.group(2))
            return {
                "status_code": status_code,
                "message": payload.get("error", {}).get("message", "Unknown error"),
                "request_id": payload.get("request_id"),
            }
        except json.JSONDecodeError:
            return {
                "status_code": status_code,
                "message": "Unknown error",
                "request_id": None,
            }

    plain = _API_ERROR_PLAIN_PATTERN.search(text)
    if not plain:
        return None
    return {
        "status_code": int(plain.group(1)),
        "message": plain.group(2).strip() or "Unknown error",
        "request_id": None,
    }


def parse_retry_after(text: str) -> float | None:
    """The provider's requested wait in seconds, capped, or None.

    Capped at ``RETRY_AFTER_MAX_SECONDS`` and floored at 0 — a negative or
    absurd value is treated as absent rather than obeyed.
    """
    if not text:
        return None
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, RETRY_AFTER_MAX_SECONDS)


def _looks_like_api_error(text: str) -> bool:
    """Whether ``text`` carries a provider API-error signal at all."""
    if not text:
        return False
    return bool(
        parse_api_error(text)
        or _API_ERROR_MARKER.search(text)
        or _NET_ERRNO_RE.search(text)
    )


def is_transient_api_error(text: str) -> bool:
    """Check if the error text represents a transient API error worth retrying.

    Two signals: a capacity/gateway *status code* (429 + 5xx + 529), or a
    network-level failure. The network branch is gated on the ``API Error``
    marker (or an unambiguous errno) so ordinary prose mentioning a connection
    reset can't route a task onto the retry/fallback path — this predicate is
    also run against arbitrary tmux pane text.

    A 429 whose body signals *quota/subscription exhaustion* is NOT transient —
    ``is_usage_limit_error`` catches that case first at every call site, so a
    usage limit reroutes to the configured fallback brain instead of being
    retried against the same exhausted primary.
    """
    if not text:
        return False
    parsed = parse_api_error(text)
    if parsed:
        status = parsed["status_code"]
        if _status_is_transient(status):
            return True
        if status in PERMANENT_STATUS_CODES:
            # An explicit request-shaped status is authoritative: a 400 whose
            # body quotes "connection reset" is still a client error (NB-13a).
            return False
        # An unclassified status (a provider's own 4xx extension) falls through
        # to the text signals rather than being declared non-transient.
    if _NET_ERRNO_RE.search(text):
        return True
    return bool(
        _API_ERROR_MARKER.search(text) and _NETWORK_TRANSIENT_RE.search(text)
    )


def is_permanent_api_error(text: str) -> bool:
    """True when the error is request-shaped: no retry, no fallback attempt.

    400 / 401 / 403 / 404 / 413 and friends, plus context-length and
    content-filter refusals. The status code wins over the body text, so a 529
    whose message happens to quote a request-shaped phrase stays transient.
    """
    if not text:
        return False
    parsed = parse_api_error(text)
    if parsed:
        status = parsed["status_code"]
        if _status_is_transient(status):
            return False
        if status in PERMANENT_STATUS_CODES:
            return True
    # Text-only fall-through. Gated on the API-error marker for the same reason
    # the transient one is: "the model's context window is 200k tokens" is an
    # answer, not a failure. A network signal wins, so an unparseable
    # "API Error: connection reset while building the context window" doesn't
    # read as permanent on the strength of a phrase in its message.
    if not _looks_like_api_error(text) or _NETWORK_TRANSIENT_RE.search(text):
        return False
    return bool(_REQUEST_SHAPED_RE.search(text))


def api_error_stop_reason(text: str) -> str | None:
    """The ``stop_reason`` for a provider API error, or None if not one.

    The single classifier the execution paths use, so "is this worth a retry /
    a fallback attempt?" is answered the same way everywhere. Precedence:
    a usage limit is persistent and outranks its own 429 status; a
    request-shaped failure is permanent; capacity and network failures are
    transient; anything else that still looks like an API error is a plain
    error (unknown status → don't gamble a fallback call on it).
    """
    if not text:
        return None
    if is_usage_limit_error(text):
        return "usage_limit"
    if not _looks_like_api_error(text):
        return None
    if is_permanent_api_error(text):
        return "error"
    if is_transient_api_error(text):
        return "transient_api_error"
    return "error"


# Substrings (case-insensitive) that mark a subscription/quota/billing limit —
# a *persistent* "brain unavailable until the window resets" condition, distinct
# from a transient overload 429. Kept as a plain keyword set rather than tied to
# the ``API Error: NNN`` shape so the same detector works on ClaudeCodeBrain's
# CLI output, the tmux TUI transcript/pane text, and NativeBrain's arbitrary
# OpenAI-compatible error bodies. Best-effort and tunable against real output
# (see the spec's "Real usage-limit output samples" open question).
_USAGE_LIMIT_KEYWORDS: tuple[str, ...] = (
    "usage limit",
    "session limit",
    "limit reached",
    "quota",
    "insufficient_quota",
    "credit balance",
    "out of credit",
    "billing",
    "plan limit",
    "monthly limit",
    "usage cap",
    "spending limit",
)

# "...exceeded ... limit" where the two words are close together (an explicit
# limit-exceeded phrasing). Requires "exceeded" to precede "limit" so a plain
# "rate limit exceeded" (transient) does NOT match.
_EXCEEDED_LIMIT_RE = re.compile(r"exceeded[^.]{0,40}?\blimit\b", re.IGNORECASE)

# Claude Code's subscription-limit stem: "You've hit your <scope> limit · resets …".
# The scope varies — session / weekly / Opus / org's monthly spend (per the
# Claude Code error docs, https://code.claude.com/docs/en/errors, plus the live
# "You've hit your org's monthly spend limit · ask your admin to raise it"
# banner) — and all are a persistent "brain unavailable until reset" condition.
# Anchoring on "hit your … limit" catches every current phrasing (and a future
# scope word) without enumerating each noun, and the "hit your" anchor keeps a
# transient "rate limit" from matching.
_HIT_LIMIT_RE = re.compile(r"hit your[^.]{0,40}?\blimit\b", re.IGNORECASE)

# Standalone credit-exhaustion banner ("Credit balance is too low", per the docs).
_CREDIT_BALANCE_LOW_RE = re.compile(r"credit balance is too low", re.IGNORECASE)

# "<scope> limit reached" — the legacy/API banner phrasing ("usage limit
# reached · resets …"). "reached" (past-tense, adjacent to "limit") is a banner
# signal a normal answer rarely produces; the length gate in
# ``is_usage_limit_banner`` guards the residual risk.
_LIMIT_REACHED_RE = re.compile(r"\blimit reached\b", re.IGNORECASE)

# Claude Code's explicit "this is a server-side capacity throttle, NOT your quota"
# disclaimer ("API Error: Server is temporarily limiting requests (not your usage
# limit)", per the docs). It contains the substring "usage limit", so without this
# guard the broad keyword set below would misread a transient throttle as a
# persistent usage limit and needlessly trip the fallback breaker.
_NOT_USAGE_LIMIT_RE = re.compile(r"not your usage limit", re.IGNORECASE)

# A genuine usage-limit *banner* is a short standalone one-liner delivered as the
# whole result. A real answer that merely quotes a limit word (e.g. a memory
# extraction summarising a past "usage limit" incident) is longer and is not a
# banner; this ceiling is what keeps such content off the strict success-frame
# path (see ``is_usage_limit_banner``).
_BANNER_MAX_CHARS = 400


def is_usage_limit_error(text: str) -> bool:
    """True if ``text`` indicates a subscription/quota/billing usage limit.

    Shared by all three brains to classify a persistent "primary unavailable"
    condition as ``stop_reason="usage_limit"`` (which reroutes to the configured
    fallback brain) rather than a transient retry or a generic error.

    This is the **broad** detector, meant for genuine *error* bodies (native
    provider error JSON, ``claude`` stderr, a failure result). It matches the
    keyword set liberally, so it must NOT be run against a *successful* answer —
    use :func:`is_usage_limit_banner` there instead. A server-side throttle that
    explicitly says "(not your usage limit)" is excluded so a transient capacity
    error can't be mistaken for a quota outage.
    """
    if not text:
        return False
    if _NOT_USAGE_LIMIT_RE.search(text):
        return False
    low = text.lower()
    if any(keyword in low for keyword in _USAGE_LIMIT_KEYWORDS):
        return True
    return bool(_EXCEEDED_LIMIT_RE.search(text)) or bool(_HIT_LIMIT_RE.search(text))


def is_usage_limit_banner(text: str) -> bool:
    """True iff ``text`` *is* a standalone Claude Code usage-limit / credit banner.

    Stricter than :func:`is_usage_limit_error`, for the paths where ``claude``
    reports a subscription limit as a **successful** result frame (rc 0, the
    limit banner as the whole answer). It must not fire on a genuine answer that
    merely mentions a limit word — e.g. a nightly memory-extraction summarising a
    conversation *about* a past usage limit, whose successful output otherwise
    re-classified as ``usage_limit`` and kept the availability breaker armed long
    after the real limit cleared (the observed feedback loop).

    Robustness comes from matching only the *precise published banner shapes*
    (`https://code.claude.com/docs/en/errors`) — "You've hit your <scope> limit",
    "Credit balance is too low", or an explicit "exceeded … limit" — and only
    when the text is short enough to be a standalone banner rather than a real
    answer. The broad keyword set is deliberately not consulted here.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _BANNER_MAX_CHARS:
        return False
    if _NOT_USAGE_LIMIT_RE.search(stripped):
        return False
    return bool(
        _HIT_LIMIT_RE.search(stripped)
        or _CREDIT_BALANCE_LOW_RE.search(stripped)
        or _LIMIT_REACHED_RE.search(stripped)
        or _EXCEEDED_LIMIT_RE.search(stripped)
    )


# A bare provider error delivered as the whole answer opens with the marker;
# a real answer that *discusses* one has it somewhere in the middle. Anchored at
# the start (past up to 8 chars of decoration — an emoji, a bullet, a quote
# marker) and length-gated, mirroring ``is_usage_limit_banner``.
_API_ERROR_BANNER_RE = re.compile(
    r"^[\s\W]{0,8}API Error\b[\s:(\[,-]*"       # marker + whatever separates it
    r"(?:(?P<status>\d{3})\b[ \t]*)?"           # optional status code
    r"(?P<tail>[^\n]*)",
    re.IGNORECASE,
)


def is_api_error_banner(text: str) -> bool:
    """True iff ``text`` *is* a bare provider API-error banner.

    For the paths where ``claude`` reports an API failure as a **successful**
    result frame (rc 0, the error text as the whole answer) — the mechanism that
    put a raw ``API Error: 529 Overloaded`` in front of the user as the final
    reply (ISSUE-212), and the same one already handled for usage limits by
    :func:`is_usage_limit_banner`.

    Strict on purpose: it must not fire on a genuine answer that *mentions* an
    API error, because the callers act on it destructively — the brain reroutes
    to a (paid) fallback, and ``scheduler``'s masquerading-success guard fails
    the task outright.

    Three gates. **Anchored** at the start, so an answer discussing an error
    mid-sentence is out. **Length**-gated, so a long answer that merely opens
    with the phrase is out. And the token after the marker (or after the status
    code) must be **JSON or Title-cased** — every real banner is a reason
    phrase (``529 Overloaded``, ``500 Internal Server Error``, ``Connection
    error.``) or a JSON body, whereas prose continues in lowercase
    (``API Error: 529 means the provider is overloaded``). That last gate is
    what separates the banner from a sentence that legitimately starts with it.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _BANNER_MAX_CHARS:
        return False
    match = _API_ERROR_BANNER_RE.match(stripped)
    if not match:
        return False
    if not match.group("status") and not _looks_like_api_error(stripped):
        return False
    tail = match.group("tail").strip()
    if not tail:
        # "API Error: 529" — a status and nothing else is unambiguous. Without a
        # status it is a bare "API Error" with no content, which is not one.
        return bool(match.group("status"))
    return tail[0].isupper() or tail[0] in "{[\"'"


def _interruptible_sleep(seconds: float, req: BrainRequest) -> bool:
    """Sleep in slices, polling ``req.cancel_check`` between them.

    A provider-supplied Retry-After can be far longer than the old fixed 5s, and
    `time.sleep` is not cancellable — a `!stop` issued during the backoff would
    otherwise sit unanswered for the whole wait. Returns True if the caller
    should stop (cancellation requested).
    """
    # Counts the slices down rather than watching a deadline: the loop must
    # terminate on its own arithmetic, not on wall-clock progress it can't
    # observe (and a patched time.sleep would otherwise spin forever).
    remaining = max(0.0, seconds)
    while remaining > 0:
        if req.cancel_check is not None:
            try:
                if req.cancel_check():
                    return True
            except Exception:
                logger.debug("cancel_check raised during retry backoff", exc_info=True)
        slice_seconds = min(_RETRY_SLEEP_SLICE_SECONDS, remaining)
        time.sleep(slice_seconds)
        remaining -= slice_seconds
    return False


def _is_retryable(result: "BrainResult") -> bool:
    """Whether a failed attempt should be retried against the same primary.

    Reads the ``stop_reason`` first (the ``_execute_*_once`` paths already
    classified the text) and falls back to re-classifying the text for any path
    that returned a bare ``error``.

    ``work_committed`` vetoes the retry outright: a run that reached the model
    and then reported a provider error may already have executed tools, so
    re-invoking the same prompt would repeat those side effects. Such a failure
    is reroute-only — the executor sends it to the fallback brain.
    """
    if result.work_committed:
        return False
    if result.stop_reason == "transient_api_error":
        return True
    if result.stop_reason in ("usage_limit", "cancelled", "timeout", "oom", "not_found"):
        return False
    return is_transient_api_error(result.result_text)


def _success_frame_stop_reason(text: str) -> str | None:
    """The stop_reason when a *successful* CLI result actually carries a
    provider failure banner, or None when it is a genuine answer.

    ``claude -p`` reports both a subscription limit and a provider API error as
    a success (rc 0 / ``subtype:"success"``) with the banner as the whole
    answer. Left alone, both are delivered to the user verbatim as the final
    reply and neither can ever reach the fallback brain.
    """
    if is_usage_limit_banner(text):
        return "usage_limit"
    if is_api_error_banner(text):
        return api_error_stop_reason(text) or "error"
    return None


def _failure_stop_reason(text: str) -> str:
    """Classify a failure's text into ``usage_limit`` / ``transient_api_error``
    / ``error``.

    Used at ClaudeCodeBrain's error-return points so a usage-limit body carries
    the distinct stop_reason before the generic ``error`` path swallows it, and
    so a capacity error is visibly transient rather than an anonymous failure
    the fallback trigger set can't match.
    """
    return api_error_stop_reason(text) or "error"


_OOM_TEXT = "Claude Code was killed (likely out of memory)"
_TERMINATED_PREFIX = "Claude Code was terminated by "


def is_signal_termination(text: str) -> bool:
    """True when a failure text is the brain's signal-death message.

    The executor drops ``stop_reason`` at its return boundary, so the scheduler
    classifies failures by their text (the same way it recognizes OOM and
    cancellation). This keeps the marker string in one place.
    """
    return text.startswith(_TERMINATED_PREFIX)


def _signal_result(returncode: int | None, execution_trace: str | None) -> BrainResult | None:
    """Classify a process killed by a signal. Returns None if it wasn't.

    A negative returncode means the subprocess died on signal ``-returncode``.
    Only SIGKILL used to be recognized (the OOM killer's and systemd-oomd's
    signature); every other signal fell through to the generic stream-parse
    catch-all and was reported as "Stream parsing failed (rc=-15, N lines)" — a
    symptom, not a cause. SIGTERM in particular is what ``systemctl restart``
    delivers to the whole cgroup under systemd's default KillMode, so it is a
    routine event that deserves a name (ISSUE-191).
    """
    if returncode is None or returncode >= 0:
        return None
    signum = -returncode
    if signum == signal.SIGKILL:
        return BrainResult(
            success=False,
            result_text=_OOM_TEXT,
            execution_trace=execution_trace,
            stop_reason="oom",
        )
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = "signal"
    return BrainResult(
        success=False,
        result_text=f"{_TERMINATED_PREFIX}{name} (signal {signum})",
        execution_trace=execution_trace,
        stop_reason="terminated",
    )


def _is_root() -> bool:
    """True when the process runs as uid 0 (Unix). `claude` refuses
    --dangerously-skip-permissions as root unless IS_SANDBOX=1 is set. Shared by
    both the headless and tmux launch paths."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


# Flags already warned-about as unsupported, so the "dropped a flag" WARNING
# fires once per flag per process rather than every task. Module-global on
# purpose (the warning is operator-facing, not per-request).
_WARNED_UNSUPPORTED_FLAGS: set[str] = set()


def advisor_active(
    req: BrainRequest, *, unsupported: frozenset[str] = frozenset()
) -> bool:
    """True iff ``--advisor`` will actually reach the model for this request:
    an advisor is set, there are tools for it to have judgement moments about
    (a text-only call has none — see ``build_claude_cli_flags``), and
    ``--advisor`` itself isn't in ``unsupported`` (the tmux brain's
    target-CLI-surface dropped-flag set — pass ``_TMUX_UNSUPPORTED_FLAGS``
    there so a future "the interactive TUI accepts but ignores --advisor"
    finding doesn't reopen the settings-file channel it was meant to close).

    ``build_claude_cli_flags`` itself calls this with the default empty
    ``unsupported`` — it always attempts ``_add("--advisor", ...)`` like every
    other flag and lets ``_add``'s own ``unsupported`` check drop it (with the
    same operator-facing WARNING every other dropped flag gets). The
    ``unsupported``-aware form is for the env-suppression decision in
    ``ClaudeCodeBrain.execute`` / ``TmuxClaudeBrain``'s session env, which sits
    outside flag-building and has no ``_add`` to fall back on: it must decide
    for itself whether the flag will land, so the two stay structurally
    exclusive — either the flag fires, or the settings-file channel is
    suppressed, never both, never neither (advisor-model spec, "Env
    exclusivity" test).
    """
    return (
        bool(req.advisor)
        and bool(req.allowed_tools)
        and "--advisor" not in unsupported
    )


def build_claude_cli_flags(
    req: BrainRequest, *, unsupported: frozenset[str] = frozenset()
) -> list[str]:
    """Build the `claude` CLI flags shared by both the headless (`-p`) and the
    interactive (tmux) launch paths.

    Covers the model / effort / tool / system-prompt flags both brains need; it
    deliberately does NOT add ``-p -`` or the ``--output-format stream-json``
    flags (headless-only) nor ``--dangerously-skip-permissions`` (which both
    brains append themselves) — each brain appends its own path-specific flags
    around this common core.

    ``unsupported`` names flags the *target* CLI surface rejects (the interactive
    TUI may not accept every ``-p`` flag; Stage 1 of the tmux production spec
    verifies which). A flag in this set is dropped from the argv and warned about
    once per process rather than passed through to a launch failure. The default
    (empty set) reproduces the headless argv exactly, so ``ClaudeCodeBrain``'s
    output is byte-for-byte unchanged.
    """
    flags: list[str] = []
    # Empty allowed_tools means text-only invocation (e.g. sleep cycle): skip the
    # tool flags entirely so claude's defaults stay out of the equation. The
    # prompt itself, plus the absence of --dangerously-skip-permissions, is what
    # keeps the call text-only.
    if req.allowed_tools:
        # Both brains run non-interactively with --dangerously-skip-permissions
        # (added per-brain), so the model gets its full default toolset and an
        # --allowedTools allowlist would only restrict it below that, blocking
        # tools we didn't think to enumerate. The bwrap sandbox + network proxy
        # are the security boundary, not an interactive permission prompt; Bash
        # is permitted anyway, which is effectively unrestricted inside the
        # sandbox. So we drop --allowedTools and rely on skip-permissions.
        #
        # We DO still explicitly deny the harness's built-in multi-agent
        # orchestration tools (Agent + Workflow): deny rules win even under
        # --dangerously-skip-permissions, so this keeps Istota orchestrating
        # through its own skills / subtasks rather than Claude Code's fan-out,
        # whose dozens-of-subagents cost profile we don't want a task reaching
        # for unprompted.
        #
        # Workflow had briefly been dropped from this list (ISSUE-110 follow-up)
        # because the old --allowedTools allowlist already excluded it — the only
        # reason to name it then was to suppress a harness auto-inject reminder
        # that stopped firing in 2.1.162. Now that the allowlist is gone (we run
        # with --dangerously-skip-permissions), the allowlist no longer
        # implicitly blocks Workflow, so it must be denied explicitly again.
        flags += ["--disallowedTools", "Agent", "Workflow"]

    def _add(flag: str, *values: str) -> None:
        if flag in unsupported:
            if flag not in _WARNED_UNSUPPORTED_FLAGS:
                _WARNED_UNSUPPORTED_FLAGS.add(flag)
                logger.warning("tmux_brain unsupported_flag flag=%s (dropped)", flag)
            return
        flags.extend([flag, *values])

    if req.model:
        _add("--model", req.model)
    if req.effort:
        _add("--effort", req.effort)
    # Unconditional call, like --model/--effort above: _add itself drops an
    # unsupported flag (and warns once) rather than advisor_active silently
    # pre-filtering it, so the operator-facing "dropped a flag" WARNING still
    # fires for --advisor exactly like every other flag.
    if advisor_active(req):
        _add("--advisor", req.advisor)
    if req.custom_system_prompt_path and req.custom_system_prompt_path.exists():
        _add("--system-prompt-file", str(req.custom_system_prompt_path))
    # Istota's composed standing instructions (`BrainRequest`'s three-channel
    # note). *Append*, never replace: with no operator file configured — the
    # default deployment — `--system-prompt-file` here would discard Claude
    # Code's own default harness prompt. The two flags are independent on the
    # pinned CLI, which enforces only `--system-prompt` against
    # `--system-prompt-file` and `--append-system-prompt` against this one, so
    # an operator file and the composed file may both be passed.
    #
    # No `exists()` gate, unlike the optional operator file above. This is
    # required input: a path that no longer resolves must reach the CLI, which
    # answers `Error: Append system prompt file not found: <path>` and exits
    # without running. Dropping it instead would run the task with the user half
    # alone — no persona, no rules, no tool descriptions — which is ISSUE-375
    # reintroduced by a cleanup bug, and silently.
    if req.composed_system_prompt_path is not None:
        _add("--append-system-prompt-file", str(req.composed_system_prompt_path))
    return flags


# ---------------------------------------------------------------------------
# Anthropic model namespace
#
# These tables describe the models *this* brain can run. A future
# OpenRouter / Anthropic-direct brain ships its own analogous tables in
# its own module; consumers never reach in here directly — they go through
# Brain.resolve_alias / Brain.resolve_model_name.
#
# Versioning: bare aliases like ``opus`` always resolve to a *specific*
# version constant (``OPUS = "claude-opus-5"``) so a model release can't
# silently re-route us. A model release bumps the constant in one place and
# ripples through every alias + role that points at it. Prior versions are NOT
# enumerated as aliases — an operator who needs one types the canonical id with
# an optional ``:effort`` modifier (``claude-opus-4-7:high``), which resolves via
# the canonical passthrough below.
# ---------------------------------------------------------------------------

OPUS: str = "claude-opus-5"
SONNET: str = "claude-sonnet-5"
HAIKU: str = "claude-haiku-4-5"

# The unified alias registry for *this brain* — the code-shipped floor. Maps a
# base alias name → ``(model_id, default_effort)`` in the Anthropic namespace.
# Holds the portable tiers (CANONICAL_ROLES) AND the provider shortcuts together,
# base names only: effort is an orthogonal ``:effort`` modifier applied generically
# at resolution (``opus:high``), never baked into a name. Operators overlay this
# via ``[models.aliases]`` TOML; a model release edits one constant here.
# Every surface (``!model`` prefix, ``!models`` output, scheduled-job overrides)
# reads through this table via ``Brain.resolve_alias`` / ``.list_aliases``.
DEFAULT_ALIASES: dict[str, tuple[str | None, str | None]] = {
    # Portable tiers (CANONICAL_ROLES) — code-floor efforts stay None; every
    # brain must map every canonical role (enforced by the role-contract test),
    # so a portable intent survives the cross-provider fallback.
    "fast":    (HAIKU, None),
    "general": (SONNET, None),
    "smart":   (OPUS, None),
    # Provider shortcuts (pins) — base names, no effort variants.
    "opus":    (OPUS, None),
    "sonnet":  (SONNET, None),
    "haiku":   (HAIKU, None),
    # Explicit "no override — use the brain/config default model".
    "default": (None, None),
}
assert set(CANONICAL_ROLES) <= set(DEFAULT_ALIASES), (
    "ClaudeCodeBrain must map every canonical role tier"
)

# The non-tier subset of the registry — the provider shortcuts. An operator
# alias override whose NAME collides with one of these silently changes what
# ``!model opus`` resolves to (almost always a typo for a tier override), so
# ``validate_alias_override`` warns on it. Overriding a tier is the normal case
# and never warns.
_SHORTCUT_NAMES: frozenset[str] = frozenset(set(DEFAULT_ALIASES) - set(CANONICAL_ROLES))


def _looks_canonical(name: str) -> bool:
    """Whether ``name`` is a raw Anthropic model id (passthrough target)."""
    return name.startswith("claude-")


def _resolve_target_with_effort(target: str) -> tuple[str, str | None]:
    """Translate an override RHS through ``DEFAULT_ALIASES`` to ``(model_id, effort)``.

    Splits an optional ``:effort`` modifier first, then resolves the base name.
    Operator wrote e.g. ``smart = "opus:high"`` → ``("claude-opus-5", "high")``
    (the modifier's effort wins over the alias's default). A bare shortcut
    ``smart = "opus"`` → ``("claude-opus-5", None)``. An unknown / canonical
    base passes through unchanged (raw ids like ``claude-opus-4-7`` work as
    targets), carrying only the modifier effort.
    """
    if not target:
        return target, None
    base, suffix_effort = split_effort(target)
    pair = DEFAULT_ALIASES.get(base.lower())
    if pair is not None and pair[0] is not None:
        return pair[0], (suffix_effort or pair[1])
    return base, suffix_effort


class ClaudeCodeBrain:
    """Brain that delegates to the `claude` CLI as a subprocess."""

    # The headless `claude -p` subprocess reads its whole prompt on stdin, then
    # stdin closes — there is no open channel to the running model, so mid-flight
    # steering is impossible by construction (see the !steer spec).
    supports_steering = False

    # This brain speaks the Anthropic model namespace. Operators key an
    # ``[models.aliases.<name>]`` sub-table on this string; TmuxClaudeBrain shares
    # it (same `claude` binary), so an ``anthropic`` value covers both.
    model_namespace = "anthropic"

    def __init__(self, config=None) -> None:
        """``config`` is the ``[brain.claude_code]`` block, or None for none.

        Optional because this class is constructed bare in several places, and
        None is honest for each: they have no deployment default to apply.
        Most only resolve model *names* — the web brain catalogue and the two
        alias validators in ``config.py``. ``TmuxClaudeBrain`` composes one and
        hands it the ``[brain.tmux]`` block instead, which works because the
        only fields read off a config here are ``model`` and ``effort`` and that
        block declares both.

        One bare-constructing caller does *execute*: ``context._claude_cli_triage``
        builds one for conversation triage. It is unaffected because that request
        always sets ``model`` itself (from ``conversation.selection_model``), so
        ``with_defaults`` takes neither branch — a fact about the caller rather
        than about the construction, which is why it is written down here.
        """
        self._config = config

    @property
    def default_model(self) -> str:
        """This brain's configured default, unresolved (alias or canonical id).

        Empty means the CLI's own default, which is what an omitted ``--model``
        gets. Read by the surfaces that display a deployment's effective default
        rather than by ``execute``, which goes through ``_with_defaults``.
        """
        return (getattr(self._config, "model", "") or "").strip()

    @property
    def default_effort(self) -> str:
        return (getattr(self._config, "effort", "") or "").strip()

    def with_defaults(self, req: BrainRequest) -> BrainRequest:
        """Fill an unpinned request from this brain's own configured default.

        The ``or`` that `NativeBrain` has always had, and that the executor used
        to pre-empt by substituting the deployment-wide `config.model` into
        every request (ISSUE-418).

        **Public, and the executor calls it before `execute`.** It is also
        applied inside `execute`, which is idempotent — a request whose model is
        already set takes neither branch — and the two callers answer different
        needs. `execute` covers the direct brain callers, which never go through
        the executor. The executor's call is what makes `req.model` and
        `req.effort` the *effective* values at every site downstream of the
        request rather than only inside the brain: `_persist_task_usage` writes
        both to `task_usage`, and `execute_task` reads `req.model` as the
        `model_used` fallback. Defaulting on a `dataclasses.replace` copy the
        executor never saw left those columns holding the empty string for every
        unpinned task, where they used to hold the deployment default.

        The configured name goes through ``resolve_alias`` rather than being
        passed to the CLI verbatim: ``model = "opus"`` in the block must mean
        what ``!model opus`` means, and an alias entry can carry an effort of its
        own. A pinned model with no effort deliberately takes no effort from
        here — the same rule ``executor._resolve_effort`` applies for the same
        reason, that an effort chosen for one model need not be valid on
        another.

        **Effort precedence is request > this block's own key > the alias's.**
        The block's explicit ``effort`` outranks an effort carried by the alias
        its ``model`` resolves through, because the operator wrote it beside
        that model and the alias's is a default for the alias rather than for
        this deployment. Getting this backwards made the block's key
        *unreachable* whenever ``model`` named an effort-carrying alias, which
        also broke the migration's one promise: the old path resolved the model
        with ``resolve_model_name``, which strips the modifier, and took
        ``config.effort`` verbatim — so ``model = "smart"`` + ``effort = "low"``
        ran at ``low`` before and would have run at the alias's ``high`` after.
        """
        model, effort = req.model, req.effort
        if not req.model:
            alias_effort = ""
            if self.default_model:
                resolved = self.resolve_alias(self.default_model)
                if resolved is not None and resolved[0]:
                    model = resolved[0]
                    alias_effort = resolved[1] or ""
                else:
                    model = self.resolve_model_name(self.default_model)
            effort = effort or self.default_effort or alias_effort
        if model == req.model and effort == req.effort:
            return req
        return dataclasses.replace(req, model=model, effort=effort)

    # --- Model resolution (Brain Protocol) ---------------------------------

    def resolve_alias(
        self, alias: str
    ) -> tuple[str | None, str | None] | None:
        """Resolve a `!model <alias>` (with optional ``:effort``) to (model_id, effort).

        Splits a ``:effort`` modifier first, then the base name resolves:
        operator override > ``DEFAULT_ALIASES`` (tiers + shortcuts) > canonical
        id passthrough (``claude-*``) > None (unknown). Effort precedence: the
        ``:effort`` suffix wins over the entry's own default effort. A role
        override's target is itself resolved through this brain's alias table
        (``smart = "opus"`` → ``claude-opus-5``), and an explicit
        ``RoleTarget.effort`` wins over the target's alias-derived effort.
        """
        if not alias:
            return None
        base, suffix_effort = split_effort(alias)
        base_lower = base.lower()
        # 1. Operator-overridden alias (per-namespace, effort-carrying)
        rt = get_alias_override_target(base_lower, self.model_namespace)
        if rt is not None:
            model_id, target_effort = _resolve_target_with_effort(rt.model)
            return (model_id, suffix_effort or rt.effort or target_effort)
        # 2. Shipped default (tier or shortcut)
        pair = DEFAULT_ALIASES.get(base_lower)
        if pair is not None:
            model_id, default_effort = pair
            return (model_id, suffix_effort or default_effort)
        # 3. Canonical id passthrough (carry the modifier effort)
        if _looks_canonical(base):
            return (base, suffix_effort)
        # 4. Unknown
        return None

    def resolve_model_name(self, name: str | None) -> str:
        """Resolve any name to a canonical Anthropic model ID.

        Empty/None → ``""`` (caller falls back to brain default).
        Unknown → pass-through with any ``:effort`` stripped (raw IDs typed into
        config still work; the effort never leaks into the model id).
        """
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0] is not None:
            return resolved[0]
        return split_effort(name)[0]

    def validate_alias_override(self, name: str, target: str) -> list[str]:
        """Surface operator typos at load time.

        Two checks:
        1. Alias name collides with a provider shortcut (e.g.
           ``[models.aliases] opus = "haiku"`` silently makes ``!model opus``
           resolve to Haiku — usually a typo for a tier override). Overriding a
           tier is the normal case and never warns.
        2. Override target is neither a known alias nor a canonical ``claude-*``
           id (it'd pass through to the CLI and fail at task time). A ``:effort``
           modifier on the target is stripped before the check.
        """
        warnings: list[str] = []
        name_lower = name.lower()
        if name_lower in _SHORTCUT_NAMES:
            warnings.append(
                f"alias override {name!r} shadows the built-in provider shortcut "
                f"of the same name; future `!model {name}` calls will resolve to "
                f"{target!r} instead of the shipped default"
            )
        if target:
            base, _effort = split_effort(target)
            pair = DEFAULT_ALIASES.get(base.lower())
            if pair is not None and pair[0] is None:
                # A known alias that pins no model (the reserved ``default``).
                # Used as a target it'd be sent to the CLI as the literal string
                # "default" rather than resolving to a real model.
                warnings.append(
                    f"alias override {name!r} target {target!r} resolves to no "
                    f"model (it is the reserved 'use the brain default' alias); "
                    f"tasks using this alias would send it verbatim — pin a "
                    f"concrete model id or tier instead"
                )
            elif pair is None and not _looks_canonical(base):
                warnings.append(
                    f"alias override {name!r} target {target!r} is neither a "
                    f"canonical model id nor a known alias; tasks using this "
                    f"alias will fail at execution time"
                )
        return warnings

    def list_aliases(self) -> list[tuple[str, str | None, str | None]]:
        """Merged alias table for display — base names + resolved default effort.

        Tiers sorted first, then the shipped shortcuts in declaration order, then
        any custom operator aliases (sorted). Operator overrides are reflected
        (resolved in this brain's own namespace, effort preserved). Used by
        ``!models`` and the composer autocomplete.
        """
        resolved: dict[str, tuple[str | None, str | None]] = dict(DEFAULT_ALIASES)
        for name in get_alias_overrides():
            rt = get_alias_override_target(name, self.model_namespace)
            if rt is not None:
                model_id, target_effort = _resolve_target_with_effort(rt.model)
                resolved[name] = (model_id, rt.effort or target_effort)
        tiers = sorted(n for n in resolved if n in CANONICAL_ROLES)
        shortcuts = [n for n in DEFAULT_ALIASES if n not in CANONICAL_ROLES]
        extras = sorted(
            n for n in resolved if n not in DEFAULT_ALIASES and n not in CANONICAL_ROLES
        )
        out: list[tuple[str, str | None, str | None]] = []
        for name in tiers + shortcuts + extras:
            model, effort = resolved[name]
            out.append((name, model, effort))
        return out

    # --- Execution (Brain Protocol) ----------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        # Stamped on the way out rather than at each of the ~30 return sites
        # below, so a return added later cannot forget it. `brain_kind` is what
        # the usage row records as the brain that ran, and it has to be right on
        # the fallback path, where the executor's own variable no longer
        # describes the result it is holding.
        #
        # The default is applied here, above `_execute`, for the same reason:
        # `_execute` re-issues once on an image rejection and every argv build
        # below reads `req.model`, so filling it at the single entry point is
        # what keeps the re-issue on the same model as the first attempt.
        req = self.with_defaults(req)
        result = self._execute(req)
        result.brain_kind = BRAIN_KIND
        # What this attempt actually ran with, for `task_usage.effort`. Stamped
        # here because `with_defaults` fills the default onto a *copy*, so the
        # executor's own `req` no longer describes the attempt (ISSUE-418) —
        # the same reason `model_used` exists. `if not` rather than an
        # unconditional write, so an inner path that knows better keeps it.
        if not result.effort_used:
            result.effort_used = req.effort
        return result

    def _execute(self, req: BrainRequest) -> BrainResult:
        """One attempt, plus the one re-issue an image rejection is allowed.

        The image section is applied here rather than inside either transport
        path, so the streaming and non-streaming halves cannot disagree about
        what the model was told, and so the re-issue below is a plain second
        call with a different request rather than a special case threaded
        through two retry loops.
        """
        import dataclasses as _dc

        attempt = (
            _dc.replace(req, prompt=build_image_prompt(req)) if req.images else req
        )
        started = time.monotonic()
        result = self._execute_attempt(attempt)

        if result.success or not is_image_payload_rejection(
            result.result_text, bool(req.images)
        ):
            # `success` first, and not merely belt-and-braces: an answer that
            # *quotes* a provider error — summarising an incident, explaining a
            # log line — would otherwise cost the user a second paid call and
            # replace their answer with one written without the images.
            return result

        reason = _rejection_reason(result.result_text)

        # The same veto `_is_retryable` applies, for the same reason and using
        # the same flag: a run that reached the model may already have sent an
        # email or pushed a commit, and re-invoking the identical prompt repeats
        # those side effects. A 413 on the *first* API call — the case this
        # branch is for — never reaches the model, so it arrives as an ordinary
        # failure with the flag clear; a 413 later in a run, caused by the
        # accumulated context the images are still in, arrives with it set and
        # is a reroute, not a re-issue.
        if result.work_committed:
            logger.warning(
                "claude_code: image payload rejected (%s) after the run had "
                "already committed work; not re-issuing", reason,
            )
            return result

        logger.warning(
            "claude_code: provider rejected the image payload (%s); "
            "re-issuing once without %d image(s)",
            reason, len(req.images),
        )
        # The first attempt may have written the result file before the
        # provider refused it. Both read paths are guarded on `.exists()`
        # alone, and the executor unlinks it only once, before the run — so
        # without this the re-issue can deliver text the *images* produced,
        # under a prompt saying they were withdrawn.
        if req.result_file is not None:
            try:
                req.result_file.unlink(missing_ok=True)
            except OSError:
                logger.debug("could not clear the result file before re-issue")

        # `timeout_seconds` is a per-subprocess bound and `execute_task` runs on
        # a worker-pool thread, so two full attempts under the same value would
        # let one task hold a worker for twice its configured budget. The
        # re-issue gets what is left, floored so it is never handed a
        # non-positive timeout.
        remaining = max(
            _MIN_REISSUE_SECONDS, req.timeout_seconds - (time.monotonic() - started)
        )

        # Once. If the re-issue is rejected too, the existing classification
        # decides retry or fallback on its own result, and the provider's
        # diagnostic reaches the user with it.
        return self._execute_attempt(
            _dc.replace(
                req,
                images=[],
                prompt=build_withdrawn_image_prompt(req, req.images, reason),
                timeout_seconds=remaining,
            )
        )

    def _execute_attempt(self, req: BrainRequest) -> BrainResult:
        try:
            # --dangerously-skip-permissions (added by _build_command for
            # tool-bearing tasks) is refused under root/sudo unless IS_SANDBOX=1
            # signals an external isolation boundary. That's the Docker
            # container-as-sandbox case (bwrap off, runs as root); on the
            # non-root prod VM service user the flag is allowed without it, so we
            # leave it unset. Mirrors the tmux brain's root handling.
            if req.allowed_tools and _is_root() and "IS_SANDBOX" not in req.env:
                req.env["IS_SANDBOX"] = "1"

            # Close the settings-file advisor channel whenever this request
            # won't emit --advisor itself: a host's ~/.claude/settings.json
            # advisorModel is honored in -p mode (RO-bound into the sandbox),
            # so without this, any host carrying that key would turn on an
            # advisor Istota's config never asked for. advisor_active(req) is
            # the same predicate build_claude_cli_flags uses for --advisor, so
            # exactly one of the two is ever true (advisor-model spec) — the
            # positive branch pops rather than leaves alone, since req.env
            # isn't guaranteed clean: a passthrough env var or an inherited
            # dict (e.g. a fallback req built via dataclasses.replace) could
            # already carry the disable var even when this request wants an
            # advisor, which would silently kill the flag despite it being set.
            if advisor_active(req):
                req.env.pop("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", None)
            else:
                req.env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"

            cmd = self._build_command(req)
            if req.sandbox_wrap is not None:
                cmd = req.sandbox_wrap(cmd)

            if req.streaming:
                return self._execute_streaming(cmd, req)
            return self._execute_simple(cmd, req)
        except FileNotFoundError:
            return BrainResult(
                success=False,
                result_text="Claude Code CLI not found. Is it installed and in PATH?",
                stop_reason="not_found",
            )
        except Exception as e:
            logger.exception("ClaudeCodeBrain.execute raised")
            return BrainResult(
                success=False,
                result_text=f"Execution error: {e}",
                stop_reason="error",
            )

    @staticmethod
    def _build_command(req: BrainRequest) -> list[str]:
        cmd = ["claude", "-p", "-"] + build_claude_cli_flags(req)
        if req.allowed_tools:
            # Run non-interactively without per-tool permission prompts (which
            # can't be answered in -p mode and would otherwise auto-deny tools).
            # The sandbox + network proxy are the boundary; an allowlist buys
            # nothing here. Skipped for text-only invocations (no tools), so
            # those stay tool-less. Mirrors the tmux brain.
            cmd += ["--dangerously-skip-permissions"]
        if req.streaming:
            # --include-partial-messages emits content deltas as they arrive so
            # the final answer streams token-by-token on stream surfaces instead
            # of landing as one whole block. Without it the CLI only emits
            # complete ``assistant`` messages, so the answer would dump all at
            # once (the whole-block TextEvent). Parsed in brain._events.
            cmd += [
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
            ]
        else:
            # The non-streaming path is where the daemon's own model calls run
            # — the nightly sleep cycle, shared briefing blocks, three health OCR
            # paths plus the biomarker explainer, and
            # conversation-context triage — none
            # of which has a task row. The code reviewer was in that list and
            # moved to the streaming path (ISSUE-448), because this one loses a
            # timed-out call's accounting entirely: `accounting["usage"]` is
            # assigned only after the process exits and its output parses. That
            # is a reason to be on the other path rather than a hole here — the
            # rest of these are short calls that do not time out — but it does
            # mean this path is now the *less* measured of the two.
            # Without a structured format they were the
            # largest unmeasured spend in the deployment. Triage is the odd one
            # out on frequency: it runs once per conversational task with older
            # history, where the rest are occasional (ISSUE-272). What `json` emits is CLI-version-dependent: 2.1.227
            # gives an array of the same frames the streaming path produces,
            # 2.1.238 gives the bare terminal `result` object.
            # `_parse_simple_json_output` reads either. There are no
            # `message_delta` frames on this path, so these runs carry totals
            # and NULL context.
            #
            # `--verbose` is what pins that version-dependence down: with it,
            # both 2.1.227 and 2.1.239 emit the frame array including the
            # `system`/`init` frame. Without it the newer CLI drops that frame,
            # and with it goes `apiKeySource` — so every call on this path
            # recorded `cost_basis = "unknown"` while carrying real reported
            # cost. On a subscription deployment that split the daemon's own
            # spend off from the identically-credentialled task rows for no
            # reason a reader could see. The flag asks the CLI to report the
            # credential it used; the alternative was inferring one from
            # config, which is the guess `cost_basis_from_api_key_source`
            # exists to refuse.
            cmd += ["--output-format", "json", "--verbose"]
        return cmd

    # --- non-streaming path ---

    def _execute_simple(self, cmd: list[str], req: BrainRequest) -> BrainResult:
        """Subprocess.run with auto-retry on transient API errors.

        Carries the last attempt's usage onto the two results this loop builds
        itself, for the reason the streaming path does: a run that reached the
        model and then exhausted its retries spent real tokens, and dropping
        them writes no row at all for the worst case. Inert until ISSUE-271 —
        on a CLI emitting the single-object shape there was never any usage
        here to lose.
        """
        last_error = ""
        last_usage = None
        for attempt in range(API_RETRY_MAX_ATTEMPTS):
            result = self._execute_simple_once(cmd, req)
            if result.usage is not None:
                last_usage = result.usage

            if result.success:
                return result

            # A usage/quota limit is persistent — do NOT retry it against the
            # same exhausted primary (a quota 429 matches is_transient_api_error,
            # so this short-circuit must precede that check). It reroutes to the
            # configured fallback brain at the executor level.
            if result.stop_reason == "usage_limit":
                return result

            if not _is_retryable(result):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"
            delay = parse_retry_after(result.result_text) or API_RETRY_DELAY_SECONDS

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ss...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, delay,
                )
                if _interruptible_sleep(delay, req):
                    return BrainResult(
                        success=False,
                        result_text="Cancelled by user",
                        stop_reason="cancelled",
                        usage=last_usage,
                    )
            else:
                logger.error(
                    "Transient API error persisted after %d attempts (request_id=%s)",
                    API_RETRY_MAX_ATTEMPTS, request_id,
                )

        return BrainResult(
            success=False,
            result_text=last_error,
            stop_reason="transient_api_error",
            usage=last_usage,
        )

    @staticmethod
    def _execute_simple_once(cmd: list[str], req: BrainRequest) -> BrainResult:
        """One non-streaming attempt, with its usage attached to whatever it
        returns.

        Same single-exit shape as the streaming path, and for the same reason:
        the inner function has several return points and tokens are spent on
        all of them.
        """
        # A timeout is classified here rather than left to propagate. The old
        # comment said it reached "the caller's own timeout handling, which
        # builds the result itself" — there is no such caller on this path: it
        # unwound past `_execute_simple` to `_execute_attempt`'s generic `except
        # Exception`, which logs `logger.exception` and returns
        # `stop_reason="error"`. So every non-streaming caller's timeout arrived
        # as an ERROR-level stack trace attributed to the brain, and lost the
        # `timeout` classification the vocabulary already has. That is merely
        # noisy for a nightly OCR pass and wrong for context triage, which times
        # out benignly once per conversational task on a slow provider.
        # `timeout` is in neither the fallback trigger set nor the breaker's
        # cooldown set, and `_is_retryable` already rejects it, so naming it
        # changes no routing — only the log line and the reported reason.
        # Nothing was parsed by then, so there is no usage to carry.
        accounting: dict = {}
        try:
            result = ClaudeCodeBrain._execute_simple_once_inner(cmd, req, accounting)
        except subprocess.TimeoutExpired:
            logger.warning(
                "claude timed out after %ss (non-streaming)", req.timeout_seconds
            )
            return BrainResult(
                success=False,
                result_text=f"Claude Code timed out after {req.timeout_seconds}s",
                stop_reason="timeout",
                usage=accounting.get("usage"),
            )
        result.usage = accounting.get("usage")
        return result

    @staticmethod
    def _execute_simple_once_inner(
        cmd: list[str], req: BrainRequest, accounting: dict
    ) -> BrainResult:
        # Still subprocess.run, and so still killing only the direct child on
        # timeout — the non-streaming path's grandchildren are orphaned the way
        # the streaming path's used to be (ISSUE-257, deferred half). Fixing it
        # means spawning via Popen so the group can be killed, and roughly
        # ninety tests across six files patch `subprocess.run` to keep the brain
        # from spawning at all, so they would each have to move to Popen first.
        # Narrower than the streaming path in the meantime: `_execute_simple_once`
        # never calls `req.on_pid`, so no `worker_pid` is recorded and neither
        # `!stop` nor the web cancel endpoint reaches this path at all — its own
        # timeout is the only killer, and nothing here wedges a worker (on POSIX
        # `subprocess.run` kills the child and `wait()`s, it does not re-drain
        # the pipes).
        #
        # Placement is the one thing this path does get, because it costs a
        # keyword argument. `subprocess.run` takes `preexec_fn` like `Popen`
        # does, so the child places itself before exec (ISSUE-285). It matters
        # more here than the paragraph above suggests: `use_streaming` is
        # `event_writer is not None`, so a deployment with
        # `scheduler.event_log_enabled = false` runs *every* task through here,
        # and without this the startup line would report containment for a host
        # on which no task was ever contained. There is no `verify_placement`
        # afterwards — `run` does not hand back a pid to check.
        with task_cgroup.placement(req.task_cgroup) as place_in_cgroup:
            result = subprocess.run(
                cmd,
                input=req.prompt,
                capture_output=True,
                text=True,
                timeout=req.timeout_seconds,
                cwd=str(req.cwd),
                env=req.env,
                preexec_fn=place_in_cgroup,
            )

        output = result.stdout.strip()

        # `--output-format json` gives either an array of the same frames the
        # streaming path emits or the bare terminal `result` object, depending
        # on the CLI version. Parsed here so the daemon's task-less model calls
        # are measured at all.
        #
        # The parse is guarded and the fallback is the whole point: roughly
        # ninety tests across six files patch `subprocess.run` with plain-text
        # stdout, and every real deployment predating this flag behaves the same
        # way. Anything that decodes as neither shape is treated as the answer
        # text exactly as before.
        answer_text, simple_usage = _parse_simple_json_output(output)
        if answer_text is not None:
            output = answer_text
        accounting["usage"] = simple_usage

        signal_death = _signal_result(result.returncode, None)
        if signal_death is not None:
            return signal_death

        # A session/quota limit or a provider API error is reported by
        # `claude -p` as a *successful* completion (rc 0, the banner as the
        # answer), so classify both on the success branch too — otherwise they
        # default to stop_reason="completed", never match the fallback trigger
        # set, and get delivered to the user as the reply.
        if result.returncode == 0 and output:
            reclassified = _success_frame_stop_reason(output)
            if reclassified:
                return BrainResult(
                    success=False, result_text=output, stop_reason=reclassified,
                    work_committed=True,
                )
            return BrainResult(success=True, result_text=output)
        if result.returncode == 0 and req.result_file and req.result_file.exists():
            file_text = req.result_file.read_text(encoding="utf-8").strip()
            reclassified = _success_frame_stop_reason(file_text)
            if reclassified:
                return BrainResult(
                    success=False, result_text=file_text, stop_reason=reclassified,
                    work_committed=True,
                )
            return BrainResult(success=True, result_text=file_text)
        if output:
            return BrainResult(
                success=False, result_text=output,
                stop_reason=_failure_stop_reason(output),
            )
        if result.stderr.strip():
            stderr = result.stderr.strip()
            return BrainResult(
                success=False, result_text=stderr,
                stop_reason=_failure_stop_reason(stderr),
            )
        return BrainResult(
            success=False,
            result_text=f"Claude Code produced no output (rc={result.returncode})",
            stop_reason="error",
        )

    # --- streaming path ---

    def _execute_streaming(self, cmd: list[str], req: BrainRequest) -> BrainResult:
        """Popen + stream-json parsing with auto-retry on transient API errors.

        Only the final attempt's usage is carried, which is the documented
        limitation: an in-brain retry can burn two context loads before a 529 and
        this records one. What it must not do is record *none* — the two results
        this function builds itself used to leave `usage` at None, so a run that
        streamed real requests and then exhausted its retries wrote no row at
        all. That is the worst case to lose, because `transient_api_error` is in
        the executor's default fallback trigger set: the primary's spend would
        vanish and the fallback's would be the only tokens the task ever
        recorded.
        """
        last_error = ""
        last_trace = None
        last_usage = None
        last_partial = ""

        for attempt in range(API_RETRY_MAX_ATTEMPTS):
            result = self._execute_streaming_once(cmd, req)
            if result.usage is not None:
                last_usage = result.usage

            if result.success:
                return result

            last_trace = result.execution_trace
            # Carried across attempts for the same reason `last_usage` is: the
            # cancel return below is built here, outside the attempt, so without
            # this a `!stop` landing during a backoff discards prose the previous
            # attempt had already produced (ISSUE-372).
            if result.partial_text:
                last_partial = result.partial_text

            # Persistent usage/quota limit — reroute (not retry). Precedes the
            # transient check because a quota 429 also matches it.
            if result.stop_reason == "usage_limit":
                return result

            if not _is_retryable(result):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"
            delay = parse_retry_after(result.result_text) or API_RETRY_DELAY_SECONDS

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ss...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, delay,
                )
                if _interruptible_sleep(delay, req):
                    return BrainResult(
                        success=False,
                        result_text="Cancelled by user",
                        stop_reason="cancelled",
                        usage=last_usage,
                        partial_text=last_partial or None,
                    )
            else:
                logger.error(
                    "Transient API error persisted after %d attempts (request_id=%s)",
                    API_RETRY_MAX_ATTEMPTS, request_id,
                )

        return BrainResult(
            success=False,
            result_text=last_error,
            execution_trace=last_trace,
            stop_reason="transient_api_error",
            usage=last_usage,
            partial_text=last_partial or None,
        )

    @staticmethod
    def _execute_streaming_once(cmd: list[str], req: BrainRequest) -> BrainResult:
        """One streaming attempt, with its usage attached to whatever it returns.

        The accounting is collected into a dict the inner function fills as it
        parses, and the `BrainUsage` is built here — once, on the way out. The
        inner function has around a dozen return points (cancel, timeout, OOM,
        several error classifications) and tokens are spent on all of them, so
        stamping at a single exit is what keeps a later-added return from
        silently dropping a measurement.
        """
        accounting: dict = {}
        try:
            result = ClaudeCodeBrain._execute_streaming_once_inner(
                cmd, req, accounting
            )
        except Exception as e:
            # A raise past the inner body would otherwise discard everything
            # measured before it: `execute`'s catch-all returns a bare result,
            # and the tokens are spent either way. Converted to a failure result
            # here so the accounting survives.
            logger.exception("streaming attempt raised")
            result = BrainResult(
                success=False,
                result_text=f"Execution error: {e}",
                stop_reason="error",
            )
        result.usage = usage_types.from_cli_result(
            accounting.get("result_frame"),
            accounting.get("requests") or [],
            accounting.get("api_key_source"),
            model_hint=accounting.get("model_seen", ""),
            subagent_requests=accounting.get("subagent_requests", 0),
            compacted_requests=accounting.get("compacted_requests", 0),
            rate_limit=accounting.get("rate_limit"),
        )
        return result

    @staticmethod
    def _execute_streaming_once_inner(
        cmd: list[str], req: BrainRequest, accounting: dict
    ) -> BrainResult:
        actions_descriptions: list[str] = []
        execution_trace: list[dict] = []
        last_text = ""
        stderr_lines: list[str] = []

        # Per-task cgroup (A6), placed from the child rather than moved into
        # place afterwards. bwrap forks its inner process during namespace
        # setup, long before Popen returns, and cgroup v2 membership is
        # inherited at fork — so a write to `cgroup.procs` after the fact caught
        # the outer bwrap and left the CLI, its bash grandchildren and whatever
        # they spawn in the daemon's own leaf forever (ISSUE-285). `preexec_fn`
        # is None on any deployment with no delegated subtree, which is the
        # spawn this path has always done.
        with task_cgroup.placement(req.task_cgroup) as place_in_cgroup:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(req.cwd),
                env=req.env,
                # Own process group so a timeout, a `!stop` or a web cancel can
                # signal the whole tree. The CLI's bash grandchildren are where
                # the work actually is — a `pytest -n auto` run survived a bare
                # process.kill() and finished on a saturated host (ISSUE-257).
                # This also makes the pid handed to on_pid below a group leader,
                # which is what lets the two cancel endpoints reach the group.
                start_new_session=True,
                preexec_fn=place_in_cgroup,
            )

        # The child cannot report a failed placement, so the parent reads the
        # membership back. Only when placement actually engaged: where it did
        # not, `placement` has already said why, and asking again would report
        # one cause twice. Nothing is retried on a miss either — moving the pid
        # at this point is exactly the write that produced ISSUE-285.
        if place_in_cgroup is not None and req.task_cgroup is not None:
            task_cgroup.verify_placement(process.pid, req.task_cgroup)

        # Feed the prompt to stdin on a dedicated thread, started immediately
        # after spawn. The `claude` CLI aborts its stdin read after ~3s
        # ("no stdin data received in 3s, proceeding without it") and then
        # runs with an *empty* prompt — so prompt delivery must not be gated
        # behind anything slow. A synchronous write here would sit behind the
        # on_pid DB write below (which can block on the SQLite write lock under
        # daemon load); if that gap exceeds the CLI's stdin deadline the task
        # fails with "produced no output". Threading also avoids a deadlock
        # when the prompt exceeds the OS pipe buffer (~64KB) before any reader
        # has drained it. Mirrors subprocess.run(input=...)'s feeder thread,
        # which is why the non-streaming path was never affected.
        def _write_stdin() -> None:
            try:
                process.stdin.write(req.prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # process may have exited / closed stdin early

        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stdin_thread.start()

        # Notify caller of PID (used for !stop). The stdin write is already in
        # flight on its own thread, so a slow DB write here no longer delays
        # prompt delivery.
        if req.on_pid is not None:
            try:
                req.on_pid(process.pid)
            except Exception:
                logger.debug("on_pid callback raised", exc_info=True)

        def _read_stderr() -> None:
            for line in process.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # Timeout via timer
        timed_out = threading.Event()

        def _kill_group_if_live() -> None:
            # `process.kill()` used to go through Popen.send_signal, which
            # no-ops once the child is reaped. A raw pid carries no such check,
            # and the reap happens at `process.wait()` below while this timer
            # can still fire during the two 5s thread joins that follow it —
            # long enough for the OS to hand the number to someone else, whose
            # group we would then kill. Mirrors the guard bash.py already has.
            if process.returncode is None:
                kill_process_group(process.pid)

        def _kill() -> None:
            timed_out.set()
            _kill_group_if_live()

        timer = threading.Timer(req.timeout_seconds, _kill)
        timer.start()

        final_result: ResultEvent | None = None
        # A count, not the lines. Every reader below wants `len()` or a
        # truthiness test, and `--include-partial-messages` emits a frame per
        # content delta — so retaining them held the whole stream of a long run
        # in memory for its duration, for three diagnostics that only ever ask
        # how many there were. Reachable at scale since ISSUE-448 put the two
        # code-review agents on this path concurrently with an eight-minute
        # budget each, in one skill-CLI process.
        stdout_line_count = 0
        cancelled = False
        # The model the CLI actually used. The stream-json ``system``/``init``
        # frame carries it (it reflects the resolved default when --model was
        # omitted), so this is more accurate than req.model for the default case.
        model_seen = ""
        # Usage accounting, collected alongside the model sniff. `api_key_source`
        # decides whether the cost figure is real money or a plan-equivalent.
        api_key_source: str | None = None
        requests: list[usage_types.RequestUsage] = []
        accounting["requests"] = requests
        parse_line = make_stream_parser()

        try:
            for line in process.stdout:
                stdout_line_count += 1
                if (not model_seen or api_key_source is None) and (
                    '"model"' in line or '"apiKeySource"' in line
                ):
                    try:
                        _d = json.loads(line)
                        if _d.get("type") == "system":
                            if not model_seen and _d.get("model"):
                                model_seen = str(_d["model"])
                                accounting["model_seen"] = model_seen
                            if api_key_source is None and _d.get("apiKeySource"):
                                api_key_source = str(_d["apiKeySource"])
                                accounting["api_key_source"] = api_key_source
                    except (json.JSONDecodeError, AttributeError):
                        pass
                event = parse_line(line)
                if event is None:
                    continue

                # Accounting frames are consumed here and never reach
                # `execution_trace` or `req.on_progress`. That is load-bearing:
                # the executor fans progress events out to live surfaces, and a
                # token-accounting frame appearing in a user's chat is a bug.
                if isinstance(event, RequestUsageEvent):
                    if event.is_subagent:
                        # Keeps `peak_context_tokens` meaning *this* agent's
                        # peak. Counted rather than dropped, so the number can
                        # be checked against reality later — fan-out is denied
                        # today, so a non-zero value means the deny list changed
                        # or the CLI dropped the `Agent` alias.
                        accounting["subagent_requests"] = (
                            accounting.get("subagent_requests", 0) + 1
                        )
                    elif event.compacted:
                        # A compaction replays the previous response; counting it
                        # would inflate `model_requests` with no real request.
                        accounting["compacted_requests"] = (
                            accounting.get("compacted_requests", 0) + 1
                        )
                    else:
                        requests.append(
                            usage_types.RequestUsage(
                                prompt_tokens=event.prompt_tokens,
                                output_tokens=event.output_tokens,
                                model=event.model or model_seen,
                            )
                        )
                    continue
                if isinstance(event, RateLimitEvent):
                    accounting["rate_limit"] = event.info
                    continue

                if isinstance(event, ResultEvent):
                    final_result = event
                    accounting["result_frame"] = event.raw
                elif isinstance(event, ContextManagementEvent):
                    execution_trace.append({"type": "cm_boundary"})
                    continue  # don't stream CM markers
                elif isinstance(event, ToolUseEvent):
                    actions_descriptions.append(event.description)
                    tool_entry = {"type": "tool", "text": event.description}
                    if event.invocation:
                        tool_entry["raw"] = event.invocation
                    execution_trace.append(tool_entry)
                elif isinstance(event, TextEvent):
                    execution_trace.append({"type": "text", "text": event.text})
                    # The last text-bearing block, for the two stops that
                    # otherwise deliver a fixed string and discard everything
                    # the model wrote (ISSUE-372). Tracked here rather than
                    # recovered from the trace at the return sites so the two
                    # brains populate `partial_text` from the same thing.
                    if event.text.strip():
                        last_text = event.text
                # ThinkingEvent / TextDeltaEvent / ThinkingDeltaEvent are
                # intentionally NOT added to execution_trace: reasoning and the
                # token-level answer deltas are live-stream-only concerns
                # (``thinking`` / ``text_delta`` task events on stream surfaces),
                # never persisted in the trace, so result composition / history
                # reconstruction stay unchanged. The whole-block TextEvent above
                # is the trace's record of the answer text.

                if isinstance(
                    event,
                    (
                        ToolUseEvent,
                        TextEvent,
                        ThinkingEvent,
                        TextDeltaEvent,
                        ThinkingDeltaEvent,
                    ),
                ) and req.on_progress is not None:
                    try:
                        req.on_progress(event)
                    except Exception:
                        logger.debug("on_progress raised", exc_info=True)

                # Cancellation poll between events
                if isinstance(event, (ToolUseEvent, TextEvent)) and req.cancel_check is not None:
                    try:
                        if req.cancel_check():
                            logger.info("Cancellation requested, killing subprocess")
                            _kill_group_if_live()
                            cancelled = True
                            break
                    except Exception:
                        logger.debug("cancel_check raised", exc_info=True)

            process.wait()
            stderr_thread.join(timeout=5)
            stdin_thread.join(timeout=5)
        finally:
            timer.cancel()

        actions_json = json.dumps(actions_descriptions) if actions_descriptions else None
        trace_json = json.dumps(execution_trace) if execution_trace else None

        # Final cancellation check — SIGTERM from !stop may kill the process
        # before the in-loop check runs.
        if not cancelled and req.cancel_check is not None:
            try:
                if req.cancel_check():
                    cancelled = True
            except Exception:
                pass

        if cancelled:
            return BrainResult(
                success=False,
                result_text="Cancelled by user",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="cancelled",
                partial_text=last_text or None,
            )

        if timed_out.is_set():
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="timeout",
                partial_text=last_text or None,
            )

        # A signal death outranks every remaining branch: the process was killed
        # from outside, so whatever it had (or hadn't) written to stdout says
        # nothing about why. The trace rides along — the tools that ran before
        # the kill are the only diagnostic left (ISSUE-183/191).
        signal_death = _signal_result(process.returncode, trace_json)
        if signal_death is not None:
            logger.warning(
                "claude subprocess died on a signal: %s (stdout_lines=%d)",
                signal_death.result_text, stdout_line_count,
            )
            return signal_death

        stderr_output = "".join(stderr_lines).strip()

        # Extract result: prefer ResultEvent, fall back to result file, then stderr.
        if final_result is not None:
            result_text = final_result.text.strip()
            if final_result.success:
                # `claude -p` reports a session/quota limit — and a provider API
                # error — as a success result frame (subtype:"success", the
                # banner as `result`). Classify both here so they reroute to the
                # fallback brain instead of being delivered as the answer with
                # the default stop_reason="completed". Use the strict *banner*
                # detectors: the broad keyword ones would misread a genuine
                # answer that merely quotes a limit word or an earlier API error
                # (e.g. a memory extraction summarising a past outage).
                reclassified = _success_frame_stop_reason(result_text)
                if reclassified:
                    return BrainResult(
                        success=False,
                        result_text=result_text,
                        execution_trace=trace_json,
                        stop_reason=reclassified,
                        model_used=model_seen or req.model,
                        work_committed=True,
                    )
                return BrainResult(
                    success=True,
                    result_text=result_text,
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
                )
            failure_text = result_text or stderr_output or "Unknown error"
            return BrainResult(
                success=False,
                result_text=failure_text,
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(failure_text),
                model_used=model_seen or req.model,
            )

        if req.result_file and req.result_file.exists():
            output = req.result_file.read_text(encoding="utf-8")
            if process.returncode == 0:
                reclassified = _success_frame_stop_reason(output)
                if reclassified:
                    return BrainResult(
                        success=False,
                        result_text=output.strip(),
                        execution_trace=trace_json,
                        stop_reason=reclassified,
                        model_used=model_seen or req.model,
                        work_committed=True,
                    )
                return BrainResult(
                    success=True,
                    result_text=output.strip(),
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
                )
            # A limit or API-error message written to the result file (rather
            # than a ResultEvent) must still carry its own stop_reason, not a
            # generic error — otherwise it's not a fallback trigger.
            return BrainResult(
                success=False,
                result_text=output.strip(),
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(output),
            )

        logger.warning(
            "No ResultEvent parsed from stream-json (rc=%s, stderr=%s, stdout_lines=%d)",
            process.returncode,
            stderr_output[:200] if stderr_output else "(empty)",
            stdout_line_count,
        )

        if stderr_output:
            return BrainResult(
                success=False, result_text=stderr_output,
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(stderr_output),
            )
        if stdout_line_count:
            return BrainResult(
                success=False,
                result_text=f"Stream parsing failed (rc={process.returncode}, {stdout_line_count} lines)",
                execution_trace=trace_json,
                stop_reason="error",
            )
        return BrainResult(
            success=False,
            result_text=f"Claude Code produced no output (rc={process.returncode})",
            stop_reason="error",
        )
