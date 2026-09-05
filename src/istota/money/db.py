"""SQLite persistence for dedup tracking and schedule state.

Single-user per instance — no user_id parameter in public API.
Work entries are stored in plaintext TOML files (see work.py).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """\
CREATE TABLE IF NOT EXISTS monarch_synced_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monarch_transaction_id TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now')),
    tags_json TEXT,
    amount REAL,
    merchant TEXT,
    posted_account TEXT,
    contra_account TEXT,
    txn_date TEXT,
    content_hash TEXT,
    recategorized_at TEXT,
    profile TEXT NOT NULL DEFAULT '',
    -- What the mapping consumed and which rules answered. Nullable, because
    -- a row synced before rule tracing has no honest value for any of them.
    src_category TEXT,
    src_account TEXT,
    src_source TEXT,
    rule_ids TEXT,
    UNIQUE(monarch_transaction_id, profile)
);

CREATE INDEX IF NOT EXISTS idx_monarch_synced_active ON monarch_synced_transactions(id)
    WHERE recategorized_at IS NULL;

CREATE TABLE IF NOT EXISTS csv_imported_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    source_file TEXT,
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoice_schedule_state (
    client_key TEXT PRIMARY KEY,
    last_reminder_at TEXT,
    last_generation_at TEXT
);

CREATE TABLE IF NOT EXISTS invoice_overdue_notified (
    invoice_number TEXT PRIMARY KEY,
    notified_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_db(db_path: Path | str):
    """Context manager for database connections with row factory.

    journal_mode (WAL) is set once by ``init_db`` and persists in the SQLite
    file header — not re-issued here. 30s busy handler absorbs any residual
    contention instead of raising SQLITE_BUSY.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str) -> None:
    """Create tables if they don't exist."""
    with get_db(db_path) as conn:
        # WAL: this DB now lives on LOCAL disk (Config.module_db_path, off the
        # rclone FUSE mount), so WAL's mmap'd -shm is safe — the SIGBUS that
        # forced DELETE (ISSUE-157) was a FUSE artifact. WAL restores
        # reader/writer concurrency. Issued unconditionally so a relocated
        # DELETE-mode DB converts on first touch; no-op once WAL.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_monarch_synced_columns(conn)
        # Portfolio schema family (runtime import: portfolio pulls in the
        # importers package, which must not load at db-module import time).
        from istota.money import portfolio
        portfolio.ensure_schema(conn)


def _migrate_monarch_synced_columns(conn: sqlite3.Connection) -> None:
    """Bring older monarch_synced_transactions schemas up to current.

    The four provenance columns go through ``portfolio._alter_once`` rather
    than the unguarded check-then-ALTER above them. ``init_db`` runs on every
    money web request, scheduler cron and skill invocation, so the first
    post-upgrade moment is routinely several connections at once, and the
    loser of a check-then-ALTER race raises ``duplicate column name`` — a
    schema error, so the 30s busy handler does not help. The helper lives in
    ``portfolio`` because that is where it was written and where its other
    caller is; one mechanism, imported at function scope for the reason
    ``init_db`` states (``portfolio`` pulls in the importers package, which
    must not load at db-module import time).

    Bringing ``profile`` and ``contra_account`` across is a separate change:
    ``profile`` is an ALTER plus two index statements, so it needs more than a
    column guard, and rewriting a migration that has already run everywhere
    buys nothing.
    """
    from istota.money.portfolio import _alter_once

    cursor = conn.execute("PRAGMA table_info(monarch_synced_transactions)")
    columns = {row["name"] for row in cursor.fetchall()}

    if "profile" not in columns:
        conn.execute("ALTER TABLE monarch_synced_transactions ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
        conn.execute("DROP INDEX IF EXISTS idx_monarch_synced_unique")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_monarch_synced_unique "
            "ON monarch_synced_transactions(monarch_transaction_id, profile)"
        )

    if "contra_account" not in columns:
        conn.execute("ALTER TABLE monarch_synced_transactions ADD COLUMN contra_account TEXT")

    # What the mapping consumed, and which rules answered. Nullable, and
    # populated going forward only: a row written before this reads as
    # "synced before rule tracing" rather than being guessed at.
    for column in ("src_category", "src_account", "src_source", "rule_ids"):
        _alter_once(
            conn,
            "monarch_synced_transactions",
            column,
            f"ALTER TABLE monarch_synced_transactions ADD COLUMN {column} TEXT",
        )


# =============================================================================
# Monarch sync tracking
# =============================================================================


@dataclass
class MonarchSyncedTransaction:
    """A previously synced Monarch transaction for reconciliation."""
    id: int
    monarch_transaction_id: str
    tags_json: str | None
    amount: float | None
    merchant: str | None
    posted_account: str | None
    txn_date: str | None
    contra_account: str | None = None


def is_monarch_transaction_synced(
    conn: sqlite3.Connection,
    monarch_transaction_id: str,
    profile: str | None = None,
) -> bool:
    """Check if a Monarch transaction has already been synced.

    When profile is given, checks only that profile. When None, checks any profile.
    """
    if profile is not None:
        cursor = conn.execute(
            "SELECT 1 FROM monarch_synced_transactions "
            "WHERE monarch_transaction_id = ? AND profile = ?",
            (monarch_transaction_id, profile),
        )
    else:
        cursor = conn.execute(
            "SELECT 1 FROM monarch_synced_transactions WHERE monarch_transaction_id = ?",
            (monarch_transaction_id,),
        )
    return cursor.fetchone() is not None


def track_monarch_transactions_batch(
    conn: sqlite3.Connection,
    transactions: list[dict],
    profile: str = "",
) -> int:
    """Record multiple Monarch transactions as synced with metadata."""
    count = 0
    for txn in transactions:
        cursor = conn.execute(
            """
            INSERT INTO monarch_synced_transactions (
                monarch_transaction_id, tags_json, amount, merchant,
                posted_account, contra_account, txn_date, content_hash, profile,
                src_category, src_account, src_source, rule_ids
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (monarch_transaction_id, profile) DO UPDATE SET
                tags_json = excluded.tags_json,
                amount = excluded.amount,
                merchant = excluded.merchant,
                posted_account = excluded.posted_account,
                contra_account = excluded.contra_account,
                txn_date = excluded.txn_date,
                content_hash = excluded.content_hash,
                src_category = excluded.src_category,
                src_account = excluded.src_account,
                src_source = excluded.src_source,
                rule_ids = excluded.rule_ids
            """,
            (
                txn["id"],
                txn.get("tags_json"),
                txn.get("amount"),
                txn.get("merchant"),
                txn.get("posted_account"),
                txn.get("contra_account"),
                txn.get("txn_date"),
                txn.get("content_hash"),
                profile,
                txn.get("src_category"),
                txn.get("src_account"),
                txn.get("src_source"),
                txn.get("rule_ids"),
            ),
        )
        count += cursor.rowcount
    return count


# The two source values a rule can be written against that a synced row
# records. A caller names a *field*, never a column: `field` reaches this
# from an HTTP query string, and interpolating it into the SQL is the whole
# injection surface.
_COVERAGE_COLUMNS = {"category": "src_category", "account": "src_account"}


def get_source_value_coverage(
    conn: sqlite3.Connection,
    *,
    field: str,
    limit: int = 500,
) -> list[dict]:
    """Distinct source values seen in synced rows, and what they posted to.

    The coverage card's whole input: which categories (or accounts) an import
    actually carried, how often, when last, and the account the rules sent the
    most recent one to — so a row resolving to ``Expenses:Uncategorized:`` can
    be flagged without re-running a sync.

    ``posted_account`` is the *last* row's, not a set. A value mapping to two
    accounts across a rule edit would need a second query to say so, and the
    card's question is what happens now.

    Recategorized rows are excluded: they were reversed out of the ledger, so
    counting them would overstate a category the user has already dealt with.
    Rows with no ``src_category`` are excluded too and counted by
    :func:`get_untraced_synced_count` instead — see its docstring for why they
    are not guessed at.
    """
    column = _COVERAGE_COLUMNS.get(field)
    if column is None:
        raise ValueError(
            f"unknown coverage field {field!r}: expected one of "
            f"{', '.join(sorted(_COVERAGE_COLUMNS))}",
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    limit = max(1, min(limit, 5000))

    rows = conn.execute(
        f"""
        SELECT {column} AS value,
               COUNT(*) AS count,
               MAX(txn_date) AS last_seen,
               posted_account
        FROM monarch_synced_transactions
        WHERE recategorized_at IS NULL
          AND {column} IS NOT NULL AND {column} != ''
        GROUP BY {column}
        ORDER BY count DESC, value ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "value": r["value"],
            "count": r["count"],
            "last_seen": r["last_seen"],
            "posted_account": r["posted_account"],
        }
        for r in rows
    ]


def get_untraced_synced_count(conn: sqlite3.Connection) -> int:
    """Active synced rows carrying no source category.

    Every row synced before rule tracing, and every row an import wrote with
    no category at all. Reported as one number rather than folded into the
    value list, because there is no value to report: the source category was
    never stored, and reading it back out of ``posted_account`` is lossy —
    ``account_component`` deletes punctuation, so "Food & Drink" and "Food
    Drink" both slug to ``FoodDrink``.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM monarch_synced_transactions "
        "WHERE recategorized_at IS NULL "
        "AND (src_category IS NULL OR src_category = '')"
    ).fetchone()
    return row["n"] if row else 0


def get_active_monarch_synced_transactions(
    conn: sqlite3.Connection,
    profile: str | None = None,
) -> list[MonarchSyncedTransaction]:
    """Get all synced transactions that haven't been recategorized.

    When profile is given, returns only that profile's transactions.
    """
    if profile is not None:
        cursor = conn.execute(
            """
            SELECT id, monarch_transaction_id, tags_json, amount, merchant,
                   posted_account, contra_account, txn_date
            FROM monarch_synced_transactions
            WHERE recategorized_at IS NULL AND profile = ?
            """,
            (profile,),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, monarch_transaction_id, tags_json, amount, merchant,
                   posted_account, contra_account, txn_date
            FROM monarch_synced_transactions
            WHERE recategorized_at IS NULL
            """
        )
    return [
        MonarchSyncedTransaction(
            id=row["id"],
            monarch_transaction_id=row["monarch_transaction_id"],
            tags_json=row["tags_json"],
            amount=row["amount"],
            merchant=row["merchant"],
            posted_account=row["posted_account"],
            txn_date=row["txn_date"],
            contra_account=row["contra_account"],
        )
        for row in cursor.fetchall()
    ]


def mark_monarch_transaction_recategorized(
    conn: sqlite3.Connection,
    monarch_transaction_id: str,
) -> bool:
    """Mark a synced transaction as recategorized (business tag removed)."""
    cursor = conn.execute(
        """
        UPDATE monarch_synced_transactions
        SET recategorized_at = datetime('now')
        WHERE monarch_transaction_id = ? AND recategorized_at IS NULL
        """,
        (monarch_transaction_id,),
    )
    return cursor.rowcount > 0


def update_monarch_transaction_posted_account(
    conn: sqlite3.Connection,
    monarch_transaction_id: str,
    new_posted_account: str,
) -> bool:
    """Update the posted_account for a synced transaction after category change."""
    cursor = conn.execute(
        """
        UPDATE monarch_synced_transactions
        SET posted_account = ?
        WHERE monarch_transaction_id = ? AND recategorized_at IS NULL
        """,
        (new_posted_account, monarch_transaction_id),
    )
    return cursor.rowcount > 0


# =============================================================================
# Content hash dedup (cross-source)
# =============================================================================


def is_content_hash_synced(
    conn: sqlite3.Connection,
    content_hash: str,
) -> bool:
    """Check if a content hash exists in any transaction tracking table."""
    cursor = conn.execute(
        """
        SELECT 1 FROM monarch_synced_transactions WHERE content_hash = ?
        UNION
        SELECT 1 FROM csv_imported_transactions WHERE content_hash = ?
        LIMIT 1
        """,
        (content_hash, content_hash),
    )
    return cursor.fetchone() is not None


# =============================================================================
# CSV import tracking
# =============================================================================


def track_csv_transactions_batch(
    conn: sqlite3.Connection,
    hashes: list[str],
    source_file: str | None = None,
) -> int:
    """Record multiple CSV transactions as imported. Returns count inserted."""
    count = 0
    for content_hash in hashes:
        cursor = conn.execute(
            """
            INSERT INTO csv_imported_transactions (content_hash, source_file)
            VALUES (?, ?)
            ON CONFLICT (content_hash) DO NOTHING
            """,
            (content_hash, source_file),
        )
        count += cursor.rowcount
    return count


# =============================================================================
# Invoice schedule state
# =============================================================================


@dataclass
class InvoiceScheduleState:
    """State for scheduled invoice generation/reminders."""
    client_key: str
    last_reminder_at: str | None
    last_generation_at: str | None


def get_invoice_schedule_state(
    conn: sqlite3.Connection,
    client_key: str,
) -> InvoiceScheduleState | None:
    cursor = conn.execute(
        "SELECT client_key, last_reminder_at, last_generation_at FROM invoice_schedule_state WHERE client_key = ?",
        (client_key,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return InvoiceScheduleState(
        client_key=row["client_key"],
        last_reminder_at=row["last_reminder_at"],
        last_generation_at=row["last_generation_at"],
    )


def set_invoice_schedule_reminder(conn: sqlite3.Connection, client_key: str) -> None:
    conn.execute(
        """
        INSERT INTO invoice_schedule_state (client_key, last_reminder_at)
        VALUES (?, datetime('now'))
        ON CONFLICT (client_key) DO UPDATE SET last_reminder_at = datetime('now')
        """,
        (client_key,),
    )


def set_invoice_schedule_generation(conn: sqlite3.Connection, client_key: str) -> None:
    conn.execute(
        """
        INSERT INTO invoice_schedule_state (client_key, last_generation_at)
        VALUES (?, datetime('now', 'localtime'))
        ON CONFLICT (client_key) DO UPDATE SET last_generation_at = datetime('now', 'localtime')
        """,
        (client_key,),
    )


# =============================================================================
# Invoice overdue tracking
# =============================================================================


def get_notified_overdue_invoices(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT invoice_number FROM invoice_overdue_notified")
    return {row["invoice_number"] for row in cursor.fetchall()}


def mark_invoice_overdue_notified(conn: sqlite3.Connection, invoice_number: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO invoice_overdue_notified (invoice_number) VALUES (?)",
        (invoice_number,),
    )


def clear_overdue_notification(conn: sqlite3.Connection, invoice_number: str) -> None:
    conn.execute(
        "DELETE FROM invoice_overdue_notified WHERE invoice_number = ?",
        (invoice_number,),
    )


# =============================================================================
# Key-value store
# =============================================================================


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a value from the key-value store, or None if not found."""
    cursor = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a value in the key-value store (upsert)."""
    conn.execute(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (key, value),
    )


def clear_invoice_state(conn: sqlite3.Connection, invoice_number: str) -> dict:
    """Remove all DB state related to an invoice.

    Clears invoice_overdue_notified rows for the invoice. Returns a dict
    summarizing what was deleted.
    """
    cursor = conn.execute(
        "DELETE FROM invoice_overdue_notified WHERE invoice_number = ?",
        (invoice_number,),
    )
    overdue_cleared = cursor.rowcount
    return {"overdue_notifications_cleared": overdue_cleared}
