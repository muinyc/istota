"""User profile store (Phase 6 of the Docker onboarding spec).

Per-user profile fields (display_name, email_addresses, timezone, log_channel,
alerts_channel, worker overrides, disabled_skills, trusted_email_senders)
live in the ``user_profiles`` table.

Resolution order at config-load time:
    1. ``user_profiles`` table     (web-UI / Docker-seeded / ``istota user ensure``)
    2. ``[users.X]`` in main config (single-file ansible / docker entrypoint)

A user is "first seen" by the system via:
- Ansible runs ``istota user ensure`` against the DB.
- Docker entrypoint emits a ``[users.X]`` block; the scheduler startup
  auto-seeds an empty profile row from the bare key.
- Web login: the OAuth2 callback calls ``ensure_profile`` to create the
  row from the NC username + display_name.

The DB row, when present, wins.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import avatars, sqlite_util

logger = logging.getLogger(__name__)

# How an external-origin turn's body renders in web chat. Lives here rather than
# inline at each consumer because two surfaces validate against it — the web
# profile PUT (`web_app._PROFILE_EDITABLE_FIELDS`) and `istota user ensure`,
# which validates twice, at the parser and in the handler — and a hand-copied
# list is the one that drifts. The outbound counterpart is
# `outbound_policy.VALID_POLICIES`, which stays with the predicate that reads it.
#
# The column *default* is a separate concern and stays a literal at its read
# sites: it names one member rather than the set, and coupling it here would
# make reordering the tuple change what an unset row means.
EXTERNAL_TURN_DISPLAY_VALUES = ("full", "collapsed", "hidden")


@dataclass
class UserProfile:
    """Profile fields that live in the ``user_profiles`` table.

    Mirrors the subset of ``UserConfig`` that Phase 6 moves out of TOML.
    Briefings and resources stay in TOML and are not represented here.
    """

    user_id: str
    display_name: str = ""
    email_addresses: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    log_channel: str = ""
    alerts_channel: str = ""
    max_foreground_workers: int = 0
    max_background_workers: int = 0
    disabled_skills: list[str] = field(default_factory=list)
    trusted_email_senders: list[str] = field(default_factory=list)
    quiet_email_senders: list[str] = field(default_factory=list)
    disabled_modules: list[str] = field(default_factory=list)
    # Purpose-keyed delivery routing: {purpose -> output_target descriptor}.
    routing: dict[str, str] = field(default_factory=dict)
    # Default delivery descriptor when no per-purpose route applies.
    default_destination: str = "talk"
    # Email-reply mirror policy: origin+thread | origin | thread.
    email_reply_routing: str = "origin+thread"
    # Outbound email approval: '' (unset — follow the operator floor) | off |
    # untrusted | all. Unset is a real value here, not a missing one.
    outbound_approval: str = ""
    # External-origin turn body in web chat: full | collapsed | hidden.
    external_turn_display: str = "collapsed"
    # Seed the shared [[default_briefings]] set into this user (default on).
    default_briefings: bool = True
    # Deliver briefing email as multipart/alternative (HTML + plain) — default on.
    briefing_email_html: bool = True
    # Follow the user's GPS timezone on travel (ISSUE-096). Default OFF: this
    # rewrites a setting the user chose, so it is opted into, not inferred.
    timezone_follow_location: bool = False
    # Per-user Google Workspace scope selection: {service -> off|readonly|full},
    # clamped at connect time to the operator's [google_workspace] scopes
    # ceiling. Empty is "unset" and resolves to the whole ceiling, which is
    # what every user had before the picker existed. See istota.google_scopes.
    google_scopes: dict[str, str] = field(default_factory=dict)


_PROFILE_COLUMNS = (
    "display_name", "email_addresses", "timezone",
    "log_channel", "alerts_channel",
    "max_foreground_workers", "max_background_workers",
    "disabled_skills", "trusted_email_senders", "quiet_email_senders",
    "disabled_modules",
    "routing", "default_destination", "email_reply_routing",
    "outbound_approval", "external_turn_display",
    "default_briefings", "briefing_email_html",
    "timezone_follow_location",
    "google_scopes",
)

# Columns whose value is a JSON-encoded dict (vs the JSON-list columns).
_DICT_COLUMNS = frozenset({"routing", "google_scopes"})
_LIST_COLUMNS = frozenset({
    "email_addresses", "disabled_skills", "trusted_email_senders",
    "quiet_email_senders", "disabled_modules",
})
# Columns stored as INTEGER 0/1 booleans, mapped to the default a missing
# value means. Per-column rather than one shared default: the briefing pair are
# opt-*outs* (absent = on) while timezone-following is an opt-*in* (absent =
# off), and a single default would silently switch one of them on.
_BOOL_COLUMN_DEFAULTS = {
    "default_briefings": True,
    "briefing_email_html": True,
    "timezone_follow_location": False,
}
_BOOL_COLUMNS = frozenset(_BOOL_COLUMN_DEFAULTS)


def _coerce_bool(value: object, default: bool = True) -> bool:
    """Coerce a stored/int/None value to bool; None → default."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with 30s timeout, matching db.get_db semantics."""
    with sqlite_util.open_db(
        db_path, busy_timeout_ms=None, foreign_keys=False, commit=True,
    ) as conn:
        yield conn


def _row_to_profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"] or "",
        email_addresses=_parse_json_list(row["email_addresses"]),
        timezone=row["timezone"] or "UTC",
        log_channel=row["log_channel"] or "",
        alerts_channel=row["alerts_channel"] or "",
        max_foreground_workers=int(row["max_foreground_workers"] or 0),
        max_background_workers=int(row["max_background_workers"] or 0),
        disabled_skills=_parse_json_list(row["disabled_skills"]),
        trusted_email_senders=_parse_json_list(row["trusted_email_senders"]),
        quiet_email_senders=_parse_json_list(row["quiet_email_senders"]),
        disabled_modules=_parse_json_list(row["disabled_modules"]),
        routing=_parse_json_dict(row["routing"]),
        default_destination=row["default_destination"] or "talk",
        email_reply_routing=row["email_reply_routing"] or "origin+thread",
        # No `or` default: '' is the unset value the floor resolution reads.
        outbound_approval=str(_row_get(row, "outbound_approval") or ""),
        external_turn_display=(
            _row_get(row, "external_turn_display") or "collapsed"
        ),
        default_briefings=_coerce_bool(_row_get(row, "default_briefings"), True),
        briefing_email_html=_coerce_bool(
            _row_get(row, "briefing_email_html"), True,
        ),
        timezone_follow_location=_coerce_bool(
            _row_get(row, "timezone_follow_location"), False,
        ),
        google_scopes=_parse_json_dict(_row_get(row, "google_scopes")),
    )


def _row_get(row: sqlite3.Row, key: str) -> object:
    """Read a column that may be absent on a not-yet-migrated row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _parse_json_dict(value: object) -> dict[str, str]:
    # ``object`` rather than ``str | None``: a not-yet-migrated row reaches
    # here through ``_row_get``, which can only promise "some column value".
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as e:
        logger.warning(
            "user_profiles: failed to decode JSON dict column (%s); falling back to {}",
            e,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "user_profiles: JSON dict column has non-dict type %s; falling back to {}",
            type(parsed).__name__,
        )
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as e:
        logger.warning(
            "user_profiles: failed to decode JSON list column (%s); falling back to []",
            e,
        )
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "user_profiles: JSON list column has non-list type %s; falling back to []",
            type(parsed).__name__,
        )
        return []
    return [str(x) for x in parsed]


def get_profile(
    db_path: Path,
    user_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> UserProfile | None:
    """Return the stored profile for ``user_id`` or None if no row exists.

    Pass ``conn`` to reuse an existing framework-DB connection (hot loops
    in the scheduler already hold one). Without it, opens a short-lived
    conn — keeps the API ergonomic for one-off callers.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_profile(row) if row else None
    with _connect(db_path) as cm_conn:
        row = cm_conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_profile(row) if row else None


def list_profiles(db_path: Path) -> dict[str, UserProfile]:
    """Return all stored profiles keyed by user_id."""
    out: dict[str, UserProfile] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM user_profiles ORDER BY user_id"
        ).fetchall()
    for row in rows:
        profile = _row_to_profile(row)
        out[profile.user_id] = profile
    return out


def ensure_profile(
    db_path: Path,
    user_id: str,
    *,
    display_name: str = "",
    timezone: str = "",
    seed_from: "object | None" = None,
) -> UserProfile:
    """Insert a row for ``user_id`` if missing, return the resulting profile.

    Existing rows are NOT overwritten — this is the "first-login auto-seed"
    path. Use ``update_profile`` to change values explicitly.

    ``display_name`` and ``timezone`` are accepted as initial values for new
    rows; they are ignored when the row already exists. Caller should pass
    derived defaults (e.g. NC display_name).

    If ``seed_from`` is a ``UserConfig`` (or any object with the matching
    attributes), its list fields and channel/worker scalars are copied
    into the new row so the DB row carries the full TOML payload from the
    moment it exists. This eliminates the "DB has display_name but TOML
    has email_addresses" split-brain that would otherwise happen when the
    web callback auto-seeds before the scheduler's TOML import runs.
    """
    existing = get_profile(db_path, user_id)
    if existing is not None:
        return existing

    profile = UserProfile(
        user_id=user_id,
        display_name=display_name or _attr(seed_from, "display_name") or user_id,
        timezone=timezone or _attr(seed_from, "timezone") or "UTC",
        log_channel=_attr(seed_from, "log_channel") or "",
        alerts_channel=_attr(seed_from, "alerts_channel") or "",
        max_foreground_workers=int(_attr(seed_from, "max_foreground_workers") or 0),
        max_background_workers=int(_attr(seed_from, "max_background_workers") or 0),
        email_addresses=list(_attr(seed_from, "email_addresses") or []),
        trusted_email_senders=list(_attr(seed_from, "trusted_email_senders") or []),
        quiet_email_senders=list(_attr(seed_from, "quiet_email_senders") or []),
        disabled_skills=list(_attr(seed_from, "disabled_skills") or []),
        disabled_modules=list(_attr(seed_from, "disabled_modules") or []),
        routing=dict(_attr(seed_from, "routing") or {}),
        default_destination=_attr(seed_from, "default_destination") or "talk",
        email_reply_routing=_attr(seed_from, "email_reply_routing") or "origin+thread",
        outbound_approval=str(_attr(seed_from, "outbound_approval") or ""),
        external_turn_display=(
            _attr(seed_from, "external_turn_display") or "collapsed"
        ),
        default_briefings=_coerce_bool(_attr(seed_from, "default_briefings"), True),
        briefing_email_html=_coerce_bool(
            _attr(seed_from, "briefing_email_html"), True,
        ),
        timezone_follow_location=_coerce_bool(
            _attr(seed_from, "timezone_follow_location"), False,
        ),
    )
    _insert(db_path, profile)
    logger.info("ensured user_profile user=%s (new row)", user_id)
    return profile


def ensure_profile_with_status(
    db_path: Path,
    user_id: str,
    *,
    display_name: str = "",
    timezone: str = "",
    seed_from: "object | None" = None,
) -> tuple[UserProfile, bool]:
    """Same as ``ensure_profile`` but returns ``(profile, created)``.

    ``created`` is True iff this call inserted a new row. Web UI auto-seed
    in the OAuth callback uses this to decide whether to refresh
    ``display_name`` from NC: the spec is "first-login auto-seed", not
    "every login overwrites." Without this signal, a user whose NC
    display_name happens to equal their user_id triggers the
    placeholder-detection heuristic on every login.
    """
    existing = get_profile(db_path, user_id)
    if existing is not None:
        return existing, False
    profile = ensure_profile(
        db_path, user_id,
        display_name=display_name, timezone=timezone, seed_from=seed_from,
    )
    return profile, True


def _attr(obj: "object | None", name: str) -> "object | None":
    """Best-effort attribute read; returns None if obj is falsy or attr missing."""
    if obj is None:
        return None
    return getattr(obj, name, None)


def upsert_profile(db_path: Path, profile: UserProfile) -> None:
    """Replace the entire row for ``profile.user_id``.

    Use this for ``istota-admin user ensure`` and one-time TOML migration.
    Web UI writes go through :func:`update_profile` (partial update).
    """
    _insert(db_path, profile, replace=True)


def update_profile_with_status(
    db_path: Path,
    user_id: str,
    **fields: object,
) -> "tuple[UserProfile, str]":
    """Idempotent partial update. Returns ``(profile, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``user_briefings.ensure_briefing``,
    ``db.upsert_user_resource``, and ``secrets_store.upsert_secret``.

    Behavior:
    - If no row exists: insert via :func:`ensure_profile` (seeded from
      ``display_name`` / ``timezone`` in ``fields`` when present), then
      apply remaining ``fields`` via :func:`update_profile`. State is
      ``"created"``.
    - If row exists and every field in ``fields`` already matches: no
      write. State is ``"noop"``.
    - Otherwise: apply via :func:`update_profile`. State is ``"updated"``.
    """
    existing = get_profile(db_path, user_id)
    if existing is None:
        seed_display = fields.get("display_name")
        seed_tz = fields.get("timezone")
        ensure_profile(
            db_path, user_id,
            display_name=seed_display if isinstance(seed_display, str) else "",
            timezone=seed_tz if isinstance(seed_tz, str) else "",
        )
        if fields:
            profile = update_profile(db_path, user_id, **fields)
        else:
            profile = get_profile(db_path, user_id)
            assert profile is not None
        return profile, "created"

    if not fields:
        return existing, "noop"

    same = True
    for col, value in fields.items():
        if col not in _PROFILE_COLUMNS:
            same = False  # update_profile will raise — let it; treat as change
            break
        current = getattr(existing, col)
        if col in _LIST_COLUMNS:
            if list(current or []) != list(value or []):
                same = False
                break
        elif col in _DICT_COLUMNS:
            if dict(current or {}) != dict(value or {}):
                same = False
                break
        elif col in _BOOL_COLUMNS:
            bool_default = _BOOL_COLUMN_DEFAULTS[col]
            if _coerce_bool(current, bool_default) != _coerce_bool(value, bool_default):
                same = False
                break
        elif col == "default_destination":
            if (current or "talk") != (value or "talk"):
                same = False
                break
        elif col == "email_reply_routing":
            if (current or "origin+thread") != (value or "origin+thread"):
                same = False
                break
        elif col == "external_turn_display":
            if (current or "collapsed") != (value or "collapsed"):
                same = False
                break
        elif col in {"max_foreground_workers", "max_background_workers"}:
            if int(current or 0) != int(value or 0):
                same = False
                break
        else:
            if (current or "") != (value or ""):
                same = False
                break

    if same:
        return existing, "noop"

    profile = update_profile(db_path, user_id, **fields)
    return profile, "updated"


def update_profile(
    db_path: Path,
    user_id: str,
    **fields: object,
) -> UserProfile:
    """Partial update — only specified columns change. Returns the new profile.

    Raises ValueError if the user has no row yet (caller should ensure first)
    or if an unknown field is passed (defends against schema drift).
    """
    if not fields:
        existing = get_profile(db_path, user_id)
        if existing is None:
            raise ValueError(f"no user_profile row for {user_id!r}")
        return existing

    unknown = set(fields) - set(_PROFILE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown profile field(s): {sorted(unknown)}")

    sets: list[str] = []
    params: list[object] = []
    for col, value in fields.items():
        if col in _LIST_COLUMNS:
            value = json.dumps(list(value or []))
        elif col in _DICT_COLUMNS:
            value = json.dumps(dict(value or {}))
        elif col in _BOOL_COLUMNS:
            value = 1 if _coerce_bool(value, _BOOL_COLUMN_DEFAULTS[col]) else 0
        elif col in {"max_foreground_workers", "max_background_workers"}:
            value = int(value or 0)
        elif col == "default_destination":
            value = str(value) if value else "talk"
        elif col == "email_reply_routing":
            value = str(value) if value else "origin+thread"
        elif col == "external_turn_display":
            value = str(value) if value else "collapsed"
        else:
            value = str(value or "")
        sets.append(f"{col} = ?")
        params.append(value)

    sets.append("updated_at = datetime('now')")
    params.append(user_id)

    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise ValueError(f"no user_profile row for {user_id!r}")

    updated = get_profile(db_path, user_id)
    assert updated is not None  # row was just updated
    return updated


def delete_profile(db_path: Path, user_id: str) -> bool:
    """Remove a profile row and the user's avatars. True if a profile row went.

    The avatar delete rides this function's own connection, inside the same
    transaction. A *second* connection opened here would wait out the 30s busy
    timeout on the write lock this one is already holding and then raise — the
    hazard AGENTS.md documents for `notification_store` — which is why
    `avatars.delete_all_user_avatars` takes a connection rather than a path.

    The avatar rows go whether or not a profile row existed: they are keyed on
    the user id, not on the profile, so a user removed twice still leaves none
    behind. The return value stays a statement about the profile row.
    """
    with _connect(db_path) as conn:
        avatars.delete_all_user_avatars(conn, user_id)
        cur = conn.execute(
            "DELETE FROM user_profiles WHERE user_id = ?", (user_id,),
        )
        return cur.rowcount > 0


def _insert(db_path: Path, profile: UserProfile, *, replace: bool = False) -> None:
    """Insert (replace=False) or upsert (replace=True) a profile row.

    ``created_at`` survives an upsert because we use ON CONFLICT instead of
    INSERT OR REPLACE. ``updated_at`` is always set to ``datetime('now')``.
    """
    insert_cols = ("user_id", *_PROFILE_COLUMNS)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = (
        profile.user_id,
        profile.display_name,
        json.dumps(list(profile.email_addresses)),
        profile.timezone,
        profile.log_channel,
        profile.alerts_channel,
        int(profile.max_foreground_workers or 0),
        int(profile.max_background_workers or 0),
        json.dumps(list(profile.disabled_skills)),
        json.dumps(list(profile.trusted_email_senders)),
        json.dumps(list(profile.quiet_email_senders)),
        json.dumps(list(profile.disabled_modules)),
        json.dumps(dict(profile.routing)),
        profile.default_destination or "talk",
        profile.email_reply_routing or "origin+thread",
        profile.outbound_approval or "",
        profile.external_turn_display or "collapsed",
        1 if profile.default_briefings else 0,
        1 if profile.briefing_email_html else 0,
        1 if profile.timezone_follow_location else 0,
        json.dumps(dict(profile.google_scopes)),
    )
    cols_sql = ", ".join(insert_cols)
    if replace:
        update_clauses = ",\n                ".join(
            f"{c} = excluded.{c}" for c in _PROFILE_COLUMNS
        )
        sql = f"""
            INSERT INTO user_profiles ({cols_sql}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                {update_clauses},
                updated_at = datetime('now')
        """
    else:
        sql = f"""
            INSERT OR IGNORE INTO user_profiles ({cols_sql}, updated_at)
            VALUES ({placeholders}, datetime('now'))
        """

    with _connect(db_path) as conn:
        conn.execute(sql, values)


# --- Migration: TOML → DB --------------------------------------------------
#
# One-time import on every scheduler startup. Walks the loaded TOML
# UserConfig values; copies profile fields into the user_profiles table —
# but only for users that don't already have a row. Idempotent across
# restarts; safe to call before any TOML files exist.

def import_from_user_configs(
    db_path: Path,
    user_configs: dict[str, "object"],
) -> int:
    """Seed user_profiles rows from loaded TOML user configs.

    Skips users that already have a DB row (DB wins, never overwritten).
    Returns the number of rows written. Logs each new row at INFO level so
    operators can tell when migration runs vs. no-op restarts.
    """
    written = 0
    for user_id, user_config in user_configs.items():
        existing = get_profile(db_path, user_id)
        if existing is not None:
            continue

        profile = UserProfile(
            user_id=user_id,
            display_name=getattr(user_config, "display_name", "") or user_id,
            email_addresses=list(getattr(user_config, "email_addresses", []) or []),
            timezone=getattr(user_config, "timezone", "") or "UTC",
            log_channel=getattr(user_config, "log_channel", "") or "",
            alerts_channel=getattr(user_config, "alerts_channel", "") or "",
            max_foreground_workers=int(getattr(user_config, "max_foreground_workers", 0) or 0),
            max_background_workers=int(getattr(user_config, "max_background_workers", 0) or 0),
            disabled_skills=list(getattr(user_config, "disabled_skills", []) or []),
            trusted_email_senders=list(getattr(user_config, "trusted_email_senders", []) or []),
            quiet_email_senders=list(getattr(user_config, "quiet_email_senders", []) or []),
            disabled_modules=list(getattr(user_config, "disabled_modules", []) or []),
            routing=dict(getattr(user_config, "routing", {}) or {}),
            default_destination=getattr(user_config, "default_destination", "") or "talk",
            email_reply_routing=getattr(user_config, "email_reply_routing", "") or "origin+thread",
            outbound_approval=str(getattr(user_config, "outbound_approval", "") or ""),
            external_turn_display=(
                getattr(user_config, "external_turn_display", "") or "collapsed"
            ),
            default_briefings=_coerce_bool(getattr(user_config, "default_briefings", True), True),
            briefing_email_html=_coerce_bool(
                getattr(user_config, "briefing_email_html", True), True,
            ),
            timezone_follow_location=_coerce_bool(
                getattr(user_config, "timezone_follow_location", False), False,
            ),
        )
        try:
            _insert(db_path, profile, replace=False)
            written += 1
            logger.info("user_profile imported from TOML user=%s", user_id)
        except Exception as e:
            logger.warning("user_profile import failed user=%s: %s", user_id, e)

    if written:
        logger.info("user_profiles migration: wrote %d new row(s) from TOML", written)
    return written


def merge_into_user_config(profile: UserProfile, user_config: "object") -> "object":
    """Apply DB profile fields onto a TOML-loaded ``UserConfig`` in place.

    The DB row, when it exists, is authoritative for every field it owns.
    Briefings and resources stay TOML-only.

    Why this rule rather than "DB wins only when non-empty":
    A user clearing their email addresses via the web UI must not have the
    TOML resurrect them on the next config reload. Because ``ensure_profile``
    seeds list fields from the full TOML ``UserConfig`` at row-creation time
    (see :func:`ensure_profile`), an "empty" list in the DB unambiguously
    means "the user explicitly emptied it" — not "row not yet populated."
    """
    if user_config is None:
        return user_config

    setattr(user_config, "display_name", profile.display_name or getattr(user_config, "display_name", "") or profile.user_id)
    setattr(user_config, "timezone", profile.timezone or "UTC")
    setattr(user_config, "default_destination", profile.default_destination or "talk")
    setattr(user_config, "email_reply_routing", profile.email_reply_routing or "origin+thread")
    setattr(user_config, "outbound_approval", profile.outbound_approval or "")
    setattr(
        user_config, "external_turn_display",
        profile.external_turn_display or "collapsed",
    )
    setattr(user_config, "default_briefings", bool(profile.default_briefings))
    setattr(user_config, "briefing_email_html", bool(profile.briefing_email_html))
    setattr(
        user_config, "timezone_follow_location",
        bool(profile.timezone_follow_location),
    )
    for attr in (
        "log_channel", "alerts_channel",
        "max_foreground_workers", "max_background_workers",
    ):
        setattr(user_config, attr, getattr(profile, attr))

    # List fields: DB row owns them once it exists. The auto-seed path
    # carries TOML lists into the row, so an empty DB list is a deliberate
    # "user cleared this" signal.
    for attr in (
        "email_addresses", "disabled_skills",
        "trusted_email_senders", "quiet_email_senders", "disabled_modules",
    ):
        setattr(user_config, attr, list(getattr(profile, attr) or []))

    # routing is a dict but follows the same "DB owns it once the row exists"
    # rule as the list fields.
    setattr(user_config, "routing", dict(profile.routing or {}))

    return user_config
