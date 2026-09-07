"""The guard that refuses a release cut with no announcement in front of it.

`scripts/release.sh` requires the `[Unreleased]` section to open with prose —
everything before the first `### ` heading — because that is what both
extractors emit ahead of the changeset, in the tag annotation and in the
GitHub Release body. See `.claude/rules/releases.md`.

The guard is a python heredoc inside the shell script, and it is extracted
from the shipped file rather than restated here: a copy of it in this module
would be the thing that drifts, and the guard's whole job is to be running on
the day somebody forgets. `scripts/release.sh` itself is not run — it pushes
tags — so what is exercised is the check, against a CHANGELOG in a tmp dir.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO / "scripts" / "release.sh"

CANONICAL_SECTIONS = "### Added\n\n- Something a person can see.\n"


def _guard_source() -> str:
    text = RELEASE_SH.read_text()
    m = re.search(r"<<'ANNOUNCEMENT_CHECK'\n(.*?)\nANNOUNCEMENT_CHECK\n", text, re.S)
    assert m, "the announcement guard is no longer a heredoc in scripts/release.sh"
    return m.group(1)


def _run(tmp_path: Path, changelog: str) -> subprocess.CompletedProcess:
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return subprocess.run(
        [sys.executable, "-c", _guard_source()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def _changelog(unreleased_body: str) -> str:
    return (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        f"{unreleased_body}"
        "## [0.40.1] - 2026-08-12\n\n"
        "### Fixed\n\n- An older release, which the guard must not read.\n"
    )


ANNOUNCEMENT = (
    "This release is mostly one thing: the boundary the deployment reported as "
    "working was not working, on the shape that ships. It also carries the "
    "offline web chat work, and a bell that collects everything waiting on you.\n\n"
    "**Before you upgrade.** Reissue your forge tokens; the CLIs need wider scopes "
    "than the wrappers they replace.\n\n"
)


class TestTheGuardAcceptsAnAnnouncement:
    def test_prose_before_the_first_subsection_passes(self, tmp_path):
        r = _run(tmp_path, _changelog(ANNOUNCEMENT + CANONICAL_SECTIONS))
        assert r.returncode == 0, r.stderr

    def test_the_repository_s_own_changelog_passes(self, tmp_path):
        r = subprocess.run(
            [sys.executable, "-c", _guard_source()],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr


class TestTheGuardRefuses:
    def test_a_section_that_goes_straight_into_the_changeset(self, tmp_path):
        r = _run(tmp_path, _changelog(CANONICAL_SECTIONS))
        assert r.returncode != 0
        assert "no release announcement" in r.stderr

    def test_a_placeholder_too_short_to_be_an_announcement(self, tmp_path):
        r = _run(tmp_path, _changelog("Bug fixes.\n\n" + CANONICAL_SECTIONS))
        assert r.returncode != 0
        assert "no release announcement" in r.stderr

    def test_a_third_level_heading_inside_the_announcement(self, tmp_path):
        # Reordered to the bottom of the release notes by both extractors,
        # which is the announcement arriving after what it introduces.
        body = ANNOUNCEMENT + "### Highlights\n\nMore prose.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "Highlights" in r.stderr

    def test_a_top_level_heading_inside_the_announcement(self, tmp_path):
        body = ANNOUNCEMENT + "## Security\n\nMore prose.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "## Security" in r.stderr

    def test_a_misspelled_changeset_subsection(self, tmp_path):
        # Same mechanism as a stray heading: not a canonical bucket, so it is
        # emitted after `### Security` rather than where it was written.
        body = ANNOUNCEMENT + "### Fixes\n\n- Something.\n\n" + CANONICAL_SECTIONS
        r = _run(tmp_path, _changelog(body))
        assert r.returncode != 0
        assert "Fixes" in r.stderr


class TestTheGuardReadsTheRightSection:
    def test_an_announcement_on_the_previous_release_does_not_count(self, tmp_path):
        changelog = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            f"{CANONICAL_SECTIONS}\n"
            "## [0.40.1] - 2026-08-12\n\n"
            f"{ANNOUNCEMENT}"
            "### Fixed\n\n- Something.\n"
        )
        r = _run(tmp_path, changelog)
        assert r.returncode != 0
        assert "no release announcement" in r.stderr


class TestTheEscapeHatch:
    def test_the_script_takes_no_announcement_and_a_version_in_either_order(self):
        text = RELEASE_SH.read_text()
        assert "--no-announcement" in text
        assert 'REQUIRE_ANNOUNCEMENT=0' in text

    @pytest.mark.parametrize("argv", [["0.9.0"], ["--no-announcement", "0.9.0"]])
    def test_the_argument_loop_finds_the_version(self, tmp_path, argv):
        loop = re.search(
            r"REQUIRE_ANNOUNCEMENT=1\n(.*?)\n: \"\$\{NEW:\?", RELEASE_SH.read_text(), re.S
        )
        assert loop, "the argument loop in scripts/release.sh has moved"
        script = "REQUIRE_ANNOUNCEMENT=1\n" + loop.group(1) + '\necho "$NEW $REQUIRE_ANNOUNCEMENT"'
        r = subprocess.run(
            ["bash", "-c", script, "release.sh", *argv],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        version, required = r.stdout.split()
        assert version == "0.9.0"
        assert required == ("0" if "--no-announcement" in argv else "1")
