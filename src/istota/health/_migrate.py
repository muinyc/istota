"""Schema init + biomarker_refs seeding for the health module.

:func:`ensure_initialised` is the single entry point — wires up the dirs,
runs schema migrations, re-seeds the canonical ``biomarker_refs`` table
when the bundled JSON has changed, and recanonicalizes existing biomarker
rows so newly-added aliases reach data imported before the alias existed.
Both passes are content-hashed and cheap when there's nothing to do.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from importlib.resources import as_file, files

from istota.health import db as health_db
from istota.health.models import HealthContext
from istota.timestamps import iso_now as _iso_now


logger = logging.getLogger(__name__)


# Legacy "seeded once" sentinel — kept readable for backward compat so a
# fresh deploy doesn't crash when migrating off the old gate. New gate is
# the content hash below, which re-runs the upsert whenever the bundled
# refs JSON content changes.
_SEED_SENTINEL_KEY = "biomarker_refs_seeded_at"
_SEED_HASH_KEY = "biomarker_refs_hash"
_RECANON_HASH_KEY = "biomarker_recanonicalize_hash"

_IMM_SEED_HASH_KEY = "immunization_refs_hash"
_IMM_SEED_SENTINEL_KEY = "immunization_refs_seeded_at"


def _read_bundled_refs() -> list[dict] | None:
    """Read the package-shipped ``biomarker_refs.json``."""
    try:
        resource = files("istota.health").joinpath("data/biomarker_refs.json")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as concrete:
            if not concrete.is_file():
                return None
            text = concrete.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("health_biomarker_refs_unparseable error=%s", e)
        return None
    if not isinstance(data, list):
        return None
    return data


def _refs_hash(refs: list[dict]) -> str:
    blob = json.dumps(refs, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def seed_biomarker_refs(ctx: HealthContext) -> int | None:
    """Seed (or re-seed) the ``biomarker_refs`` table from the bundled JSON.

    Re-runs whenever the bundled file's content hash differs from the
    last-applied hash recorded in ``schema_meta``. ``upsert_biomarker_ref``
    handles existing rows, so this is safe to call on every startup.
    Returns the row count when an upsert ran, ``None`` when up-to-date.
    """
    health_db.init_db(ctx.db_path)
    refs = _read_bundled_refs()
    if not refs:
        return None
    new_hash = _refs_hash(refs)
    with health_db.connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (_SEED_HASH_KEY,),
        ).fetchone()
        if row and row["value"] == new_hash:
            return None
        for ref in refs:
            try:
                health_db.upsert_biomarker_ref(conn, ref)
            except (KeyError, sqlite3.Error) as e:
                logger.warning(
                    "health_biomarker_ref_skip name=%s error=%s",
                    ref.get("name", "?"), e,
                )
                continue
        # Update both sentinels so old deploys can detect the new gate.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_SEED_HASH_KEY, new_hash),
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_SEED_SENTINEL_KEY, _iso_now()),
        )
        conn.commit()
    logger.info("health_biomarker_refs_seeded count=%d hash=%s", len(refs), new_hash)
    return len(refs)


def recanonicalize_biomarker_names(ctx: HealthContext) -> int:
    """Rewrite ``biomarkers.name`` rows that match an alias of a canonical ref.

    Biomarker rows are stored under whatever ``name`` field the import or
    OCR step produced. When we add a new alias (e.g. ``"Cholesterol"`` →
    ``Cholesterol_Total``), rows imported before that alias existed stay
    stuck under the raw name and land in the matrix's "Other" bucket.
    This pass rewrites them once. It's idempotent — canonical names
    self-match — and gated by the refs content hash so it only does
    actual work when the alias table changed.
    """
    new_hash = _refs_hash(_read_bundled_refs() or [])
    with health_db.connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (_RECANON_HASH_KEY,),
        ).fetchone()
        if row and row["value"] == new_hash:
            return 0
        refs = health_db.list_biomarker_refs(conn)
        canonical_names = {r.name for r in refs}
        alias_to_canonical: dict[str, str] = {}
        for r in refs:
            for a in r.aliases:
                alias_to_canonical[a.strip().lower()] = r.name
        rows = conn.execute("SELECT id, name FROM biomarkers").fetchall()
        fixed = 0
        for b in rows:
            current = b["name"] or ""
            if current in canonical_names:
                continue
            canonical = alias_to_canonical.get(current.strip().lower())
            if canonical and canonical != current:
                conn.execute(
                    "UPDATE biomarkers SET name = ? WHERE id = ?",
                    (canonical, b["id"]),
                )
                fixed += 1
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_RECANON_HASH_KEY, new_hash),
        )
        conn.commit()
    if fixed:
        logger.info("health_biomarker_recanonicalize fixed=%d hash=%s", fixed, new_hash)
    return fixed


def _read_bundled_immunization_refs() -> list[dict] | None:
    """Read the package-shipped ``immunization_refs.json``."""
    try:
        resource = files("istota.health").joinpath("data/immunization_refs.json")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as concrete:
            if not concrete.is_file():
                return None
            text = concrete.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("health_immunization_refs_unparseable error=%s", e)
        return None
    if not isinstance(data, list):
        return None
    return data


def seed_immunization_refs(ctx: HealthContext) -> int | None:
    """Seed (or re-seed) the ``immunization_refs`` table from the bundled JSON.

    Re-runs whenever the bundled file's content hash differs from the
    last-applied hash recorded in ``schema_meta``. Mirrors
    :func:`seed_biomarker_refs`.
    """
    health_db.init_db(ctx.db_path)
    refs = _read_bundled_immunization_refs()
    if not refs:
        return None
    new_hash = _refs_hash(refs)
    with health_db.connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_IMM_SEED_HASH_KEY,),
        ).fetchone()
        if row and row["value"] == new_hash:
            return None
        for ref in refs:
            try:
                health_db.upsert_immunization_ref(conn, ref)
            except (KeyError, sqlite3.Error) as e:
                logger.warning(
                    "health_immunization_ref_skip name=%s error=%s",
                    ref.get("name", "?"), e,
                )
                continue
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_IMM_SEED_HASH_KEY, new_hash),
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_IMM_SEED_SENTINEL_KEY, _iso_now()),
        )
        conn.commit()
    logger.info(
        "health_immunization_refs_seeded count=%d hash=%s", len(refs), new_hash,
    )
    return len(refs)


def ensure_initialised(ctx: HealthContext) -> None:
    """Wire up the health workspace: dirs + schema + seed + recanonicalize."""
    ctx.ensure_dirs()
    health_db.init_db(ctx.db_path)
    seed_biomarker_refs(ctx)
    recanonicalize_biomarker_names(ctx)
    seed_immunization_refs(ctx)
    _backfill_content_hashes(ctx)
    _sweep_orphan_documents(ctx)


def _sweep_orphan_documents(ctx: HealthContext) -> None:
    """Collect linkless documents — abandoned imports, detached leftovers.

    Rides this once-per-touch hook rather than getting its own scheduler job:
    the work is tiny (an indexed anti-join returning nothing on almost every
    call) and the window is 24h, so opportunistic timing is enough. Never
    fatal — a sweep failure must not stop the module initialising.
    """
    from istota.health import documents as health_documents

    try:
        with health_db.connect(ctx.db_path) as conn:
            n = health_documents.sweep_orphan_documents(conn, ctx)
            if n:
                conn.commit()
    except (sqlite3.Error, OSError) as e:
        logger.warning("health_document_sweep_failed error=%s", e)


def _backfill_content_hashes(ctx: HealthContext) -> None:
    """One-shot backfill of ``panels.content_hash``.

    Replays per-panel hash computation for any panel created before the
    column existed. Gated by a ``schema_meta`` sentinel so it runs once.
    """
    with health_db.connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ("panels_content_hash_backfilled",),
        ).fetchone()
        if row:
            return
        n = health_db.backfill_panel_content_hashes(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("panels_content_hash_backfilled", _iso_now()),
        )
        conn.commit()
    if n:
        logger.info("health_panels_content_hash_backfilled count=%d", n)
