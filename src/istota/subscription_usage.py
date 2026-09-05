"""Plan utilization for a Claude Code subscription.

On a subscription deployment the dashboard's cost column is deliberately blank —
a plan-equivalent list price is not spend — so the real budget is the rate-limit
windows Anthropic reports at ``GET /api/oauth/usage``. This module is the one
place that knows that endpoint's shape: a pure parser, a thin blocking fetch, and
a disk cache with a TTL and a stale-fallback read. Three surfaces (the doctor
check, the admin card, ``!usage``) read it; none of them grows its own fetch or
its own parse.

A fourth reader is not a surface and does not fetch. ``cached_reset_seconds`` is
how the brain-availability breaker learns when a quota comes back (ISSUE-374),
and it is on a *task's* failure path rather than a diagnostic one, so it takes
the disk cache alone — no credential, no socket, no dependence on a fetch having
succeeded. That constraint is the whole reason it lives here as its own entry
point rather than as a ``get_snapshot`` call at the call site.

A *failure* is recorded beside the cache rather than in it, and suppresses the
next TTL's worth of attempts — see the failure-timer section below. Without it a
rejected credential would be re-tried on every dashboard poll, because a failed
reading is never cached and there is nothing else to bound the retry.

**Nothing here raises.** Every snapshot entry point returns a ``UsageSnapshot``,
and a failure is a snapshot carrying a non-empty ``error``; the two reset
readers return ``int | None`` and answer a failure with ``None``. Callers reach
this module from a diagnostic path — one of them is the daemon's boot sequence —
or from a task that is already failing, where an exception is worse than a
missing number either way.

**Stdlib only** (``urllib.request``, not httpx), following the leaf convention of
``host_pressure.py``, ``forge_cli.py`` and ``ntfy_headers.py``: this is imported
from ``doctor.py``, which sits near the config-load path, and the request is one
GET with three headers.

Nothing here knows about doctor, the web app or the sandbox. Every path, the
environment, the clock and the transport are parameters. ``get_snapshot`` takes a
``Config`` only to read ``db_path`` and the ``brain.claude_code.*`` settings, and
reads them defensively so a config predating the block behaves as the shipping
default.

The credential is **read, never written and never refreshed**. See
``resolve_token``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from istota.retry_after import parse_retry_after as _parse_retry_after
from istota.retry_after import retry_after_from_headers as _retry_after_from_headers

from . import __version__
from .atomic_write import write_text_atomic

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

logger = logging.getLogger("istota.subscription_usage")

BASE_URL = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = f"istota/{__version__}"
_CACHE_FILENAME = "subscription_usage.json"
# The failure timer, beside the reading cache. Separate file rather than a field
# of the cache: the cache holds successful readings only, and a failure that
# shared the file would have to be skipped by every reader that measures
# freshness — one condition, in four places, that a sibling avoids entirely.
_FAILURE_FILENAME = "subscription_usage.failure.json"

# Ceiling on an error string read back out of the timer file. The doctor prints
# it on one line of a terminal and the admin card renders it into a note, so an
# unbounded string off disk would push the rest of a report off the screen.
MAX_ERROR_CHARS = 300

# Ceiling on a server-stated ``Retry-After``. Six hours comfortably clears the
# ~39 minutes the endpoint actually asks for while bounding a buggy or hostile
# value: past this we retry earlier than asked, which is impolite but keeps a
# single bad header from silencing the reading indefinitely. The doctor keeps
# WARNing throughout, so a deployment stuck at the ceiling is visible rather
# than quiet.
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60

# Ceiling on either file's size. Both are a few kB when this module writes them;
# past this they are corrupt, and reading them whole is a memory cost chosen by
# whatever is in a shared data dir rather than by this module.
_MAX_FILE_CHARS = 1 << 20

# Bound on the macOS Keychain probe. Short: it is a local lookup, and a hung
# `security` call would stall a doctor run or a dashboard refresh. Named for what
# it bounds rather than `PROBE_TIMEOUT`, which is a *different* value (10) in
# doctor.py — and doctor imports this module at Stage 3.
KEYCHAIN_PROBE_TIMEOUT = 5.0

# Magnitude ceiling for any number taken off the wire or out of the cache.
# `json.loads` builds an arbitrary-precision int for a long integer literal, and
# `float()` on one raises OverflowError. Percentages, epoch seconds and minor
# currency units are all many orders of magnitude below this.
_MAX_MAGNITUDE = 10**15

# Response bodies are small (a few kB). The cap is a guard against a proxy or a
# captive portal handing us something enormous, not a real size expectation.
_MAX_BODY_BYTES = 1 << 20
# HTTP error bodies are read for the DEBUG log only; they are never echoed into
# an error string a user or an operator sees.
_ERROR_BODY_CHARS = 200

# Settings defaults. The same three numbers as the corresponding fields of
# ``ClaudeCodeBrainConfig`` in config.py, deliberately copied rather than shared
# through an import in either direction. This module must not import
# ``istota.config`` — it is a stdlib-only leaf, reached from doctor. And
# ``istota.config`` must not import this module, because it is loaded by every
# CLI invocation and every host-side skill CLI the skill proxy spawns per call,
# and three numbers are not worth pulling urllib and subprocess onto that path.
# The copy also earns its keep on its own: ``_settings`` reads defensively, and
# a Config predating ``[brain.claude_code]`` has to behave as the shipping
# default rather than raise.
#
# ``TestOneSourceOfTruthForTheDefaults`` in tests/test_config_claude_code_brain.py
# pins these against the dataclass, so the two sets cannot drift apart.
DEFAULT_SUBSCRIPTION_USAGE = True
DEFAULT_CACHE_TTL_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 10.0

# Every value ``resolve_token`` can report, and the only ones a caller may
# render. The cache is a file on disk, so what comes back out of it is input:
# the doctor check and (from Stage 4) the admin payload interpolate this into
# text a person reads, and an unvalidated read would let a hand-edited cache put
# arbitrary content there. Anything else reads as "unknown", which is what an
# unrecognized branch is.
TOKEN_SOURCES = frozenset({"env", "file", "keychain"})

NO_CREDENTIAL_ERROR = "no Claude Code OAuth credential found"
NO_WINDOWS_ERROR = "the endpoint returned no recognizable rate-limit windows"
DISABLED_ERROR = "disabled by config"

# Allowlist for the fallback parse path. Anything not named here is dropped,
# including the unreleased codenames the endpoint returns in the same top-level
# namespace. This is the one place the module deliberately renders less than the
# payload offers: an unshipped feature name must not reach a public project's
# dashboard.
TOP_LEVEL_WINDOWS: dict[str, str] = {
    "five_hour": "5-hour",
    "seven_day": "Weekly (all models)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_oauth_apps": "Weekly (OAuth apps)",
}

# Labels for the ``limits[]`` path. ``weekly_scoped`` takes its label from
# ``scope.model.display_name``, so it is not in this table. An unknown kind is
# *kept* here (labelled from the kind itself) rather than dropped: ``limits[]``
# entries are structured records, not a namespace shared with codenames.
LIMIT_KINDS: dict[str, str] = {
    "session": "5-hour",
    "weekly_all": "Weekly (all models)",
}

_SCOPED_KIND = "weekly_scoped"

# ``(url, headers, timeout) -> (status, body, response_headers)``. Injectable so
# no test touches the network; the default is a small urllib wrapper.
#
# The response headers are in the contract because of ``Retry-After``. A 429 is
# the endpoint's normal answer to a deployment that polls it, and it says in
# that header when to come back — observed at 2327 seconds against a cache TTL
# that was retrying every 300. Dropping the headers, as this seam used to, made
# the one number that could have prevented seven pointless retries per window
# unreachable by construction.
Transport = Callable[
    [str, dict[str, str], float], "tuple[int, bytes, Mapping[str, str]]"
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageWindow:
    """One rate-limit window, from either parse path.

    ``key`` is a stable id (``"session"``, ``"weekly_all"``,
    ``"weekly_scoped:fable"``); ``label`` is display text. ``severity`` and
    ``is_active`` are the server's own — carried on the wire for a future use,
    never acted on and never filtered on here.
    """

    key: str
    label: str
    percent: float
    resets_at: str | None = None
    resets_in_seconds: int | None = None
    severity: str = ""
    is_active: bool | None = None


@dataclass(frozen=True)
class Spend:
    """Pay-as-you-go credits beyond the plan. Real money, unlike a token cost.

    Amounts are minor units (cents for USD); ``exponent`` is the number of minor
    units per major as a power of ten. Taken from the payload rather than
    hardcoded to 100, which is wrong for any currency that is not two-decimal.
    """

    enabled: bool = False
    used_minor: int = 0
    limit_minor: int = 0
    currency: str = "USD"
    exponent: int = 2
    percent: float = 0.0


@dataclass(frozen=True)
class UsageSnapshot:
    """A reading, or the reason there isn't one.

    ``fetched_at`` is when the data was *obtained*, not when it was read, so a
    cache hit carries the original fetch time; a snapshot with no data at all
    carries ``0.0``, uniformly, so a caller cannot read an age off one branch and
    a different meaning off the next. ``source`` names where the data came from:
    ``"fetch"``, ``"cache"``, ``"stale-cache"``, or ``"none"`` when there is no
    data (disabled, no credential, or a failed fetch with no cache behind it).

    ``error`` and ``windows`` are independent, and the ``stale-cache`` branch is
    why: it carries real windows *and* the fetch error that made them stale, so a
    caller can render an old reading and say why it is old. Read ``ok`` to mean
    "this is a current, trustworthy reading" and ``has_data`` to mean "there is
    something here to render". A snapshot carrying an ``error`` is never written
    to the cache.

    ``token_source`` is :func:`resolve_token`'s branch name — ``"env"``,
    ``"file"`` or ``"keychain"`` — and **never the token**. It is here rather
    than re-derived by each caller because both of them need to name which
    credential produced the reading, and re-resolving to recover it would spawn
    ``security`` on every cache hit on macOS, which is the cost the cache exists
    to avoid. It is populated on the failures too: "which credential was
    rejected" is the whole value of the field, and a setup token in the
    environment and an interactive login in the keychain fail for different
    reasons and have different repairs. It is empty where no branch resolved
    anything (disabled, no credential).
    """

    fetched_at: float
    windows: tuple[UsageWindow, ...] = ()
    spend: Spend | None = None
    source: str = "fetch"
    token_source: str = ""
    error: str = ""
    # Seconds the endpoint asked us to wait, off a failure's ``Retry-After``.
    # Never rendered — it exists to reach ``write_failure``, so the backoff is
    # the interval the server named rather than the one we guessed.
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        """True when this is a current reading with nothing wrong with it.

        False on a stale-cache snapshot even though that one has windows — use
        ``has_data`` to decide whether there is anything to render.
        """
        return not self.error

    @property
    def has_data(self) -> bool:
        """True when there are windows to render, current or stale."""
        return bool(self.windows)

    def age_seconds(self, now_ts: float) -> float:
        return now_ts - self.fetched_at


# ---------------------------------------------------------------------------
# Coercion helpers — same discipline as usage.py's _int / _float
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A JSON number as a finite float, or ``None`` for anything else.

    ``bool`` is excluded because it is an ``int`` subclass and ``True`` would
    otherwise read as the number 1. Non-finite floats are rejected: ``json.loads``
    accepts the bare tokens ``NaN`` and ``Infinity`` by default, so they really do
    arrive. Strings are rejected — this endpoint reports numbers as numbers, and
    coercing ``"40"`` would be guessing.

    The magnitude bound is the part that is easy to miss. ``json.loads`` builds an
    arbitrary-precision ``int`` for a long integer literal, and ``float()`` on one
    raises ``OverflowError`` — an exception out of a coercion helper, from remote
    input, in a module whose whole contract is that it does not raise. The bound
    is far above any figure this endpoint reports.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and not -_MAX_MAGNITUDE < value < _MAX_MAGNITUDE:
        return None
    try:
        as_float = float(value)
    except (OverflowError, ValueError):  # a float subclass with a hostile __float__
        return None
    return as_float if math.isfinite(as_float) else None


def _percent(value: Any) -> float | None:
    """A utilization figure clamped to ``[0, 100]``, or ``None`` if unusable.

    A window with an unusable percent is dropped by its caller rather than
    rendered as zero: a fabricated 0% against a full quota is the worst error
    this could make. Over-quota is a real state, so anything above 100 clamps to
    100 rather than being dropped.
    """
    as_float = _number(value)
    if as_float is None:
        return None
    return max(0.0, min(100.0, as_float))


def _unclamped_percent(value: Any, default: float = 0.0) -> float:
    """A percentage that may legitimately exceed 100.

    Used for ``Spend.percent`` only. Clamping is right for a plan window, whose
    ceiling is the plan, and wrong for pay-as-you-go credits: 150% of a spend cap
    is real money already committed, and rendering it as 100% would hide an
    overage on the one figure here that is not a token count.
    """
    as_float = _number(value)
    if as_float is None:
        return default
    return max(0.0, as_float)


def _int(value: Any, default: int = 0) -> int:
    as_float = _number(value)
    if as_float is None:
        return default
    try:
        return int(as_float)
    except (OverflowError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _slug(value: str) -> str:
    """A display name reduced to a stable key fragment. May be empty."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _redact(text: str, token: str) -> str:
    """Remove a credential from a message built out of an exception string.

    Nothing in this module puts a token into an error deliberately, but an
    injected transport's exception text is not ours to trust, and this string
    ends up in a doctor ``detail`` and an admin payload.
    """
    if token and token in text:
        return text.replace(token, "***")
    return text


def _normalize_resets_at(value: Any) -> tuple[str | None, float | None]:
    """``(canonical ISO-8601 UTC, the reset's epoch seconds)``.

    An unparseable *string* is carried through verbatim (it is what the endpoint
    said) with ``None`` for the timestamp; anything that is not a non-empty
    string yields ``(None, None)``. An offset other than UTC is converted, not
    just relabelled.

    The whole conversion is inside the ``try``, not just the parse. A value at
    the edge of the datetime range parses cleanly and then overflows on the
    shift to UTC — ``"9999-12-31T23:59:59-14:00"`` does, and that is one
    keystroke from the sentinel expiry this codebase writes into credential
    files, so it is not an exotic input.
    """
    if not isinstance(value, str) or not value:
        return None, None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), parsed.timestamp()
    except Exception:  # noqa: BLE001 — an unusable date costs the timestamp, not the window
        return value, None


def _window_times(value: Any, now_ts: float) -> tuple[str | None, int | None]:
    """``(canonical resets_at, resets_in_seconds)`` for one window.

    ``resets_in_seconds`` floors at 0: clock skew between this host and
    Anthropic is normal, and a negative duration would render as nonsense.
    """
    canonical, ts = _normalize_resets_at(value)
    if ts is None:
        return canonical, None
    return canonical, max(0, int(ts - now_ts))


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------


def resolve_token(env: Mapping[str, str], home: Path | None) -> tuple[str, str] | None:
    """``(token, source)`` from the first source that has one, else ``None``.

    Ordered: ``CLAUDE_CODE_OAUTH_TOKEN`` (``"env"``, what both server shapes
    actually set) → ``~/.claude/.credentials.json`` (``"file"``) → the macOS
    Keychain (``"keychain"``).

    **No expiry check, no refresh, no write, on any branch.** Three reasons, each
    sufficient on its own. The server credential's ``expiresAt`` is the sentinel
    ``"9999-12-31T23:59:59.999Z"`` — a *string* where the keychain blob holds
    epoch milliseconds as an *int*, so arithmetic on it raises on exactly the
    deployment shape this has to work on. The server credential has no
    ``refreshToken`` to refresh with. And a daemon rewriting
    ``~/.claude/.credentials.json`` would be racing the ``claude`` subprocesses
    it spawns for that same file. An expired token is reported as an error, not
    repaired.

    ``env`` and ``home`` are parameters so no test reads the real ones. ``home``
    may be ``None`` when this process has no resolvable home directory, in which
    case the file branch is skipped rather than raising.
    """
    token = _clean_token(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
    if token:
        return token, "env"

    if home is not None:
        token = _token_from_file(Path(home) / ".claude" / ".credentials.json")
        if token:
            return token, "file"

    token = _token_from_keychain(env)
    if token:
        return token, "keychain"

    return None


def _clean_token(value: Any) -> str:
    """A resolved credential, or ``""`` if it is not usable as a header value.

    Surrounding whitespace is stripped (an env var set from a file usually ends
    in a newline). A token containing a control character — CR, LF, NUL — is
    rejected outright rather than passed on. Two reasons: ``http.client`` refuses
    such a header value anyway, so it could never authenticate; and it refuses it
    by raising a ``ValueError`` that embeds the value as a *repr*, which a
    substring redaction would not match, putting the credential into an error
    string and a log line. Rejecting it at the source removes that class rather
    than trying to scrub it downstream.
    """
    token = _str(value).strip()
    if not token or any(ch in token for ch in "\r\n\x00") or not token.isprintable():
        return ""
    return token


def _token_from_blob(text: str) -> str:
    """``claudeAiOauth.accessToken`` out of a credentials JSON blob, or ``""``."""
    try:
        blob = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed credential file is "no token"
        return ""
    return _clean_token(_dict(_dict(blob).get("claudeAiOauth")).get("accessToken"))


def _token_from_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 — unreadable / a directory / permissions
        logger.debug("credential file read failed: %s", path, exc_info=True)
        return ""
    return _token_from_blob(text)


def _token_from_keychain(env: Mapping[str, str]) -> str:
    """The macOS Keychain branch. Spawns nothing anywhere but Darwin."""
    try:
        if platform.system() != "Darwin":
            return ""
        account = _str(env.get("USER"))
        if not account:
            return ""
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_PROBE_TIMEOUT,
        )
        if proc.returncode != 0:
            return ""
        return _token_from_blob(proc.stdout or "")
    except Exception:  # noqa: BLE001 — a timeout, a missing binary, anything
        logger.debug("keychain credential lookup failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


def parse_usage(raw: object, *, now_ts: float) -> tuple[tuple[UsageWindow, ...], Spend | None]:
    """Parse a ``/api/oauth/usage`` payload into windows plus optional spend.

    Two overlapping views of the same fact exist in the payload. ``limits[]`` is
    the richer one — it carries the server's ``severity``, an ``is_active`` flag
    and a ``scope`` naming the model in a form fit for display — so it is the
    primary path; the allowlisted top-level keys are the fallback, taken when
    ``limits`` is absent, empty, or not a list. Both produce the same
    ``UsageWindow`` list, so nothing downstream learns which one ran.

    Pure, and never raises: a bad field drops its window, a bad payload yields
    ``((), None)``.
    """
    payload = _dict(raw)
    if not payload:
        return (), None

    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        windows = _parse_limits(limits, now_ts)
    else:
        windows = _parse_top_level(payload, now_ts)

    return tuple(windows), _parse_spend(payload)


def _parse_limits(entries: list, now_ts: float) -> list[UsageWindow]:
    """Every usable entry, first occurrence of each key winning.

    The de-duplication is what makes ``_scoped_key_and_label``'s promise true.
    Slugging is lossy — ``"Fable 4"`` and ``"Fable-4"`` both slug to ``fable-4``
    — so dropping the unnamed scoped entry is not on its own enough to stop two
    windows sharing a key. Two tiles with the same key and different numbers is
    the outcome being avoided, whichever way the collision arises.
    """
    out: list[UsageWindow] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            window = _parse_one_limit(entry, now_ts)
        except Exception:  # noqa: BLE001 — one bad entry must not kill the rest
            logger.debug("rate-limit entry parse raised; skipping", exc_info=True)
            continue
        if window is None:
            continue
        if window.key in seen:
            logger.debug("duplicate rate-limit window key; keeping the first: %s", window.key)
            continue
        seen.add(window.key)
        out.append(window)
    return out


def _parse_one_limit(entry: Any, now_ts: float) -> UsageWindow | None:
    if not isinstance(entry, dict):
        return None
    kind = _str(entry.get("kind"))
    if not kind:
        return None
    percent = _percent(entry.get("percent"))
    if percent is None:
        return None

    if kind == _SCOPED_KIND:
        named = _scoped_key_and_label(entry)
        if named is None:
            return None
        key, label = named
    else:
        key = kind
        label = LIMIT_KINDS.get(kind) or kind.replace("_", " ").capitalize()

    resets_at, resets_in = _window_times(entry.get("resets_at"), now_ts)
    is_active = entry.get("is_active")
    return UsageWindow(
        key=key,
        label=label,
        percent=percent,
        resets_at=resets_at,
        resets_in_seconds=resets_in,
        severity=_str(entry.get("severity")),
        is_active=is_active if isinstance(is_active, bool) else None,
    )


def _scoped_key_and_label(entry: dict) -> tuple[str, str] | None:
    """``weekly_scoped`` names itself from ``scope.model``.

    ``display_name`` first, then ``id``. When neither is usable the entry is
    dropped rather than sharing a key with another scoped window: two tiles with
    identical labels and different numbers is worse than one missing tile.
    (That drop is why the spec's third label fallback, ``"Weekly (scoped)"``, is
    unreachable and so not implemented.)
    """
    model = _dict(_dict(entry.get("scope")).get("model"))
    name = _str(model.get("display_name")) or _str(model.get("id"))
    slug = _slug(name) if name else ""
    if not slug:
        return None
    return f"{_SCOPED_KIND}:{slug}", f"Weekly ({name})"


def _parse_top_level(payload: dict, now_ts: float) -> list[UsageWindow]:
    out: list[UsageWindow] = []
    for key, label in TOP_LEVEL_WINDOWS.items():
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        percent = _percent(entry.get("utilization"))
        if percent is None:
            continue
        resets_at, resets_in = _window_times(entry.get("resets_at"), now_ts)
        out.append(
            UsageWindow(
                key=key,
                label=label,
                percent=percent,
                resets_at=resets_at,
                resets_in_seconds=resets_in,
            )
        )
    return out


def _parse_spend(payload: dict) -> Spend | None:
    """``spend`` if present, else the legacy ``extra_usage``, else ``None``."""
    spend = _dict(payload.get("spend"))
    if spend:
        used = _dict(spend.get("used"))
        limit = _dict(spend.get("limit"))
        currency = (
            _str(used.get("currency"))
            or _str(limit.get("currency"))
            or _str(spend.get("currency"), "USD")
        )
        exponent = _first_exponent(used, limit, spend)
        return Spend(
            enabled=spend.get("enabled") is True,
            used_minor=_int(used.get("amount_minor")),
            limit_minor=_int(limit.get("amount_minor")),
            currency=currency,
            exponent=exponent,
            percent=_unclamped_percent(spend.get("percent")),
        )

    extra = _dict(payload.get("extra_usage"))
    if extra:
        return Spend(
            enabled=extra.get("is_enabled") is True,
            used_minor=_int(extra.get("used_credits")),
            limit_minor=_int(extra.get("monthly_limit")),
            currency=_str(extra.get("currency"), "USD"),
            exponent=_exponent(extra.get("decimal_places")),
            percent=_unclamped_percent(extra.get("utilization")),
        )

    return None


def _exponent(value: Any) -> int:
    """Minor units per major, as a power of ten. Defaults to 2 when absent.

    Bounded: a nonsense exponent would become a division by an absurd power of
    ten in a renderer. The removed ``!usage`` divided by a hardcoded 100, which
    is wrong for any currency that is not two-decimal.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 2
    return value if 0 <= value <= 6 else 2


def _first_exponent(*blocks: dict) -> int:
    for block in blocks:
        value = block.get("exponent")
        if isinstance(value, int) and not isinstance(value, bool):
            return _exponent(value)
    return 2


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect, because following one would leak the token.

    ``urllib``'s default ``HTTPRedirectHandler.redirect_request`` copies every
    header except ``content-length`` and ``content-type`` onto the new request,
    so a 30x from ``api.anthropic.com`` to anywhere else would re-send
    ``Authorization: Bearer <token>`` to that host. httpx and requests both drop
    the header on a cross-host redirect; the stdlib does not, and this module
    holds a subscription credential. The endpoint does not redirect, so refusing
    outright costs nothing and needs no host comparison to get right.

    Returning ``None`` makes ``urllib`` raise the 30x as an ``HTTPError``, which
    the caller turns back into ``(status, body)`` — the redirect is reported as
    the status it was, not as a success.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that follows no redirects and consults no proxy env vars.

    ``ProxyHandler({})`` is passed explicitly so the credentialed request cannot
    be routed through whatever ``http_proxy`` happens to be set in the daemon's
    environment.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


# `parse_retry_after` and `_retry_after_from` used to live here, and this
# module paid for their hardening. They moved to `istota.retry_after` when the
# feeds poller became a second caller (ISSUE-347) — a stdlib-only leaf, so a
# caller with a light import graph need not pull in this module's. Re-exported
# under both names because this module's tests and its own call sites address
# them here.
parse_retry_after = _parse_retry_after
_retry_after_from = _retry_after_from_headers


def _urllib_transport(
    url: str, headers: dict[str, str], timeout: float
) -> tuple[int, bytes, Mapping[str, str]]:
    """The default ``Transport``: one GET, stdlib only.

    An ``HTTPError`` is a response, not a transport failure, so it is turned back
    into ``(status, body, headers)`` — that is what routes a 401 or a 403 into
    the status branch instead of the exception branch, what turns a refused
    redirect into a reported 30x, and what carries a 429's ``Retry-After`` back
    to the caller. The headers matter most on exactly the branch that used to
    look like an error and nothing else.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _build_opener().open(request, timeout=timeout) as response:
            return (
                int(response.status),
                response.read(_MAX_BODY_BYTES),
                dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001 — the status is what we needed
            body = b""
        try:
            response_headers = dict(exc.headers or {})
        except Exception:  # noqa: BLE001 — same: the status is the load-bearing part
            response_headers = {}
        return int(exc.code), body, response_headers


def fetch_snapshot(
    token: str,
    *,
    timeout: float,
    now_ts: float,
    transport: Transport | None = None,
    token_source: str = "",
) -> UsageSnapshot:
    """One GET against the usage endpoint. Returns; never raises.

    The token goes into the ``Authorization`` header and nowhere else — not into
    the URL, not into a log line, and not into the returned snapshot. An HTTP
    error body is read for the DEBUG log only and never echoed into ``error``,
    which is built from a fixed set of literals plus a status code.

    ``token_source`` is stamped onto every snapshot this returns, the failures
    included. It is the resolver's branch *name*; passing anything derived from
    the credential itself would defeat the paragraph above.
    """
    url = f"{BASE_URL}{USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    send = transport or _urllib_transport

    try:
        status, body, response_headers = send(url, headers, timeout)
    except Exception as exc:  # noqa: BLE001 — a diagnostic fetch never propagates
        logger.debug("subscription usage fetch failed", exc_info=True)
        return _failed(
            f"could not reach api.anthropic.com ({_reason(exc, token)})", token_source
        )

    if status != 200:
        retry_after = _retry_after_from(response_headers, now_ts=now_ts)
        logger.debug(
            "subscription usage endpoint returned HTTP %s (retry-after %s): %s",
            status,
            retry_after,
            _snippet(body),
        )
        return _failed(
            f"the usage endpoint returned HTTP {status}", token_source, retry_after
        )

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        windows, spend = parse_usage(payload, now_ts=now_ts)
    except Exception:  # noqa: BLE001 — a proxy or a captive portal, most likely
        logger.debug("subscription usage response was not usable", exc_info=True)
        return _failed("the usage endpoint returned a non-JSON response", token_source)

    if not windows:
        return UsageSnapshot(
            fetched_at=now_ts,
            windows=(),
            spend=spend,
            source="fetch",
            token_source=token_source,
            error=NO_WINDOWS_ERROR,
        )
    return UsageSnapshot(
        fetched_at=now_ts,
        windows=windows,
        spend=spend,
        source="fetch",
        token_source=token_source,
    )


def _reason(exc: BaseException, token: str) -> str:
    """A short cause for a transport failure, with no room for the credential.

    Deliberately the exception *class* plus its module, not ``str(exc)``. A
    message is attacker- and stdlib-influenced text: ``http.client`` embeds a
    rejected header value in its ``ValueError`` as a repr, which a substring
    redaction would miss. ``_clean_token`` already refuses a credential that
    could end up there, and this is the second half of the same guarantee —
    ``URLError`` or ``TimeoutError`` is all an operator needs to distinguish a
    firewall from a hang. ``_redact`` stays as a backstop over the class name in
    case a caller ever names a class after something it should not.
    """
    return _redact(type(exc).__name__, token)


def _failed(
    message: str, token_source: str = "", retry_after: float | None = None
) -> UsageSnapshot:
    """A snapshot with no data. ``fetched_at`` is 0.0 on every such branch."""
    return UsageSnapshot(
        fetched_at=0.0,
        source="none",
        token_source=token_source,
        error=message,
        retry_after=retry_after,
    )


def _snippet(body: bytes) -> str:
    try:
        return body.decode("utf-8", "replace")[:_ERROR_BODY_CHARS]
    except Exception:  # noqa: BLE001 — a debug log line is never worth an exception
        return ""


# ---------------------------------------------------------------------------
# disk cache
# ---------------------------------------------------------------------------


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / _CACHE_FILENAME


def _snapshot_to_json(snapshot: UsageSnapshot) -> dict:
    return {
        "version": 1,
        "fetched_at": snapshot.fetched_at,
        # The branch name, not the credential. A reader of this file learns which
        # of the three sources the deployment is running on and nothing else.
        "token_source": snapshot.token_source,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "percent": w.percent,
                "resets_at": w.resets_at,
                "resets_in_seconds": w.resets_in_seconds,
                "severity": w.severity,
                "is_active": w.is_active,
            }
            for w in snapshot.windows
        ],
        "spend": None
        if snapshot.spend is None
        else {
            "enabled": snapshot.spend.enabled,
            "used_minor": snapshot.spend.used_minor,
            "limit_minor": snapshot.spend.limit_minor,
            "currency": snapshot.spend.currency,
            "exponent": snapshot.spend.exponent,
            "percent": snapshot.spend.percent,
        },
    }


def _windows_from_json(raw: Any, now_ts: float | None) -> tuple[UsageWindow, ...]:
    """Rebuild the cached windows, recomputing the countdown against ``now_ts``.

    ``resets_in_seconds`` is derived, not data: it is a delta from the moment of
    the fetch. Restoring it verbatim would render "resets in 58 minutes" six
    hours after the window actually reset, which is exactly the reading the
    stale-cache path exists to serve. It is recomputed from ``resets_at``
    whenever a clock is supplied, and the stored value is used only when there is
    no clock or no parseable ``resets_at``.
    """
    if not isinstance(raw, list):
        return ()
    out: list[UsageWindow] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _str(entry.get("key"))
        label = _str(entry.get("label"))
        percent = _percent(entry.get("percent"))
        if not key or not label or percent is None:
            continue
        stored = entry.get("resets_in_seconds")
        is_active = entry.get("is_active")
        if now_ts is None:
            resets_at, _ = _normalize_resets_at(entry.get("resets_at"))
            resets_in = None if isinstance(stored, bool) else _optional_int(stored)
        else:
            resets_at, resets_in = _window_times(entry.get("resets_at"), now_ts)
            if resets_in is None and not isinstance(stored, bool):
                resets_in = _optional_int(stored)
        out.append(
            UsageWindow(
                key=key,
                label=label,
                percent=percent,
                resets_at=resets_at,
                resets_in_seconds=resets_in,
                severity=_str(entry.get("severity")),
                is_active=is_active if isinstance(is_active, bool) else None,
            )
        )
    return tuple(out)


def _token_source(value: Any) -> str:
    """A stored branch name, or ``""`` for anything not in ``TOKEN_SOURCES``."""
    name = _str(value).strip()
    return name if name in TOKEN_SOURCES else ""


def _optional_int(value: Any) -> int | None:
    """``_int`` that distinguishes "absent" from "zero"."""
    if _number(value) is None:
        return None
    return max(0, _int(value))


def _spend_from_json(raw: Any) -> Spend | None:
    if not isinstance(raw, dict):
        return None
    return Spend(
        enabled=raw.get("enabled") is True,
        used_minor=_int(raw.get("used_minor")),
        limit_minor=_int(raw.get("limit_minor")),
        currency=_str(raw.get("currency"), "USD"),
        exponent=_exponent(raw.get("exponent")),
        percent=_unclamped_percent(raw.get("percent")),
    )


def _read_raw(path: Path) -> dict | None:
    """A JSON object off disk, or ``None``. Shared by the cache and the timer.

    The read is capped. Both files are a few kB when this module writes them, so
    anything past the cap is corrupt by definition — but nothing outside this
    module guarantees that, and an uncapped ``read_text`` on the daemon's boot
    path is a memory cost set by whatever happens to be in a shared data dir.
    The cap is deliberately generous: it is a guard, not a size expectation, and
    it is the same reasoning as ``_MAX_BODY_BYTES`` on the wire.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(_MAX_FILE_CHARS + 1)
        if len(text) > _MAX_FILE_CHARS:
            logger.debug("subscription usage file is implausibly large: %s", path)
            return None
        raw = json.loads(text)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — corrupt / truncated / a directory / perms
        logger.debug("subscription usage read failed: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _snapshot_from_raw(raw: dict, now_ts: float | None) -> UsageSnapshot | None:
    """A cached snapshot, or ``None`` for anything unusable. Never raises."""
    try:
        fetched_at = _number(raw.get("fetched_at"))
        if fetched_at is None:
            return None
        windows = _windows_from_json(raw.get("windows"), now_ts)
        if not windows:
            return None
        return UsageSnapshot(
            fetched_at=fetched_at,
            windows=windows,
            spend=_spend_from_json(raw.get("spend")),
            source="cache",
            # Absent in a file written before the field existed, and anything
            # that is not one of the three branch names reads as unknown.
            token_source=_token_source(raw.get("token_source")),
        )
    except Exception:  # noqa: BLE001 — a cache is never repaired, only ignored
        logger.debug("subscription usage cache entry unusable", exc_info=True)
        return None


def read_cache(path: Path, ttl_seconds: float, *, now_ts: float) -> UsageSnapshot | None:
    """The cached reading if it exists and is within TTL, else ``None``.

    A negative age — the clock moved backwards, or the file claims to be from
    next week — counts as stale rather than as fresh forever. A TTL of 0 or
    below expires everything, which is the direction an operator setting it to
    zero means; the loader floors the configured value at 1 so nobody reaches
    that by accident. Use ``read_cache_any_age`` for the stale-fallback path.
    """
    raw = _read_raw(path)
    if raw is None:
        return None
    snapshot = _snapshot_from_raw(raw, now_ts)
    if snapshot is None:
        return None
    age = now_ts - snapshot.fetched_at
    if age < 0 or age > ttl_seconds:
        return None
    return snapshot


def read_cache_any_age(path: Path, *, now_ts: float | None = None) -> UsageSnapshot | None:
    """The cached reading regardless of TTL. ``None`` if absent or unusable.

    Pass ``now_ts`` to have each window's countdown recomputed against it. This
    is the stale-fallback path, where the stored countdown is by definition out
    of date, so a caller that has a clock should always pass it.
    """
    raw = _read_raw(path)
    if raw is None:
        return None
    return _snapshot_from_raw(raw, now_ts)


def write_cache(path: Path, snapshot: UsageSnapshot) -> None:
    """Persist a successful reading. Best-effort; never raises.

    A snapshot carrying an ``error`` is never written — the cache holds
    successful readings only. A *failure* is recorded separately, by
    :func:`write_failure`, which is what bounds the retry.
    """
    if snapshot.error or not snapshot.windows:
        return
    try:
        payload = _snapshot_to_json(snapshot)
    except Exception:  # noqa: BLE001 — a snapshot this function cannot serialize
        # Inside the guard rather than beside it: `_write_json` cannot protect an
        # argument evaluated at its call site, and "never raises" has to hold for
        # a direct caller too, not only for the one behind `get_snapshot`'s
        # blanket except.
        logger.debug("subscription usage snapshot could not be serialized", exc_info=True)
        return
    _write_json(path, payload)


def _write_json(path: Path, payload: dict) -> None:
    """Publish a JSON file atomically at ``0600``. Best-effort; never raises.

    Two processes read and write these files — ``istota-scheduler`` and
    ``istota-web`` are separate units — so the write goes to a staging file
    unique to the **writer**, not to a fixed name, and is ``os.replace``d into
    place. That uniqueness is what makes the claim true: ``os.replace`` is
    atomic with respect to the rename, not with respect to two writers opening
    one fixed temp name ``O_TRUNC`` and writing at independent offsets, which
    publishes a torn file. It has to be unique per *call* rather than per
    process, because the second writer is not always another process: the admin
    doctor endpoint runs its shallow phase through ``asyncio.to_thread`` with no
    semaphore, so two dashboards refreshing at once are two threads of one
    process. ``atomic_write`` mints the name with ``mkstemp``, which covers
    both. Racing the *fetch* is still fine — one redundant request, last writer
    wins, both readings are equally true — so there is no lock.

    ``0600``: neither file is a credential, but the reading is account data and
    the timer names which credential was refused, and the data dir is shared.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(payload), mode=0o600, fsync=True)
    except Exception:  # noqa: BLE001 — writing either file is best-effort
        logger.debug("subscription usage write failed: %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# the failure timer
# ---------------------------------------------------------------------------
#
# A failed reading is never written to the cache above, so before this existed
# nothing bounded the retry: with no prior success every caller re-fetched. The
# admin dashboard polls every 60 seconds, which on a rejected credential is
# roughly 1,440 live 403s a day against api.anthropic.com per open dashboard,
# and on a missing one that many `security` subprocesses on macOS. Recording the
# failure separately makes the TTL the retry interval, which is what the
# rejected-credential case was always specified to do.
#
# Everything read back out of this file is input — it sits in a shared data dir
# and both callers interpolate its `error` into text a person reads — so it gets
# the same treatment as the reading cache: an allowlisted `token_source`, a
# flattened and capped message, and *no backoff* for anything unusable. Failing
# open costs one extra request; failing closed would suppress the reading
# indefinitely on a deployment where nothing is wrong.
#
# **Only a failure the endpoint itself produced goes in the file.** A 403, a 500
# and an unreachable host are facts about the deployment, and every process that
# shares the data dir is entitled to reuse them. "No credential here" is not: it
# is a fact about the environment and home directory of *the calling process*,
# which is what `resolve_token` reads. `istota-scheduler` and `istota-web` take
# the token from a systemd `EnvironmentFile`; an operator's `istota doctor` in a
# shell usually does not, and on macOS a background agent and a login session do
# not see the same keychain. Writing that process's answer into the shared file
# would have one read-only diagnostic run tell the dashboard for a full TTL that
# there is no credential while the daemon is happily using one. So the
# no-credential branch is rate-limited by a **process-local** record instead —
# which is where its cost is anyway. The expense the spec names is the macOS
# `security` subprocess, and a process bounds its own subprocesses perfectly
# well without publishing its environment as a deployment fact.

# Keyed by data dir, because that is what a "deployment" is to this module and a
# process could be pointed at two configs. Expired entries are pruned on write,
# so it stays the size of the number of configs in flight (one, in the daemon).
_NO_CREDENTIAL_AT: dict[str, float] = {}
_NO_CREDENTIAL_LOCK = threading.Lock()


def failure_path(data_dir: Path) -> Path:
    return Path(data_dir) / _FAILURE_FILENAME


def _within(age: float, ttl_seconds: float) -> bool:
    """True while a recorded moment is still inside its window.

    Written as a range rather than as ``age < ttl_seconds`` so a nonsense clock
    fails *open*: a ``nan`` age satisfies neither comparison, so it reads as "not
    within", which is no backoff — the direction every unusable timer here takes.
    A negative age is a clock that moved backwards, or a hand-edited file, and it
    must not produce a timer nobody can wait out.

    The upper bound is exclusive where ``read_cache``'s is inclusive, and the two
    mean opposite things: that one bounds how long a reading stays *fresh*, this
    one bounds how long an attempt stays *suppressed*. Both err toward serving
    data — a reading exactly ``ttl`` old is still returned, and a failure exactly
    ``ttl`` old is retried — which also makes the retry interval exactly the TTL
    rather than a tick more.
    """
    return 0 <= age < ttl_seconds


def _no_credential_recently(key: str, now_ts: float, ttl_seconds: float) -> bool:
    with _NO_CREDENTIAL_LOCK:
        recorded = _NO_CREDENTIAL_AT.get(key)
    return recorded is not None and _within(now_ts - recorded, ttl_seconds)


def _record_no_credential(key: str, now_ts: float, ttl_seconds: float) -> None:
    with _NO_CREDENTIAL_LOCK:
        expired = [
            k for k, ts in _NO_CREDENTIAL_AT.items() if not _within(now_ts - ts, ttl_seconds)
        ]
        for k in expired:
            del _NO_CREDENTIAL_AT[k]
        _NO_CREDENTIAL_AT[key] = now_ts


def _clean_message(value: Any) -> str:
    """An error string off disk, safe to print on one line. May be empty.

    Non-printables become spaces rather than being dropped, so a newline cannot
    splice two fragments into a plausible-looking third; runs collapse; the
    result is capped. The strings this module writes are built from literals and
    a status code and need none of this — the file is what needs it.
    """
    # Truncate first, then flatten. The other order rebuilds the whole string
    # character by character before throwing all but 300 of them away, which
    # turns an implausibly large file into an implausibly large allocation on
    # the daemon's boot path. The slack is for the whitespace runs that collapse
    # below; anything past it could not have reached the cap anyway.
    text = _str(value)[: MAX_ERROR_CHARS * 4]
    if not text:
        return ""
    flattened = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", flattened).strip()[:MAX_ERROR_CHARS]


def _backoff_seconds(raw: Mapping[str, Any], ttl_seconds: float) -> float:
    """How long a recorded failure suppresses retries.

    The TTL is the floor, not the answer. When the endpoint stated a
    ``Retry-After`` the longer of the two wins: retrying inside a window the
    server named is what kept a deployment permanently rate-limited, because it
    asked for ~39 minutes while the TTL came back every 5 and each early knock
    was another request against the limit.

    The stated value is capped at :data:`MAX_RETRY_AFTER_SECONDS`, and a value
    that is absent, unusable or *shorter* than the TTL leaves the TTL in place —
    a server asking us to come back sooner than we intended to is not a reason
    to poll harder.
    """
    floor = ttl_seconds if _is_finite(ttl_seconds) else 0.0
    stated = _number(raw.get("retry_after_seconds"))
    if stated is None or stated <= 0:
        return floor
    return max(floor, min(stated, float(MAX_RETRY_AFTER_SECONDS)))


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def read_failure(path: Path, ttl_seconds: float, *, now_ts: float) -> UsageSnapshot | None:
    """The recorded failure if it is still inside the backoff, else ``None``.

    ``None`` means "no backoff": the file is absent, expired, corrupt, or claims
    a time in the future. The returned snapshot is indistinguishable from the one
    the suppressed attempt would have produced, which is the point: a caller
    cannot tell a bounded retry from a real one, and none of them has to. That
    is also why the no-credential branch is not in this file — its live answer
    takes no stale fallback, so replaying it from here would produce a pairing
    (no credential, beside real windows) the live path cannot.
    """
    raw = _read_raw(path)
    if raw is None:
        return None
    try:
        failed_at = _number(raw.get("failed_at"))
        if failed_at is None:
            return None
        if not _within(now_ts - failed_at, _backoff_seconds(raw, ttl_seconds)):
            return None
        error = _clean_message(raw.get("error"))
        if not error:
            # A timer that cannot name its own failure is unusable, not
            # authoritative: an empty `error` would rebuild as a snapshot that
            # reads as `ok` to every caller while carrying nothing to render,
            # which is the one shape this module promises never to emit.
            return None
        return _failed(error, _token_source(raw.get("token_source")))
    except Exception:  # noqa: BLE001 — a timer is never repaired, only ignored
        logger.debug("subscription usage failure timer unusable", exc_info=True)
        return None


def write_failure(
    path: Path,
    *,
    now_ts: float,
    error: str,
    token_source: str = "",
    retry_after: float | None = None,
) -> None:
    """Record a failed reading so the next TTL's worth of callers reuse it.

    ``error`` is the reason to hand the suppressed callers; a blank one — or one
    that is not a usable string at all — records nothing, symmetrically with
    ``write_cache`` refusing a failed snapshot. It goes through the same cleaner
    the reader uses, so this stays total on any argument: "nothing here raises"
    covers a public name called directly, not only the path behind
    ``get_snapshot``'s guard.

    ``token_source`` is the resolver's branch *name*, never the credential — it
    is what lets the doctor say which one was refused without re-resolving — and
    it is validated on the way in as well as on the way out.
    """
    message = _clean_message(error)
    if not message:
        return
    record: dict[str, Any] = {
        "version": 1,
        "failed_at": now_ts,
        "error": message,
        "token_source": _token_source(token_source),
    }
    # Recorded only when the endpoint actually stated one. An absent key reads
    # back as "no hint" through `_backoff_seconds`, which is also what a file
    # written before this field existed looks like — so an in-place upgrade
    # needs no version bump and no migration.
    stated = _number(retry_after)
    if stated is not None and stated > 0:
        record["retry_after_seconds"] = min(stated, float(MAX_RETRY_AFTER_SECONDS))
    _write_json(path, record)


def clear_failure(path: Path) -> None:
    """Drop the timer. Best-effort, idempotent, never raises.

    Called on every success, so recovery is never delayed by a record of a
    failure that is over.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — a leftover timer costs one TTL, not a boot
        logger.debug("subscription usage failure timer clear failed: %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# get_snapshot — the only function the callers use
# ---------------------------------------------------------------------------


def _settings(config: object) -> tuple[bool, float, float]:
    """``(enabled, ttl_seconds, timeout_seconds)`` from ``brain.claude_code``.

    Read defensively: this module must not import ``Config`` (leaf), and a
    deployment whose config predates ``[brain.claude_code]`` gets the shipping
    defaults rather than an ``AttributeError`` on the daemon's boot path. The
    loader corrects these fields; the guard here is a second line so a value
    that reached the dataclass some other way cannot turn the TTL into a fetch
    on every dashboard poll or the timeout into an unbounded socket read.

    The two do not agree on *what* to substitute, deliberately: the loader
    floors a below-1 value at 1 because the operator asked for something small
    and 1 is the smallest honest answer, while ``_positive`` here substitutes
    the shipping default, because a value arriving past the loader has no
    intent behind it worth preserving. Both refuse the bad value; only the
    loader is in a position to log about it.
    """
    block = getattr(getattr(config, "brain", None), "claude_code", None)
    enabled = getattr(block, "subscription_usage", DEFAULT_SUBSCRIPTION_USAGE)
    ttl = _positive(
        getattr(block, "subscription_usage_cache_ttl_seconds", None), DEFAULT_CACHE_TTL_SECONDS
    )
    timeout = _positive(
        getattr(block, "subscription_usage_timeout_seconds", None), DEFAULT_TIMEOUT_SECONDS
    )
    return bool(enabled), ttl, timeout


def _positive(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    as_float = float(value)
    if not math.isfinite(as_float) or as_float < 1:
        return default
    return as_float


def _data_dir(config: object) -> Path | None:
    db_path = getattr(config, "db_path", None)
    if not db_path:
        return None
    try:
        return Path(db_path).parent
    except Exception:  # noqa: BLE001 — a nonsense db_path only costs the cache
        return None


def get_snapshot(
    config: "Config",
    *,
    now_ts: float,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> UsageSnapshot:
    """The whole policy, in the one function the callers use.

    1. Disabled by config → ``source="none"``, ``error="disabled by config"``.
    2. Fresh cache within TTL → return it, ``source="cache"``. No request.
    3. A failure recorded less than one TTL ago → return that failure, without
       resolving a credential and without a request. The TTL is the retry
       interval, which is what the rejected-credential case has always promised.
       Two records, deliberately: what the endpoint said is deployment-wide and
       lives in a file beside the cache, while "this process found no credential"
       is process-local and never written down — see the failure-timer section.
    4. No credential → an error snapshot. Still **not** written to the reading
       cache — that holds readings — and not to the shared file either, but it is
       rate-limited: "cheap to re-check" is true of an environment variable and
       false of a ``security`` subprocess, and one TTL of delay in noticing a
       credential that appeared five minutes ago is the trade that buys it. No
       stale fallback on this branch, as before — the fetch that would have
       earned one never happened.
    5. Fetch. On success, write the cache, clear the record and return
       ``source="fetch"``. Clearing immediately is what keeps recovery from
       waiting on a record of a failure that is over.
    6. On failure, record it, then fall back to a cache of any age:
       ``source="stale-cache"`` with the fetch error preserved, so the caller can
       decide what an old-but-real reading is worth. No cache to fall back on →
       the fetch failure. A suppressed call at step 3 takes this same fallback,
       so an old real reading outranks a fresh failure either way.

    ``env`` and ``home`` default to this process's own and are parameters only so
    a test never reads the real ones.

    Never raises, and the outer guard is deliberate: this is the daemon's boot
    path, and an unforeseen exception out of any helper must read as a missing
    number rather than a failed boot. The helpers are total in their own right —
    ``parse_usage`` and the cache readers are tested directly for that — so the
    guard is a backstop, not the mechanism.
    """
    try:
        return _get_snapshot(config, now_ts, transport, env, home)
    except Exception as exc:  # noqa: BLE001 — a diagnostic never fails a boot
        logger.debug("subscription usage snapshot failed unexpectedly", exc_info=True)
        return _failed(f"subscription usage could not be read ({type(exc).__name__})")


def _get_snapshot(
    config: object,
    now_ts: float,
    transport: Transport | None,
    env: Mapping[str, str] | None,
    home: Path | None,
) -> UsageSnapshot:
    enabled, ttl, timeout = _settings(config)
    if not enabled:
        return _failed(DISABLED_ERROR)

    data_dir = _data_dir(config)
    path = cache_path(data_dir) if data_dir is not None else None
    timer = failure_path(data_dir) if data_dir is not None else None
    # The same identity the two files have, for the process-local record. A
    # config with no `db_path` has no data dir, so it gets no cache, no timer
    # and no rate limit — it fetches every time, exactly as it did before.
    local_key = str(data_dir) if data_dir is not None else None

    if path is not None:
        cached = read_cache(path, ttl, now_ts=now_ts)
        if cached is not None:
            return cached

    # Both checks sit after the cache and before the resolver, deliberately on
    # both counts: a usable reading is never withheld by a record of a failure,
    # and the keychain subprocess this exists to bound is inside `resolve_token`.
    if local_key is not None and _no_credential_recently(local_key, now_ts, ttl):
        # No stale fallback, because the live branch below takes none either.
        return _failed(NO_CREDENTIAL_ERROR)

    if timer is not None:
        recorded = read_failure(timer, ttl, now_ts=now_ts)
        if recorded is not None:
            return _with_stale_fallback(recorded, path, now_ts)

    resolved = resolve_token(
        os.environ if env is None else env,
        _home_dir() if home is None else home,
    )
    if resolved is None:
        if local_key is not None:
            _record_no_credential(local_key, now_ts, ttl)
        return _failed(NO_CREDENTIAL_ERROR)

    # No record to clear here: reaching the resolver at all means the check above
    # found none active, so the only entry that could still be in the map for
    # this data dir is an expired one, which the next write prunes.
    token, token_source = resolved
    snapshot = fetch_snapshot(
        token, timeout=timeout, now_ts=now_ts, transport=transport, token_source=token_source
    )
    if not snapshot.error:
        if path is not None:
            write_cache(path, snapshot)
        if timer is not None:
            clear_failure(timer)
        return snapshot

    if timer is not None:
        write_failure(
            timer,
            now_ts=now_ts,
            error=snapshot.error,
            token_source=token_source,
            retry_after=snapshot.retry_after,
        )
    return _with_stale_fallback(snapshot, path, now_ts)


def _with_stale_fallback(
    failure: UsageSnapshot, cache: Path | None, now_ts: float
) -> UsageSnapshot:
    """An old real reading if there is one, else the failure itself.

    Reached from both failure paths — the fetch that just failed and the one
    suppressed by the timer — so a backoff never costs a caller the stale
    reading it would otherwise have been served.
    """
    stale = read_cache_any_age(cache, now_ts=now_ts) if cache is not None else None
    if stale is None:
        return failure
    # The windows are the cache's; the ``token_source`` is the one that was
    # refused, which is what makes the pair legible — the reading is old
    # precisely because that credential stopped working.
    return replace(
        stale,
        source="stale-cache",
        token_source=failure.token_source,
        error=failure.error,
    )


# ---------------------------------------------------------------------------
# Reset lookup (ISSUE-374)
# ---------------------------------------------------------------------------


def soonest_reset_seconds(snapshot: "UsageSnapshot | None") -> int | None:
    """Seconds until the soonest *future* reset among a snapshot's windows.

    The availability breaker's question is "when could the primary plausibly
    work again", and the earliest reset is the earliest moment anything changes.
    Which window actually produced the limit is not knowable from a
    ``stop_reason``, and picking the earliest is the side of that ignorance
    worth being on: too short costs one failed primary attempt, one reopened
    breaker and the operator alert that reopening arms, while too long runs
    every task in the remainder of the window on a different model. That extra
    alert is the accepted cost, and it is bounded — a reopen that finds the
    quota back succeeds and alerts nobody, and one that does not re-arms against
    the *next* reset, which is a full window away.

    ``percent`` is deliberately not read, though a window at 100% would name the
    culprit better than the arithmetically earliest one does. In the shipped
    payload the five-hour session window is always the earliest, so the two
    rules differ only when an exhausted weekly window resets sooner than the
    ceiling — where they collapse to the same answer anyway — and a threshold on
    a percentage sampled minutes before the limit landed is a tunable with no
    good value.

    A window already at or past its reset is skipped rather than reported as
    zero — it has reset, so it explains nothing about a limit being hit now, and
    a zero would collapse the breaker onto its floor once a minute.

    The type guards here are belt-and-braces, not the boundary: every route into
    ``resets_in_seconds`` already floors at 0, and the containment for a value
    that is merely *wrong* is the caller's clamp — never past the configured
    cooldown, never below its floor — not validation here.

    Returns ``None`` when there is nothing to go on, which is the caller's
    signal to keep whatever duration it had.
    """
    windows = getattr(snapshot, "windows", ()) or ()
    soonest: int | None = None
    for window in windows:
        seconds = getattr(window, "resets_in_seconds", None)
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
            continue
        if soonest is None or seconds < soonest:
            soonest = seconds
    return soonest


def cached_reset_seconds(config: object, *, now_ts: float) -> int | None:
    """:func:`soonest_reset_seconds` over the disk cache alone. Never fetches.

    Deliberately not :func:`get_snapshot`. The caller is the brain-availability
    breaker, on the path a task takes when the primary just reported a usage
    limit, and that path must not resolve a credential (a ``security``
    subprocess on macOS), must not open a socket, and must not depend on a
    fetch having succeeded. It need not: ``resets_at`` is absolute, and
    :func:`read_cache_any_age` recomputes the countdown against ``now_ts``, so a
    reading of any age answers this question as well as a fresh one — with one
    exception, which the clamp contains. ``_windows_from_json`` recomputes only
    where ``resets_at`` parses, and falls back to the stored countdown where it
    does not, so an entry the endpoint wrote in a shape this module cannot read
    can hand back an arbitrarily old number. Nothing on the deployment reads
    only this — the doctor check, the admin card and ``!usage`` keep the cache
    warm.

    Honours ``subscription_usage``: an operator who turned the endpoint off gets
    ``None`` rather than an answer off a cache nothing is maintaining.

    Never raises.
    """
    try:
        enabled, _ttl, _timeout = _settings(config)
        if not enabled:
            return None
        data_dir = _data_dir(config)
        if data_dir is None:
            return None
        return soonest_reset_seconds(read_cache_any_age(cache_path(data_dir), now_ts=now_ts))
    except Exception:  # noqa: BLE001 — a missing hint costs the hint, nothing else
        logger.debug("subscription usage reset lookup failed", exc_info=True)
        return None


def _home_dir() -> Path | None:
    """This process's home directory, or ``None`` if it has none.

    ``Path.home()`` raises ``RuntimeError`` when ``HOME`` is unset *and* the
    running uid has no passwd entry — the ``docker run --user 1000:1000`` shape
    against an image that never created that uid, and systemd units routinely do
    not set ``HOME``. That is a deployment with no credential file to read, not a
    reason to take the daemon's boot down.
    """
    try:
        return Path.home()
    except Exception:  # noqa: BLE001 — no home is a fact, not a failure
        return None
