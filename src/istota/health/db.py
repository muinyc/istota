"""SQLite layer for the per-user health DB.

One DB per user, lives at ``{ctx.db_path}``. ``init_db`` is idempotent and
sets WAL mode exactly once at creation (re-issuing the pragma races with
sibling readers — same rule as the feeds / location DBs).

Tables:

* ``stats``           — time-series body stats (weight, BP, resting HR, …)
* ``panels``          — one row per lab visit / uploaded report
* ``biomarkers``      — individual values from a panel
* ``biomarker_refs``  — canonical names + Istota-curated reference ranges
* ``health_settings`` — DOB, height, biological sex, display unit prefs
* ``documents``       — stored paperwork (scans, discharge summaries, cards)
* ``document_links``  — polymorphic join to encounter / diagnosis / immunization
* ``schema_meta``     — schema version + migration sentinels
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from istota.health.models import (
    Biomarker,
    BiomarkerRef,
    Diagnosis,
    Document,
    Encounter,
    Immunization,
    ImmunizationRef,
    Panel,
    Stat,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 4


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stats_metric_date ON stats(metric, measured_at);
CREATE INDEX IF NOT EXISTS idx_stats_measured ON stats(measured_at);
-- Defence against the overlapping-sync double-insert race (H7). Source-
-- tagged rows (garmin, apple_health, etc.) must dedupe on
-- (metric, measured_at, source); manual entries are excluded because a
-- user may legitimately log two readings at the same wall-clock time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_source_unique
    ON stats(metric, measured_at, source)
    WHERE source != 'manual';

CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drawn_at TEXT NOT NULL,
    lab_name TEXT,
    panel_type TEXT,
    source_file TEXT,
    source_mime TEXT,
    ocr_text TEXT,
    draft INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_panels_drawn ON panels(drawn_at);
-- idx_panels_content_hash is created in _migrate_add_content_hash so it
-- runs *after* the ALTER TABLE on older DBs that pre-date the column.

CREATE TABLE IF NOT EXISTS biomarkers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    display_name TEXT,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    ref_range_low REAL,
    ref_range_high REAL,
    flag TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_biomarkers_panel ON biomarkers(panel_id);
CREATE INDEX IF NOT EXISTS idx_biomarkers_name ON biomarkers(name);

CREATE TABLE IF NOT EXISTS health_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biomarker_explainers (
    name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('high', 'low')),
    summary TEXT NOT NULL,
    causes_json TEXT NOT NULL DEFAULT '[]',
    mitigations_json TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, direction)
);

CREATE TABLE IF NOT EXISTS biomarker_refs (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    default_unit TEXT NOT NULL,
    ref_range_low REAL,
    ref_range_high REAL,
    ref_range_low_m REAL,
    ref_range_high_m REAL,
    ref_range_low_f REAL,
    ref_range_high_f REAL,
    aliases TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_date TEXT NOT NULL,
    encounter_type TEXT NOT NULL,
    provider TEXT,
    facility TEXT,
    specialty TEXT,
    reason TEXT,
    notes TEXT,
    dedup_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_encounters_date ON encounters(encounter_date);
CREATE INDEX IF NOT EXISTS idx_encounters_type ON encounters(encounter_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_dedup
    ON encounters(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    icd10 TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    date_diagnosed TEXT,
    date_resolved TEXT,
    encounter_id INTEGER REFERENCES encounters(id) ON DELETE SET NULL,
    severity TEXT,
    notes TEXT,
    dedup_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_status ON diagnoses(status);
CREATE INDEX IF NOT EXISTS idx_diagnoses_name ON diagnoses(name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_diagnoses_dedup
    ON diagnoses(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS immunizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    product_name TEXT,
    date_given TEXT NOT NULL,
    manufacturer TEXT,
    dose_label TEXT,
    lot_number TEXT,
    route TEXT,
    site TEXT,
    administered_by TEXT,
    facility TEXT,
    encounter_id INTEGER REFERENCES encounters(id) ON DELETE SET NULL,
    cvx_code TEXT,
    notes TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    dedup_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_immunizations_name ON immunizations(name);
CREATE INDEX IF NOT EXISTS idx_immunizations_date ON immunizations(date_given);
CREATE INDEX IF NOT EXISTS idx_immunizations_encounter ON immunizations(encounter_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_immunizations_dedup
    ON immunizations(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS immunization_refs (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    schedule TEXT NOT NULL,
    interval_days INTEGER,
    primary_series_doses INTEGER,
    aliases TEXT,
    description TEXT,
    typical_age_range TEXT
);

-- Stored paperwork: one row per blob, independent of what it evidences.
-- Bytes live at {uploads_dir}/documents/{id}/{filename}; `stored_path` is
-- relative to uploads_dir, matching how panels.source_file stores its path.
-- New in SCHEMA_VERSION 3. No migration function is needed: both tables are
-- brand new, so `CREATE TABLE IF NOT EXISTS` inside executescript(SCHEMA_SQL)
-- creates them on an existing DB too. (Contrast panels.content_hash, which
-- needed an ALTER because the table already existed.)
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,          -- sanitized, as stored on disk
    original_filename TEXT,          -- as supplied by the client, for display
    mime TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    stored_path TEXT NOT NULL,       -- relative to uploads_dir
    ocr_text TEXT,
    source TEXT NOT NULL DEFAULT 'manual',  -- manual | import | agent
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Last time anything referenced this document: created, deduped onto by a
    -- fresh upload, linked, or unlinked. The orphan sweep measures its window
    -- from HERE, not from created_at. Measuring from creation would mean a
    -- document older than the window is destroyed the instant its last link
    -- goes — which is exactly the detach-then-reattach-elsewhere correction
    -- flow the delay exists to protect (D5).
    last_touched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

-- Polymorphic join. `entity_id` carries no FK — it points at one of three
-- tables — so an entity delete must clear its links by hand (see
-- delete_encounter / delete_diagnosis / delete_immunization).
CREATE TABLE IF NOT EXISTS document_links (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,       -- encounter | diagnosis | immunization
    entity_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (document_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_document_links_entity
    ON document_links(entity_type, entity_id);

-- Which encounters a condition was seen at. New in SCHEMA_VERSION 4.
--
-- A condition is routinely managed across several visits — the GP who found
-- it, the specialist it was referred to, the follow-up six weeks later — so
-- the relationship is many-to-many. It began life as the scalar
-- `diagnoses.encounter_id`, which silently kept only the first link (the
-- importer's `reconcile=True` path deliberately would not overwrite it, so
-- every later mention of the same condition was dropped).
--
-- That column is still written by `insert_diagnosis` for the benefit of
-- anything reading an old DB, but nothing here reads it any more: this table
-- is the source of truth. `_migrate_diagnosis_encounters` backfills it once.
--
-- Unlike `document_links`, both columns carry real foreign keys, so deletes
-- cascade on their own — every caller goes through `connect()`, which sets
-- `PRAGMA foreign_keys = ON`.
CREATE TABLE IF NOT EXISTS diagnosis_encounters (
    diagnosis_id INTEGER NOT NULL REFERENCES diagnoses(id) ON DELETE CASCADE,
    encounter_id INTEGER NOT NULL REFERENCES encounters(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (diagnosis_id, encounter_id)
);
CREATE INDEX IF NOT EXISTS idx_diagnosis_encounters_encounter
    ON diagnosis_encounters(encounter_id);

"""


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        # WAL: this DB now lives on LOCAL disk (Config.module_db_path, off the
        # rclone FUSE mount), so WAL's mmap'd -shm is safe — the SIGBUS that
        # forced DELETE (ISSUE-157) was a FUSE artifact. WAL restores
        # reader/writer concurrency. Issued unconditionally so a relocated
        # DELETE-mode DB converts on first touch; no-op once WAL.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        _migrate_add_content_hash(conn)
        _migrate_add_panel_encounter_fk(conn)
        _migrate_add_history_dedup_keys(conn)
        _migrate_diagnosis_encounters(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


def _migrate_add_content_hash(conn: sqlite3.Connection) -> None:
    """Add ``panels.content_hash`` on older DBs + ensure its index. Idempotent.

    Pre-existing rows are left NULL; backfill runs once via
    :func:`backfill_panel_content_hashes` from ``_migrate.ensure_initialised``.

    Index creation lives here rather than in ``SCHEMA_SQL`` because
    ``executescript`` would otherwise hit the ``CREATE INDEX`` line before
    the ALTER on a pre-migration DB and blow up with
    ``no such column: content_hash``.
    """
    # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk).
    # ``init_db`` opens the connection without a row factory, so index by
    # position rather than name.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE panels ADD COLUMN content_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_panels_content_hash "
        "ON panels(content_hash)",
    )


def _migrate_add_history_dedup_keys(conn: sqlite3.Connection) -> None:
    """Add ``dedup_key`` to encounters / diagnoses on older DBs. Idempotent.

    Used by the deferred-op replayer so retry-after-partial-success doesn't
    double-insert. NULL on legacy rows; only fresh insert ops carry one.
    """
    for table in ("encounters", "diagnoses"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "dedup_key" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN dedup_key TEXT")
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_dedup "
            f"ON {table}(dedup_key) WHERE dedup_key IS NOT NULL",
        )


def _migrate_add_panel_encounter_fk(conn: sqlite3.Connection) -> None:
    """Add ``panels.encounter_id`` on older DBs. Idempotent.

    The reference is declared in the ALTER (SQLite stores the constraint
    in the column definition); the ON DELETE SET NULL fires when
    ``PRAGMA foreign_keys = ON`` is set on the connection — which
    :func:`connect` does.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
    if "encounter_id" not in cols:
        conn.execute(
            "ALTER TABLE panels ADD COLUMN encounter_id INTEGER "
            "REFERENCES encounters(id) ON DELETE SET NULL",
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_panels_encounter "
        "ON panels(encounter_id)",
    )


def _migrate_diagnosis_encounters(conn: sqlite3.Connection) -> None:
    """Seed ``diagnosis_encounters`` from the legacy scalar column. Once only.

    The table itself is created by ``CREATE TABLE IF NOT EXISTS`` in
    ``SCHEMA_SQL``; all this adds is the one-time backfill of links that were
    recorded before the relationship became many-to-many.

    Gated on the stored schema version rather than on the table being empty.
    ``diagnoses.encounter_id`` is deliberately *not* cleared by the migration
    (an old DB stays readable by old code), so a backfill that ran on every
    open would silently re-create any link the user later removed — the next
    unlink would appear to work and then come back. Running strictly below
    version 4 means it happens exactly once.

    A DB with no recorded version is either brand new (``diagnoses`` empty, so
    the backfill is a no-op) or pre-dates ``schema_meta``, which is precisely
    the case that wants backfilling — both are handled by treating a missing
    version as 0.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'",
    ).fetchone()
    try:
        current = int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        current = 0
    if current >= 4:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO diagnosis_encounters (diagnosis_id, encounter_id)
        SELECT d.id, d.encounter_id
        FROM diagnoses d
        JOIN encounters e ON e.id = d.encounter_id
        WHERE d.encounter_id IS NOT NULL
        """,
    )


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a row-factory-equipped connection with FKs on."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- stats -------------------------------------------------------------------


def _row_to_stat(row: sqlite3.Row) -> Stat:
    return Stat(
        id=row["id"],
        measured_at=row["measured_at"],
        metric=row["metric"],
        value=float(row["value"]),
        unit=row["unit"],
        source=row["source"],
        source_ref=row["source_ref"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def insert_stat(
    conn: sqlite3.Connection,
    *,
    metric: str,
    value: float,
    unit: str,
    measured_at: str | None = None,
    source: str = "manual",
    source_ref: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert a stat row. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO stats(measured_at, metric, value, unit, source, source_ref, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (measured_at or _now(), metric, value, unit, source, source_ref, notes),
    )
    return int(cur.lastrowid)


def list_stats(
    conn: sqlite3.Connection,
    *,
    metric: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Stat]:
    clauses: list[str] = []
    params: list[Any] = []
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if since:
        clauses.append("measured_at >= ?")
        params.append(since)
    if until:
        clauses.append("measured_at <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM stats {where} ORDER BY measured_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_stat(r) for r in rows]


def stat_exists_for_source(
    conn: sqlite3.Connection,
    *,
    metric: str,
    measured_at: str,
    source: str,
) -> bool:
    """Has a row with this (metric, measured_at, source) already been written?

    Used by source-tagged sync engines (e.g. Garmin daily summaries) to
    dedup before insert. No unique constraint exists on these columns —
    the check is application-layer.
    """
    row = conn.execute(
        "SELECT 1 FROM stats "
        "WHERE metric = ? AND measured_at = ? AND source = ? LIMIT 1",
        (metric, measured_at, source),
    ).fetchone()
    return row is not None


def latest_stats(conn: sqlite3.Connection) -> dict[str, Stat]:
    """Latest stat per metric. Maps metric → :class:`Stat`."""
    rows = conn.execute(
        """
        SELECT * FROM stats
        WHERE id IN (
            SELECT MAX(id) FROM stats GROUP BY metric
        )
        """
    ).fetchall()
    return {r["metric"]: _row_to_stat(r) for r in rows}


def delete_stat(conn: sqlite3.Connection, stat_id: int) -> int:
    cur = conn.execute("DELETE FROM stats WHERE id = ?", (stat_id,))
    return cur.rowcount


def delete_stats_for_panel(conn: sqlite3.Connection, panel_id: int) -> int:
    """Delete stats rows derived from a panel (source='lab_panel')."""
    cur = conn.execute(
        "DELETE FROM stats WHERE source = 'lab_panel' AND source_ref = ?",
        (panel_id,),
    )
    return cur.rowcount


# -- panels ------------------------------------------------------------------


def _row_to_panel(row: sqlite3.Row) -> Panel:
    # Older rows (pre content_hash migration) won't have the column on a
    # row factory built before the ALTER. Guarded so the migration window
    # doesn't crash readers.
    try:
        content_hash = row["content_hash"]
    except (IndexError, KeyError):
        content_hash = None
    try:
        encounter_id = row["encounter_id"]
    except (IndexError, KeyError):
        encounter_id = None
    return Panel(
        id=row["id"],
        drawn_at=row["drawn_at"],
        lab_name=row["lab_name"],
        panel_type=row["panel_type"],
        source_file=row["source_file"],
        source_mime=row["source_mime"],
        ocr_text=row["ocr_text"],
        draft=bool(row["draft"]),
        notes=row["notes"],
        created_at=row["created_at"],
        content_hash=content_hash,
        encounter_id=encounter_id,
    )


def compute_content_hash(biomarkers: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of a biomarker set's normalized content.

    Two panels with the same canonical biomarker rows produce the same
    hash regardless of insertion order, case, or trivial unit casing.
    Values are rounded to 6 significant figures so a CSV roundtrip
    (which formats with ``%g``) reproduces an identical hash.

    Returns the first 16 hex chars of the digest — collision-free for
    realistic per-user panel counts and short enough to be readable in
    logs.
    """
    rows: list[tuple[str, str, str]] = []
    for b in biomarkers:
        name = str(b.get("name") or "").strip().lower()
        unit = str(b.get("unit") or "").strip().lower()
        try:
            value = float(b.get("value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        rows.append((name, f"{value:.6g}", unit))
    rows.sort()
    blob = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def set_panel_content_hash(
    conn: sqlite3.Connection, panel_id: int, content_hash: str | None,
) -> None:
    conn.execute(
        "UPDATE panels SET content_hash = ? WHERE id = ?",
        (content_hash, panel_id),
    )


def find_panel_by_content_hash(
    conn: sqlite3.Connection, content_hash: str,
) -> Panel | None:
    """Return any panel (draft or confirmed) matching this content hash."""
    row = conn.execute(
        "SELECT * FROM panels WHERE content_hash = ? ORDER BY id ASC LIMIT 1",
        (content_hash,),
    ).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


def recompute_panel_content_hash(
    conn: sqlite3.Connection, panel_id: int,
) -> str | None:
    """Recompute and store the content_hash for a panel from its biomarkers.

    Returns the computed hash, or ``None`` if the panel has no biomarkers.
    """
    rows = conn.execute(
        "SELECT name, value, unit FROM biomarkers WHERE panel_id = ?",
        (panel_id,),
    ).fetchall()
    if not rows:
        set_panel_content_hash(conn, panel_id, None)
        return None
    h = compute_content_hash([dict(r) for r in rows])
    set_panel_content_hash(conn, panel_id, h)
    return h


def backfill_panel_content_hashes(conn: sqlite3.Connection) -> int:
    """Populate ``content_hash`` for every panel that doesn't have one.

    One-shot helper used by the migration in
    :func:`istota.health._migrate.ensure_initialised`. Returns the number
    of panels updated.
    """
    rows = conn.execute(
        "SELECT id FROM panels WHERE content_hash IS NULL",
    ).fetchall()
    n = 0
    for r in rows:
        if recompute_panel_content_hash(conn, int(r["id"])) is not None:
            n += 1
    return n


def insert_panel(
    conn: sqlite3.Connection,
    *,
    drawn_at: str,
    lab_name: str | None = None,
    panel_type: str | None = None,
    source_file: str | None = None,
    source_mime: str | None = None,
    ocr_text: str | None = None,
    draft: bool = False,
    notes: str | None = None,
    encounter_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO panels(
            drawn_at, lab_name, panel_type, source_file, source_mime,
            ocr_text, draft, notes, encounter_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            drawn_at, lab_name, panel_type, source_file, source_mime,
            ocr_text, 1 if draft else 0, notes, encounter_id,
        ),
    )
    return int(cur.lastrowid)


def get_panel(conn: sqlite3.Connection, panel_id: int) -> Panel | None:
    row = conn.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


def list_panels(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    include_drafts: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[Panel]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("drawn_at >= ?")
        params.append(since)
    if until:
        clauses.append("drawn_at <= ?")
        params.append(until)
    if not include_drafts:
        clauses.append("draft = 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM panels {where} ORDER BY drawn_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_panel(r) for r in rows]


def panel_counts(conn: sqlite3.Connection, panel_id: int) -> tuple[int, int]:
    """Return (biomarker_count, flagged_count) for a panel."""
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN flag IS NOT NULL AND flag != '' THEN 1 ELSE 0 END) AS flagged
        FROM biomarkers
        WHERE panel_id = ?
        """,
        (panel_id,),
    ).fetchone()
    return int(row["total"] or 0), int(row["flagged"] or 0)


def find_panel_collision(
    conn: sqlite3.Connection, *, drawn_at: str, lab_name: str | None,
) -> Panel | None:
    """Find an existing panel matching (drawn_at, lab_name).

    Used by the upload flow's collision check. Treats NULL lab_name and
    empty string as equal.
    """
    lab = lab_name or ""
    row = conn.execute(
        """
        SELECT * FROM panels
        WHERE drawn_at = ? AND COALESCE(lab_name, '') = ?
        ORDER BY id DESC LIMIT 1
        """,
        (drawn_at, lab),
    ).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


_UNSET: Any = object()


def update_panel(
    conn: sqlite3.Connection,
    panel_id: int,
    *,
    drawn_at: str | None = None,
    lab_name: str | None = None,
    panel_type: str | None = None,
    notes: str | None = None,
    draft: bool | None = None,
    encounter_id: Any = _UNSET,
) -> int:
    fields: list[str] = []
    params: list[Any] = []
    if drawn_at is not None:
        fields.append("drawn_at = ?")
        params.append(drawn_at)
    if lab_name is not None:
        fields.append("lab_name = ?")
        params.append(lab_name)
    if panel_type is not None:
        fields.append("panel_type = ?")
        params.append(panel_type)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if draft is not None:
        fields.append("draft = ?")
        params.append(1 if draft else 0)
    if encounter_id is not _UNSET:
        fields.append("encounter_id = ?")
        params.append(encounter_id)
    if not fields:
        return 0
    params.append(panel_id)
    cur = conn.execute(
        f"UPDATE panels SET {', '.join(fields)} WHERE id = ?", params,
    )
    return cur.rowcount


def delete_panel(conn: sqlite3.Connection, panel_id: int) -> int:
    cur = conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
    return cur.rowcount


# -- biomarkers --------------------------------------------------------------


def _row_to_biomarker(row: sqlite3.Row) -> Biomarker:
    return Biomarker(
        id=row["id"],
        panel_id=row["panel_id"],
        name=row["name"],
        display_name=row["display_name"],
        value=float(row["value"]),
        unit=row["unit"],
        ref_range_low=row["ref_range_low"],
        ref_range_high=row["ref_range_high"],
        flag=row["flag"],
        created_at=row["created_at"],
    )


def insert_biomarker(
    conn: sqlite3.Connection,
    *,
    panel_id: int,
    name: str,
    value: float,
    unit: str,
    display_name: str | None = None,
    ref_range_low: float | None = None,
    ref_range_high: float | None = None,
    flag: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO biomarkers(
            panel_id, name, display_name, value, unit,
            ref_range_low, ref_range_high, flag
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            panel_id, name, display_name, value, unit,
            ref_range_low, ref_range_high, flag,
        ),
    )
    return int(cur.lastrowid)


def replace_biomarkers(
    conn: sqlite3.Connection,
    panel_id: int,
    biomarkers: list[dict],
) -> int:
    """Delete all biomarkers for a panel and insert the new list.

    Used by the OCR review flow when the user confirms / edits the extracted
    table. Also refreshes the panel's ``content_hash`` so dedup catches
    later imports of the same content. Returns the number of rows
    inserted.
    """
    conn.execute("DELETE FROM biomarkers WHERE panel_id = ?", (panel_id,))
    inserted = 0
    for b in biomarkers:
        insert_biomarker(
            conn,
            panel_id=panel_id,
            name=str(b["name"]),
            value=float(b["value"]),
            unit=str(b["unit"]),
            display_name=b.get("display_name"),
            ref_range_low=b.get("ref_range_low"),
            ref_range_high=b.get("ref_range_high"),
            flag=b.get("flag"),
        )
        inserted += 1
    recompute_panel_content_hash(conn, panel_id)
    return inserted


def list_biomarkers_for_panel(
    conn: sqlite3.Connection, panel_id: int,
) -> list[Biomarker]:
    rows = conn.execute(
        "SELECT * FROM biomarkers WHERE panel_id = ? ORDER BY name COLLATE NOCASE",
        (panel_id,),
    ).fetchall()
    return [_row_to_biomarker(r) for r in rows]


def biomarker_trend(
    conn: sqlite3.Connection,
    *,
    name: str,
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[Biomarker, str]]:
    """Time series for a biomarker.

    Excludes draft panels so unreviewed extractions don't pollute the
    trend. Returns ``[(biomarker, drawn_at), …]`` sorted ascending.
    """
    clauses = ["b.name = ?", "p.draft = 0"]
    params: list[Any] = [name]
    if since:
        clauses.append("p.drawn_at >= ?")
        params.append(since)
    if until:
        clauses.append("p.drawn_at <= ?")
        params.append(until)
    rows = conn.execute(
        f"""
        SELECT b.*, p.drawn_at AS panel_drawn_at
        FROM biomarkers b
        JOIN panels p ON p.id = b.panel_id
        WHERE {' AND '.join(clauses)}
        ORDER BY p.drawn_at ASC, b.id ASC
        """,
        params,
    ).fetchall()
    out: list[tuple[Biomarker, str]] = []
    for r in rows:
        out.append((_row_to_biomarker(r), r["panel_drawn_at"]))
    return out


def flagged_biomarkers_latest(
    conn: sqlite3.Connection, limit: int = 50,
) -> list[tuple[Biomarker, Panel]]:
    """Most recent flagged biomarker per name (across confirmed panels)."""
    rows = conn.execute(
        """
        SELECT b.*, p.drawn_at AS panel_drawn_at, p.lab_name AS panel_lab,
               p.panel_type AS panel_type, p.id AS panel_id_full
        FROM biomarkers b
        JOIN panels p ON p.id = b.panel_id
        WHERE p.draft = 0
          AND b.flag IS NOT NULL AND b.flag != ''
          AND b.id IN (
            SELECT MAX(b2.id) FROM biomarkers b2
            JOIN panels p2 ON p2.id = b2.panel_id
            WHERE p2.draft = 0
              AND b2.flag IS NOT NULL AND b2.flag != ''
            GROUP BY b2.name
          )
        ORDER BY p.drawn_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[tuple[Biomarker, Panel]] = []
    for r in rows:
        b = _row_to_biomarker(r)
        p = Panel(
            id=r["panel_id_full"],
            drawn_at=r["panel_drawn_at"],
            lab_name=r["panel_lab"],
            panel_type=r["panel_type"],
            source_file=None,
            source_mime=None,
            ocr_text=None,
            draft=False,
            notes=None,
        )
        out.append((b, p))
    return out


# -- biomarker_refs ----------------------------------------------------------


def _row_to_ref(row: sqlite3.Row) -> BiomarkerRef:
    aliases_raw = row["aliases"] or ""
    try:
        aliases = json.loads(aliases_raw) if aliases_raw else []
    except (ValueError, TypeError):
        aliases = []
    return BiomarkerRef(
        name=row["name"],
        display_name=row["display_name"],
        category=row["category"],
        default_unit=row["default_unit"],
        ref_range_low=row["ref_range_low"],
        ref_range_high=row["ref_range_high"],
        ref_range_low_m=row["ref_range_low_m"],
        ref_range_high_m=row["ref_range_high_m"],
        ref_range_low_f=row["ref_range_low_f"],
        ref_range_high_f=row["ref_range_high_f"],
        aliases=list(aliases),
        description=row["description"],
    )


def upsert_biomarker_ref(conn: sqlite3.Connection, ref: dict) -> None:
    aliases_json = json.dumps(ref.get("aliases") or [])
    conn.execute(
        """
        INSERT INTO biomarker_refs(
            name, display_name, category, default_unit,
            ref_range_low, ref_range_high,
            ref_range_low_m, ref_range_high_m,
            ref_range_low_f, ref_range_high_f,
            aliases, description
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            category = excluded.category,
            default_unit = excluded.default_unit,
            ref_range_low = excluded.ref_range_low,
            ref_range_high = excluded.ref_range_high,
            ref_range_low_m = excluded.ref_range_low_m,
            ref_range_high_m = excluded.ref_range_high_m,
            ref_range_low_f = excluded.ref_range_low_f,
            ref_range_high_f = excluded.ref_range_high_f,
            aliases = excluded.aliases,
            description = excluded.description
        """,
        (
            ref["name"], ref["display_name"], ref["category"], ref["default_unit"],
            ref.get("ref_range_low"), ref.get("ref_range_high"),
            ref.get("ref_range_low_m"), ref.get("ref_range_high_m"),
            ref.get("ref_range_low_f"), ref.get("ref_range_high_f"),
            aliases_json, ref.get("description"),
        ),
    )


def list_biomarker_refs(conn: sqlite3.Connection) -> list[BiomarkerRef]:
    rows = conn.execute(
        "SELECT * FROM biomarker_refs ORDER BY category, display_name COLLATE NOCASE",
    ).fetchall()
    return [_row_to_ref(r) for r in rows]


def get_biomarker_ref(
    conn: sqlite3.Connection, name: str,
) -> BiomarkerRef | None:
    row = conn.execute(
        "SELECT * FROM biomarker_refs WHERE name = ?", (name,),
    ).fetchone()
    if not row:
        return None
    return _row_to_ref(row)


def find_biomarker_ref_by_alias(
    conn: sqlite3.Connection, candidate: str,
) -> BiomarkerRef | None:
    """Match by canonical name first, then by any alias (case-insensitive)."""
    direct = get_biomarker_ref(conn, candidate)
    if direct:
        return direct
    needle = candidate.strip().lower()
    if not needle:
        return None
    for ref in list_biomarker_refs(conn):
        if ref.name.lower() == needle:
            return ref
        for a in ref.aliases:
            if a.lower() == needle:
                return ref
    return None


# -- health_settings ---------------------------------------------------------


# Whitelisted keys with the JSON shape they expect. The API copies values
# verbatim; validation/coercion stays in the route layer.
SETTINGS_KEYS = ("dob", "height_cm", "sex", "display_units")


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key, value FROM health_settings",
    ).fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            out[r["key"]] = r["value"]
    return out


def set_setting(
    conn: sqlite3.Connection, key: str, value: Any,
) -> None:
    """Upsert a single setting. Value is JSON-encoded."""
    conn.execute(
        """
        INSERT INTO health_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, json.dumps(value)),
    )


def delete_setting(conn: sqlite3.Connection, key: str) -> int:
    cur = conn.execute("DELETE FROM health_settings WHERE key = ?", (key,))
    return cur.rowcount


# -- biomarker_explainers ----------------------------------------------------


def get_biomarker_explainer(
    conn: sqlite3.Connection, name: str, direction: str,
) -> dict | None:
    """Return the cached explainer for a biomarker × direction, or None."""
    row = conn.execute(
        """
        SELECT name, direction, summary, causes_json, mitigations_json, generated_at
        FROM biomarker_explainers WHERE name = ? AND direction = ?
        """,
        (name, direction),
    ).fetchone()
    if not row:
        return None
    try:
        causes = json.loads(row["causes_json"] or "[]")
    except (ValueError, TypeError):
        causes = []
    try:
        mitigations = json.loads(row["mitigations_json"] or "[]")
    except (ValueError, TypeError):
        mitigations = []
    return {
        "name": row["name"],
        "direction": row["direction"],
        "summary": row["summary"],
        "causes": list(causes) if isinstance(causes, list) else [],
        "mitigations": list(mitigations) if isinstance(mitigations, list) else [],
        "generated_at": row["generated_at"],
    }


def save_biomarker_explainer(
    conn: sqlite3.Connection,
    *,
    name: str,
    direction: str,
    summary: str,
    causes: list[str],
    mitigations: list[str],
) -> None:
    """Upsert an explainer payload. Callers commit."""
    conn.execute(
        """
        INSERT INTO biomarker_explainers(
            name, direction, summary, causes_json, mitigations_json
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name, direction) DO UPDATE SET
            summary = excluded.summary,
            causes_json = excluded.causes_json,
            mitigations_json = excluded.mitigations_json,
            generated_at = datetime('now')
        """,
        (
            name, direction, summary,
            json.dumps(causes or []),
            json.dumps(mitigations or []),
        ),
    )


# -- encounters --------------------------------------------------------------


_ENCOUNTER_UPDATE_FIELDS = (
    "encounter_date", "encounter_type", "provider", "facility",
    "specialty", "reason", "notes",
)


def _row_to_encounter(row: sqlite3.Row) -> Encounter:
    return Encounter(
        id=row["id"],
        encounter_date=row["encounter_date"],
        encounter_type=row["encounter_type"],
        provider=row["provider"],
        facility=row["facility"],
        specialty=row["specialty"],
        reason=row["reason"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def insert_encounter(
    conn: sqlite3.Connection,
    *,
    encounter_date: str,
    encounter_type: str,
    provider: str | None = None,
    facility: str | None = None,
    specialty: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
    dedup_key: str | None = None,
) -> int:
    # If a dedup_key is supplied and already exists, return the existing id
    # rather than insert a duplicate — used by the deferred-op replayer to
    # make retry-after-partial-success idempotent.
    if dedup_key is not None:
        row = conn.execute(
            "SELECT id FROM encounters WHERE dedup_key = ?", (dedup_key,),
        ).fetchone()
        if row is not None:
            return int(row[0])
    cur = conn.execute(
        """
        INSERT INTO encounters(
            encounter_date, encounter_type, provider, facility,
            specialty, reason, notes, dedup_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            encounter_date, encounter_type, provider, facility,
            specialty, reason, notes, dedup_key,
        ),
    )
    return int(cur.lastrowid)


def get_encounter(conn: sqlite3.Connection, encounter_id: int) -> Encounter | None:
    row = conn.execute(
        "SELECT * FROM encounters WHERE id = ?", (encounter_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_encounter(row)


def list_encounters(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    encounter_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Encounter]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("encounter_date >= ?")
        params.append(since)
    if until:
        clauses.append("encounter_date <= ?")
        params.append(until)
    if encounter_type:
        clauses.append("encounter_type = ?")
        params.append(encounter_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM encounters {where} "
        f"ORDER BY encounter_date DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_encounter(r) for r in rows]


def update_encounter(
    conn: sqlite3.Connection, encounter_id: int, **kwargs: Any,
) -> int:
    fields: list[str] = []
    params: list[Any] = []
    for k in _ENCOUNTER_UPDATE_FIELDS:
        if k not in kwargs:
            continue
        v = kwargs[k]
        # ``encounter_date`` and ``encounter_type`` are NOT NULL; the rest
        # accept explicit None so callers can clear them.
        if k in ("encounter_date", "encounter_type") and v is None:
            continue
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        return 0
    params.append(encounter_id)
    cur = conn.execute(
        f"UPDATE encounters SET {', '.join(fields)} WHERE id = ?", params,
    )
    return cur.rowcount


def delete_encounter(conn: sqlite3.Connection, encounter_id: int) -> int:
    cur = conn.execute("DELETE FROM encounters WHERE id = ?", (encounter_id,))
    if cur.rowcount:
        unlink_entity_documents(conn, "encounter", encounter_id)
    return cur.rowcount


def panels_for_encounter(
    conn: sqlite3.Connection, encounter_id: int,
) -> list[Panel]:
    rows = conn.execute(
        "SELECT * FROM panels WHERE encounter_id = ? "
        "ORDER BY drawn_at DESC, id DESC",
        (encounter_id,),
    ).fetchall()
    return [_row_to_panel(r) for r in rows]


# -- diagnoses ---------------------------------------------------------------


_DIAGNOSIS_STATUSES = ("active", "resolved", "chronic")

_DIAGNOSIS_UPDATE_FIELDS = (
    "name", "icd10", "status", "date_diagnosed", "date_resolved",
    "encounter_id", "severity", "notes",
)


def _row_to_diagnosis(row: sqlite3.Row) -> Diagnosis:
    return Diagnosis(
        id=row["id"],
        name=row["name"],
        icd10=row["icd10"],
        status=row["status"],
        date_diagnosed=row["date_diagnosed"],
        date_resolved=row["date_resolved"],
        encounter_id=row["encounter_id"],
        severity=row["severity"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def _normalize_condition_name(name: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation."""
    return " ".join(name.strip().lower().split()).rstrip(".,;:")


def _normalize_icd10(code: str | None) -> str | None:
    """Uppercase and strip an ICD10 code for identity comparison."""
    if not code:
        return None
    c = code.strip().upper().replace(" ", "")
    return c or None


def _find_matching_diagnosis(
    conn: sqlite3.Connection, *, name: str, icd10: str | None,
) -> sqlite3.Row | None:
    """Locate a clinically-equivalent existing diagnosis, if any.

    Identity is ICD10-first: when both the incoming row and a candidate
    carry a code, they match only when the normalized codes are equal (and
    a differing code means *not* the same condition, even if the names
    agree). When either side lacks a code, fall back to normalized-name
    equality.
    """
    inc_name = _normalize_condition_name(name)
    inc_icd = _normalize_icd10(icd10)
    rows = conn.execute(
        "SELECT id, name, icd10, severity, date_resolved FROM diagnoses",
    ).fetchall()
    for r in rows:
        ex_icd = _normalize_icd10(r["icd10"])
        if inc_icd and ex_icd:
            if inc_icd == ex_icd:
                return r
        elif inc_name == _normalize_condition_name(r["name"]):
            return r
    return None


def _backfill_null_columns(
    conn: sqlite3.Connection, table: str, row: sqlite3.Row,
    candidates: dict[str, Any],
) -> None:
    """Set each candidate column on ``row`` only where it is currently null.

    Enrich-on-merge: a second source can fill a gap the first left blank,
    but never overwrites a value already recorded.
    """
    updates = {
        col: val for col, val in candidates.items()
        if val is not None and row[col] is None
    }
    if not updates:
        return
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(
        f"UPDATE {table} SET {set_clause} WHERE id = ?",  # noqa: S608 (fixed cols)
        (*updates.values(), row["id"]),
    )


def insert_diagnosis(
    conn: sqlite3.Connection,
    *,
    name: str,
    status: str = "active",
    icd10: str | None = None,
    date_diagnosed: str | None = None,
    date_resolved: str | None = None,
    encounter_id: int | None = None,
    severity: str | None = None,
    notes: str | None = None,
    dedup_key: str | None = None,
    reconcile: bool = False,
) -> int:
    if status not in _DIAGNOSIS_STATUSES:
        raise ValueError(f"unknown diagnosis status: {status!r}")
    if dedup_key is not None:
        row = conn.execute(
            "SELECT id FROM diagnoses WHERE dedup_key = ?", (dedup_key,),
        ).fetchone()
        if row is not None:
            did = int(row[0])
            if encounter_id is not None:
                link_diagnosis_encounter(conn, did, encounter_id)
            return did
    if reconcile:
        existing = _find_matching_diagnosis(conn, name=name, icd10=icd10)
        if existing is not None:
            _backfill_null_columns(
                conn, "diagnoses", existing,
                {
                    "icd10": icd10.strip() if icd10 else None,
                    "severity": severity,
                    "date_resolved": date_resolved,
                },
            )
            did = int(existing["id"])
            # The whole point of the many-to-many: a condition mentioned again
            # at a later visit reconciles onto the same row, and that visit is
            # an ADDITIONAL link. This used to return here, and because
            # `_backfill_null_columns` deliberately leaves a populated
            # `encounter_id` alone, every encounter after the first was lost.
            if encounter_id is not None:
                link_diagnosis_encounter(conn, did, encounter_id)
            return did
    cur = conn.execute(
        """
        INSERT INTO diagnoses(
            name, icd10, status, date_diagnosed, date_resolved,
            encounter_id, severity, notes, dedup_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name, icd10, status, date_diagnosed, date_resolved,
            encounter_id, severity, notes, dedup_key,
        ),
    )
    did = int(cur.lastrowid)
    # `encounter_id` is still written above so an older build reading this DB
    # keeps working; the join row is what anything current reads.
    if encounter_id is not None:
        link_diagnosis_encounter(conn, did, encounter_id)
    return did


def get_diagnosis(conn: sqlite3.Connection, diagnosis_id: int) -> Diagnosis | None:
    row = conn.execute(
        "SELECT * FROM diagnoses WHERE id = ?", (diagnosis_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_diagnosis(row)


def list_diagnoses(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Diagnosis]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # Sort: active first (mirrors how UI surfaces them), then chronic,
    # then resolved, each ordered by most-recent date_diagnosed.
    rows = conn.execute(
        f"""
        SELECT * FROM diagnoses {where}
        ORDER BY
            CASE status
                WHEN 'active' THEN 0
                WHEN 'chronic' THEN 1
                WHEN 'resolved' THEN 2
                ELSE 3
            END,
            COALESCE(date_diagnosed, '') DESC,
            id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_diagnosis(r) for r in rows]


def update_diagnosis(
    conn: sqlite3.Connection, diagnosis_id: int, **kwargs: Any,
) -> int:
    fields: list[str] = []
    params: list[Any] = []
    for k in _DIAGNOSIS_UPDATE_FIELDS:
        if k not in kwargs:
            continue
        v = kwargs[k]
        # ``status`` and ``name`` must not be cleared to NULL; other fields
        # accept explicit None to clear.
        if k in ("name", "status") and v is None:
            continue
        if k == "status" and v not in _DIAGNOSIS_STATUSES:
            raise ValueError(f"unknown diagnosis status: {v!r}")
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        return 0
    params.append(diagnosis_id)
    cur = conn.execute(
        f"UPDATE diagnoses SET {', '.join(fields)} WHERE id = ?", params,
    )
    # Legacy shorthand kept working: setting the scalar means "this is now the
    # one encounter", clearing it means "no encounters". A caller that wants to
    # ADD a link without disturbing the others calls
    # `link_diagnosis_encounter` (or passes `encounter_ids`) instead.
    if "encounter_id" in kwargs and cur.rowcount:
        eid = kwargs["encounter_id"]
        set_diagnosis_encounters(conn, diagnosis_id, [] if eid is None else [int(eid)])
    return cur.rowcount


def delete_diagnosis(conn: sqlite3.Connection, diagnosis_id: int) -> int:
    cur = conn.execute("DELETE FROM diagnoses WHERE id = ?", (diagnosis_id,))
    if cur.rowcount:
        unlink_entity_documents(conn, "diagnosis", diagnosis_id)
    return cur.rowcount


def diagnoses_for_encounter(
    conn: sqlite3.Connection, encounter_id: int,
) -> list[Diagnosis]:
    rows = conn.execute(
        "SELECT d.* FROM diagnoses d "
        "JOIN diagnosis_encounters de ON de.diagnosis_id = d.id "
        "WHERE de.encounter_id = ? "
        "ORDER BY COALESCE(d.date_diagnosed, '') DESC, d.id DESC",
        (encounter_id,),
    ).fetchall()
    return [_row_to_diagnosis(r) for r in rows]


def encounters_for_diagnosis(
    conn: sqlite3.Connection, diagnosis_id: int,
) -> list[Encounter]:
    """Every encounter this condition has been seen at, newest first."""
    rows = conn.execute(
        "SELECT e.* FROM encounters e "
        "JOIN diagnosis_encounters de ON de.encounter_id = e.id "
        "WHERE de.diagnosis_id = ? "
        "ORDER BY e.encounter_date DESC, e.id DESC",
        (diagnosis_id,),
    ).fetchall()
    return [_row_to_encounter(r) for r in rows]


def link_diagnosis_encounter(
    conn: sqlite3.Connection, diagnosis_id: int, encounter_id: int,
) -> bool:
    """Link a condition to an encounter. True when the link is new.

    Raises ``sqlite3.IntegrityError`` when either id does not exist, so a
    caller cannot quietly create a link pointing at nothing.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO diagnosis_encounters (diagnosis_id, encounter_id) "
        "VALUES (?, ?)",
        (diagnosis_id, encounter_id),
    )
    return cur.rowcount > 0


def unlink_diagnosis_encounter(
    conn: sqlite3.Connection, diagnosis_id: int, encounter_id: int,
) -> int:
    """Drop one link. The condition and the encounter both survive."""
    cur = conn.execute(
        "DELETE FROM diagnosis_encounters "
        "WHERE diagnosis_id = ? AND encounter_id = ?",
        (diagnosis_id, encounter_id),
    )
    return cur.rowcount


def set_diagnosis_encounters(
    conn: sqlite3.Connection, diagnosis_id: int, encounter_ids: list[int],
) -> None:
    """Replace a condition's whole link set. An empty list clears it."""
    conn.execute(
        "DELETE FROM diagnosis_encounters WHERE diagnosis_id = ?",
        (diagnosis_id,),
    )
    for eid in dict.fromkeys(encounter_ids):
        link_diagnosis_encounter(conn, diagnosis_id, eid)


def encounter_ids_for_diagnoses(
    conn: sqlite3.Connection, diagnosis_ids: list[int],
) -> dict[int, list[int]]:
    """Linked encounter ids per diagnosis, so a list view costs no N+1.

    Mirrors ``document_counts_for_entities``: chunked, and a diagnosis with no
    links is simply absent rather than mapped to an empty list.
    """
    out: dict[int, list[int]] = {}
    ids = [i for i in dict.fromkeys(diagnosis_ids) if i is not None]
    for start in range(0, len(ids), _ENTITY_ID_CHUNK):
        chunk = ids[start:start + _ENTITY_ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT de.diagnosis_id, de.encounter_id FROM diagnosis_encounters de "
            f"JOIN encounters e ON e.id = de.encounter_id "
            f"WHERE de.diagnosis_id IN ({marks}) "
            f"ORDER BY e.encounter_date DESC, e.id DESC",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["diagnosis_id"], []).append(r["encounter_id"])
    return out


# -- immunizations -----------------------------------------------------------


_IMMUNIZATION_UPDATE_FIELDS = (
    "name", "product_name", "date_given", "manufacturer", "dose_label",
    "lot_number", "route", "site", "administered_by", "facility",
    "encounter_id", "cvx_code", "notes",
)


def _row_to_immunization(row: sqlite3.Row) -> Immunization:
    return Immunization(
        id=row["id"],
        name=row["name"],
        product_name=row["product_name"],
        date_given=row["date_given"],
        manufacturer=row["manufacturer"],
        dose_label=row["dose_label"],
        lot_number=row["lot_number"],
        route=row["route"],
        site=row["site"],
        administered_by=row["administered_by"],
        facility=row["facility"],
        encounter_id=row["encounter_id"],
        cvx_code=row["cvx_code"],
        notes=row["notes"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _find_matching_immunization(
    conn: sqlite3.Connection, *, name: str, date_given: str,
) -> sqlite3.Row | None:
    """Locate an existing immunization for the same vaccine on the same day.

    Keyed on normalized name + ``date_given``: a re-import of the same shot
    merges, but a genuine booster on another date is a distinct row.
    """
    inc_name = _normalize_condition_name(name)
    inc_date = (date_given or "").strip()
    rows = conn.execute("SELECT * FROM immunizations").fetchall()
    for r in rows:
        if (
            _normalize_condition_name(r["name"]) == inc_name
            and (r["date_given"] or "").strip() == inc_date
        ):
            return r
    return None


def insert_immunization(
    conn: sqlite3.Connection,
    *,
    name: str,
    date_given: str,
    product_name: str | None = None,
    manufacturer: str | None = None,
    dose_label: str | None = None,
    lot_number: str | None = None,
    route: str | None = None,
    site: str | None = None,
    administered_by: str | None = None,
    facility: str | None = None,
    encounter_id: int | None = None,
    cvx_code: str | None = None,
    notes: str | None = None,
    source: str = "manual",
    dedup_key: str | None = None,
    reconcile: bool = False,
) -> int:
    if dedup_key is not None:
        row = conn.execute(
            "SELECT id FROM immunizations WHERE dedup_key = ?", (dedup_key,),
        ).fetchone()
        if row is not None:
            return int(row[0])
    if reconcile:
        existing = _find_matching_immunization(
            conn, name=name, date_given=date_given,
        )
        if existing is not None:
            _backfill_null_columns(
                conn, "immunizations", existing,
                {
                    "product_name": product_name,
                    "manufacturer": manufacturer,
                    "dose_label": dose_label,
                    "lot_number": lot_number,
                    "route": route,
                    "site": site,
                    "administered_by": administered_by,
                    "facility": facility,
                    "cvx_code": cvx_code,
                    "notes": notes,
                },
            )
            return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO immunizations(
            name, product_name, date_given, manufacturer, dose_label,
            lot_number, route, site, administered_by, facility,
            encounter_id, cvx_code, notes, source, dedup_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name, product_name, date_given, manufacturer, dose_label,
            lot_number, route, site, administered_by, facility,
            encounter_id, cvx_code, notes, source, dedup_key,
        ),
    )
    return int(cur.lastrowid)


def get_immunization(
    conn: sqlite3.Connection, immunization_id: int,
) -> Immunization | None:
    row = conn.execute(
        "SELECT * FROM immunizations WHERE id = ?", (immunization_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_immunization(row)


def list_immunizations(
    conn: sqlite3.Connection,
    *,
    name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Immunization]:
    clauses: list[str] = []
    params: list[Any] = []
    if name:
        clauses.append("name = ?")
        params.append(name)
    if since:
        clauses.append("date_given >= ?")
        params.append(since)
    if until:
        clauses.append("date_given <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM immunizations {where} "
        f"ORDER BY date_given DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_immunization(r) for r in rows]


def update_immunization(
    conn: sqlite3.Connection, immunization_id: int, **kwargs: Any,
) -> int:
    fields: list[str] = []
    params: list[Any] = []
    for k in _IMMUNIZATION_UPDATE_FIELDS:
        if k not in kwargs:
            continue
        v = kwargs[k]
        # name and date_given are NOT NULL; explicit None is a no-op.
        if k in ("name", "date_given") and v is None:
            continue
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        return 0
    params.append(immunization_id)
    cur = conn.execute(
        f"UPDATE immunizations SET {', '.join(fields)} WHERE id = ?", params,
    )
    return cur.rowcount


def delete_immunization(
    conn: sqlite3.Connection, immunization_id: int,
) -> int:
    cur = conn.execute(
        "DELETE FROM immunizations WHERE id = ?", (immunization_id,),
    )
    if cur.rowcount:
        unlink_entity_documents(conn, "immunization", immunization_id)
    return cur.rowcount


def immunizations_for_encounter(
    conn: sqlite3.Connection, encounter_id: int,
) -> list[Immunization]:
    rows = conn.execute(
        "SELECT * FROM immunizations WHERE encounter_id = ? "
        "ORDER BY date_given DESC, id DESC",
        (encounter_id,),
    ).fetchall()
    return [_row_to_immunization(r) for r in rows]


# -- immunization_refs -------------------------------------------------------


def _row_to_immunization_ref(row: sqlite3.Row) -> ImmunizationRef:
    aliases_raw = row["aliases"] or ""
    try:
        aliases = json.loads(aliases_raw) if aliases_raw else []
    except (ValueError, TypeError):
        aliases = []
    return ImmunizationRef(
        name=row["name"],
        display_name=row["display_name"],
        category=row["category"],
        schedule=row["schedule"],
        interval_days=row["interval_days"],
        primary_series_doses=row["primary_series_doses"],
        aliases=list(aliases),
        description=row["description"],
        typical_age_range=row["typical_age_range"],
    )


def upsert_immunization_ref(conn: sqlite3.Connection, ref: dict) -> None:
    aliases_json = json.dumps(ref.get("aliases") or [])
    conn.execute(
        """
        INSERT INTO immunization_refs(
            name, display_name, category, schedule, interval_days,
            primary_series_doses, aliases, description, typical_age_range
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            category = excluded.category,
            schedule = excluded.schedule,
            interval_days = excluded.interval_days,
            primary_series_doses = excluded.primary_series_doses,
            aliases = excluded.aliases,
            description = excluded.description,
            typical_age_range = excluded.typical_age_range
        """,
        (
            ref["name"], ref["display_name"], ref["category"], ref["schedule"],
            ref.get("interval_days"), ref.get("primary_series_doses"),
            aliases_json, ref.get("description"), ref.get("typical_age_range"),
        ),
    )


def list_immunization_refs(
    conn: sqlite3.Connection,
) -> list[ImmunizationRef]:
    rows = conn.execute(
        "SELECT * FROM immunization_refs "
        "ORDER BY category, display_name COLLATE NOCASE",
    ).fetchall()
    return [_row_to_immunization_ref(r) for r in rows]


def get_immunization_ref(
    conn: sqlite3.Connection, name: str,
) -> ImmunizationRef | None:
    row = conn.execute(
        "SELECT * FROM immunization_refs WHERE name = ?", (name,),
    ).fetchone()
    if not row:
        return None
    return _row_to_immunization_ref(row)


def find_immunization_ref_by_alias(
    conn: sqlite3.Connection, candidate: str,
) -> ImmunizationRef | None:
    """Match by canonical name first, then by any alias (case-insensitive)."""
    direct = get_immunization_ref(conn, candidate)
    if direct:
        return direct
    needle = candidate.strip().lower()
    if not needle:
        return None
    for ref in list_immunization_refs(conn):
        if ref.name.lower() == needle:
            return ref
        for a in ref.aliases:
            if a.lower() == needle:
                return ref
    return None




# -- documents ---------------------------------------------------------------


# Closed set. An entity_id carries no foreign key (it points at one of three
# tables), so this tuple is the only thing standing between a typo and a link
# row nothing will ever read. `panel` is deliberately absent — panels keep
# their own source_file column and /panels/{id}/source route (D1).
DOCUMENT_ENTITY_TYPES = ("encounter", "diagnosis", "immunization")

# The table each entity type's records live in. Declared here, beside the tuple
# above, so `DOCUMENT_ENTITY_TYPES` and this map cannot disagree — a type added
# to one and not the other is caught by `_check_entity_tables` at import rather
# than by a KeyError inside a request.
_ENTITY_TABLES = {
    "encounter": "encounters",
    "diagnosis": "diagnoses",
    "immunization": "immunizations",
}

# The row converter for each, same reasoning.
_ENTITY_ROW_CONVERTERS = {
    "encounter": _row_to_encounter,
    "diagnosis": _row_to_diagnosis,
    "immunization": _row_to_immunization,
}

for _name, _map in (("tables", _ENTITY_TABLES),
                    ("row converters", _ENTITY_ROW_CONVERTERS)):
    if set(_map) != set(DOCUMENT_ENTITY_TYPES):
        raise RuntimeError(
            f"document entity {_name} disagree with DOCUMENT_ENTITY_TYPES: "
            f"{sorted(set(_map) ^ set(DOCUMENT_ENTITY_TYPES))}",
        )
del _name, _map

# Chunk size for every `IN (...)` over an id list below. SQLite's default
# SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds.
_ENTITY_ID_CHUNK = 500


def _check_entity_type(entity_type: str) -> str:
    if entity_type not in DOCUMENT_ENTITY_TYPES:
        raise ValueError(f"unknown entity type: {entity_type!r}")
    return entity_type


def entity_exists(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
) -> bool:
    """Does the record a document would attach to actually exist?

    ``document_links.entity_id`` is polymorphic and carries no FK, so nothing
    in SQLite stops a link pointing at a row that was never there. A link to a
    nonexistent entity is worse than a plain error: the document is invisible
    on every page *and* permanently exempt from the orphan sweep, because it
    does have a link. Every writer checks this first.
    """
    _check_entity_type(entity_type)
    table = _ENTITY_TABLES[entity_type]
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ?", (int(entity_id),),
    ).fetchone()
    return row is not None


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        filename=row["filename"],
        original_filename=row["original_filename"],
        mime=row["mime"],
        byte_size=row["byte_size"],
        content_hash=row["content_hash"],
        stored_path=row["stored_path"],
        ocr_text=row["ocr_text"],
        source=row["source"],
        notes=row["notes"],
        created_at=row["created_at"],
        last_touched_at=(
            row["last_touched_at"]
            if "last_touched_at" in row.keys() else row["created_at"]
        ),
    )


def insert_document(
    conn: sqlite3.Connection,
    *,
    filename: str,
    mime: str,
    byte_size: int,
    content_hash: str,
    stored_path: str,
    original_filename: str | None = None,
    ocr_text: str | None = None,
    source: str = "manual",
    notes: str | None = None,
) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO documents(filename, original_filename, mime, byte_size, "
        "content_hash, stored_path, ocr_text, source, notes, created_at, "
        "last_touched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            filename, original_filename, mime, int(byte_size), content_hash,
            stored_path, ocr_text, source, notes, now, now,
        ),
    )
    return int(cur.lastrowid)


def touch_document(conn: sqlite3.Connection, document_id: int) -> None:
    """Restart the document's orphan clock.

    Called whenever something references it — a link, an unlink, or a fresh
    upload that deduped onto it. The sweep measures its window from this
    stamp, so touching is what keeps a mid-correction or mid-import document
    alive (see the column comment in SCHEMA_SQL).
    """
    conn.execute(
        "UPDATE documents SET last_touched_at = ? WHERE id = ?",
        (_now(), int(document_id)),
    )


def get_document(
    conn: sqlite3.Connection, document_id: int,
) -> Document | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,),
    ).fetchone()
    return _row_to_document(row) if row else None


def find_document_by_hash(
    conn: sqlite3.Connection, content_hash: str,
) -> Document | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE content_hash = ?", (content_hash,),
    ).fetchone()
    return _row_to_document(row) if row else None


def list_documents(
    conn: sqlite3.Connection, *, limit: int = 200, offset: int = 0,
) -> list[Document]:
    rows = conn.execute(
        "SELECT * FROM documents ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_document(r) for r in rows]


def documents_for_entity(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
) -> list[Document]:
    _check_entity_type(entity_type)
    rows = conn.execute(
        "SELECT d.* FROM documents d "
        "JOIN document_links l ON l.document_id = d.id "
        "WHERE l.entity_type = ? AND l.entity_id = ? "
        "ORDER BY d.created_at DESC, d.id DESC",
        (entity_type, int(entity_id)),
    ).fetchall()
    return [_row_to_document(r) for r in rows]


def document_counts_for_entities(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_ids: Iterable[int],
) -> dict[int, int]:
    """Per-entity attached-document counts, for list views.

    Exists so a list page can render a paperclip badge per row without one
    query per row. Ids with no documents are absent from the result.
    """
    _check_entity_type(entity_type)
    ids = [int(i) for i in entity_ids]
    out: dict[int, int] = {}
    for start in range(0, len(ids), _ENTITY_ID_CHUNK):
        chunk = ids[start:start + _ENTITY_ID_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT entity_id, COUNT(*) AS n FROM document_links "
            f"WHERE entity_type = ? AND entity_id IN ({placeholders}) "
            "GROUP BY entity_id",
            [entity_type, *chunk],
        ).fetchall()
        for r in rows:
            out[int(r["entity_id"])] = int(r["n"])
    return out


def link_document(
    conn: sqlite3.Connection,
    document_id: int,
    entity_type: str,
    entity_id: int,
) -> bool:
    """Attach a document to an entity. Returns True when a row was created.

    Re-linking an existing pair is a no-op, not an error — the UI reports
    "already attached" rather than failing a drag-drop the user repeated.
    """
    _check_entity_type(entity_type)
    cur = conn.execute(
        "INSERT OR IGNORE INTO document_links(document_id, entity_type, "
        "entity_id, created_at) VALUES (?, ?, ?, ?)",
        (int(document_id), entity_type, int(entity_id), _now()),
    )
    touch_document(conn, document_id)
    return cur.rowcount > 0


def unlink_document(
    conn: sqlite3.Connection,
    document_id: int,
    entity_type: str,
    entity_id: int,
) -> int:
    _check_entity_type(entity_type)
    cur = conn.execute(
        "DELETE FROM document_links WHERE document_id = ? "
        "AND entity_type = ? AND entity_id = ?",
        (int(document_id), entity_type, int(entity_id)),
    )
    if cur.rowcount:
        touch_document(conn, document_id)
    return cur.rowcount


def unlink_entity_documents(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
) -> int:
    """Clear every link pointing at one entity. Called on entity delete.

    ``document_links.entity_id`` has no FK (it is polymorphic), so nothing
    cascades — this is the hand-rolled cleanup. The documents themselves
    survive; the orphan sweep collects any that are now unreferenced.
    """
    _check_entity_type(entity_type)
    affected = [
        int(r["document_id"])
        for r in conn.execute(
            "SELECT document_id FROM document_links "
            "WHERE entity_type = ? AND entity_id = ?",
            (entity_type, int(entity_id)),
        ).fetchall()
    ]
    cur = conn.execute(
        "DELETE FROM document_links WHERE entity_type = ? AND entity_id = ?",
        (entity_type, int(entity_id)),
    )
    for did in affected:
        touch_document(conn, did)
    return cur.rowcount


def entity_links_for_document(
    conn: sqlite3.Connection, document_id: int,
) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT entity_type, entity_id FROM document_links "
        "WHERE document_id = ? ORDER BY entity_type, entity_id",
        (int(document_id),),
    ).fetchall()
    return [(r["entity_type"], int(r["entity_id"])) for r in rows]


def entity_links_for_documents(
    conn: sqlite3.Connection,
    document_ids: Iterable[int],
) -> dict[int, list[tuple[str, int]]]:
    """Every listed document's links, in one chunked query.

    The bulk counterpart of :func:`entity_links_for_document`, ordered the same
    way so a list view and a detail view cannot disagree about what a document
    is attached to. A document with no links is absent from the result, as in
    :func:`document_counts_for_entities`.
    """
    # Deduped because the accumulator appends: a repeated id landing in two
    # different chunks would otherwise list its links twice.
    ids = list(dict.fromkeys(int(i) for i in document_ids))
    out: dict[int, list[tuple[str, int]]] = {}
    for start in range(0, len(ids), _ENTITY_ID_CHUNK):
        chunk = ids[start:start + _ENTITY_ID_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT document_id, entity_type, entity_id FROM document_links "
            f"WHERE document_id IN ({placeholders}) "
            "ORDER BY document_id, entity_type, entity_id",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(int(r["document_id"]), []).append(
                (r["entity_type"], int(r["entity_id"])),
            )
    return out


def entities_by_id(
    conn: sqlite3.Connection,
    entity_type: str,
    ids: Iterable[int],
) -> dict[int, Encounter | Diagnosis | Immunization]:
    """Link targets of one type, keyed by id, in one chunked query.

    Exists so a document list can label every link without one ``get_*`` per
    link. An id with no row is **absent** rather than mapped to None:
    ``document_links.entity_id`` is polymorphic and carries no FK, so a link
    can outlive its record, and the caller renders a generic label for those.
    """
    _check_entity_type(entity_type)
    table = _ENTITY_TABLES[entity_type]
    to_model = _ENTITY_ROW_CONVERTERS[entity_type]
    # Duplicates are harmless here (the result is keyed by id) but not in
    # `entity_links_for_documents`, which appends; both dedupe for one rule.
    wanted = list(dict.fromkeys(int(i) for i in ids))
    out: dict[int, Encounter | Diagnosis | Immunization] = {}
    for start in range(0, len(wanted), _ENTITY_ID_CHUNK):
        chunk = wanted[start:start + _ENTITY_ID_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE id IN ({placeholders})", chunk,
        ).fetchall()
        for r in rows:
            out[int(r["id"])] = to_model(r)
    return out


def delete_document(conn: sqlite3.Connection, document_id: int) -> int:
    """Delete the row and its links. Bytes are the caller's problem —
    :func:`istota.health.documents.delete_document_fully` does both."""
    conn.execute(
        "DELETE FROM document_links WHERE document_id = ?", (int(document_id),),
    )
    cur = conn.execute("DELETE FROM documents WHERE id = ?", (int(document_id),))
    return cur.rowcount


def orphan_document_ids(
    conn: sqlite3.Connection, *, older_than_hours: float,
) -> list[int]:
    """Documents with no links, created more than ``older_than_hours`` ago.

    Measured from ``last_touched_at`` — stamped on create, link, unlink, and
    on a dedup hit — not from ``created_at``. The delay is deliberate (D5):
    detach-then-reattach-elsewhere is a normal correction flow, and deleting
    bytes the instant the last link goes would make it lossy. Keying on
    creation instead would have destroyed any document older than the window
    the moment it was detached — the opposite of the promise. An abandoned
    import upload is still collected: nothing ever touches it after insert.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=float(older_than_hours))
    ).isoformat()
    rows = conn.execute(
        "SELECT d.id FROM documents d "
        "LEFT JOIN document_links l ON l.document_id = d.id "
        "WHERE l.document_id IS NULL "
        "AND COALESCE(d.last_touched_at, d.created_at) < ? "
        "ORDER BY d.id",
        (cutoff,),
    ).fetchall()
    return [int(r["id"]) for r in rows]
