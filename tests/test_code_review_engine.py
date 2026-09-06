"""Tests for the code_review engine — everything the review does without a model.

The engine assembles a reviewer's whole view of a change from a worktree the
sandboxed model named. Two properties are load-bearing and neither is obvious
from reading the happy path:

**The git invocations are hardened, and the tests prove the attack first.**
`DEVELOPER_REPOS_DIR` is bound read-write into the admin sandbox, so a worktree
that passes `resolve_under_repos` cleanly can still carry a repository whose
*configuration* the model wrote. Three escapes were demonstrated against such a
path: a repo-local `diff.external` runs a command as the daemon user (the user
holding the forge tokens), a plain directory sends git searching upward past the
root, and a `.git` file containing `gitdir:` redirects the repository out of the
root while `rev-parse --show-toplevel` still reports the contained path. Each
regression test here builds the attack, asserts it is live against a plain git
invocation, and only then asserts the engine refuses it. A hardening test that
never demonstrates the hole passes just as happily against no hardening at all.

**Content comes out of the object store, never off the filesystem.** A symlink
planted in a worktree makes `(worktree / path).read_text()` read straight out of
the root with no race needed, and git lists such a path in `--name-only` quite
happily. `git show <rev>:<path>` returns the link *text*, so the class does not
arise — pinned by `test_a_symlink_yields_its_target_text_not_the_file`.

Fixtures shell out to real git, because the hardening under test is git's own
behaviour and a hand-built `.git` directory would not exercise it. They pin
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` so the developer's own git
configuration cannot change what a fixture repository does.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from istota.skills.code_review import engine
from istota.skills.code_review.engine import (
    Caps,
    Finding,
    ReviewConfig,
    ReviewError,
    assemble_context,
    build_prompt,
    changed_symbols,
    collect_callers,
    collect_conventions,
    collect_diff,
    collect_file_bodies,
    collect_needed_files,
    git_dir,
    merge_findings,
    parse_findings,
    resolve_range,
    size_review,
)

# Enough identity to commit, and enough isolation that the developer's own
# ~/.gitconfig cannot decide what a fixture repository does.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def commit(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repos_root(tmp_path, monkeypatch) -> Path:
    """A `DEVELOPER_REPOS_DIR` with nothing in it yet.

    The variable is one user's own subtree of `developer.repos_dir`, which is
    what `setup_env` derives and what `build_bwrap_cmd` binds, so
    `developer_repos_root` requires it to be named for `ISTOTA_USER_ID`.
    """
    root = tmp_path / "repos" / "alice"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    return root.resolve()


@pytest.fixture
def repo(repos_root) -> Path:
    """A repository on `main` with one base commit, inside the repos root."""
    wt = repos_root / "proj"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "AGENTS.md").write_text("# Project rules\n\nSpaces, never tabs.\n")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    (wt / "caller.py").write_text("from app import existing\n\nprint(existing())\n")
    commit(wt, "base")
    return wt


def branch_with_change(repo: Path, *, name: str = "feature") -> Path:
    """A `feature` branch adding one Python function. HEAD ends on it."""
    run_git(repo, "checkout", "-q", "-b", name)
    (repo / "app.py").write_text(
        "def existing():\n    return 1\n\n\ndef added_helper(value):\n    return value * 2\n"
    )
    commit(repo, "app: add a helper")
    return repo


class TestResolveRange:
    def test_an_explicit_range_wins_over_a_base(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo, base="main", explicit="HEAD~1..HEAD") == "HEAD~1..HEAD"

    def test_a_base_produces_the_three_dot_form(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo, base="main") == "main...HEAD"

    def test_neither_falls_back_to_the_tracked_default_branch(self, repo):
        branch_with_change(repo)
        assert resolve_range(repo) == "main...HEAD"

    def test_a_bad_ref_raises_with_the_git_stderr_attached(self, repo):
        branch_with_change(repo)
        with pytest.raises(ReviewError) as excinfo:
            resolve_range(repo, base="no-such-ref")
        assert "no-such-ref" in str(excinfo.value)
        # Something only git says, so the test cannot pass against an
        # implementation that echoes the command and drops stderr.
        assert "bad revision" in str(excinfo.value)
        assert excinfo.value.reason == "bad_range"

    def test_a_dangling_origin_head_falls_through_to_a_local_branch(self, repo):
        """Ordinary after the upstream default branch is renamed."""
        branch_with_change(repo)
        run_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")

        assert resolve_range(repo) == "main...HEAD"

    def test_an_option_shaped_range_is_refused_before_git_sees_it(self, repo, tmp_path):
        """A range is a bare argv element, so an option is what git reads.

        `--output=<path>` is an arbitrary daemon-side write and `--ext-diff`
        turns the attribute diff driver back on. Both exit 0, so nothing
        downstream reports a problem. Leaving this to the validating command is
        not a boundary either: the option sets differ per subcommand, so a
        spelling `rev-list` rejects can still be one `diff` accepts.
        """
        branch_with_change(repo)
        target = tmp_path / "written_by_git"

        # Positive control: git really does honour it, and really does write.
        subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", f"--output={target}", "--"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert target.exists(), "fixture is wrong: --output did not write"
        target.unlink()

        for bad in (f"--output={target}", "--ext-diff", "--textconv", "--stdin", "--all"):
            with pytest.raises(ReviewError) as excinfo:
                resolve_range(repo, explicit=bad)
            assert excinfo.value.reason == "bad_range"
            with pytest.raises(ReviewError):
                collect_diff(repo, bad, 200_000)
        assert not target.exists()

    def test_an_option_shaped_base_is_refused(self, repo):
        branch_with_change(repo)
        with pytest.raises(ReviewError) as excinfo:
            resolve_range(repo, base="--ext-diff")
        assert excinfo.value.reason == "bad_range"

    def test_a_git_command_cannot_read_the_daemons_stdin(self, repo):
        """`rev-list --stdin` would otherwise block on an inherited stdin.

        Hashing empty input rather than hanging is the proof that stdin is
        closed; a test that actually hangs proves the same thing far too slowly.
        """
        from istota.skills.code_review.engine import _git

        # The hash of the empty blob. Reached only if stdin gave EOF at once.
        assert _git(repo, ["hash-object", "--stdin"]).strip() == (
            "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
        )

    def test_a_base_only_commit_is_not_attributed_to_the_branch(self, repo):
        """The regression that motivated the three-dot form.

        Two-dot `main..HEAD` means `git diff main HEAD`, so the moment `main`
        moves ahead of the branch point every base-only commit shows up
        inverted — as a deletion the branch never made. Reviewers then file
        findings about code that is not in the change, which costs the driving
        model a round of chasing them.
        """
        branch_with_change(repo)
        run_git(repo, "checkout", "-q", "main")
        (repo / "base_only.py").write_text("BASE_ONLY_SENTINEL = 1\n")
        commit(repo, "base: unrelated work")
        run_git(repo, "checkout", "-q", "feature")

        bundle = collect_diff(repo, resolve_range(repo, base="main"), 200_000)

        assert "BASE_ONLY_SENTINEL" not in bundle.body
        assert "base_only.py" not in bundle.files
        assert "app.py" in bundle.files


class TestGitHardening:
    """The three escapes demonstrated against a cleanly contained worktree."""

    def test_a_repo_local_diff_external_does_not_execute(self, repo, tmp_path):
        branch_with_change(repo)
        sentinel = tmp_path / "sentinel"
        script = tmp_path / "ext.sh"
        script.write_text(f"#!/bin/sh\necho pwned > {sentinel}\necho 'fake diff'\n")
        script.chmod(0o755)
        run_git(repo, "config", "diff.external", str(script))

        # Positive control: the attack is live. Without this the hardening
        # assertion below would pass against an engine that hardens nothing.
        subprocess.run(
            ["git", "diff", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: diff.external never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert not sentinel.exists()
        assert "fake diff" not in bundle.body
        assert "added_helper" in bundle.body

    def test_a_plain_directory_does_not_pick_up_a_repository_above_the_root(
        self, tmp_path, monkeypatch
    ):
        outer = tmp_path / "outer"
        root = outer / "repos" / "alice"
        plain = root / "plain"
        plain.mkdir(parents=True)
        run_git(outer, "init", "-q", "-b", "main", ".")
        (outer / "outer_secret.py").write_text("OUTER_SENTINEL = 1\n")
        commit(outer, "outer")
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        # Positive control: git really does search upward out of the root.
        found = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(plain),
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert found.returncode == 0
        assert str(outer.resolve()) in found.stdout

        with pytest.raises(ReviewError) as excinfo:
            git_dir(plain)
        assert excinfo.value.reason == "not_a_repository"

    def test_a_gitdir_redirect_out_of_the_root_is_refused(self, repos_root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "outside_secret.py").write_text("OUTSIDE_SENTINEL = 1\n")
        commit(outside, "outside")

        wt = repos_root / "redirected"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {outside / '.git'}\n")

        # Positive control: the obvious hardening does not catch this. The
        # worktree still reports itself as the contained path, so a check
        # built on --show-toplevel would approve it.
        toplevel = run_git(wt, "rev-parse", "--show-toplevel").strip()
        assert Path(toplevel).resolve() == wt.resolve()

        with pytest.raises(ReviewError) as excinfo:
            git_dir(wt)
        assert excinfo.value.reason == "git_dir_not_allowed"

    def test_a_commondir_redirect_out_of_the_root_is_refused(self, repos_root, tmp_path):
        """The `gitdir:` escape's second spelling, and the one that looks legitimate.

        A linked worktree's git dir is a small directory holding `HEAD`,
        `gitdir` and `commondir`, and `commondir` names the *real* repository —
        objects, refs and config all live there. The model can create such a
        directory inside the root and point `commondir` outside it. Then
        `--absolute-git-dir` reports a contained path and the obvious check
        passes, while every read comes from a repository the operator never put
        in the root, under a config file the model wrote.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "outside_secret.py").write_text("OUTSIDE_SENTINEL = 1\n")
        commit(outside, "outside")

        main_repo = repos_root / "proj"
        main_repo.mkdir()
        run_git(main_repo, "init", "-q", "-b", "main", ".")
        (main_repo / "f").write_text("x\n")
        commit(main_repo, "base")

        evil = main_repo / ".git" / "worktrees" / "evil"
        evil.mkdir(parents=True)
        (evil / "commondir").write_text(f"{outside / '.git'}\n")
        (evil / "gitdir").write_text(f"{repos_root / 'wt' / '.git'}\n")
        (evil / "HEAD").write_text("ref: refs/heads/main\n")
        wt = repos_root / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {evil}\n")

        # Positive control: the contained-looking answer really is contained,
        # and the outside repository really is readable through it. Without
        # this the assertion below would pass against a check that refused for
        # some unrelated reason.
        reported = run_git(wt, "rev-parse", "--absolute-git-dir").strip()
        assert Path(reported).resolve() == evil.resolve()
        assert str(repos_root) in reported
        assert "OUTSIDE_SENTINEL" in run_git(wt, "show", "main:outside_secret.py")

        with pytest.raises(ReviewError) as excinfo:
            git_dir(wt)
        assert excinfo.value.reason == "common_dir_not_allowed"

    def test_log_show_signature_does_not_run_a_repo_local_gpg_program(self, repo, tmp_path):
        """`git log` is a content command too, and it had none of the flags.

        `log.showSignature` is a plain repo-local boolean and `gpg.program` a
        plain repo-local path, so a `git log` over a signed commit runs a
        chosen command as the daemon user — past `-c diff.external=` and
        `--no-ext-diff`, neither of which has anything to do with signatures.
        """
        sentinel = tmp_path / "gpg_sentinel"
        fake_gpg = tmp_path / "gpg.sh"
        fake_gpg.write_text(f"#!/bin/sh\necho pwned > {sentinel}\nexit 0\n")
        fake_gpg.chmod(0o755)

        # A commit object carrying a gpgsig header, built by hand: making a
        # real `commit -S` succeed needs a program that speaks gpg's status
        # protocol, and the header is all `--show-signature` needs to bite.
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "ordinary")
        tree = run_git(repo, "rev-parse", "HEAD^{tree}").strip()
        parent = run_git(repo, "rev-parse", "HEAD").strip()
        raw = (
            f"tree {tree}\n"
            f"parent {parent}\n"
            "author Test <test@example.invalid> 1700000000 +0000\n"
            "committer Test <test@example.invalid> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n"
            " \n"
            " ZmFrZQ==\n"
            " -----END PGP SIGNATURE-----\n"
            "\n"
            "signed commit\n"
        )
        proc = subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=str(repo),
            input=raw,
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert proc.returncode == 0, proc.stderr
        run_git(repo, "update-ref", "refs/heads/feature", proc.stdout.strip())
        run_git(repo, "config", "log.showSignature", "true")
        run_git(repo, "config", "gpg.program", str(fake_gpg))

        # Positive control: the attack is live against a log invocation that
        # carries the diff hardening but nothing about signatures.
        subprocess.run(
            ["git", "-c", "diff.external=", "log", "--format=%s", "--no-ext-diff", "main..HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: gpg.program never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)
        context = assemble_context(repo, bundle, ReviewConfig())

        assert not sentinel.exists()
        assert "signed commit" in context

    def test_forced_colour_does_not_silently_empty_the_diff(self, repo):
        """`color.ui = always` is not execution, and is just as load-bearing.

        With colour forced on, every diff header arrives wrapped in ANSI
        escapes, the section splitter matches none of them, and the reviewer is
        handed an empty diff — with `truncated` still False and nothing
        anywhere reporting a loss. A review of nothing that says it reviewed
        something is the worst output this module could produce.
        """
        branch_with_change(repo)
        run_git(repo, "config", "color.ui", "always")

        # Positive control: colour really is forced for a plain invocation.
        plain = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert "\x1b[" in plain.stdout

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert "\x1b[" not in bundle.body
        assert "\x1b[" not in bundle.stat
        assert "def added_helper" in bundle.body
        assert bundle.files == ["app.py"]

    def test_an_attribute_driven_textconv_does_not_execute(self, repo, tmp_path):
        """The second route to a command, which the `-c` overrides do not cover.

        `-c diff.external=` clears the global external driver and does nothing
        about a `.gitattributes` line naming a driver plus a `[diff "name"]
        textconv=` entry. Only `--no-textconv` closes that.
        """
        sentinel = tmp_path / "textconv_sentinel"
        script = tmp_path / "tc.sh"
        script.write_text(f"#!/bin/sh\necho pwned > {sentinel}\necho converted\n")
        script.chmod(0o755)
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / ".gitattributes").write_text("*.py diff=evil\n")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "attribute driver")
        run_git(repo, "config", "diff.evil.textconv", str(script))

        # Positive control: the attribute driver fires when the flag is absent.
        # (Not run with `-c diff.external=` — an empty external command is one
        # git tries to execute, so it dies on the first file and proves
        # nothing. That is itself why `--no-ext-diff` carries the weight here.)
        subprocess.run(
            ["git", "diff", "--textconv", "main...HEAD"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: the textconv driver never fired"
        sentinel.unlink()

        bundle = collect_diff(repo, "main...HEAD", 200_000)
        collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert not sentinel.exists()
        assert "converted" not in bundle.body

    def test_core_fsmonitor_is_neutralised(self, repo, tmp_path):
        """Pinned as defence in depth, not as a hole that was open.

        `core.fsmonitor` fires when git refreshes the index, which a
        working-tree diff does and a range diff does not — and this module only
        ever diffs ranges. So the positive control below uses the working-tree
        form to show the config is live; the `-c core.fsmonitor=` override is
        what keeps that true if a later verb ever reads the working tree.
        """
        sentinel = tmp_path / "fsm_sentinel"
        script = tmp_path / "fsm.sh"
        script.write_text(f"#!/bin/sh\necho fired > {sentinel}\nexit 1\n")
        script.chmod(0o755)
        branch_with_change(repo)
        run_git(repo, "config", "core.fsmonitor", str(script))
        (repo / "app.py").write_text("def existing():\n    return 99\n")

        subprocess.run(
            ["git", "diff", "--stat", "--"],
            cwd=str(repo),
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        assert sentinel.exists(), "fixture is wrong: core.fsmonitor never fired"
        sentinel.unlink()

        collect_diff(repo, "main...HEAD", 200_000)

        assert not sentinel.exists()

    def test_a_legitimate_worktree_resolves_its_git_dir(self, repo):
        assert git_dir(repo) == (repo / ".git").resolve()

    def test_a_linked_worktree_inside_the_root_is_accepted(self, repo, repos_root):
        """`git worktree add` puts the git dir under the main repo, not the tree."""
        branch_with_change(repo)
        linked = repos_root / "proj-wt"
        run_git(repo, "worktree", "add", "-q", str(linked), "main")

        resolved = git_dir(linked)

        assert str(resolved).startswith(str((repo / ".git").resolve()))


class TestCollectDiff:
    def test_it_reports_files_and_changed_lines(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.files == ["app.py"]
        assert bundle.lines == 4  # the two-line function and the two blanks before it
        assert bundle.truncated is False
        assert "def added_helper" in bundle.body
        assert "app.py" in bundle.stat

    def test_a_deleted_file_is_listed_separately(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "caller.py").unlink()
        commit(repo, "drop the caller")

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.deleted == ["caller.py"]
        assert "caller.py" in bundle.files

    def test_binary_files_are_named_but_not_inlined(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
        commit(repo, "add a binary")

        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.binary == ["blob.bin"]
        assert "blob.bin" in bundle.stat
        assert "Binary files" not in bundle.body

    def test_over_the_cap_every_file_keeps_its_stat_line_and_some_body(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "big.py").write_text("".join(f"BIG_{n} = {n}\n" for n in range(4000)))
        (repo / "small.py").write_text("SMALL_SENTINEL = 1\n")
        commit(repo, "one big file and one small one")

        bundle = collect_diff(repo, "main...HEAD", 4000)

        assert bundle.truncated is True
        assert "big.py" in bundle.stat and "small.py" in bundle.stat
        assert "big.py" in bundle.truncated_files
        # The small file must not be starved by the big one.
        assert "SMALL_SENTINEL" in bundle.body
        assert "small.py" not in bundle.truncated_files
        assert len(bundle.body) <= 4000

    def test_an_empty_range_is_an_empty_bundle_not_an_error(self, repo):
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        assert bundle.files == []
        assert bundle.body == ""
        assert bundle.lines == 0

    def test_the_head_is_the_ranges_endpoint_not_always_HEAD(self, repo):
        """Everything in the context is read at `bundle.head`.

        `resolve_range` produces `<base>...HEAD`, but an explicit range need
        not end there, and reading the file bodies, conventions and callers at
        HEAD for a range that ends elsewhere hands the reviewer a different
        tree than the diff with nothing saying so.
        """
        branch_with_change(repo)
        (repo / "app.py").write_text("def existing():\n    return 1\n\n\nLATER_SENTINEL = 1\n")
        commit(repo, "a later commit")
        earlier = run_git(repo, "rev-parse", "HEAD~1").strip()

        bundle = collect_diff(repo, f"main..{earlier}", 200_000)
        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert bundle.head == earlier
        assert "def added_helper" in bodies
        assert "LATER_SENTINEL" not in bodies

    def test_a_bad_range_raises_with_the_git_stderr(self, repo):
        with pytest.raises(ReviewError) as excinfo:
            collect_diff(repo, "nope...HEAD", 200_000)
        assert excinfo.value.reason == "bad_range"


class TestCollectFileBodies:
    def test_a_changed_file_arrives_whole(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "def existing():" in bodies  # context the hunk alone would hide
        assert "def added_helper" in bodies
        assert "app.py" in bodies

    def test_a_deleted_file_is_skipped(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "caller.py").unlink()
        commit(repo, "drop the caller")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "caller.py" not in bodies

    def test_a_file_over_the_per_file_cap_is_replaced_by_a_pointer_to_the_diff(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "big.py").write_text(
            "HEAD_SENTINEL = 0\n"
            + "".join(f"BIG_{n} = {n}\n" for n in range(2000))
            + "TAIL_SENTINEL = 1\n"
        )
        commit(repo, "big file")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=200, max_total_chars=60_000)

        assert "big.py" in bodies
        assert "hunks only" in bodies
        assert len(bodies) < 20_000

    def test_a_symlink_yields_its_target_text_not_the_file(self, repo, tmp_path):
        """Bodies come from the object store, so a planted link reads as text."""
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "link.txt").symlink_to(outside)
        commit(repo, "plant a link")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=60_000)

        assert "OUTSIDE_SENTINEL" not in bodies
        assert str(outside) in bodies  # the link text itself, which is harmless

    def test_the_total_cap_stops_the_gather(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(6):
            (repo / f"mod{n}.py").write_text(f"VALUE_{n} = {n}\n" * 40)
        commit(repo, "six modules")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        bodies = collect_file_bodies(repo, bundle, max_file_chars=20_000, max_total_chars=1200)

        # The exact cap, not the cap plus slack. A notice appended on top of a
        # budget is the one place that budget is guaranteed to be wrong, so the
        # assertion has to be the bound itself or it pins nothing.
        assert len(bodies) <= 1200
        assert "VALUE_0" in bodies
        assert "omitted for space" in bodies


class TestChangedSymbols:
    def test_it_finds_python_definitions(self):
        diff = (
            "+++ b/app.py\n"
            "+def added_helper(value):\n"
            "+async def fetch_thing(url):\n"
            "+class Widget:\n"
            "+    return 1\n"
        )
        assert changed_symbols(diff) == ["added_helper", "fetch_thing", "Widget"]

    def test_it_finds_typescript_exports(self):
        diff = (
            "+++ b/web/src/lib/api.ts\n"
            "+export function loadRooms(fetch: typeof globalThis.fetch) {\n"
            "+export const ROOM_LIMIT = 50;\n"
        )
        assert changed_symbols(diff) == ["loadRooms", "ROOM_LIMIT"]

    def test_a_deleted_definition_is_not_reported_as_changed(self):
        diff = "+++ b/app.py\n-def removed_helper(value):\n+    return 1\n"
        assert changed_symbols(diff) == []

    def test_the_diff_header_is_not_mistaken_for_an_addition(self):
        diff = "+++ b/def_something.py\n@@ -1 +1 @@\n+VALUE = 1\n"
        assert changed_symbols(diff) == []

    def test_a_symbol_defined_twice_appears_once(self):
        diff = "+++ b/a.py\n+def dup(x):\n+++ b/b.py\n+def dup(y):\n"
        assert changed_symbols(diff) == ["dup"]


class TestCollectCallers:
    def test_it_finds_a_caller_outside_the_diff(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=8, total_chars=10_000), head)

        assert "caller.py" in out
        assert "existing" in out

    def test_a_symbol_with_no_callers_contributes_no_header(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(
            repo, ["nowhere_at_all"], Caps(per_symbol=8, total_chars=10_000), head
        )

        assert out == ""
        assert "nowhere_at_all" not in out

    def test_the_per_symbol_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(10):
            (repo / f"site{n}.py").write_text("existing()\n")
        commit(repo, "many callers")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=3, total_chars=10_000), head)

        assert len([ln for ln in out.splitlines() if ln.startswith("site")]) <= 3

    def test_the_total_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(10):
            (repo / f"site{n}.py").write_text("existing()\n")
        commit(repo, "many callers")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_callers(repo, ["existing"], Caps(per_symbol=50, total_chars=200), head)

        assert len(out) <= 200

    def test_an_option_shaped_revision_is_refused(self, repo):
        """`git grep` is the one subcommand that rejects `--end-of-options`.

        Debian bookworm's git 2.39 — what `docker/istota/Dockerfile` shipped
        until ISSUE-440 — exits 128 on it, so the flag cannot be the guard
        here. The substitute is a full object id, and this is the test that it
        actually refuses.

        That substitute is why the base bump costs nothing: no image in the
        tree runs a git that rejects the flag any more, so nothing reproduces
        the original symptom, but this test never depended on the git version
        — it exercises the object-id guard directly.
        """
        branch_with_change(repo)

        with pytest.raises(ReviewError, match="not a resolved object id"):
            collect_callers(
                repo, ["existing"], Caps(per_symbol=8, total_chars=10_000),
                "--output=/tmp/pwned",
            )

    def test_a_bare_ref_is_refused_rather_than_resolved(self, repo):
        """Stricter than `--end-of-options`, deliberately.

        A ref would be safe to grep, but accepting one would mean the guard is
        "does it start with a dash", which is the check this replaced. The only
        caller passes `bundle.head`, already resolved.
        """
        branch_with_change(repo)

        with pytest.raises(ReviewError, match="not a resolved object id"):
            collect_callers(repo, ["existing"], Caps(per_symbol=8, total_chars=10_000), "HEAD")


class TestCollectConventions:
    def test_root_agents_and_claude_files_are_included(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "CLAUDE.md").write_text("See AGENTS.md.\n")
        (repo / "app.py").write_text("def existing():\n    return 2\n")
        commit(repo, "add CLAUDE.md")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["app.py"], 60_000)

        assert "Spaces, never tabs." in out
        assert "See AGENTS.md." in out

    def test_a_rules_file_is_included_only_when_its_paths_match(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        rules = repo / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "brain.md").write_text(
            '---\npaths:\n  - "src/brain/**"\n---\nBRAIN_RULE_SENTINEL\n'
        )
        (rules / "money.md").write_text(
            '---\npaths:\n  - "src/money/**"\n---\nMONEY_RULE_SENTINEL\n'
        )
        commit(repo, "add rules")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["src/brain/native.py"], 60_000)

        assert "BRAIN_RULE_SENTINEL" in out
        assert "MONEY_RULE_SENTINEL" not in out

    def test_an_absent_rules_directory_is_silent(self, repo):
        branch_with_change(repo)
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["app.py"], 60_000)

        assert ".claude/rules" not in out
        assert "Spaces, never tabs." in out

    def test_the_cap_holds(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "AGENTS.md").write_text("padding\n" * 5000)
        commit(repo, "huge conventions")
        head = run_git(repo, "rev-parse", "HEAD").strip()

        out = collect_conventions(repo, head, ["AGENTS.md"], 500)

        assert len(out) <= 500
        assert "conventions truncated" in out


class TestSizeReview:
    def _bundle(self, monkeypatch, *, lines: int, files: list[str]):
        from istota.skills.code_review.engine import DiffBundle

        return DiffBundle(
            rng="main...HEAD",
            head="deadbeef",
            stat="",
            body="",
            files=files,
            deleted=[],
            binary=[],
            lines=lines,
            truncated=False,
            truncated_files=[],
        )

    def test_a_small_ordinary_diff_gets_conformance_alone(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=20, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance"]
        assert "threshold" in reason

    def test_over_the_line_threshold_gets_both(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=400, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(both_agents_threshold_lines=150), None)
        assert agents == ["conformance", "bughunt"]
        assert "400" in reason and "150" in reason

    def test_a_boundary_path_in_a_tiny_diff_gets_both(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["src/auth/session.py"])
        agents, reason = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance", "bughunt"]
        assert "auth" in reason
        assert "src/auth/session.py" in reason

    def test_boundary_matching_is_case_insensitive(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["db/Migration_007.sql"])
        agents, _ = size_review(bundle, ReviewConfig(), None)
        assert agents == ["conformance", "bughunt"]

    def test_forcing_both_overrides_the_sizing(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(), "both")
        assert agents == ["conformance", "bughunt"]
        assert "requested" in reason

    def test_forcing_conformance_overrides_a_boundary_path(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=900, files=["src/auth/session.py"])
        agents, reason = size_review(bundle, ReviewConfig(), "conformance")
        assert agents == ["conformance"]
        assert "requested" in reason

    def test_forcing_bughunt_gets_the_bug_hunter(self, monkeypatch):
        """Falling through would answer a request nobody made.

        `bughunt` is a member of the module's own agent tuple, so silently
        sizing automatically would hand back the conformance reviewer and then
        report a threshold decision as the reason — leaving the caller no way
        to see its choice was dropped.
        """
        bundle = self._bundle(monkeypatch, lines=3, files=["app.py"])
        agents, reason = size_review(bundle, ReviewConfig(), "bughunt")
        assert agents == ["bughunt"]
        assert "requested" in reason

    def test_an_unrecognised_agents_value_is_an_error(self, monkeypatch):
        bundle = self._bundle(monkeypatch, lines=3, files=["app.py"])
        with pytest.raises(ReviewError) as excinfo:
            size_review(bundle, ReviewConfig(), "skinner")
        assert excinfo.value.reason == "unknown_agent"


class TestBuildPrompt:
    def _bundle(self):
        from istota.skills.code_review.engine import DiffBundle

        return DiffBundle(
            rng="main...HEAD",
            head="deadbeef",
            stat=" app.py | 2 +-\n",
            body="+def added_helper(value):\n",
            files=["app.py"],
            deleted=[],
            binary=[],
            lines=2,
            truncated=False,
            truncated_files=[],
        )

    def test_it_carries_the_intent_the_diff_and_the_context(self):
        prompt = build_prompt("conformance", self._bundle(), "CONTEXT_SENTINEL", "fix the header")

        assert "fix the header" in prompt
        assert "added_helper" in prompt
        assert "CONTEXT_SENTINEL" in prompt
        assert "main...HEAD" in prompt

    def test_it_states_that_the_reviewer_has_no_tools(self):
        prompt = build_prompt("conformance", self._bundle(), "", "x")

        assert "no tools" in prompt.lower()
        assert "unverified" in prompt

    def test_the_two_agents_ask_for_different_things(self):
        bundle = self._bundle()
        conformance = build_prompt("conformance", bundle, "", "x")
        bughunt = build_prompt("bughunt", bundle, "", "x")

        assert conformance != bughunt
        assert "conformance" in conformance.lower()
        assert "off-by-one" in bughunt.lower()

    def test_a_truncated_diff_says_so_and_names_the_files(self):
        bundle = self._bundle()
        bundle.truncated = True
        bundle.truncated_files = ["big.py"]

        prompt = build_prompt("conformance", bundle, "", "x")

        assert "truncated" in prompt.lower()
        assert "big.py" in prompt

    def test_an_unknown_agent_is_refused(self):
        with pytest.raises(ReviewError):
            build_prompt("skinner", self._bundle(), "", "x")

    def test_it_offers_the_need_files_round_trip_when_one_is_available(self):
        prompt = build_prompt("conformance", self._bundle(), "", "x", max_need_files=6)

        assert "need_files" in prompt
        assert "6" in prompt

    def test_it_never_offers_a_round_trip_it_cannot_serve(self):
        """`max_need_files = 0`, or a call budget with no room for a second
        round. Advertising a facility that will be refused spends the
        reviewer's attention on a request nothing answers."""
        prompt = build_prompt("conformance", self._bundle(), "", "x", max_need_files=0)

        assert "need_files" not in prompt

    def test_the_round_trip_is_off_by_default_for_callers_that_do_not_ask(self):
        assert "need_files" not in build_prompt("conformance", self._bundle(), "", "x")


class TestParseFindings:
    PAYLOAD = {
        "findings": [
            {
                "severity": "must-fix",
                "file": "app.py",
                "line": 12,
                "claim": "Null deref",
                "evidence": "value may be None",
                "action": "guard it",
            }
        ]
    }

    def test_bare_json_parses(self):
        found = parse_findings(json.dumps(self.PAYLOAD), "conformance")
        assert len(found) == 1
        assert found[0].file == "app.py"
        assert found[0].line == 12
        assert found[0].sources == ["conformance"]

    def test_fenced_json_parses(self):
        raw = "```json\n" + json.dumps(self.PAYLOAD) + "\n```"
        assert len(parse_findings(raw, "bughunt")) == 1

    def test_leading_prose_is_tolerated(self):
        raw = "Here is what I found.\n\n" + json.dumps(self.PAYLOAD)
        assert len(parse_findings(raw, "conformance")) == 1

    def test_a_bare_list_of_findings_parses(self):
        assert len(parse_findings(json.dumps(self.PAYLOAD["findings"]), "conformance")) == 1

    def test_malformed_output_returns_nothing_so_the_caller_can_retry(self):
        assert parse_findings("I could not review this, sorry.", "conformance") == []
        assert parse_findings("", "conformance") == []
        assert parse_findings("{ not json at all", "conformance") == []

    def test_severity_is_normalised_and_an_unknown_one_is_kept_as_medium(self):
        raw = json.dumps(
            {
                "findings": [
                    {"severity": "MUST_FIX", "file": "a.py", "line": 1, "claim": "x"},
                    {"severity": "wat", "file": "b.py", "line": 1, "claim": "y"},
                ]
            }
        )
        found = parse_findings(raw, "conformance")
        assert [f.severity for f in found] == ["must-fix", "medium"]

    def test_a_finding_without_a_file_is_dropped(self):
        raw = json.dumps({"findings": [{"severity": "high", "claim": "vague"}]})
        assert parse_findings(raw, "conformance") == []

    def test_a_missing_line_is_none_rather_than_a_guess(self):
        raw = json.dumps({"findings": [{"severity": "high", "file": "a.py", "claim": "x"}]})
        assert parse_findings(raw, "conformance")[0].line is None

    def test_the_unverified_flag_survives(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "severity": "high",
                        "file": "a.py",
                        "line": 3,
                        "claim": "x",
                        "unverified": True,
                    }
                ]
            }
        )
        assert parse_findings(raw, "conformance")[0].unverified is True


class TestMergeFindings:
    def _f(self, severity, file, line, source, evidence=""):
        return Finding(
            severity=severity,
            file=file,
            line=line,
            claim=f"{file}:{line}",
            evidence=evidence,
            action="",
            sources=[source],
        )

    def test_the_same_location_from_both_agents_merges_with_both_sources(self):
        merged = merge_findings(
            [
                [self._f("high", "a.py", 5, "conformance", "rule says no")],
                [self._f("high", "a.py", 5, "bughunt", "races with the writer")],
            ]
        )

        assert len(merged) == 1
        assert merged[0].sources == ["bughunt", "conformance"]
        assert "rule says no" in merged[0].evidence
        assert "races with the writer" in merged[0].evidence

    def test_two_different_claims_at_one_line_do_not_lose_the_second(self):
        """The ordinary case, not an edge one.

        Conformance reporting "wrong error type" and bughunt reporting "null
        deref" at the same line is two defects, not corroboration of one. The
        entry still merges — a caller acts on a location — but the second
        claim has to survive, or the merged finding reads as two reviewers
        agreeing about something only one of them said.
        """
        first = self._f("high", "a.py", 5, "conformance")
        first.claim = "wrong error type raised"
        second = self._f("high", "a.py", 5, "bughunt")
        second.claim = "null deref on the same line"

        merged = merge_findings([[first], [second]])

        assert len(merged) == 1
        assert "null deref on the same line" in merged[0].evidence
        assert merged[0].sources == ["bughunt", "conformance"]

    def test_a_merge_keeps_the_higher_severity(self):
        merged = merge_findings(
            [
                [self._f("medium", "a.py", 5, "conformance")],
                [self._f("must-fix", "a.py", 5, "bughunt")],
            ]
        )
        assert merged[0].severity == "must-fix"

    def test_low_and_preference_findings_are_dropped(self):
        merged = merge_findings(
            [
                [
                    self._f("low", "a.py", 1, "conformance"),
                    self._f("preference", "a.py", 2, "conformance"),
                    self._f("medium", "a.py", 3, "conformance"),
                ]
            ]
        )
        assert [f.line for f in merged] == [3]

    def test_sorting_is_severity_then_path_then_line(self):
        merged = merge_findings(
            [
                [
                    self._f("medium", "a.py", 1, "conformance"),
                    self._f("must-fix", "z.py", 9, "conformance"),
                    self._f("must-fix", "a.py", 20, "conformance"),
                    self._f("must-fix", "a.py", 3, "conformance"),
                    self._f("high", "b.py", 1, "conformance"),
                ]
            ]
        )
        assert [(f.severity, f.file, f.line) for f in merged] == [
            ("must-fix", "a.py", 3),
            ("must-fix", "a.py", 20),
            ("must-fix", "z.py", 9),
            ("high", "b.py", 1),
            ("medium", "a.py", 1),
        ]

    def test_different_lines_in_one_file_stay_separate(self):
        merged = merge_findings(
            [[self._f("high", "a.py", 5, "conformance"), self._f("high", "a.py", 6, "conformance")]]
        )
        assert len(merged) == 2

    def test_a_finding_outside_the_diff_is_kept_and_marked(self):
        merged = merge_findings(
            [[self._f("high", "untouched.py", 5, "conformance")]],
            changed_files=["app.py"],
        )
        assert len(merged) == 1
        assert merged[0].outside_diff is True

    def test_a_finding_inside_the_diff_is_not_marked(self):
        merged = merge_findings(
            [[self._f("high", "app.py", 5, "conformance")]],
            changed_files=["app.py"],
        )
        assert merged[0].outside_diff is False


class TestAssembleContext:
    def test_it_carries_conventions_file_bodies_commits_and_callers(self, repo):
        # A *changed signature* rather than a new function, so there is an
        # existing caller for the callers section to find.
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "app.py").write_text("def existing(scale=1):\n    return 1 * scale\n")
        commit(repo, "app: add a helper")
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        context = assemble_context(repo, bundle, ReviewConfig())

        assert "Spaces, never tabs." in context  # conventions
        assert "--- app.py (whole file) ---" in context  # whole-file body
        assert "app: add a helper" in context  # commit subject
        assert "caller.py" in context  # callers of a changed symbol

    def test_the_total_cap_holds(self, repo):
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)

        context = assemble_context(repo, bundle, ReviewConfig(max_context_chars=300))

        assert len(context) <= 300

    def test_a_negative_cap_yields_nothing_rather_than_a_negative_slice(self, repo):
        """`text[:-5]` is not an empty string, it is most of the string.

        Every cap here reaches a slice, and a config field a TOML edit can make
        negative would otherwise turn each one into "drop the last five
        characters" — a budget that grows the output it was meant to bound.
        """
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", -5)

        assert bundle.body == ""
        assert assemble_context(repo, bundle, ReviewConfig(max_context_chars=-5)) == ""
        assert collect_conventions(repo, bundle.head, bundle.files, -5) == ""
        assert collect_file_bodies(repo, bundle, max_file_chars=10, max_total_chars=-5) == ""


class TestCollectNeededFiles:
    """The `need_files` round trip's file server.

    A reviewer names paths and the engine serves them, which makes this the one
    place in the module where a *model* chooses which blob gets read. Two rules
    hold it: the path must be a plain relative path inside the repository, and
    the body still comes out of the object store rather than off the filesystem.
    The second is what makes the first defence in depth rather than the whole
    boundary — see `test_a_symlink_yields_its_target_text_not_the_file` below.
    """

    def test_a_requested_file_arrives_whole(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["caller.py"], max_files=6, max_file_chars=20_000
        )

        assert served.served == ["caller.py"]
        assert served.refused == []
        assert "from app import existing" in served.text
        assert "caller.py" in served.text

    def test_a_file_the_diff_never_touched_is_servable(self, repo):
        """The whole point: the reviewer is asking for something it was not given."""
        branch_with_change(repo)
        bundle = collect_diff(repo, "main...HEAD", 200_000)
        assert "caller.py" not in bundle.files

        served = collect_needed_files(
            repo, bundle.head, ["caller.py"], max_files=6, max_file_chars=20_000
        )

        assert served.served == ["caller.py"]

    def test_a_path_outside_the_worktree_is_dropped_and_the_rest_still_served(
        self, repo, tmp_path
    ):
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n")
        branch_with_change(repo)

        served = collect_needed_files(
            repo,
            "HEAD",
            ["../outside_secret.txt", "app.py"],
            max_files=6,
            max_file_chars=20_000,
        )

        assert served.served == ["app.py"]
        assert "../outside_secret.txt" in served.refused
        assert "OUTSIDE_SENTINEL" not in served.text
        assert "def existing" in served.text, "one bad path must not sink the rest"

    def test_a_deeply_escaping_path_is_refused(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo,
            "HEAD",
            ["src/../../etc/passwd", "a/b/../../../outside"],
            max_files=6,
            max_file_chars=20_000,
        )

        assert served.served == []
        assert len(served.refused) == 2

    def test_an_absolute_path_is_refused(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["/etc/passwd"], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert served.refused == ["/etc/passwd"]

    def test_an_option_shaped_path_is_refused(self, repo):
        """Defence in depth, not the thing that closes option injection: `_show`
        embeds the path behind `END_OF_OPTIONS`, so git cannot read it as an
        option today. Pinned for the caller that passes one as its own argv."""
        branch_with_change(repo)

        served = collect_needed_files(
            repo,
            "HEAD",
            ["--output=/tmp/pwned", "-c"],
            max_files=6,
            max_file_chars=20_000,
        )

        assert served.served == []
        assert len(served.refused) == 2

    def test_a_symlink_yields_its_target_text_not_the_file(self, repo, tmp_path):
        """The containment check is defence in depth; this is the real boundary.

        A link planted inside the worktree passes every path rule — it *is* a
        contained relative path — so if the body came off the filesystem it
        would read straight out of the root. Coming out of the object store, it
        reads as the link text, which is harmless.
        """
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n")
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "link.txt").symlink_to(outside)
        commit(repo, "plant a link")

        served = collect_needed_files(
            repo, "HEAD", ["link.txt"], max_files=6, max_file_chars=20_000
        )

        assert served.served == ["link.txt"]
        assert "OUTSIDE_SENTINEL" not in served.text
        assert str(outside) in served.text

    def test_the_cap_truncates_the_request(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(5):
            (repo / f"mod{n}.py").write_text(f"VALUE_{n} = {n}\n")
        commit(repo, "five modules")

        served = collect_needed_files(
            repo,
            "HEAD",
            [f"mod{n}.py" for n in range(5)],
            max_files=2,
            max_file_chars=20_000,
        )

        assert served.served == ["mod0.py", "mod1.py"]
        assert served.refused == ["mod2.py", "mod3.py", "mod4.py"]

    def test_a_cap_of_zero_serves_nothing(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["app.py"], max_files=0, max_file_chars=20_000
        )

        assert served.served == []
        assert served.text == ""

    def test_an_unknown_path_is_refused_rather_than_served_empty(self, repo):
        """A reviewer told nothing about a file it asked for would take the
        silence for an empty file and file a finding on it."""
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["no/such/file.py"], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert served.refused == ["no/such/file.py"]

    def test_a_duplicate_request_is_served_once(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo,
            "HEAD",
            ["app.py", "app.py", "./app.py"],
            max_files=6,
            max_file_chars=20_000,
        )

        assert served.served == ["app.py"]
        assert served.text.count("(whole file)") == 1

    def test_a_file_over_the_per_file_cap_is_truncated_rather_than_dropped(self, repo):
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "big.py").write_text(
            "HEAD_SENTINEL = 0\n" + "".join(f"BIG_{n} = {n}\n" for n in range(2000))
        )
        commit(repo, "big file")

        served = collect_needed_files(
            repo, "HEAD", ["big.py"], max_files=6, max_file_chars=200
        )

        assert served.served == ["big.py"]
        assert "HEAD_SENTINEL" in served.text
        assert "truncated" in served.text
        assert len(served.text) < 1000

    def test_a_refusal_is_named_in_the_text_the_reviewer_sees(self, repo):
        """A dropped request the reviewer is not told about is a finding resting
        on a file it believes it was given."""
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["app.py", "/etc/passwd"], max_files=6, max_file_chars=20_000
        )

        assert "/etc/passwd" in served.text
        assert "not served" in served.text.lower()

    def test_an_empty_request_produces_nothing(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", [], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert served.refused == []
        assert served.text == ""


    def test_a_directory_is_refused_rather_than_served_as_a_file(self, repo):
        """`git show <rev>:<dir>` prints a tree listing quite happily, and
        labelling that "(whole file)" is how a reviewer comes to believe a
        directory is a two-line module."""
        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text("VALUE = 1\n")
        commit(repo, "add a package")

        served = collect_needed_files(
            repo, "HEAD", ["pkg"], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert served.refused == ["pkg"]
        assert "mod.py" not in served.text

    def test_the_request_list_itself_is_bounded(self, repo):
        """The list is model-written and every refusal is echoed back into the
        reviewer's own prompt, so an enormous one must not become an enormous
        prompt."""
        branch_with_change(repo)
        requested = ["app.py"] + [f"no/such/file{n}.py" for n in range(500)]

        served = collect_needed_files(
            repo, "HEAD", requested, max_files=6, max_file_chars=20_000
        )

        assert served.served == ["app.py"]
        assert len(served.refused) < 500
        assert "not looked at" in served.text
        assert len(served.text) < 20_000

    def test_an_absurdly_long_path_is_truncated_before_it_is_echoed(self, repo):
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["z" * 50_000], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert len(served.refused[0]) < 200
        assert len(served.text) < 1000

    def test_the_total_cap_stops_the_gather(self, repo):
        """`max_files * max_file_chars` otherwise exceeds the whole context
        budget, so the round trip would be the one context source that ignores
        it — `collect_file_bodies` next door is bounded the same way."""
        run_git(repo, "checkout", "-q", "-b", "feature")
        for n in range(4):
            (repo / f"mod{n}.py").write_text(f"VALUE_{n} = {n}\n" * 60)
        commit(repo, "four modules")

        served = collect_needed_files(
            repo,
            "HEAD",
            [f"mod{n}.py" for n in range(4)],
            max_files=6,
            max_file_chars=20_000,
            max_total_chars=1500,
        )

        assert served.served, "the first file must still fit"
        assert len(served.served) < 4
        assert served.refused, "and what did not fit must be named, not dropped"
        assert len(served.text) < 3000


    def test_a_dash_created_by_normalising_is_refused(self, repo):
        """Normalising can *create* the option shape: `./-output=x` collapses to
        `-output=x`, so the check that runs before it would let the caller be
        handed exactly what it was meant to refuse."""
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", ["./-output=x"], max_files=6, max_file_chars=20_000
        )

        assert served.served == []
        assert served.refused == ["./-output=x"]

    def test_an_oversized_blob_is_refused_unread(self, repo):
        """`_show` would read up to MAX_GIT_OUTPUT_BYTES before giving up, so a
        request naming oversized blobs is the daemon reading a gigabyte to
        produce a few excerpts. The size is asked for before the content."""
        from istota.skills.code_review import engine

        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "huge.bin").write_text("x" * 4000)
        (repo / "small.py").write_text("VALUE = 1\n")
        commit(repo, "one large file, one small")

        # Not a real 2 MiB file — the bound is what is under test, not git.
        original = engine.MAX_NEED_FILE_BYTES
        engine.MAX_NEED_FILE_BYTES = 1000
        try:
            served = collect_needed_files(
                repo,
                "HEAD",
                ["huge.bin", "small.py"],
                max_files=6,
                max_file_chars=20_000,
            )
        finally:
            engine.MAX_NEED_FILE_BYTES = original

        assert served.served == ["small.py"]
        assert served.refused == ["huge.bin"]
        assert "xxxx" not in served.text

    def test_a_one_shot_iterable_does_not_produce_a_negative_overflow(self, repo):
        """`requested` is untyped and this function is called directly. Consuming
        it twice made the overflow count negative and the note nonsense."""
        branch_with_change(repo)

        served = collect_needed_files(
            repo, "HEAD", iter(["app.py"]), max_files=6, max_file_chars=20_000
        )

        assert served.served == ["app.py"]
        assert "further requested path(s)" not in served.text

    def test_a_gitdir_redirect_is_refused_here_too(self, repos_root, tmp_path):
        """`collect_file_bodies` validates the git directory before it reads a
        blob, and this reads blobs the same way for a model-chosen path list."""
        outside = tmp_path / "outside_repo"
        outside.mkdir()
        run_git(outside, "init", "-q", "-b", "main", ".")
        (outside / "secret.py").write_text("OUTSIDE_SENTINEL = 1\n")
        commit(outside, "outside")

        contained = repos_root / "redirected"
        contained.mkdir()
        (contained / ".git").write_text(f"gitdir: {outside}/.git\n")

        with pytest.raises(ReviewError):
            collect_needed_files(
                contained, "HEAD", ["secret.py"], max_files=6, max_file_chars=20_000
            )


class TestRoundTripBudget:
    """Whether the `need_files` round trip is affordable, measured not assumed.

    The round trip is optional: the first answer already stands, and the second
    call is strictly the larger of the two — the same prompt, plus the served
    bodies, plus the final-answer instruction. So the only call worth starting
    is one that can finish, and the evidence for that is what the round that
    just ran actually cost. A flat floor cannot see it: against any budget of
    30s or more it admitted the round whenever 15s remained, which is how a
    reviewer that spent 78 of its 120 seconds was sent back out with 42.

    These drive `_run_agent` directly against a fake clock, because the thing
    under test is arithmetic over durations and a real one would need the test
    to spend the minutes it is measuring.
    """

    @staticmethod
    def _clock():
        """Stands in for the `time` module inside `engine` only.

        Everything but `monotonic` is delegated to the real module. `engine`
        also reads `time` from `_read_bounded`'s subprocess deadline and from
        `run_review`'s bounded join, and a stand-in that answered only
        `monotonic` would make any future use of another attribute fail from
        inside a daemon thread rather than in the test that installed it.
        """

        class Clock:
            def __init__(self):
                self.now = 1_000.0

            def monotonic(self):
                return self.now

            def __getattr__(self, name):
                return getattr(time, name)

        return Clock()

    @staticmethod
    def _serve(*served):
        def serve(requested):
            return engine.NeededFiles(
                text="## Files you asked for\n\nBODY\n", served=list(served)
            )

        return serve

    def _run(self, monkeypatch, *, budget, durations, serve_cost=0.0, serve=None):
        """Run one reviewer whose calls take `durations` seconds each.

        Returns `(outcome, timeouts)` — the second being the budget each call
        was actually handed, so a test can assert on the round that was made as
        well as on the one that was not.
        """
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        replies = [
            json.dumps({"need_files": ["helper.py"], "findings": []}),
            json.dumps({"findings": []}),
        ]
        timeouts: list[int] = []
        spent = iter(durations)

        def invoke(agent, prompt, timeout_seconds):
            timeouts.append(timeout_seconds)
            clock.now += next(spent)
            return engine.AgentReply(ok=True, text=replies[len(timeouts) - 1])

        def serving(requested):
            clock.now += serve_cost
            return (serve or self._serve("helper.py"))(requested)

        outcome = engine._run_agent(
            engine.CONFORMANCE,
            "first prompt",
            invoke,
            budget,
            serve=serving,
            build_final_prompt=lambda: "final prompt",
        )
        return outcome, timeouts

    def test_a_round_that_cannot_finish_in_what_is_left_is_not_started(
        self, monkeypatch
    ):
        """The reported failure: 120s budget, 78s spent, 42s left, and the flat
        15s floor waved the second call through to be charged and time out."""
        outcome, timeouts = self._run(monkeypatch, budget=120, durations=[78.0])

        assert timeouts == [120], "the round trip must not have been invoked"
        assert outcome.round_trip is False
        assert outcome.round_trip_refused is True
        assert outcome.calls == 1, "a round that was not made must not be charged"
        assert "78s" in outcome.note and "42s" in outcome.note

    def test_the_other_reported_budget_is_refused_the_same_way(self, monkeypatch):
        """240s budget, 157s spent, 83s left. A larger budget does not make a
        second call that costs more than 83s fit into 83s."""
        outcome, timeouts = self._run(monkeypatch, budget=240, durations=[157.0])

        assert timeouts == [240], "the round trip must not have been invoked"
        assert outcome.round_trip_refused is True
        assert outcome.calls == 1

    def test_a_retry_does_not_reset_the_measurement(self, monkeypatch):
        """The reintroduction path. `started` covers both calls, but the retry
        is timed on its own — so taking the *last* duration rather than the
        slowest lets a reviewer that burned 100s on prose and answered the nudge
        in 2s be estimated at 3s and admitted with 18s left, which is the
        original bug arriving through the fix."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        timeouts: list[int] = []
        spent = iter([100.0, 2.0])
        replies = [
            "prose, not JSON",
            json.dumps({"need_files": ["helper.py"], "findings": []}),
        ]

        def invoke(agent, prompt, timeout_seconds):
            timeouts.append(timeout_seconds)
            clock.now += next(spent)
            return engine.AgentReply(ok=True, text=replies[len(timeouts) - 1])

        outcome = engine._run_agent(
            engine.CONFORMANCE,
            "first prompt",
            invoke,
            120,
            serve=self._serve("helper.py"),
            build_final_prompt=lambda: "final prompt",
        )

        assert timeouts == [120, 20], "the retry runs, the round trip does not"
        assert outcome.round_trip is False
        assert outcome.round_trip_refused is True
        assert outcome.calls == 2, "the retry is charged; the refused round is not"
        assert "100s" in outcome.note

    def test_a_reviewer_that_answered_quickly_still_gets_its_round(self, monkeypatch):
        """The point is to refuse the rounds that cannot finish, not to stop
        offering them. 20s of a 240s budget leaves 220s for a call estimated to
        cost 30 — that one runs, and runs against what is left."""
        outcome, timeouts = self._run(monkeypatch, budget=240, durations=[20.0, 25.0])

        assert len(timeouts) == 2, "the round trip must have been invoked"
        assert timeouts[1] == 220
        assert outcome.round_trip is True
        assert outcome.round_trip_refused is False
        assert outcome.calls == 2

    def test_the_estimate_carries_a_margin_for_the_larger_second_prompt(
        self, monkeypatch
    ):
        """A second call is not the same size as the first, so "it fits if the
        last one did" is the wrong test. 70s of a 120s budget leaves 50, which
        clears the 70 the last round cost only if the second prompt were no
        bigger than the first — and it never is."""
        outcome, timeouts = self._run(monkeypatch, budget=120, durations=[70.0])

        assert timeouts == [120], "the round trip must not have been invoked"
        assert outcome.round_trip_refused is True

        # The margin is what separates this from the previous case: the same
        # 120s budget with a round cheap enough to leave one and a half of it
        # goes through.
        outcome, timeouts = self._run(monkeypatch, budget=120, durations=[45.0, 5.0])
        assert len(timeouts) == 2
        assert outcome.round_trip is True

    def test_min_retry_seconds_remains_the_lower_bound(self, monkeypatch):
        """The estimate raises the floor; it never lowers it.

        Asserted against `_remaining` directly, because the end-to-end version
        below cannot tell the two implementations apart: a 3s estimate and the
        old flat constant both refuse a call with 3s left, so it would pass
        against the pre-change engine and prove nothing. What discriminates is
        the floor `_remaining` actually returns for a cheap estimate.
        """
        assert engine._remaining(0.0, 120, estimated_cost=3.0)[1] == (
            engine.MIN_RETRY_SECONDS
        ), "a cheap estimate must not lower the floor below the flat minimum"
        assert engine._remaining(0.0, 120, estimated_cost=45.0)[1] == 45

        # And the end-to-end half: gathering the files is charged to the same
        # clock, so a first round of 2s can still arrive at the second check
        # with almost nothing left.
        outcome, timeouts = self._run(
            monkeypatch, budget=120, durations=[2.0], serve_cost=115.0
        )

        assert timeouts == [120]
        assert outcome.round_trip_refused is True
        assert f"{engine.MIN_RETRY_SECONDS}s" in outcome.note

    def test_a_non_finite_estimate_is_ignored_rather_than_raising(self):
        """`_remaining` is called outside `_round_trip`'s exception guards, and
        an escape there turns a review already paid for into a failed reviewer
        with its findings discarded. `math.ceil(inf)` raises `OverflowError`."""
        assert engine._remaining(0.0, 120, estimated_cost=float("inf"))[1] == (
            engine.MIN_RETRY_SECONDS
        )
        assert engine._remaining(0.0, 120, estimated_cost=float("nan"))[1] == (
            engine.MIN_RETRY_SECONDS
        )

    def test_a_reviewer_that_asks_with_no_round_trip_on_offer_says_so(
        self, monkeypatch
    ):
        """`serve=None` is how a spent *call* budget reaches `_run_agent`, and
        nothing stops a reviewer emitting `need_files` with the offer absent
        from its prompt. Reporting that as `round_trip_refused: false` would say
        the reviewer never asked — and this is the case the flag matters most
        in, since a finding left `unverified` here went unchecked for want of a
        round the CLI could not buy."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        calls: list[int] = []

        def invoke(agent, prompt, timeout_seconds):
            calls.append(timeout_seconds)
            clock.now += 5.0
            return engine.AgentReply(
                ok=True,
                text=json.dumps({"need_files": ["helper.py"], "findings": []}),
            )

        outcome = engine._run_agent(
            engine.CONFORMANCE, "first prompt", invoke, 240, serve=None
        )

        assert calls == [240]
        assert outcome.round_trip is False
        assert outcome.round_trip_refused is True
        assert outcome.calls == 1
        assert outcome.note, "the note is the only place that can say why"

    def test_a_reviewer_that_never_asked_is_not_a_refusal(self, monkeypatch):
        """The other half of the same distinction, and the ordinary path."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)

        def invoke(agent, prompt, timeout_seconds):
            clock.now += 5.0
            return engine.AgentReply(ok=True, text=json.dumps({"findings": []}))

        outcome = engine._run_agent(
            engine.CONFORMANCE, "first prompt", invoke, 240, serve=None
        )

        assert outcome.round_trip_refused is False
        assert outcome.note == ""

    def test_a_second_prompt_that_cannot_be_built_is_not_charged(self, monkeypatch):
        """`build_final_prompt` is a second pass over a diff that can run to
        `max_diff_chars`, and the docstring names it as one of the two raisers
        the guards exist for. It does no model work, so a raise there must not
        be billed as a round trip that was never made."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        calls: list[int] = []

        def invoke(agent, prompt, timeout_seconds):
            calls.append(timeout_seconds)
            clock.now += 5.0
            return engine.AgentReply(
                ok=True,
                text=json.dumps(
                    {
                        "need_files": ["helper.py"],
                        "findings": [
                            {
                                "severity": "high",
                                "file": "app.py",
                                "line": 4,
                                "claim": "a defect",
                                "evidence": "observed",
                                "action": "fix it",
                            }
                        ],
                    }
                ),
            )

        def explode():
            raise RuntimeError("diff vanished under us")

        outcome = engine._run_agent(
            engine.CONFORMANCE,
            "first prompt",
            invoke,
            240,
            serve=self._serve("helper.py"),
            build_final_prompt=explode,
        )

        assert calls == [240], "no second call was made"
        assert outcome.calls == 1, "and none was charged"
        assert outcome.round_trip is False
        assert outcome.round_trip_refused is True
        assert [f.claim for f in outcome.findings] == ["a defect"]
        assert "RuntimeError" in outcome.note

    def test_a_request_nothing_could_serve_reports_a_refused_round(self, monkeypatch):
        """`round_trip_refused` is "it asked and no second call was made", not
        "the budget said no". A caller weighing an `unverified` finding needs
        the same answer either way, and the note says which it was."""
        outcome, timeouts = self._run(
            monkeypatch,
            budget=240,
            durations=[10.0],
            serve=lambda requested: engine.NeededFiles(),
        )

        assert timeouts == [240]
        assert outcome.round_trip is False
        assert outcome.round_trip_refused is True
        assert outcome.calls == 1

    def test_a_round_that_was_made_and_failed_is_not_a_refusal(self, monkeypatch):
        """The distinction the envelope has to carry: this one cost a call."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        calls: list[int] = []

        def invoke(agent, prompt, timeout_seconds):
            calls.append(timeout_seconds)
            clock.now += 10.0
            if len(calls) == 1:
                return engine.AgentReply(
                    ok=True,
                    text=json.dumps({"need_files": ["helper.py"], "findings": []}),
                )
            return engine.AgentReply(ok=False, error="timeout")

        outcome = engine._run_agent(
            engine.CONFORMANCE,
            "first prompt",
            invoke,
            240,
            serve=self._serve("helper.py"),
            build_final_prompt=lambda: "final prompt",
        )

        assert len(calls) == 2
        assert outcome.round_trip is True
        assert outcome.round_trip_refused is False
        assert outcome.calls == 2

    def test_the_malformed_retry_keeps_its_flat_floor(self, monkeypatch):
        """Deliberately not changed. A retry is the same prompt plus a nudge,
        and without it the reviewer has no usable answer at all — so a poor
        chance beats a certain failure. The round trip is the other way round:
        the first answer already stands, so a poor chance buys nothing and
        costs a call."""
        clock = self._clock()
        monkeypatch.setattr(engine, "time", clock)
        calls: list[int] = []

        def invoke(agent, prompt, timeout_seconds):
            calls.append(timeout_seconds)
            clock.now += 78.0
            if len(calls) == 1:
                return engine.AgentReply(ok=True, text="prose, not JSON")
            return engine.AgentReply(ok=True, text=json.dumps({"findings": []}))

        outcome = engine._run_agent(
            engine.CONFORMANCE, "first prompt", invoke, 120, serve=None
        )

        assert calls == [120, 42], "the retry runs on what is left, and still runs"
        assert outcome.calls == 2
        assert outcome.findings == []
