"""`sqlite_util.add_columns`, and the race it exists to survive.

The race is the whole subject, so the harness has to actually produce it.
`tests/support/sqlite_race.RacingConnection` is that harness and its docstring
carries the reasoning; ``test_the_harness_reproduces_the_race_against_an_
unguarded_alter`` below is the in-suite control that it can still make an
unguarded statement fail. Delete that control and this file stops being
evidence of anything.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import sqlite_util
from .support.sqlite_race import RacingConnection


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture()
def racing(tmp_path):
    """A one-column table, a connection about to lose a race for a second."""
    path = tmp_path / "race.db"
    loser = sqlite3.connect(path, timeout=5.0)
    winner = sqlite3.connect(path, timeout=5.0)
    loser.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    loser.commit()
    try:
        yield RacingConnection(loser, winner)
    finally:
        loser.close()
        winner.close()


class TestTheRace:
    def test_the_harness_reproduces_the_race_against_an_unguarded_alter(self, racing):
        """The control. Without this the tests below could be vacuous."""
        with pytest.raises(sqlite3.OperationalError, match="duplicate column name"):
            racing.execute("ALTER TABLE t ADD COLUMN c TEXT")
        assert racing.raced

    def test_both_connections_survive_one_first_upgrade_alter(self, racing):
        added = sqlite_util.add_columns(racing, "t", {"c": "TEXT"})

        assert racing.raced, "the harness did not interleave; the test is vacuous"
        assert added == [], "the rival added the column, so this call added nothing"
        assert _columns(racing._conn, "t") == {"id", "c"}

    def test_the_columns_it_did_win_come_back(self, racing):
        added = sqlite_util.add_columns(
            racing, "t", {"c": "TEXT", "d": "INTEGER NOT NULL DEFAULT 0"}
        )

        assert racing.raced
        assert added == ["d"], "only the second column was this connection's to add"
        assert _columns(racing._conn, "t") == {"id", "c", "d"}


class TestWhatItStillRaises:
    def test_a_genuine_operational_error_propagates(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

        with pytest.raises(sqlite3.OperationalError):
            # SQLite cannot add a UNIQUE column to an existing table.
            sqlite_util.add_columns(conn, "t", {"c": "TEXT UNIQUE"})

        assert _columns(conn, "t") == {"id"}

    def test_tolerate_errors_swallows_it_instead(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

        added = sqlite_util.add_columns(
            conn,
            "t",
            {"c": "TEXT UNIQUE", "d": "TEXT"},
            tolerate_errors=True,
        )

        assert added == ["d"], "one bad column does not stop the ones after it"
        assert _columns(conn, "t") == {"id", "d"}


class TestTheMissingTable:
    """`db.py` runs its migrations *before* `schema.sql`, so this is first boot."""

    def test_a_table_that_does_not_exist_yet_is_skipped_silently(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")

        assert sqlite_util.add_columns(conn, "nope", {"c": "TEXT"}) == []

    def test_and_tolerate_errors_makes_no_difference_to_that(self, tmp_path):
        """The skip is unconditional, which is the asymmetry worth stating.

        `db.py`'s bare `except` swallowed two conditions at once — column
        already present, table not yet created — so a helper guarding only the
        first would break first-boot ordering with nothing to catch it. The
        module databases, which do not tolerate anything else, get the second
        one anyway; `sqlite_util.add_columns`' docstring says why that is inert
        at each of them.
        """
        conn = sqlite3.connect(tmp_path / "t.db")

        assert (
            sqlite_util.add_columns(conn, "nope", {"c": "TEXT"}, tolerate_errors=True)
            == []
        )


class TestTheOrdinaryPath:
    def test_it_adds_what_is_missing_and_leaves_what_is_there(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")

        added = sqlite_util.add_columns(
            conn, "t", {"a": "TEXT", "b": "INTEGER NOT NULL DEFAULT 7"}
        )

        assert added == ["b"]
        assert conn.execute("PRAGMA table_info(t)").fetchall()[-1][4] == "7"

    def test_it_reads_column_names_positionally(self, tmp_path):
        """Both connection shapes reach this helper.

        `feeds/db.py` migrates under a `sqlite3.Row` connection and `health`,
        `location` and `db.init_db`'s own migration pass do not, so a helper
        indexing `PRAGMA table_info` by name raises `TypeError` on three of
        the four.
        """
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

        assert sqlite_util.add_columns(conn, "t", {"a": "TEXT"}) == ["a"]

        conn.row_factory = sqlite3.Row
        assert sqlite_util.add_columns(conn, "t", {"b": "TEXT"}) == ["b"]

    def test_commit_is_off_by_default(self, tmp_path):
        """`db.py`'s migration pass owns its own transaction boundaries."""
        path = tmp_path / "t.db"
        conn = sqlite3.connect(path, isolation_level="DEFERRED")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.execute("BEGIN")

        sqlite_util.add_columns(conn, "t", {"a": "TEXT"})

        other = sqlite3.connect(path, timeout=0.2)
        try:
            with pytest.raises(sqlite3.OperationalError):
                other.execute("INSERT INTO t (id) VALUES (1)")
                other.commit()
        finally:
            other.close()
            conn.rollback()
            conn.close()

    def test_commit_true_ends_the_transaction(self, tmp_path):
        """What `money`'s `_alter_once` did, and its callers still rely on."""
        path = tmp_path / "t.db"
        conn = sqlite3.connect(path, isolation_level="DEFERRED")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.execute("BEGIN")

        sqlite_util.add_columns(conn, "t", {"a": "TEXT"}, commit=True)

        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute("INSERT INTO t (id) VALUES (1)")
            other.commit()
            assert _columns(other, "t") == {"id", "a"}
        finally:
            other.close()
            conn.close()
