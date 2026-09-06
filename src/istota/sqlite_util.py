"""One SQLite open, with every caller's pragma set expressed as parameters.

Fourteen helpers in this tree opened a connection and then issued some subset
of the same four pragmas, and the five-line explanation of *why* ``journal_mode``
is not among them was pasted into four of them verbatim. This is that open,
once, with the pragmas as arguments so each caller's set stays visible at the
call site rather than being buried in a body a reader has to go and compare.

**There is no ``journal_mode`` parameter, and adding one would be a defect.**
WAL is persistent in the SQLite file header, so it is issued once by each
store's ``init_db`` and never re-issued per connection: re-issuing takes a
write lock that races sibling readers, which is the recorded cause of a
dispatch-loop stall. ``money/config_store``'s ``init`` says so in its own
comment and has twenty-odd ``with _connect(...)`` sites behind it. A
``journal_mode`` argument here — or a default that set WAL — would reintroduce
that across all of them, and nothing in the suite would go red.

**``timeout`` already is a busy timeout.** Python's ``sqlite3.connect(timeout=T)``
calls ``sqlite3_busy_timeout(T * 1000)``, so a connection opened with
``timeout=30.0`` reads back ``PRAGMA busy_timeout = 30000`` having issued no
pragma at all. ``busy_timeout_ms`` is therefore an *override* of that value,
not the thing that supplies it — which is why passing ``None`` is not the same
as "no busy timeout", and why a test asserting ``busy_timeout == 30000`` on a
``timeout=30.0`` connection proves nothing about whether the pragma ran.

Stdlib-only leaf: ``sqlite3``, ``pathlib`` and ``contextlib``. Imports nothing
from the package, so a module DB helper or a skill subprocess can reach it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["add_columns", "connect", "connect_read_only", "open_db"]


def connect(
    path: Path | str,
    *,
    timeout: float = 30.0,
    row_factory: bool = True,
    busy_timeout_ms: int | None = 30_000,
    foreign_keys: bool = True,
    synchronous: str | None = None,
) -> sqlite3.Connection:
    """Open a connection and apply the requested pragmas. Caller closes it.

    The bare form, for the two callers that hand a live connection back to
    something else rather than wrapping a block: ``money/cli._get_db_conn`` and
    ``money/routes._portfolio_conn``. Everything else wants :func:`open_db`.

    ``busy_timeout_ms=None`` issues no ``PRAGMA busy_timeout``, which leaves the
    handler ``timeout`` already installed — see the module docstring.
    ``synchronous`` is a per-connection setting (unlike ``journal_mode``) and is
    passed as the literal SQLite keyword, e.g. ``"NORMAL"``.
    """
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        if busy_timeout_ms is not None:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        if synchronous is not None:
            conn.execute(f"PRAGMA synchronous = {synchronous}")
        if row_factory:
            conn.row_factory = sqlite3.Row
    except BaseException:
        # A pragma that raised leaves a connection nobody holds a name for.
        conn.close()
        raise
    return conn


def connect_read_only(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` read-only through the URI form. Caller closes it.

    ``doctor`` is the only caller, five times, and what it gets out of this is
    that the connection cannot write to the database. No pragmas: a read-only
    connection has nothing to protect from a writer, and all five call sites
    issued none.

    **What it does not get is the thing all five said it was for.** Their
    comments claim ``mode=ro`` avoids materializing the ``-wal`` / ``-shm``
    sidecars, so that ``sudo istota doctor`` against a stopped daemon leaves no
    root-owned files behind. Driven against sqlite 3.47 it is the other way
    round: a read-only open creates both **and leaves them**, because a
    read-only connection cannot delete them on close, while the read-write open
    they were avoiding creates them and cleans them up. Only ``immutable=1``
    creates neither, and that is unsafe against a live database because it
    tells SQLite the file never changes. Recorded rather than corrected —
    changing which URI a diagnostic uses is a decision about what it may assume
    of a running daemon, and this function is a move. Pinned by
    ``tests/test_sqlite_util.py::TestConnectReadOnly``.

    The path is interpolated into the URI unencoded, which is what all five
    call sites did and is preserved rather than fixed here: a ``db_path``
    containing ``?`` or ``#`` would be misparsed. Every shipped ``db_path``
    comes from a config file an operator wrote, so this is a latent sharp edge
    rather than a live one, and correcting it is a behaviour change with its
    own argument to make.
    """
    return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)


@contextmanager
def open_db(
    path: Path | str,
    *,
    timeout: float = 30.0,
    row_factory: bool = True,
    busy_timeout_ms: int | None = 30_000,
    foreign_keys: bool = True,
    synchronous: str | None = None,
    commit: bool = False,
    rollback_on_error: bool = False,
) -> Iterator[sqlite3.Connection]:
    """:func:`connect`, plus the close/commit/rollback block around it.

    ``commit`` commits on a clean exit; ``rollback_on_error`` rolls back before
    re-raising. The two are independent because the callers are: the framework
    stores commit and do not roll back, the module ``connect`` helpers do
    neither, and only the money pair does both.

    ``rollback_on_error`` catches ``Exception``, not ``BaseException``, which is
    exactly what both money callers had: on a ``KeyboardInterrupt`` or a
    ``GeneratorExit`` the ``finally`` below closes the connection and SQLite
    rolls back implicitly, so an explicit ``rollback()`` there buys nothing and
    can raise ``ProgrammingError`` over the in-flight exception if the body
    closed the connection itself. The ``finally`` is the real guarantee.
    """
    conn = connect(
        path,
        timeout=timeout,
        row_factory=row_factory,
        busy_timeout_ms=busy_timeout_ms,
        foreign_keys=foreign_keys,
        synchronous=synchronous,
    )
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if rollback_on_error:
            conn.rollback()
        raise
    finally:
        conn.close()


def add_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    *,
    commit: bool = False,
    tolerate_errors: bool = False,
) -> list[str]:
    """Add each missing column to ``table``, tolerating a rival that wins one.

    ``columns`` maps a column name to the rest of its ``ADD COLUMN`` clause —
    ``{"brain": "TEXT", "once": "INTEGER DEFAULT 0"}``. Returns the names this
    call actually added, in the order it added them, so a caller with follow-up
    work gated on a column being new (an index to rebuild, a one-shot backfill)
    can ask rather than re-reading the schema.

    **The race is the reason this exists.** ``ensure_schema`` and ``init_db``
    run on every money web request, scheduler cron and skill invocation, so the
    first post-upgrade moment is routinely several connections at once.
    Check-then-ALTER lets both see the column absent and the loser raise
    ``duplicate column name`` — a *schema* error, so the 30s busy handler does
    not help — surfacing as a one-off 500 or a failed task. So the ``ALTER`` is
    re-checked on the way out: the column being present now is the race and
    nothing else, and the loser returns having done its job.

    **A table that does not exist yet is skipped, not an error**, and that is a
    second condition rather than a nicety. ``db._run_migrations`` runs *before*
    ``schema.sql``, so on a first boot every table it names is absent; its bare
    ``except sqlite3.OperationalError: pass`` was carrying that case and the
    duplicate-column one with one handler, and a helper that guarded only the
    column would break first-boot ordering with nothing in the suite to catch
    it. ``PRAGMA table_info`` on a missing table yields no rows, which would
    otherwise read as "column absent" and point the ``ALTER`` at nothing.

    The skip is **unconditional** — it does not consult ``tolerate_errors`` —
    which is a widening for the module-database callers, whose check-then-ALTER
    would have raised ``no such table``. It is inert at all of them and it is
    worth knowing why rather than assuming: ``health``, ``location`` and
    ``money`` run their migrations *after* ``executescript``, so the table is
    always there, and ``feeds._read_schema_version`` returns the current
    version when ``feed_entries`` is absent, so its chain does not run at all.
    A caller that could genuinely meet a missing table wants an argument here,
    not this default.

    ``tolerate_errors`` swallows an ``OperationalError`` that is neither of
    those — a lock, in practice. It exists because that is what
    ``db._run_migrations``' bare handler did at every one of its sites, and
    this stage is a consolidation rather than a change of failure mode; the
    argument for why degrading there is safe is per-table and is written at the
    ``user_profiles`` block, not re-made here. **Do not pass it where a missing
    column raises at read time** — ``outbound_drafts.reply_to`` is the site
    that says so, and it keeps the default, because there a swallowed lock
    leaves every draft read raising ``IndexError`` and stops all outbound mail
    on the instance.

    ``commit`` commits after each column it adds, which is what ``money``'s
    ``_alter_once`` did for its two callers. It is off by default because
    ``db._run_migrations`` owns its own transaction boundaries and states the
    contract for a migration that wants one.

    ``table`` and the column names and clauses are interpolated into DDL, which
    SQLite gives no way to parameterize. Every caller passes a code literal;
    this is the same latent sharp edge :func:`connect_read_only` records for its
    URI, not a live one, and narrowing it is a change with its own argument to
    make. ``PRAGMA table_info`` is read positionally because both connection
    shapes reach here: ``feeds`` migrates under a ``sqlite3.Row`` connection
    and ``health``, ``location`` and ``db.init_db``'s own pass do not.
    """
    added: list[str] = []
    try:
        present = _column_names(conn, table)
    except sqlite3.OperationalError:
        if tolerate_errors:
            return added
        raise
    if not present:
        return added  # Table not created yet — see the docstring.

    for name, clause in columns.items():
        if name in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {clause}")
        except sqlite3.OperationalError:
            try:
                lost_the_race = name in _column_names(conn, table)
            except sqlite3.OperationalError:
                lost_the_race = False
            if not lost_the_race and not tolerate_errors:
                raise
            continue
        added.append(name)
        if commit:
            conn.commit()
    return added


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    # (cid, name, type, notnull, dflt_value, pk) — indexed by position, not by
    # name, because half the callers migrate under a row factory and half do not.
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
