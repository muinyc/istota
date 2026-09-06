"""Per-file advisory flock for USER.md / CHANNEL.md parse-modify-write.

Two writers can race on USER.md: the nightly curator (long-running brain
call followed by an `os.replace`) and one or more runtime CLI calls fired
from interactive tasks. Last writer wins. Without a lock, ops can be
silently dropped.

`memory_md_lock(path, timeout=..., lock_dir=...)` returns a context manager
that acquires an exclusive flock on a **local** anchor file (NOT next to the
target). USER.md lives on the rclone/FUSE Nextcloud mount, where
`fcntl.flock` is unreliable (it can be a silent no-op or raise
`ENOLCK`/`ENOTSUP`) and a sibling `USER.md.lock` clutters the user's config
dir. The anchor name is keyed on a hash of the target's absolute path so two
writer processes deterministically agree on the same anchor while different
targets never collide. On timeout, raises `MemoryMdLocked`.

Where the anchor lives matters for cross-process exclusion. The two writers
are the nightly curator (always on the host) and the runtime memory CLI
(host-side under the skill proxy, or *inside* the bwrap sandbox when the
proxy is off). bwrap mounts a private tmpfs over `/tmp`, so a top-level
`/tmp/...` anchor a sandboxed CLI creates is invisible to the host curator —
silently breaking the lock. The fix: callers in the daemon pass `lock_dir`
pointed at the per-user deferred dir (`config.temp_dir/<user_id>`, exported
as `ISTOTA_DEFERRED_DIR`), which the executor bind-mounts into the sandbox
at the same path. An anchor there is the same inode for the host curator,
the host proxy CLI, and a sandboxed CLI alike — and it's per-user, so no
cross-tenant reach. `deferred_lock_dir()` builds that path. The system-temp
default is only a fallback for ad-hoc/host-only use (manual CLI, tests).

Linux + macOS: `fcntl.flock`. Windows is not a supported deployment for
istota; we don't paper over that here.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from istota.file_lock import exclusive_lock


class MemoryMdLocked(RuntimeError):
    """Raised when the parse-modify-write lock can't be acquired in time."""


_LOCK_SUBDIR = ".md-locks"


def _default_lock_dir() -> Path:
    """Fallback anchor dir for ad-hoc/host-only use (manual CLI, tests).

    The system temp dir is local (never the FUSE mount) and the same for
    every istota process on the host. Daemon writers should pass an explicit
    `lock_dir` via `deferred_lock_dir()` instead — see the module docstring
    for why a top-level `/tmp` anchor doesn't survive the bwrap sandbox.
    """
    return Path(tempfile.gettempdir()) / "istota-md-locks"


def deferred_lock_dir(deferred_dir: Path) -> Path:
    """Anchor dir under a task's deferred/temp dir (`ISTOTA_DEFERRED_DIR`).

    The deferred dir is a local (non-FUSE) path the executor bind-mounts into
    the bwrap sandbox at the same path, so an anchor here is the same inode
    whether the writer is the host curator, the host skill-proxy CLI, or a
    sandboxed CLI — restoring the mutual exclusion a top-level `/tmp` anchor
    loses to the sandbox's private `/tmp` tmpfs. Kept in a `.md-locks`
    subdir so the anchors don't sit next to (or get scanned with) the
    task-scoped deferred JSON files.
    """
    return deferred_dir / _LOCK_SUBDIR


def lock_path_for(target_path: Path, *, lock_dir: Path | None = None) -> Path:
    """Return the local flock-anchor path for `target_path`.

    Deterministic in the target's absolute path: two processes (nightly
    curator + runtime CLI) compute the same anchor, while two different
    users' USER.md files (same basename, different dirs) never collide. The
    target's basename is kept in the anchor name purely so a human reading
    the lock dir can tell what it guards.
    """
    base = lock_dir if lock_dir is not None else _default_lock_dir()
    abs_target = os.path.abspath(str(target_path))
    digest = hashlib.sha256(abs_target.encode("utf-8")).hexdigest()[:16]
    return base / f"{target_path.name}.{digest}.lock"


@contextmanager
def memory_md_lock(
    target_path: Path,
    *,
    timeout_seconds: float = 5.0,
    lock_dir: Path | None = None,
):
    """Acquire an exclusive flock on a local anchor for `target_path` for the
    duration of the context. Polls every 100 ms, raises `MemoryMdLocked`
    after `timeout_seconds`. The anchor file is created lazily and left in
    place — it carries no lock state once the FD closes; the OS releases the
    flock on context exit (and unconditionally on process death).

    The acquisition loop itself is `istota.file_lock.exclusive_lock`, shared
    with the money work store and the ledger. This wrapper survives under its
    own name because `MemoryMdLocked` and the anchor-path derivation are part
    of this package's surface."""
    lock_path = lock_path_for(target_path, lock_dir=lock_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        poll_seconds=0.1,
        on_timeout=MemoryMdLocked,
    ):
        yield
