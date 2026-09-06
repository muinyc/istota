"""Du-style tree measurement and the first-level directory scan beneath it.

Four callers walked a tree with ``os.walk(followlinks=False)``, ``os.lstat``,
``st_blocks * 512`` and a swallowed ``OSError``, and five listed a root's
immediate subdirectories with the same sorted, symlink-skipping,
non-directory-skipping scan. Both are here once.

**Blocks, not ``st_size``.** A volume is filled by blocks, so a sparse file
costs what it occupies and not what it claims, and a directory of hardlinks
costs one copy — which is why ``dedupe_inodes`` exists at all and why the
sandbox cache sweeper is the only caller that passes it. ``sandbox_cache_sweeper``
and ``session/session_log`` each argued this for themselves; the argument is
now here and their comments point at it.

**Directory inodes are counted only on request.** The sweep's subject is a
package cache whose directory count is a real part of what it occupies; the
session-log sweep's subject is a ceiling nothing can reclaim a directory
against, so counting them would leave a many-user deployment permanently over
a ceiling nothing could clear. ``include_dirs`` is the switch and defaults off,
which is what three of the four callers do.

**Nothing raises.** Every caller is inside an explicit never-raises contract —
``sandbox_cache_sweeper``, ``session_log``'s sweep, ``session_log_read`` and
``doctor``'s checks. An unreadable root is zero bytes and no directories, which
is honest: nothing was established either way. ``on_error`` is how a caller
that counts its unreadable entries gets to see them; it follows ``os.walk``'s
convention, taking the ``OSError`` with its ``filename`` attribute set.

``ValueError`` is caught beside ``OSError`` at every point that touches the
filesystem, because a path holding an embedded null byte raises that out of
``open``, ``stat`` and ``scandir`` rather than an ``OSError`` —
``session_log_read`` found that one. ``os.walk`` needs its own guard rather
than relying on ``onerror``: CPython wraps only ``OSError`` around its own
``scandir(top)``, so a ``ValueError`` on the *root* comes straight out of the
generator, past a handler that never sees it. That is the one way this module
could have raised into a never-raises caller.

``on_error`` is deliberately **not** called for the ``ValueError`` arms: it is
typed for ``OSError`` and a caller counting unreadable entries is counting a
filesystem condition, where an embedded NUL is a bad argument rather than a
directory that could not be read. A filesystem name cannot contain one, so this
costs nothing today and is stated so a caller does not read the count as
covering it.

Stdlib-only leaf: ``os``, ``pathlib``. Imports nothing from the package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator

__all__ = ["BLOCK_SIZE", "entry_bytes", "first_level_dirs", "iter_tree", "tree_bytes"]

#: Bytes per block in the unit ``st_blocks`` is defined in. POSIX fixes it at
#: 512 regardless of the filesystem's own block size, and Linux, macOS and the
#: BSDs all report in 512-byte units whatever ``st_blksize`` says.
BLOCK_SIZE = 512

ErrorHandler = Callable[[OSError], None]


def entry_bytes(info: os.stat_result) -> int:
    """What one stat result occupies on disk, du-style."""
    return info.st_blocks * BLOCK_SIZE


def iter_tree(
    root: Path | str,
    *,
    include_dirs: bool = False,
    on_error: ErrorHandler | None = None,
) -> Iterator[tuple[str, os.stat_result]]:
    """Every entry below *root*, as ``(path, lstat result)``.

    Symlinks are never followed and are yielded as themselves — ``os.lstat``,
    so a link costs its own inode rather than its target's. An entry that
    cannot be stat'ed is skipped and reported to *on_error*.

    Yields paths as ``str`` because every caller either joins them again or
    compares a suffix; a caller wanting a ``Path`` wraps the one entry it keeps.
    """

    def _walk_error(exc: OSError) -> None:
        if on_error is not None:
            on_error(exc)

    # The `try` wraps the iteration, not just the call: `os.walk` is a
    # generator, so its first `scandir(top)` runs on the first `next()`, and
    # CPython routes only `OSError` from it to `onerror`.
    try:
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=_walk_error, followlinks=False,
        ):
            names = (*dirnames, *filenames) if include_dirs else filenames
            for name in names:
                full = os.path.join(dirpath, name)
                try:
                    info = os.lstat(full)
                except (OSError, ValueError) as exc:
                    if on_error is not None and isinstance(exc, OSError):
                        on_error(exc)
                    continue
                yield full, info
    except ValueError:
        # An embedded null byte in the root. Not an OSError, so `onerror` never
        # saw it and it would have left this module through a never-raises
        # caller. Nothing was measured, which is what an unreadable root means.
        return


def tree_bytes(
    root: Path | str,
    *,
    dedupe_inodes: bool = False,
    include_dirs: bool = False,
    on_error: ErrorHandler | None = None,
) -> int:
    """Total bytes occupied below *root*, du-style.

    ``dedupe_inodes`` counts each ``(st_dev, st_ino)`` once, which is what a
    tree full of hardlinks needs: uv's cache links a wheel into every venv that
    wants it, and counting one per link reports an overage no reclaim can clear.
    """
    total = 0
    seen: set[tuple[int, int]] = set()
    for _full, info in iter_tree(root, include_dirs=include_dirs, on_error=on_error):
        if dedupe_inodes:
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
        total += entry_bytes(info)
    return total


def first_level_dirs(
    root: Path | str, *, on_error: ErrorHandler | None = None,
) -> list[Path]:
    """The immediate subdirectories of *root*, sorted by name.

    A symlink is skipped whether or not it points at a directory: following one
    would reach outside the root, and every caller's subject is a tree it is
    about to measure, sweep or attribute to the user whose name is on the entry.
    A plain file is skipped. An unreadable root is an empty list.

    The per-entry ``except`` and the root one are separate arms and both fire.
    A mode-``0444`` parent is what separates them and is the case worth knowing:
    listing a directory needs ``r`` and stat'ing what is in it needs ``x``, so
    ``iterdir`` succeeds and then *every* entry's ``is_symlink`` raises
    ``PermissionError`` — which ``pathlib`` does not swallow, unlike the ENOENT
    / ENOTDIR / EBADF / ELOOP set it ignores by design. A caller counting
    unreadable entries gets one per entry there, and nought directories, which
    is the honest answer for a directory whose contents cannot be examined.

    Sorted because two of the callers iterate for an operator-visible answer
    and a directory order that depends on how the filesystem happened to fill
    is not one.
    """
    root = Path(root)
    try:
        entries = sorted(root.iterdir())
    except (OSError, ValueError) as exc:
        if on_error is not None and isinstance(exc, OSError):
            on_error(exc)
        return []
    found: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except (OSError, ValueError) as exc:
            if on_error is not None and isinstance(exc, OSError):
                on_error(exc)
            continue
        found.append(entry)
    return found
