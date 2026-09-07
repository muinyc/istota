"""Every column migration goes through one guard, and the guard survives a race.

`sqlite_util.add_columns` is the mechanism; `tests/test_add_columns.py` covers
it in isolation. This file covers the two things that only hold at the call
sites: that a real first-upgrade `ALTER` inside `db._run_migrations` survives a
rival landing the column in the window, and that no second implementation of
check-then-ALTER has grown back anywhere in `src/`.

The pre-upgrade state is synthesized by dropping a column from a freshly
migrated database rather than by shipping an old `schema.sql`, so the fixture
cannot drift: the column dropped is the *newest* one on each table, which is
exactly the column a rewritten migration list is most likely to lose. The
before/after schema comparison this stage was verified against — `init_db`
run fresh and over a v0.35.0 database, dumped and diffed — is a one-off
migration check rather than a standing property and lives in the commit
message, not here.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from istota import db as istota_db

from .support.sqlite_race import RacingConnection

SRC = Path(__file__).resolve().parent.parent / "src" / "istota"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# (table, column) pairs: the newest column each migrated table gained, so a
# rewrite that drops it fails here rather than at runtime with "no such column".
NEWEST_COLUMNS = [
    ("tasks", "model_namespace"),
    ("user_profiles", "external_turn_display"),
    ("scheduled_jobs", "publish_shared_kv_trusted"),
    ("messages", "reply_to_message_id"),
    ("rooms", "model_namespace"),
    ("web_chat_rooms", "color"),
]


@pytest.mark.parametrize("table,column", NEWEST_COLUMNS)
def test_a_dropped_column_is_re_added_by_the_migrations(tmp_path, table, column):
    """The migration list still names the newest column on each table.

    `70c9d62f` added `tasks.model_namespace` after this spec's baseline was
    written, so a rewrite planned against that baseline drops it silently and
    surfaces as a missing column at runtime rather than as a conflict.
    """
    path = tmp_path / "istota.db"
    istota_db.init_db(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.commit()
        assert column not in _columns(conn, table)

        istota_db._run_migrations(conn)

        assert column in _columns(conn, table)
    finally:
        conn.close()


# The subset of the above that a rival can actually be made to win, which is
# every table whose ALTERs run before `_backfill_briefing_output` issues the
# first UPDATE. Past that point this connection holds the write transaction
# `_run_migrations`' own docstring is about, so a rival's ALTER blocks on the
# lock rather than landing in the window — the harness cannot produce the race
# for `messages`, `rooms` or `web_chat_rooms`, and a test claiming to would be
# asserting against a lock timeout.
RACEABLE_COLUMNS = NEWEST_COLUMNS[:3]


@pytest.mark.parametrize("table,column", RACEABLE_COLUMNS)
def test_a_concurrent_loser_of_that_alter_does_not_raise(tmp_path, table, column):
    """Two connections at one first-upgrade ALTER; both have to finish.

    `init_db` is run by the daemon, by the auto-update script and by every CLI
    invocation, so the first moment after an upgrade is routinely more than one
    of them. The loser's `duplicate column name` is a schema error, so the 30s
    busy handler does not help — this is the case the guard exists for.

    `raced` is asserted because the harness is what makes the test non-vacuous:
    a rival that lands the column *before* `_run_migrations` is called is seen
    by the migration's own `PRAGMA table_info` and produces no race at all.
    """
    path = tmp_path / "istota.db"
    istota_db.init_db(path)

    loser = sqlite3.connect(path, timeout=5.0)
    loser.row_factory = sqlite3.Row
    rival = sqlite3.connect(path, timeout=5.0)
    try:
        loser.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        loser.commit()

        racing = RacingConnection(loser, rival, match=f"ADD COLUMN {column}")
        istota_db._run_migrations(racing)

        assert racing.raced, "the harness did not interleave; the test is vacuous"
        assert column in _columns(loser, table)
    finally:
        loser.close()
        rival.close()


class TestTheModuleDatabasesWhereTheGuardIsTheWholeStory:
    """The sites where losing the race used to raise, rather than be swallowed.

    `db._run_migrations` passes `tolerate_errors=True`, which is what its bare
    `except sqlite3.OperationalError: pass` did — so the two tests above hold
    there with the race re-check present *or* absent, and what they pin is the
    product property rather than the guard. The four module databases are the
    other half: their migrations were a check-then-ALTER with no handler at
    all, so a loser raised and took `init_db` with it. These are the cases the
    re-check is load-bearing for, and removing it turns them red.
    """

    def test_feeds_survives_a_lost_race_on_its_first_upgrade(self, tmp_path):
        from istota.feeds import db as feeds_db

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)

        loser = sqlite3.connect(path, timeout=5.0)
        loser.row_factory = sqlite3.Row
        rival = sqlite3.connect(path, timeout=5.0)
        try:
            loser.execute("ALTER TABLE feed_entries DROP COLUMN embed_url")
            loser.commit()

            racing = RacingConnection(loser, rival, match="ADD COLUMN embed_url")
            feeds_db._migrate_v3_to_v4(racing)

            assert racing.raced, "the harness did not interleave; the test is vacuous"
            assert "embed_url" in _columns(loser, "feed_entries")
        finally:
            loser.close()
            rival.close()

    def test_health_survives_a_lost_race_on_its_first_upgrade(self, tmp_path):
        from istota.health import db as health_db

        path = tmp_path / "health.db"
        health_db.init_db(path)

        loser = sqlite3.connect(path, timeout=5.0)
        rival = sqlite3.connect(path, timeout=5.0)
        try:
            loser.execute("DROP INDEX IF EXISTS idx_panels_content_hash")
            loser.execute("ALTER TABLE panels DROP COLUMN content_hash")
            loser.commit()

            racing = RacingConnection(loser, rival, match="ADD COLUMN content_hash")
            health_db._migrate_add_content_hash(racing)

            assert racing.raced, "the harness did not interleave; the test is vacuous"
            assert "content_hash" in _columns(loser, "panels")
        finally:
            loser.close()
            rival.close()


class TestTheOneSiteThatDoesNotTolerate:
    """`outbound_drafts.reply_to` must keep calling the strict form.

    Every other framework migration goes through `db._add_columns`, which
    passes `tolerate_errors=True`. This one deliberately does not, and the
    comment above it argues at length that a swallowed lock there stops all
    outbound mail on the instance. Changing it to `_add_columns` is a
    one-token edit that turns nothing else red, so it is pinned at the source:
    a behavioural test would have to force a lock at exactly that statement,
    which is a race this suite cannot stage deterministically.
    """

    def _callee(self, name: str) -> str | None:
        """Which function the call passing `{"reply_to": ...}` belongs to."""
        tree = ast.parse((SRC / "db.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if len(node.args) < 3 or not isinstance(node.args[2], ast.Dict):
                continue
            keys = [
                k.value
                for k in node.args[2].keys
                if isinstance(k, ast.Constant)
            ]
            if keys != [name]:
                continue
            return ast.unparse(node.func)
        return None

    def test_reply_to_calls_the_helper_directly(self):
        assert self._callee("reply_to") == "sqlite_util.add_columns", (
            "outbound_drafts.reply_to must not take db._add_columns' tolerance"
        )

    def test_and_a_tolerating_site_looks_different(self):
        """The control: the assertion above distinguishes the two spellings."""
        assert self._callee("extras") == "_add_columns"


class TestNoSecondImplementation:
    """A grep guard, following `tests/test_lint_scope.py`'s shape.

    Twenty-six blocks in `db._run_migrations` and twelve check-then-ALTER sites
    across the four module databases were each their own copy of this. The
    point of the consolidation is that a twenty-seventh cannot be added without
    somebody noticing.
    """

    # Path *relative to `src/istota`* -> why it may still spell one itself.
    #
    # Relative, not the basename: five files under this tree are called
    # `db.py`, four of them the module databases this stage converted, so a
    # basename key exempts exactly the population the guard exists to watch —
    # and `test_the_exemptions_are_still_real` cannot see it either, because
    # the top-level `db.py` supplies a hit for the whole name.
    EXEMPT = {
        "sqlite_util.py": "the helper; this is where the statement is built",
        "db.py": (
            "the `monarch_synced_transactions` loop, which is ISSUE-427's "
            "vestigial pair and is left standing untouched"
        ),
    }

    ADD_COLUMN_RE = re.compile(r"ADD\s+COLUMN", re.IGNORECASE)

    def _hits(self, source: str) -> list[int]:
        """Line numbers of string literals spelling ADD COLUMN, docstrings apart.

        Read out of the parsed source rather than off raw lines, because prose
        naming the statement is not an instance of it — `money/portfolio.py`'s
        `_migrate_owner_to_group` docstring says why a `RENAME COLUMN` cannot
        fold into the helper, and a line-based guard reports that as an
        offender and pushes the next person into exempting the whole file.
        """
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                first = node.body[0] if node.body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstrings.add(id(first.value))

        # An f-string's pieces are Constants of their own; report the joined
        # form once rather than each fragment.
        in_fstring = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for part in ast.walk(node):
                    if isinstance(part, ast.Constant):
                        in_fstring.add(id(part))

        found: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and id(node) not in in_fstring
            ):
                text = node.value
            else:
                continue
            if self.ADD_COLUMN_RE.search(text):
                found.append(node.lineno)
        return sorted(set(found))

    def _offenders(self) -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        for path in sorted(SRC.rglob("*.py")):
            rel = str(path.relative_to(SRC))
            if rel in self.EXEMPT:
                continue
            hits = self._hits(path.read_text())
            if hits:
                found[rel] = hits
        return found

    def test_no_module_builds_its_own_add_column(self):
        offenders = self._offenders()
        assert offenders == {}, (
            "these build an ALTER ... ADD COLUMN by hand instead of calling "
            f"sqlite_util.add_columns: {offenders}"
        )

    def test_the_exemptions_are_still_real(self):
        """An exemption for a file that no longer spells one is a stale rule."""
        for rel in self.EXEMPT:
            path = SRC / rel
            assert path.exists(), f"{rel} is exempt but no longer exists"
            assert self._hits(path.read_text()), (
                f"{rel} is exempt but no longer contains an ADD COLUMN"
            )

    def test_the_guard_can_see_a_reintroduced_copy(self, tmp_path, monkeypatch):
        """The control for the two tests above, in the suite rather than beside it.

        A grep guard that walks the wrong tree, or whose expression stopped
        matching, reports a clean result forever. This one points it at a tree
        holding exactly the copy it is meant to find.
        """
        fake = tmp_path / "istota"
        (fake / "sub").mkdir(parents=True)
        (fake / "sub" / "regrown.py").write_text(
            'conn.execute("ALTER TABLE t ADD COLUMN c TEXT")\n'
        )
        # Named `db.py`, under a subpackage, because that is the shape the
        # exemption used to swallow: four of the twelve converted sites are in
        # a file with that basename, and a control planting `regrown.py` never
        # touches the exemption path at all.
        (fake / "sub" / "db.py").write_text(
            'conn.execute("ALTER TABLE t ADD COLUMN d TEXT")\n'
        )
        monkeypatch.setattr(
            "tests.test_guarded_column_migrations.SRC", fake, raising=False
        )

        assert self._offenders() == {
            "sub/db.py": [1],
            "sub/regrown.py": [1],
        }
