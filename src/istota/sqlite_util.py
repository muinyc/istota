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

__all__ = ["connect", "connect_read_only", "open_db"]


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

    The connection is always closed. A rollback on the way out of an abandoned
    generator is redundant with ``close()``'s implicit one and is harmless.
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
    except BaseException:
        if rollback_on_error:
            conn.rollback()
        raise
    finally:
        conn.close()
