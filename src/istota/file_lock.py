"""Poll for an exclusive ``fcntl.flock``, giving up after a deadline.

Three modules carried the same twenty-line acquisition loop and differed only
in the anchor path, the poll interval and the exception raised on timeout:
``memory/curation/file_lock.py`` (``MemoryMdLocked``, 0.1s),
``money/work.py`` (``WorkStoreLocked``, 0.05s) and ``money/core/edit.py``
(``LedgerLocked``, 0.05s).

``on_timeout`` takes the anchor path as a string and returns the exception to
raise, so each caller keeps its own exception type and every ``except`` clause
in the tree goes on working unchanged. That is deliberate rather than
incidental: the type is part of each of those packages' published surface, and
collapsing all three onto ``TimeoutError`` would be a behaviour change wearing
a refactor's clothes.

**Non-blocking plus a poll rather than a blocking ``LOCK_EX``.** A blocking
acquisition has no deadline, so a writer that dies holding the lock — or an
anchor on a filesystem where the lock is a silent no-op — wedges the caller
indefinitely. The callers here are a web request, a scheduler tick and a
model-invoked CLI; each of them would rather fail in a few seconds and say so.
An ``errno`` other than ``EAGAIN``/``EWOULDBLOCK`` is a real fault and
propagates rather than being retried until the deadline.

The anchor is opened ``a+``, which gives the same descriptor shape whether or
not it already existed; nothing is ever read from it. It carries no lock state
once the descriptor closes — the OS releases the flock on context exit and
unconditionally on process death — so the anchor file is left in place rather
than unlinked, which is what keeps two processes agreeing on one inode.

Linux and macOS. Windows is not a supported deployment for istota and this
does not paper over that.

stdlib-only leaf, imports nothing from the package.
"""

from __future__ import annotations

import errno
import fcntl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive_lock(
    lock_path: Path | str,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
    on_timeout: Callable[[str], BaseException] | None = None,
) -> Iterator[None]:
    """Hold an exclusive flock on ``lock_path`` for the duration of the context.

    Raises whatever ``on_timeout`` returns for the anchor path, or
    ``TimeoutError`` when it is None. The caller owns the anchor's parent
    directory; nothing here creates it.
    """
    lock_path = Path(lock_path)
    fd = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    if on_timeout is None:
                        raise TimeoutError(str(lock_path)) from None
                    raise on_timeout(str(lock_path)) from None
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fd.close()
