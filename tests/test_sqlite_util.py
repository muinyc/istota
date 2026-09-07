"""`sqlite_util`, and the pragma read-back matrix over every converted caller.

The read-back is the deliverable, and the trap it has to avoid is the one
round 1 kept falling into: a pragma test that reads back a value the connection
would have had anyway proves nothing. ``sqlite3.connect(timeout=T)`` calls
``sqlite3_busy_timeout(T * 1000)``, so *every* caller opened with ``timeout=30``
reads back ``busy_timeout = 30000`` whether or not the pragma ran —
``test_a_connect_timeout_alone_already_installs_the_busy_handler`` pins that
fact so the matrix below is read for what it is.

What discriminates, therefore, is the *matrix*, not any one row: ``foreign_keys``
is off by default and on in five callers, ``synchronous`` is FULL by default and
NORMAL in exactly one, ``row_factory`` is absent in two, ``busy_timeout`` is
5000 in one, and the commit/rollback pair differs three ways. A change to
``open_db``'s defaults moves some subset of those and cannot move all of them
in agreement.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from istota import sqlite_util

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "istota"


def _pragmas(conn: sqlite3.Connection) -> dict:
    return {
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        "row_factory": conn.row_factory is sqlite3.Row,
    }


class TestTheHandlerTimeoutAlreadyInstalls:
    """The fact the rest of this file has to be read against."""

    def test_a_connect_timeout_alone_already_installs_the_busy_handler(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db", timeout=30.0)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        finally:
            conn.close()

    def test_and_the_default_timeout_installs_five_seconds(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()


class TestOpenDb:
    def test_the_defaults(self, tmp_path):
        with sqlite_util.open_db(tmp_path / "t.db") as conn:
            assert _pragmas(conn) == {
                "busy_timeout": 30000,
                "foreign_keys": 1,
                "synchronous": 2,
                "row_factory": True,
            }

    def test_foreign_keys_off_is_the_sqlite_default(self, tmp_path):
        with sqlite_util.open_db(tmp_path / "t.db", foreign_keys=False) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    def test_row_factory_off_yields_tuples(self, tmp_path):
        with sqlite_util.open_db(tmp_path / "t.db", row_factory=False) as conn:
            conn.execute("CREATE TABLE t(a)")
            conn.execute("INSERT INTO t VALUES (1)")
            row = conn.execute("SELECT a FROM t").fetchone()
        assert isinstance(row, tuple) and not isinstance(row, sqlite3.Row)

    def test_a_busy_timeout_override_beats_the_connect_timeout(self, tmp_path):
        with sqlite_util.open_db(
            tmp_path / "t.db", timeout=30.0, busy_timeout_ms=2000,
        ) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000

    def test_busy_timeout_none_leaves_the_connect_timeout_standing(self, tmp_path):
        with sqlite_util.open_db(
            tmp_path / "t.db", timeout=7.0, busy_timeout_ms=None,
        ) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7000

    def test_synchronous_is_applied(self, tmp_path):
        with sqlite_util.open_db(tmp_path / "t.db", synchronous="NORMAL") as conn:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_commit_false_discards_on_exit(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db) as conn:
            conn.execute("CREATE TABLE t(a)")
            conn.commit()
        with sqlite_util.open_db(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with sqlite_util.open_db(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_commit_true_persists_on_exit(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with sqlite_util.open_db(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_rollback_on_error_leaves_nothing_behind(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        with pytest.raises(RuntimeError):
            with sqlite_util.open_db(db, commit=True, rollback_on_error=True) as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        with sqlite_util.open_db(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_the_connection_is_closed_on_the_way_out(self, tmp_path):
        with sqlite_util.open_db(tmp_path / "t.db") as conn:
            pass
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_the_connection_is_closed_after_a_raise(self, tmp_path):
        with pytest.raises(RuntimeError):
            with sqlite_util.open_db(tmp_path / "t.db") as conn:
                raise RuntimeError("boom")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_a_failing_pragma_does_not_leak_the_connection(self, tmp_path, monkeypatch):
        """`connect` closes what it opened when a pragma raises.

        Without the guard the connection is unreachable and stays open until the
        garbage collector gets to it, holding whatever lock it took.
        """
        closed: list[bool] = []

        class Boom(sqlite3.Connection):
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("no such pragma")

            def close(self):
                closed.append(True)
                super().close()

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            sqlite_util.sqlite3,
            "connect",
            lambda *a, **kw: real_connect(*a, factory=Boom, **kw),
        )
        with pytest.raises(sqlite3.OperationalError):
            sqlite_util.connect(tmp_path / "t.db")
        assert closed == [True]


class TestConnectReadOnly:
    def test_it_refuses_a_write(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        conn = sqlite_util.connect_read_only(db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO t VALUES (1)")
        finally:
            conn.close()

    def test_it_reads(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
            conn.execute("INSERT INTO t VALUES (42)")
        conn = sqlite_util.connect_read_only(db)
        try:
            assert conn.execute("SELECT a FROM t").fetchone()[0] == 42
        finally:
            conn.close()

    def test_it_does_materialize_the_wal_sidecars_and_leaves_them(self, tmp_path):
        """Measured, and it is the opposite of the reason the callers give.

        All five `doctor` call sites say they open ``mode=ro`` so that a
        ``sudo istota doctor`` against a stopped daemon does not materialize the
        ``-wal`` / ``-shm`` sidecars and leave root-owned files behind. Driven
        against sqlite 3.47, ``mode=ro`` creates both **and leaves them**,
        because a read-only connection cannot delete them on close — while the
        read-write open they were avoiding creates them and cleans them up. Only
        ``immutable=1`` avoids creating them, and that is unsafe against a live
        database because it tells SQLite the file never changes.

        Pinned as measured rather than corrected: this stage moved the open, and
        changing which URI these checks use is a decision about what a
        diagnostic may assume of a running daemon. A later fix turns this red on
        purpose.
        """
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(a)")
        for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            sidecar.unlink(missing_ok=True)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["t.db"]

        conn = sqlite_util.connect_read_only(db)
        try:
            conn.execute("SELECT COUNT(*) FROM t").fetchone()
        finally:
            conn.close()
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "t.db", "t.db-shm", "t.db-wal",
        ]

    def test_a_read_write_open_cleans_its_sidecars_up(self, tmp_path):
        """The control for the case above: same tree, the other open."""
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(a)")
        for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            sidecar.unlink(missing_ok=True)

        with sqlite_util.open_db(db) as conn:
            conn.execute("SELECT COUNT(*) FROM t").fetchone()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["t.db"]

    def test_a_missing_database_raises_rather_than_creating_one(self, tmp_path):
        db = tmp_path / "gone.db"
        with pytest.raises(sqlite3.OperationalError):
            sqlite_util.connect_read_only(db).execute("SELECT 1")
        assert not db.exists()


# ---------------------------------------------------------------------------
# The read-back matrix, one row per converted caller.
# ---------------------------------------------------------------------------

#: ``name -> (opener, expected pragmas)``. The openers each build the caller's
#: own helper against a scratch file. Read the *columns*, counted over all
#: fourteen converted callers (the twelve below plus the two bare ones in
#: ``TestTheTwoBareCallers``): ``foreign_keys`` splits them 5/9, ``synchronous``
#: 1/13, ``row_factory`` 12/2 and ``busy_timeout`` 12/2 — ``money.config_store``
#: and ``money.cli`` are the two that wait five seconds. Nothing about
#: ``open_db``'s defaults can move all four at once, which is what makes this a
#: matrix rather than a restatement.
_EXPECTED = {
    "db.get_db": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 1, "row_factory": True,
    },
    "feeds.db.connect": {
        "busy_timeout": 30000, "foreign_keys": 1, "synchronous": 2, "row_factory": True,
    },
    "briefings.db.connect": {
        "busy_timeout": 30000, "foreign_keys": 1, "synchronous": 2, "row_factory": True,
    },
    "health.db.connect": {
        "busy_timeout": 30000, "foreign_keys": 1, "synchronous": 2, "row_factory": True,
    },
    "location.db.connect": {
        "busy_timeout": 30000, "foreign_keys": 1, "synchronous": 2, "row_factory": True,
    },
    "location.db.with_geocode_conn": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": True,
    },
    "money.db.get_db": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": True,
    },
    # The one caller that waits five seconds rather than thirty, and the one the
    # spec's round-2 note singles out: raising it is a real change to what
    # succeeds under contention, so it is stated rather than inherited.
    "money.config_store._connect": {
        "busy_timeout": 5000, "foreign_keys": 1, "synchronous": 2, "row_factory": True,
    },
    "user_profiles._connect": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": True,
    },
    "user_briefings._connect": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": True,
    },
    "secrets_store._connect": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": False,
    },
    "web_tokens._connect": {
        "busy_timeout": 30000, "foreign_keys": 0, "synchronous": 2, "row_factory": True,
    },
}


def _openers():
    from istota import db as framework_db
    from istota import secrets_store, user_briefings, user_profiles, web_tokens
    from istota.briefings import db as briefings_db
    from istota.feeds import db as feeds_db
    from istota.health import db as health_db
    from istota.location import db as location_db
    from istota.money import config_store as money_config_store
    from istota.money import db as money_db

    return {
        "db.get_db": framework_db.get_db,
        "feeds.db.connect": feeds_db.connect,
        "briefings.db.connect": briefings_db.connect,
        "health.db.connect": health_db.connect,
        "location.db.connect": location_db.connect,
        "location.db.with_geocode_conn": location_db.with_geocode_conn,
        "money.db.get_db": money_db.get_db,
        "money.config_store._connect": money_config_store._connect,
        "user_profiles._connect": user_profiles._connect,
        "user_briefings._connect": user_briefings._connect,
        "secrets_store._connect": secrets_store._connect,
        "web_tokens._connect": web_tokens._connect,
    }


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_each_converted_caller_keeps_its_pragma_set(name, tmp_path):
    opener = _openers()[name]
    with opener(tmp_path / "scratch.db") as conn:
        assert _pragmas(conn) == _EXPECTED[name], name


def test_the_matrix_covers_every_context_manager_caller():
    """A thirteenth helper must be added here, not left unmeasured."""
    assert set(_openers()) == set(_EXPECTED)


class TestTheTwoBareCallers:
    """`money/cli` and `money/routes` hand a live connection on rather than
    wrapping a block, so they take `sqlite_util.connect` and close it
    themselves. Measured the same way."""

    def test_money_routes_portfolio_conn(self, tmp_path):
        from istota.money import routes

        ctx = type("Ctx", (), {"db_path": tmp_path / "money.db"})()
        conn = routes._portfolio_conn(ctx)
        try:
            assert _pragmas(conn) == {
                "busy_timeout": 30000,
                "foreign_keys": 0,
                "synchronous": 2,
                # Positional indexing: the portfolio queries never name a column.
                "row_factory": False,
            }
        finally:
            conn.close()

    def test_money_cli_get_db_conn(self, tmp_path):
        from istota.money import cli

        ctx = type("Ctx", (), {"db_path": tmp_path / "money.db"})()
        conn = cli._get_db_conn(ctx)
        try:
            assert _pragmas(conn) == {
                "busy_timeout": 5000,
                "foreign_keys": 0,
                "synchronous": 2,
                "row_factory": True,
            }
        finally:
            conn.close()


class TestTheFrameworkGeocodeConnectionKeepsAShortBudget:
    """`web_app`'s day summary opens *istota.db*, not a per-user location DB.

    The review caught this: the other three `web_app` location connections were
    meant to go from sqlite3's 5s default to 30s, and this one came along with
    them by sharing a helper. It is the framework database, the geocode callback
    can take a write lock on it (`reverse_geocode` caches what it looked up),
    and it runs on a web request thread — `db.get_db`'s own docstring argues for
    a short budget here, because 30s of waiting on istota.db holds the thread
    and, on the dispatch loop, trips the stall watchdog.

    Two assertions, because either alone is satisfiable by the wrong fix: the
    helper's default is still 30s for its skill and scheduler callers, and the
    web call site overrides it.
    """

    def test_the_helper_still_defaults_to_thirty_seconds(self, tmp_path):
        from istota.location import db as location_db

        with location_db.with_geocode_conn(tmp_path / "istota.db") as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000

    def test_the_web_day_summary_call_site_asks_for_five(self, tmp_path, monkeypatch):
        from istota import web_app
        from istota.location import db as location_db

        seen: list[float] = []
        real = location_db.with_geocode_conn

        def spy(path, *, timeout=30.0):
            seen.append(timeout)
            return real(path, timeout=timeout)

        monkeypatch.setattr(location_db, "with_geocode_conn", spy)
        monkeypatch.setattr(
            web_app, "_config", type("C", (), {"db_path": tmp_path / "istota.db"})(),
        )
        monkeypatch.setattr(
            web_app, "location_day_summary", lambda *a, **kw: {"ok": True},
        )
        assert web_app._location_query_day_summary(
            str(tmp_path / "location.db"), "UTC", None,
        ) == {"ok": True}
        assert seen == [5.0]


class TestCommitSemanticsPerCaller:
    """The half a pragma read-back cannot see: three different exit contracts."""

    def test_the_module_connects_do_not_commit(self, tmp_path):
        from istota.feeds import db as feeds_db

        db = tmp_path / "feeds.db"
        with feeds_db.connect(db) as conn:
            conn.execute("CREATE TABLE t(a)")
            conn.commit()
        with feeds_db.connect(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with feeds_db.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_the_framework_stores_commit(self, tmp_path):
        from istota import user_profiles

        db = tmp_path / "istota.db"
        with user_profiles._connect(db) as conn:
            conn.execute("CREATE TABLE t(a)")
        with user_profiles._connect(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with user_profiles._connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_money_rolls_back_and_re_raises(self, tmp_path):
        from istota.money import db as money_db

        db = tmp_path / "money.db"
        with money_db.get_db(db) as conn:
            conn.execute("CREATE TABLE t(a)")
        with pytest.raises(RuntimeError):
            with money_db.get_db(db) as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        with money_db.get_db(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


class TestNoJournalModeParameter:
    """`money/config_store.init` sets WAL once and says why. A `journal_mode`
    argument here would let a caller re-issue it per open, which takes a write
    lock that races sibling readers — the recorded cause of a dispatch stall —
    across twenty-odd `with _connect(...)` sites, and nothing else in the suite
    would go red."""

    def test_open_db_takes_no_journal_mode(self):
        import inspect

        params = set(inspect.signature(sqlite_util.open_db).parameters)
        assert "journal_mode" not in params
        assert "journal_mode" not in set(inspect.signature(sqlite_util.connect).parameters)

    def test_it_does_not_switch_a_delete_mode_database_to_wal(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        with sqlite_util.open_db(db) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


class TestBusyTimeoutActuallyWaits:
    """The behaviour a busy timeout buys, driven rather than read back.

    A 100ms budget loses to a writer holding the lock for 400ms; a 5s budget
    wins. Without this, `busy_timeout` is only ever a number in a pragma.
    """

    @staticmethod
    def _hold_then_write(db, budget_ms):
        holding = threading.Event()
        release = threading.Event()

        def holder():
            conn = sqlite3.connect(db, timeout=30.0, isolation_level=None)
            conn.execute("BEGIN EXCLUSIVE")
            holding.set()
            release.wait(5)
            conn.rollback()
            conn.close()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        holding.wait(5)
        outcome = "ok"
        try:
            with sqlite_util.open_db(
                db, timeout=30.0, busy_timeout_ms=budget_ms, commit=True,
            ) as conn:
                conn.execute("INSERT INTO t VALUES (1)")
        except sqlite3.OperationalError:
            outcome = "locked"
        finally:
            release.set()
            t.join(5)
        return outcome

    def test_a_short_budget_gives_up(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        assert self._hold_then_write(db, 50) == "locked"

    def test_a_long_budget_waits_the_writer_out(self, tmp_path):
        db = tmp_path / "t.db"
        with sqlite_util.open_db(db, commit=True) as conn:
            conn.execute("CREATE TABLE t(a)")
        assert self._hold_then_write(db, 5000) == "ok"


class TestNoSecondCopy:
    """Grep guard over the module DBs and framework stores that were converted.

    Scoped to the files this stage touched rather than to all of `src/`: the
    migration layer, `db_backup`, `db_health`, `db_relocate` and the skill
    subprocesses each open a connection for a reason `open_db` does not serve,
    and an all-of-`src` guard would have to carry an exemption list — which is
    the shape round 1 found blind by construction. An equality against the
    converted set is what can fail.
    """

    CONVERTED = [
        "db.py",
        "doctor.py",
        "web_app.py",
        "secrets_store.py",
        "user_briefings.py",
        "user_profiles.py",
        "web_tokens.py",
        "briefings/db.py",
        "feeds/db.py",
        "health/db.py",
        "location/db.py",
        "money/cli.py",
        "money/config_store.py",
        "money/db.py",
        "money/routes.py",
    ]

    #: What survives, per file, with the reason. Three are ``init_db`` bodies:
    #: those are the *only* place ``PRAGMA journal_mode=WAL`` is issued, and the
    #: init/connect split is what keeps it out of the per-open path. The fourth
    #: is `web_app`'s feeds dashboard read, which is a feeds connection and
    #: belongs on `feeds.db.connect` — a conversion outside this stage's line,
    #: named here rather than exempted quietly.
    SURVIVING = {
        "db.py": 1,           # init_db
        "health/db.py": 1,    # init_db
        "location/db.py": 1,  # init_db
        "web_app.py": 1,      # the feeds dashboard read
    }

    def test_the_hand_rolled_connects_are_exactly_the_four_named(self):
        counts = {
            rel: (SRC / rel).read_text(encoding="utf-8").count("sqlite3.connect(")
            for rel in self.CONVERTED
        }
        assert {k: v for k, v in counts.items() if v} == self.SURVIVING, (
            "a hand-rolled sqlite3.connect came back in a converted file; use "
            "sqlite_util.open_db / connect / connect_read_only"
        )

    def test_each_surviving_init_db_still_sets_wal(self):
        """The survivors are load-bearing, not leftovers.

        If one of them stops issuing ``journal_mode=WAL`` its store is born in
        DELETE mode and every reader loses concurrency, silently — so the guard
        above must not be satisfied by deleting them.
        """
        for rel in ("db.py", "health/db.py", "location/db.py"):
            body = (SRC / rel).read_text(encoding="utf-8")
            assert "journal_mode=WAL" in body or "journal_mode = WAL" in body, rel

    def test_the_read_only_uri_spelling_lives_in_one_place(self):
        hits = sorted(
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if "?mode=ro" in p.read_text(encoding="utf-8")
        )
        assert hits == ["sqlite_util.py"], (
            "a read-only URI open appeared outside sqlite_util; call "
            "sqlite_util.connect_read_only"
        )
