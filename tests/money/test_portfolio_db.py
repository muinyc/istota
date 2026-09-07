"""Tests for the portfolio storage module (schema, snapshots, analytics)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from istota import sqlite_util
from istota.money import portfolio
from istota.money.core.importers import IMPORT_SOURCES, detect_source
from istota.money.core.importers.positions_base import ParsedSnapshot, PositionRow

from ..support.sqlite_race import RacingConnection

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "money.db"))
    conn.row_factory = sqlite3.Row
    portfolio.ensure_schema(conn)
    yield conn
    conn.close()


def make_row(**kw) -> PositionRow:
    defaults = dict(
        account_number="X1",
        account_name="Taxable Brokerage",
        symbol="VTI",
        description="VANGUARD TOTAL STK",
        row_type="position",
        quantity=10.0,
        price=100.0,
        value=1000.0,
        cost_basis=900.0,
        avg_cost_basis=90.0,
        day_gain=None,
        day_gain_pct=None,
        total_gain=None,
        total_gain_pct=None,
        pct_of_account=None,
        security_type="Cash",
    )
    defaults.update(kw)
    return PositionRow(**defaults)


def make_snapshot(rows, exported_at=None, **kw) -> ParsedSnapshot:
    defaults = dict(
        exported_at=exported_at or datetime(2026, 1, 15, 10, 0),
        exported_at_estimated=False,
        rows=rows,
        source="fidelity-positions-csv",
        warnings=[],
    )
    defaults.update(kw)
    return ParsedSnapshot(**defaults)


class TestSchema:
    def test_ensure_schema_idempotent(self, conn):
        portfolio.ensure_schema(conn)
        portfolio.ensure_schema(conn)
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "portfolio_snapshots",
            "portfolio_accounts",
            "portfolio_positions",
            "portfolio_classifications",
            "schema_meta",
        } <= tables

    def test_owner_column_migrates_to_account_group(self, tmp_path):
        """A DB created before the rename carries ``owner``; ensure_schema
        renames it in place and the stored labels survive."""
        db = sqlite3.connect(str(tmp_path / "money.db"))
        db.execute(
            "CREATE TABLE portfolio_accounts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "account_name TEXT NOT NULL UNIQUE, "
            "account_number TEXT NOT NULL DEFAULT '', "
            "owner TEXT NOT NULL DEFAULT '', "
            "account_type TEXT NOT NULL DEFAULT '', "
            "excluded INTEGER NOT NULL DEFAULT 0, "
            "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO portfolio_accounts "
            "(account_name, owner, first_seen_at, last_seen_at) "
            "VALUES ('Taxable Brokerage', 'Alice', '2026-01-01', '2026-01-01')"
        )
        db.commit()
        portfolio.ensure_schema(db)
        cols = {r[1] for r in db.execute("PRAGMA table_info(portfolio_accounts)")}
        assert "account_group" in cols
        assert "owner" not in cols
        accounts = portfolio.list_accounts(db)
        assert accounts[0].group == "Alice"
        # Idempotent on a second run.
        portfolio.ensure_schema(db)
        db.close()

    def test_init_db_creates_portfolio_tables(self, tmp_path):
        from istota.money.db import get_db, init_db

        db_path = tmp_path / "money.db"
        init_db(db_path)
        with get_db(db_path) as c:
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE name='portfolio_snapshots'"
            ).fetchall()
            assert rows


class TestNormalizeSymbol:
    def test_strips_trailing_stars_and_uppercases(self):
        assert portfolio.normalize_symbol("SPAXX**") == "SPAXX"
        assert portfolio.normalize_symbol("USD***") == "USD"
        assert portfolio.normalize_symbol("nwax.u") == "NWAX.U"
        assert portfolio.normalize_symbol("**") == ""
        assert portfolio.normalize_symbol("") == ""


class TestInsertAndDedup:
    def test_insert_returns_summary(self, conn):
        result = portfolio.insert_snapshot(conn, make_snapshot([make_row()]))
        assert result["status"] == "ok"
        assert result["snapshot_id"] >= 1
        assert result["position_count"] == 1
        assert result["total_value"] == 1000.0
        assert result["new_accounts"] == ["Taxable Brokerage"]

    def test_reimport_is_duplicate(self, conn):
        first = portfolio.insert_snapshot(conn, make_snapshot([make_row()]))
        second = portfolio.insert_snapshot(conn, make_snapshot([make_row()]))
        assert second["status"] == "duplicate"
        assert second["snapshot_id"] == first["snapshot_id"]
        count = conn.execute("SELECT COUNT(*) c FROM portfolio_snapshots").fetchone()["c"]
        assert count == 1

    def test_hash_excludes_exported_at(self, conn):
        portfolio.insert_snapshot(
            conn, make_snapshot([make_row()], exported_at=datetime(2026, 1, 1))
        )
        second = portfolio.insert_snapshot(
            conn, make_snapshot([make_row()], exported_at=datetime(2026, 2, 1))
        )
        assert second["status"] == "duplicate"

    def test_different_content_is_new_snapshot(self, conn):
        portfolio.insert_snapshot(conn, make_snapshot([make_row()]))
        second = portfolio.insert_snapshot(
            conn, make_snapshot([make_row(quantity=11.0, value=1100.0)])
        )
        assert second["status"] == "ok"
        count = conn.execute("SELECT COUNT(*) c FROM portfolio_snapshots").fetchone()["c"]
        assert count == 2

    def test_unique_race_loser_gets_duplicate(self, conn, tmp_path):
        # Simulate the race by pre-inserting the same hash directly.
        snap = make_snapshot([make_row()])
        first = portfolio.insert_snapshot(conn, snap)
        # a second connection importing the same content
        conn2 = sqlite3.connect(str(tmp_path / "money.db"))
        conn2.row_factory = sqlite3.Row
        result = portfolio.insert_snapshot(conn2, snap)
        conn2.close()
        assert result["status"] == "duplicate"
        assert result["snapshot_id"] == first["snapshot_id"]

    def test_unclassified_symbols_reported(self, conn):
        result = portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(),  # VTI — seeded classification
                make_row(symbol="ZZZT", description="MYSTERY CORP", value=50.0),
            ]),
        )
        assert result["unclassified_symbols"] == ["ZZZT"]


class TestAccountRegistry:
    def test_types_guessed_on_first_sight(self, conn):
        portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(account_name="Roth IRA A"),
                make_row(account_name="Active Trading (IBKR)", symbol="GOOG"),
                make_row(account_name="Tax Reserve", symbol="CORE**", row_type="cash"),
                make_row(account_name="Joint Brokerage", symbol="VOO"),
            ]),
        )
        accounts = {a.account_name: a for a in portfolio.list_accounts(conn)}
        assert accounts["Roth IRA A"].account_type == "retirement"
        assert accounts["Active Trading (IBKR)"].account_type == "trading"
        assert accounts["Tax Reserve"].account_type == "cash"
        assert accounts["Joint Brokerage"].account_type == "taxable"
        assert all(a.group == "" for a in accounts.values())
        assert all(a.excluded is False for a in accounts.values())

    def test_never_reguessed_and_last_seen_advances(self, conn):
        portfolio.insert_snapshot(
            conn, make_snapshot([make_row(account_name="Roth IRA A")])
        )
        (acct,) = portfolio.list_accounts(conn)
        portfolio.update_account(conn, acct.id, account_type="custom-type", group="Alice")
        conn.execute(
            "UPDATE portfolio_accounts SET last_seen_at = '2000-01-01T00:00:00'"
        )
        portfolio.insert_snapshot(
            conn,
            make_snapshot([make_row(account_name="Roth IRA A", quantity=99.0)]),
        )
        (acct2,) = portfolio.list_accounts(conn)
        assert acct2.account_type == "custom-type"
        assert acct2.group == "Alice"
        assert acct2.last_seen_at > "2000-01-01"

    def test_group_hints_fill_blank_only(self, conn):
        portfolio.insert_snapshot(
            conn,
            make_snapshot(
                [make_row(account_name="Taxable Brokerage")],
                group_hints={"Taxable Brokerage": "Alice"},
            ),
        )
        (acct,) = portfolio.list_accounts(conn)
        assert acct.group == "Alice"
        portfolio.update_account(conn, acct.id, group="Somebody Else")
        portfolio.insert_snapshot(
            conn,
            make_snapshot(
                [make_row(account_name="Taxable Brokerage", quantity=1.0)],
                group_hints={"Taxable Brokerage": "Alice"},
            ),
        )
        (acct2,) = portfolio.list_accounts(conn)
        assert acct2.group == "Somebody Else"

    def test_update_account_validates_id(self, conn):
        assert portfolio.update_account(conn, 999, group="X") is False


class TestClassifications:
    def test_seeded_once(self, conn):
        rows = portfolio.list_classifications(conn)
        by_symbol = {c.symbol_norm: c for c in rows}
        assert by_symbol["VTI"].asset_class == "Stocks"
        assert by_symbol["SPAXX"].asset_class == "Cash & Equivalents"
        assert len(rows) >= 25

    def test_user_delete_survives_reseed(self, conn):
        assert portfolio.delete_classification(conn, "VTI") is True
        portfolio.ensure_schema(conn)  # re-init must not resurrect
        assert "VTI" not in {c.symbol_norm for c in portfolio.list_classifications(conn)}

    def test_set_classification_normalizes(self, conn):
        portfolio.set_classification(
            conn, "zzzt**", asset_class="Stocks", sub_class="Small Cap", geography="US"
        )
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZT"].sub_class == "Small Cap"

    def test_fallback_chain(self, conn):
        # cash by symbol/description patterns
        assert portfolio.resolve_classification(conn, "FDRXX", "GOVT MONEY MARKET", "", "position")[0] == "Cash & Equivalents"
        assert portfolio.resolve_classification(conn, "", "", "", "cash")[0] == "Cash & Equivalents"
        # options by security_type and by description shape
        cls = portfolio.resolve_classification(conn, "MU", "MU 18JUL25 120 C", "", "position")
        assert cls[0] == "Options"
        assert cls[1] == "Call Options"
        cls = portfolio.resolve_classification(conn, "MU", "MU 18JUL25 120 P", "Option", "position")
        assert cls[1] == "Put Options"
        # unknown
        assert portfolio.resolve_classification(conn, "ZZZQ", "MYSTERY", "", "position")[0] == "Unclassified"

    def test_explicit_row_wins_over_fallback(self, conn):
        portfolio.set_classification(conn, "FDRXX", asset_class="Fixed Income")
        assert portfolio.resolve_classification(conn, "FDRXX", "GOVT MONEY MARKET", "", "position")[0] == "Fixed Income"

    def test_seed_rows_carry_seed_source(self, conn):
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["VTI"].source == "seed"

    def test_set_classification_defaults_to_user_source(self, conn):
        portfolio.set_classification(conn, "ZZZT", asset_class="Stocks")
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZT"].source == "user"

    def test_set_classification_accepts_explicit_source(self, conn):
        portfolio.set_classification(
            conn, "ZZZT", asset_class="Stocks", source="auto"
        )
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZT"].source == "auto"

    def test_source_column_migrates_in_place(self, tmp_path):
        """A DB created before the source column gets it via ALTER; existing
        rows read back with the empty-string default."""
        db = tmp_path / "money.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE portfolio_classifications (
                symbol_norm TEXT PRIMARY KEY,
                asset_class TEXT NOT NULL,
                sub_class TEXT NOT NULL DEFAULT '',
                geography TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            INSERT INTO portfolio_classifications VALUES
                ('ZZZT', 'Stocks', 'Large Cap', 'US', '2026-01-01T00:00:00');
            """
        )
        conn.commit()
        portfolio.ensure_schema(conn)
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZT"].source == ""
        assert by_symbol["ZZZT"].asset_class == "Stocks"
        conn.close()

    def test_concurrent_migration_does_not_raise(self, tmp_path):
        """ensure_schema runs on every money web request, scheduler cron and
        skill invocation, so the first post-upgrade moment is routinely
        several connections at once. Check-then-ALTER let both see the column
        absent, and the loser raised `duplicate column name` — a schema
        error, so busy_timeout does not help.

        The interleave is forced rather than raced: the rival lands the column
        between this connection's read and its own ALTER, which is the loser's
        exact sequence. Staged through `RacingConnection` rather than through a
        patched column reader, so the test does not have to name the helper's
        internals — the pre-Stage-3 version patched `portfolio._table_columns`,
        which `_migrate_classification_source` no longer calls.
        """
        db = tmp_path / "money.db"
        setup = sqlite3.connect(str(db))
        setup.executescript(
            """
            CREATE TABLE portfolio_classifications (
                symbol_norm TEXT PRIMARY KEY,
                asset_class TEXT NOT NULL,
                sub_class TEXT NOT NULL DEFAULT '',
                geography TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )
        setup.commit()
        setup.close()

        winner = sqlite3.connect(str(db))
        loser = sqlite3.connect(str(db))
        racing = RacingConnection(loser, winner)
        try:
            portfolio._migrate_classification_source(racing)
            assert racing.raced, "the harness did not interleave; the test is vacuous"
            cols = portfolio._table_columns(loser, "portfolio_classifications")
            assert "source" in cols
        finally:
            winner.close()
            loser.close()

    def test_migration_reraises_a_real_alter_failure(self, tmp_path):
        """The guard tolerates losing the race, not an ALTER that genuinely
        failed — otherwise a broken migration is silent.

        Against `sqlite_util.add_columns` directly, since this asserts the
        helper's contract rather than the migration's; the migration cannot
        reach it, because its own column adds cleanly.
        """
        db = tmp_path / "money.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("CREATE TABLE t (a TEXT);")
        conn.commit()
        try:
            with pytest.raises(sqlite3.OperationalError):
                sqlite_util.add_columns(conn, "t", {"b": "TEXT UNIQUE"})
        finally:
            conn.close()

    def test_migration_skips_a_missing_table(self, tmp_path):
        """PRAGMA table_info on a missing table returns no rows, which reads
        as "column absent" and would point the ALTER at nothing."""
        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        try:
            portfolio._migrate_classification_source(conn)
            portfolio._migrate_owner_to_group(conn)
        finally:
            conn.close()


class TestSummaryAndExclusion:
    def _seed(self, conn):
        return portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(account_name="Taxable Brokerage", symbol="VTI", value=6000.0, cost_basis=5000.0),
                make_row(account_name="Taxable Brokerage", symbol="SPAXX**", row_type="cash",
                         value=1000.0, cost_basis=None, quantity=None),
                make_row(account_name="Tax Reserve", symbol="CORE**", row_type="cash",
                         value=3000.0, cost_basis=None, quantity=None),
                make_row(account_name="Joint Brokerage", symbol="VTI", value=2000.0, cost_basis=1500.0),
            ], group_hints={"Taxable Brokerage": "Alice", "Tax Reserve": "Alice",
                            "Joint Brokerage": "Bob"}),
        )

    def test_summary_totals_and_groups(self, conn):
        result = self._seed(conn)
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"])
        assert summary["total_value"] == 12000.0
        by_class = {g["key"]: g["value"] for g in summary["by_asset_class"]}
        assert by_class["Stocks"] == 8000.0
        assert by_class["Cash & Equivalents"] == 4000.0
        by_group = {g["key"]: g["value"] for g in summary["by_group"]}
        assert by_group["Alice"] == 10000.0
        assert by_group["Bob"] == 2000.0

    def test_holdings_aggregate_and_cash_collapse(self, conn):
        result = self._seed(conn)
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"])
        holdings = {h["symbol"]: h for h in summary["holdings"]}
        assert holdings["VTI"]["value"] == 8000.0
        assert holdings["VTI"]["quantity"] == 20.0
        assert holdings["VTI"]["cost_basis"] == 6500.0
        assert holdings["VTI"]["gain"] == pytest.approx(1500.0)
        assert holdings["VTI"]["accounts"] == 2
        assert holdings["CASH"]["value"] == 4000.0
        # sorted by value descending
        assert summary["holdings"][0]["symbol"] == "VTI"

    def test_excluded_account_filtered_everywhere(self, conn):
        result = self._seed(conn)
        accounts = {a.account_name: a for a in portfolio.list_accounts(conn)}
        portfolio.update_account(conn, accounts["Tax Reserve"].id, excluded=True)
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"])
        assert summary["total_value"] == 9000.0
        assert "Tax Reserve" not in {g["key"] for g in summary["by_account"]}
        series = portfolio.history_series(conn)
        assert series["series"][0]["total"] == 9000.0
        snaps = portfolio.list_snapshots(conn)
        assert snaps[0]["total_value"] == 9000.0
        # stored raw total keeps the full number
        raw = conn.execute("SELECT total_value FROM portfolio_snapshots").fetchone()[0]
        assert raw == 12000.0

    def test_group_filter(self, conn):
        result = self._seed(conn)
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"], group="Bob")
        assert summary["total_value"] == 2000.0

    def test_pending_rows_count_in_totals(self, conn):
        result = portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(value=1000.0),
                make_row(symbol="", description="Pending Activity", row_type="pending",
                         value=-100.0, quantity=None, cost_basis=None),
            ]),
        )
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"])
        assert summary["total_value"] == 900.0


class TestHistoryAndDiff:
    def _two_snapshots(self, conn):
        r1 = portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(symbol="VTI", quantity=10.0, value=1000.0),
                make_row(symbol="TMF", quantity=5.0, value=500.0),
            ], exported_at=datetime(2026, 1, 1)),
        )
        r2 = portfolio.insert_snapshot(
            conn,
            make_snapshot([
                make_row(symbol="VTI", quantity=20.0, value=2100.0),
                make_row(symbol="SGOV", quantity=3.0, value=300.0),
            ], exported_at=datetime(2026, 2, 1)),
        )
        return r1["snapshot_id"], r2["snapshot_id"]

    def test_history_series_total_and_grouped(self, conn):
        self._two_snapshots(conn)
        series = portfolio.history_series(conn)
        assert [p["total"] for p in series["series"]] == [1500.0, 2400.0]
        grouped = portfolio.history_series(conn, group_by="asset_class")
        assert grouped["series"][0]["groups"]["Stocks"] == 1000.0
        assert grouped["series"][0]["groups"]["Fixed Income"] == 500.0

    def test_symbol_history(self, conn):
        self._two_snapshots(conn)
        hist = portfolio.symbol_history(conn, "vti")
        assert hist["symbol"] == "VTI"
        assert [p["quantity"] for p in hist["points"]] == [10.0, 20.0]
        assert [p["value"] for p in hist["points"]] == [1000.0, 2100.0]

    def test_snapshot_diff(self, conn):
        older, newer = self._two_snapshots(conn)
        diff = portfolio.snapshot_diff(conn, older, newer)
        opened = {d["symbol"] for d in diff["opened"]}
        closed = {d["symbol"] for d in diff["closed"]}
        changed = {d["symbol"]: d for d in diff["changed"]}
        assert opened == {"SGOV"}
        assert closed == {"TMF"}
        assert "VTI" in changed
        assert changed["VTI"]["quantity_from"] == 10.0
        assert changed["VTI"]["quantity_to"] == 20.0

    def test_diff_ignores_noise(self, conn):
        r1 = portfolio.insert_snapshot(
            conn,
            make_snapshot([make_row(value=1000.0)], exported_at=datetime(2026, 1, 1)),
        )
        r2 = portfolio.insert_snapshot(
            conn,
            make_snapshot([make_row(value=1000.005)], exported_at=datetime(2026, 2, 1)),
        )
        diff = portfolio.snapshot_diff(conn, r1["snapshot_id"], r2["snapshot_id"])
        assert diff["changed"] == []

    def test_list_snapshots_newest_first(self, conn):
        self._two_snapshots(conn)
        snaps = portfolio.list_snapshots(conn)
        assert len(snaps) == 2
        assert snaps[0]["exported_at"] > snaps[1]["exported_at"]
        assert snaps[0]["position_count"] == 2


class TestDelete:
    def test_delete_cascades_without_pragma(self, conn):
        result = portfolio.insert_snapshot(conn, make_snapshot([make_row()]))
        assert portfolio.delete_snapshot(conn, result["snapshot_id"]) is True
        assert conn.execute("SELECT COUNT(*) c FROM portfolio_positions").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM portfolio_snapshots").fetchone()["c"] == 0

    def test_delete_missing_returns_false(self, conn):
        assert portfolio.delete_snapshot(conn, 42) is False

    def test_delete_frees_hash_for_reimport(self, conn):
        snap = make_snapshot([make_row()])
        r1 = portfolio.insert_snapshot(conn, snap)
        portfolio.delete_snapshot(conn, r1["snapshot_id"])
        r2 = portfolio.insert_snapshot(conn, snap)
        assert r2["status"] == "ok"


class TestImporterRegistry:
    def test_kind_axis(self):
        assert IMPORT_SOURCES["monarch-csv"].kind == "transactions"
        assert IMPORT_SOURCES["fidelity-positions-csv"].kind == "positions"
        assert IMPORT_SOURCES["fina-history-csv"].kind == "positions"

    def test_detect_source_routes_positions(self):
        assert detect_source(FIXTURES / "fidelity_positions_2025.csv") == "fidelity-positions-csv"
        assert detect_source(FIXTURES / "fidelity_positions_2026.csv") == "fidelity-positions-csv"
        assert detect_source(FIXTURES / "fina_history_small.csv") == "fina-history-csv"

    def test_parse_positions_file_auto_detects(self):
        from istota.money.core.importers import parse_positions_file

        snapshots = parse_positions_file(FIXTURES / "fidelity_positions_2025.csv")
        assert len(snapshots) == 1
        snapshots = parse_positions_file(FIXTURES / "fina_history_small.csv")
        assert len(snapshots) == 3

    def test_parse_positions_file_rejects_transactions_source(self):
        from istota.money.core.importers import parse_positions_file
        from istota.money.core.importers.positions_base import PositionParseError

        with pytest.raises(PositionParseError, match="import-csv"):
            parse_positions_file(
                FIXTURES / "fidelity_positions_2025.csv", "monarch-csv"
            )

    def test_parse_positions_file_unknown_source(self, tmp_path):
        from istota.money.core.importers import parse_positions_file
        from istota.money.core.importers.positions_base import PositionParseError

        with pytest.raises(PositionParseError, match="Unknown"):
            parse_positions_file(FIXTURES / "fidelity_positions_2025.csv", "nope")

    def test_parse_positions_file_undetectable(self, tmp_path):
        from istota.money.core.importers import parse_positions_file
        from istota.money.core.importers.positions_base import PositionParseError

        bogus = tmp_path / "bogus.csv"
        bogus.write_text("a,b,c\n1,2,3\n")
        with pytest.raises(PositionParseError, match="detect"):
            parse_positions_file(bogus)


class TestFixtureRoundTrip:
    """End-to-end: parse the real fixtures and store them."""

    def test_fidelity_2025_round_trip(self, conn):
        from istota.money.core.importers.fidelity_positions import (
            parse_fidelity_positions_csv,
        )

        (snap,) = parse_fidelity_positions_csv(FIXTURES / "fidelity_positions_2025.csv")
        result = portfolio.insert_snapshot(conn, snap)
        assert result["status"] == "ok"
        assert result["position_count"] == 45
        accounts = portfolio.list_accounts(conn)
        assert len(accounts) == 12
        summary = portfolio.snapshot_summary(conn, result["snapshot_id"])
        assert summary["total_value"] == pytest.approx(
            sum(r.value for r in snap.rows if r.value is not None)
        )

    def test_fina_history_round_trip(self, conn):
        from istota.money.core.importers.fina_history import parse_fina_history_csv

        snapshots = parse_fina_history_csv(FIXTURES / "fina_history_small.csv")
        results = [portfolio.insert_snapshot(conn, s) for s in snapshots]
        assert [r["status"] for r in results] == ["ok", "ok", "ok"]
        # re-running the whole import is a no-op
        again = [portfolio.insert_snapshot(conn, s) for s in snapshots]
        assert [r["status"] for r in again] == ["duplicate"] * 3
        accounts = {a.account_name: a for a in portfolio.list_accounts(conn)}
        assert accounts["Taxable Brokerage"].group == "Alice"
        assert accounts["Joint Brokerage"].group == "Bob"
