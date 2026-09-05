"""`atomic_write`, and the guard that stops a tenth copy of it appearing.

The concurrent-writer case is the reason this module exists rather than a
tidiness argument: three of the nine call sites it replaced named their staging
file after the process (or after nothing at all), and three of those writers
are threads of one process. Two threads at one path is what the old naming
could not survive.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from istota.atomic_write import atomic_writer, write_bytes_atomic, write_text_atomic

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "istota"


class TestWriteTextAtomic:
    def test_it_writes_the_text(self, tmp_path):
        target = tmp_path / "doc.md"
        write_text_atomic(target, "hello\nworld\n")
        assert target.read_text(encoding="utf-8") == "hello\nworld\n"

    def test_it_replaces_existing_contents(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text("old and much longer than the new one")
        write_text_atomic(target, "new")
        assert target.read_text() == "new"

    def test_the_default_mode_is_0644(self, tmp_path):
        target = tmp_path / "doc.md"
        write_text_atomic(target, "x")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_the_mode_is_honoured(self, tmp_path):
        target = tmp_path / "secret.json"
        write_text_atomic(target, "{}", mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_the_mode_lands_before_the_file_has_its_final_name(self, tmp_path):
        """`mkstemp` is 0600; the target must never be observable at that mode.

        Asserted through the descriptor rather than by racing: the mode is
        applied inside the context, so a handle opened there already sees it.
        """
        target = tmp_path / "doc.md"
        seen: list[int] = []
        with atomic_writer(target, mode=0o644) as handle:
            seen.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
            handle.write(b"x")
        assert seen == [0o644]

    def test_a_non_utf8_encoding_is_honoured(self, tmp_path):
        target = tmp_path / "doc.txt"
        write_text_atomic(target, "café", encoding="latin-1")
        assert target.read_bytes() == "café".encode("latin-1")

    def test_fsync_does_not_change_what_is_written(self, tmp_path):
        target = tmp_path / "doc.json"
        write_text_atomic(target, '{"a":1}', fsync=True)
        assert target.read_text() == '{"a":1}'


class TestWriteBytesAtomic:
    def test_it_writes_the_bytes(self, tmp_path):
        target = tmp_path / "scan.pdf"
        write_bytes_atomic(target, b"%PDF-1.4\x00\xff")
        assert target.read_bytes() == b"%PDF-1.4\x00\xff"


class TestFailure:
    def test_a_raise_inside_the_context_leaves_no_staging_file(self, tmp_path):
        target = tmp_path / "doc.md"
        with pytest.raises(RuntimeError):
            with atomic_writer(target):
                raise RuntimeError("boom")
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_baseexception_also_cleans_up(self, tmp_path):
        """`KeyboardInterrupt` is not an `Exception`, and it is the realistic one.

        An operator's Ctrl-C between the write and the replace must not leave a
        dot-file in a directory the user reads over Nextcloud.
        """
        target = tmp_path / "doc.md"
        with pytest.raises(KeyboardInterrupt):
            with atomic_writer(target) as handle:
                handle.write(b"partial")
                raise KeyboardInterrupt
        assert list(tmp_path.iterdir()) == []

    def test_a_failing_replace_leaves_no_staging_file(self, tmp_path, monkeypatch):
        target = tmp_path / "doc.md"

        def boom(src, dst):
            raise OSError("EXDEV")

        monkeypatch.setattr("istota.atomic_write.os.replace", boom)
        with pytest.raises(OSError):
            write_text_atomic(target, "x")
        assert list(tmp_path.iterdir()) == []

    def test_a_missing_parent_raises_and_creates_nothing(self, tmp_path):
        target = tmp_path / "nope" / "doc.md"
        with pytest.raises(OSError):
            write_text_atomic(target, "x")
        assert not (tmp_path / "nope").exists()

    def test_a_read_only_parent_raises_before_the_target_is_touched(self, tmp_path):
        """The property `os.replace` has and truncate-in-place does not.

        A write that cannot proceed must leave the previous contents intact
        rather than emptying the file and then failing.
        """
        if os.geteuid() == 0:
            pytest.skip("root bypasses the write bit")
        target = tmp_path / "doc.md"
        target.write_text("previous")
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                write_text_atomic(target, "replacement")
            assert target.read_text() == "previous"
        finally:
            tmp_path.chmod(0o755)


class TestConcurrentWriters:
    """Two threads of one process, one target path.

    This is the case `money/core/edit.py`, `money/work.py` and
    `health/documents.py` could not survive: a staging name derived from the
    pid (or fixed outright) is the *same* path for both threads, so one
    truncates the other's half-written file and the loser's `os.replace` hits a
    file that is not what it wrote — or is gone.
    """

    def test_two_threads_at_one_path_both_succeed_and_neither_tears(self, tmp_path):
        target = tmp_path / "ledger.beancount"
        a = "A" * 200_000
        b = "B" * 200_000
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def writer(text: str) -> None:
            try:
                start.wait(timeout=10)
                for _ in range(25):
                    write_text_atomic(target, text)
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(a,)),
            threading.Thread(target=writer, args=(b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        # One writer won the last `os.replace`; whichever it was, the published
        # file is exactly what that writer wrote and not a mixture.
        assert target.read_text() in (a, b)
        # And nothing was left staged.
        assert [p.name for p in tmp_path.iterdir()] == ["ledger.beancount"]

    def test_readers_never_observe_a_partial_file(self, tmp_path):
        """A concurrent reader sees the old file or the new one, never a mixture.

        Compared on bytes rather than on length, and the reader records any
        exception rather than swallowing `OSError`: a torn read of UTF-8 raises
        `UnicodeDecodeError`, which is a `ValueError`, so an `except OSError`
        would let the reader thread die and leave the assertion passing on the
        readings it had already taken.
        """
        target = tmp_path / "CHANNEL.md"
        old = ("old\n" * 50_000).encode()
        new = ("new\n" * 60_000).encode()
        target.write_bytes(old)
        observed: set[bytes] = set()
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    observed.add(target.read_bytes())
                except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                    errors.append(exc)
                    return

        r = threading.Thread(target=reader)
        r.start()
        try:
            for _ in range(50):
                write_text_atomic(target, new.decode())
                write_text_atomic(target, old.decode())
        finally:
            stop.set()
            r.join(timeout=10)

        assert errors == []
        assert observed
        assert observed <= {old, new}


class TestTheStagingName:
    def test_it_is_dot_prefixed_and_derived_from_the_target(self, tmp_path):
        """`skills/developer._remove_shims` skips dot files, which is what stops
        one writer's sweep deleting another's in-flight staging file."""
        target = tmp_path / "glab"
        with atomic_writer(target):
            staged = [p.name for p in tmp_path.iterdir()]
        assert len(staged) == 1
        assert staged[0].startswith(".glab.")

    def test_it_differs_between_two_open_writers(self, tmp_path):
        target = tmp_path / "doc.md"
        with atomic_writer(target):
            with atomic_writer(target):
                names = sorted(p.name for p in tmp_path.iterdir())
        assert len(names) == 2
        assert names[0] != names[1]


class TestNoSecondCopy:
    """Grep guard. Nine hand-rolled writers were replaced; a tenth must not appear.

    `os.replace` is the discriminator rather than `mkstemp`, because
    `session/tools/bash.py` legitimately uses `mkstemp` for a scratch file it
    never renames, and the publishing rename is what makes a writer a *copy* of
    this module rather than an unrelated use of the temp API.
    """

    def test_os_replace_appears_only_in_atomic_write(self):
        hits = sorted(
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if "os.replace(" in p.read_text(encoding="utf-8")
        )
        assert hits == ["atomic_write.py"], (
            "a new temp-file-then-rename writer appeared; call "
            "atomic_write.write_text_atomic / write_bytes_atomic instead"
        )
