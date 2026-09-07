"""`du`, and the guard that stops a fifth hand-rolled tree measurement.

The measurement tests are driven against a real tree rather than a stubbed
`os.walk`, because every property that matters here — a hardlink counted once,
a symlink not followed, a sparse file costing what it occupies rather than what
it claims — is a filesystem property and a stub would answer whatever it was
told to.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from istota import du

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "istota"


def _blocks(path: Path) -> int:
    return os.lstat(path).st_blocks * du.BLOCK_SIZE


class TestTreeBytes:
    def test_it_sums_the_files_below_the_root(self, tmp_path):
        (tmp_path / "a").write_bytes(b"x" * 8192)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b").write_bytes(b"y" * 8192)
        expected = _blocks(tmp_path / "a") + _blocks(tmp_path / "sub" / "b")
        assert du.tree_bytes(tmp_path) == expected
        assert expected > 0

    def test_include_dirs_adds_exactly_the_directory_inodes_and_nothing_else(
        self, tmp_path,
    ):
        """The byte axis cannot discriminate this on APFS and can on ext4.

        Measured: a directory on APFS reports ``st_blocks == 0`` however many
        entries it holds, so ``tree_bytes(include_dirs=True)`` and
        ``tree_bytes()`` return the same number on a developer's macOS host and
        different numbers on the Linux deployment where the sweep runs. Asserting
        an equality against ``_blocks(dir)`` here would therefore be ``0 == 0``
        and would pass with ``include_dirs`` ignored entirely — the control
        `K_include_dirs_ignored` is what found that.

        So the *entry set* is what carries the property
        (``TestIterTree::test_include_dirs_yields_directories_too``), and this
        asserts the identity, which is non-vacuous wherever a directory costs
        anything.
        """
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f").write_bytes(b"x" * 8192)
        files_only = du.tree_bytes(tmp_path)
        assert files_only == _blocks(tmp_path / "sub" / "f") > 0
        assert du.tree_bytes(tmp_path, include_dirs=True) == (
            files_only + _blocks(tmp_path / "sub")
        )

    def test_it_measures_blocks_not_apparent_size(self, tmp_path):
        """A sparse file costs what it occupies. `st_size` would say 1 GiB."""
        sparse = tmp_path / "sparse"
        with open(sparse, "wb") as fh:
            fh.truncate(1024 ** 3)
        if os.lstat(sparse).st_blocks * 512 >= os.lstat(sparse).st_size:
            pytest.skip("this filesystem does not do sparse files")
        assert du.tree_bytes(tmp_path) < 1024 ** 3

    def test_dedupe_inodes_counts_a_hardlink_once(self, tmp_path):
        real = tmp_path / "real"
        real.write_bytes(b"z" * 65536)
        os.link(real, tmp_path / "link")
        one = _blocks(real)
        assert du.tree_bytes(tmp_path) == 2 * one
        assert du.tree_bytes(tmp_path, dedupe_inodes=True) == one

    def test_a_symlink_is_not_followed(self, tmp_path):
        outside = tmp_path.parent / "outside_tree"
        outside.mkdir()
        (outside / "big").write_bytes(b"q" * (256 * 1024))
        root = tmp_path / "root"
        root.mkdir()
        (root / "link").symlink_to(outside)
        # The link's own inode, never the 256 KiB behind it.
        assert du.tree_bytes(root, include_dirs=True) < 256 * 1024

    def test_a_missing_root_is_zero_and_does_not_raise(self, tmp_path):
        assert du.tree_bytes(tmp_path / "nope") == 0

    def test_a_null_byte_in_the_root_is_zero_rather_than_a_raise(self, tmp_path):
        """The one way this module could have raised into a never-raises caller.

        An embedded NUL is a `ValueError`, not an `OSError`, and CPython's
        `os.walk` wraps only `OSError` around its own `scandir(top)` — so it
        comes straight out of the generator past the `onerror` handler that is
        supposed to absorb it. `session_log`'s sweep and `doctor`'s checks both
        have hard never-raises contracts resting on this.

        Two calls, because the two functions guard it in different places:
        `iter_tree` around the walk, `first_level_dirs` around `iterdir`.
        """
        bad = str(tmp_path) + "/a\x00b"
        assert du.tree_bytes(bad) == 0
        assert du.first_level_dirs(bad) == []
        assert list(du.iter_tree(bad)) == []

    def test_an_unreadable_directory_is_reported_not_raised(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "f").write_bytes(b"a" * 4096)
        locked.chmod(0o000)
        try:
            seen: list[OSError] = []
            assert du.tree_bytes(tmp_path, on_error=seen.append) == 0
            if os.geteuid() != 0:
                assert len(seen) == 1
                assert isinstance(seen[0], PermissionError)
        finally:
            locked.chmod(0o755)


class TestFirstLevelDirs:
    def test_it_returns_the_immediate_subdirectories_sorted(self, tmp_path):
        for name in ("charlie", "alpha", "bravo"):
            (tmp_path / name).mkdir()
        assert [p.name for p in du.first_level_dirs(tmp_path)] == [
            "alpha", "bravo", "charlie",
        ]

    def test_a_plain_file_is_skipped(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "notes.txt").write_text("x")
        assert [p.name for p in du.first_level_dirs(tmp_path)] == ["a"]

    def test_a_symlinked_directory_is_skipped(self, tmp_path):
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        assert [p.name for p in du.first_level_dirs(tmp_path)] == ["real"]

    def test_it_does_not_descend(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        assert [p.name for p in du.first_level_dirs(tmp_path)] == ["a"]

    def test_a_missing_root_is_empty_and_reported(self, tmp_path):
        seen: list[OSError] = []
        assert du.first_level_dirs(tmp_path / "nope", on_error=seen.append) == []
        assert len(seen) == 1 and isinstance(seen[0], FileNotFoundError)

    def test_an_unreadable_root_is_empty_and_reported(self, tmp_path):
        root = tmp_path / "locked"
        root.mkdir()
        root.chmod(0o000)
        try:
            seen: list[OSError] = []
            assert du.first_level_dirs(root, on_error=seen.append) == []
            if os.geteuid() != 0:
                assert len(seen) == 1 and isinstance(seen[0], PermissionError)
        finally:
            root.chmod(0o755)


class TestIterTree:
    def test_it_yields_paths_and_stat_results(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f").write_text("x")
        got = list(du.iter_tree(tmp_path))
        assert [Path(p).name for p, _ in got] == ["f"]
        assert got[0][1].st_size == 1

    def test_include_dirs_yields_directories_too(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f").write_text("x")
        names = sorted(Path(p).name for p, _ in du.iter_tree(tmp_path, include_dirs=True))
        assert names == ["f", "sub"]


class TestTheConvertedCallersStillAnswerWhatTheyAnsweredBefore:
    """Behaviour equivalence at each converted site, driven end to end."""

    def test_measure_cache_counts_a_hardlinked_wheel_once(self, tmp_path):
        """The property uv's cache needs. Discriminating on any filesystem:
        without dedupe the answer is twice this."""
        from istota.sandbox_cache_sweeper import measure_cache

        real = tmp_path / "wheel"
        real.write_bytes(b"w" * 65536)
        os.link(real, tmp_path / "linked")
        assert _blocks(real) > 0
        assert measure_cache(tmp_path).bytes == _blocks(real)

    def test_measure_cache_counts_directory_inodes(self, tmp_path):
        """Vacuous on APFS (a directory is nought blocks there) and not on the
        Linux host the sweep runs on — see
        `TestTreeBytes::test_include_dirs_adds_exactly_the_directory_inodes_and_nothing_else`.
        The identity is what is asserted, not a number."""
        from istota.sandbox_cache_sweeper import measure_cache

        (tmp_path / "f").write_bytes(b"w" * 8192)
        (tmp_path / "d").mkdir()
        assert measure_cache(tmp_path).bytes == (
            _blocks(tmp_path / "f") + _blocks(tmp_path / "d")
        )

    def test_largest_child_dedupes_hardlinks_the_way_measure_cache_does(self, tmp_path):
        """`_largest_child` is `tree_bytes`'s only caller in the tree, and it
        has to answer what `measure_cache` answers or the `still-over` note
        names the wrong directory."""
        from istota.sandbox_cache_sweeper import _largest_child, measure_cache

        (tmp_path / "linky").mkdir()
        real = tmp_path / "linky" / "wheel"
        real.write_bytes(b"w" * 65536)
        for i in range(4):
            os.link(real, tmp_path / "linky" / f"link{i}")
        (tmp_path / "plain").mkdir()
        (tmp_path / "plain" / "f").write_bytes(b"w" * 131072)

        assert _largest_child(tmp_path) == (
            "plain", measure_cache(tmp_path / "plain").bytes,
        )

    def test_measure_cache_reports_the_newest_mtime_in_the_tree(self, tmp_path):
        from istota.sandbox_cache_sweeper import measure_cache

        (tmp_path / "old").write_text("x")
        os.utime(tmp_path / "old", (1_000_000, 1_000_000))
        (tmp_path / "new").write_text("y")
        os.utime(tmp_path / "new", (2_000_000, 2_000_000))
        os.utime(tmp_path, (500_000, 500_000))
        assert measure_cache(tmp_path).newest_mtime == 2_000_000

    def test_measure_cache_on_a_non_directory_is_zero(self, tmp_path):
        from istota.sandbox_cache_sweeper import measure_cache

        f = tmp_path / "f"
        f.write_text("x")
        assert measure_cache(f) == (0, 0.0)

    def test_largest_child_names_the_biggest_immediate_subdirectory(self, tmp_path):
        from istota.sandbox_cache_sweeper import _largest_child

        (tmp_path / "small").mkdir()
        (tmp_path / "small" / "f").write_bytes(b"a" * 1024)
        (tmp_path / "big").mkdir()
        (tmp_path / "big" / "f").write_bytes(b"a" * (512 * 1024))
        assert _largest_child(tmp_path)[0] == "big"

    def test_largest_child_ignores_a_symlinked_child(self, tmp_path):
        from istota.sandbox_cache_sweeper import _largest_child

        outside = tmp_path.parent / "elsewhere"
        outside.mkdir(exist_ok=True)
        (outside / "f").write_bytes(b"a" * (512 * 1024))
        root = tmp_path / "root"
        root.mkdir()
        (root / "small").mkdir()
        (root / "small" / "f").write_bytes(b"a" * 1024)
        (root / "link").symlink_to(outside)
        assert _largest_child(root)[0] == "small"

    def test_report_orphan_caches_still_filters_on_the_known_users(self, tmp_path, caplog):
        from istota.sandbox_cache_sweeper import CACHE_ROOT_NAME, report_orphan_caches

        for name in ("alice", "mallory"):
            (tmp_path / name / CACHE_ROOT_NAME).mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "mallory")
        orphans = report_orphan_caches(tmp_path, ["alice"])
        assert [p.parent.name for p in orphans] == ["mallory"]

    def test_session_log_user_dirs_skips_a_stray_file_without_counting_it(
        self, tmp_path,
    ):
        """A plain file in the log root is not an unreadable entry.

        Named for what it establishes. It used to be called "counts unreadable
        entries", which the body never drove — the review is what caught that,
        and the branch that name claimed turns out to be unreachable through
        `pathlib` at all (see the test below).
        """
        from istota.session.session_log import _user_dirs

        (tmp_path / "alice").mkdir()
        (tmp_path / "stray.txt").write_text("x")
        found, errors = _user_dirs(tmp_path)
        assert [p.name for p in found] == ["alice"]
        assert errors == 0

    def test_session_log_user_dirs_counts_one_error_per_unreadable_entry(
        self, tmp_path,
    ):
        """The entry-level arm, driven — and it took a control to find the shape.

        A mode-`0000` root fails at `iterdir` and fires the *root* arm, which is
        the case the other test covers. `0444` is what separates them: listing
        needs `r` and stat'ing the entries needs `x`, so `iterdir` succeeds and
        then every `is_symlink` raises `PermissionError` — which `pathlib` does
        not swallow, unlike the ENOENT/ENOTDIR/EBADF/ELOOP set it ignores.

        Two entries, so the assertion is `2` rather than `1`: a per-entry
        counter and a once-per-root counter both give `1` for a single entry,
        and this is meant to distinguish them.
        """
        import os

        from istota.session.session_log import _user_dirs

        if os.geteuid() == 0:
            pytest.skip("root bypasses the permission bits")

        root = tmp_path / "root"
        (root / "alice").mkdir(parents=True)
        (root / "bob").mkdir()
        root.chmod(0o444)
        try:
            found, errors = _user_dirs(root)
            assert found == []
            assert errors == 2
        finally:
            root.chmod(0o755)

    def test_first_level_dirs_reports_each_unreadable_entry(self, tmp_path):
        """The same shape one layer down, on `du`'s own contract."""
        import os

        if os.geteuid() == 0:
            pytest.skip("root bypasses the permission bits")

        root = tmp_path / "root"
        (root / "alice").mkdir(parents=True)
        (root / "bob").mkdir()
        root.chmod(0o444)
        try:
            seen: list[OSError] = []
            assert du.first_level_dirs(root, on_error=seen.append) == []
            assert len(seen) == 2
            assert all(isinstance(e, PermissionError) for e in seen)
            # The root listed fine; it is the entries that could not be stat'ed.
            assert sorted(Path(e.filename).name for e in seen) == ["alice", "bob"]
        finally:
            root.chmod(0o755)

    def test_session_log_user_dirs_reads_a_missing_root_as_no_errors(self, tmp_path):
        from istota.session.session_log import _user_dirs

        assert _user_dirs(tmp_path / "nope") == ([], 0)

    def test_session_log_user_dirs_reports_an_unreadable_root(self, tmp_path):
        from istota.session.session_log import _user_dirs

        root = tmp_path / "locked"
        root.mkdir()
        root.chmod(0o000)
        try:
            found, errors = _user_dirs(root)
            assert found == []
            if os.geteuid() != 0:
                assert errors == 1
        finally:
            root.chmod(0o755)

    def test_doctor_session_log_tree_counts_jsonl_under_user_dirs_only(self, tmp_path):
        from istota.doctor import _session_log_tree

        (tmp_path / "alice").mkdir()
        (tmp_path / "alice" / "a_task-1-1.jsonl").write_bytes(b"{}\n" * 100)
        (tmp_path / "alice" / "notes.txt").write_bytes(b"x" * 100)
        # A file directly in the root belongs to no user, so the sweep's
        # ceiling never sees it and neither does this.
        (tmp_path / "stray.jsonl").write_bytes(b"x" * 4096)
        count, total = _session_log_tree(tmp_path)
        assert count == 1
        expected = (
            _blocks(tmp_path / "alice" / "a_task-1-1.jsonl")
            + _blocks(tmp_path / "alice" / "notes.txt")
        )
        assert total == expected


class TestNoSecondCopy:
    """Grep guard.

    ``.st_blocks`` with the leading attribute dot rather than the bare word: the
    prose in `sandbox_cache_sweeper`, `session_log` and `doctor` still explains
    the measurement, and `briefings/db.list_blocks` contains the bare word as a
    substring. The dot is what makes this a search for the *arithmetic*.
    """

    def test_the_block_arithmetic_lives_only_in_du(self):
        hits = sorted(
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if ".st_blocks" in p.read_text(encoding="utf-8")
        )
        assert hits == ["du.py"], (
            "a hand-rolled du-style measurement appeared; call du.tree_bytes / "
            "du.iter_tree + du.entry_bytes instead"
        )

    def test_no_converted_caller_redeclares_the_block_size(self):
        """Three of the four carried their own `_BLOCK = 512` with an identical
        four-line comment above it. One constant, in `du`."""
        converted = [
            "sandbox_cache_sweeper.py",
            "session/session_log.py",
            "session/session_log_read.py",
            "doctor.py",
        ]
        offenders = [
            rel
            for rel in converted
            if "_BLOCK = 512" in (SRC / rel).read_text(encoding="utf-8")
        ]
        assert offenders == []
