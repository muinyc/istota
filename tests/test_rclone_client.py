"""The rclone API: one runner, and a guard against a third copy of it.

``storage.py`` and ``skills/files/__init__.py`` each carried ``_rclone_run``
and its wrappers, with docstrings on both sides naming the other module. The
``FileNotFoundError`` defect they describe had to be fixed twice.

Asserting that both modules' entry points are the *same object* would be true
by construction, so each assertion below goes through a call site and pins the
**argv** it produces against what the pre-consolidation tests pinned.

One thing there is not what it looks like, and the negative control is what
said so: patching ``istota.rclone_client.subprocess.run`` does **not**
distinguish a shared runner from a reintroduced local one, because
``istota.storage.subprocess`` is the same module object and the patch reaches
both. ``TestTheEntryPointsRouteThroughTheLeafsOwnRunner`` patches
``rclone_client.rclone_run`` instead, which a local copy never calls, and
``TestNoThirdCopy`` scans the source. Between them, reintroducing the copy in
``storage.py`` turns two tests red; with only the ``subprocess`` patches it
turned one.
"""

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from istota import rclone_client, storage
from istota.skills import files as skill_files


def _completed(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestBothModulesReachTheOneRunner:
    """Patch only the leaf. A local copy would slip past the patch."""

    @patch("istota.rclone_client.subprocess.run")
    def test_storage_mkdir_argv(self, run):
        run.return_value = _completed()
        assert storage._rclone_mkdir("nc", "/Users/alice/inbox") is True
        run.assert_called_once_with(
            ["rclone", "mkdir", "nc:/Users/alice/inbox"],
            capture_output=True,
            text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_skill_mkdir_argv(self, run):
        run.return_value = _completed()
        assert skill_files.rclone_mkdir("nc", "/Users/alice/inbox") is True
        run.assert_called_once_with(
            ["rclone", "mkdir", "nc:/Users/alice/inbox"],
            capture_output=True,
            text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_skill_path_exists_argv(self, run):
        run.return_value = _completed()
        assert skill_files.rclone_path_exists("nc", "/Users/alice/inbox") is True
        run.assert_called_once_with(
            ["rclone", "lsjson", "nc:/Users/alice/inbox"],
            capture_output=True,
            text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_the_skills_raising_helpers_still_reach_it(self, run):
        """`rclone_list` and friends stayed in the skill — they were never
        duplicated — but they are built on the shared runner now."""
        run.return_value = _completed(stdout="[]")
        assert skill_files.rclone_list("nc", "/Users/alice") == []
        run.assert_called_once_with(
            ["rclone", "lsjson", "nc:/Users/alice"],
            capture_output=True,
            text=True,
        )


class TestTheEntryPointsRouteThroughTheLeafsOwnRunner:
    """Patching ``subprocess.run`` is not enough to prove this, and that is
    worth knowing before trusting the class above: ``istota.storage.subprocess``
    and ``istota.rclone_client.subprocess`` are the *same module object*, so
    ``patch("istota.rclone_client.subprocess.run")`` intercepts a reintroduced
    local copy just as happily. Patching ``rclone_client.rclone_run`` does not
    — a copy resolves its own name and never reaches this."""

    @patch("istota.rclone_client.rclone_run")
    def test_storage_mkdir(self, run):
        run.return_value = _completed()
        assert storage._rclone_mkdir("nc", "/x") is True
        run.assert_called_once_with(["rclone", "mkdir", "nc:/x"])

    @patch("istota.rclone_client.rclone_run")
    def test_storage_cat(self, run):
        run.return_value = _completed(stdout="body")
        assert storage._rclone_cat("nc", "/x") == "body"
        run.assert_called_once_with(["rclone", "cat", "nc:/x"])

    @patch("istota.rclone_client.rclone_run")
    def test_storage_rcat(self, run):
        run.return_value = _completed()
        assert storage._rclone_rcat("nc", "/x", "content") is True
        run.assert_called_once_with(["rclone", "rcat", "nc:/x"], input="content")

    @patch("istota.rclone_client.rclone_run")
    def test_storage_path_exists(self, run):
        run.return_value = _completed()
        assert storage._rclone_path_exists("nc", "/x") is True
        run.assert_called_once_with(["rclone", "lsjson", "nc:/x"])

    @patch("istota.rclone_client.rclone_run")
    def test_skill_mkdir(self, run):
        run.return_value = _completed()
        assert skill_files.rclone_mkdir("nc", "/x") is True
        run.assert_called_once_with(["rclone", "mkdir", "nc:/x"])

    @patch("istota.rclone_client.rclone_run")
    def test_skill_path_exists(self, run):
        run.return_value = _completed()
        assert skill_files.rclone_path_exists("nc", "/x") is True
        run.assert_called_once_with(["rclone", "lsjson", "nc:/x"])


class TestTheMissingBinaryStaysAFailureNotARaise:
    """The defect both copies documented, asserted once through each module."""

    @patch("istota.rclone_client.subprocess.run", side_effect=FileNotFoundError("rclone"))
    def test_storage_helpers(self, run):
        assert storage._rclone_cat("nc", "/x") is None
        assert storage._rclone_path_exists("nc", "/x") is False
        assert storage._rclone_mkdir("nc", "/x") is False
        assert storage._rclone_rcat("nc", "/x", "content") is False

    @patch("istota.rclone_client.subprocess.run", side_effect=FileNotFoundError("rclone"))
    def test_skill_helpers(self, run):
        assert skill_files.rclone_mkdir("nc", "/x") is False
        assert skill_files.rclone_path_exists("nc", "/x") is False
        assert skill_files.rclone_move("nc", "/a", "/b") is False

    @patch("istota.rclone_client.subprocess.run", side_effect=FileNotFoundError("rclone"))
    def test_the_skills_raising_helpers_raise_runtime_error_not_oserror(self, run):
        """Their callers handle ``RuntimeError`` and not ``FileNotFoundError``."""
        with pytest.raises(RuntimeError, match="not installed"):
            skill_files.rclone_read_text("nc", "/x")

    @patch("istota.rclone_client.subprocess.run", side_effect=PermissionError("denied"))
    def test_any_oserror_counts_not_only_a_missing_file(self, run):
        assert rclone_client.rclone_mkdir("nc", "/x") is False


class TestNoThirdCopy:
    """The two modules that had one must not grow another. A source scan
    rather than an identity check, because the identity check is what a
    reintroduced copy would keep passing under a different name."""

    CONVERTED = ("storage.py", "skills/files/__init__.py")

    def test_neither_converted_module_calls_subprocess_run(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders = [
            name for name in self.CONVERTED
            if "subprocess.run(" in (root / name).read_text()
        ]
        assert offenders == [], (
            "a module converted onto istota.rclone_client has grown its own "
            f"subprocess runner back: {offenders}"
        )

    def test_the_leaf_is_the_one_that_does(self):
        """Without this the guard above would stay green if the leaf were
        deleted and every caller shelled out some other way."""
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "istota"
        assert "subprocess.run(" in (root / "rclone_client.py").read_text()

    def test_the_leaf_imports_nothing_from_the_package(self):
        """``skills/files`` runs in a subprocess; a leaf is what lets it share
        this without pulling in ``istota.storage``."""
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src" / "istota" / "rclone_client.py"
        ).read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "istota" not in stripped, stripped
                assert not stripped.startswith("from ."), stripped
