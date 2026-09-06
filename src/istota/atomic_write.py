"""Publish a file's new contents without a reader ever seeing them half-written.

Nine call sites wrote this by hand and disagreed on the one detail that decides
whether the helper is safe: **the staging name has to be unique per call, not
per process.** ``skills/developer/__init__.py`` paid for that lesson and its
docstring is the record of it — the worker pool is threads in one process
(``scheduler.UserWorker``), so two concurrent tasks for one user produced the
identical staging path in the identical directory, and the interleaved
write / write / chmod / replace / chmod ends in ``FileNotFoundError`` for the
second. ``money/core/edit.py`` and ``money/work.py`` still used ``pid`` alone
and ``health/documents.py`` used a fixed ``.{name}.part`` shared by every
writer; all three are threads-in-one-process shapes (``money/routes.py``
reaches the ledger through ``asyncio.to_thread``), so the only thing standing
between them and a torn publish was the flock above them.

Three properties are decisions rather than consequences:

**The mode is always applied, and with ``os.fchmod`` on the open descriptor
rather than ``chmod`` on the name.** Several of these directories are model-writable or
user-writable over the Nextcloud mount, so a symlink swapped in between the
close and a path-based ``chmod`` would take the new mode to whatever it names.
The descriptor cannot be redirected. ``os.replace`` needs no such care —
rename does not follow the final component. Applying the mode *before* the
content is written also means there is no window where the file exists at its
final name carrying ``mkstemp``'s ``0600``. Note that *always* is a change
for the three callers that previously reached ``Path.write_text`` /
``write_bytes`` and so took ``0o666 & ~umask``: under the default ``umask 022``
every shipped shape uses, ``0o644`` is the identical answer, but under a
hardened umask these files were tighter and now are not. The mode is a
parameter for exactly that reason — a caller wanting ``0600`` says so, as
``brain_availability`` and ``subscription_usage`` do.

**Cleanup unlinks on ``BaseException``, not ``Exception``.** A
``KeyboardInterrupt`` or a ``SystemExit`` between the write and the replace
must not leave a dot-file behind in a directory the user reads over Nextcloud.

**``fsync=True`` fsyncs the file descriptor and not the directory**, so it
buys durability of the *contents* against a crash, not durability of the
rename. Nothing here promises the latter, and the two callers that ask for it
(``brain_availability``, ``subscription_usage``) want the former.

Nothing is swallowed: every caller that must not raise already carries its own
handler and its own fallback value, and a helper that reported success on a
failed write would make each of those worse rather than better. A missing
parent directory raises, and a read-only parent raises at ``mkstemp`` —
before anything at the destination is touched, which is the property
``os.replace``-based publishing has and truncate-in-place does not.

stdlib-only leaf, imports nothing from the package, so a skill subprocess can
reach it without pulling in ``istota``.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


@contextmanager
def atomic_writer(
    path: Path | str, *, mode: int = 0o644, fsync: bool = False
) -> Iterator[IO[bytes]]:
    """Yield a binary handle on a staging file that becomes ``path`` on exit.

    The staging file is created by ``tempfile.mkstemp`` in ``path``'s own
    directory — same filesystem, so the publishing ``os.replace`` is a rename
    rather than a copy — under a dot-prefixed name derived from the target, so
    a sweep that skips dot files (``skills/developer``'s ``_remove_shims``)
    cannot delete another writer's in-flight staging file.

    **Do not close the yielded handle.** The flush, the optional fsync and the
    rename all happen after the body returns, and a handle the caller closed
    turns the publish into a ``ValueError`` — which at least one caller
    (``storage.write_regular_file``) reports as an ordinary write failure.

    A staging file only outlives the call when the process dies between the
    write and the rename; nothing sweeps one afterwards. The old pid-derived
    names were reclaimed by whichever later run happened to draw the same pid,
    and these are not, which is the price of making the name unique per call.
    They are inert — every directory scan in the tree either enumerates whole
    entries or globs a suffix these names cannot match (checked against
    ``money/work._load_all``, ``brain_availability.clear_all``,
    ``skills/developer._remove_shims`` and the health document sweeps).
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "wb")
    except BaseException:
        # The descriptor is not owned by a file object yet, so nothing else
        # will close it.
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    try:
        with handle:
            os.fchmod(handle.fileno(), mode)
            yield handle
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_bytes_atomic(
    path: Path | str, data: bytes, *, mode: int = 0o644, fsync: bool = False
) -> None:
    """Replace ``path`` with ``data``."""
    with atomic_writer(path, mode=mode, fsync=fsync) as handle:
        handle.write(data)


def write_text_atomic(
    path: Path | str,
    text: str,
    *,
    mode: int = 0o644,
    fsync: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Replace ``path`` with ``text``.

    The encoding is explicit and defaults to UTF-8 for the reason the readers
    pin it: the revision tag the web save compares against a memory document
    is a UTF-8 hash, and a writer taking the locale's default would publish a
    file the reader cannot verify. Newlines are written through unchanged —
    there is no text-mode translation layer between here and the descriptor.
    """
    write_bytes_atomic(path, text.encode(encoding), mode=mode, fsync=fsync)
