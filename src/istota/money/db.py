"""SQLite persistence for dedup tracking and schedule state.

Single-user per instance — no user_id parameter in public API.
Work entries are stored in plaintext TOML files (see work.py).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from istota import sqlite_util


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
    with sqlite_util.open_db(
        db_path, foreign_keys=False, commit=True, rollback_on_error=True,
    ) as conn:
        yield conn


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


def _legacy_monarch_index(conn: sqlite3.Connection) -> bool:
    """Is `idx_monarch_synced_unique` present and still missing `profile`?

    `PRAGMA index_info` yields (seqno, cid, name) per indexed column and no rows
    at all for an index that does not exist, so the two cases this has to tell
    apart — never created, and created before `profile` — read differently.
    """
    columns = [row[2] for row in conn.execute(
        "PRAGMA index_info(idx_monarch_synced_unique)"
    )]
    return bool(columns) and "profile" not in columns


def _migrate_monarch_synced_columns(conn: sqlite3.Connection) -> None:
    """Bring older monarch_synced_transactions schemas up to current.

    Every column here goes through ``sqlite_util.add_columns`` rather than a
    bare check-then-ALTER. ``init_db`` runs on every money web request,
    scheduler cron and skill invocation, so the first post-upgrade moment is
    routinely several connections at once, and the loser of a check-then-ALTER
    race raises ``duplicate column name`` — a schema error, so the 30s busy
    handler does not help.

    ``profile`` keeps its own branch, because it is the one column here that is
    more than a column: the ``ALTER`` is followed by a ``DROP INDEX`` and a
    ``CREATE UNIQUE INDEX``. **That pair is gated on the index's own shape, not
    on who added the column**, and the difference is a repair path. DDL
    autocommits, so the ``ALTER`` and the index swap were never one step:
    anything that ends the process in between — a crash, a lock on the
    ``DROP`` — leaves ``profile`` present beside the old single-column index,
    every later run reads the column as already there, and cross-profile
    inserts fail on a stale unique constraint for good with nothing saying so.
    Asking the index instead makes that state converge on the next run. It also
    covers the loser of the race, which previously aborted ``init_db`` loudly
    and now returns quietly: the loser sees the same half-applied index the
    winner is about to replace, and both statements are idempotent.

    A fresh database has no ``idx_monarch_synced_unique`` at all — ``SCHEMA``
    declares the constraint inline, so SQLite builds an autoindex under its own
    name — which is why the gate asks whether the *named* index exists and does
    not carry ``profile``, rather than whether it carries it. Asking the second
    way would create an index on every fresh install that no install has today.
    """
    sqlite_util.add_columns(
        conn,
        "monarch_synced_transactions",
        {"profile": "TEXT NOT NULL DEFAULT ''"},
        commit=True,
    )
    if _legacy_monarch_index(conn):
        conn.execute("DROP INDEX IF EXISTS idx_monarch_synced_unique")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_monarch_synced_unique "
            "ON monarch_synced_transactions(monarch_transaction_id, profile)"
        )
        conn.commit()

    # `contra_account`, then what the mapping consumed and which rules answered.
    # All nullable and populated going forward only: a row written before these
    # reads as "synced before rule tracing" rather than being guessed at.
    sqlite_util.add_columns(
        conn,
        "monarch_synced_transactions",
        {
            "contra_account": "TEXT",
            "src_category": "TEXT",
            "src_account": "TEXT",
            "src_source": "TEXT",
            "rule_ids": "TEXT",
        },
        commit=True,
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


def _profile_scope(profile: str | None) -> tuple[str, tuple[str, ...]]:
    """The optional ``profile`` predicate, as a SQL fragment and its params.

    Returns nothing for ``None`` — every profile — and an equality for any
    string, ``''`` included. Shared by the two coverage readers so the one
    that reports a count and the one that reports the values it excludes
    can never be looking at different sets of rows.

    The returned fragment is a **literal** and the caller's value travels as
    a bound parameter, never in the string. Both callers concatenate it into
    a query that already f-strings a column name, so a future edit that
    interpolated ``profile`` here would put a caller-supplied value into SQL
    text — the one thing this signature exists to keep out.
    """
    if profile is None:
        return "", ()
    return "AND profile = ?", (profile,)


def get_source_value_coverage(
    conn: sqlite3.Connection,
    *,
    field: str,
    limit: int = 500,
    profile: str | None = None,
) -> list[dict]:
    """Distinct source values seen in synced rows, and what they posted to.

    The coverage card's whole input: which categories (or accounts) an import
    actually carried, how often, when last, and the account the rules sent the
    most recent one to — so a row resolving to ``Expenses:Uncategorized:`` can
    be flagged without re-running a sync.

    ``posted_account`` is the *last* row's by ``txn_date``, not a set. A value
    mapping to two accounts across a rule edit would need a second query to
    say so, and the card's question is what happens now. That it is the last
    row's rests on a SQLite guarantee rather than on standard SQL: a bare
    column in an aggregate query with exactly one ``min()``/``max()`` takes
    its value from the row that matched the aggregate. Adding a second
    aggregate, or moving to another engine, silently makes it an arbitrary
    group member — which is why the query below carries the same note.

    Recategorized rows are excluded: they were reversed out of the ledger, so
    counting them would overstate a category the user has already dealt with.
    Rows with no value in the *queried* column are excluded too. For
    ``field='category'`` those are the ones
    :func:`get_untraced_synced_count` reports; for ``field='account'`` the
    exclusion is real but that count does not describe it, since a row can
    carry a category and no account.

    ``profile`` scopes the read to one profile's rows. A profile is bound to
    one ledger, so an unscoped read on a multi-profile deployment mixes
    ledgers: a category mapped on one and not on the other appears once,
    carrying whichever ledger's ``posted_account`` was written last, which
    reads as a coverage gap the rules for that ledger do not have. ``None``
    is every profile — the right answer for the single-profile shape, and
    the back-compatible one — while ``''`` is a scope of its own, selecting
    the rows a profile-less sync wrote. The two are not interchangeable, and
    ``routes._resolve_profile_query`` folds them together for the *config*
    accessors, where ``None`` means the global scope. That is the wrong
    translation here and must not be reused.
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

    scope, params = _profile_scope(profile)
    rows = conn.execute(
        # `posted_account` is bare under GROUP BY: SQLite takes it from the
        # row matching the single MAX(), which is the contract the docstring
        # states. A second aggregate here breaks that silently.
        f"""
        SELECT {column} AS value,
               COUNT(*) AS count,
               MAX(txn_date) AS last_seen,
               posted_account
        FROM monarch_synced_transactions
        WHERE recategorized_at IS NULL
          AND {column} IS NOT NULL AND {column} != ''
          {scope}
        GROUP BY {column}
        ORDER BY count DESC, value ASC
        LIMIT ?
        """,
        (*params, limit),
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


def get_untraced_synced_count(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
) -> int:
    """Active synced rows carrying no source category.

    Every row synced before rule tracing, and every row an import wrote with
    no category at all. Reported as one number rather than folded into the
    value list, because there is no value to report: the source category was
    never stored, and reading it back out of ``posted_account`` is lossy —
    ``account_component`` deletes punctuation, so "Food & Drink" and "Food
    Drink" both slug to ``FoodDrink``.

    ``profile`` scopes it exactly as :func:`get_source_value_coverage` does,
    and the two are meant to be called with the same value: this count is
    what the card renders beside that list to account for the rows missing
    from it, so a count over a wider set than the list would be a number
    naming rows the reader is not being shown.
    """
    scope, params = _profile_scope(profile)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM monarch_synced_transactions "
        "WHERE recategorized_at IS NULL "
        "AND (src_category IS NULL OR src_category = '') "
        f"{scope}",
        params,
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
