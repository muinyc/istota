"""SQLite layer for the per-user location DB.

One DB per user, lives at ``{workspace}/location/data/location.db``.
The framework ``istota.db`` no longer owns location tables — only the
two global geocode caches (``geocode_cache``, ``reverse_geocode_cache``)
which remain there because their keys are not user-scoped.

``init_db`` is idempotent and sets WAL mode exactly once at creation.
``connect`` does NOT re-issue the WAL pragma — re-issuing while sibling
readers hold a transaction races and raises "database is locked"; same
rule documented in :mod:`istota.feeds.db`.

Two writers are expected: the webhook receiver (FastAPI, multi-threaded)
and the scheduler (reconcile + cleanup). All readers (web routes, skill
CLI) hit the same file.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from istota import sqlite_util
from istota.location.models import (
    Cluster,
    LocationPing,
    LocationState,
    Place,
    Visit,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 4


# Tables in FK-target-first order: places -> visits -> location_pings,
# location_state. SQLite resolves references at insert time, but having
# the targets created first matches the order in which we copy data
# during migration and keeps the schema readable.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_meters INTEGER NOT NULL DEFAULT 100,
    category TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id INTEGER REFERENCES places(id),
    place_name TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    exited_at TEXT,
    duration_sec INTEGER,
    ping_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_visits_time ON visits(entered_at);

CREATE TABLE IF NOT EXISTS location_pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    altitude REAL,
    accuracy REAL,
    vertical_accuracy REAL,
    speed REAL,
    course REAL,
    battery REAL,
    activity_type TEXT,
    wifi TEXT,
    wifi_zone INTEGER NOT NULL DEFAULT 0,
    place_id INTEGER REFERENCES places(id),
    visit_id INTEGER REFERENCES visits(id),
    source TEXT NOT NULL DEFAULT 'overland',
    client_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_location_pings_time ON location_pings(timestamp);
-- The client_id uniqueness index is created in _migrate_schema, not here.
-- See the note there.

CREATE TABLE IF NOT EXISTS dismissed_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_meters INTEGER NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS location_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_place_id INTEGER REFERENCES places(id),
    current_visit_id INTEGER REFERENCES visits(id),
    consecutive_count INTEGER DEFAULT 0,
    last_ping_place_id INTEGER REFERENCES places(id),
    exit_started_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(path: Path) -> None:
    """Create or open ``location.db``, ensure schema, set WAL mode once.

    Idempotent and concurrency-safe: serialised across threads /
    processes via an ``fcntl.flock`` on a sidecar lockfile, so concurrent
    callers don't race on the WAL pragma or the schema_only_at sentinel.

    Writes the ``schema_only_at`` sentinel on fresh creation so the
    migrator can distinguish "newly initialised empty file" from
    "pre-existing unmigrated user data".
    """
    import fcntl

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".init.lock")

    with open(lock_path, "w") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        fresh = not path.exists()
        conn = sqlite3.connect(path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            # WAL: this DB now lives on LOCAL disk (Config.module_db_path, off
            # the rclone FUSE mount), so WAL's mmap'd -shm is safe — the SIGBUS
            # that forced DELETE (ISSUE-157) was a FUSE artifact. WAL restores
            # reader/writer concurrency. Issued unconditionally so a relocated
            # DELETE-mode DB converts on first touch; no-op once WAL.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            _migrate_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) "
                "VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            # A v1 DB wrote version='1' with INSERT OR IGNORE; keep the
            # recorded version honest after an additive migration.
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'version'",
                (str(SCHEMA_VERSION),),
            )
            if fresh:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) "
                    "VALUES('schema_only_at', datetime('now'))",
                )
            conn.commit()
        finally:
            conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema migrations for pre-existing DBs.

    ``executescript(SCHEMA_SQL)`` uses ``CREATE TABLE IF NOT EXISTS``, so a
    table that already exists is never re-created and never gains new
    columns. Column additions run here, each guarded by a
    ``PRAGMA table_info`` presence check so re-running is a no-op.

    v2 (garmin-track-import-to-location): ``location_pings.source`` — a
    first-class provenance column so Overland and Garmin (and any future
    source) pings are distinguishable by a plain predicate. Existing rows
    predate multi-source ingest and are all Overland, so the DEFAULT
    backfills them correctly.

    v3 (native-ios-app): ``location_pings.client_id`` — an id the sending
    client mints per point, so a batch that was stored but whose response
    was lost can be re-sent without landing twice. Nullable, because stock
    Overland and the Garmin importer send none; existing rows correctly
    backfill to NULL, which the partial unique index excludes.

    v4 (ISSUE-229): ``location_pings.wifi_zone`` and
    ``location_pings.vertical_accuracy``. The shell marks a *declared*
    wifi-zone point on the wire so a coordinate the device never measured
    stays identifiable afterwards; the server was dropping the marker, so a
    stored row kept no trace of the substitution. ``vertical_accuracy``
    arrives on every feature and was dropped by the same omission — it is
    the invalid-vertical-fix signal ISSUE-218 had to work around with a rate
    heuristic. Both default correctly for existing rows: a measured point
    that is not so marked, and an accuracy nobody recorded.

    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(location_pings)")}
    missing = {"source", "client_id", "vertical_accuracy", "wifi_zone"} - cols
    if missing:
        # One explicit transaction around the ALTERs and the one-shot backfill
        # they gate, because SQLite DDL is transactional and the guard above is
        # the column's own presence. Without it the ALTER autocommits on its
        # own while the UPDATE waits for init_db's commit at the very end — so
        # a crash or a SQLITE_BUSY in between leaves the column added and the
        # sweep undone, and the guard then suppresses it forever. init_db calls
        # this straight after executescript (which commits) and a PRAGMA, so no
        # transaction is open here.
        conn.execute("BEGIN")
        added = sqlite_util.add_columns(conn, "location_pings", {
            "source": "TEXT NOT NULL DEFAULT 'overland'",
            "client_id": "TEXT",
            "vertical_accuracy": "REAL",
            "wifi_zone": "INTEGER NOT NULL DEFAULT 0",
        })
        # Gated on what this call added rather than on what the read above
        # found missing, which are two different questions once the two reads
        # are separated by a `BEGIN`. `add_columns` does not commit — this
        # block's own transaction is the point of it, and inside it a rival
        # cannot land the column between our read and our write: our snapshot
        # is already fixed, so its commit costs us a lock error rather than a
        # duplicate-column one, and this call raises as the check-then-ALTER
        # before it did. The gate is about the ALTERs and the backfill agreeing
        # about the same call, not about surviving a race.
        if "wifi_zone" in added:
            # Runs after the `source` ALTER above, which it filters on.
            _backfill_declared_points(conn)
        conn.commit()

    # This index cannot live in SCHEMA_SQL: executescript runs before this
    # function, so on a pre-v3 DB it would index a column the ALTER above
    # has not added yet and the whole init would fail. IF NOT EXISTS makes
    # it a no-op on every subsequent open, fresh DBs included.
    #
    # Partial so NULLs never participate. SQLite already treats NULLs as
    # distinct in a UNIQUE index, but the WHERE clause keeps the index to
    # client-identified pings only and states the intent: dedup applies to
    # points from a client that mints ids, and to nothing else.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_location_pings_client_id "
        "ON location_pings(client_id) WHERE client_id IS NOT NULL"
    )


def _backfill_declared_points(conn: sqlite3.Connection) -> None:
    """Retire the -1 altitude sentinel from rows stored before the marker.

    Rows written before v4 carry the fabricated value with nothing marking
    them, so the only handle left is the declared fix's own signature —
    which is why the ingest fix matches on ``wifi_zone`` instead and this
    sweep runs exactly once, guarded by the column's absence.

    The signature is narrow rather than a bare ``altitude = -1`` match: the
    shell builds the declared ``CLLocation`` with a horizontal accuracy of
    exactly 1 metre and a zeroed speed and course, and consumer GPS does not
    report a 1 m fix. Marking the matched rows is an inference from that
    signature, not something the row itself recorded — but a NULL altitude
    with no stated reason is the worse of the two, and the value it replaces
    was never a measurement either.
    """
    conn.execute(
        """
        UPDATE location_pings
        SET altitude = NULL, wifi_zone = 1
        WHERE source = 'overland'
          AND altitude = -1
          AND accuracy = 1
          AND speed = 0
          AND course = 0
        """
    )


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a connection to an already-initialised ``location.db``.

    Yields a row-factory-equipped connection with ``foreign_keys`` on.
    Does NOT touch ``journal_mode`` — see :func:`init_db`.

    ``busy_timeout`` is now issued explicitly, matching ``init_db`` and the
    other three module stores. It changes nothing at runtime — the 30s connect
    timeout already installed a 30s busy handler — but it stops the lock budget
    depending on an argument two lines away.
    """
    with sqlite_util.open_db(path) as conn:
        yield conn


@contextmanager
def with_geocode_conn(
    framework_db_path: Path | str, *, timeout: float = 30.0,
) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection to framework ``istota.db``.

    Used by paths that need both the per-user ``location.db`` AND the
    framework-side geocode caches (``reverse_geocode_cache``,
    ``geocode_cache``). One context manager so if we ever split the
    caches into a separate file the change lands in one place.

    ``timeout`` is the lock budget, and it is a parameter because this is the
    **framework** database rather than a per-user one. ``db.get_db``'s own
    docstring argues for a short budget here — a caller that waits 30s on
    ``istota.db`` blocks whatever thread it is on and, on the dispatch loop,
    trips the stall watchdog. The default keeps this helper's existing 30s for
    the skill and scheduler paths; a web request thread passes something
    shorter.
    """
    with sqlite_util.open_db(
        framework_db_path,
        timeout=timeout,
        busy_timeout_ms=None,
        foreign_keys=False,
    ) as conn:
        yield conn


# -- places -----------------------------------------------------------------


def get_places(conn: sqlite3.Connection) -> list[Place]:
    rows = conn.execute(
        """
        SELECT id, name, lat, lon, radius_meters, category, created_at, notes
        FROM places
        ORDER BY name
        """
    ).fetchall()
    return [Place(**dict(r)) for r in rows]


def get_place_by_id(conn: sqlite3.Connection, place_id: int) -> Place | None:
    row = conn.execute(
        """
        SELECT id, name, lat, lon, radius_meters, category, created_at, notes
        FROM places WHERE id = ?
        """,
        (place_id,),
    ).fetchone()
    if not row:
        return None
    return Place(**dict(row))


def get_place_by_name(conn: sqlite3.Connection, name: str) -> Place | None:
    row = conn.execute(
        """
        SELECT id, name, lat, lon, radius_meters, category, created_at, notes
        FROM places WHERE name = ?
        """,
        (name,),
    ).fetchone()
    if not row:
        return None
    return Place(**dict(row))


def add_place(
    conn: sqlite3.Connection,
    name: str,
    lat: float,
    lon: float,
    *,
    radius_meters: int = 25,
    category: str | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO places (name, lat, lon, radius_meters, category, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, lat, lon, radius_meters, category, notes),
    )
    return int(cur.lastrowid or 0)


def upsert_place(
    conn: sqlite3.Connection,
    name: str,
    lat: float,
    lon: float,
    *,
    radius_meters: int = 25,
    category: str | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO places (name, lat, lon, radius_meters, category, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET
            lat = excluded.lat,
            lon = excluded.lon,
            radius_meters = excluded.radius_meters,
            category = excluded.category,
            notes = excluded.notes
        RETURNING id
        """,
        (name, lat, lon, radius_meters, category, notes),
    )
    return int(cur.fetchone()[0])


def update_place(
    conn: sqlite3.Connection,
    place_id: int,
    *,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_meters: int | None = None,
    category: str | None = None,
    notes: str | None = None,
) -> bool:
    fields: list[str] = []
    values: list = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if lat is not None:
        fields.append("lat = ?")
        values.append(lat)
    if lon is not None:
        fields.append("lon = ?")
        values.append(lon)
    if radius_meters is not None:
        fields.append("radius_meters = ?")
        values.append(radius_meters)
    if category is not None:
        fields.append("category = ?")
        values.append(category)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if not fields:
        return False
    values.append(place_id)
    cur = conn.execute(
        f"UPDATE places SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    return cur.rowcount > 0


def delete_place(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM places WHERE name = ?", (name,))
    return cur.rowcount > 0


def delete_place_by_id(conn: sqlite3.Connection, place_id: int) -> bool:
    cur = conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    return cur.rowcount > 0


def nullify_place_on_pings(conn: sqlite3.Connection, place_id: int) -> int:
    cur = conn.execute(
        "UPDATE location_pings SET place_id = NULL WHERE place_id = ?",
        (place_id,),
    )
    return cur.rowcount or 0


# -- pings ------------------------------------------------------------------


def insert_ping(
    conn: sqlite3.Connection,
    timestamp: str,
    lat: float,
    lon: float,
    *,
    altitude: float | None = None,
    accuracy: float | None = None,
    vertical_accuracy: float | None = None,
    speed: float | None = None,
    course: float | None = None,
    battery: float | None = None,
    activity_type: str | None = None,
    wifi: str | None = None,
    wifi_zone: bool = False,
    place_id: int | None = None,
    visit_id: int | None = None,
    source: str = "overland",
    received_at: str | None = None,
    client_id: str | None = None,
) -> int:
    """Insert a ping, returning its rowid — or ``0`` when nothing was written.

    ``source`` tags provenance ('overland' native, 'garmin' imported).
    ``received_at`` overrides the schema ``datetime('now')`` default — the
    Garmin importer passes the point's own historical timestamp so retention
    (which deletes by ``received_at``) treats a backfilled ping as
    contemporaneous, not as 'arrived today'.

    ``client_id`` is an id the sending client mints per point. The insert is
    ``OR IGNORE`` against the partial unique index on it, which makes a
    replayed batch idempotent: a device that stored a batch but lost the
    response re-sends it, and the second delivery writes nothing.

    **A caller that acts on the ping must branch on the return value.**
    ``0`` means the point was already here, and treating it as a fresh id
    would run whatever follows a second time — the visit state machine in
    particular, where a replay would manufacture an arrival. ``lastrowid``
    is not a substitute: after an ignored insert it still holds the previous
    successful one, so the rowcount is what distinguishes them.

    A ``NULL`` client_id never collides (the index excludes it), so stock
    Overland and the Garmin importer are unaffected — including their
    byte-identical repeat points, which are genuine and must all land.
    """
    if received_at is None:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO location_pings (
                timestamp, lat, lon, altitude, accuracy, vertical_accuracy,
                speed, course, battery, activity_type, wifi, wifi_zone,
                place_id, visit_id, source, client_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, lat, lon, altitude, accuracy, vertical_accuracy,
                speed, course, battery, activity_type, wifi, int(wifi_zone),
                place_id, visit_id, source, client_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO location_pings (
                timestamp, received_at, lat, lon, altitude, accuracy,
                vertical_accuracy, speed, course, battery, activity_type,
                wifi, wifi_zone, place_id, visit_id, source, client_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, received_at, lat, lon, altitude, accuracy,
                vertical_accuracy, speed, course, battery, activity_type,
                wifi, int(wifi_zone), place_id, visit_id, source, client_id,
            ),
        )
    if cur.rowcount == 0:
        return 0
    return int(cur.lastrowid or 0)


def update_ping_place(
    conn: sqlite3.Connection,
    ping_id: int,
    place_id: int | None,
    visit_id: int | None = None,
) -> None:
    conn.execute(
        "UPDATE location_pings SET place_id = ?, visit_id = ? WHERE id = ?",
        (place_id, visit_id, ping_id),
    )


_PING_COLUMNS = """
        id, timestamp, received_at, lat, lon, altitude, accuracy,
        vertical_accuracy, speed, course, battery, activity_type, wifi,
        wifi_zone, place_id, visit_id, client_id
"""


def _ping_from_row(row: sqlite3.Row) -> LocationPing:
    """SQLite has no boolean type, so ``wifi_zone`` arrives as 0 or 1."""
    fields = dict(row)
    fields["wifi_zone"] = bool(fields["wifi_zone"])
    return LocationPing(**fields)


def get_latest_ping(conn: sqlite3.Connection) -> LocationPing | None:
    row = conn.execute(
        f"""
        SELECT {_PING_COLUMNS}
        FROM location_pings
        ORDER BY timestamp DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return _ping_from_row(row)


def get_pings(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[LocationPing]:
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT {_PING_COLUMNS}
        FROM location_pings
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_ping_from_row(r) for r in rows]


def cleanup_old_pings(
    conn: sqlite3.Connection, retention_days: int = 365,
) -> int:
    cur = conn.execute(
        "DELETE FROM location_pings WHERE received_at < datetime('now', ? || ' days')",
        (f"-{retention_days}",),
    )
    return cur.rowcount or 0


# -- visits -----------------------------------------------------------------


def open_visit(
    conn: sqlite3.Connection,
    place_id: int | None,
    place_name: str,
    entered_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO visits (place_id, place_name, entered_at, ping_count)
        VALUES (?, ?, ?, 1)
        """,
        (place_id, place_name, entered_at),
    )
    return int(cur.lastrowid or 0)


def close_visit(
    conn: sqlite3.Connection,
    visit_id: int,
    exited_at: str,
    duration_sec: int | None = None,
    ping_count: int | None = None,
) -> None:
    """Close a visit. If ``duration_sec`` is None, derive it from
    ``entered_at``. If ``ping_count`` is provided, overwrite it."""
    if duration_sec is None:
        if ping_count is None:
            conn.execute(
                """
                UPDATE visits SET
                    exited_at = ?,
                    duration_sec = CAST(
                        (julianday(?) - julianday(entered_at)) * 86400 AS INTEGER
                    )
                WHERE id = ?
                """,
                (exited_at, exited_at, visit_id),
            )
        else:
            conn.execute(
                """
                UPDATE visits SET
                    exited_at = ?,
                    duration_sec = CAST(
                        (julianday(?) - julianday(entered_at)) * 86400 AS INTEGER
                    ),
                    ping_count = ?
                WHERE id = ?
                """,
                (exited_at, exited_at, ping_count, visit_id),
            )
        return
    if ping_count is None:
        conn.execute(
            "UPDATE visits SET exited_at = ?, duration_sec = ? WHERE id = ?",
            (exited_at, duration_sec, visit_id),
        )
    else:
        conn.execute(
            "UPDATE visits SET exited_at = ?, duration_sec = ?, ping_count = ? WHERE id = ?",
            (exited_at, duration_sec, ping_count, visit_id),
        )


def increment_visit_ping_count(conn: sqlite3.Connection, visit_id: int) -> None:
    conn.execute(
        "UPDATE visits SET ping_count = ping_count + 1 WHERE id = ?",
        (visit_id,),
    )


def get_open_visit(conn: sqlite3.Connection) -> Visit | None:
    row = conn.execute(
        """
        SELECT id, place_id, place_name, entered_at, exited_at,
               duration_sec, ping_count
        FROM visits
        WHERE exited_at IS NULL
        ORDER BY entered_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return Visit(**dict(row))


def get_visits(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[Visit]:
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("entered_at >= ?")
        params.append(since)
    if until:
        clauses.append("entered_at <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, place_id, place_name, entered_at, exited_at,
               duration_sec, ping_count
        FROM visits
        {where}
        ORDER BY entered_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [Visit(**dict(r)) for r in rows]


def reconcile_visits(
    conn: sqlite3.Connection,
    since: str,
    until: str,
    *,
    grace_minutes: float = 10.0,
    min_pings: int = 3,
    min_dwell_sec: int = 60,
    accuracy_threshold_m: float | None = None,
    read_lookback_hours: float = 24.0,
) -> int:
    """Re-derive closed visits that overlap [since, until).

    Per-user equivalent of the framework ``reconcile_visits``. The DB is
    already user-scoped, so no ``user_id`` parameter.
    """

    def _parse(ts: str) -> datetime:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)

    read_since_dt = _parse(since) - timedelta(hours=read_lookback_hours)
    read_since = read_since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if accuracy_threshold_m is None:
        place_expr = "place_id"
        params: tuple = (read_since, until)
    else:
        place_expr = (
            "CASE WHEN accuracy IS NOT NULL AND accuracy > ? "
            "THEN NULL ELSE place_id END"
        )
        params = (accuracy_threshold_m, read_since, until)

    all_rows = conn.execute(
        f"""
        SELECT timestamp, {place_expr} AS place_id
        FROM location_pings
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp ASC
        """,
        params,
    ).fetchall()

    grace_sec = grace_minutes * 60.0
    segments: list[dict] = []
    current: dict | None = None

    for row in all_rows:
        ts = row["timestamp"]
        pid = row["place_id"]
        if current is None:
            if pid is not None:
                current = {
                    "place_id": pid,
                    "first_ts": ts,
                    "last_ts": ts,
                    "ping_count": 1,
                }
            continue

        gap_sec = (_parse(ts) - _parse(current["last_ts"])).total_seconds()

        if pid == current["place_id"]:
            # A quiet tracker supplies no evidence that the user left. When
            # reporting resumes at the same place, keep extending the visit
            # regardless of the time between pings.
            current["last_ts"] = ts
            current["ping_count"] += 1
            continue

        if pid is None:
            if gap_sec > grace_sec:
                segments.append(current)
                current = None
            continue

        segments.append(current)
        current = {
            "place_id": pid,
            "first_ts": ts,
            "last_ts": ts,
            "ping_count": 1,
        }

    if current is not None:
        segments.append(current)

    kept: list[dict] = []
    for seg in segments:
        dur_sec = (_parse(seg["last_ts"]) - _parse(seg["first_ts"])).total_seconds()
        if seg["ping_count"] < min_pings or dur_sec < min_dwell_sec:
            continue
        if seg["last_ts"] < since:
            continue
        kept.append(seg)

    place_names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM places").fetchall()
    }

    delete_predicate = "exited_at >= ? AND entered_at < ?"
    delete_params: tuple = (since, until)
    crossing = next((seg for seg in kept if seg["first_ts"] < since), None)
    if crossing is not None:
        # A same-place segment can now cross the rolling window boundary.
        # Replace older rows covered by that full segment as well, or the
        # reconstructed visit would overlap the preserved pre-window row.
        delete_predicate = (
            f"({delete_predicate}) OR "
            "(place_id = ? AND exited_at >= ? AND entered_at <= ?)"
        )
        delete_params += (
            crossing["place_id"], crossing["first_ts"], crossing["last_ts"],
        )

    # `location_pings.visit_id` and `location_state.current_visit_id`
    # both REFERENCE visits(id). With `PRAGMA foreign_keys = ON` (set by
    # `connect()`), deleting visits while pings still point at them
    # raises FOREIGN KEY constraint failed. Null the back-references
    # first; pings keep their `place_id`, which is what reconcile reads.
    conn.execute(
        f"""
        UPDATE location_pings SET visit_id = NULL
        WHERE visit_id IN (
            SELECT id FROM visits
            WHERE exited_at IS NOT NULL
              AND ({delete_predicate})
        )
        """,
        delete_params,
    )
    conn.execute(
        f"""
        UPDATE location_state SET current_visit_id = NULL
        WHERE current_visit_id IN (
            SELECT id FROM visits
            WHERE exited_at IS NOT NULL
              AND ({delete_predicate})
        )
        """,
        delete_params,
    )

    conn.execute(
        f"""
        DELETE FROM visits
        WHERE exited_at IS NOT NULL
          AND ({delete_predicate})
        """,
        delete_params,
    )

    written = 0
    open_v = get_open_visit(conn)
    open_place_id = open_v.place_id if open_v else None
    open_entered = open_v.entered_at if open_v else None

    for seg in kept:
        if (
            open_entered is not None
            and seg["place_id"] == open_place_id
            and seg["last_ts"] >= open_entered
        ):
            continue
        visit_id = open_visit(
            conn, seg["place_id"],
            place_names.get(seg["place_id"], "?"), seg["first_ts"],
        )
        close_visit(conn, visit_id, seg["last_ts"], ping_count=seg["ping_count"])
        written += 1

    return written


# -- location_state ---------------------------------------------------------


def get_location_state(conn: sqlite3.Connection) -> LocationState | None:
    row = conn.execute(
        """
        SELECT current_place_id, current_visit_id, consecutive_count,
               last_ping_place_id, exit_started_at
        FROM location_state
        WHERE id = 1
        """
    ).fetchone()
    if not row:
        return None
    return LocationState(**dict(row))


def set_location_state(
    conn: sqlite3.Connection,
    *,
    current_place_id: int | None,
    current_visit_id: int | None,
    consecutive_count: int,
    last_ping_place_id: int | None = None,
    exit_started_at: str | None = None,
) -> None:
    """UPSERT the ``location_state`` singleton (id=1)."""
    conn.execute(
        """
        INSERT INTO location_state (
            id, current_place_id, current_visit_id,
            consecutive_count, last_ping_place_id, exit_started_at
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            current_place_id = excluded.current_place_id,
            current_visit_id = excluded.current_visit_id,
            consecutive_count = excluded.consecutive_count,
            last_ping_place_id = excluded.last_ping_place_id,
            exit_started_at = excluded.exit_started_at
        """,
        (current_place_id, current_visit_id, consecutive_count,
         last_ping_place_id, exit_started_at),
    )


# -- dismissed clusters -----------------------------------------------------


def list_dismissed_clusters(conn: sqlite3.Connection) -> list[Cluster]:
    rows = conn.execute(
        """
        SELECT id, lat, lon, radius_meters, dismissed_at
        FROM dismissed_clusters
        ORDER BY dismissed_at DESC
        """
    ).fetchall()
    return [Cluster(**dict(r)) for r in rows]


def dismiss_cluster(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    radius_meters: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO dismissed_clusters (lat, lon, radius_meters)
        VALUES (?, ?, ?)
        """,
        (lat, lon, radius_meters),
    )
    return int(cur.lastrowid or 0)


def restore_dismissed_cluster(
    conn: sqlite3.Connection, cluster_id: int,
) -> bool:
    cur = conn.execute(
        "DELETE FROM dismissed_clusters WHERE id = ?",
        (cluster_id,),
    )
    return cur.rowcount > 0


def delete_dismissed_cluster(
    conn: sqlite3.Connection, cluster_id: int,
) -> bool:
    """Alias of :func:`restore_dismissed_cluster` for naming parity with
    the framework helper. They do the same thing — both remove the row.
    """
    return restore_dismissed_cluster(conn, cluster_id)
