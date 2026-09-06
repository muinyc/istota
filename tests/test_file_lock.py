"""`file_lock.exclusive_lock`, and the guard that stops a fourth copy of it.

The three copies it replaced — `memory/curation/file_lock`, `money/work` and
`money/core/edit` — each kept their own exception type, which is why
`on_timeout` exists rather than one shared `TimeoutError`. Those types are part
of their packages' surface and every `except` clause in the tree still works.
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from istota.file_lock import exclusive_lock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "istota"


def _hold(lock_path: str, ready, release) -> None:
    """Take the lock in another *process* and hold it until told to stop.

    A separate process, not a thread: `flock` is per open file description,
    and two threads of one process opening the anchor twice get two
    descriptions — so a thread would contend correctly, but a process is what
    the real writers are and is the honest control.
    """
    with exclusive_lock(lock_path, timeout_seconds=10):
        ready.set()
        release.wait(timeout=30)


class TestAcquisition:
    def test_an_uncontended_lock_is_taken_and_released(self, tmp_path):
        anchor = tmp_path / ".work.lock"
        with exclusive_lock(anchor, timeout_seconds=1):
            assert anchor.exists()
        # Released — a second acquisition does not block.
        with exclusive_lock(anchor, timeout_seconds=1):
            pass

    def test_the_anchor_is_left_in_place(self, tmp_path):
        anchor = tmp_path / ".ledger.lock"
        with exclusive_lock(anchor, timeout_seconds=1):
            pass
        assert anchor.exists()

    def test_it_releases_when_the_body_raises(self, tmp_path):
        anchor = tmp_path / ".lock"
        with pytest.raises(RuntimeError):
            with exclusive_lock(anchor, timeout_seconds=1):
                raise RuntimeError("boom")
        with exclusive_lock(anchor, timeout_seconds=1):
            pass


class TestTimeout:
    def test_a_held_lock_times_out_with_the_callers_own_exception(self, tmp_path):
        class WorkStoreLocked(RuntimeError):
            pass

        anchor = tmp_path / ".work.lock"
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30), "holder never acquired the lock"
            started = time.monotonic()
            with pytest.raises(WorkStoreLocked) as excinfo:
                with exclusive_lock(
                    anchor,
                    timeout_seconds=0.3,
                    poll_seconds=0.02,
                    on_timeout=WorkStoreLocked,
                ):
                    pass
            waited = time.monotonic() - started
        finally:
            release.set()
            holder.join(timeout=30)

        assert str(excinfo.value) == str(anchor)
        assert waited >= 0.3

    def test_the_default_is_a_timeout_error(self, tmp_path):
        anchor = tmp_path / ".lock"
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30)
            with pytest.raises(TimeoutError):
                with exclusive_lock(anchor, timeout_seconds=0.2, poll_seconds=0.02):
                    pass
        finally:
            release.set()
            holder.join(timeout=30)

    def test_a_negative_timeout_gives_up_at_once_rather_than_looping(self, tmp_path):
        """The `max(0.0, ...)` clamp, reached only under contention.

        Uncontended, the first `LOCK_NB` succeeds and the deadline is never
        evaluated — so an uncontended version of this test passes with the
        clamp deleted outright.
        """
        anchor = tmp_path / ".lock"
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30)
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                with exclusive_lock(anchor, timeout_seconds=-5, poll_seconds=0.02):
                    pass
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            holder.join(timeout=30)


class TestCallerTypesSurvive:
    """Each migrated caller still raises its own class, unchanged."""

    def test_money_work_raises_workstorelocked(self, tmp_path):
        from istota.money.work import WorkStoreLocked, _work_lock

        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        anchor = tmp_path / "invoices" / "work" / ".work.lock"
        anchor.parent.mkdir(parents=True)
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30)
            with pytest.raises(WorkStoreLocked):
                with _work_lock(tmp_path, timeout_seconds=0.2):
                    pass
        finally:
            release.set()
            holder.join(timeout=30)

    def test_ledger_raises_ledgerlocked(self, tmp_path):
        from istota.money.core.edit import LedgerLocked, _ledger_lock

        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        ledger = tmp_path / "main.beancount"
        anchor = tmp_path / ".ledger.lock"
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30)
            with pytest.raises(LedgerLocked):
                with _ledger_lock(ledger, timeout_seconds=0.2):
                    pass
        finally:
            release.set()
            holder.join(timeout=30)

    def test_memory_md_raises_memorymdlocked(self, tmp_path):
        from istota.memory.curation.file_lock import (
            MemoryMdLocked,
            lock_path_for,
            memory_md_lock,
        )

        target = tmp_path / "USER.md"
        lock_dir = tmp_path / "locks"
        anchor = lock_path_for(target, lock_dir=lock_dir)
        anchor.parent.mkdir(parents=True, exist_ok=True)

        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        holder = ctx.Process(target=_hold, args=(str(anchor), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=30)
            with pytest.raises(MemoryMdLocked):
                with memory_md_lock(target, timeout_seconds=0.2, lock_dir=lock_dir):
                    pass
        finally:
            release.set()
            holder.join(timeout=30)


#: Files that take an `fcntl.LOCK_NB` lock and are deliberately not copies of
#: `exclusive_lock`, because none of them retries: each takes the lock once and
#: acts on the answer immediately.
_SINGLE_SHOT = {
    "db_restore.py",
    "updater.py",
    "scheduler.py",
}


class TestNoSecondCopy:
    """Grep guard. The polling acquisition loop lives in one module.

    The discriminator is `LOCK_NB` *together with* a wait — that pairing is
    what makes a use a copy of this one. Keying on `EWOULDBLOCK` alone, which
    is what the first version of this guard did, does not work:
    `errno.EAGAIN == errno.EWOULDBLOCK` on both supported platforms, so a
    fourth copy written as `if e.errno != errno.EAGAIN: raise` never mentions
    the token and passes silently.

    The blocking users are invisible to this guard by construction and need no
    exemption: `brain_availability`, `location/db`, `health/garmin` and
    `location/garmin_import` take a plain `LOCK_EX` with no deadline, so they
    carry no `LOCK_NB`. The single-shot `LOCK_NB` users do need one, and it is
    `_SINGLE_SHOT` above.
    """

    def test_the_retry_loop_appears_only_in_file_lock(self):
        hits = set()
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "LOCK_NB" not in text:
                continue
            if "time.sleep" not in text and "time.monotonic" not in text:
                continue
            hits.add(str(path.relative_to(SRC)))
        assert hits - _SINGLE_SHOT == {"file_lock.py"}, (
            "a new flock retry loop appeared; call "
            "file_lock.exclusive_lock with an `on_timeout` instead"
        )

    def test_every_single_shot_exemption_still_takes_the_lock(self):
        """A stale exemption is how a guard quietly stops guarding."""
        for rel in _SINGLE_SHOT:
            text = (SRC / rel).read_text(encoding="utf-8")
            assert "LOCK_NB" in text, f"{rel} no longer locks; drop it from _SINGLE_SHOT"
