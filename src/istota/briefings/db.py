"""SQLite layer for the native briefings module.

One DB per user at ``{ctx.db_path}`` (local disk, WAL). Schema lives inline;
``init_db`` is idempotent and walks ``_MIGRATIONS`` to bring an existing DB up
to ``SCHEMA_VERSION`` one step at a time. Mirrors :mod:`istota.feeds.db`.

Content model:

* ``briefing_blocks`` — ordered subject blocks per (logical) briefing name.
* ``briefing_block_sources`` — the 1..N sources fanning into a block.
* ``briefing_archive`` — one row per rendered briefing (the landing page).

Phase 2 substrate (``briefing_items`` / ``briefing_item_state``) ships now but
is UNUSED until the continuous-ingestion phase.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from istota import sqlite_util
from istota.briefings.models import (
    ArchivedBriefing,
    BlockSource,
    BriefingBlock,
    parse_json_dict,
    parse_json_list,
)
from istota.timestamps import iso_now as _now_iso


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS briefing_blocks (
    id INTEGER PRIMARY KEY,
    briefing_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    directive TEXT,
    render_mode TEXT NOT NULL DEFAULT 'synthesis',
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(briefing_name, position)
);

CREATE TABLE IF NOT EXISTS briefing_block_sources (
    id INTEGER PRIMARY KEY,
    block_id INTEGER NOT NULL REFERENCES briefing_blocks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(block_id, position)
);

CREATE TABLE IF NOT EXISTS briefing_archive (
    id INTEGER PRIMARY KEY,
    briefing_name TEXT NOT NULL,
    subject TEXT,
    body_md TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    task_id INTEGER,
    block_meta TEXT NOT NULL DEFAULT '{}',
    delivered_to TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_archive_name_time
    ON briefing_archive(briefing_name, generated_at DESC);

-- Phase 2 substrate (created now, UNUSED until continuous ingestion).
CREATE TABLE IF NOT EXISTS briefing_items (
    id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    dedup_key TEXT NOT NULL,
    title TEXT,
    body_md TEXT,
    summary_md TEXT,
    url TEXT,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE(source_kind, dedup_key)
);
CREATE TABLE IF NOT EXISTS briefing_item_state (
    item_id INTEGER PRIMARY KEY REFERENCES briefing_items(id) ON DELETE CASCADE,
    seen_at TEXT,
    briefing_name TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# No migrations yet — v1 is the initial schema. Future column additions append
# a (target_version, migrate_fn) tuple here (see feeds/db.py for the pattern).
_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = []


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema for the briefings DB.

    Idempotent. Safe to call on every startup. WAL is set once here (persistent
    in the file header); a relocated DELETE-mode DB is converted on first touch.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        current = _read_schema_version(conn)
        for target_version, migrate in _MIGRATIONS:
            if current < target_version:
                migrate(conn)
                logger.info(
                    "briefings_db_migrated from=v%s to=v%s", current, target_version,
                )
                current = target_version
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the persisted schema version.

    Brand-new file (no ``schema_meta``) → ``SCHEMA_VERSION`` (skip migrations,
    let ``SCHEMA_SQL`` build from scratch).
    """
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not has_meta:
        return SCHEMA_VERSION
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        return SCHEMA_VERSION
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return SCHEMA_VERSION


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the module's conventions.

    ``foreign_keys = ON`` (so the source→block cascade behaves) + ``Row``
    factory. ``journal_mode`` is set once by ``init_db`` (not re-issued here).
    """
    with sqlite_util.open_db(db_path) as conn:
        yield conn


# -- blocks -------------------------------------------------------------------


def _next_block_position(conn: sqlite3.Connection, briefing_name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p "
        "FROM briefing_blocks WHERE briefing_name = ?",
        (briefing_name,),
    ).fetchone()
    return int(row["p"])


def add_block(
    conn: sqlite3.Connection,
    *,
    briefing_name: str,
    title: str,
    directive: str | None = None,
    render_mode: str = "synthesis",
    options: dict | None = None,
    position: int | None = None,
) -> int:
    """Insert a block at ``position`` (default: append). Returns the row id."""
    if position is None:
        position = _next_block_position(conn, briefing_name)
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO briefing_blocks(
            briefing_name, position, title, directive, render_mode,
            options, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            briefing_name, position, title, directive, render_mode,
            json.dumps(options or {}), now, now,
        ),
    )
    return int(cur.fetchone()["id"])


def update_block(
    conn: sqlite3.Connection,
    block_id: int,
    *,
    title: str | None = None,
    directive: str | None = None,
    render_mode: str | None = None,
    options: dict | None = None,
) -> None:
    """Patch a block's mutable fields (only non-None args are applied)."""
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if directive is not None:
        sets.append("directive = ?")
        params.append(directive)
    if render_mode is not None:
        sets.append("render_mode = ?")
        params.append(render_mode)
    if options is not None:
        sets.append("options = ?")
        params.append(json.dumps(options))
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(block_id)
    conn.execute(
        f"UPDATE briefing_blocks SET {', '.join(sets)} WHERE id = ?",
        tuple(params),
    )


def delete_block(conn: sqlite3.Connection, block_id: int) -> None:
    """Delete a block (its sources cascade)."""
    conn.execute("DELETE FROM briefing_blocks WHERE id = ?", (block_id,))


def get_block(
    conn: sqlite3.Connection, block_id: int, *, with_sources: bool = True,
) -> BriefingBlock | None:
    row = conn.execute(
        "SELECT * FROM briefing_blocks WHERE id = ?", (block_id,)
    ).fetchone()
    if not row:
        return None
    block = _row_to_block(row)
    if with_sources:
        block.sources = list_sources(conn, block_id)
    return block


def list_blocks(
    conn: sqlite3.Connection,
    briefing_name: str,
    *,
    with_sources: bool = True,
) -> list[BriefingBlock]:
    """Return a briefing's blocks in position order (sources attached)."""
    rows = conn.execute(
        "SELECT * FROM briefing_blocks WHERE briefing_name = ? "
        "ORDER BY position ASC, id ASC",
        (briefing_name,),
    ).fetchall()
    blocks = [_row_to_block(r) for r in rows]
    if with_sources:
        for block in blocks:
            block.sources = list_sources(conn, block.id)
    return blocks


def list_briefing_names(conn: sqlite3.Connection) -> list[str]:
    """Distinct briefing names that have at least one block."""
    rows = conn.execute(
        "SELECT DISTINCT briefing_name FROM briefing_blocks "
        "ORDER BY briefing_name COLLATE NOCASE"
    ).fetchall()
    return [r["briefing_name"] for r in rows]


def list_briefing_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Briefing names with block counts and their latest archive timestamp."""
    rows = conn.execute(
        """
        SELECT
            blocks.briefing_name AS name,
            blocks.block_count,
            MAX(briefing_archive.generated_at) AS last_generated_at
        FROM (
            SELECT briefing_name, COUNT(*) AS block_count
            FROM briefing_blocks
            GROUP BY briefing_name
        ) AS blocks
        LEFT JOIN briefing_archive
            ON briefing_archive.briefing_name = blocks.briefing_name
        GROUP BY blocks.briefing_name, blocks.block_count
        ORDER BY blocks.briefing_name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {
            "name": row["name"],
            "block_count": row["block_count"],
            "last_generated_at": row["last_generated_at"],
        }
        for row in rows
    ]


def reorder_blocks(
    conn: sqlite3.Connection, briefing_name: str, ordered_ids: list[int],
) -> None:
    """Rewrite the ``position`` of a briefing's blocks to match ``ordered_ids``.

    Two-phase to dodge the ``UNIQUE(briefing_name, position)`` constraint: park
    every block at a high, collision-free position, then set final positions.
    ``ordered_ids`` must be exactly the briefing's block ids (any order); ids
    not belonging to the briefing are ignored.
    """
    valid = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM briefing_blocks WHERE briefing_name = ?",
            (briefing_name,),
        ).fetchall()
    }
    target = [bid for bid in ordered_ids if bid in valid]
    now = _now_iso()
    # Phase 1: park out of the way (offset well past any real count).
    offset = 1000000
    for i, bid in enumerate(target):
        conn.execute(
            "UPDATE briefing_blocks SET position = ?, updated_at = ? WHERE id = ?",
            (offset + i, now, bid),
        )
    # Phase 2: final compact positions.
    for i, bid in enumerate(target):
        conn.execute(
            "UPDATE briefing_blocks SET position = ?, updated_at = ? WHERE id = ?",
            (i, now, bid),
        )


# -- sources ------------------------------------------------------------------


def _next_source_position(conn: sqlite3.Connection, block_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p "
        "FROM briefing_block_sources WHERE block_id = ?",
        (block_id,),
    ).fetchone()
    return int(row["p"])


def add_source(
    conn: sqlite3.Connection,
    *,
    block_id: int,
    kind: str,
    config: dict | None = None,
    enabled: bool = True,
    position: int | None = None,
) -> int:
    """Insert a source into a block (default: append). Returns the row id."""
    if position is None:
        position = _next_source_position(conn, block_id)
    cur = conn.execute(
        """
        INSERT INTO briefing_block_sources(
            block_id, position, kind, config, enabled, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            block_id, position, kind, json.dumps(config or {}),
            1 if enabled else 0, _now_iso(),
        ),
    )
    return int(cur.fetchone()["id"])


def update_source(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    config: dict | None = None,
    enabled: bool | None = None,
) -> None:
    sets: list[str] = []
    params: list = []
    if config is not None:
        sets.append("config = ?")
        params.append(json.dumps(config))
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not sets:
        return
    params.append(source_id)
    conn.execute(
        f"UPDATE briefing_block_sources SET {', '.join(sets)} WHERE id = ?",
        tuple(params),
    )


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM briefing_block_sources WHERE id = ?", (source_id,))


def list_sources(conn: sqlite3.Connection, block_id: int) -> list[BlockSource]:
    rows = conn.execute(
        "SELECT * FROM briefing_block_sources WHERE block_id = ? "
        "ORDER BY position ASC, id ASC",
        (block_id,),
    ).fetchall()
    return [_row_to_source(r) for r in rows]


# -- archive ------------------------------------------------------------------


def insert_archive(
    conn: sqlite3.Connection,
    *,
    briefing_name: str,
    subject: str | None,
    body_md: str,
    generated_at: str | None = None,
    task_id: int | None = None,
    block_meta: dict | None = None,
    delivered_to: list[str] | None = None,
) -> int:
    """Persist one rendered briefing. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO briefing_archive(
            briefing_name, subject, body_md, generated_at, task_id,
            block_meta, delivered_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            briefing_name, subject, body_md, generated_at or _now_iso(),
            task_id, json.dumps(block_meta or {}),
            json.dumps(delivered_to or []),
        ),
    )
    return int(cur.fetchone()["id"])


def get_archived(conn: sqlite3.Connection, archive_id: int) -> ArchivedBriefing | None:
    row = conn.execute(
        "SELECT * FROM briefing_archive WHERE id = ?", (archive_id,)
    ).fetchone()
    return _row_to_archived(row) if row else None


def delete_archived(conn: sqlite3.Connection, archive_id: int) -> bool:
    """Delete a single archived briefing by id. Returns True if a row was
    removed (False when the id doesn't exist)."""
    cur = conn.execute("DELETE FROM briefing_archive WHERE id = ?", (archive_id,))
    return bool(cur.rowcount)


def list_archive(
    conn: sqlite3.Connection,
    *,
    briefing_name: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[ArchivedBriefing]:
    """Page through archived briefings, newest first."""
    clauses: list[str] = []
    params: list = []
    if briefing_name:
        clauses.append("briefing_name = ?")
        params.append(briefing_name)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT * FROM briefing_archive
        {where}
        ORDER BY generated_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_archived(r) for r in rows]


def latest_archived(
    conn: sqlite3.Connection, *, briefing_name: str | None = None,
) -> ArchivedBriefing | None:
    rows = list_archive(conn, briefing_name=briefing_name, limit=1)
    return rows[0] if rows else None


def count_archive(
    conn: sqlite3.Connection, *, briefing_name: str | None = None,
) -> int:
    clauses: list[str] = []
    params: list = []
    if briefing_name:
        clauses.append("briefing_name = ?")
        params.append(briefing_name)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM briefing_archive {where}", params,
    ).fetchone()
    return int(row["c"])


def prune_archive(
    conn: sqlite3.Connection,
    *,
    briefing_name: str,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete archived briefings for ``briefing_name`` older than the retention
    window. ``retention_days <= 0`` keeps everything (no-op). Returns the count
    deleted.
    """
    if retention_days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - retention_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    cur = conn.execute(
        "DELETE FROM briefing_archive "
        "WHERE briefing_name = ? AND generated_at < ?",
        (briefing_name, cutoff_iso),
    )
    return cur.rowcount or 0


# -- schema_meta helpers ------------------------------------------------------


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        (key, value),
    )


# -- row mappers --------------------------------------------------------------


def _row_to_block(row: sqlite3.Row) -> BriefingBlock:
    return BriefingBlock(
        id=row["id"],
        briefing_name=row["briefing_name"],
        position=row["position"],
        title=row["title"],
        directive=row["directive"],
        render_mode=row["render_mode"],
        options=parse_json_dict(row["options"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_source(row: sqlite3.Row) -> BlockSource:
    return BlockSource(
        id=row["id"],
        block_id=row["block_id"],
        position=row["position"],
        kind=row["kind"],
        config=parse_json_dict(row["config"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def _row_to_archived(row: sqlite3.Row) -> ArchivedBriefing:
    return ArchivedBriefing(
        id=row["id"],
        briefing_name=row["briefing_name"],
        subject=row["subject"],
        body_md=row["body_md"],
        generated_at=row["generated_at"],
        task_id=row["task_id"],
        block_meta=parse_json_dict(row["block_meta"]),
        delivered_to=parse_json_list(row["delivered_to"]),
    )
