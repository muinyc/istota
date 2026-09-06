"""Audit log and fingerprints for op-based USER.md curation.

State lives in the framework KV store (``istota_kv``), not in sidecar files
beside USER.md. Two namespaces, both reserved:

- ``_memory_audit`` — one key per write event, the key being the event's UTC
  timestamp plus a per-second counter (``2026-08-29T18:22:59Z-000``). ISO-8601
  with a ``Z`` suffix sorts lexically in timestamp order, so ``ORDER BY key``
  in ``db.kv_list`` reads the log oldest-first the way the JSONL did. One key
  per entry rather than one growing JSON array is what keeps an append O(1);
  an array would be read-parse-append-rewrite on every write.
- ``_memory_curation`` — ``last_seen`` (USER.md's size + sha256 as of the last
  write that went through the ops engine) and ``lint_seen`` (the Phase-A lint
  dedup set).

A write event is one of:
  - a nightly curator run (one entry batches all that night's ops)
  - a runtime CLI invocation (one entry per CLI call, typically one op)
  - a synthetic ``legacy`` entry when the bypass detector notices an
    unexplained USER.md mtime/size change

Each entry carries:
  - ``source``: "nightly" | "runtime" | "cli" | "legacy"
  - ``entry_kind``: "batch" (default), "lint_candidate", "aborted",
    "legacy_detected"

**Why the store and not the mount.** The sidecars sat under
``{mount}/Users/{user_id}/{bot_dir}/config/``, which ``build_bwrap_cmd`` binds
**read-write** into that user's sandbox — so a task could delete its own audit
trail or rewrite the ``last_seen`` fingerprint the bypass detector compares
against, and the operator saw three machine-only files in a folder they read by
hand. The framework database is in no sandbox at any path, and the ``_`` prefix
on both namespaces is refused by the model-facing ``kv`` skill and by the
deferred-op applier behind it, so the model cannot reach these rows through the
one tool that otherwise spans the whole store.

The caller only needs ``config.db_path``; nothing here reads the mount except
``migrate_user_md_sidecars``, which is the one-time importer. Every function is
best-effort and never raises — several callers sit on a graceful-degradation
path where an exception would abort a nightly run.

No retention in v1, the same as the JSONL it replaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ... import db
from ...storage import _get_mount_path, get_user_memory_path
from ...timestamps import iso_now_seconds as _utc_now

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger("istota.memory.curation.audit")

# Both namespaces carry the reserved prefix, which is what makes `skills/kv`
# and `scheduler_deferred` refuse them. `tests/test_curation_audit.py` asserts
# that here rather than trusting the two literals to keep starting with "_".
AUDIT_NAMESPACE = "_memory_audit"
CURATION_NAMESPACE = "_memory_curation"
LAST_SEEN_KEY = "last_seen"
LINT_SEEN_KEY = "lint_seen"


def _db_path(config: "Config") -> Path | None:
    """The framework DB path, or None when the caller has no database.

    The runtime CLI builds a shim carrying only ``db_path``; the daemon passes
    a real Config. Both answer this.
    """
    path = getattr(config, "db_path", None)
    if not path:
        return None
    return Path(path)


def _with_conn(config: "Config", fn, *, default=None, what: str = "operation"):
    """Run `fn(conn)` against the framework DB, returning `default` on any
    failure. Never raises.

    One exception boundary for the whole module, and it is deliberately around
    the *whole* `with` rather than inside it: `db.get_db` commits only when its
    body returns normally, so an error partway through a multi-row write leaves
    nothing committed. That is what makes the sidecar import safe to retry —
    a half-finished import would otherwise commit its first N entries, keep the
    file, and duplicate them on the next pass.

    A caller that wants "no database" and "database failed" to read the same is
    why this returns `default` for both: every caller here is either a nightly
    background pass or a CLI that has already written USER.md, and neither has
    anything useful to do with the distinction.
    """
    path = _db_path(config)
    if path is None:
        logger.debug("curation audit: no db_path configured, skipping %s", what)
        return default
    try:
        with db.get_db(path) as conn:
            return fn(conn)
    except Exception as e:  # noqa: BLE001 - never raise at the caller
        logger.warning("curation audit: %s failed against %s: %s", what, path, e)
        return default


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _next_audit_key(conn, user_id: str, ts: str) -> str:
    """An unused key for `ts`, as `<ts>-<NNN>`.

    Several entries can land in the same second — a nightly run emits
    `legacy_detected`, then `lint_candidate`, then `batch` — and the timestamp
    has one-second resolution, so the counter is what stops the second write
    overwriting the first through `kv_set`'s upsert.

    The maximum is taken by parsing rather than by `ORDER BY key DESC`: a
    counter past 999 would widen to four digits and sort below `999`
    lexically. Rows-per-second here is a handful, so the scan is free.
    """
    highest = -1
    cursor = conn.execute(
        "SELECT key FROM istota_kv WHERE user_id = ? AND namespace = ? AND key LIKE ?",
        (user_id, AUDIT_NAMESPACE, f"{ts}-%"),
    )
    for row in cursor.fetchall():
        try:
            highest = max(highest, int(str(row["key"]).rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{ts}-{highest + 1:03d}"


def write_audit_log(
    config: "Config",
    user_id: str,
    applied: list[dict],
    rejected: list[dict],
    user_md_size_bytes: int | None = None,
    source: str = "nightly",
    entry_kind: str = "batch",
    extra: dict | None = None,
) -> None:
    """Append a single audit entry. No-op when both lists are empty AND
    no `extra` payload is supplied.

    `user_md_size_bytes`, when provided, records USER.md size at the time of
    the curation run so growth curves are inspectable from the audit alone.

    `source` distinguishes "nightly" curator entries from "runtime" CLI
    entries from operator "cli" entries from synthetic "legacy" entries.

    `entry_kind` is "batch" by default; lint Phase A logging uses
    "lint_candidate", aborted curator runs use "aborted", and bypass
    detection uses "legacy_detected".

    `extra` is merged into the entry as additional top-level keys
    (e.g. `lint_candidates`, `aborted_reason`, `legacy_signal`).
    """
    if not applied and not rejected and not extra:
        return

    entry: dict = {
        "ts": _utc_now(),
        "user_id": user_id,
        "source": source,
        "entry_kind": entry_kind,
        "applied": applied,
        "rejected": rejected,
    }
    if user_md_size_bytes is not None:
        entry["user_md_size_bytes"] = user_md_size_bytes
    if extra:
        for k, v in extra.items():
            if k not in entry:
                entry[k] = v

    def _write(conn):
        key = _next_audit_key(conn, user_id, entry["ts"])
        db.kv_set(
            conn, user_id, AUDIT_NAMESPACE, key,
            json.dumps(entry, ensure_ascii=False),
        )

    _with_conn(config, _write, what=f"audit write for {user_id}")


def read_audit_entries(
    config: "Config", user_id: str, *, limit: int | None = None
) -> list[dict]:
    """The user's audit entries, oldest first. `limit` keeps the newest N.

    A row whose value no longer parses is skipped rather than raising — the
    log is diagnostic, and one bad row must not hide the rest.
    """
    rows = _with_conn(
        config,
        lambda conn: db.kv_list(conn, user_id, AUDIT_NAMESPACE),
        default=[],
        what=f"audit read for {user_id}",
    )
    entries: list[dict] = []
    for row in rows:
        try:
            entries.append(json.loads(row["value"]))
        except (TypeError, ValueError):
            continue
    if limit is not None and limit >= 0:
        entries = entries[-limit:] if limit else []
    return entries


# ---------------------------------------------------------------------------
# USER.md fingerprint (bypass detection)
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json_key(config: "Config", user_id: str, key: str) -> dict | None:
    row = _with_conn(
        config,
        lambda conn: db.kv_get(conn, user_id, CURATION_NAMESPACE, key),
        what=f"{key} read for {user_id}",
    )
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_key(config: "Config", user_id: str, key: str, value: dict) -> None:
    _with_conn(
        config,
        lambda conn: db.kv_set(
            conn, user_id, CURATION_NAMESPACE, key,
            json.dumps(value, ensure_ascii=False, sort_keys=True),
        ),
        what=f"{key} write for {user_id}",
    )


def read_last_seen(config: "Config", user_id: str) -> dict | None:
    """Return the last-known USER.md fingerprint, or None on first sight /
    on any read error. Never raises."""
    return _read_json_key(config, user_id, LAST_SEEN_KEY)


def write_last_seen(
    config: "Config", user_id: str, *, size_bytes: int, sha256: str
) -> None:
    """Update the USER.md fingerprint. Best-effort; never raises."""
    _write_json_key(
        config, user_id, LAST_SEEN_KEY,
        {"ts": _utc_now(), "size_bytes": size_bytes, "sha256": sha256},
    )


def read_lint_seen(config: "Config", user_id: str) -> dict[str, str]:
    """The Phase-A lint dedup set: `{candidate_hash: "YYYY-MM-DD"}`.

    Empty on first sight or on any read error, which re-emits the candidates —
    the pre-existing behaviour when the sidecar was unreadable.
    """
    value = _read_json_key(config, user_id, LINT_SEEN_KEY) or {}
    hashes = value.get("hashes")
    return hashes if isinstance(hashes, dict) else {}


def write_lint_seen(config: "Config", user_id: str, hashes: dict[str, str]) -> None:
    """Persist the lint dedup set. Best-effort; never raises."""
    _write_json_key(config, user_id, LINT_SEEN_KEY, {"hashes": hashes})


def detect_bypass_write(
    config: "Config", user_id: str, current_text: str
) -> dict | None:
    """Detect whether USER.md changed since last seen WITHOUT a recorded
    audit entry. Returns the bypass-signal dict if a bypass is suspected,
    else None.

    Caller responsibilities:
      - Pass the current contents of USER.md (already read once).
      - On a positive return, write a synthetic legacy entry via
        `write_audit_log(..., source="legacy", entry_kind="legacy_detected", extra=signal)`.
      - Always call `write_last_seen()` afterwards to update the fingerprint.

    Note: this function returns the signal but does NOT consult the
    audit log itself. The nightly run should only call this on its first
    pass through a user — after that, runtime writes will have updated
    the last_seen fingerprint via `write_last_seen()` themselves.
    """
    last = read_last_seen(config, user_id)
    if last is None:
        return None  # First sight; baseline only.
    current_sha = _hash_text(current_text)
    current_size = len(current_text.encode("utf-8"))
    if last.get("sha256") == current_sha:
        return None
    return {
        "previous_size_bytes": last.get("size_bytes"),
        "previous_sha256": last.get("sha256"),
        "previous_ts": last.get("ts"),
        "current_size_bytes": current_size,
        "current_sha256": current_sha,
    }


# ---------------------------------------------------------------------------
# One-time import of the pre-KV sidecars
# ---------------------------------------------------------------------------


def legacy_audit_sidecar_path(config: "Config", user_id: str) -> Path:
    """The pre-KV `USER.md.audit.jsonl`. Read by the migration, written by
    nothing."""
    user_md = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    return user_md.parent / "USER.md.audit.jsonl"


def legacy_last_seen_sidecar_path(config: "Config", user_id: str) -> Path:
    """The pre-KV `USER.md.last_seen.json`. Migration-only."""
    user_md = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    return user_md.parent / "USER.md.last_seen.json"


def legacy_lint_seen_sidecar_path(config: "Config", user_id: str) -> Path:
    """The pre-KV `USER.md.lint_seen.json`. Migration-only."""
    user_md = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    return user_md.parent / "USER.md.lint_seen.json"


def _import_audit_sidecar(config: "Config", user_id: str, path: Path) -> bool:
    """Import every JSONL line into `_memory_audit`. True when the file can be
    unlinked.

    Each line becomes its own key. Existing rows are never overwritten: the
    key is derived from the entry's own `ts` and the next free counter, so an
    import that runs twice would duplicate rather than clobber — which is why
    the file is unlinked on success and why a partial import leaves it in
    place for the next pass rather than being retried against a half-written
    namespace.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("sidecar import: cannot read %s: %s", path, e)
        return False

    entries: list[dict] = []
    malformed = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            malformed += 1

    if malformed:
        # Unlinking would destroy the only copy of a line we could not read.
        logger.warning(
            "sidecar import: %s has %d unparseable line(s); importing the rest "
            "and keeping the file", path, malformed,
        )

    def _write_all(conn) -> int:
        written = 0
        for entry in entries:
            ts = entry.get("ts")
            if not isinstance(ts, str) or not ts:
                ts = _utc_now()
            key = _next_audit_key(conn, user_id, ts)
            db.kv_set(
                conn, user_id, AUDIT_NAMESPACE, key,
                json.dumps(entry, ensure_ascii=False),
            )
            written += 1
        return written

    written = _with_conn(
        config, _write_all, default=None, what=f"sidecar import for {user_id}",
    )
    if written != len(entries):
        return False
    return malformed == 0


def _import_json_sidecar(
    config: "Config", user_id: str, path: Path, key: str
) -> bool:
    """Import a single-blob sidecar into `_memory_curation`. True when the file
    can be unlinked.

    A row already in the store wins — a runtime CLI write that landed before
    the first migration pass is newer than the file, and overwriting it with
    the stale fingerprint would re-arm the bypass detector against a change it
    had already accounted for. An unreadable file is *not* a reason to keep
    it: it carries nothing, so it is dropped.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, ValueError) as e:
        logger.warning("sidecar import: discarding unreadable %s: %s", path, e)
        return True
    if not isinstance(value, dict):
        logger.warning("sidecar import: discarding non-object %s", path)
        return True
    if _read_json_key(config, user_id, key) is not None:
        return True
    _write_json_key(config, user_id, key, value)
    return _read_json_key(config, user_id, key) is not None


def migrate_user_md_sidecars(config: "Config", user_id: str) -> dict[str, str]:
    """Import the three pre-KV sidecars into the store, then remove them.

    Idempotent by absence: once a file is gone there is nothing to import, so
    this costs three `stat` calls on every night after the first. Returns a
    per-file outcome map for the log — `"absent"`, `"migrated"`, or `"kept"`
    when the import was incomplete and the file has to survive for the next
    pass.

    Deletion is the only destructive step in this module and it is gated on a
    completed import of that specific file, one file at a time. Never raises.
    """
    outcomes: dict[str, str] = {}
    if not getattr(config, "use_mount", False):
        return outcomes

    try:
        targets = [
            ("audit", legacy_audit_sidecar_path(config, user_id), None),
            ("last_seen", legacy_last_seen_sidecar_path(config, user_id), LAST_SEEN_KEY),
            ("lint_seen", legacy_lint_seen_sidecar_path(config, user_id), LINT_SEEN_KEY),
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("sidecar import: cannot resolve paths for %s: %s", user_id, e)
        return outcomes

    for name, path, key in targets:
        try:
            if not path.is_file():
                outcomes[name] = "absent"
                continue
            if key is None:
                done = _import_audit_sidecar(config, user_id, path)
            else:
                done = _import_json_sidecar(config, user_id, path, key)
            if not done:
                outcomes[name] = "kept"
                continue
            path.unlink()
            outcomes[name] = "migrated"
        except Exception as e:  # noqa: BLE001
            logger.warning("sidecar import: %s failed for %s: %s", name, user_id, e)
            outcomes[name] = "kept"

    if any(v != "absent" for v in outcomes.values()):
        logger.info(
            "memory_curation_sidecar_migration user=%s %s",
            user_id,
            " ".join(f"{k}={v}" for k, v in sorted(outcomes.items())),
        )
    return outcomes
