"""Reproduce the check-then-ALTER race deterministically, without threads.

The race `sqlite_util.add_columns` exists to survive is two connections both
reading a column as absent and both issuing the ``ALTER``; the loser gets
``duplicate column name``, which is a schema error the busy handler does not
help with.

**Calling a migration twice does not produce it**, and that is the trap this
module exists to remove. The second call re-reads ``PRAGMA table_info`` and
finds the column, so it issues no ``ALTER`` at all — an unguarded
check-then-ALTER passes that identically. So does the shape where a rival lands
the column *before* the migration is called: the migration's own read is inside
itself, and it sees the finished schema. What distinguishes guarded from
unguarded is a rival landing the column **between** this connection's read and
its write, which is what :class:`RacingConnection` does.

Used by `tests/test_add_columns.py`, `tests/money/test_db.py` and
`tests/money/test_portfolio_db.py`, so all three drive the same shape.
"""

from __future__ import annotations

import sqlite3


class RacingConnection:
    """A connection whose Nth ``ALTER`` is preceded by a rival winning it.

    Wraps a real connection and, on the ``ALTER`` selected by ``race_on``,
    first runs that same statement on a second real connection to the same file
    and commits it. The wrapped connection then issues its own ``ALTER`` into a
    schema that already carries the column.

    ``race_on`` is a 0-based index over the ``ALTER`` statements this connection
    is asked to execute. ``match`` selects by content instead, and is what a
    test driving a whole migration pass wants: index counting is order-
    dependent, and `db._run_migrations` issues two unconditional
    ``ALTER … DROP COLUMN``s partway through, so which index an ``ADD COLUMN``
    lands on depends on which table the test dropped a column from.

    ``raced`` records that the interleave actually happened — assert on it, or
    the test proves nothing. It is one-shot: a migration wanting two rivals in
    one pass needs a counter here rather than a flag.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        rival: sqlite3.Connection,
        *,
        race_on: int = 0,
        match: str | None = None,
    ) -> None:
        self._conn = conn
        self._rival = rival
        self._race_on = race_on
        self._match = match
        self._alters = 0
        self.raced = False

    def _should_race(self, sql: str) -> bool:
        if self.raced:
            return False
        if self._match is not None:
            return self._match in sql
        return self._alters == self._race_on

    def execute(self, sql, *args):
        if isinstance(sql, str) and sql.lstrip().upper().startswith("ALTER"):
            if self._should_race(sql):
                self._rival.execute(sql)
                self._rival.commit()
                self.raced = True
            self._alters += 1
        return self._conn.execute(sql, *args)

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    # Declared rather than left to `__getattr__`: Python looks an implicit
    # dunder up on the type, so `with conn:` inside a migration would raise
    # `TypeError` instead of delegating, silently and only for the next caller.
    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        # Everything else — commit, rollback, executescript, cursor — goes
        # straight through, so a whole migration pass can be run against this
        # rather than only a single helper.
        return getattr(self._conn, name)
