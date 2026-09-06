"""Bound the on-disk package caches the sandbox creates per user (ISSUE-317).

ISSUE-305 moved a task's uv and npm caches off bubblewrap's root tmpfs and onto
disk, because a cache in RAM is unattributable, capped at half the box's memory,
and thrown away at task exit so every task downloads again. The cost of fixing
that is that the caches now *persist*, and nothing removed them: on the
reference deployment one ``uv sync --all-extras`` is about 1.8 GB of wheels, and
the volume the caches share with ``developer.repos_dir`` was already at 79%.
Turning the key on without this module trades a bounded RAM burn for an
unbounded disk leak on the fuller resource.

:func:`sweep_and_report` runs from the **scheduler**, on
``scheduler.sandbox_cache_sweep_interval``, for the same reason
:mod:`istota.worktree_reaper` does: ``dispatch_setup_env_hooks`` calls every
skill's ``setup_env`` whatever the task selected, so a sweep there would fire
before every Talk reply, every cron job and every heartbeat tick. A delete path
belongs on a stated cadence.

**A size ceiling, not an age rule.** An age window is the obvious policy and it
does not work here. A single dependency resolution writes more than any sane
window's worth of bytes at once — 456 MB for one ``uv sync --extra test`` plus
``npm ci``, roughly 1.8 GB for ``--all-extras`` — so a rule phrased in days
either keeps everything or throws away a cache that is minutes old and about to
be reused. What the operator actually has is a fixed volume, so the budget is
stated in bytes. Every visited cache gets the cheap reclaim first (``uv cache
prune``, ``npm cache verify``), which removes unreachable entries and keeps the
warm ones; only a cache still over its ceiling afterwards is wiped.

**The sweeper never deletes a file itself.** Not the root, not a per-user
directory, not a cache entry. It runs the package managers' own reclaim verbs
and measures the result. A tool that is missing, that fails, or that times out
is reported and the cache is left alone — there is no ``rm -rf`` fallback for a
directory we could not get a tool to reclaim properly, because the difference
between "uv's cache" and "everything the model put in this directory" is exactly
what uv knows and this module does not.

**The concurrency hazard, and the three guards that answer it.** A wipe that
lands while a developer task is mid-``uv sync`` against the same cache breaks
that task: the sync has resolved a wheel to a cache path and unlinking it under
the process turns the next ``link(2)`` into ``ENOENT``. Hoping uv tolerates that
is not a plan, so:

1. **The caller's in-flight set.** ``busy_users`` names every user with a task
   holding a live worker, read from the task table by the scheduler wrapper.
   A user in that set is skipped *entirely* — not even the cheap reclaim, since
   ``uv cache prune`` unlinks as surely as ``clean`` does. This is the only
   guard that sees a sync against a fully warm cache, which writes nothing at
   all and merely hardlinks out. A caller that cannot answer the question must
   pass no sweep at all rather than an empty set; the wrapper does that.

   **The set is a snapshot and it ages across the sweep.** It is read once, so
   the caches visited last are judged against a reading that can be tens of
   minutes old by then — every cache costs a tree walk and up to four
   subprocesses, each bounded at :data:`_TOOL_TIMEOUT`. Re-reading it per user
   would mean this module holding a database handle, which is the one thing a
   leaf here must not do. Stated as a cost rather than left for a reader to
   work out from the call site.
2. **An idle window on the tree's newest mtime**, :data:`DEFAULT_MIN_IDLE_SECONDS`.
   This covers a writer the task table never knew about — an operator shell, a
   devbox, a task that started after the busy set was read — and it is
   deliberately short, because it is a backstop and not the policy. A cache
   being written *right now* is the case it catches, which is also the case
   guard 1's staleness is most likely to have missed.
3. **uv's own in-use check**, preserved by never passing ``--force`` to either
   ``cache prune`` or ``cache clean``. uv takes an exclusive lock on the cache
   and holds it for the whole of an install, and it is the last thing standing
   when guards 1 and 2 both lose a race with the kernel. **It blocks rather
   than refusing**, which is worth knowing because it changes what happens
   next: the call waits, :data:`_TOOL_TIMEOUT` eventually kills it, and the
   outcome is reported as a tool failure. Safe, and slow — a reclaim that has
   to wait is one that should have been skipped. It also runs the other way:
   this module's own ``clean`` holds that lock for the whole delete, so a
   task's ``uv sync`` starting mid-sweep queues behind it. ``npm``'s
   ``--force`` on ``cache clean`` is *not* the same flag: npm has no in-use
   check to bypass, it is refusing an operation it considers unnecessary, so
   the npm half rests on guards 1 and 2 alone.

The accepted cost, stated rather than implied: a deployment busy enough that
some user always has a task in flight will keep skipping that user, and the
ceiling becomes advisory for them. The skip is logged with its reason so that
shows up as a growing number rather than as silence. Skipping is the right way
round — a cache one interval too large costs disk, a wipe one second too early
costs a task.

**Two layouts, because ``resolve_sandbox_cache_dir`` has two branches.** With
``developer.repos_dir`` set the cache is derived at
``{repos_dir}/{user_id}/{CACHE_ROOT_NAME}``; without it the cache is
``{security.sandbox_cache_dir}/{user_id}``. ``user_ids`` on
:func:`sweep_caches` selects which, and the difference is not cosmetic — it
decides where the *user id* comes from.

**Containment, and why the id's provenance is the crux.** The rule is an
equality in both shapes: a candidate must resolve to exactly the path the
layout names, which a symlink fails by construction. What changes is what a
name is worth. Under one level the root is an operator-named directory outside
``repos_dir``, so an entry that resolves to its own name inside it is that
user's cache and the name can be trusted. Under two levels the root *is*
``developer.repos_dir``, bound read-write into every admin developer task — a
task can create ``{repos_dir}/zzz/`` and fill it, and a sweeper enumerating the
tree would take ``zzz`` for a user id, ask the busy check about a user that
cannot possibly have a task in flight, and then run a reclaim verb inside a
directory the model chose. So on the derived layout the caller supplies the
user ids and this module derives one path each, reading no name back out of
the tree. Getting that list wrong is safe in both directions: a missing user is
a cache that goes unswept, an invented one measures zero.

Either way the *resolved* path is what goes on to the subprocess, never the
entry as read: the check and the use are separated by a tree walk and up to
four subprocesses, so an unresolved path can be renamed away and replaced with
a symlink inside the window.

For the same reason the tools are run with their cwd in a fresh temporary
directory rather than in the cache, with ``uv --no-config``, with npm's user
and global config files pointed at two names inside that directory (two, never
one: npm refuses a path it has already loaded under another scope, so pointing
both at ``os.devnull`` exited 1 before the cache verb ran), and with an
environment built from an allowlist rather than inherited whole — the daemon's own
environment carries the secret key and every module credential, and a process
whose job is to unlink files needs none of it. The per-user cache is
model-written by construction; a host-side tool started with its cwd inside it
would pick up a ``uv.toml`` or an ``.npmrc`` the model wrote, as the daemon
user. Same shape as the reasoning in :mod:`istota.git_hardening`, for a
different pair of programs.

A tool is run only where its own subdirectory exists, which also covers the one
asymmetry between the two: ``uv cache clean`` removes the cache *directory*
along with its contents, while ``npm cache clean`` empties one and leaves it.
Both are right — uv recreates it on the next sync, and the sweep after a wipe
simply has no uv half to reclaim.

**Measurement is du-style**, and the walk itself now lives in ``du.py``.
Blocks rather than apparent size, because the ceiling exists to protect a
volume and blocks are what fill one; an inode is counted once, because uv's
cache is full of hardlinks and counting a shared inode per link would report an
overage that no amount of reclaiming can clear. Directory inodes *are* counted
here (``include_dirs=True``), unlike in ``session_log``'s sweep: uv's
``archive-v0`` is one directory per unpacked wheel, so they are a real part of
what a package cache occupies. A symlink is stat'd, never followed out of the
tree.

**The ceiling counts the whole per-user directory, and only two tools can act
on it.** ``XDG_CACHE_HOME`` points at the user root, so a third tool's cache —
``huggingface/`` is the one shipped today — lands beside ``uv/`` and ``npm/``
and counts toward the budget while neither reclaim verb can touch it. That case
reports ``still-over`` and names the largest subdirectory rather than looping or
reaching for the filesystem, which is the honest answer and the one an operator
can act on.

**The subdirectory names are restated here, not imported.** They belong to
``executor.SANDBOX_CACHE_UV`` / ``SANDBOX_CACHE_NPM``, and importing
:mod:`istota.executor` would drag the whole task path into a maintenance
thread. ``tests/test_sandbox_cache_sweeper.py`` holds the two pairs equal, the
same way the forge-CLI version literals are held across the role and the two
Dockerfiles.

stdlib-only apart from :mod:`istota.du`, which holds the tree walk and the
first-level directory scan this module shares with the session-log sweep and
with ``doctor``; ``du`` is itself a leaf that imports nothing from the package,
so the boundary this file depends on still holds one level down. Takes its root
and its policy as parameters rather than reading a ``Config``, and never raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import NamedTuple

from istota import du

logger = logging.getLogger("istota.sandbox_cache_sweeper")

# Mirrors executor.SANDBOX_CACHE_UV / SANDBOX_CACHE_NPM — see the module
# docstring for why these are a copy and what holds them equal.
CACHE_UV = "uv"
CACHE_NPM = "npm"

# Mirrors executor.SANDBOX_CACHE_ROOT_NAME — the derived cache's name inside a
# user's own repos subtree. A copy for the same reason the two above are, and
# held equal by the same test.
CACHE_ROOT_NAME = ".package-caches"

# The default budget, per user. Two full `uv sync --extra test` sets plus their
# npm counterparts, with room for the wheels a second Python version pulls in —
# large enough that a working deployment never trips it, small enough that a
# 40 GB volume shared with `developer.repos_dir` survives a handful of users.
DEFAULT_MAX_BYTES = 10 * 1024 ** 3

# The floor the ceiling is clamped to. Below roughly a gigabyte the ceiling is
# under the working set of a *single* dependency resolution, so every sweep
# would wipe a cache that is doing its job and the next task would re-download
# the same bytes — the exact behaviour ISSUE-305 removed, restored by a config
# typo. The knob stays useful; it just cannot be set to "never keep anything".
MIN_MAX_BYTES = 1024 ** 3

# How long the cache tree must have been unwritten before the sweep will act.
# Guard 2 in the module docstring: a backstop for a writer the caller's
# in-flight set never knew about, not the policy.
DEFAULT_MIN_IDLE_SECONDS = 900.0

# Per invocation. `npm cache verify` walks every entry in the cache index and a
# cold, large cache is genuinely slow, so this is generous — it bounds a wedged
# binary rather than a busy one.
_TOOL_TIMEOUT = 900

ACTION_OUTSIDE = "outside"          # not where the layout says it should be; nothing run
ACTION_BUSY = "busy"                # the user has a task in flight
ACTION_RECENT = "recent"            # something wrote into the cache too recently
ACTION_FUTURE_MTIME = "future-mtime"  # stamped ahead of the clock; a pin or a clock fault
ACTION_NO_TOOLS = "no-tools"        # over the ceiling with no reclaim verb available
ACTION_RECLAIMED = "reclaimed"      # swept, and inside the ceiling afterwards
ACTION_WIPED = "wiped"              # escalated to a full clean, and now inside it
ACTION_STILL_OVER = "still-over"    # everything available was run and it is still over
ACTION_SWAPPED = "swapped"          # the directory changed identity mid-sweep; nothing further run


class CacheSize(NamedTuple):
    """Disk usage of a tree, and the newest mtime anywhere in it."""

    bytes: int
    newest_mtime: float


class SweepOutcome(NamedTuple):
    user_id: str
    path: Path
    action: str
    before_bytes: int
    after_bytes: int
    detail: str = ""


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def measure_cache(path: Path) -> CacheSize:
    """Disk usage and newest mtime of ``path``, du-style. Never raises.

    An inode is counted once: uv's cache hardlinks aggressively, and counting a
    shared inode per link reports an overage that reclaiming cannot clear.
    Symlinks are stat'd but never followed, so nothing outside the tree is
    counted and nothing outside it can be reached.
    """
    total = 0
    newest = 0.0
    seen: set[tuple[int, int]] = set()

    def _on_error(exc: OSError) -> None:
        logger.debug("sandbox_cache_sweeper: skipping %s (%s)", getattr(exc, "filename", "?"), exc)

    try:
        if not path.is_dir():
            return CacheSize(0, 0.0)
        root_stat = path.lstat()
        newest = root_stat.st_mtime
    except OSError:
        return CacheSize(0, 0.0)

    # `include_dirs=True`: a package cache's directory inodes are a real part of
    # what it occupies (uv's `archive-v0` is one directory per unpacked wheel),
    # and the idle window below reads a directory's mtime as activity.
    for _full, info in du.iter_tree(path, include_dirs=True, on_error=_on_error):
        if info.st_mtime > newest:
            newest = info.st_mtime
        key = (info.st_dev, info.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += du.entry_bytes(info)
    return CacheSize(total, newest)


def _largest_child(path: Path) -> tuple[str, int]:
    """The biggest immediate subdirectory of ``path``, for the ``still-over`` note."""
    biggest = ("", 0)
    for entry in du.first_level_dirs(path):
        # The same measurement `measure_cache` makes, minus the mtime it does
        # not need: hardlinks counted once, directory inodes counted.
        size = du.tree_bytes(entry, dedupe_inodes=True, include_dirs=True)
        if size > biggest[1]:
            biggest = (entry.name, size)
    return biggest


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------

def _is_one_component(user_id: str) -> bool:
    """Whether *user_id* is a single, ordinary path component.

    Everything a join would treat as navigation rather than as a name: empty,
    ``.``, ``..``, anything holding a separator, anything absolute. Written out
    rather than inferred from a parent comparison because ``PurePath`` keeps
    ``..`` literal, so ``(root / "..").parent == root`` and the comparison says
    yes to the one value that matters most.

    ``os.altsep`` is checked as well as ``os.sep``: it is ``None`` on Linux and
    ``\\`` on Windows, and a rule about what is safe to join should not be
    quietly narrower on the platform nobody tests on.
    """
    if not user_id or user_id in (".", ".."):
        return False
    if os.sep in user_id or (os.altsep and os.altsep in user_id):
        return False
    return not Path(user_id).is_absolute()


def _candidates_in_root(root: Path) -> Iterator[tuple[str, Path, bool]]:
    """One-level layout: each entry in ``root`` is a user's cache.

    The shape ``security.sandbox_cache_dir`` produces —
    ``resolve_sandbox_cache_dir`` creates ``{root}/{user_id}`` and nothing
    deeper on that branch — so a candidate is a directory that **resolves to
    its own name inside the root**: ``entry.resolve() == root.resolve() /
    entry.name``. A directory that fails that yields ``False`` and is reported
    rather than silently skipped: a planted symlink is the interesting case,
    and a sweep that quietly ignored one would look identical to a sweep that
    found nothing.

    **Equality, not "the resolved parent is the root".** The weaker test reads
    as though it excludes every symlink and does not: ``{root}/zzz`` pointing at
    ``{root}/bob`` resolves to a path whose parent *is* the root, so it passes
    — and then ``user_id`` is taken from the entry, so the busy check asks
    whether ``zzz`` has a task in flight while the reclaim verb runs against
    bob's real cache. That is guard 1 defeated by a name, and it was found by
    review after the weaker rule had been written down as sufficient. Requiring
    the resolved path to carry the same name closes both that and the
    target-outside-the-root case in one comparison.

    **The user id still comes from the tree here, and that is now the narrower
    of the two shapes.** It is sound because this root is an operator-named
    directory that is not inside ``developer.repos_dir`` — the derived layout
    is what covers the case where it would be, and
    :func:`_candidates_for_users` takes the id from the daemon instead. The
    equality above is what makes a name read back from this tree trustworthy:
    an entry that is not what it says it is never yields ``True``.

    **What is yielded is the resolved path, not the entry as read**, and that is
    the half that makes the rule worth anything. The check and the use are
    separated by a full tree walk and up to four subprocesses, so a validated
    ``{root}/alice`` that is handed on unresolved can be renamed away and
    replaced with a symlink inside the window. A resolved path has no symlink
    component left in it, so there is nothing to swap.
    """
    try:
        resolved_root = root.resolve()
        entries = sorted(root.iterdir())
    except (OSError, ValueError) as exc:
        logger.warning("sandbox_cache_sweeper: %s is unreadable (%s); nothing swept.", root, exc)
        return

    for entry in entries:
        try:
            # A plain file in the root is not a cache and nothing here would
            # remove one; it needs no outcome row.
            if not entry.is_dir():
                continue
            resolved = entry.resolve()
            if resolved != resolved_root / entry.name:
                yield entry.name, entry, False
                continue
        except OSError:
            continue
        # The *resolved* path is what goes on, never the entry as read. The
        # check and the use are separated by a full tree walk and two
        # subprocesses, and the model can rename its own cache directory and
        # drop a symlink in its place inside that window — so validating one
        # path and handing a different one to `uv cache clean` would leave the
        # containment rule describing a check nothing acted on. A resolved path
        # contains no symlink component, so there is nothing left to swap.
        yield entry.name, resolved, True


def _candidates_for_users(
    root: Path, user_ids: Collection[str],
) -> Iterator[tuple[str, Path, bool]]:
    """Two-level layout: ``{root}/{user_id}/{CACHE_ROOT_NAME}``, derived.

    The shape ``developer.repos_dir`` produces. ``root`` is the *repos* root, a
    tree of per-user subtrees, and a user's cache is a fixed name inside their
    own subtree — so the entries between the root and the cache are that user's
    clones and worktrees, which are model-written by construction.

    **The user ids come from the caller and are never read back from the tree,
    and that is the whole difference between this function and the one above.**
    Under one level the equality made a name found in the root trustworthy;
    under two it cannot, because the root here is the directory bound
    read-write into every admin developer task. A task can make
    ``{repos_dir}/zzz/`` and put anything under it, and an enumerating sweeper
    would then take ``zzz`` as a user id — asking the busy check about a user
    that does not exist (which can never be in flight, so guard 1 always
    passes) and running a reclaim verb against a directory of the model's
    choosing. So the daemon says who the users are and this derives one path
    per user. A user the caller omits is a cache that is never swept, which
    costs disk; a user the caller invents finds no directory and measures zero.
    Wrong in the safe direction either way.

    The containment rule stays an **equality**, now against the whole derived
    path: ``candidate.resolve() == root.resolve() / user_id / CACHE_ROOT_NAME``.
    Both components are model-plantable, and the equality refuses either being
    a symlink — a link at ``{repos_dir}/{user_id}`` aiming into another user's
    subtree, or one at ``.package-caches`` aiming anywhere at all.

    A lexical check on the id runs first, and it is worth being exact about
    what each half does because the obvious division of labour is wrong.
    ``PurePath`` does *not* collapse ``..``, so ``(root / "..").parent == root``
    — a parent comparison alone lets a bare ``..`` through to ``resolve()``,
    which answers perfectly happily for a path outside the root. The equality
    would still refuse the result, but only after a traversal outside the root
    had been stat'd. So the id is checked against a small explicit rule
    instead: not empty, not ``.`` or ``..``, no separator, not absolute. What
    the *resolved* equality is for is symlinks, which are children by name and
    somewhere else on disk. Same two-check pair as
    ``executor.get_user_repos_dir``, with the labour divided the way that
    function's docstring divides it.

    A user with no cache directory yet is skipped silently rather than reported:
    on this layout that is every user who has not run a task, and an outcome row
    each would drown the ones that mean something. Orphans run the other way —
    a cache whose user is not in the list — and :func:`report_orphan_caches` is
    what makes those visible without acting on a name from this tree.

    **The equality assumes a case-sensitive filesystem**, stated so nobody
    hand-tests the guard on a mac and concludes it holds. ``Path.resolve()``
    uses ``realpath``, which does not canonicalise case, so on darwin two ids
    differing only in case both satisfy the equality against one real directory
    and the busy check is then asked about the spelling that was passed rather
    than the one that owns the cache. The deployment target is Linux, where the
    kernel makes the two ids different directories.
    """
    try:
        resolved_root = root.resolve()
    except (OSError, ValueError) as exc:
        logger.warning("sandbox_cache_sweeper: %s is unreadable (%s); nothing swept.", root, exc)
        return

    for user_id in sorted({str(u) for u in user_ids}):
        if not user_id:
            continue
        subtree = root / user_id
        candidate = subtree / CACHE_ROOT_NAME
        try:
            # Lexical first, and stated as a rule about the id rather than as a
            # parent comparison — `PurePath` keeps `..` as a literal component,
            # so `(root / "..").parent` *is* the root and a bare `..` would
            # otherwise reach `resolve()` and stat a path outside the tree.
            if not _is_one_component(user_id) or candidate.parent != subtree:
                yield user_id, candidate, False
                continue
            if not candidate.is_dir():
                continue
            if candidate.resolve() != resolved_root / user_id / CACHE_ROOT_NAME:
                yield user_id, candidate, False
                continue
        except (OSError, ValueError, TypeError):
            # `resolve()` raises ValueError on an embedded NUL and the joins
            # raise TypeError on an id that is not a string. Both come from a
            # profile row rather than from this module, and `sweep_caches`
            # promises never to raise — a generator's exception surfaces at the
            # caller's `for`, which is outside its per-user `try`.
            continue
        # Resolved, for the reason `_candidates_in_root` gives: the check and
        # the use are separated by a tree walk and up to four subprocesses.
        yield user_id, candidate.resolve(), True


def _identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path*, opened without following a final symlink.

    The anchor for :func:`_still_the_same`. ``O_NOFOLLOW`` matters rather than
    ``lstat``: it refuses at the *last* component, which is the one an attacker
    can replace, and it does so at open time rather than telling us about a link
    we would then have to reason about.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    return (info.st_dev, info.st_ino)


def _still_the_same(path: Path, pinned: tuple[int, int] | None) -> bool:
    """Whether *path* still names the inode it did when *pinned* was taken.

    **Why a resolved path is not enough on the derived layout, contrary to what
    this module used to claim.** Under ``security.sandbox_cache_dir`` the cache
    root's *parent* was never bound into a sandbox, so no component of a
    resolved cache path was reachable from a task and resolving really did
    remove everything swappable. Under ``developer.repos_dir`` the whole subtree
    ``{repos_dir}/{user_id}`` is bound read-write into that user's own admin
    developer tasks, so ``.package-caches`` is an ordinary entry a task can
    ``mv`` aside and replace with a symlink — on the host, while the sweep is
    running. A resolved path is a string of names, and every consumer here
    re-traverses it: ``measure_cache`` walks the tree, then ``_reclaim`` joins
    ``uv``/``npm`` onto it and hands those to subprocesses bounded at
    :data:`_TOOL_TIMEOUT`. That is minutes of window per round, and ``npm cache
    clean --force`` has no in-use check behind it (guard 3 is uv's alone).

    So the identity is pinned once and re-asserted immediately before each
    round. **It narrows the window rather than closing it**: the tool still
    opens the path by name after this returns, so a swap in the microseconds
    between the check and ``execve`` still lands. Closing it completely means
    handing the tools an fd-anchored path (``/proc/self/fd/N``), which is
    Linux-only and changes what the tools are told their cache directory is
    called. What actually protects a *live* task remains guard 1, the busy set;
    this is what stops a wipe being aimed somewhere else entirely.
    """
    if pinned is None:
        return False
    return _identity(path) == pinned


# --------------------------------------------------------------------------
# Running a package manager's own reclaim verb
# --------------------------------------------------------------------------

# What a reclaim verb is allowed to inherit. An allowlist, not the daemon's
# environment minus a few names: on a deployment the daemon is started from a
# systemd `EnvironmentFile` and carries the secret key, the Nextcloud app
# password and every module credential, and none of that is any business of a
# subprocess whose whole job is to unlink files. It is also the only way to be
# sure of the two variables that would quietly break the sweep — an inherited
# `npm_config_cache` redirects the reclaim, and `UV_NO_CACHE` makes uv work out
# of a temporary directory so the prune reclaims nothing — since npm reads its
# entire configuration out of that namespace and a deny-list has to guess at
# every spelling of it.
_INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


def _tool_env(uv_dir: Path, npm_dir: Path, scratch: Path) -> dict[str, str]:
    """A minimal environment with the cache locations pinned and config disarmed.

    ``scratch`` is the throwaway working directory the round already runs in.
    The two npm config paths are named inside it rather than both at
    :data:`os.devnull`, and the distinctness is the whole point: npm resolves
    its configuration by loading each scope in turn and **refuses a path it has
    already loaded under another scope**, exiting 1 with ``double-loading config
    "/dev/null" as "global", previously loaded as "user"`` before it reaches the
    cache verb. So the obvious way to disarm both files disarmed the reclaim
    instead — measured on a live deployment, where every sweep logged
    ``npm exited 1`` and took zero npm bytes while reporting a cache reclaimed.

    Neither file is created. A path that does not exist is an empty config to
    npm, which is what ``--no-config`` buys from uv on the other side, and
    naming them under a directory this module just made is what keeps them out
    of reach: ``~/.npmrc`` and ``/usr/etc/npmrc`` are outside this module's
    control and inside the model's on some deployments.
    """
    env = {
        key: os.environ[key] for key in _INHERITED_ENV if key in os.environ
    }
    env["UV_CACHE_DIR"] = str(uv_dir)
    env["npm_config_cache"] = str(npm_dir)
    env["npm_config_userconfig"] = str(scratch / "npmrc-user")
    env["npm_config_globalconfig"] = str(scratch / "npmrc-global")
    return env


def _run(argv: list[str], cwd: str, env: dict[str, str]) -> tuple[bool, str]:
    """Run one reclaim verb. Returns (succeeded, detail). Never raises."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=_TOOL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"{Path(argv[0]).name} timed out after {_TOOL_TIMEOUT}s"
    except OSError as exc:
        return False, f"{Path(argv[0]).name} could not be run ({exc})"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, f"{Path(argv[0]).name} exited {proc.returncode}: {tail[-1] if tail else ''}"
    return True, ""


def _uv_argv(binary: str, uv_dir: Path, verb: str) -> list[str]:
    # `--cache-dir` on the argv as well as `UV_CACHE_DIR` in the environment, so
    # an inherited variable cannot redirect the removal. Never `--force`: that
    # bypasses uv's in-use check, which is guard 3.
    return [binary, "--no-config", "--cache-dir", str(uv_dir), "cache", verb]


def _npm_argv(binary: str, npm_dir: Path, verb: str) -> list[str]:
    argv = [binary, "cache", verb, "--cache", str(npm_dir)]
    if verb == "clean":
        # npm's `--force` is not uv's: there is no in-use check behind it, it is
        # npm declining an operation it considers unnecessary. The npm half is
        # protected by guards 1 and 2 alone, which is why it is safe to pass.
        argv.append("--force")
    return argv


def _reclaim(
    user_dir: Path,
    verbs: tuple[str, str],
    uv_bin: str | None,
    npm_bin: str | None,
    pinned: tuple[int, int] | None = None,
) -> tuple[int, int, list[str]]:
    """Run one round (``verbs`` = the uv verb and the npm verb).

    Returns ``(ran, missing, notes)``. ``missing`` counts a tool whose cache
    subdirectory is *there* and whose binary is not — which is a different
    outcome from a cache that simply holds nothing of that tool's, and the
    caller reports the two differently.

    ``pinned`` is re-asserted immediately before **each** tool rather than once
    per round, and the two are far apart in wall-clock terms: the uv call can
    block for the whole of :data:`_TOOL_TIMEOUT` on uv's own cache lock, and the
    npm call runs after it. Checking once at the top would leave the npm
    ``execve`` minutes from its evidence — and npm is the half with no in-use
    check of its own. ``None`` skips the assertion, which is what the tests and
    any future caller with no identity to offer get.
    """
    uv_dir = user_dir / CACHE_UV
    npm_dir = user_dir / CACHE_NPM
    ran = 0
    missing = 0
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="istota-cache-sweep-") as cwd:
        env = _tool_env(uv_dir, npm_dir, Path(cwd))
        for binary, name, directory, argv in (
            (uv_bin, "uv", uv_dir, _uv_argv(uv_bin or "", uv_dir, verbs[0])),
            (npm_bin, "npm", npm_dir, _npm_argv(npm_bin or "", npm_dir, verbs[1])),
        ):
            if not directory.is_dir():
                continue
            if not binary:
                missing += 1
                notes.append(f"{name} is not installed, so its cache was not reclaimed")
                continue
            if pinned is not None and not _still_the_same(user_dir, pinned):
                notes.append(
                    f"the cache directory changed identity before {name} ran; stopped"
                )
                break
            ok, detail = _run(argv, cwd, env)
            ran += 1
            if not ok:
                notes.append(detail)
    return ran, missing, notes


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def sweep_caches(
    root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    busy_users: Collection[str] = (),
    user_ids: Collection[str] | None = None,
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS,
    floor_bytes: int = MIN_MAX_BYTES,
    now: float | None = None,
) -> list[SweepOutcome]:
    """Bring every per-user cache under ``root`` inside ``max_bytes``. Never raises.

    **``user_ids`` selects the layout**, because the two the daemon can produce
    put a cache in different places and only one of them can be enumerated
    safely:

    * ``None`` — ``root`` is ``security.sandbox_cache_dir`` and each entry in it
      is a user's cache (:func:`_candidates_in_root`). The shape a deployment
      running the sandbox without the developer skill has.
    * a collection — ``root`` is ``developer.repos_dir`` and a user's cache is
      ``{root}/{user_id}/{CACHE_ROOT_NAME}`` (:func:`_candidates_for_users`).
      The ids come from the daemon; nothing here reads a user id out of a tree
      the model can write. Passing an empty collection is a valid answer
      meaning "no users", and sweeps nothing.

    ``floor_bytes`` is the clamp :data:`MIN_MAX_BYTES` describes. It is a
    parameter only so the tests can exercise the ceiling on kilobytes instead of
    on gigabytes; no caller passes anything but the default.

    ``now`` is likewise injected for the tests. Everything else comes from the
    caller because this module reads no configuration of its own.
    """
    root_path = Path(root)
    if max_bytes < floor_bytes:
        logger.warning(
            "sandbox_cache_sweeper: a ceiling of %d bytes is below the %d-byte floor "
            "and would wipe a cache after every single dependency resolution; "
            "using the floor.", max_bytes, floor_bytes,
        )
        max_bytes = floor_bytes

    uv_bin = shutil.which("uv")
    npm_bin = shutil.which("npm")
    stamp = time.time() if now is None else now
    busy = set(busy_users)
    outcomes: list[SweepOutcome] = []

    candidates = (
        _candidates_in_root(root_path) if user_ids is None
        else _candidates_for_users(root_path, user_ids)
    )
    for user_id, entry, usable in candidates:
        if not usable:
            outcomes.append(SweepOutcome(
                user_id, entry, ACTION_OUTSIDE, 0, 0,
                "does not resolve to the path the cache layout names",
            ))
            continue
        # Per user, so one cache that blows up in an unforeseen way cannot end
        # the sweep and leave every later one unswept while the caller reads the
        # result as complete.
        try:
            outcomes.append(_sweep_one(
                user_id, entry, max_bytes, busy, min_idle_seconds, stamp, uv_bin, npm_bin,
            ))
        except Exception:  # noqa: BLE001 — see above
            logger.exception("sandbox_cache_sweeper: sweeping %s failed", entry)
    return outcomes


def _sweep_one(
    user_id: str,
    user_dir: Path,
    max_bytes: int,
    busy: set[str],
    min_idle_seconds: float,
    stamp: float,
    uv_bin: str | None,
    npm_bin: str | None,
) -> SweepOutcome:
    # `user_id` is passed rather than taken from `user_dir.name`, and that is
    # load-bearing on the derived layout, where the directory is called
    # `.package-caches` for everybody — reading the name there would ask the
    # busy check about `.package-caches` instead of about a user, so guard 1
    # would pass for every cache on the deployment.
    before = measure_cache(user_dir)

    if user_id in busy:
        return SweepOutcome(
            user_id, user_dir, ACTION_BUSY, before.bytes, before.bytes,
            "a task for this user is in flight",
        )
    # **Clamped, because this mtime is model-controlled.** The tree is bound
    # read-write into that user's own sandbox, so one `touch -d '+10 years'`
    # inside it makes `idle` negative, which is below any window — and the cache
    # is then pinned for good, which is the unbounded disk leak this module
    # exists to prevent, restored by a single command. Without the clamp it is
    # also invisible: `recent` is not warned, so an operator sees a count and
    # not a growing cache, and the negative duration in the detail reads as an
    # arithmetic bug rather than as a boundary being pushed on.
    #
    # A future stamp gets its own outcome rather than being quietly clamped into
    # `recent`. It is either a clock problem or a deliberate pin, both of which
    # need somebody to look, and the guard it defeats is the one protecting a
    # running task — so the sweep still declines to act on this pass and says
    # loudly why.
    if before.newest_mtime > stamp:
        return SweepOutcome(
            user_id, user_dir, ACTION_FUTURE_MTIME, before.bytes, before.bytes,
            f"newest mtime is {before.newest_mtime - stamp:.0f}s in the future; "
            "not sweeping on a timestamp this cache's own writer controls",
        )
    idle = stamp - before.newest_mtime
    if idle < min_idle_seconds:
        return SweepOutcome(
            user_id, user_dir, ACTION_RECENT, before.bytes, before.bytes,
            f"written {idle:.0f}s ago, inside the {min_idle_seconds:.0f}s idle window",
        )

    # Pinned once, re-asserted before each round — see `_still_the_same` for
    # why the resolved path this was handed is not by itself enough here.
    pinned = _identity(user_dir)
    if pinned is None:
        return SweepOutcome(
            user_id, user_dir, ACTION_SWAPPED, before.bytes, before.bytes,
            "the cache directory could not be opened without following a symlink",
        )

    ran, missing, notes = _reclaim(
        user_dir, ("prune", "verify"), uv_bin, npm_bin, pinned,
    )
    after = measure_cache(user_dir)
    if after.bytes <= max_bytes:
        return SweepOutcome(
            user_id, user_dir, ACTION_RECLAIMED, before.bytes, after.bytes,
            "; ".join(notes),
        )

    # **Escalate only against an overage a reclaim verb can actually reach.**
    # `XDG_CACHE_HOME` points at the user root and that root is bound
    # read-write into the user's own sandbox, so bytes can sit beside `uv/` and
    # `npm/` in a directory neither `clean` verb touches. Measuring the whole
    # directory and escalating on it means a single large file in a third
    # subdirectory wipes both real caches on every sweep, for good, while the
    # total never comes under the ceiling — every task re-downloading every
    # time, which is precisely the pre-ISSUE-305 behaviour the `MIN_MAX_BYTES`
    # floor exists to prevent, arriving by another road. So the wipe is decided
    # on the reclaimable portion, and an overage outside it is reported instead.
    reclaimable = sum(
        measure_cache(user_dir / name).bytes for name in (CACHE_UV, CACHE_NPM)
    )
    # Wiping can only ever remove `reclaimable`, so it can only ever get the
    # total down to `unreclaimable`. If that alone is already over the ceiling,
    # the wipe cannot succeed and its only effect is to throw away two working
    # caches.
    unreclaimable = after.bytes - reclaimable
    if unreclaimable > max_bytes:
        name, size = _largest_child(user_dir)
        note = (
            f"{_human(unreclaimable)} of this cache is outside {CACHE_UV}/ and "
            f"{CACHE_NPM}/, which neither reclaim verb can touch"
        )
        if name:
            note += f"; largest subdirectory is {name} ({_human(size)})"
        notes.append(note)
        return SweepOutcome(user_id, user_dir, ACTION_STILL_OVER,
                            before.bytes, after.bytes, "; ".join(notes))

    # **The liveness decision is re-taken before the wipe, not carried over.**
    # The prune round can take a long time, and the delay correlates with the
    # hazard rather than being independent of it: uv holds an exclusive lock on
    # the cache for the whole of an install, and `uv cache prune` *blocks* on a
    # held lock rather than refusing, so the round stalls for exactly as long as
    # a task is syncing. Carrying the earlier reading into the escalation would
    # fire the wipe on evidence gathered before that task existed. Same idea as
    # `worktree_reaper` repeating its dirty check immediately before the delete.
    #
    # The busy set is not re-read here; that would mean holding a database
    # handle, which this module does not do. The mtime is what is available, and
    # a sync that stalled the prune round has written into the cache to do it.
    fresh = measure_cache(user_dir)
    if fresh.newest_mtime > before.newest_mtime:
        notes.append(
            "something wrote into this cache during the reclaim; not escalating"
        )
        return SweepOutcome(user_id, user_dir, ACTION_RECENT,
                            before.bytes, fresh.bytes, "; ".join(notes))

    # And the identity, for the same reason the mtime is re-taken: the prune
    # round can stall for minutes on uv's lock, and this is the escalation that
    # deletes rather than reclaims. A directory that changed identity in that
    # window is not the one anything above was decided about.
    if not _still_the_same(user_dir, pinned):
        notes.append(
            "the cache directory changed identity during the reclaim; not escalating"
        )
        return SweepOutcome(user_id, user_dir, ACTION_SWAPPED,
                            before.bytes, fresh.bytes, "; ".join(notes))

    wiped, wipe_missing, wipe_notes = _reclaim(
        user_dir, ("clean", "clean"), uv_bin, npm_bin, pinned,
    )
    notes.extend(n for n in wipe_notes if n not in notes)
    after = measure_cache(user_dir)

    # Nothing ran and something should have: the caches are there and the tool
    # that owns them is not installed. Report that rather than deleting by hand.
    if ran == 0 and wiped == 0 and (missing or wipe_missing):
        return SweepOutcome(
            user_id, user_dir, ACTION_NO_TOOLS, before.bytes, after.bytes,
            "; ".join(notes) or "no package manager available to reclaim this cache",
        )
    if after.bytes <= max_bytes:
        return SweepOutcome(user_id, user_dir, ACTION_WIPED, before.bytes, after.bytes,
                            "; ".join(notes))

    name, size = _largest_child(user_dir)
    if name:
        notes.append(f"largest remaining subdirectory is {name} ({_human(size)})")
    return SweepOutcome(user_id, user_dir, ACTION_STILL_OVER, before.bytes, after.bytes,
                        "; ".join(notes))


def report_orphan_caches(root: Path | str, user_ids: Collection[str]) -> list[Path]:
    """Caches under *root* belonging to nobody the caller named. Reports only.

    The cost of the derived layout's security property, made visible. Because
    the sweep derives one path per supplied user id and reads no name back out
    of the tree, a cache for a user the caller does not name is swept by
    nothing — forever, on the volume ``worktree_reaper`` is already fighting
    for. That happens for two ordinary reasons, neither of them a
    misconfiguration: ``config.users`` is read once at load time, so a user
    onboarded afterwards accumulates cache the running daemon cannot see; and a
    removed or renamed account leaves its cache behind. The one-level layout had
    no such gap because it enumerated the tree, so this is a regression the
    layout change buys and this function is what stops it being a silent one.

    **It never acts and never returns a user id.** It returns paths, logs a
    count, and stops. Acting on a name found here is precisely what
    :func:`_candidates_for_users` exists to prevent — an entry under
    ``repos_dir`` is model-creatable, so ``{repos_dir}/zzz/.package-caches`` is
    a directory a task can make, and a "clean up the orphans" pass would aim a
    reclaim verb at it. Whoever reads the log decides.

    Never raises: an unreadable root is no orphans, which is honest — nothing
    was established either way, and the caller is a periodic sweep.
    """
    known = {str(u) for u in user_ids}
    orphans: list[Path] = []
    for entry in du.first_level_dirs(root):
        try:
            if entry.name in known:
                continue
            if (entry / CACHE_ROOT_NAME).is_dir():
                orphans.append(entry / CACHE_ROOT_NAME)
        except OSError:
            continue
    if orphans:
        logger.warning(
            "sandbox_cache_sweeper: %d package cache(s) under %s belong to no user "
            "this daemon knows about, so nothing bounds them: %s. A user added since "
            "the daemon started is the usual cause — restart to pick them up. Not "
            "swept and not removed: acting on a directory name found here is what "
            "the per-user layout exists to avoid.",
            len(orphans), root, ", ".join(str(o) for o in orphans),
        )
    return orphans


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def sweep_and_report(
    root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    busy_users: Collection[str] = (),
    user_ids: Collection[str] | None = None,
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS,
) -> list[SweepOutcome]:
    """:func:`sweep_caches`, logged.

    A line for each cache that was acted on or could not be, and one summary
    line counting the rest. The skipped set is the number an operator needs to
    see growing — a deployment where every sweep skips the same user is one
    where the ceiling has quietly stopped applying.
    """
    try:
        outcomes = sweep_caches(
            root, max_bytes=max_bytes, busy_users=busy_users, user_ids=user_ids,
            min_idle_seconds=min_idle_seconds,
        )
    except Exception:  # noqa: BLE001 — a periodic sweep must not kill its thread
        logger.exception("sandbox_cache_sweeper: sweep of %s failed", root)
        return []

    if user_ids is not None:
        # Derived layout only: on the one-level layout the sweep enumerates the
        # tree itself, so there is no such thing as a cache it cannot see.
        try:
            report_orphan_caches(root, user_ids)
        except Exception:  # noqa: BLE001 — reporting must not cost the sweep
            logger.exception("sandbox_cache_sweeper: orphan scan of %s failed", root)
        if not outcomes:
            # Otherwise this is a completely silent no-op: `skipped` is empty
            # too, so nothing below logs, and a deployment whose user list is
            # empty reads exactly like one whose caches are all inside their
            # ceiling. That is the failure this whole re-rooting was fixing,
            # arriving through a different door.
            logger.info(
                "sandbox_cache_sweeper: no package cache found under %s for any of "
                "the %d user(s) this daemon knows about.", root, len(set(user_ids)),
            )

    skipped: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.action in (
            ACTION_WIPED, ACTION_STILL_OVER, ACTION_NO_TOOLS, ACTION_FUTURE_MTIME,
            # Both of these mean a directory was not what the layout says it
            # should be, which on the derived layout is the security-interesting
            # event rather than a housekeeping one. Folded into the anonymous
            # `skipped` count they were reported as "1 outside" with no user and
            # no path — a planted symlink and an idle deployment reading alike.
            ACTION_OUTSIDE, ACTION_SWAPPED,
        ):
            level = logger.info if outcome.action == ACTION_WIPED else logger.warning
            level(
                "sandbox_cache_sweeper: %s cache for %s — %s to %s%s",
                outcome.action, outcome.user_id,
                _human(outcome.before_bytes), _human(outcome.after_bytes),
                f" ({outcome.detail})" if outcome.detail else "",
            )
        elif outcome.action == ACTION_RECLAIMED and (
            outcome.after_bytes < outcome.before_bytes or outcome.detail
        ):
            # The `detail` arm matters on its own: a cache under its ceiling
            # with npm missing reclaims nothing and says so, and dropping that
            # line means the operator first hears about it months later when the
            # cache crosses the line.
            logger.info(
                "sandbox_cache_sweeper: reclaimed the cache for %s — %s to %s%s",
                outcome.user_id,
                _human(outcome.before_bytes), _human(outcome.after_bytes),
                f" ({outcome.detail})" if outcome.detail else "",
            )
        else:
            skipped[outcome.action] = skipped.get(outcome.action, 0) + 1

    if skipped:
        logger.info(
            "sandbox_cache_sweeper: took no bytes from %d cache(s): %s.",
            sum(skipped.values()),
            ", ".join(f"{count} {action}" for action, count in sorted(skipped.items())),
        )
    return outcomes
