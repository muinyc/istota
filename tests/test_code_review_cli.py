"""Tests for the code_review CLI — the guards, the call cap, and the model call.

Everything the review does *without* a model lives in `engine.py` and is tested
by `test_code_review_engine.py`. This file covers the layer above it: the gates
that run before a single token is spent, the budget counter that stops a loop
from spending the operator's money, and the envelope the workflow branches on.

Three properties are load-bearing here and none of them is visible from the
happy path:

**A refused run must not construct a brain.** Every guard test monkeypatches
`make_brain` to raise, so a gate that lets a call through fails loudly rather
than passing quietly with an unasserted side effect.

**The counter is in the framework database, not in a file.** `ISTOTA_DEFERRED_DIR`
is bound read-write into the sandbox, so a loop that hit a file-backed cap could
delete the counter and carry on spending. The cap tests read `code_review_calls`
back through `db` directly rather than trusting the envelope's own count.

**A round is a wave of calls, and it is charged on invocations made rather than
on answers parsed.** A run refused by a guard and one short-circuited by the
availability breaker are free, because they spent nothing; the retry half of a
malformed-output round rides on the round that provoked it, because it did. A
reviewer that answers in prose twice has spent real money, and counting only
clean rounds would leave that loop unbounded — the failure the cap exists to
prevent, inverted. One run charges 1, or 2 when a reviewer took its `need_files`
round trip.

The brain is the mock boundary, the same place the sleep-cycle and explainer
tests draw it. There is no live model call anywhere in this file.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, ReviewConfig, load_config
from istota.skills import code_review
from istota.skills.code_review import engine

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


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def repos_root(tmp_path, monkeypatch) -> Path:
    """The caller's own subtree of `developer.repos_dir`.

    `setup_env` derives the variable as `{repos_dir}/{user_id}` and
    `developer_repos_root` refuses a value not named for `ISTOTA_USER_ID`, so
    the user id here has to match the one `review_env` sets.
    """
    root = tmp_path / "repos" / "admin"
    root.mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
    return root.resolve()


@pytest.fixture
def worktree(repos_root) -> Path:
    """A repository inside the repos root with one commit on a `feature` branch."""
    wt = repos_root / "proj"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "AGENTS.md").write_text("# Rules\n\nSpaces, never tabs.\n")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    # Committed, unchanged by the branch, and named by no convention rule — so
    # it reaches no reviewer unless one asks for it. That is what makes it the
    # subject of the `need_files` tests: `AGENTS.md` is already in every prompt
    # as a conventions file, so serving it proves nothing.
    (wt / "helper.py").write_text("SUPPORT_SENTINEL = 'unrequested support module'\n")
    commit(wt, "base")
    run_git(wt, "checkout", "-q", "-b", "feature")
    (wt / "app.py").write_text(
        "def existing():\n    return 1\n\n\ndef added(value):\n    return value * 2\n"
    )
    commit(wt, "app: add a helper")
    return wt


@pytest.fixture
def empty_worktree(repos_root) -> Path:
    """A repository whose `feature` branch adds nothing over `main`."""
    wt = repos_root / "empty"
    wt.mkdir()
    run_git(wt, "init", "-q", "-b", "main", ".")
    (wt / "app.py").write_text("def existing():\n    return 1\n")
    commit(wt, "base")
    run_git(wt, "checkout", "-q", "-b", "feature")
    return wt


@pytest.fixture
def review_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "framework.db"
    db.init_db(path)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(path))
    return path


@pytest.fixture
def task_row(review_db):
    """A real `tasks` row, because `code_review_calls` has a FK against it."""
    with db.get_db(review_db) as conn:
        row = conn.execute(
            "INSERT INTO tasks (prompt, user_id, source_type, status) "
            "VALUES ('review me', 'admin', 'cli', 'running') RETURNING id"
        ).fetchone()
        conn.commit()
        return int(row["id"])


@pytest.fixture
def review_env(monkeypatch, task_row):
    monkeypatch.setenv("ISTOTA_USER_ID", "admin")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_row))
    return task_row


# --------------------------------------------------------------------------
# Stub brain
# --------------------------------------------------------------------------


@dataclass
class StubResult:
    success: bool = True
    result_text: str = ""
    stop_reason: str = "completed"
    usage: object | None = None
    model_used: str = ""
    # Mirrors BrainResult. A double that drifts from the contract it stands in
    # for stops testing the caller and starts testing the double.
    brain_kind: str = ""
    actions_taken: object | None = None
    execution_trace: object | None = None
    partial_text: str | None = None


@dataclass
class StubBrain:
    """A brain that answers from a per-agent script and records what it saw."""

    replies: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    timeouts: list = field(default_factory=list)
    # Whether each call asked the brain to stream. Recorded rather than assumed:
    # the non-streaming path discards a timed-out call's usage and its partial
    # text, so which path the reviewer takes is a property of the CLI worth
    # pinning (ISSUE-448).
    streaming: list = field(default_factory=list)
    # Wall time one call burns, for the tests that drive a budget to exhaustion.
    delay: float = 0.0

    def resolve_model_name(self, name: str) -> str:
        return f"resolved/{name}"

    def execute(self, req):
        if self.delay:
            time.sleep(self.delay)
        # Which reviewer this is, read off the prompt the engine built. The
        # brain has no other way to tell them apart, and asserting on it here
        # is what proves the CLI routed each agent to its own model.
        agent = "bughunt" if "skeptical bug-hunter" in req.prompt else "conformance"
        self.calls.append(agent)
        self.prompts.append(req.prompt)
        self.timeouts.append(req.timeout_seconds)
        self.streaming.append(req.streaming)
        script = self.replies.get(agent, [])
        if not script:
            return StubResult(result_text='{"findings": []}')
        reply = script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, StubResult):
            return reply
        return StubResult(result_text=reply)


def findings_json(*findings) -> str:
    return json.dumps({"findings": list(findings)})


def finding(severity="high", file="app.py", line=4, claim="a defect"):
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "claim": claim,
        "evidence": "observed",
        "action": "fix it",
    }


@pytest.fixture
def stub_brain(monkeypatch):
    brain = StubBrain()
    monkeypatch.setattr("istota.brain.make_brain", lambda cfg: brain)
    monkeypatch.setattr(
        "istota.brain.primary_brain_unavailable", lambda cfg: (True, "")
    )
    monkeypatch.setattr(
        "istota.brain.report_brain_result", lambda result, cfg, **kwargs: None
    )
    return brain


@pytest.fixture
def no_brain(monkeypatch):
    """Every guard test installs this: constructing a brain is a test failure."""

    def _explode(cfg):
        raise AssertionError("make_brain was called after a guard should have refused")

    monkeypatch.setattr("istota.brain.make_brain", _explode)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@pytest.fixture
def developer_config(tmp_path, monkeypatch):
    """A Config with `developer` enabled and review defaults, installed as `load_config`."""

    def _make(**review_overrides):
        cfg = Config(
            db_path=tmp_path / "framework.db",
            temp_dir=tmp_path / "temp",
        )
        cfg.developer = DeveloperConfig(
            enabled=True,
            # Read back from the environment on purpose: the `worktree` fixture
            # (line 87) sets `DEVELOPER_REPOS_DIR` to the tmp root it built, and
            # `_make` has no other way to reach it. Not an ambient read despite
            # appearances — `test_repos_root_missing_from_the_environment_is_skipped`
            # clears it deliberately, and the ISSUE-301 scrub runs before the
            # `worktree` fixture, so what lands here is always the test's own.
            repos_dir=str(os.environ.get("DEVELOPER_REPOS_DIR", "")),
            review=ReviewConfig(**review_overrides),
        )
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        return cfg

    return _make


class TestReviewConfigParsing:
    def test_block_parses(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\n"
            "enabled = true\n"
            'repos_dir = "/srv/repos"\n'
            'author_credit = "Co-Authored-By: Bot <bot@example.invalid>"\n'
            "\n"
            "[developer.review]\n"
            "enabled = false\n"
            'conformance_model = "fast:low"\n'
            'bughunt_model = "smart:high"\n'
            "both_agents_threshold_lines = 42\n"
            'boundary_patterns = ["auth", "billing"]\n'
            "max_diff_chars = 1234\n"
            "max_calls_per_task = 3\n"
            "timeout_seconds = 90\n"
        )
        cfg = load_config(path)
        review = cfg.developer.review
        assert review.enabled is False
        assert review.conformance_model == "fast:low"
        assert review.bughunt_model == "smart:high"
        assert review.both_agents_threshold_lines == 42
        assert review.boundary_patterns == ["auth", "billing"]
        assert review.max_diff_chars == 1234
        assert review.max_calls_per_task == 3
        assert review.timeout_seconds == 90

    def test_defaults_hold_when_the_block_is_absent(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[developer]\nenabled = true\nrepos_dir = "/srv/repos"\n')
        review = load_config(path).developer.review
        assert review.enabled is True
        assert review.max_calls_per_task == 8
        assert review.both_agents_threshold_lines == 150
        assert "auth" in review.boundary_patterns

    def test_unknown_key_is_ignored_rather_than_fatal(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            "[developer.review]\nno_such_key = 7\nmax_calls_per_task = 2\n"
        )
        review = load_config(path).developer.review
        assert review.max_calls_per_task == 2

    def test_author_credit_is_parsed(self, tmp_path):
        """Declared on the dataclass and by the env spec, but never read from TOML.

        The `commit` skill makes this the one permitted commit trailer, so a
        silently-dead field would ship a rule nothing can satisfy.
        """
        path = tmp_path / "config.toml"
        path.write_text(
            "[developer]\nenabled = true\n"
            'author_credit = "Co-Authored-By: Bot <bot@example.invalid>"\n'
        )
        cfg = load_config(path)
        assert cfg.developer.author_credit == "Co-Authored-By: Bot <bot@example.invalid>"


# --------------------------------------------------------------------------
# The call counter
# --------------------------------------------------------------------------


class TestCallCounterHelpers:
    def test_unknown_task_reads_zero(self, review_db, task_row):
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, task_row) == 0

    def test_increment_returns_the_new_count(self, review_db, task_row):
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_increment(conn, task_row) == 1
            assert db.code_review_calls_increment(conn, task_row) == 2
            assert db.code_review_calls_get(conn, task_row) == 2

    def test_a_multi_round_charge_lands_in_one_statement(self, review_db, task_row):
        """A review whose reviewers took the `need_files` round trip spent two
        model rounds. Charging both in one upsert is what keeps the guarantee
        that two concurrent reviews cannot interleave into a single increment."""
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_increment(conn, task_row, 2) == 2
            assert db.code_review_calls_increment(conn, task_row, 2) == 4

    def test_the_cascade_is_decorative_like_every_other_fk_here(
        self, review_db, task_row
    ):
        """`PRAGMA foreign_keys` is never enabled on these connections, so the
        `ON DELETE CASCADE` on `code_review_calls` does not fire — matching every
        other FK in `db.py`, each annotated the same way. Pinned because the
        docstring used to claim the opposite, and a test that switched the pragma
        on itself would have validated a behaviour production never has.
        """
        with db.get_db(review_db) as conn:
            db.code_review_calls_increment(conn, task_row)
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_row,))
            conn.commit()
            assert db.code_review_calls_get(conn, task_row) == 1


# --------------------------------------------------------------------------
# Guards — none of these may construct a brain
# --------------------------------------------------------------------------


class TestGuards:
    def test_developer_disabled(
        self, capsys, tmp_path, monkeypatch, worktree, review_env, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=False, repos_dir=str(worktree.parent))
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["reason"] == "developer_disabled"

    def test_repos_dir_unset(
        self, capsys, tmp_path, monkeypatch, worktree, review_env, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=True, repos_dir="")
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["reason"] == "repos_dir_unset"

    def test_review_disabled_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        """An operator switch is a state of the deployment, not of the diff.

        An `error` blocks the push, so filing it here would mean a deployment
        that deliberately turned review off could never land anything. The
        config block shipped alongside says as much: "false disables the CLI;
        the workflow then reports 'review unavailable' and lands anyway".
        """
        developer_config(enabled=False)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_disabled"

    def test_repos_root_missing_from_the_environment_is_skipped(
        self, capsys, monkeypatch, tmp_path, review_env, developer_config, no_brain
    ):
        """`repos_dir` in config and `DEVELOPER_REPOS_DIR` in the environment can
        disagree: the variable is injected for *authorized* skills only, so a
        deployment with `[developer]` configured but no resolved credential has
        the config key and not the variable. Reporting that as
        `path_not_allowed` would blame the caller's path and block the push for
        something no amount of not-pushing fixes.
        """
        cfg = developer_config()
        cfg.developer.repos_dir = str(tmp_path / "repos")
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        code, envelope = drive(capsys, "run", "--worktree", str(tmp_path / "repos/x"))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "repos_root_unavailable"

    def test_non_admin_refused(
        self, capsys, monkeypatch, worktree, review_env, developer_config, no_brain
    ):
        cfg = developer_config()
        cfg.admin_users = {"someone-else"}
        monkeypatch.setenv("ISTOTA_USER_ID", "nonadmin")
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 1
        assert envelope["reason"] == "not_admin"

    def test_admin_check_fails_open_with_no_admins_file(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Matches the sandbox bind exactly: an empty admin set binds repos_dir
        for everyone, so refusing here would deny a worktree the deployment
        already handed out. `is_shared_kv_writer` deliberately does the
        opposite; the two must not be collapsed."""
        cfg = developer_config()
        assert cfg.admin_users == set()
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "ok"

    def test_worktree_outside_repos_dir(
        self, capsys, tmp_path, worktree, review_env, developer_config, no_brain
    ):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(outside))
        assert code == 1
        assert envelope["reason"] == "path_not_allowed"

    def test_symlink_out_of_repos_dir(
        self, capsys, tmp_path, repos_root, review_env, developer_config, no_brain
    ):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = repos_root / "sneaky"
        link.symlink_to(outside, target_is_directory=True)
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(link))
        assert code == 1
        assert envelope["reason"] == "path_not_allowed"

    def test_tmux_brain_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, no_brain
    ):
        """A tmux deployment has no text-only path at all. Reporting it as an
        error would block every push on a deployment that can never review."""
        cfg = developer_config()
        cfg.brain.kind = "tmux_claude"
        code, envelope = drive(capsys, "run", "--worktree", str(worktree))
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "brain_unsupported"

    def test_a_refused_run_does_not_increment_the_counter(
        self, capsys, tmp_path, monkeypatch, worktree, review_env, review_db, no_brain
    ):
        cfg = Config(db_path=tmp_path / "db", temp_dir=tmp_path / "t")
        cfg.developer = DeveloperConfig(enabled=False, repos_dir=str(worktree.parent))
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
        drive(capsys, "run", "--worktree", str(worktree))
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 0


# --------------------------------------------------------------------------
# The brain seam
# --------------------------------------------------------------------------


class TestReviewRun:
    def test_happy_path_envelope(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [
            findings_json(finding(severity="must-fix", line=4, claim="no test"))
        ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--intent", "add a helper",
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["agents"] == ["conformance"]
        assert envelope["range"] == "main...HEAD"
        assert envelope["sizing_reason"]
        assert envelope["counts"]["must-fix"] == 1
        assert envelope["counts"]["total"] == 1
        assert envelope["findings"][0]["file"] == "app.py"
        assert envelope["findings"][0]["sources"] == ["conformance"]
        assert envelope["notice"]
        assert envelope["partial"] is False

    def test_intent_reaches_the_prompt_and_the_model_is_resolved(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--intent", "add a doubling helper",
        )
        assert "add a doubling helper" in stub_brain.prompts[0]
        assert "## Diff" in stub_brain.prompts[0]

    def test_effort_modifier_is_split_off_rather_than_swallowed(
        self, capsys, worktree, review_env, developer_config, stub_brain, monkeypatch
    ):
        """`resolve_model_name` strips a `:effort` tail and keeps only the base,
        so a config of `smart:high` handed to it whole runs at default effort
        and silently ignores the operator's setting."""
        seen = {}

        def _capture(req):
            seen["model"] = req.model
            seen["effort"] = req.effort
            return StubResult(result_text='{"findings": []}')

        monkeypatch.setattr(StubBrain, "execute", lambda self, req: _capture(req))
        developer_config(conformance_model="general:medium")
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert seen["model"] == "resolved/general"
        assert seen["effort"] == "medium"

    def test_both_agents_when_forced(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [findings_json(finding(line=4))]
        stub_brain.replies["bughunt"] = [findings_json(finding(line=4))]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert sorted(envelope["agents"]) == ["bughunt", "conformance"]
        # Merged by (file, line), so one entry carrying both sources.
        assert len(envelope["findings"]) == 1
        assert envelope["findings"][0]["sources"] == ["bughunt", "conformance"]

    def test_empty_diff_is_ok_and_costs_nothing(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["findings"] == []
        assert envelope["notice"]
        assert stub_brain.calls == []

    def test_breaker_open_skips_without_calling(
        self, capsys, worktree, review_env, developer_config, stub_brain, monkeypatch
    ):
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable",
            lambda cfg: (False, "usage_limit"),
        )
        developer_config()
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "brain_unavailable"
        assert stub_brain.calls == []

    def test_breaker_skip_does_not_increment(
        self, capsys, worktree, review_env, developer_config, stub_brain,
        review_db, monkeypatch,
    ):
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable", lambda cfg: (False, "usage_limit")
        )
        developer_config()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 0

    def test_second_agent_failing_leaves_the_first_intact(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [findings_json(finding(claim="real"))]
        stub_brain.replies["bughunt"] = [RuntimeError("api exploded")]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["status"] == "ok"
        assert envelope["partial"] is True
        assert "bughunt" in envelope["partial_reason"]
        assert envelope["agents"] == ["conformance"]
        assert len(envelope["findings"]) == 1

    def test_malformed_once_then_good_is_one_round(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config()
        stub_brain.replies["conformance"] = [
            "I would rather explain this in prose.",
            findings_json(finding()),
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"
        assert len(envelope["findings"]) == 1
        assert stub_brain.calls == ["conformance", "conformance"]
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_malformed_twice_is_skipped_not_errored_and_carries_the_raw_output(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A bad *response* is not a bad request. Nothing about the diff, the
        range or the paths caused it, and no change to any of them fixes it —
        so blocking the push on it strands finished work for a reason the
        branch cannot answer for. A broken adapter did exactly that on
        2026-08-21: every review on the deployment came back malformed."""
        developer_config()
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "malformed_output"
        assert "not json" in envelope["error"]

    def test_every_reviewer_failing_its_call_is_skipped_not_errored(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The other half of the same block. A reviewer whose call never
        returned is the degraded brain `skipped` already exists for; it reached
        the caller as `error` only because it shared a return with the
        malformed path."""
        developer_config()
        for agent in ("conformance", "bughunt"):
            # One reply each: `_run_agent` returns on `not reply.ok` without
            # retrying, so a second would be dead setup claiming a retry exists.
            stub_brain.replies[agent] = [
                StubResult(success=False, stop_reason="api_error")
            ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "review_failed"
        assert "api_error" in envelope["error"]
        # Both really ran. Conformance failing alone reaches the same reason, so
        # without this the `--agents both` argument carries no weight.
        assert sorted(stub_brain.calls) == ["bughunt", "conformance"]

    def test_the_error_quotes_what_the_reviewer_actually_said(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`skill.md` promises the reading model the head of the reviewer's own
        output on a failed review, and the code dropped `result_text`, so only
        the `stop_reason` slug arrived. Every cause — no credential, no route,
        an unknown model — is `error`, and the sentence the CLI exits on is the
        one thing that tells them apart."""
        developer_config()
        for agent in ("conformance", "bughunt"):
            stub_brain.replies[agent] = [
                StubResult(
                    success=False,
                    stop_reason="error",
                    result_text="Not logged in \u00b7 Please run /login\n",
                )
            ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["reason"] == "review_failed"
        assert "Not logged in" in envelope["error"]
        # One line: the envelope is JSON a model reads, not a log.
        assert "\n" not in envelope["error"]

    def test_a_reviewer_that_said_nothing_still_gets_a_usable_error(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        for agent in ("conformance", "bughunt"):
            stub_brain.replies[agent] = [
                StubResult(success=False, stop_reason="timeout", result_text="")
            ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert "timeout" in envelope["error"]
        assert not envelope["error"].rstrip().endswith(":")

    def test_a_reviewer_that_said_far_too_much_is_capped(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        for agent in ("conformance", "bughunt"):
            stub_brain.replies[agent] = [
                StubResult(
                    success=False, stop_reason="error", result_text="x" * 5000,
                )
            ]
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert code == 0
        assert envelope["error"].count("x") == 2 * code_review._ERROR_TEXT_CHARS

    def test_a_request_fault_inside_a_reviewer_still_blocks_the_push(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """`_one`'s catch-all sits between a containment refusal and a push. It
        was safe by accident while every all-failed round was `error`; once that
        became `skipped` a `ReviewError` caught there would have told the
        workflow to land a branch whose worktree reaches outside the allowed
        roots. Nothing raises there today — this pins the classification so a
        future unwrapped raiser fails closed."""
        from istota.skills.code_review import engine

        developer_config()
        monkeypatch.setattr(
            engine, "_run_agent",
            lambda *a, **k: (_ for _ in ()).throw(
                engine.ReviewError(
                    "repo reaches outside DEVELOPER_REPOS_DIR",
                    reason="git_dir_not_allowed",
                )
            ),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 1
        assert envelope["status"] == "error"
        # The ReviewError's own slug, not a category: the workflow branches on it.
        assert envelope["reason"] == "git_dir_not_allowed"
        assert "reaches outside" in envelope["error"]

    def test_a_skip_that_spent_model_calls_says_so(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`rounds` is what separates a skip that burned invocations from one
        that refused before spending any. Both are `status: skipped` with an
        empty `findings`, and a caller deciding whether re-running is free
        cannot tell them apart otherwise."""
        developer_config()
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        _, spent = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert spent["status"] == "skipped"
        assert spent["rounds"] == 1

        developer_config(enabled=False)
        _, refused = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert refused["status"] == "skipped"
        assert refused["reason"] == "review_disabled"
        assert refused.get("rounds", 0) == 0

    def test_an_all_failed_review_carries_its_own_notice(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The ordinary notice opens by talking about findings, and there are
        none here. What this envelope carries is raw reviewer output in `error`,
        on a status whose instruction is to land the work and name the reason —
        so the untrusted-input warning has to cover that field, not findings."""
        developer_config()
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        _, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert envelope["findings"] == []
        assert "error` field quotes raw reviewer output" in envelope["notice"]

    def test_a_round_that_spent_calls_and_failed_still_charges_the_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Otherwise a reviewer that reliably answers in prose loops forever:
        the round returns nothing usable, the workflow re-runs, and the cap that
        is supposed to stop the spend never moves because no round ever
        "succeeded". The charge is what bounds it, not the exit code."""
        developer_config()
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_a_well_shaped_envelope_of_unusable_findings_is_not_a_clean_review(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`parse_findings` drops any item naming no file, so a must-fix without
        one empties to `[]` and would otherwise be reported as `ok` with zero
        findings — indistinguishable from a reviewer that found nothing. The
        prompt asks explicitly for findings the reviewer could not verify, which
        is exactly where a missing `file` comes from."""
        developer_config()
        stub_brain.replies["conformance"] = [
            json.dumps({"findings": [{"severity": "must-fix", "claim": "secret leaked"}]}),
            findings_json(finding(severity="must-fix", file="app.py", line=4)),
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        # Retried rather than accepted, and the retry's usable finding survives.
        assert stub_brain.calls == ["conformance", "conformance"]
        assert code == 0
        assert envelope["counts"]["must-fix"] == 1

    def test_partly_unusable_findings_are_counted_not_swallowed(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config()
        stub_brain.replies["conformance"] = [
            json.dumps({
                "findings": [
                    finding(severity="high", file="app.py", line=4),
                    {"severity": "must-fix", "claim": "no file named"},
                ]
            })
        ]
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["counts"]["total"] == 1
        assert envelope["dropped_findings"] == 1

    def test_an_empty_range_is_flagged_machine_readably(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """A gate reading `status == "ok" and counts["must-fix"] == 0` would
        otherwise take an unreviewed empty range for a clean review; prose in
        `notice` is not something a consumer branches on."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )
        assert envelope["empty"] is True

    def test_every_envelope_carries_the_same_keys(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A consumer must be able to read `findings` or `counts` without first
        branching on `status`, and the all-reviewers-failed path — the only one
        that embeds raw model text — must carry the untrusted-input notice like
        the rest."""
        developer_config()
        _, ok = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        stub_brain.replies["conformance"] = ["not json", "still not json"]
        _, err = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        for key in ("findings", "counts", "partial", "notice", "range", "agents"):
            assert key in ok, key
            assert key in err, key

    def test_bad_range_returns_git_stderr(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Git's own diagnosis has to survive into the envelope. A generic "bad
        range" costs the caller a round trip working out which ref was wrong."""
        developer_config()
        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--range", "no-such-ref...HEAD"
        )
        assert code == 1
        assert envelope["status"] == "error"
        assert "no-such-ref" in envelope["error"]

    def test_an_unexpected_exception_still_produces_an_envelope(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """The facade contract is one line of JSON and an exit code, and the
        scheduler sniffs stdout for that shape. The engine shells out through
        `subprocess.Popen`, which raises OSError and friends outside
        `ReviewError`."""
        developer_config()
        monkeypatch.setattr(
            "istota.skills.code_review.engine.run_review",
            lambda *a, **k: (_ for _ in ()).throw(OSError("git vanished")),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 1
        assert envelope["status"] == "error"
        assert envelope["reason"] == "internal_error"
        assert "git vanished" in envelope["error"]


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------


class TestCallCap:
    def test_runs_up_to_the_cap_then_degrade_to_skipped(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config(max_calls_per_task=2)
        for _ in range(2):
            code, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main"
            )
            assert code == 0
            assert envelope["status"] == "ok"

        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0, "the cap degrades rather than blocking a task that already worked"
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "call_cap"
        assert envelope["calls_used"] == 2
        assert envelope["max_calls"] == 2

    def test_the_counter_is_read_back_from_the_database(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Not from a file under ISTOTA_DEFERRED_DIR, which the model can write."""
        developer_config(max_calls_per_task=5)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2

    def test_at_the_cap_no_model_call_is_made(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config(max_calls_per_task=1)
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        stub_brain.calls.clear()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert stub_brain.calls == []

    def test_a_cap_of_zero_permits_nothing_rather_than_everything(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`max_need_files = 0` disables its feature, so a neighbouring knob
        where 0 silently means "unlimited" is a trap — and on a spend control
        the expensive reading is the wrong one to guess at. An operator who
        wants the feature off has `enabled = false`."""
        developer_config(max_calls_per_task=0)
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "skipped"
        assert envelope["reason"] == "call_cap"
        assert stub_brain.calls == []

    def test_an_unreadable_budget_does_not_sink_the_review(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """Losing the cap check is a bounded cost risk; refusing the review turns
        a transient database lock into a blocked push."""
        developer_config()
        monkeypatch.setattr(
            db, "code_review_calls_get",
            lambda conn, task_id: (_ for _ in ()).throw(RuntimeError("database is locked")),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"

    def test_an_unrecordable_charge_does_not_lose_a_paid_for_review(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """The model calls are already paid for by the time the counter is
        written. A traceback here would violate the facade contract and hand the
        caller nothing at all for the money."""
        developer_config()
        stub_brain.replies["conformance"] = [findings_json(finding())]
        monkeypatch.setattr(
            db, "code_review_calls_increment",
            lambda conn, task_id, count=1: (_ for _ in ()).throw(
                RuntimeError("disk I/O error")
            ),
        )
        code, envelope = drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert code == 0
        assert envelope["status"] == "ok"
        assert len(envelope["findings"]) == 1


# --------------------------------------------------------------------------
# The timeout budget
# --------------------------------------------------------------------------


MIN = code_review.MIN_AGENT_TIMEOUT_SECONDS


class TestTimeoutBudget:
    def test_each_agent_gets_the_configured_timeout(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Both agents run concurrently, so wall time is max(t1, t2) and each
        gets the whole `timeout_seconds` rather than half of it."""
        developer_config(timeout_seconds=45)
        drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert stub_brain.timeouts == [45, 45]

    def test_a_budget_over_the_proxy_ceiling_is_clamped_and_warned_about(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The proxy kills the command at `security.skill_proxy_timeout`. Warning
        and then handing each agent the full budget anyway describes the problem
        without avoiding it: every review would be killed half-finished having
        paid for its agents. Shrinking is the only outcome that returns
        anything."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        with caplog.at_level("WARNING"):
            drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert any("skill_proxy_timeout" in r.message for r in caplog.records)
        assert stub_brain.timeouts == [300 - code_review.RESERVED_SECONDS]

    def test_the_ceiling_warning_fires_even_when_the_run_is_capped(
        self, capsys, caplog, worktree, review_env, developer_config,
        stub_brain, review_db,
    ):
        """A warning that only fires on the runs that were going to work anyway
        is not much of a warning."""
        cfg = developer_config(timeout_seconds=400, max_calls_per_task=1)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        caplog.clear()
        with caplog.at_level("WARNING"):
            _, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main"
            )
        assert envelope["reason"] == "call_cap"
        assert any("skill_proxy_timeout" in r.message for r in caplog.records)

    def test_the_envelope_reports_the_budget_each_agent_actually_got(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A caller reporting a review has to be able to say what it ran on.
        Unclamped, the effective budget is the configured one and `clamped` is
        false — the field is present on every run, not only on the short ones,
        because a reader who has to infer "not clamped" from a missing key is
        back to guessing."""
        developer_config(timeout_seconds=45)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_configured"] == 45
        assert envelope["agent_timeout_clamped"] is False

    def test_a_clamped_budget_says_so_in_the_envelope(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The clamp warns into the daemon journal, which the model that invoked
        the CLI cannot read. Without this the only difference between a review
        that had its whole budget and one cut to a third of it is in a log the
        caller has no route to — same shape, same `status: ok`, quietly less
        thinking behind the findings."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        effective = 300 - code_review.RESERVED_SECONDS
        assert envelope["agent_timeout_seconds"] == effective
        assert envelope["agent_timeout_configured"] == 400
        assert envelope["agent_timeout_clamped"] is True
        assert stub_brain.timeouts == [effective]

    def test_an_empty_range_still_carries_the_budget_fields(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """`run_review` promises every return path the same key set, and the
        empty-range path returns before any reviewer is sized. A consumer that
        reads the budget without first branching on `empty` must not hit a
        KeyError."""
        developer_config(timeout_seconds=45)
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main",
        )
        assert envelope["empty"] is True
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_clamped"] is False

    def test_a_clamp_that_changes_nothing_does_not_claim_a_short_review(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A budget already at the floor trips the ceiling arithmetic without
        losing a second. `clamped` answers "did this review run short", not "was
        the branch taken", so it stays false."""
        cfg = developer_config(timeout_seconds=MIN)
        cfg.security.skill_proxy_timeouts = {"code_review": MIN + 50}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == MIN
        assert envelope["agent_timeout_configured"] == MIN
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [MIN]

    def test_the_clamp_never_raises_a_budget_that_already_fit(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`max(floor, ceiling - allowance)` on its own turned a configured 25s
        into 30s — a "clamp" that made the fit worse, under a ceiling the
        original 25s already fit. The floor may still raise the budget, but the
        ceiling arithmetic must only ever lower it, and a budget that came out
        above the configured one is not a short review."""
        cfg = developer_config(timeout_seconds=MIN - 5)
        cfg.security.skill_proxy_timeouts = {"code_review": MIN + 50}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == MIN - 5
        assert envelope["agent_timeout_configured"] == MIN - 5
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [MIN - 5]

    def test_a_nonpositive_budget_is_floored_rather_than_passed_to_the_brain(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """Nothing in the config loader floors `timeout_seconds`, and the brains
        disagree about what a 0 means: the native one runs unbounded until the
        proxy kills the command, `claude_code` hands it to a `threading.Timer`
        and kills each agent at once. Neither is a review, and before this the
        envelope reported the deployment had got exactly what it asked for."""
        developer_config(timeout_seconds=0)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert stub_brain.timeouts == [MIN]
        assert envelope["agent_timeout_seconds"] == MIN
        assert envelope["agent_timeout_configured"] == 0
        assert envelope["agent_timeout_clamped"] is False

    def test_a_ceiling_too_tight_for_the_floor_says_so_on_its_own_line(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The clamp cannot deliver a fit under a ceiling smaller than the
        assembly allowance plus the floor, so the proxy kills the command with
        empty stdout and the caller gets no envelope at all. The log is the only
        place that deployment can say what happened, so it gets its own line
        rather than the ordinary "being given less" warning."""
        cfg = developer_config(timeout_seconds=120)
        cfg.security.skill_proxy_timeouts = {
            "code_review": code_review.ASSEMBLY_ALLOWANCE_SECONDS
        }
        with caplog.at_level("WARNING"):
            drive(capsys, "run", "--worktree", str(worktree), "--base", "main")
        assert any("cannot fit a review at all" in r.message for r in caplog.records)
        assert stub_brain.timeouts == [MIN]

    def test_a_nonpositive_proxy_ceiling_does_not_blame_a_clamp(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A negative ceiling is truthy. Read as a real ceiling it pinned every
        review to the floor and reported a clamp whose stated cause never
        happened; the proxy surfaces the misconfiguration itself by killing the
        command immediately."""
        cfg = developer_config(timeout_seconds=45)
        cfg.security.skill_proxy_timeout = -1
        cfg.security.skill_proxy_timeouts = {}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 45
        assert envelope["agent_timeout_clamped"] is False


class TestTheShippedDefaultsFitAReviewer:
    """ISSUE-448: 240s was the largest per-agent budget the shipped pair allowed,
    and bughunt — a `smart:high` reviewer, sized onto the *large* diffs only —
    died at exactly that number on every real diff, twice out of two.

    The arithmetic that produced 240 had two independent faults. The ceiling was
    `security.skill_proxy_timeout`, one global applied to every proxied skill
    call, so the only lever was a limit on everything else too. And the reserve
    subtracted from it was a 60-second guess against about one second of measured
    assembly, while the 10 seconds of join slack it did not model pushed the real
    wall bound past the ceiling anyway.
    """

    def test_a_default_deployment_gives_a_reviewer_its_whole_configured_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The headline regression. Nothing here overrides anything: this is what
        an operator who sets nothing gets, and before the fix it was 240."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        configured = ReviewConfig().timeout_seconds
        assert configured > 240, (
            "the code default must itself be more than the budget bughunt was "
            "measured dying at, or a bare install reproduces the bug"
        )
        assert envelope["agent_timeout_seconds"] == configured
        assert envelope["agent_timeout_clamped"] is False
        assert stub_brain.timeouts == [configured]

    def test_the_ceiling_that_binds_a_review_is_the_review_s_own(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`security.skill_proxy_timeout` is one number for every proxied skill,
        and `code_review` is the only one that drives model calls. A per-skill
        entry is what lets the review have minutes without handing them to
        everything else — so the global must not be what bounds it."""
        cfg = developer_config(timeout_seconds=400)
        cfg.security.skill_proxy_timeout = 300
        cfg.security.skill_proxy_timeouts = {"code_review": 540}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == 400
        assert envelope["agent_timeout_clamped"] is False

    def test_a_table_naming_another_skill_leaves_the_review_ceiling_alone(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A `dict` config field replaces its default rather than merging, so a
        shipped `code_review` entry would be dropped by an operator who wrote
        the table to configure something else — taking the ceiling back to the
        global and reproducing ISSUE-448 with only a log line. The shipped
        policy lives in `skill_proxy.DEFAULT_SKILL_TIMEOUTS` for that reason,
        and this is the case that would go red if it moved back."""
        cfg = developer_config()
        cfg.security.skill_proxy_timeout = 300
        cfg.security.skill_proxy_timeouts = {"browse": 90}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_seconds"] == ReviewConfig().timeout_seconds
        assert envelope["agent_timeout_clamped"] is False

    def test_the_clamp_reserves_the_join_slack_it_actually_spends(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`_run_agents` joins its threads at `timeout_seconds + JOIN_SLACK_SECONDS`,
        so the command's true wall bound was always ten seconds past what the
        clamp modelled. Reserving only the assembly allowance meant a budget
        clamped to "just fit" still overran the ceiling by the slack."""
        cfg = developer_config(timeout_seconds=1000)
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        effective = envelope["agent_timeout_seconds"]
        assert effective == 300 - code_review.RESERVED_SECONDS
        assert effective + engine.JOIN_SLACK_SECONDS \
            + code_review.ASSEMBLY_ALLOWANCE_SECONDS <= 300

    def test_an_explicit_timeout_overrides_the_configured_budget(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The escape hatch. Config comes from a deploy, so before this there was
        no way to ask what a reviewer needs on a real diff without one — and
        every attempt cost a `smart:high` call that was then thrown away."""
        developer_config(timeout_seconds=480)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--timeout", "77",
        )
        assert envelope["agent_timeout_override"] == 77
        assert envelope["agent_timeout_seconds"] == 77
        assert stub_brain.timeouts == [77]
        # The deployment's own number stays reported. The flag reaches this CLI
        # from the model's argv and shortens as readily as it lengthens, so a
        # `--timeout 30` that overwrote `agent_timeout_configured` would leave
        # nothing in the envelope saying 480 was asked for.
        assert envelope["agent_timeout_configured"] == 480

    def test_a_run_with_no_flag_reports_no_override(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`None` rather than a repeat of the configured value, so a caller can
        tell "not overridden" from "overridden to the same number"."""
        developer_config(timeout_seconds=480)
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        assert envelope["agent_timeout_override"] is None
        assert envelope["agent_timeout_configured"] == 480

    def test_an_explicit_timeout_is_still_bounded_by_the_ceiling(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A flag that could outrun the proxy would be a way to guarantee the
        failure this issue is about rather than a way to measure it."""
        cfg = developer_config()
        cfg.security.skill_proxy_timeouts = {"code_review": 300}
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--timeout", "9000",
        )
        assert envelope["agent_timeout_override"] == 9000
        assert envelope["agent_timeout_seconds"] == 300 - code_review.RESERVED_SECONDS
        assert envelope["agent_timeout_clamped"] is True

    def test_the_envelope_reports_what_everything_outside_the_agents_cost(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The allowance is a constant and the entry's whole complaint is that
        constants here are not measuring the thing they gate. Reporting the
        measurement is what lets the next reader check 20 against a real diff
        instead of picking another number blind.

        The delay is the discriminating half: a figure that included the agent
        phase would be at least as large as it, so an overhead below it can only
        have come from excluding it.
        """
        developer_config()
        stub_brain.delay = 3.0
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
        )
        overhead = envelope["overhead_seconds"]
        assert isinstance(overhead, (int, float))
        # Non-zero because assembly shells out to git several times, so a flat
        # 0.0 would mean the clock is not running rather than that the work is
        # free — and under the delay because that is the only way it can have
        # excluded the agent phase. Three seconds rather than one: the suite
        # runs `-n auto` and is throughput-bound, so a handful of git spawns
        # under contention can cross a one-second bound and turn the
        # discriminating half of this assertion into a flake.
        assert 0 < overhead < 3.0

    def test_a_lost_reviewer_is_named_in_a_field_a_gate_can_read(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`status: "ok"` with `counts.total: 0` is what a one-agent review
        reported, and a gate reading those two cannot tell it from a clean
        two-agent one. `partial_reason` said which reviewer was lost, in prose;
        substring-matching prose to find out whether the correctness reviewer
        ran is not something a caller should have to do."""
        developer_config()
        stub_brain.replies = {
            "conformance": [findings_json()],
            "bughunt": [StubResult(
                success=False,
                result_text="Claude Code timed out after 240s",
                stop_reason="timeout",
            )],
        }
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert envelope["status"] == "ok"
        assert envelope["agents"] == ["conformance"]
        assert envelope["agents_failed"] == ["bughunt"]
        assert envelope["partial"] is True

    def test_a_whole_review_reports_no_lost_reviewers(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The field is present on every run, not only the short ones — a reader
        inferring "nothing was lost" from a missing key is back to guessing."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )
        assert envelope["agents_failed"] == []

    def test_the_reviewer_calls_stream(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """On the non-streaming path a timeout is a `TimeoutExpired` out of
        `subprocess.run`, and `_execute_simple_once` reads its usage from an
        accounting dict only filled after the process exits and its output
        parses. So the most expensive call the deployment makes was also the one
        it never billed, and `persist_brain_usage` returns immediately on a
        `None` usage. The streaming path stamps usage at a single exit from the
        per-request frames it has already parsed, and hands back `partial_text`."""
        developer_config()
        drive(capsys, "run", "--worktree", str(worktree), "--base", "main",
              "--agents", "both")
        assert stub_brain.streaming == [True, True]

    def test_what_a_timed_out_reviewer_wrote_reaches_the_log(
        self, capsys, caplog, worktree, review_env, developer_config, stub_brain
    ):
        """The half of the streaming switch that has an observable outcome here.
        `stub_brain.streaming` pins the argument; this pins what the argument is
        for — a `partial_text` on the result is now read and kept, where before
        the field was ignored by this caller because the non-streaming path
        never populated it.

        Not in the envelope: a reviewer answers in one JSON blob at the end, so
        a timed-out one has written prose, and prose in a findings-adjacent
        field is a diagnostic wearing the wrong label."""
        developer_config()
        stub_brain.replies = {
            "conformance": [StubResult(
                success=False,
                result_text="Claude Code timed out after 480s",
                stop_reason="timeout",
                partial_text="I was partway through checking the migration when",
            )],
        }
        with caplog.at_level("INFO"):
            _, envelope = drive(
                capsys, "run", "--worktree", str(worktree), "--base", "main",
            )
        assert envelope["status"] == "skipped"
        assert any(
            "checking the migration" in r.getMessage() for r in caplog.records
        )

    def test_an_empty_range_carries_the_new_fields_too(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """`run_review` promises every return path the same key set, and the
        empty-range path returns before any reviewer is sized — so it is the one
        where `agent_seconds` is still zero when the overhead is stamped, and
        the one a consumer reading either field without branching on `empty`
        would hit a KeyError on."""
        developer_config()
        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main",
        )
        assert envelope["empty"] is True
        assert envelope["agents_failed"] == []
        assert isinstance(envelope["overhead_seconds"], (int, float))
        assert envelope["overhead_seconds"] > 0


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def drive(capsys, *argv) -> tuple[int, dict]:
    """Run `main(argv)`, returning `(exit_code, parsed envelope)`.

    The facade contract is one line of JSON on stdout and an exit code, so the
    tests read exactly what the workflow reads.
    """
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        code_review.main(list(argv))
    out = capsys.readouterr().out.strip()
    assert out, "the CLI printed nothing"
    envelope = json.loads(out.splitlines()[-1])
    code = excinfo.value.code
    return (0 if code is None else int(code)), envelope


# --------------------------------------------------------------------------
# The need_files round trip
# --------------------------------------------------------------------------


def need_files_json(*paths, findings=()) -> str:
    return json.dumps({"findings": list(findings), "need_files": list(paths)})


class TestNeedFilesRoundTrip:
    """A reviewer may name files once, and exactly once.

    The round trip is the cheapest way to close the gap a text-only reviewer
    has, and it is also the one place a *model* picks which blob the daemon
    reads. Three properties hold it down, and each has a test here: paths are
    served from inside the worktree only, the cap bounds how many, and the
    re-invocation is a single extra round charged to the task's budget rather
    than a loop that can spend it.
    """

    def test_a_request_produces_a_second_call_carrying_the_bodies(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py"),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["status"] == "ok"
        assert stub_brain.calls == ["conformance", "conformance"]
        assert "SUPPORT_SENTINEL" in stub_brain.prompts[1], (
            "the second prompt must carry the requested body"
        )
        assert "SUPPORT_SENTINEL" not in stub_brain.prompts[0], (
            "and the first must not, or the round trip served nothing new"
        )
        assert envelope["files_served"] == ["helper.py"]
        assert len(envelope["findings"]) == 1

    def test_the_second_call_keeps_everything_the_first_one_had(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The served files are added to the prompt, not swapped in for it. A
        reviewer re-invoked without the diff would be reviewing nothing."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py"),
            findings_json(finding()),
        ]

        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        first, second = stub_brain.prompts
        assert "def added" in second, "the diff must survive the re-invocation"
        assert "## Diff stat" in second
        assert "Spaces, never tabs." in second, "and so must the conventions"
        assert "no tools" in second.lower()
        assert "SUPPORT_SENTINEL" in second
        # The one thing that is deliberately *not* carried over: a reviewer
        # reading the offer twice has been told it may ask twice.
        assert "need_files" in first
        assert "need_files" not in second.split("## Files you asked for")[0]

    def test_a_path_outside_the_worktree_is_dropped_and_the_rest_served(
        self, capsys, tmp_path, worktree, review_env, developer_config, stub_brain
    ):
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("OUTSIDE_SENTINEL\n")
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("../../outside_secret.txt", "helper.py"),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert "OUTSIDE_SENTINEL" not in stub_brain.prompts[1]
        assert "SUPPORT_SENTINEL" in stub_brain.prompts[1], (
            "one bad path must not sink the rest of the request"
        )
        assert envelope["files_served"] == ["helper.py"]
        assert envelope["files_refused"] == ["../../outside_secret.txt"]

    def test_more_than_the_cap_are_truncated_to_it(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(max_need_files=1)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md", "app.py"),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["files_served"] == ["AGENTS.md"]
        assert envelope["files_refused"] == ["app.py"]

    def test_zero_disables_the_round_trip_entirely(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(max_need_files=0)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md", findings=[finding()]),
            findings_json(finding(claim="never reached")),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance"]
        assert "need_files" not in stub_brain.prompts[0]
        assert envelope["files_served"] == []
        assert len(envelope["findings"]) == 1

    def test_the_re_invocation_counts_as_its_own_round(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Read back from `code_review_calls`, not from the envelope. A round
        trip that charged nothing would be a way to spend past the cap."""
        developer_config(max_need_files=6, max_calls_per_task=8)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md"),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2
        assert envelope["calls_used"] == 2

    def test_a_run_without_a_round_trip_still_charges_one(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        developer_config(max_need_files=6, max_calls_per_task=8)
        stub_brain.replies["conformance"] = [findings_json(finding())]

        drive(capsys, "run", "--worktree", str(worktree), "--base", "main")

        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 1

    def test_one_re_invocation_and_never_a_loop(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The second answer asking again is answered by returning what it has,
        not by serving a third round."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md"),
            need_files_json("app.py", findings=[finding()]),
            findings_json(finding(claim="never reached")),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance", "conformance"]
        assert len(envelope["findings"]) == 1

    def test_the_round_trip_is_not_started_when_the_budget_cannot_pay_for_it(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """At `cap - 1` there is room for this round and not for a second one.
        Offering the round trip anyway would either overshoot the operator's
        budget or refuse a request the prompt had just invited."""
        developer_config(max_need_files=6, max_calls_per_task=2)
        with db.get_db(review_db) as conn:
            db.code_review_calls_increment(conn, review_env)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md", findings=[finding()]),
            findings_json(finding(claim="never reached")),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance"]
        assert "need_files" not in stub_brain.prompts[0]
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2

    def test_a_failed_re_invocation_falls_back_to_the_first_answer(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The first answer is already paid for. Discarding it because the
        optional extra round failed loses a usable review to an improvement."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md", findings=[finding(claim="found early")]),
            StubResult(success=False, stop_reason="timeout"),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["status"] == "ok"
        assert [f["claim"] for f in envelope["findings"]] == ["found early"]
        assert "timeout" in envelope["need_files_note"]

    def test_an_unparseable_re_invocation_falls_back_rather_than_retrying(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md", findings=[finding(claim="found early")]),
            "I have read the files and everything looks fine to me.",
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance", "conformance"]
        assert [f["claim"] for f in envelope["findings"]] == ["found early"]
        assert envelope["need_files_note"]

    def test_each_agent_gets_its_own_round_trip_but_they_share_one_round(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """Two agents re-invoking is one extra wave, not two. A round is a wave
        of calls — that is what makes the default of 8 a budget an operator can
        reason about."""
        developer_config(max_need_files=6, max_calls_per_task=8)
        stub_brain.replies["conformance"] = [
            need_files_json("AGENTS.md"),
            findings_json(finding()),
        ]
        stub_brain.replies["bughunt"] = [
            need_files_json("app.py"),
            findings_json(finding(claim="another defect", line=5)),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )

        assert code == 0
        assert sorted(stub_brain.calls) == [
            "bughunt", "bughunt", "conformance", "conformance",
        ]
        assert envelope["files_served"] == ["AGENTS.md", "app.py"]
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2

    def test_a_malformed_first_answer_still_gets_its_round_trip(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The malformed retry and the round trip are different mechanisms with
        different causes, so one must not consume the other's chance."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            "sorry, here is some prose instead",
            need_files_json("AGENTS.md"),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance"] * 3
        assert len(envelope["findings"]) == 1

    def test_an_empty_range_never_reaches_the_round_trip(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        developer_config(max_need_files=6)

        code, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["empty"] is True
        assert stub_brain.calls == []
        assert envelope["files_served"] == []

    def test_a_raising_re_invocation_falls_back_rather_than_sinking_the_review(
        self, capsys, worktree, review_env, developer_config, stub_brain, review_db
    ):
        """`make_brain` and `brain.execute` raise; neither returns an
        `AgentReply`. `run_review` turns an escaping exception into a *failed
        reviewer*, so without a guard in `_round_trip` an optional extra round
        would take a paid-for `ok` review down to `error` — which the workflow
        reads as "block the push"."""
        developer_config(max_need_files=6, max_calls_per_task=8)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py", findings=[finding(claim="found early")]),
            RuntimeError("brain blew up on the re-invocation"),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["status"] == "ok", "a paid-for review must survive this"
        assert [f["claim"] for f in envelope["findings"]] == ["found early"]
        assert envelope["files_served"] == ["helper.py"]
        assert "RuntimeError" in envelope["need_files_note"]
        # The invocation was made, so it is charged: counting only calls that
        # returned would let a reviewer whose re-invocation always raises spend
        # two invocations for every round it pays for.
        with db.get_db(review_db) as conn:
            assert db.code_review_calls_get(conn, review_env) == 2

    def test_an_ungatherable_request_falls_back_without_a_second_call(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """`serve` runs `git_dir`, which raises `ReviewError` on a repository it
        refuses. That is not a reason to lose the first answer either."""
        from istota.skills.code_review import engine

        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py", findings=[finding(claim="found early")]),
            findings_json(finding(claim="never reached")),
        ]
        monkeypatch.setattr(
            engine, "collect_needed_files",
            lambda *a, **k: (_ for _ in ()).throw(
                engine.ReviewError("gitdir refused", reason="git_dir_not_allowed")
            ),
        )

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["status"] == "ok"
        assert stub_brain.calls == ["conformance"]
        assert [f["claim"] for f in envelope["findings"]] == ["found early"]
        assert "gitdir refused" in envelope["need_files_note"]
        assert envelope["round_trip_refused"] is True

    def test_the_envelope_says_whether_a_wanted_round_was_refused(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """Three states, and prose in `need_files_note` was the only thing that
        told them apart. A caller deciding how much to trust an `unverified`
        finding branches on this: nobody asked, it asked and got its round, or
        it asked and was refused one — and only the middle case cost a call."""
        from istota.skills.code_review import engine

        developer_config(max_need_files=6)

        # Nobody asked.
        stub_brain.replies["conformance"] = [findings_json(finding())]
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )
        assert envelope["round_trip_refused"] is False
        assert envelope["rounds"] == 1

        # Asked, and got the round.
        stub_brain.calls.clear()
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py"),
            findings_json(finding()),
        ]
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )
        assert envelope["round_trip_refused"] is False
        assert envelope["rounds"] == 2

        # Asked, and nothing could be served, so no second call was made.
        stub_brain.calls.clear()
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py"),
            findings_json(finding(claim="never reached")),
        ]
        monkeypatch.setattr(
            engine, "collect_needed_files",
            lambda *a, **k: engine.NeededFiles(refused=["helper.py"]),
        )
        _, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )
        assert envelope["round_trip_refused"] is True
        assert envelope["rounds"] == 1
        assert stub_brain.calls == ["conformance"]

    def test_one_reviewer_refused_and_one_served_reports_both(
        self, capsys, monkeypatch, worktree, review_env, developer_config,
        stub_brain, review_db,
    ):
        """The field is collapsed across agents with `any()`, like
        `files_served` beside it — so on a two-agent review it reads true while
        `rounds` is 2. That is not a contradiction: `rounds` is what the run
        cost, this is whether a round somebody wanted went unbought, and the
        note names which reviewer. Two agents is the ordinary shape on any diff
        over `both_agents_threshold_lines`, so this is the common case."""
        from istota.skills.code_review import engine

        developer_config(max_need_files=6, max_calls_per_task=8)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py"),
            findings_json(finding()),
        ]
        stub_brain.replies["bughunt"] = [
            need_files_json("app.py", findings=[finding(claim="asked in vain")]),
            findings_json(finding(claim="never reached")),
        ]

        # Serve conformance's request and refuse bughunt's, so exactly one
        # reviewer takes its round.
        real = engine.collect_needed_files

        def serve_only_helper(worktree_path, rev, paths, **kwargs):
            if paths == ["app.py"]:
                return engine.NeededFiles(refused=["app.py"])
            return real(worktree_path, rev, paths, **kwargs)

        monkeypatch.setattr(engine, "collect_needed_files", serve_only_helper)

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main",
            "--agents", "both",
        )

        assert code == 0
        assert envelope["status"] == "ok"
        assert sorted(stub_brain.calls) == ["bughunt", "conformance", "conformance"]
        assert envelope["rounds"] == 2, "conformance's round was made and charged"
        assert envelope["round_trip_refused"] is True, "bughunt's was not"
        assert "bughunt" in envelope["need_files_note"]
        assert envelope["files_served"] == ["helper.py"]
        assert envelope["files_refused"] == ["app.py"]

    def test_an_envelope_that_never_reached_a_reviewer_still_carries_the_field(
        self, capsys, empty_worktree, review_env, developer_config, stub_brain
    ):
        """`run_review` promises every return path the same key set. A consumer
        reading this without first branching on `empty` must not hit a
        KeyError."""
        developer_config(max_need_files=6)

        _, envelope = drive(
            capsys, "run", "--worktree", str(empty_worktree), "--base", "main"
        )

        assert envelope["empty"] is True
        assert envelope["round_trip_refused"] is False

    def test_a_bare_string_request_is_accepted_rather_than_retried(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """A reviewer naming one file often writes it bare. Cheaper to accept
        than to spend a round teaching it the list form."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            json.dumps({"findings": [], "need_files": "helper.py"}),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance", "conformance"]
        assert envelope["files_served"] == ["helper.py"]
        assert "SUPPORT_SENTINEL" in stub_brain.prompts[1]

    def test_no_budget_left_in_the_agents_timeout_skips_the_second_call(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The round trip runs against what is left of the agent's own budget,
        never a fresh one, so a reviewer cannot double the wall time by asking
        for files."""
        developer_config(max_need_files=6, timeout_seconds=1)
        stub_brain.delay = 1.1
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py", findings=[finding()]),
            findings_json(finding(claim="never reached")),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance"], "no budget left for a second call"
        assert len(envelope["findings"]) == 1
        assert "budget remained" in envelope["need_files_note"]

    def test_an_empty_second_answer_is_never_a_silent_clean_review(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The dangerous shape: a reviewer answers `{"findings": []}` on its
        re-invocation because it believes it already reported them. Taken at
        face value the envelope is byte-identical to a genuinely clean review —
        `ok`, all counts zero, nothing partial — which is the workflow's signal
        to let the push through. The second answer still wins, because a
        reviewer that read the file may legitimately be retracting; what must
        not happen is the loss going unrecorded."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json(
                "helper.py", findings=[finding(severity="must-fix", claim="found early")]
            ),
            findings_json(),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["counts"]["total"] == 0
        assert envelope["need_files_note"], (
            "a review that lost every finding on its round trip must not read "
            "as a clean one"
        )
        assert "down from 1" in envelope["need_files_note"]

    def test_a_second_answer_that_keeps_its_findings_says_nothing(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """The note is for a loss. An ordinary round trip is silent, or every
        review that used one would read as suspect."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py", findings=[finding(claim="found early")]),
            findings_json(
                finding(claim="found early"),
                # A distinct line, or `merge_findings` folds the two into one by
                # `(file, line)` and the test measures the merge, not the round
                # trip. The engine compares pre-merge counts for the same reason.
                finding(claim="and another", line=5),
            ),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert len(envelope["findings"]) == 2
        assert envelope["need_files_note"] == ""

    def test_a_request_only_answer_is_served_rather_than_retried(
        self, capsys, worktree, review_env, developer_config, stub_brain
    ):
        """`{"need_files": [...]}` with no findings key is the shape the offer
        invites from a reviewer with nothing to report yet. Retrying it as
        malformed would spend a model call teaching it a key it was never told
        was mandatory."""
        developer_config(max_need_files=6)
        stub_brain.replies["conformance"] = [
            json.dumps({"need_files": ["helper.py"]}),
            findings_json(finding()),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert stub_brain.calls == ["conformance", "conformance"], (
            "two calls: the request and its answer, not a malformed retry"
        )
        assert "SUPPORT_SENTINEL" in stub_brain.prompts[1]
        assert len(envelope["findings"]) == 1

    def test_an_unreadable_budget_does_not_also_buy_the_optional_round(
        self, capsys, monkeypatch, worktree, review_env, developer_config, stub_brain
    ):
        """A failed budget read leaves the review uncapped rather than sunk —
        but "we could not check the cap" is not a reason to also spend the
        optional extra round on it."""
        developer_config(max_need_files=6, max_calls_per_task=8)
        monkeypatch.setattr(
            db, "code_review_calls_get",
            lambda conn, task_id: (_ for _ in ()).throw(
                RuntimeError("database is locked")
            ),
        )
        stub_brain.replies["conformance"] = [
            need_files_json("helper.py", findings=[finding()]),
            findings_json(finding(claim="never reached")),
        ]

        code, envelope = drive(
            capsys, "run", "--worktree", str(worktree), "--base", "main"
        )

        assert code == 0
        assert envelope["status"] == "ok"
        assert stub_brain.calls == ["conformance"]
        assert "need_files" not in stub_brain.prompts[0]
