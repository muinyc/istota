"""Portfolio positions storage + analytics (per-user money DB).

Third schema family in ``money.db`` beside config (``config_store``) and
tracking (``db``). The model is snapshots, not transactions: one row per
snapshot per import, one position row per line in the source export. All
knobs fina hardcoded are data here — the account registry
(``portfolio_accounts``) and the symbol classification table
(``portfolio_classifications``), both auto-populated on import and
user-editable thereafter.

Classification is resolved at read time (explicit row → cash patterns →
options detection → Unclassified), never stamped onto position rows, so
editing a classification retroactively reclassifies all history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files

from istota.money.core.importers.positions_base import (
    OPTION_DESCRIPTION_RE,
    ParsedSnapshot,
    is_cash_row,
)
from istota.timestamps import iso_now as _iso_now

_SEED_SENTINEL = "portfolio_classifications_seeded_at"

UNCLASSIFIED = "Unclassified"
CASH_CLASS = "Cash & Equivalents"

HISTORY_GROUP_BYS = ("total", "group", "account_type", "asset_class")

# schema_meta is shared with config_store; CREATE IF NOT EXISTS keeps whichever
# family initialises first authoritative. The positions FK cascade clause is
# decorative (PRAGMA foreign_keys is not set on db.get_db connections) —
# delete_snapshot deletes position rows explicitly.
PORTFOLIO_SCHEMA = """\
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exported_at TEXT NOT NULL,
    exported_at_estimated INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_file TEXT,
    content_hash TEXT NOT NULL UNIQUE,
    position_count INTEGER NOT NULL,
    total_value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_number TEXT NOT NULL DEFAULT '',
    account_group TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT '',
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES portfolio_accounts(id),
    row_type TEXT NOT NULL DEFAULT 'position',
    symbol TEXT NOT NULL DEFAULT '',
    symbol_norm TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    quantity REAL, price REAL, value REAL,
    cost_basis REAL, avg_cost_basis REAL,
    day_gain REAL, day_gain_pct REAL,
    total_gain REAL, total_gain_pct REAL,
    pct_of_account REAL,
    security_type TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portfolio_positions_snapshot ON portfolio_positions(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_symbol ON portfolio_positions(symbol_norm);

CREATE TABLE IF NOT EXISTS portfolio_classifications (
    symbol_norm TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    sub_class TEXT NOT NULL DEFAULT '',
    geography TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


@dataclass
class PortfolioSnapshot:
    id: int
    exported_at: str
    exported_at_estimated: bool
    imported_at: str
    source: str
    source_file: str | None
    content_hash: str
    position_count: int
    total_value: float


@dataclass
class PortfolioAccount:
    id: int
    account_name: str
    account_number: str
    group: str
    account_type: str
    excluded: bool
    first_seen_at: str
    last_seen_at: str


@dataclass
class SymbolClassification:
    symbol_norm: str
    asset_class: str
    sub_class: str
    geography: str
    source: str  # 'seed' | 'auto' | 'user' | '' (pre-provenance row)
    updated_at: str


def normalize_symbol(symbol: str) -> str:
    """``SPAXX**`` → ``SPAXX``; join key for classifications and history."""
    return (symbol or "").strip().rstrip("*").upper()


def guess_account_type(account_name: str) -> str:
    """One-shot initial guess; the registry row is user-owned thereafter."""
    if "IRA" in account_name:
        return "retirement"
    if "Trading" in account_name:
        return "trading"
    if any(word in account_name for word in ("Cash", "Expense", "Tax", "Holding")):
        return "cash"
    return "taxable"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the portfolio tables and run the one-time classification seed.

    Called from ``db.init_db`` so every connection path gets the tables.
    """
    conn.executescript(PORTFOLIO_SCHEMA)
    _migrate_owner_to_group(conn)
    _migrate_classification_source(conn)
    seed_classifications(conn)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _alter_once(conn: sqlite3.Connection, table: str, column: str, sql: str) -> None:
    """Run a one-time ALTER, tolerating a concurrent connection winning it.

    ``ensure_schema`` runs on every money web request, scheduler cron and
    skill invocation, so the first post-upgrade moment is routinely several
    connections at once. Check-then-ALTER lets both see the column absent and
    the loser raise ``duplicate column name`` — a schema error, so
    ``busy_timeout`` does not help — surfacing as a one-off 500 or failed
    task. Re-check on the way out so the loser exits having done its job.
    """
    cols = _table_columns(conn, table)
    # A missing table also yields no rows from PRAGMA table_info, which would
    # read as "column absent" and point the ALTER at nothing.
    if not cols or column in cols:
        return
    try:
        conn.execute(sql)
        conn.commit()
    except sqlite3.OperationalError:
        if column not in _table_columns(conn, table):
            raise


def _migrate_owner_to_group(conn: sqlite3.Connection) -> None:
    """Rename the original ``owner`` column to ``account_group``.

    The registry label started as "owner" but a group is the general concept
    (an owner is one way to group accounts). CREATE IF NOT EXISTS leaves an
    existing table on the old column, so rename in place; values carry over.
    """
    cols = _table_columns(conn, "portfolio_accounts")
    if "owner" in cols and "account_group" not in cols:
        try:
            conn.execute(
                "ALTER TABLE portfolio_accounts "
                "RENAME COLUMN owner TO account_group"
            )
            conn.commit()
        except sqlite3.OperationalError:
            # Another connection renamed it first (see _alter_once).
            if "account_group" not in _table_columns(conn, "portfolio_accounts"):
                raise


def _migrate_classification_source(conn: sqlite3.Connection) -> None:
    """Add the ``source`` provenance column to a pre-provenance table.

    Existing rows keep the '' default — a mix of seed and hand edits we can't
    tell apart after the fact; only rows written from here on carry provenance.
    """
    _alter_once(
        conn, "portfolio_classifications", "source",
        "ALTER TABLE portfolio_classifications "
        "ADD COLUMN source TEXT NOT NULL DEFAULT ''",
    )


def seed_classifications(conn: sqlite3.Connection) -> int:
    """Seed the bundled classification map once (sentinel-gated).

    User edits win forever after — deleting a seeded row does not resurrect
    it on the next init.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (_SEED_SENTINEL,)
    ).fetchone()
    if row is not None:
        return 0
    resource = files("istota.money").joinpath("data/portfolio_classifications.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    count = 0
    now = _iso_now()
    for symbol, cls in data.items():
        conn.execute(
            "INSERT OR IGNORE INTO portfolio_classifications "
            "(symbol_norm, asset_class, sub_class, geography, source, updated_at) "
            "VALUES (?, ?, ?, ?, 'seed', ?)",
            (
                normalize_symbol(symbol),
                cls.get("asset_class", UNCLASSIFIED),
                cls.get("sub_class", ""),
                cls.get("geography", ""),
                now,
            ),
        )
        count += 1
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (_SEED_SENTINEL, now),
    )
    conn.commit()
    return count


# =============================================================================
# Snapshot identity + insert
# =============================================================================


def compute_snapshot_hash(rows) -> str:
    """Content hash over the sorted, normalized position rows.

    Deliberately excludes ``exported_at`` so an identical file re-imported is
    a no-op even when the footer-date fallback stamped ``now()``.
    """
    parts = []
    for r in rows:
        parts.append("|".join([
            r.account_number,
            r.account_name,
            r.symbol,
            "" if r.quantity is None else repr(r.quantity),
            "" if r.value is None else repr(r.value),
            "" if r.cost_basis is None else repr(r.cost_basis),
        ]))
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _duplicate_result(conn: sqlite3.Connection, content_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT id FROM portfolio_snapshots WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if row is None:
        return None
    return {"status": "duplicate", "snapshot_id": row[0]}


def _upsert_account(
    conn: sqlite3.Connection,
    account_name: str,
    account_number: str,
    group_hint: str,
    now: str,
) -> tuple[int, bool]:
    """Returns (account_id, created)."""
    row = conn.execute(
        "SELECT id, account_number, account_group FROM portfolio_accounts WHERE account_name = ?",
        (account_name,),
    ).fetchone()
    if row is not None:
        account_id, stored_number, stored_group = row[0], row[1], row[2]
        conn.execute(
            "UPDATE portfolio_accounts SET last_seen_at = ? WHERE id = ?",
            (now, account_id),
        )
        if not stored_number and account_number:
            conn.execute(
                "UPDATE portfolio_accounts SET account_number = ? WHERE id = ?",
                (account_number, account_id),
            )
        if not stored_group and group_hint:
            conn.execute(
                "UPDATE portfolio_accounts SET account_group = ? WHERE id = ?",
                (group_hint, account_id),
            )
        return account_id, False
    cursor = conn.execute(
        "INSERT INTO portfolio_accounts "
        "(account_name, account_number, account_group, account_type, excluded, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (
            account_name,
            account_number,
            group_hint,
            guess_account_type(account_name),
            now,
            now,
        ),
    )
    return cursor.lastrowid, True


def insert_snapshot(
    conn: sqlite3.Connection,
    parsed: ParsedSnapshot,
    *,
    source_file: str | None = None,
) -> dict:
    """Store one parsed snapshot: registry upsert + hash dedup + row insert.

    One transaction; a UNIQUE(content_hash) race resolves to a clean
    ``duplicate`` result for the loser.
    """
    content_hash = compute_snapshot_hash(parsed.rows)
    duplicate = _duplicate_result(conn, content_hash)
    if duplicate is not None:
        return duplicate

    now = _iso_now()
    new_accounts: list[str] = []
    account_ids: dict[str, int] = {}
    try:
        for row in parsed.rows:
            if row.account_name in account_ids:
                continue
            hint = parsed.group_hints.get(row.account_name, "")
            account_id, created = _upsert_account(
                conn, row.account_name, row.account_number, hint, now
            )
            account_ids[row.account_name] = account_id
            if created:
                new_accounts.append(row.account_name)

        total_value = sum(r.value for r in parsed.rows if r.value is not None)
        cursor = conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(exported_at, exported_at_estimated, imported_at, source, source_file, "
            " content_hash, position_count, total_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed.exported_at.isoformat(),
                1 if parsed.exported_at_estimated else 0,
                now,
                parsed.source,
                source_file,
                content_hash,
                len(parsed.rows),
                total_value,
            ),
        )
        snapshot_id = cursor.lastrowid
        for row in parsed.rows:
            conn.execute(
                "INSERT INTO portfolio_positions "
                "(snapshot_id, account_id, row_type, symbol, symbol_norm, description, "
                " quantity, price, value, cost_basis, avg_cost_basis, day_gain, "
                " day_gain_pct, total_gain, total_gain_pct, pct_of_account, security_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    account_ids[row.account_name],
                    row.row_type,
                    row.symbol,
                    normalize_symbol(row.symbol),
                    row.description,
                    row.quantity, row.price, row.value,
                    row.cost_basis, row.avg_cost_basis, row.day_gain,
                    row.day_gain_pct, row.total_gain, row.total_gain_pct,
                    row.pct_of_account, row.security_type,
                ),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        duplicate = _duplicate_result(conn, content_hash)
        if duplicate is not None:
            return duplicate
        raise

    cls_map = _classification_map(conn)
    unclassified = sorted({
        normalize_symbol(r.symbol)
        for r in parsed.rows
        if r.row_type == "position"
        and normalize_symbol(r.symbol)
        and _resolve(cls_map, normalize_symbol(r.symbol), r.description,
                     r.security_type, r.row_type)[0] == UNCLASSIFIED
    })
    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "exported_at": parsed.exported_at.isoformat(),
        "exported_at_estimated": parsed.exported_at_estimated,
        "position_count": len(parsed.rows),
        "total_value": total_value,
        "new_accounts": new_accounts,
        "unclassified_symbols": unclassified,
        "warnings": list(parsed.warnings),
    }


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    """Hard delete; positions removed explicitly (no PRAGMA reliance)."""
    conn.execute(
        "DELETE FROM portfolio_positions WHERE snapshot_id = ?", (snapshot_id,)
    )
    cursor = conn.execute(
        "DELETE FROM portfolio_snapshots WHERE id = ?", (snapshot_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> PortfolioSnapshot | None:
    row = conn.execute(
        "SELECT id, exported_at, exported_at_estimated, imported_at, source, "
        "source_file, content_hash, position_count, total_value "
        "FROM portfolio_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return PortfolioSnapshot(
        id=row[0], exported_at=row[1], exported_at_estimated=bool(row[2]),
        imported_at=row[3], source=row[4], source_file=row[5],
        content_hash=row[6], position_count=row[7], total_value=row[8],
    )


def find_snapshot_by_date(
    conn: sqlite3.Connection, exported_at: datetime
) -> PortfolioSnapshot | None:
    """A snapshot sharing the same calendar date (same-day collision check)."""
    day = exported_at.date().isoformat()
    row = conn.execute(
        "SELECT id FROM portfolio_snapshots WHERE date(exported_at) = ? "
        "ORDER BY exported_at DESC LIMIT 1",
        (day,),
    ).fetchone()
    if row is None:
        return None
    return get_snapshot(conn, row[0])


def list_snapshots(conn: sqlite3.Connection) -> list[dict]:
    """Snapshots newest-first; ``total_value`` here is the read-time total
    over non-excluded accounts (the stored column keeps the raw file total).
    """
    rows = conn.execute(
        "SELECT s.id, s.exported_at, s.exported_at_estimated, s.imported_at, "
        "s.source, s.source_file, s.position_count, "
        "COALESCE((SELECT SUM(p.value) FROM portfolio_positions p "
        "          JOIN portfolio_accounts a ON a.id = p.account_id "
        "          WHERE p.snapshot_id = s.id AND a.excluded = 0 "
        "          AND p.value IS NOT NULL), 0) "
        "FROM portfolio_snapshots s ORDER BY s.exported_at DESC"
    ).fetchall()
    return [
        {
            "id": r[0],
            "exported_at": r[1],
            "exported_at_estimated": bool(r[2]),
            "imported_at": r[3],
            "source": r[4],
            "source_file": r[5],
            "position_count": r[6],
            "total_value": round(r[7], 2),
        }
        for r in rows
    ]


# =============================================================================
# Classification resolution (read-time)
# =============================================================================


def _classification_map(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT symbol_norm, asset_class, sub_class, geography "
        "FROM portfolio_classifications"
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _resolve(
    cls_map: dict[str, tuple[str, str, str]],
    symbol_norm: str,
    description: str,
    security_type: str,
    row_type: str,
) -> tuple[str, str, str]:
    """Explicit row → cash patterns → options detection → Unclassified."""
    explicit = cls_map.get(symbol_norm)
    if explicit is not None:
        return explicit
    if row_type in ("cash", "pending") or is_cash_row(symbol_norm, description):
        sub = "Cash" if ("USD" in symbol_norm or "CORE" in symbol_norm or not symbol_norm) else "Money Market"
        return (CASH_CLASS, sub, "US")
    desc = description or ""
    is_option = (
        security_type == "Option"
        or OPTION_DESCRIPTION_RE.search(desc)
        or "CALL" in desc
        or "PUT" in desc
    )
    if is_option:
        if "PUT" in desc or desc.endswith(" P"):
            sub = "Put Options"
        else:
            sub = "Call Options"
        underlying = cls_map.get(symbol_norm.split()[0] if " " in symbol_norm else symbol_norm)
        geography = underlying[2] if underlying is not None else "US"
        return ("Options", sub, geography)
    return (UNCLASSIFIED, UNCLASSIFIED, UNCLASSIFIED)


def resolve_classification(
    conn: sqlite3.Connection,
    symbol: str,
    description: str = "",
    security_type: str = "",
    row_type: str = "position",
) -> tuple[str, str, str]:
    """Resolve one symbol's (asset_class, sub_class, geography)."""
    return _resolve(
        _classification_map(conn),
        normalize_symbol(symbol),
        description,
        security_type,
        row_type,
    )


# =============================================================================
# Analytics
# =============================================================================


def _load_positions(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    group: str | None = None,
    account_id: int | None = None,
) -> list[dict]:
    """Positions for one snapshot joined with their accounts; excluded
    accounts filtered out before any aggregation."""
    sql = (
        "SELECT p.row_type, p.symbol, p.symbol_norm, p.description, p.quantity, "
        "p.price, p.value, p.cost_basis, p.security_type, "
        "a.id, a.account_name, a.account_group, a.account_type "
        "FROM portfolio_positions p JOIN portfolio_accounts a ON a.id = p.account_id "
        "WHERE p.snapshot_id = ? AND a.excluded = 0"
    )
    params: list = [snapshot_id]
    if group is not None:
        sql += " AND a.account_group = ?"
        params.append(group)
    if account_id is not None:
        sql += " AND a.id = ?"
        params.append(account_id)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "row_type": r[0], "symbol": r[1], "symbol_norm": r[2],
            "description": r[3], "quantity": r[4], "price": r[5],
            "value": r[6], "cost_basis": r[7], "security_type": r[8],
            "account_id": r[9], "account_name": r[10], "group": r[11],
            "account_type": r[12],
        }
        for r in rows
    ]


def _holding_key(pos: dict) -> str:
    if pos["row_type"] in ("cash", "pending"):
        return "CASH"
    return pos["symbol_norm"] or pos["symbol"] or "UNKNOWN"


def _group_sums(positions: list[dict], label_fn) -> list[dict]:
    sums: dict[str, float] = {}
    for pos in positions:
        if pos["value"] is None:
            continue
        label = label_fn(pos)
        sums[label] = sums.get(label, 0.0) + pos["value"]
    total = sum(sums.values())
    return [
        {"key": key, "value": round(value, 2),
         "pct": round(value / total, 4) if total else 0.0}
        for key, value in sorted(sums.items(), key=lambda kv: -kv[1])
    ]


def snapshot_summary(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    group: str | None = None,
    account_id: int | None = None,
) -> dict | None:
    snapshot = get_snapshot(conn, snapshot_id)
    if snapshot is None:
        return None
    positions = _load_positions(conn, snapshot_id, group=group, account_id=account_id)
    cls_map = _classification_map(conn)

    def classify(pos: dict) -> tuple[str, str, str]:
        return _resolve(cls_map, pos["symbol_norm"], pos["description"],
                        pos["security_type"], pos["row_type"])

    total_value = round(sum(p["value"] for p in positions if p["value"] is not None), 2)

    by_account_rows: dict[int, dict] = {}
    for pos in positions:
        if pos["value"] is None:
            continue
        entry = by_account_rows.setdefault(pos["account_id"], {
            "key": pos["account_name"],
            "account_id": pos["account_id"],
            "group": pos["group"],
            "account_type": pos["account_type"],
            "value": 0.0,
        })
        entry["value"] += pos["value"]
    by_account = sorted(
        (
            {**e, "value": round(e["value"], 2),
             "pct": round(e["value"] / total_value, 4) if total_value else 0.0}
            for e in by_account_rows.values()
        ),
        key=lambda e: -e["value"],
    )

    holdings: dict[str, dict] = {}
    for pos in positions:
        key = _holding_key(pos)
        entry = holdings.setdefault(key, {
            "symbol": key,
            "description": "",
            "quantity": None,
            "value": 0.0,
            "cost_basis": None,
            "accounts": set(),
        })
        if pos["value"] is not None:
            entry["value"] += pos["value"]
        if pos["quantity"] is not None and key != "CASH":
            entry["quantity"] = (entry["quantity"] or 0.0) + pos["quantity"]
        if pos["cost_basis"] is not None:
            entry["cost_basis"] = (entry["cost_basis"] or 0.0) + pos["cost_basis"]
        if not entry["description"] and pos["description"]:
            entry["description"] = pos["description"]
        entry["accounts"].add(pos["account_id"])

    holdings_list = []
    for key, entry in holdings.items():
        if key == "CASH":
            asset_class, sub_class, geography = (CASH_CLASS, "Cash", "US")
            entry["description"] = "Cash & equivalents"
        else:
            sample = next(p for p in positions if _holding_key(p) == key)
            asset_class, sub_class, geography = classify(sample)
        gain = None
        gain_pct = None
        if entry["cost_basis"]:
            gain = round(entry["value"] - entry["cost_basis"], 2)
            gain_pct = round(gain / entry["cost_basis"], 4)
        holdings_list.append({
            "symbol": entry["symbol"],
            "description": entry["description"],
            "quantity": entry["quantity"],
            "value": round(entry["value"], 2),
            "cost_basis": round(entry["cost_basis"], 2) if entry["cost_basis"] is not None else None,
            "gain": gain,
            "gain_pct": gain_pct,
            "asset_class": asset_class,
            "sub_class": sub_class,
            "geography": geography,
            "accounts": len(entry["accounts"]),
        })
    holdings_list.sort(key=lambda h: -(h["value"] or 0.0))

    return {
        "snapshot_id": snapshot_id,
        "exported_at": snapshot.exported_at,
        "exported_at_estimated": snapshot.exported_at_estimated,
        "total_value": total_value,
        "position_count": len(positions),
        "by_asset_class": _group_sums(positions, lambda p: classify(p)[0]),
        "by_account": by_account,
        "by_account_type": _group_sums(positions, lambda p: p["account_type"] or "unspecified"),
        "by_group": _group_sums(positions, lambda p: p["group"] or "Ungrouped"),
        "by_geography": _group_sums(positions, lambda p: classify(p)[2]),
        "holdings": holdings_list,
    }


def history_series(
    conn: sqlite3.Connection,
    *,
    group_by: str = "total",
    group: str | None = None,
) -> dict:
    if group_by not in HISTORY_GROUP_BYS:
        raise ValueError(f"group_by must be one of {HISTORY_GROUP_BYS}")
    snapshots = conn.execute(
        "SELECT id, exported_at, exported_at_estimated FROM portfolio_snapshots "
        "ORDER BY exported_at ASC"
    ).fetchall()
    cls_map = _classification_map(conn)
    series = []
    for snap_id, exported_at, estimated in snapshots:
        positions = _load_positions(conn, snap_id, group=group)
        total = round(sum(p["value"] for p in positions if p["value"] is not None), 2)
        point = {
            "snapshot_id": snap_id,
            "exported_at": exported_at,
            "exported_at_estimated": bool(estimated),
            "total": total,
        }
        if group_by != "total":
            if group_by == "group":
                label_fn = lambda p: p["group"] or "Ungrouped"  # noqa: E731
            elif group_by == "account_type":
                label_fn = lambda p: p["account_type"] or "unspecified"  # noqa: E731
            else:
                label_fn = lambda p: _resolve(  # noqa: E731
                    cls_map, p["symbol_norm"], p["description"],
                    p["security_type"], p["row_type"],
                )[0]
            point["groups"] = {
                g["key"]: g["value"] for g in _group_sums(positions, label_fn)
            }
        series.append(point)
    return {"group_by": group_by, "series": series}


def symbol_history(conn: sqlite3.Connection, symbol: str) -> dict:
    norm = normalize_symbol(symbol)
    rows = conn.execute(
        "SELECT s.id, s.exported_at, SUM(p.quantity), MAX(p.price), SUM(p.value) "
        "FROM portfolio_positions p "
        "JOIN portfolio_snapshots s ON s.id = p.snapshot_id "
        "JOIN portfolio_accounts a ON a.id = p.account_id "
        "WHERE p.symbol_norm = ? AND a.excluded = 0 "
        "GROUP BY s.id ORDER BY s.exported_at ASC",
        (norm,),
    ).fetchall()
    return {
        "symbol": norm,
        "points": [
            {
                "snapshot_id": r[0],
                "exported_at": r[1],
                "quantity": r[2],
                "price": r[3],
                "value": round(r[4], 2) if r[4] is not None else None,
            }
            for r in rows
        ],
    }


_DIFF_VALUE_NOISE = 0.01
_DIFF_QUANTITY_NOISE = 1e-6


def snapshot_diff(conn: sqlite3.Connection, older_id: int, newer_id: int) -> dict | None:
    """Positions opened, closed and changed between two snapshots, per account."""
    if get_snapshot(conn, older_id) is None or get_snapshot(conn, newer_id) is None:
        return None

    def aggregate(snapshot_id: int) -> dict[tuple[int, str], dict]:
        agg: dict[tuple[int, str], dict] = {}
        for pos in _load_positions(conn, snapshot_id):
            key = (pos["account_id"], _holding_key(pos))
            entry = agg.setdefault(key, {
                "symbol": key[1],
                "account_name": pos["account_name"],
                "quantity": 0.0,
                "value": 0.0,
            })
            if pos["quantity"] is not None:
                entry["quantity"] += pos["quantity"]
            if pos["value"] is not None:
                entry["value"] += pos["value"]
        return agg

    older = aggregate(older_id)
    newer = aggregate(newer_id)

    opened = [
        {"symbol": e["symbol"], "account_name": e["account_name"],
         "quantity": e["quantity"], "value": round(e["value"], 2)}
        for key, e in sorted(newer.items()) if key not in older
    ]
    closed = [
        {"symbol": e["symbol"], "account_name": e["account_name"],
         "quantity": e["quantity"], "value": round(e["value"], 2)}
        for key, e in sorted(older.items()) if key not in newer
    ]
    changed = []
    for key in sorted(older.keys() & newer.keys()):
        before, after = older[key], newer[key]
        quantity_delta = abs(after["quantity"] - before["quantity"])
        value_delta = abs(after["value"] - before["value"])
        if quantity_delta <= _DIFF_QUANTITY_NOISE and value_delta <= _DIFF_VALUE_NOISE:
            continue
        changed.append({
            "symbol": before["symbol"],
            "account_name": before["account_name"],
            "quantity_from": before["quantity"],
            "quantity_to": after["quantity"],
            "value_from": round(before["value"], 2),
            "value_to": round(after["value"], 2),
        })
    return {
        "older_id": older_id,
        "newer_id": newer_id,
        "opened": opened,
        "closed": closed,
        "changed": changed,
    }


# =============================================================================
# Account registry + classification CRUD
# =============================================================================


def list_accounts(conn: sqlite3.Connection) -> list[PortfolioAccount]:
    rows = conn.execute(
        "SELECT id, account_name, account_number, account_group, account_type, excluded, "
        "first_seen_at, last_seen_at FROM portfolio_accounts ORDER BY account_name"
    ).fetchall()
    return [
        PortfolioAccount(
            id=r[0], account_name=r[1], account_number=r[2], group=r[3],
            account_type=r[4], excluded=bool(r[5]),
            first_seen_at=r[6], last_seen_at=r[7],
        )
        for r in rows
    ]


def get_account(conn: sqlite3.Connection, account_id: int) -> PortfolioAccount | None:
    for account in list_accounts(conn):
        if account.id == account_id:
            return account
    return None


def update_account(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    group: str | None = None,
    account_type: str | None = None,
    excluded: bool | None = None,
) -> bool:
    sets = []
    params: list = []
    if group is not None:
        sets.append("account_group = ?")
        params.append(group)
    if account_type is not None:
        sets.append("account_type = ?")
        params.append(account_type)
    if excluded is not None:
        sets.append("excluded = ?")
        params.append(1 if excluded else 0)
    if not sets:
        return False
    params.append(account_id)
    cursor = conn.execute(
        f"UPDATE portfolio_accounts SET {', '.join(sets)} WHERE id = ?", params
    )
    conn.commit()
    return cursor.rowcount > 0


def list_classifications(conn: sqlite3.Connection) -> list[SymbolClassification]:
    rows = conn.execute(
        "SELECT symbol_norm, asset_class, sub_class, geography, source, updated_at "
        "FROM portfolio_classifications ORDER BY symbol_norm"
    ).fetchall()
    return [
        SymbolClassification(
            symbol_norm=r[0], asset_class=r[1], sub_class=r[2],
            geography=r[3], source=r[4], updated_at=r[5],
        )
        for r in rows
    ]


def _validated_classification(
    symbol: str, asset_class: str, sub_class: str, geography: str,
) -> tuple[str, str, str, str]:
    norm = normalize_symbol(symbol)
    if not norm:
        raise ValueError("symbol normalizes to empty")
    if not asset_class.strip():
        raise ValueError("asset_class is required")
    return (norm, asset_class.strip(), sub_class.strip(), geography.strip())


def set_classification(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    asset_class: str,
    sub_class: str = "",
    geography: str = "",
    source: str = "user",
) -> str:
    """Upsert a classification; returns the normalized symbol key.

    The write path for a *deliberate* classification (the web PUT, the CLI's
    ``classify``, the seed). An automatic one goes through
    :func:`insert_classification_if_absent` instead, which is the only way to
    learn whether the write landed.
    """
    norm, asset_class, sub_class, geography = _validated_classification(
        symbol, asset_class, sub_class, geography,
    )
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_classifications "
        "(symbol_norm, asset_class, sub_class, geography, source, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (norm, asset_class, sub_class, geography, source, _iso_now()),
    )
    conn.commit()
    return norm


def insert_classification_if_absent(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    asset_class: str,
    sub_class: str = "",
    geography: str = "",
    source: str = "auto",
) -> bool:
    """Write a classification only if the symbol has none; True if it landed.

    The write path for automatic classification. A guess must never replace
    what the user said — including a deliberate ``Unclassified``, which is an
    offered value on every surface — and a read-then-write gate cannot promise
    that, because the network lookup sits between the read and the write.
    ``INSERT OR IGNORE`` makes it structural: the row's own primary key
    decides, inside the same statement.
    """
    norm, asset_class, sub_class, geography = _validated_classification(
        symbol, asset_class, sub_class, geography,
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO portfolio_classifications "
        "(symbol_norm, asset_class, sub_class, geography, source, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (norm, asset_class, sub_class, geography, source, _iso_now()),
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_classification(conn: sqlite3.Connection, symbol: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM portfolio_classifications WHERE symbol_norm = ?",
        (normalize_symbol(symbol),),
    )
    conn.commit()
    return cursor.rowcount > 0
