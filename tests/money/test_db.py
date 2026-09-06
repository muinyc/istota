"""Tests for money.db module."""


import pytest

from istota.money.db import (
    _migrate_monarch_synced_columns,
    clear_invoice_state,
    clear_overdue_notification,
    get_active_monarch_synced_transactions,
    get_db,
    get_invoice_schedule_state,
    get_notified_overdue_invoices,
    get_source_value_coverage,
    get_untraced_synced_count,
    init_db,
    is_content_hash_synced,
    is_monarch_transaction_synced,
    mark_invoice_overdue_notified,
    mark_monarch_transaction_recategorized,
    set_invoice_schedule_generation,
    set_invoice_schedule_reminder,
    track_csv_transactions_batch,
    track_monarch_transactions_batch,
    update_monarch_transaction_posted_account,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


class TestMonarchSync:
    def test_track_and_check(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 42.50, "merchant": "Store", "posted_account": "Expenses:Food"},
            ])
        with get_db(db) as conn:
            assert is_monarch_transaction_synced(conn, "txn-1") is True
            assert is_monarch_transaction_synced(conn, "txn-2") is False

    def test_get_active(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A", "posted_account": "Expenses:A", "txn_date": "2026-01-01"},
                {"id": "txn-2", "amount": 20, "merchant": "B", "posted_account": "Expenses:B", "txn_date": "2026-01-02"},
            ])
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 2

    def test_recategorize(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A", "posted_account": "Expenses:A"},
            ])
        with get_db(db) as conn:
            assert mark_monarch_transaction_recategorized(conn, "txn-1") is True
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 0

    def test_update_posted_account(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A", "posted_account": "Expenses:Old"},
            ])
        with get_db(db) as conn:
            assert update_monarch_transaction_posted_account(conn, "txn-1", "Expenses:New") is True
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert active[0].posted_account == "Expenses:New"

    def test_upsert(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A", "posted_account": "Expenses:A"},
            ])
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 20, "merchant": "A Updated", "posted_account": "Expenses:B"},
            ])
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 1
        assert active[0].merchant == "A Updated"

    def test_contra_account_round_trip(self, db):
        """contra_account is persisted and returned for reversal-based recat."""
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{
                "id": "txn-1", "amount": 40.89, "merchant": "eBay",
                "posted_account": "Income:Sales",
                "contra_account": "Equity:Owner-InvestmentDrawings",
                "txn_date": "2026-04-21",
            }])
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 1
        assert active[0].posted_account == "Income:Sales"
        assert active[0].contra_account == "Equity:Owner-InvestmentDrawings"

    def test_contra_account_optional_for_legacy_rows(self, db):
        """Older sync data without contra_account still works (returns None)."""
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{
                "id": "txn-1", "amount": 10, "merchant": "A",
                "posted_account": "Expenses:A",
            }])
        with get_db(db) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert active[0].contra_account is None

    def test_migration_adds_contra_account_column(self, tmp_path):
        """Existing DBs without contra_account get the column added on init."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        # Build a pre-migration schema without contra_account.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE monarch_synced_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monarch_transaction_id TEXT NOT NULL,
                synced_at TEXT DEFAULT (datetime('now')),
                tags_json TEXT,
                amount REAL,
                merchant TEXT,
                posted_account TEXT,
                txn_date TEXT,
                content_hash TEXT,
                recategorized_at TEXT,
                profile TEXT NOT NULL DEFAULT '',
                UNIQUE(monarch_transaction_id, profile)
            );
        """)
        conn.execute(
            "INSERT INTO monarch_synced_transactions "
            "(monarch_transaction_id, amount, merchant, posted_account) "
            "VALUES ('legacy-1', 10, 'L', 'Expenses:Old')"
        )
        conn.commit()
        conn.close()

        # Re-init: should add contra_account without dropping data.
        init_db(db_path)

        with get_db(db_path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(monarch_synced_transactions)").fetchall()}
        assert "contra_account" in cols

        with get_db(db_path) as conn:
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 1
        assert active[0].monarch_transaction_id == "legacy-1"
        assert active[0].contra_account is None


class TestContentHashDedup:
    def test_monarch_content_hash(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{"id": "txn-1", "content_hash": "abc123"}])
        with get_db(db) as conn:
            assert is_content_hash_synced(conn, "abc123") is True
            assert is_content_hash_synced(conn, "xyz789") is False

    def test_csv_content_hash(self, db):
        with get_db(db) as conn:
            track_csv_transactions_batch(conn, ["hash1", "hash2"], "export.csv")
        with get_db(db) as conn:
            assert is_content_hash_synced(conn, "hash1") is True
            assert is_content_hash_synced(conn, "hash3") is False

    def test_cross_source(self, db):
        with get_db(db) as conn:
            track_csv_transactions_batch(conn, ["shared_hash"], "export.csv")
        with get_db(db) as conn:
            assert is_content_hash_synced(conn, "shared_hash") is True


class TestCSVTracking:
    def test_batch_insert(self, db):
        with get_db(db) as conn:
            count = track_csv_transactions_batch(conn, ["h1", "h2", "h3"], "file.csv")
        assert count == 3

    def test_dedup_on_conflict(self, db):
        with get_db(db) as conn:
            track_csv_transactions_batch(conn, ["h1"], "file1.csv")
        with get_db(db) as conn:
            count = track_csv_transactions_batch(conn, ["h1", "h2"], "file2.csv")
        assert count == 1


class TestInvoiceScheduleState:
    def test_initial_state_none(self, db):
        with get_db(db) as conn:
            state = get_invoice_schedule_state(conn, "acme")
        assert state is None

    def test_set_reminder(self, db):
        with get_db(db) as conn:
            set_invoice_schedule_reminder(conn, "acme")
        with get_db(db) as conn:
            state = get_invoice_schedule_state(conn, "acme")
        assert state is not None
        assert state.last_reminder_at is not None

    def test_set_generation(self, db):
        with get_db(db) as conn:
            set_invoice_schedule_generation(conn, "acme")
        with get_db(db) as conn:
            state = get_invoice_schedule_state(conn, "acme")
        assert state.last_generation_at is not None


class TestInvoiceOverdue:
    def test_initial_empty(self, db):
        with get_db(db) as conn:
            result = get_notified_overdue_invoices(conn)
        assert result == set()

    def test_mark_and_get(self, db):
        with get_db(db) as conn:
            mark_invoice_overdue_notified(conn, "INV-001")
            mark_invoice_overdue_notified(conn, "INV-002")
        with get_db(db) as conn:
            result = get_notified_overdue_invoices(conn)
        assert result == {"INV-001", "INV-002"}

    def test_clear(self, db):
        with get_db(db) as conn:
            mark_invoice_overdue_notified(conn, "INV-001")
        with get_db(db) as conn:
            clear_overdue_notification(conn, "INV-001")
        with get_db(db) as conn:
            result = get_notified_overdue_invoices(conn)
        assert result == set()

    def test_idempotent(self, db):
        with get_db(db) as conn:
            mark_invoice_overdue_notified(conn, "INV-001")
            mark_invoice_overdue_notified(conn, "INV-001")
        with get_db(db) as conn:
            result = get_notified_overdue_invoices(conn)
        assert len(result) == 1


class TestMonarchSyncProfiles:
    """Tests for profile-aware Monarch sync tracking."""

    def test_track_with_profile(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 42.50, "merchant": "Store",
                 "posted_account": "Expenses:Food"},
            ], profile="business")
        with get_db(db) as conn:
            assert is_monarch_transaction_synced(conn, "txn-1", profile="business") is True
            assert is_monarch_transaction_synced(conn, "txn-1", profile="personal") is False
            # Without profile filter, finds it regardless
            assert is_monarch_transaction_synced(conn, "txn-1") is True

    def test_same_txn_different_profiles(self, db):
        """Same Monarch transaction can be tracked under different profiles."""
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 100, "merchant": "Store",
                 "posted_account": "Expenses:Business:Food"},
            ], profile="business")
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 100, "merchant": "Store",
                 "posted_account": "Expenses:Personal:Food"},
            ], profile="personal")
        with get_db(db) as conn:
            assert is_monarch_transaction_synced(conn, "txn-1", profile="business") is True
            assert is_monarch_transaction_synced(conn, "txn-1", profile="personal") is True

    def test_get_active_with_profile(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A",
                 "posted_account": "Expenses:A", "txn_date": "2026-01-01"},
            ], profile="business")
            track_monarch_transactions_batch(conn, [
                {"id": "txn-2", "amount": 20, "merchant": "B",
                 "posted_account": "Expenses:B", "txn_date": "2026-01-02"},
            ], profile="personal")
        with get_db(db) as conn:
            biz = get_active_monarch_synced_transactions(conn, profile="business")
            personal = get_active_monarch_synced_transactions(conn, profile="personal")
            all_active = get_active_monarch_synced_transactions(conn)
        assert len(biz) == 1
        assert biz[0].monarch_transaction_id == "txn-1"
        assert len(personal) == 1
        assert personal[0].monarch_transaction_id == "txn-2"
        assert len(all_active) == 2

    def test_backward_compat_no_profile(self, db):
        """Existing code that doesn't pass profile still works."""
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A",
                 "posted_account": "Expenses:A"},
            ])
        with get_db(db) as conn:
            assert is_monarch_transaction_synced(conn, "txn-1") is True
            active = get_active_monarch_synced_transactions(conn)
        assert len(active) == 1


class TestClearInvoiceState:
    def test_clears_overdue_notifications(self, db):
        with get_db(db) as conn:
            mark_invoice_overdue_notified(conn, "INV-000001")
            mark_invoice_overdue_notified(conn, "INV-000002")
        with get_db(db) as conn:
            result = clear_invoice_state(conn, "INV-000001")
        assert result["overdue_notifications_cleared"] == 1
        with get_db(db) as conn:
            remaining = get_notified_overdue_invoices(conn)
        assert remaining == {"INV-000002"}

    def test_clears_nothing_when_no_state(self, db):
        with get_db(db) as conn:
            result = clear_invoice_state(conn, "INV-999999")
        assert result["overdue_notifications_cleared"] == 0


class TestTheProfileUniqueIndex:
    """The `profile` migration is an ALTER plus an index swap, and they can part.

    DDL autocommits, so the two were never one step. A run that died between
    them — or the loser of the race, which used to abort `init_db` and now
    returns quietly — left `profile` present beside the old single-column
    index, and gating the swap on "did *this* call add the column" meant no
    later run would ever put it right.
    """

    _COLUMNS = (
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "monarch_transaction_id TEXT NOT NULL, "
        "synced_at TEXT DEFAULT (datetime('now')), "
        "recategorized_at TEXT"
    )

    def _index_columns(self, path):
        import sqlite3

        conn = sqlite3.connect(str(path))
        try:
            return [
                r[2]
                for r in conn.execute(
                    "PRAGMA index_info(idx_monarch_synced_unique)"
                )
            ]
        finally:
            conn.close()

    def _seed(self, path, extra_columns=""):
        import sqlite3

        conn = sqlite3.connect(str(path))
        conn.executescript(
            f"CREATE TABLE monarch_synced_transactions ({self._COLUMNS}"
            f"{extra_columns});"
            "CREATE UNIQUE INDEX idx_monarch_synced_unique "
            "ON monarch_synced_transactions(monarch_transaction_id);"
        )
        conn.commit()
        conn.close()

    def test_a_pre_profile_database_gains_the_two_column_index(self, tmp_path):
        path = tmp_path / "money.db"
        self._seed(path)

        init_db(path)

        assert self._index_columns(path) == ["monarch_transaction_id", "profile"]

    def test_a_half_applied_migration_is_repaired(self, tmp_path):
        """The column landed, the swap did not. The next run has to finish it."""
        path = tmp_path / "money.db"
        self._seed(path, ", profile TEXT NOT NULL DEFAULT ''")
        assert self._index_columns(path) == ["monarch_transaction_id"]

        init_db(path)

        assert self._index_columns(path) == ["monarch_transaction_id", "profile"]

    def test_a_fresh_database_gains_no_named_index(self, tmp_path):
        """`SCHEMA` declares the constraint inline, so SQLite names it itself.

        Asking whether the named index *carries* `profile` rather than whether
        it exists and does not would create an index here that no shipped
        install has, which is a schema change wearing a repair's clothes.
        """
        path = tmp_path / "money.db"

        init_db(path)

        assert self._index_columns(path) == []


class TestSourceProvenanceColumns:
    """Stage 3's four columns on ``monarch_synced_transactions``."""

    def test_columns_exist_on_a_fresh_db(self, db):
        with get_db(db) as conn:
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(monarch_synced_transactions)"
                )
            }
        assert {"src_category", "src_account", "src_source", "rule_ids"} <= cols

    def test_migration_adds_them_to_a_pre_existing_table(self, tmp_path):
        """A DB written before Stage 3 gains the columns and keeps its rows."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE monarch_synced_transactions (
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
                UNIQUE(monarch_transaction_id, profile)
            );
        """)
        conn.execute(
            "INSERT INTO monarch_synced_transactions "
            "(monarch_transaction_id, amount, merchant, posted_account) "
            "VALUES ('legacy-1', 10, 'L', 'Expenses:Old')"
        )
        conn.commit()
        conn.close()

        init_db(db_path)

        with get_db(db_path) as conn:
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(monarch_synced_transactions)"
                )
            }
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions"
            ).fetchone()
        assert {"src_category", "src_account", "src_source", "rule_ids"} <= cols
        assert row["monarch_transaction_id"] == "legacy-1"
        assert row["src_category"] is None
        assert row["rule_ids"] is None

    def test_a_second_init_is_a_no_op(self, db):
        init_db(db)
        init_db(db)
        with get_db(db) as conn:
            names = [
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(monarch_synced_transactions)"
                )
            ]
        assert names.count("src_category") == 1

    def test_a_concurrent_loser_does_not_raise(self, tmp_path):
        """``add_columns``' reason for existing, on the four new columns.

        ``init_db`` runs on every money web request, cron and skill
        invocation, so two connections routinely see a column absent
        *together* and the loser's ``duplicate column name`` is a schema error
        ``busy_timeout`` does not help.

        **The shape has to be built rather than approximated, and the version
        of this test that shipped before Stage 3 did not build it.** It landed
        the rival's four columns *before* calling the migration, so the
        migration's own ``PRAGMA table_info`` — which is inside the function —
        saw the finished schema and issued no ``ALTER`` at all. Reverting the
        four columns to a bare check-then-ALTER left it green. What
        distinguishes guarded from unguarded is a rival landing the column
        **between** this connection's read and its write, which is what
        `RacingConnection` does; `raced` is asserted below so a harness that
        stops interleaving fails rather than passing vacuously.
        """
        import sqlite3

        from ..support.sqlite_race import RacingConnection

        db_path = tmp_path / "race.db"
        # A pre-Stage-3 table, so the four columns are genuinely absent.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE monarch_synced_transactions (
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
                UNIQUE(monarch_transaction_id, profile)
            );
        """)
        conn.commit()
        conn.close()

        loser = sqlite3.connect(str(db_path))
        loser.row_factory = sqlite3.Row
        winner = sqlite3.connect(str(db_path))
        # `profile` and `contra_account` are already present on the table
        # above, so the first ALTER this migration issues is `src_category`.
        racing = RacingConnection(loser, winner, race_on=0)
        try:
            # The loser must finish quietly rather than raise.
            _migrate_monarch_synced_columns(racing)
            loser.commit()
        finally:
            loser.close()
            winner.close()

        assert racing.raced, "the harness did not interleave; the test is vacuous"

        with get_db(db_path) as conn:
            names = [
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(monarch_synced_transactions)"
                )
            ]
        assert names.count("src_category") == 1
        assert names.count("rule_ids") == 1

    def test_batch_writes_the_provenance(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{
                "id": "txn-1", "amount": -35.0, "merchant": "Hi Tops",
                "posted_account": "Expenses:Business:Meals",
                "contra_account": "Liabilities:Visa",
                "txn_date": "2026-07-13",
                "src_category": "Meals", "src_account": "Visa",
                "src_source": "monarch-api", "rule_ids": "[7, 9]",
            }])
        with get_db(db) as conn:
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions"
            ).fetchone()
        assert row["src_category"] == "Meals"
        assert row["src_account"] == "Visa"
        assert row["src_source"] == "monarch-api"
        assert row["rule_ids"] == "[7, 9]"

    def test_an_upsert_refreshes_the_provenance(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{
                "id": "txn-1", "amount": -35.0, "merchant": "Hi Tops",
                "src_category": "Meals", "rule_ids": "[7]",
            }])
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [{
                "id": "txn-1", "amount": -35.0, "merchant": "Hi Tops",
                "src_category": "Restaurants", "rule_ids": "[8]",
            }])
        with get_db(db) as conn:
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions"
            ).fetchone()
        assert row["src_category"] == "Restaurants"
        assert row["rule_ids"] == "[8]"

    def test_a_caller_that_supplies_nothing_leaves_them_null(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "txn-1", "amount": 10, "merchant": "A"},
            ])
        with get_db(db) as conn:
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions"
            ).fetchone()
        assert row["src_category"] is None
        assert row["src_source"] is None


class TestSourceValueCoverage:
    def _seed(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "a", "amount": -1, "merchant": "A", "txn_date": "2026-07-01",
                 "src_category": "Meals", "src_account": "Visa",
                 "posted_account": "Expenses:Business:Meals"},
                {"id": "b", "amount": -2, "merchant": "B", "txn_date": "2026-07-05",
                 "src_category": "Meals", "src_account": "Checking",
                 "posted_account": "Expenses:Business:Meals"},
                {"id": "c", "amount": -3, "merchant": "C", "txn_date": "2026-07-03",
                 "src_category": "Groceries", "src_account": "Visa",
                 "posted_account": "Expenses:Uncategorized:Groceries"},
                # No provenance: synced before rule tracing.
                {"id": "d", "amount": -4, "merchant": "D", "txn_date": "2026-06-01",
                 "posted_account": "Expenses:Old"},
            ])

    def test_counts_and_last_seen(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        by_value = {v["value"]: v for v in values}
        assert by_value["Meals"]["count"] == 2
        assert by_value["Meals"]["last_seen"] == "2026-07-05"
        assert by_value["Groceries"]["count"] == 1
        assert "" not in by_value
        assert None not in by_value

    def test_ordered_by_count_descending(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        assert [v["value"] for v in values] == ["Meals", "Groceries"]

    def test_the_account_field(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="account")
        by_value = {v["value"]: v["count"] for v in values}
        assert by_value == {"Visa": 2, "Checking": 1}

    def test_it_carries_the_account_the_rules_posted_to(self, db):
        """So the card can flag what fell through without re-running a sync."""
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        by_value = {v["value"]: v for v in values}
        assert by_value["Groceries"]["posted_account"] == (
            "Expenses:Uncategorized:Groceries"
        )

    def test_the_account_is_the_most_recent_rows(self, db):
        """A category whose account changed reports the account in force now.

        The discriminating case: both rows are the same source value, so a
        query returning an arbitrary group member passes every other
        assertion in this class and fails this one.
        """
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "old", "amount": -1, "merchant": "A",
                 "txn_date": "2026-01-01", "src_category": "Software",
                 "posted_account": "Expenses:Uncategorized:Software"},
                {"id": "new", "amount": -2, "merchant": "B",
                 "txn_date": "2026-08-01", "src_category": "Software",
                 "posted_account": "Expenses:Business:Software"},
            ])
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        row = next(v for v in values if v["value"] == "Software")
        assert row["count"] == 2
        assert row["last_seen"] == "2026-08-01"
        assert row["posted_account"] == "Expenses:Business:Software"

    def test_recategorized_rows_are_excluded(self, db):
        self._seed(db)
        with get_db(db) as conn:
            mark_monarch_transaction_recategorized(conn, "b")
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        by_value = {v["value"]: v["count"] for v in values}
        assert by_value["Meals"] == 1

    def test_an_unknown_field_is_refused(self, db):
        with get_db(db) as conn:
            with pytest.raises(ValueError):
                get_source_value_coverage(conn, field="payee")

    def test_the_limit_bounds_the_list(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category", limit=1)
        assert len(values) == 1

    def test_untraced_rows_are_counted_separately(self, db):
        self._seed(db)
        with get_db(db) as conn:
            assert get_untraced_synced_count(conn) == 1

    def test_untraced_count_excludes_recategorized_rows(self, db):
        self._seed(db)
        with get_db(db) as conn:
            mark_monarch_transaction_recategorized(conn, "d")
        with get_db(db) as conn:
            assert get_untraced_synced_count(conn) == 0

    def test_an_empty_table_is_an_empty_list(self, db):
        with get_db(db) as conn:
            assert get_source_value_coverage(conn, field="category") == []
            assert get_untraced_synced_count(conn) == 0


class TestCoverageIsScopedByProfile:
    """A multi-profile deployment syncs several ledgers into one table.

    ``monarch_synced_transactions`` is keyed ``(monarch_transaction_id,
    profile)`` and a profile is bound to one ledger, so an unscoped read
    mixes ledgers: a category mapped on one ledger and not on another shows
    up once, carrying whichever ledger's account was written last. The
    card's question is per-scope, so the query has to be.
    """

    def _seed(self, db):
        with get_db(db) as conn:
            track_monarch_transactions_batch(conn, [
                {"id": "a", "amount": -1, "merchant": "A", "txn_date": "2026-07-01",
                 "src_category": "Software", "src_account": "Visa",
                 "posted_account": "Expenses:Biz:Software"},
            ], profile="biz")
            track_monarch_transactions_batch(conn, [
                {"id": "b", "amount": -2, "merchant": "B", "txn_date": "2026-07-02",
                 "src_category": "Groceries", "src_account": "Checking",
                 "posted_account": "Expenses:Home:Groceries"},
                {"id": "c", "amount": -3, "merchant": "C", "txn_date": "2026-07-03"},
            ], profile="home")
            # The no-profiles shape writes ''. It is a stored value, not a
            # "no filter" marker, and the two must stay tellable apart.
            track_monarch_transactions_batch(conn, [
                {"id": "d", "amount": -4, "merchant": "D", "txn_date": "2026-07-04",
                 "src_category": "Rent", "posted_account": "Expenses:Rent"},
            ])

    def test_a_named_profile_sees_only_its_own_rows(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category", profile="biz")
        assert [v["value"] for v in values] == ["Software"]

    def test_the_default_is_every_profile(self, db):
        """Back-compatible, and right for the single-profile shape."""
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category")
        assert sorted(v["value"] for v in values) == ["Groceries", "Rent", "Software"]

    def test_the_empty_profile_is_a_scope_not_a_wildcard(self, db):
        """``''`` selects the no-profiles sync's rows; ``None`` selects all."""
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="category", profile="")
        assert [v["value"] for v in values] == ["Rent"]

    def test_the_account_field_is_scoped_too(self, db):
        self._seed(db)
        with get_db(db) as conn:
            values = get_source_value_coverage(conn, field="account", profile="home")
        assert [v["value"] for v in values] == ["Checking"]

    def test_the_untraced_count_is_scoped(self, db):
        self._seed(db)
        with get_db(db) as conn:
            assert get_untraced_synced_count(conn, profile="home") == 1
            assert get_untraced_synced_count(conn, profile="biz") == 0
            assert get_untraced_synced_count(conn) == 1
