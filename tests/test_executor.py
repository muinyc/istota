"""Configuration loading for istota.executor module."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from istota.executor import (
    _compose_full_result,
    _resolve_user_tz,
    _is_automated_task,
    _is_terse,
    _last_substantial_region,
    _NO_FINAL_ANSWER_NOTICE,
    _TERSE_RESULT_MAX_CHARS,
    detect_malformed_result,
    parse_api_error,
    is_transient_api_error,
    build_prompt,
    load_persona,
    load_emissaries,
    _pre_transcribe_attachments,
    _detect_notification_reply,
    _apply_recency_window_talk,
    _apply_recency_window_db,
    _AUDIO_EXTENSIONS,
    _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    API_RETRY_DELAY_SECONDS,
    TRANSIENT_STATUS_CODES,
)
from istota import db as _db
from istota import executor
from istota.brain import BrainRequest, ClaudeCodeBrain
from istota.brain import claude_code
from tests.support.monotonic_spy import monotonic_spy
from tests.support.sleep_spy import sleep_spy
from istota.brain._types import BrainResult
import json
from pathlib import Path

from istota.config import Config, DeveloperConfig, EmailConfig as AppEmailConfig, NextcloudConfig, SecurityConfig, SiteConfig, UserConfig
from istota import db


def _system_half(config, user_id="alice", task_id=1) -> str:
    """The standing instructions `execute_task` wrote for this task.

    Since the prompt split, `input=` on the CLI subprocess carries the *user*
    half alone — the request, retrieved memory and conversation history. Skill
    bodies, the skills changelog and the workspace vocabulary are standing
    instructions and travel as `system_prompt.txt` in the task's control
    directory, which the brain passes with `--append-system-prompt-file`. A
    test asserting on one of those reads this file, and a *negative* assertion
    about one has to read it or it passes for the wrong reason.
    """
    from istota.executor import get_task_control_dir

    return (
        get_task_control_dir(config, user_id, task_id) / "system_prompt.txt"
    ).read_text(encoding="utf-8")



# ---------------------------------------------------------------------------
# TestParseApiError
# ---------------------------------------------------------------------------


class TestParseApiError:
    def test_parses_500_error(self):
        error_text = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_abc123"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Internal server error"
        assert result["request_id"] == "req_abc123"

    def test_parses_429_error(self):
        error_text = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"},"request_id":"req_xyz"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 429
        assert result["message"] == "Rate limit exceeded"
        assert result["request_id"] == "req_xyz"

    def test_parses_401_error(self):
        error_text = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"},"request_id":"req_auth"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 401
        assert result["message"] == "Invalid API key"

    def test_parses_error_with_prefix_text(self):
        error_text = 'Some prefix text before API Error: 503 {"type":"error","error":{"type":"overloaded_error","message":"Service overloaded"}}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 503
        assert result["message"] == "Service overloaded"

    def test_returns_none_for_non_api_error(self):
        error_text = "Claude Code was killed (likely out of memory)"
        result = parse_api_error(error_text)
        assert result is None

    def test_returns_none_for_regular_text(self):
        result = parse_api_error("Task completed successfully")
        assert result is None

    def test_handles_malformed_json(self):
        # Malformed JSON with closing brace but invalid content
        error_text = 'API Error: 500 {broken json}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Unknown error"
        assert result["request_id"] is None

    def test_unclosed_json_still_yields_the_status(self):
        # The JSON pattern needs a closing brace, so this used to parse as "not
        # an API error at all" — which meant a truncated 500 was never retried
        # and never reached the fallback (ISSUE-212). A 500 is a 500; only the
        # message is lost.
        error_text = 'API Error: 500 {broken json'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["request_id"] is None

    def test_handles_missing_error_field(self):
        error_text = 'API Error: 500 {"type":"error","request_id":"req_123"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Unknown error"
        assert result["request_id"] == "req_123"


# ---------------------------------------------------------------------------
# TestIsTransientApiError
# ---------------------------------------------------------------------------


class TestIsTransientApiError:
    def test_500_is_transient(self):
        error_text = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        assert is_transient_api_error(error_text) is True

    def test_502_is_transient(self):
        error_text = 'API Error: 502 {"type":"error","error":{"type":"api_error","message":"Bad gateway"}}'
        assert is_transient_api_error(error_text) is True

    def test_503_is_transient(self):
        error_text = 'API Error: 503 {"type":"error","error":{"type":"api_error","message":"Service unavailable"}}'
        assert is_transient_api_error(error_text) is True

    def test_504_is_transient(self):
        error_text = 'API Error: 504 {"type":"error","error":{"type":"api_error","message":"Gateway timeout"}}'
        assert is_transient_api_error(error_text) is True

    def test_529_is_transient(self):
        error_text = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
        assert is_transient_api_error(error_text) is True

    def test_429_is_transient(self):
        error_text = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limited"}}'
        assert is_transient_api_error(error_text) is True

    def test_401_is_not_transient(self):
        error_text = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Unauthorized"}}'
        assert is_transient_api_error(error_text) is False

    def test_403_is_not_transient(self):
        error_text = 'API Error: 403 {"type":"error","error":{"type":"permission_error","message":"Forbidden"}}'
        assert is_transient_api_error(error_text) is False

    def test_400_is_not_transient(self):
        error_text = 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Bad request"}}'
        assert is_transient_api_error(error_text) is False

    def test_non_api_error_is_not_transient(self):
        assert is_transient_api_error("Claude Code was killed (likely out of memory)") is False
        assert is_transient_api_error("Task execution timed out") is False
        assert is_transient_api_error("Cancelled by user") is False


# ---------------------------------------------------------------------------
# TestTransientStatusCodes
# ---------------------------------------------------------------------------


class TestTransientStatusCodes:
    def test_includes_common_server_errors(self):
        assert 500 in TRANSIENT_STATUS_CODES
        assert 502 in TRANSIENT_STATUS_CODES
        assert 503 in TRANSIENT_STATUS_CODES
        assert 504 in TRANSIENT_STATUS_CODES

    def test_includes_anthropic_overloaded(self):
        assert 529 in TRANSIENT_STATUS_CODES

    def test_excludes_client_errors(self):
        assert 400 not in TRANSIENT_STATUS_CODES
        assert 401 not in TRANSIENT_STATUS_CODES
        assert 403 not in TRANSIENT_STATUS_CODES
        assert 404 not in TRANSIENT_STATUS_CODES


# ---------------------------------------------------------------------------
# TestRetryConfiguration
# ---------------------------------------------------------------------------


class TestRetryConfiguration:
    def test_max_attempts_is_reasonable(self):
        assert API_RETRY_MAX_ATTEMPTS >= 2
        assert API_RETRY_MAX_ATTEMPTS <= 5

    def test_delay_is_reasonable(self):
        assert API_RETRY_DELAY_SECONDS >= 3
        assert API_RETRY_DELAY_SECONDS <= 30


# ---------------------------------------------------------------------------
# TestExecuteStreamingRetry
# ---------------------------------------------------------------------------


class TestExecuteStreamingRetry:
    """Retry logic for transient API errors lives in ClaudeCodeBrain.

    Tests use the static _execute_streaming_once method as the mock target
    and drive the public _execute_streaming wrapper, which is the same
    layering the executor used to have.
    """

    def _make_request(self, tmp_path: Path) -> BrainRequest:
        return BrainRequest(
            prompt="test",
            allowed_tools=["Bash"],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            streaming=True,
            result_file=tmp_path / "result.txt",
        )

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_retries_on_transient_error(self, mock_exec_once, tmp_path, monkeypatch):
        """Should retry on transient 500 errors before giving up."""
        slept = sleep_spy(monkeypatch, claude_code)
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_123"}'
        mock_exec_once.side_effect = [
            BrainResult(False, error_500, stop_reason="error"),
            BrainResult(True, "Success after retry"),
        ]

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Success after retry"
        assert mock_exec_once.call_count == 2
        # The backoff is slept in slices so a `!stop` lands during it rather
        # than after (ISSUE-212 — the wait can now be a provider-supplied
        # Retry-After, not just the fixed 5s). The total is the contract.
        assert sum(slept) == pytest.approx(API_RETRY_DELAY_SECONDS)

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_no_retry_on_permanent_error(self, mock_exec_once, tmp_path, monkeypatch):
        """Should not retry on permanent 401 errors."""
        slept = sleep_spy(monkeypatch, claude_code)
        error_401 = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"}}'
        mock_exec_once.return_value = BrainResult(False, error_401, stop_reason="error")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "401" in result.result_text
        assert mock_exec_once.call_count == 1
        assert slept == []

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_no_retry_on_non_api_error(self, mock_exec_once, tmp_path, monkeypatch):
        """Should not retry on non-API errors like OOM."""
        slept = sleep_spy(monkeypatch, claude_code)
        mock_exec_once.return_value = BrainResult(
            False, "Claude Code was killed (likely out of memory)", stop_reason="oom",
        )

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "out of memory" in result.result_text
        assert mock_exec_once.call_count == 1
        assert slept == []

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_gives_up_after_max_retries(self, mock_exec_once, tmp_path, monkeypatch):
        """Should give up after max retry attempts."""
        slept = sleep_spy(monkeypatch, claude_code)
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        mock_exec_once.return_value = BrainResult(False, error_500, stop_reason="error")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "500" in result.result_text
        assert mock_exec_once.call_count == API_RETRY_MAX_ATTEMPTS
        assert sum(slept) == pytest.approx(
            API_RETRY_DELAY_SECONDS * (API_RETRY_MAX_ATTEMPTS - 1)
        )

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_success_on_first_try_no_retry(self, mock_exec_once, tmp_path):
        """Should not retry if first attempt succeeds."""
        mock_exec_once.return_value = BrainResult(True, "Immediate success")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Immediate success"
        assert mock_exec_once.call_count == 1

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_actions_taken_passed_through(self, mock_exec_once, tmp_path):
        """Should pass through actions_taken from _execute_streaming_once."""
        actions = '["📄 Reading file.py", "✏️ Editing file.py"]'
        mock_exec_once.return_value = BrainResult(
            True, "Done", actions_taken=actions, execution_trace='[]',
        )

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Done"
        assert result.actions_taken == actions

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_actions_taken_from_successful_retry(
        self, mock_exec_once, tmp_path, monkeypatch,
    ):
        """On retry, should use actions_taken from the successful attempt."""
        sleep_spy(monkeypatch, claude_code, record=False)
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"err"},"request_id":"req_1"}'
        actions = '["📄 Reading config"]'
        mock_exec_once.side_effect = [
            BrainResult(False, error_500, stop_reason="error"),
            BrainResult(True, "ok", actions_taken=actions),
        ]

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.actions_taken == actions


# ---------------------------------------------------------------------------
# TestBuildPromptSkillsChangelog
# ---------------------------------------------------------------------------


class TestBuildPromptSkillsChangelog:
    def _make_task(self, source_type="talk"):
        return db.Task(
            id=1,
            status="running",
            source_type=source_type,
            user_id="alice",
            prompt="hello",
            conversation_token="room1",
        )

    def _make_config(self, tmp_path):
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def test_changelog_included_when_provided(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(
            task, [], config,
            skills_changelog="## 2026-02-08\n- New feature added",
        ).system
        assert "## What's New in Skills" in prompt
        assert "New feature added" in prompt

    def test_changelog_not_included_when_none(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(task, [], config, skills_changelog=None).system
        assert "What's New in Skills" not in prompt

    def test_changelog_appears_before_skills_doc(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(
            task, [], config,
            skills_doc="## Skills Reference (v: abc123)\n\n### Files\n\nFile ops.",
            skills_changelog="## 2026-02-08\n- Updated files skill",
        ).system
        changelog_pos = prompt.index("What's New in Skills")
        skills_pos = prompt.index("Skills Reference")
        assert changelog_pos < skills_pos


# ---------------------------------------------------------------------------
# TestResolveUserTz (ISSUE-099)
# ---------------------------------------------------------------------------


class TestResolveUserTz:
    """`_resolve_user_tz` must reflect live web-UI timezone edits.

    The web UI writes timezone to the ``user_profiles`` DB row, but the
    scheduler's in-memory ``Config`` is only built once at startup. Reading
    the timezone from the DB (with the in-memory ``UserConfig`` as fallback)
    means a travelling user's timezone change takes effect on the next task
    without a daemon restart. Mirrors the ``Config.is_module_enabled`` pattern.
    """

    def _make_config(self, tmp_path, *, user_tz="America/Los_Angeles"):
        db_path = tmp_path / "test.db"
        _db.init_db(db_path)
        return Config(
            db_path=db_path,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(timezone=user_tz)},
        )

    def test_db_profile_wins_over_stale_in_memory_config(self, tmp_path):
        from istota import user_profiles
        config = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        # Simulate a web-UI timezone change written to the DB after startup.
        user_profiles.ensure_profile(config.db_path, "alice", timezone="Europe/Lisbon")

        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "Europe/Lisbon"
        assert tz.key == "Europe/Lisbon"

    def test_falls_back_to_in_memory_config_when_no_db_row(self, tmp_path):
        config = self._make_config(tmp_path, user_tz="America/New_York")
        # No user_profiles row written.
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "America/New_York"

    def test_falls_back_to_utc_for_unknown_user(self, tmp_path):
        config = self._make_config(tmp_path)
        tz, tz_str = _resolve_user_tz(config, "nobody")
        assert tz_str == "UTC"

    def test_invalid_db_timezone_falls_back_to_utc(self, tmp_path):
        from istota import user_profiles
        config = self._make_config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", timezone="Not/AZone")
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "UTC"

    def test_invalid_timezone_warns_once(self, tmp_path, caplog):
        import logging

        from istota import executor as _executor
        from istota import user_profiles

        config = self._make_config(tmp_path)
        # "PDT" is an abbreviation, not an IANA name — the real-world bug.
        user_profiles.ensure_profile(config.db_path, "alice", timezone="PDT")
        _executor._INVALID_TZ_WARNED.discard(("alice", "PDT"))

        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            _resolve_user_tz(config, "alice")
            _resolve_user_tz(config, "alice")  # second call must not re-warn

        warnings = [
            r for r in caplog.records if "Invalid timezone" in r.getMessage()
        ]
        # Deduped: exactly one WARNING for the (user, tz) pair.
        assert len(warnings) == 1
        assert "PDT" in warnings[0].getMessage()
        assert "America/Los_Angeles" in warnings[0].getMessage()

    def test_no_db_path_uses_in_memory_config(self, tmp_path):
        # DB-less contexts (init/tests) must still resolve via UserConfig.
        config = Config(
            db_path=None,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(timezone="Asia/Tokyo")},
        )
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# TestSkillsFingerprintIntegration
# ---------------------------------------------------------------------------


class TestSkillsFingerprintIntegration:
    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        if not db_path.exists():
            db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def _make_task(self, conn, source_type="talk"):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type=source_type)
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_changelog_included_when_fingerprint_changed(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        # The changelog is a standing instruction, so it is in the system
        # half — the file, not stdin.
        assert "What's New in Skills" in _system_half(config)

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_when_fingerprint_matches(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        # Pre-store the current fingerprint
        from istota.skills._loader import compute_skills_fingerprint
        fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            db.set_user_skills_fingerprint(conn, "alice", fp)
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        assert "What's New in Skills" not in _system_half(config)

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_for_briefing(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="briefing")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        assert "What's New in Skills" not in _system_half(config)

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_for_scheduled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="scheduled")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        assert "What's New in Skills" not in _system_half(config)

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_updated_after_success(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from istota.skills._loader import compute_skills_fingerprint
        expected_fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is True
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp == expected_fp

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_not_updated_for_non_interactive(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="scheduled")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is True
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp is None

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_not_updated_on_failure(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is False
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp is None


# ---------------------------------------------------------------------------
# TestDeveloperEnvVars
# ---------------------------------------------------------------------------

class TestDeveloperEnvVars:
    """The developer skill's setup_env hook.

    The hook used to generate `gitlab-api` / `github-api` shell scripts whose
    bodies were a case statement built from an endpoint allowlist. Those are
    gone: the model drives the real `gh` and `glab` through the wrapper in
    src/istota/forge_cli.py. What is asserted here is what the hook installs
    and what it hands back, not the contents of a generated script.
    """

    def _make_config(
        self, tmp_path, developer_enabled=True, github=False,
        skill_proxy_enabled=False, **dev_kw,
    ):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        # exist_ok: a test may build two configs from one tmp_path to compare
        # how the hook behaves across settings.
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")
        kw = dict(
            enabled=developer_enabled,
            # Under tmp_path, never a real host path: the hook creates
            # `{repos_dir}/{user_id}` and sweeps it, so a literal `/srv/repos`
            # here would have the suite writing outside its own directory.
            repos_dir=str(tmp_path / "repos"),
            gitlab_url="https://gitlab.example.com",
            gitlab_token="glpat-test",
            gitlab_username="istotabot",
            gitlab_default_namespace="example",
        )
        if github:
            kw.update(
                github_url="https://github.com",
                github_token="ghp-test",
                github_username="istotabot",
            )
        kw.update(dev_kw)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            developer=DeveloperConfig(**kw),
            security=SecurityConfig(skill_proxy_enabled=skill_proxy_enabled),
        )

    def _make_task(self, conn):
        task_id = db.create_task(
            conn, prompt="test", user_id="alice", source_type="talk",
        )
        return db.get_task(conn, task_id)

    def _hook_env(self, config, tmp_path):
        from istota.skills.developer import setup_env

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.config = config
        ctx.user_temp_dir = str(user_temp)
        # The hook creates and scrubs the repos subtree only for an admin,
        # matching the bind's own gate.
        ctx.is_admin = True
        # The hook reads the task's user id to name the repos subtree it
        # creates and sweeps; without one it takes the fail-closed branch and
        # every assertion below would be made against a hook that did half its
        # work. The exec socket path is derived from the same user id, so this
        # one task is what both halves of the hook read.
        ctx.task = db.Task(
            id=1, prompt="test", user_id="alice",
            source_type="talk", status="running", conversation_token="",
        )
        return setup_env(ctx), user_temp

    def test_disabled_developer_returns_nothing(self, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=False)
        env, _ = self._hook_env(config, tmp_path)
        assert env == {}

    def test_git_credential_helper_written(self, tmp_path):
        config = self._make_config(tmp_path)
        env, user_temp = self._hook_env(config, tmp_path)
        helper = user_temp / ".developer" / "git-credential-helper"
        assert helper.exists()
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.https://gitlab.example.com.helper"

    def test_credential_helper_quotes_the_expansion(self, tmp_path):
        """git wants the value verbatim; an unquoted expansion is word-split
        by sh and rejoined by echo on single spaces."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        body = (user_temp / ".developer" / "git-credential-helper").read_text()
        assert 'echo password="$GITLAB_TOKEN"' in body

    def test_forge_wrappers_installed_under_every_name(self, tmp_path):
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        dev_bin = user_temp / ".developer"
        canonical = (
            Path(__file__).resolve().parent.parent / "src/istota/forge_cli.py"
        ).read_bytes()
        for name in ("gh", "glab", "github-api", "gitlab-api"):
            installed = dev_bin / name
            assert installed.exists(), name
            assert installed.read_bytes() == canonical, name
            assert installed.stat().st_mode & 0o777 == 0o700, name

    def test_retired_api_cmd_vars_are_gone(self, tmp_path):
        config = self._make_config(tmp_path, github=True)
        env, _ = self._hook_env(config, tmp_path)
        assert "GITLAB_API_CMD" not in env
        assert "GITHUB_API_CMD" not in env

    def test_path_prepend_is_the_only_env_var_the_wrapper_needs(self, tmp_path):
        """Everything else travels in the policy file. The wrapper runs as a
        child of the model's shell, so an env-supplied path is a path the model
        chooses — an ISTOTA_FORGE_POLICY pointing at a toothless file would be
        a one-token bypass of the whole rule set."""
        config = self._make_config(tmp_path, github=True)
        env, user_temp = self._hook_env(config, tmp_path)
        assert env["ISTOTA_PATH_PREPEND"] == str(user_temp / ".developer")
        for retired in (
            "ISTOTA_FORGE_POLICY", "ISTOTA_GH_CONFIG_DIR",
            "ISTOTA_GLAB_CONFIG_DIR", "ISTOTA_GH_URL", "ISTOTA_GITLAB_URL",
            "ISTOTA_GH_REAL", "ISTOTA_GLAB_REAL", "ISTOTA_FORGE_STATE_DIR",
        ):
            assert retired not in env, retired

    def test_policy_carries_the_settings_the_wrapper_must_not_trust(self, tmp_path):
        config = self._make_config(
            tmp_path, github=True, gh_bin_path="/opt/gh", glab_bin_path="/opt/glab",
        )
        _, user_temp = self._hook_env(config, tmp_path)
        dev_bin = user_temp / ".developer"
        policy = json.loads((dev_bin / "forge-policy.json").read_text())
        gh = policy["github"]
        assert gh["real_bin"] == "/opt/gh"
        assert gh["url"] == "https://github.com"
        assert gh["config_dir"] == str(dev_bin / "github-config")
        assert gh["data_dir"] == str(dev_bin / "github-data")
        assert Path(gh["state_dir"]).is_dir()
        assert policy["gitlab"]["real_bin"] == "/opt/glab"
        assert policy["gitlab"]["url"] == "https://gitlab.example.com"

    def test_unconfigured_bin_path_resolves_from_the_daemon_path(self, tmp_path, monkeypatch):
        """The binary and the config key ship separately: Ansible installs gh
        into /usr/bin, but only a full play run rewrites config.toml, and the
        auto-update cron pulls code without running Ansible. In that window the
        key is absent, the code default stands, and nothing exists at it."""
        import shutil as _shutil

        from istota.skills import developer as _dev

        monkeypatch.setattr(_dev.os.path, "exists", lambda p: False)
        monkeypatch.setattr(
            _shutil, "which", lambda name: f"/usr/bin/{name}",
        )
        config = self._make_config(tmp_path, github=True)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        assert policy["github"]["real_bin"] == "/usr/bin/gh"
        assert policy["gitlab"]["real_bin"] == "/usr/bin/glab"

    def test_explicit_bin_path_is_never_second_guessed(self, tmp_path, monkeypatch):
        """An operator who named a path gets that path even when it is missing.
        Silently exec'ing a different binary found on PATH is the wrong
        surprise; the start-up warning is how a bad path gets reported."""
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: f"/usr/bin/{name}")
        config = self._make_config(
            tmp_path, github=True,
            gh_bin_path="/opt/nonexistent/gh", glab_bin_path="/opt/nonexistent/glab",
        )
        _, user_temp = self._hook_env(config, tmp_path)
        policy = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        assert policy["github"]["real_bin"] == "/opt/nonexistent/gh"
        assert policy["gitlab"]["real_bin"] == "/opt/nonexistent/glab"

    def test_policy_grants_direct_tokens_only_with_the_proxy_off(self, tmp_path):
        """The permission lives in the policy file because that is the one
        input the model cannot redirect. An env flag would let it opt itself
        into reading whatever token it had planted."""
        _, user_temp = self._hook_env(
            self._make_config(tmp_path, skill_proxy_enabled=False), tmp_path,
        )
        off = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        assert off["github"]["direct_token"] is True

        _, user_temp2 = self._hook_env(
            self._make_config(tmp_path, skill_proxy_enabled=True), tmp_path,
        )
        on = json.loads(
            (user_temp2 / ".developer" / "forge-policy.json").read_text()
        )
        assert on["github"]["direct_token"] is False

    def test_data_dir_is_pinned_and_empty(self, tmp_path):
        """gh execs gh-<name> from $XDG_DATA_HOME/gh/extensions for an unknown
        first argument, which no argv rule can see."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        data = Path(policy["github"]["data_dir"])
        assert data.is_dir()
        assert list(data.iterdir()) == []

    def test_policy_file_is_loadable_and_denies_the_baseline(self, tmp_path):
        from istota.forge_cli import FORGE_GITHUB, denied_reason, load_policy

        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = load_policy(
            str(user_temp / ".developer" / "forge-policy.json"), FORGE_GITHUB,
        )
        assert denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], policy)
        assert denied_reason(FORGE_GITHUB, ["pr", "create"], policy) is None

    def test_operator_knobs_reach_the_policy_file(self, tmp_path):
        from istota.forge_cli import FORGE_GITHUB, denied_reason, load_policy

        config = self._make_config(
            tmp_path,
            forge_cli_extra_denied=["gh pr merge"],
            forge_cli_permit=["gh repo delete"],
        )
        _, user_temp = self._hook_env(config, tmp_path)
        policy = load_policy(
            str(user_temp / ".developer" / "forge-policy.json"), FORGE_GITHUB,
        )
        assert denied_reason(FORGE_GITHUB, ["pr", "merge", "1"], policy)
        assert denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], policy) is None

    def test_cli_config_dirs_seeded_at_the_mode_glab_demands(self, tmp_path):
        """glab refuses any mode but 0600; gh accepts either. Measured against
        glab 1.114 — see the integration tests in test_forge_cli_exec.py."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        for forge in ("github", "gitlab"):
            config_yml = Path(policy[forge]["config_dir"]) / "config.yml"
            assert config_yml.exists(), forge
            assert config_yml.stat().st_mode & 0o777 == 0o600, forge

    def test_no_token_means_no_forge_wrappers(self, tmp_path):
        config = self._make_config(tmp_path, gitlab_token="", github_token="")
        env, user_temp = self._hook_env(config, tmp_path)
        assert "ISTOTA_PATH_PREPEND" not in env
        assert not (user_temp / ".developer" / "gh").exists()

    def test_credential_fetch_written_when_the_proxy_is_on(self, tmp_path):
        """The proxy branch of setup_env. With the proxy on, the helper must
        not hold the token itself — it shells out to credential-fetch, which
        asks the proxy for it at call time."""
        config = self._make_config(tmp_path, skill_proxy_enabled=True)
        _, user_temp = self._hook_env(config, tmp_path)
        dev_bin = user_temp / ".developer"
        fetch = dev_bin / "credential-fetch"
        assert fetch.exists()
        assert fetch.stat().st_mode & 0o777 == 0o700
        body = (dev_bin / "git-credential-helper").read_text()
        assert f'echo password="$({fetch} GITLAB_TOKEN)"' in body
        assert "glpat-test" not in body

    def test_credential_fetch_absent_when_the_proxy_is_off(self, tmp_path):
        config = self._make_config(tmp_path, skill_proxy_enabled=False)
        _, user_temp = self._hook_env(config, tmp_path)
        assert not (user_temp / ".developer" / "credential-fetch").exists()

    def test_seeded_config_is_truncated_every_run(self, tmp_path):
        """user_temp_dir persists across tasks. gh expands aliases from
        config.yml before dispatch, so a file that survived one run would be
        honoured by every later one."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )
        cfg = Path(policy["github"]["config_dir"]) / "config.yml"
        cfg.write_text("aliases:\n    pwn: repo delete\n")
        self._hook_env(config, tmp_path)
        assert cfg.read_text() == ""

    def test_no_token_value_appears_in_the_returned_env(self, tmp_path):
        config = self._make_config(tmp_path, github=True)
        env, _ = self._hook_env(config, tmp_path)
        joined = " ".join(env.values())
        assert "glpat-test" not in joined
        assert "ghp-test" not in joined


class TestPlainHttpGitlabReachesTheConfiguredHost:
    """A `http://` gitlab_url has to survive into glab's own config file.

    glab discards the scheme inside `GITLAB_HOST` and keeps the port, so a
    deployment configured against `http://gitlab.internal:8080` has every call
    fail with "tls: first record does not look like a TLS handshake" — measured
    on glab 1.114.0, the version the image pins. The only lever glab offers is a
    per-host `api_protocol` in its config file, and that file is the one
    `_seed_cli_config_dir` truncates on every task, so the entry has to be
    written by the same code that empties it.

    Kept as its own class because the property is about `_seed_cli_config_dir`
    rather than about the returned env, and because the truncation invariant it
    sits next to is the thing most likely to be broken by a later edit here.
    """

    def _seed(self, tmp_path, url, forge="gitlab"):
        from istota.skills.developer import _seed_cli_config_dir

        target = _seed_cli_config_dir(
            tmp_path, f"{forge}-config", forge=forge, forge_url=url
        )
        return (target / "config.yml").read_text()

    def test_gh_gets_nothing_even_when_its_url_is_plain_http(self, tmp_path):
        """The entry is glab's, and gh must not receive it.

        The entry exists to reach a forge over plain HTTP, and gh refuses a
        scheme in `GH_HOST` outright — so there is nothing it could fix for gh.
        (The *port* half is a separate question and is handled:
        `forge_cli._gh_host` keeps a non-default one, ISSUE-279.) Worse than
        useless, though: gh *reads* a `hosts:` block, and on
        seeing one it runs its multi-account migration and writes a `hosts.yml`
        beside the config. `_seed_cli_config_dir` truncates `config.yml` and
        nothing else, and `user_temp_dir` persists across tasks — so that file
        would survive every later run in a directory whose whole design is that
        nothing does.
        """
        assert self._seed(tmp_path, "http://ghe.internal:8080", forge="github") == ""

    def test_the_forge_decides_rather_than_the_directory_name(self, tmp_path):
        """The rule lives in the function, not in what the caller passed.

        `_section` builds the directory name from the forge already, so a
        caller-blanked URL would work — and would put a security-relevant rule
        in the one place a later refactor is free to change without reading
        this docstring.
        """
        from istota.skills.developer import _seed_cli_config_dir

        target = _seed_cli_config_dir(
            tmp_path, "confusingly-named", forge="github",
            forge_url="http://ghe.internal:8080",
        )

        assert (target / "config.yml").read_text() == ""

    def test_https_still_seeds_an_empty_file(self, tmp_path):
        """The overwhelmingly common case must not grow a config surface.

        Anything written here is honoured by glab before dispatch, so the file
        stays empty wherever it does not have to carry something.
        """
        assert self._seed(tmp_path, "https://gitlab.example.com") == ""

    def test_no_url_seeds_an_empty_file(self, tmp_path):
        assert self._seed(tmp_path, "") == ""

    def test_plain_http_writes_the_protocol_for_that_host_only(self, tmp_path):
        body = self._seed(tmp_path, "http://127.0.0.1:18080")

        assert "api_protocol: http" in body, body
        # The host key carries the port. glab looks the entry up by the netloc
        # it derived from GITLAB_HOST, so an entry filed under the bare
        # hostname is never consulted and the call still forces https.
        assert "127.0.0.1:18080" in body, body

    def test_the_entry_is_valid_yaml_shaped_the_way_glab_reads_it(self, tmp_path):
        """Parsed, not pattern-matched.

        The host key contains a colon, which is exactly the shape that turns an
        unquoted YAML mapping key into something a parser reads differently
        from how it was meant. Asserting on substrings alone would pass on a
        file glab cannot load.
        """
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://gitlab.internal:8080"))

        assert parsed["hosts"]["gitlab.internal:8080"] == {
            "api_protocol": "http",
            "api_host": "gitlab.internal:8080",
        }, parsed

    def test_a_default_port_keeps_the_bare_host_as_the_key(self, tmp_path):
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://gitlab.internal"))

        assert set(parsed["hosts"]) == {"gitlab.internal"}, parsed

    def test_the_file_is_still_replaced_rather_than_appended(self, tmp_path):
        """The truncation invariant, asserted on the branch that writes content.

        `test_seeded_config_is_truncated_every_run` covers the empty branch. The
        risk here is different and worse: a branch that writes a file could be
        implemented as an append, and an alias table the model planted would
        then be preserved *and* joined by a protocol entry that made it look
        deliberate.
        """
        from istota.skills.developer import _seed_cli_config_dir

        url = "http://127.0.0.1:18080"
        target = _seed_cli_config_dir(
            tmp_path, "gitlab-config", forge="gitlab", forge_url=url
        )
        (target / "config.yml").write_text("aliases:\n    pwn: repo delete\n")

        _seed_cli_config_dir(tmp_path, "gitlab-config", forge="gitlab", forge_url=url)

        assert "pwn" not in (target / "config.yml").read_text()

    def test_an_uppercase_host_is_lowercased(self, tmp_path):
        """glab looks the entry up by a lowercased key. Measured on 1.114.0:
        `GITLAB_HOST=http://LOCALHOST:8080` with the key written verbatim finds
        nothing and forces https; the same entry filed lowercase works."""
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://GitLab.Internal:8080"))

        assert set(parsed["hosts"]) == {"gitlab.internal:8080"}, parsed

    def test_a_subpath_install_keeps_its_path_in_the_key(self, tmp_path):
        """`build_invocation` puts the *whole* URL in GITLAB_HOST — a subpath
        install is a documented supported shape (`tests/test_forge_cli.py::
        test_gitlab_host_keeps_port_and_subpath`). Measured: glab's lookup key
        carries the path, so an entry filed under the bare netloc is never
        consulted and the call still forces https."""
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://forge.internal/gitlab"))

        assert set(parsed["hosts"]) == {"forge.internal/gitlab"}, parsed

    def test_a_trailing_slash_does_not_become_part_of_the_key(self, tmp_path):
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://forge.internal/"))

        assert set(parsed["hosts"]) == {"forge.internal"}, parsed

    def test_a_url_carrying_a_password_gets_no_entry_at_all(self, tmp_path):
        """The one case where making it work would be worse than leaving it broken.

        Measured on glab 1.114.0: its lookup key includes the userinfo, so an
        entry that actually matched `http://user:token@host` would have to carry
        the password — into `config.yml`, which lives under `.developer` and is
        bound *readable* into the sandbox. That hands the model a credential to
        support a shape that should not exist: the token belongs in
        `gitlab_token`, and `git_remote_scrub` exists to strip exactly this out
        of URLs.

        So: no entry, the call fails the way it did before, and
        `developer.forge_transport` is what says why.
        """
        body = self._seed(tmp_path, "http://user:s3cr3t-value@gitlab.internal:8080")

        assert body == "", body

    def test_no_password_survives_into_the_file_by_any_route(self, tmp_path):
        """Stated over the whole output rather than over the branch.

        A later change that starts emitting the netloc again would satisfy the
        assertion above only if it also returned early — this one fails
        whatever route the value took.
        """
        for url in (
            "http://user:s3cr3t-value@gitlab.internal:8080",
            "https://user:s3cr3t-value@gitlab.internal",
            "http://s3cr3t-value@gitlab.internal:8080",
        ):
            assert "s3cr3t-value" not in self._seed(tmp_path, url), url

    def test_the_seeded_file_keeps_the_mode_glab_demands(self, tmp_path):
        from istota.skills.developer import _seed_cli_config_dir

        target = _seed_cli_config_dir(
            tmp_path, "gitlab-config", forge="gitlab",
            forge_url="http://127.0.0.1:18080",
        )

        assert (target / "config.yml").stat().st_mode & 0o777 == 0o600


class TestPathPrependOrdering:
    """A secondary guard on the *shape* of the ordering, not its effect.

    The behavioural tests are TestForgeCliPathPrepend in
    tests/test_sandbox_db_env.py: they run a real task and assert the model's
    PATH carries .developer while the skill proxy's does not. Those are what
    prove the property, and they do fail when the two statements are swapped.

    This one exists because the property is easy to break by *moving code*
    while keeping every behaviour test green in some future refactor that
    also changes the fixtures. It asserts the merge loop still skips the key
    and the application still sits after the snapshot. If it ever fights a
    legitimate refactor, delete it — the behavioural tests are the contract.
    """

    def test_reserved_key_is_not_merged_into_env(self):
        """The hook loop skips it, so it cannot ride into proxy_base_env."""
        import inspect

        from istota import task_env

        # Reads `build_task_runtime` rather than `execute_task`: the env
        # assembly moved to `task_env` whole, and the ordering it guards moved
        # with it. Same two statements, same order, one function along.
        src = inspect.getsource(task_env.build_task_runtime)
        assert "if k == HOOK_PATH_PREPEND_KEY:" in src
        # ...and the application site is after the snapshot, not before.
        # Anchored on the assignment target rather than its right-hand side:
        # the property is the *ordering* of the snapshot against the PATH
        # application, and the expression being snapshotted is free to change
        # (ISSUE-390 wrapped it in `without_claude_runtime_env`).
        assert src.index("proxy_base_env = ") < src.index(
            "_path_prepend = hook_env.get(HOOK_PATH_PREPEND_KEY"
        )


class TestWebsiteEnvVars:
    """The bot's own instance-wide web root — no per-user gating."""

    """The agent-writable static web root was removed (ISSUE-194): a
    publicly-served directory the agent could write to with a plain ``cp`` was
    an outbound egress channel the confirmation model classified as a benign
    local write. No env var may hand a task a path to one.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            site=SiteConfig(hostname="istota.example.com"),
            users={"alice": UserConfig()},
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_website_env_vars_never_set(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "WEBSITE_PATH" not in env
        assert "WEBSITE_URL" not in env


class TestKarakeepEnvVars:
    """Karakeep env vars come from the encrypted secrets table after the
    modules / connected services refactor — the karakeep resource type was
    retired with that change.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        users = {"alice": UserConfig()}
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the bookmarks manifest is loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            users=users,
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_set_when_secrets_configured(self, mock_run, tmp_path, monkeypatch):
        from istota import secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "base_url",
            "https://keep.example.com/api/v1",
        )
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "api_key", "kk-secret",
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            conn.commit()  # release writer lock so secrets_store can bump last_accessed_at
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "kk-secret"

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_not_set_when_no_secrets(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "KARAKEEP_BASE_URL" not in env
        assert "KARAKEEP_API_KEY" not in env

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_partial_when_only_one_secret(self, mock_run, tmp_path, monkeypatch):
        # Phase 3: ``bookmarks`` auto-authorizes only when its sensitive
        # spec (``KARAKEEP_API_KEY``) resolves. With only ``base_url``
        # configured and ``bookmarks`` not selected, the skill is not
        # authorized and none of its env vars flow. The user-facing
        # signal is "the half-configured user gets nothing" — a cleaner
        # failure mode than the Phase 2 partial-env shape.
        from istota import secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "base_url",
            "https://keep.example.com/api/v1",
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            conn.commit()
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "KARAKEEP_BASE_URL" not in env
        assert "KARAKEEP_API_KEY" not in env


class TestWebsitePromptSection:
    """The prompt must not advertise a writable public web root (ISSUE-194)."""

    def test_website_never_in_prompt(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            site=SiteConfig(hostname="istota.example.com"),
            users={"alice": UserConfig()},
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="build my website", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        # Both halves: the claim is that the primitive is gone from the prompt,
        # not that it landed on one side of the split.
        composed = build_prompt(task, [], config)
        whole = composed.system + composed.user
        assert "Web Root" not in whole
        assert "istota.example.com" not in whole


# ---------------------------------------------------------------------------
# TestAdminIsolation
# ---------------------------------------------------------------------------


class TestAdminPromptIsolation:
    def _make_config(self, tmp_path, admin_users=None):
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            # Admin vs non-admin mount-path scoping is a Nextcloud multi-user
            # feature — the "mounted at" wording requires a Nextcloud backend.
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_prompt_never_states_the_db_path(self, tmp_path, is_admin):
        """Naming a file that has been masked out of the sandbox is worse than
        saying nothing: a failed open reads as a broken command, not a boundary."""
        config = self._make_config(tmp_path, admin_users=None if is_admin else {"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        # Both halves: "the prompt never names the database path" is a boundary
        # claim about everything the model is shown, not a claim about where a
        # layer was classified. A half-scoped assertion would go quiet the day
        # a path started leaking through the other one.
        composed = build_prompt(task, [], config, is_admin=is_admin)
        assert str(config.db_path) not in composed.system + composed.user
        assert "Database: reachable only through skill CLIs" in composed.system

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_absence_claim_only_when_sandbox_is_in_effect(self, tmp_path, is_admin):
        config = self._make_config(tmp_path, admin_users=None if is_admin else {"bob"})
        config.security.sandbox_enabled = True
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        with patch("istota.executor._bwrap_available", return_value=True):
            prompt = build_prompt(task, [], config, is_admin=is_admin).system
        assert "the directories that hold them are empty here" in prompt

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_prohibition_kept_where_there_is_no_sandbox(self, tmp_path, is_admin):
        """Docker without CAP_SYS_ADMIN, and the standalone install.

        The databases really are on the model's filesystem on those shapes, so
        claiming they aren't would be a false boundary — the exact failure this
        change set exists to correct. The older prohibition wording covers it.
        """
        config = self._make_config(tmp_path, admin_users=None if is_admin else {"bob"})
        config.security.sandbox_enabled = True
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        with patch("istota.executor._bwrap_available", return_value=False):
            prompt = build_prompt(task, [], config, is_admin=is_admin).system
        assert "the directories that hold them are empty here" not in prompt
        assert "Never open a database file directly" in prompt
        assert "no filesystem sandbox" in prompt

    def test_admin_prompt_states_admin_privileges(self, tmp_path):
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True).system
        assert "Privileges: admin" in prompt

    def test_non_admin_prompt_states_standard_privileges(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False).system
        assert "Privileges: standard user" in prompt
        assert "Privileges: admin" not in prompt

    def test_prompt_has_no_sqlite3_tool(self, tmp_path):
        """sqlite3 tool removed in favor of deferred JSON operations."""
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True).system
        assert "sqlite3 for the task database" not in prompt

    def test_admin_prompt_no_subtask_instructions(self, tmp_path):
        """Subtask creation instructions should NOT be in the hardcoded prompt.

        They belong in the tasks skill doc, loaded only when relevant.
        """
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True).system
        assert "create subtasks" not in prompt.lower()

    def test_non_admin_prompt_no_subtask_rule(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False).system
        assert "create subtasks" not in prompt

    def test_admin_prompt_has_full_mount_path(self, tmp_path):
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True).system
        assert f"mounted at '{config.nextcloud_mount_path}'" in prompt

    def test_non_admin_prompt_has_scoped_mount_path(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        scoped = str(config.nextcloud_mount_path / "Users" / "alice")
        prompt = build_prompt(task, [], config, is_admin=False).system
        assert f"mounted at '{scoped}'" in prompt

    def test_non_admin_prompt_has_restricted_access_rule(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False).system
        assert "You can ONLY access files under" in prompt
        assert "do NOT have access to the task database" in prompt

    def test_prompt_includes_utc_anchor_and_elapsed_time_rule(self, tmp_path):
        """ISSUE-091 — UTC anchor + elapsed-time rule must be present."""
        import re
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        for is_admin in (True, False):
            prompt = build_prompt(task, [], config, is_admin=is_admin).system
            assert re.search(r"Current UTC: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prompt), (
                "Current UTC ISO 8601 line missing from prompt header"
            )
            assert "normalize both to ISO 8601 UTC" in prompt, (
                "Elapsed-time arithmetic rule missing from rules section"
            )

    def test_prompt_includes_fetched_content_date_rule(self, tmp_path):
        """ISSUE-155 — dates in fetched content must not override the prompt date."""
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        for is_admin in (True, False):
            prompt = build_prompt(task, [], config, is_admin=is_admin).system
            assert "publication or authorship dates" in prompt, (
                "Fetched-content date rule missing from rules section"
            )


class TestAdminEnvVarIsolation:
    def _make_config(self, tmp_path, admin_users=None):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_admin_no_db_path_env(self, mock_run, tmp_path):
        """Admins used to get ISTOTA_DB_PATH in Claude's env. Nobody does now.

        It goes to the skill proxy instead — see
        tests/test_sandbox_db_env.py::TestFrameworkDbPathRouting, which also
        covers the non-admin half (the path reaches the proxy for every user,
        which is what un-broke scoped reads for non-admins).
        """
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "ISTOTA_DB_PATH" not in env

    @patch("istota.executor.subprocess.run")
    def test_non_admin_no_db_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "ISTOTA_DB_PATH" not in env

    @patch("istota.executor.subprocess.run")
    def test_admin_gets_full_mount_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["NEXTCLOUD_MOUNT_PATH"] == str(config.nextcloud_mount_path)

    @patch("istota.executor.subprocess.run")
    def test_non_admin_gets_real_root_mount_path_env(self, mock_run, tmp_path):
        # The mount env var is the REAL root for non-admins too. Every consumer
        # (memory / memory_search CLIs, the schedules/reminders skill docs)
        # prepends `Users/<uid>` to it; a previously "scoped" mount
        # (real/Users/<uid>) doubled that segment, so a non-admin's USER.md write
        # landed at real/Users/<uid>/Users/<uid>/… — a phantom path never read
        # back. Filesystem isolation is enforced by the bwrap bind (only the
        # user's own Users/<uid> dir is bound), not by this env var.
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["NEXTCLOUD_MOUNT_PATH"] == str(config.nextcloud_mount_path)
        # Specifically NOT the doubled/scoped form.
        assert env["NEXTCLOUD_MOUNT_PATH"] != str(
            config.nextcloud_mount_path / "Users" / "alice"
        )

    @patch("istota.executor.subprocess.run")
    def test_admin_skills_include_admin_only(self, mock_run, tmp_path):
        """Admin user should get admin-only skills like schedules in the prompt."""
        config = self._make_config(tmp_path)
        skills_dir = config.skills_dir
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n\n'
            '[schedules]\ndescription = "Scheduled jobs"\nsource_types = ["talk"]\nadmin_only = true\n'
        )
        (skills_dir / "schedules.md").write_text("Admin scheduling reference.")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="set up a schedule", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        assert "Admin scheduling reference" in _system_half(config)

    @patch("istota.executor.subprocess.run")
    def test_non_admin_skills_exclude_admin_only(self, mock_run, tmp_path):
        """Non-admin user should NOT get admin-only skills."""
        config = self._make_config(tmp_path, admin_users={"bob"})
        skills_dir = config.skills_dir
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n\n'
            '[schedules]\ndescription = "Scheduled jobs"\nsource_types = ["talk"]\nadmin_only = true\n'
        )
        (skills_dir / "schedules.md").write_text("Admin scheduling reference.")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="set up a schedule", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        assert "Admin scheduling reference" not in _system_half(config)


class TestDeferredDirEnvVar:
    """ISTOTA_DEFERRED_DIR env var should always be set."""

    def _make_config(self, tmp_path, admin_users=None):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_deferred_dir_set_for_admin(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DEFERRED_DIR"] == str(tmp_path / "temp" / "alice")

    @patch("istota.executor.subprocess.run")
    def test_deferred_dir_set_for_non_admin(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DEFERRED_DIR"] == str(tmp_path / "temp" / "alice")

    @patch("istota.executor.subprocess.run")
    def test_experimental_features_propagated(self, mock_run, tmp_path):
        """LLM-path subprocess must carry ISTOTA_EXPERIMENTAL_FEATURES so
        skills invoked via the skill proxy (which forwards env to skill CLIs)
        see consistent gating with the scheduler subprocess paths."""
        from istota.config import ExperimentalConfig
        config = self._make_config(tmp_path)
        config.experimental = ExperimentalConfig(features=["money_tax", "money_wash_sales"])
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_EXPERIMENTAL_FEATURES"] == "money_tax,money_wash_sales"

    @patch("istota.executor.subprocess.run")
    def test_experimental_features_empty_when_unset(self, mock_run, tmp_path):
        """Always-set contract: even with no features enabled, the var
        exists (empty string) so consumers don't have to dance around
        os.environ.get(...) returning None."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_EXPERIMENTAL_FEATURES"] == ""


# ---------------------------------------------------------------------------
# TestCalDAVCredentialScoping
# ---------------------------------------------------------------------------


class TestCalDAVCredentialScoping:
    """CalDAV credentials should only be injected when user has calendars."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        # exist_ok: a test may build two configs from one tmp_path to compare
        # how the hook behaves across settings.
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the calendar manifest's
            # gate_has_discovered_calendars CALDAV_* specs are loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="bot",
                app_password="secret",
            ),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.get_calendars_for_user")
    @patch("istota.executor.get_caldav_client")
    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_present_when_user_has_calendars(
        self, mock_run, mock_client, mock_cals, tmp_path,
    ):
        mock_cals.return_value = [("Personal", "https://cal/personal", True)]
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" in env
        assert "CALDAV_USERNAME" in env

    @patch("istota.executor.get_calendars_for_user")
    @patch("istota.executor.get_caldav_client")
    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_absent_when_no_calendars(
        self, mock_run, mock_client, mock_cals, tmp_path,
    ):
        mock_cals.return_value = []  # No calendars for this user
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" not in env
        assert "CALDAV_USERNAME" not in env

    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_absent_when_no_caldav_config(self, mock_run, tmp_path):
        """No CalDAV configured at all — creds should not appear."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        config.nextcloud = NextcloudConfig()  # No URL = no CalDAV
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" not in env
        assert "CALDAV_USERNAME" not in env


# ---------------------------------------------------------------------------
# TestUserIdSubstitution
# ---------------------------------------------------------------------------


class TestUserIdSubstitution:
    """Skill docs should have {user_id} replaced with actual user ID."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text(
            '[memory]\ndescription = "Memory"\nalways_include = true\n'
        )
        skill_dir = skills_dir / "memory"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'description = "Memory"\nalways_include = true\n'
        )
        (skill_dir / "skill.md").write_text(
            "Memory file at /Users/{user_id}/bot/config/USER.md"
        )
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
        )

    def _make_task(self, conn, user_id="alice"):
        task_id = db.create_task(conn, prompt="test", user_id=user_id, source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_user_id_substituted_in_skills_doc(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, user_id="alice")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        # The skill body carrying the placeholder is a standing instruction,
        # so the substitution is visible in the system half.
        composed = _system_half(config)
        assert "/Users/alice/bot/config/USER.md" in composed
        assert "{user_id}" not in composed


# ---------------------------------------------------------------------------
# TestLoadPersona
# ---------------------------------------------------------------------------


class TestLoadPersona:
    def _make_config(self, tmp_path, use_mount=True):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        kwargs = dict(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")
        if use_mount:
            mount = tmp_path / "mount"
            mount.mkdir()
            kwargs["nextcloud_mount_path"] = mount
        return Config(**kwargs)

    def test_user_persona_overrides_global(self, tmp_path):
        config = self._make_config(tmp_path)
        # Create global persona
        (tmp_path / "config" / "persona.md").write_text("Global persona")
        # Create user workspace persona
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("Custom persona for Alice")

        result = load_persona(config, user_id="alice")
        assert result == "Custom persona for Alice"

    def test_empty_user_persona_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("   ")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_missing_user_persona_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_no_mount_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path, use_mount=False)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_no_user_id_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id=None)
        assert result == "Global persona"

    def test_bot_name_substituted_in_user_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "jarvis" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("You are {BOT_NAME}, a helpful bot.")

        result = load_persona(config, user_id="alice")
        assert result == "You are Jarvis, a helpful bot."


class TestLoadPersonaPlantedPaths(TestLoadPersona):
    """PERSONA.md sits in a directory bound read-write into the user's own
    sandbox, and `load_persona` reads it host-side, in the daemon's filesystem
    view. Whatever it returns becomes prompt text on the next task (ISSUE-339).

    Subclasses `TestLoadPersona` so the ten cases above run again against the
    hardened reader: the refusals below are only worth anything if the ordinary
    paths still work, and a guard that rejects everything would otherwise pass
    every test in this class.
    """

    def _plant_global(self, tmp_path):
        (tmp_path / "config" / "persona.md").write_text("Global persona")

    def test_a_symlink_at_persona_is_not_followed(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("TOP SECRET TOKEN")
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").symlink_to(secret)

        assert load_persona(config, user_id="alice") == "Global persona"

    def test_a_fifo_at_persona_is_refused_without_blocking(self, tmp_path):
        # Prompt assembly runs before the BrainRequest exists, so nothing
        # times this out: one mkfifo wedges every later task for this user.
        from .support.blocking import fails_if_it_blocks

        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        os.mkfifo(user_dir / "PERSONA.md")

        with fails_if_it_blocks(what="load_persona"):
            assert load_persona(config, user_id="alice") == "Global persona"

    def test_a_symlinked_config_dir_cannot_redirect_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "PERSONA.md").write_text("TOP SECRET TOKEN")
        bot_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota"
        bot_dir.mkdir(parents=True)
        (bot_dir / "config").symlink_to(elsewhere, target_is_directory=True)

        assert load_persona(config, user_id="alice") == "Global persona"

    def test_an_ancestor_symlink_inside_the_users_own_tree_is_allowed(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        base = config.nextcloud_mount_path / "Users" / "alice"
        real = base / "istota" / "real_config"
        real.mkdir(parents=True)
        (real / "PERSONA.md").write_text("Custom persona for Alice")
        (base / "istota" / "config").symlink_to(real, target_is_directory=True)

        assert load_persona(config, user_id="alice") == "Custom persona for Alice"

    def test_bot_name_substituted_in_global_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        (tmp_path / "config" / "persona.md").write_text("You are {BOT_NAME}.")

        result = load_persona(config)
        assert result == "You are Jarvis."

    def test_no_persona_files_returns_none(self, tmp_path):
        config = self._make_config(tmp_path)
        result = load_persona(config, user_id="alice")
        assert result is None


# ---------------------------------------------------------------------------
# TestLoadEmissaries
# ---------------------------------------------------------------------------


class TestLoadChannelGuidelines:
    """Guidelines are templated docs; the placeholders have to resolve.

    web.md's file-handover section names a concrete workspace path, so a
    literal ``{user_id}`` reaching the model would hand the user a broken link.
    """

    def _make_config(self, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "skills").mkdir(parents=True)
        (config_dir / "guidelines").mkdir()
        return Config(
            skills_dir=config_dir / "skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            bot_name="Istota",
        )

    def test_substitutes_user_id(self, tmp_path):
        from istota.executor import load_channel_guidelines

        config = self._make_config(tmp_path)
        (tmp_path / "config" / "guidelines" / "web.md").write_text(
            "path=/Users/{user_id}/istota/report.csv",
        )
        result = load_channel_guidelines(config, "web", "alice")
        assert result == "path=/Users/alice/istota/report.csv"
        assert "{user_id}" not in result

    def test_substitutes_bot_placeholders_too(self, tmp_path):
        from istota.executor import load_channel_guidelines

        config = self._make_config(tmp_path)
        (tmp_path / "config" / "guidelines" / "web.md").write_text(
            "{BOT_NAME} in {BOT_DIR} for {user_id}",
        )
        assert load_channel_guidelines(config, "web", "alice") == "Istota in istota for alice"

    def test_no_user_id_leaves_the_placeholder_rather_than_crashing(self, tmp_path):
        from istota.executor import load_channel_guidelines

        config = self._make_config(tmp_path)
        (tmp_path / "config" / "guidelines" / "web.md").write_text("hi {user_id}")
        assert load_channel_guidelines(config, "web") == "hi {user_id}"

    def test_missing_file_is_none(self, tmp_path):
        from istota.executor import load_channel_guidelines

        assert load_channel_guidelines(self._make_config(tmp_path), "web", "alice") is None


class TestShippedWebGuidelines:
    """The shipped web.md must actually carry the handover rule.

    Without it the model quotes a filesystem path the browser user cannot open,
    or reaches for a public share link to show someone their own file.
    """

    def _text(self):
        from pathlib import Path
        import istota
        repo = Path(istota.__file__).resolve().parents[2]
        return (repo / "config" / "guidelines" / "web.md").read_text()

    def test_points_at_the_authenticated_download_endpoint(self):
        text = self._text()
        assert "/api/chat/files?path=" in text

    def test_warns_off_a_public_share_link(self):
        assert "public share link" in self._text()

    def test_teaches_the_inline_image_form_and_its_limits(self):
        # The prompt goldens cannot witness this: `test_prompt_golden.py`
        # writes its own one-line guideline stubs into a tmp config dir, so a
        # change to the shipped file diffs nothing there.
        text = self._text()
        assert "![" in text
        for fmt in ("PNG", "JPEG", "GIF", "WebP"):
            assert fmt in text


class TestShippedTalkGuidelines:
    """The shipped talk.md must carry `share-file` and the token caveat.

    The command works end to end and was documented nowhere, and the token in
    the prompt is the room's canonical one — on a room promoted out of web
    chat it is not the Talk conversation's, and the share 404s.
    """

    def _text(self):
        from pathlib import Path
        import istota
        repo = Path(istota.__file__).resolve().parents[2]
        return (repo / "config" / "guidelines" / "talk.md").read_text()

    def test_names_the_share_file_verb(self):
        assert "nextcloud talk share-file" in self._text()

    def test_carries_the_promoted_room_caveat(self):
        assert "404" in self._text()


class TestLoadEmissaries:
    def _make_config(self, tmp_path):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        return Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

    def test_returns_none_when_absent(self, tmp_path):
        config = self._make_config(tmp_path)
        assert load_emissaries(config) is None

    def test_returns_content_when_present(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "emissaries.md").write_text("# Emissaries\n\nBe good.")
        result = load_emissaries(config)
        assert result == "# Emissaries\n\nBe good."

    def test_no_bot_name_substitution(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        (tmp_path / "config" / "emissaries.md").write_text("Agent {BOT_NAME} principles")
        result = load_emissaries(config)
        assert result == "Agent {BOT_NAME} principles"

    def test_returns_none_when_disabled(self, tmp_path):
        config = self._make_config(tmp_path)
        config.emissaries_enabled = False
        (tmp_path / "config" / "emissaries.md").write_text("# Emissaries\n\nBe good.")
        assert load_emissaries(config) is None


class TestEmissariesInPrompt:
    def _make_task(self):
        return db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type="talk", conversation_token="room1",
            created_at="2024-01-01T00:00:00",
        )

    def test_emissaries_appears_in_prompt(self):
        task = self._make_task()
        result = build_prompt(
            task, [], Config(), emissaries="# Emissaries\n\nBe good.",
        ).system
        assert "# Emissaries" in result
        assert "Be good." in result

    def test_emissaries_before_persona(self, tmp_path):
        task = self._make_task()
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        (config_dir / "persona.md").write_text("# Persona\n\nBe helpful.")
        config = Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

        result = build_prompt(
            task, [], config, emissaries="# Emissaries\n\nBe good.",
        ).system
        emissaries_pos = result.index("# Emissaries")
        persona_pos = result.index("# Persona")
        assert emissaries_pos < persona_pos

    def test_emissaries_absent_when_no_file(self):
        task = self._make_task()
        result = build_prompt(task, [], Config()).system
        assert "Emissaries" not in result


# ---------------------------------------------------------------------------
# TestPreTranscribeAttachments
# ---------------------------------------------------------------------------


_TRANSCRIBE_PATCH = "istota.executor.transcribe_audio_out_of_process"


class TestPreTranscribeAttachments:
    def test_no_attachments_returns_prompt_unchanged(self):
        assert _pre_transcribe_attachments(None, "hello") == "hello"
        assert _pre_transcribe_attachments([], "hello") == "hello"

    def test_non_audio_attachments_returns_prompt_unchanged(self):
        result = _pre_transcribe_attachments(["/tmp/photo.jpg", "/tmp/doc.pdf"], "[photo.jpg]")
        assert result == "[photo.jpg]"

    @patch(_TRANSCRIBE_PATCH)
    def test_audio_attachment_transcribed_successfully(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "remind me to buy groceries"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert "remind me to buy groceries" in result
        assert "voice.mp3" in result
        assert "Transcribed voice message:" in result
        assert mock_transcribe.call_count == 1
        assert mock_transcribe.call_args[0][0] == "/tmp/voice.mp3"

    @patch(_TRANSCRIBE_PATCH)
    def test_empty_prompt_becomes_the_transcript(self, mock_transcribe):
        """A voice memo sent with nothing typed: the transcript is the prompt."""
        mock_transcribe.return_value = {"status": "ok", "text": "call the plumber"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "")
        assert result.startswith("Transcribed voice message: call the plumber")

    @patch(_TRANSCRIBE_PATCH)
    def test_accompanying_text_is_kept(self, mock_transcribe):
        """Text sent alongside a voice memo is the instruction the audio was
        sent under — the transcript is appended, never a replacement."""
        mock_transcribe.return_value = {"status": "ok", "text": "call the plumber"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "summarize this")
        assert result.startswith("summarize this")
        assert "call the plumber" in result

    @patch(_TRANSCRIBE_PATCH)
    def test_transcription_failure_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "error", "error": "corrupted file"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    @patch(_TRANSCRIBE_PATCH)
    def test_transcription_exception_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.side_effect = RuntimeError("boom")
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    @patch(_TRANSCRIBE_PATCH)
    def test_faster_whisper_not_installed_returns_prompt_unchanged(self, mock_transcribe):
        """The dependency is now missing *in the child*, which reports it as an
        ordinary error result rather than raising in the daemon."""
        mock_transcribe.return_value = {
            "status": "error",
            "error": "faster-whisper not installed. Install with: uv sync --extra whisper",
        }
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    @patch(_TRANSCRIBE_PATCH)
    def test_mixed_audio_and_non_audio_attachments(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "schedule a meeting"}
        result = _pre_transcribe_attachments(
            ["/tmp/photo.jpg", "/tmp/memo.m4a", "/tmp/doc.pdf"],
            "[photo.jpg] [memo.m4a]",
        )
        assert "schedule a meeting" in result
        assert "memo.m4a" in result
        assert mock_transcribe.call_count == 1
        assert mock_transcribe.call_args[0][0] == "/tmp/memo.m4a"

    @patch(_TRANSCRIBE_PATCH)
    def test_multiple_audio_attachments(self, mock_transcribe):
        mock_transcribe.side_effect = [
            {"status": "ok", "text": "first part"},
            {"status": "ok", "text": "second part"},
        ]
        result = _pre_transcribe_attachments(
            ["/tmp/a.mp3", "/tmp/b.wav"],
            "[a.mp3] [b.wav]",
        )
        assert "first part" in result
        assert "second part" in result
        assert "a.mp3" in result
        assert "b.wav" in result

    @patch(_TRANSCRIBE_PATCH)
    def test_empty_transcription_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "  "}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    def test_all_audio_extensions_recognized(self):
        for ext in ["mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"]:
            assert ext in _AUDIO_EXTENSIONS


class TestPreTranscriptionStaysOutOfTheDaemon:
    """ISSUE-273.

    `import faster_whisper` costs ~293 MB of resident set, and each
    construct-transcribe-drop cycle leaves ~450 MB on glibc's free lists that
    the daemon never calls `malloc_trim` to get back. Five voice messages over
    one 66-hour run walked the scheduler from 820 MB to 2894 MB in four
    discrete steps, each within three minutes of a transcription. None of that
    memory may be spent in the daemon, so these tests pin *where* the work
    runs, not just what it returns.
    """

    def test_it_spawns_the_whisper_cli_instead_of_importing_the_model(self):
        with patch("istota.skills.whisper.out_of_process.subprocess.Popen") as popen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.communicate.return_value = (
                json.dumps({"status": "ok", "text": "buy milk"}),
                "",
            )
            popen.return_value = proc
            result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "")

        argv = popen.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:5] == ["-P", "-m", "istota.skills.whisper", "transcribe"]
        assert "buy milk" in result

    def test_the_in_process_transcriber_is_never_called(self):
        """The seam that carried the leak. `transcribe.transcribe_audio` is the
        function that pulls faster_whisper into whichever process calls it."""
        with patch("istota.skills.whisper.transcribe.transcribe_audio") as in_process, patch(
            "istota.skills.whisper.out_of_process.subprocess.Popen"
        ) as popen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.communicate.return_value = (json.dumps({"status": "ok", "text": "hi"}), "")
            popen.return_value = proc
            _pre_transcribe_attachments(["/tmp/voice.mp3"], "")

        in_process.assert_not_called()

    def test_the_timeout_budget_is_shared_across_the_send_not_per_file(self):
        """This runs on a worker thread before the brain call, so
        `scheduler.task_timeout_minutes` does not cover it. A per-file limit
        would let a five-attachment send hold the worker for five times the
        bound — the stall the timeout exists to prevent, not a smaller one."""
        with patch(_TRANSCRIBE_PATCH) as mock_transcribe:
            mock_transcribe.return_value = {"status": "ok", "text": "x"}
            _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav", "/tmp/c.m4a"], "")

        budgets = [c.kwargs["timeout"] for c in mock_transcribe.call_args_list]
        assert len(budgets) == 3
        # Strictly decreasing: each call gets what is left, not a fresh grant.
        assert budgets == sorted(budgets, reverse=True)
        assert budgets[0] <= _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS
        assert sum(budgets) < 3 * _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS

    def test_files_after_the_budget_runs_out_are_skipped_and_earlier_text_kept(
        self, monkeypatch,
    ):
        def eat_the_budget(path, timeout=None):
            # First file consumes the whole budget, as a wedged child would.
            if path.endswith("a.mp3"):
                _clock[0] += _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS + 1
                return {"status": "ok", "text": "first one landed"}
            raise AssertionError(f"should not have been called for {path}")

        _clock = [1000.0]
        monotonic_spy(monkeypatch, executor, lambda: _clock[0])
        with patch(_TRANSCRIBE_PATCH, side_effect=eat_the_budget):
            result = _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav"], "")

        assert "first one landed" in result

    def test_each_audio_file_gets_its_own_process(self):
        """One process per file, so the ratchet resets between them rather than
        accumulating across a multi-attachment send."""
        with patch("istota.skills.whisper.out_of_process.subprocess.Popen") as popen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.communicate.return_value = (json.dumps({"status": "ok", "text": "x"}), "")
            popen.return_value = proc
            _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav"], "")

        assert popen.call_count == 2


# Image preparation moved out of the executor into `image_attachments`; its
# tests live in `tests/test_image_attachments.py` and the executor-side
# integration in `tests/test_executor_images.py`.


# ---------------------------------------------------------------------------
# TestPromptOutputTarget
# ---------------------------------------------------------------------------


class TestPromptOutputTarget:
    """Verify that source_type and output_target appear in the prompt header."""

    def _make_task(self, source_type="talk", output_target=None):
        return db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type=source_type, conversation_token="room1",
            output_target=output_target,
        )

    def test_talk_source_and_target_in_prompt(self):
        task = self._make_task(source_type="talk")
        result = build_prompt(
            task, [], Config(),
            source_type="talk", output_target="talk",
        ).system
        assert "Source: talk" in result
        assert "Output target: talk" in result

    def test_scheduled_source_with_email_target(self):
        task = self._make_task(source_type="scheduled", output_target="email")
        result = build_prompt(
            task, [], Config(),
            source_type="scheduled", output_target="email",
        ).system
        assert "Source: scheduled" in result
        assert "Output target: email" in result

    def test_defaults_when_no_output_target(self):
        task = self._make_task(source_type="cli")
        result = build_prompt(task, [], Config()).system
        assert "Source: cli" in result
        assert "Output target: text" in result

    def test_email_tool_line_distinguishes_send_and_output(self):
        task = self._make_task(source_type="talk")
        result = build_prompt(task, [], Config()).system
        assert "email send" in result
        assert "email output" in result
        assert "Only use `output` when this task arrived as an incoming email" in result


# ---------------------------------------------------------------------------
# TestDetectNotificationReply
# ---------------------------------------------------------------------------


class TestDetectNotificationReply:
    def test_returns_parent_for_scheduled_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            # Create a completed scheduled parent task with a talk_response_id
            parent_id = db.create_task(
                conn, prompt="Drink water", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Time to drink water!")
            # Set talk_response_id on the parent
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            # Create a reply task
            reply_id = db.create_task(
                conn, prompt="Drinking", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is not None
            assert result.id == parent_id
            assert result.source_type == "scheduled"

    def test_returns_parent_for_briefing_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Morning briefing", user_id="alice",
                source_type="briefing", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Good morning!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (99, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Thanks", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=99,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is not None
            assert result.source_type == "briefing"

    def test_returns_none_for_talk_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="What's up?", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Not much!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (50, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Cool", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=50,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is None

    def test_returns_none_when_no_reply_to_talk_id(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Hello", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            task = db.get_task(conn, task_id)

            result = _detect_notification_reply(task, Config(), conn)
            assert result is None

    def test_returns_none_when_no_conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Hello", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            task = db.get_task(conn, task_id)

        result = _detect_notification_reply(task, Config(), None)
        assert result is None


# ---------------------------------------------------------------------------
# TestNotificationReplyContextScoping
# ---------------------------------------------------------------------------


class TestNotificationReplyContextScoping:
    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        # exist_ok: a test may build two configs from one tmp_path to compare
        # how the hook behaves across settings.
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.executor.subprocess.run")
    def test_notification_reply_scopes_context(self, mock_run, tmp_path):
        """Reply to a scheduled notification gets scoped context, not full history."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            # Create completed scheduled parent
            parent_id = db.create_task(
                conn, prompt="Drink water", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(
                conn, parent_id, "completed",
                result="Time to hydrate! Remember to drink water.",
            )
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            # Create reply task
            reply_id = db.create_task(
                conn, prompt="Drinking", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(
                reply_task, config, [], conn=conn,
            )

        # Check the prompt contains the notification hint
        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "replying to a scheduled notification" in prompt_text
        assert "respond very briefly" in prompt_text
        assert "Time to hydrate" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_notification_reply_skips_full_context(self, mock_run, tmp_path):
        """Notification reply should not call _build_talk_api_context."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Reminder", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Do the thing")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Done", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
                from istota.executor import execute_task
                execute_task(reply_task, config, [], conn=conn)
                mock_talk_ctx.assert_not_called()

    @patch("istota.executor.subprocess.run")
    def test_non_notification_reply_uses_normal_context(self, mock_run, tmp_path):
        """Reply to a regular talk message should use normal context loading."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            # Create completed talk parent (not scheduled)
            parent_id = db.create_task(
                conn, prompt="What's the weather?", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="It's sunny!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Thanks", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
                mock_talk_ctx.return_value = (None, set())  # Fall through to DB context
                from istota.executor import execute_task
                execute_task(reply_task, config, [], conn=conn)
                # Normal context path should be attempted
                mock_talk_ctx.assert_called_once()

        # Prompt should NOT contain notification hint
        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "replying to a scheduled notification" not in prompt_text


# ---------------------------------------------------------------------------
# TestRecencyWindow
# ---------------------------------------------------------------------------


class TestRecencyWindowTalk:
    def _make_config(self, recency_hours=2.0, min_messages=10):
        from istota.config import ConversationConfig
        config = Config()
        config.conversation = ConversationConfig(
            context_recency_hours=recency_hours,
            context_min_messages=min_messages,
        )
        return config

    def _make_talk_msg(self, message_id, timestamp, content="msg"):
        return db.TalkMessage(
            message_id=message_id,
            actor_id="alice",
            actor_display_name="Alice",
            is_bot=False,
            content=content,
            timestamp=timestamp,
            actions_taken=None,
            message_role="user",
            task_id=None,
        )

    def test_disabled_when_zero(self):
        config = self._make_config(recency_hours=0)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(20)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 20

    def test_empty_messages(self):
        config = self._make_config()
        assert _apply_recency_window_talk([], config) == []

    def test_fewer_than_min_returns_all(self):
        config = self._make_config(min_messages=10)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(8)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 8

    def test_all_within_window_returns_all(self):
        config = self._make_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 15 messages all within last hour
        msgs = [self._make_talk_msg(i, now - (15 - i) * 60) for i in range(15)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 15

    def test_trims_old_messages_beyond_min(self):
        config = self._make_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 5 messages from 10 hours ago
        old = [self._make_talk_msg(i, now - 36000 + i) for i in range(5)]
        # 10 messages from last 30 minutes
        recent = [self._make_talk_msg(10 + i, now - (10 - i) * 60) for i in range(10)]
        msgs = old + recent
        result = _apply_recency_window_talk(msgs, config)
        # 5 guaranteed recent (min) is less than the 10 recent, but all 10 recent
        # are within the 2h window, so we get 10 (within window) + 0 old = 10
        # Wait: min_messages=5 means guaranteed = last 5, older = first 10
        # Of the first 10 (5 old + 5 recent), only the 5 recent are within window
        assert len(result) == 10  # 5 within window from older + 5 guaranteed

    def test_guaranteed_minimum_always_kept(self):
        config = self._make_config(recency_hours=1.0, min_messages=10)
        now = 1000000
        # 20 messages, all from 5 hours ago
        msgs = [self._make_talk_msg(i, now - 18000 + i) for i in range(20)]
        # newest is at now - 18000 + 19, all within ~0 of each other
        # but the newest is the reference, so cutoff = newest - 3600
        # all messages are within 20 seconds of each other, so all within window
        # Let me make a better test: spread them out
        old_msgs = [self._make_talk_msg(i, now - 50000 + i * 100) for i in range(15)]
        recent_msgs = [self._make_talk_msg(15 + i, now - 60 + i * 10) for i in range(5)]
        msgs = old_msgs + recent_msgs
        result = _apply_recency_window_talk(msgs, config)
        # 10 guaranteed (last 10), older 10 checked against window
        # window = newest - 3600, old msgs are ~50000s ago, way outside
        # So result = 10 guaranteed minimum
        assert len(result) == 10

    def test_partial_window_inclusion(self):
        """Some older messages within window, some outside."""
        config = self._make_config(recency_hours=1.0, min_messages=3)
        now = 1000000
        # 2 messages from 5 hours ago (outside window)
        outside = [self._make_talk_msg(i, now - 18000 + i) for i in range(2)]
        # 3 messages from 30 minutes ago (within window)
        inside = [self._make_talk_msg(10 + i, now - 1800 + i * 60) for i in range(3)]
        # 3 messages from 5 minutes ago (guaranteed min)
        recent = [self._make_talk_msg(20 + i, now - 300 + i * 60) for i in range(3)]
        msgs = outside + inside + recent
        result = _apply_recency_window_talk(msgs, config)
        # guaranteed = last 3 (recent), older = outside + inside
        # inside (3) within window, outside (2) not
        assert len(result) == 6  # 3 inside + 3 guaranteed


class TestRecencyWindowDb:
    def _make_config(self, recency_hours=2.0, min_messages=10):
        from istota.config import ConversationConfig
        config = Config()
        config.conversation = ConversationConfig(
            context_recency_hours=recency_hours,
            context_min_messages=min_messages,
        )
        return config

    def _make_msg(self, msg_id, created_at, prompt="q", result="a"):
        return db.ConversationMessage(
            id=msg_id, prompt=prompt, result=result, created_at=created_at,
        )

    def test_disabled_when_zero(self):
        config = self._make_config(recency_hours=0)
        msgs = [self._make_msg(i, "2026-02-23 12:00:00") for i in range(20)]
        result = _apply_recency_window_db(msgs, config)
        assert len(result) == 20

    def test_empty_returns_empty(self):
        config = self._make_config()
        assert _apply_recency_window_db([], config) == []

    def test_fewer_than_min_returns_all(self):
        config = self._make_config(min_messages=10)
        msgs = [self._make_msg(i, f"2026-02-23 12:0{i}:00") for i in range(5)]
        result = _apply_recency_window_db(msgs, config)
        assert len(result) == 5

    def test_trims_old_db_messages(self):
        config = self._make_config(recency_hours=1.0, min_messages=3)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # 4h before newest
            self._make_msg(2, "2026-02-23 09:00:00"),  # 3h before newest
            self._make_msg(3, "2026-02-23 11:30:00"),  # 30m before newest
            self._make_msg(4, "2026-02-23 11:45:00"),  # 15m before newest
            self._make_msg(5, "2026-02-23 12:00:00"),  # newest
        ]
        result = _apply_recency_window_db(msgs, config)
        # min=3 guaranteed (ids 3,4,5), older=[1,2], 1 and 2 are >1h old
        assert len(result) == 3
        assert [m.id for m in result] == [3, 4, 5]

    def test_keeps_within_window_beyond_min(self):
        config = self._make_config(recency_hours=2.0, min_messages=2)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # outside
            self._make_msg(2, "2026-02-23 10:30:00"),  # within 2h
            self._make_msg(3, "2026-02-23 11:00:00"),  # within 2h
            self._make_msg(4, "2026-02-23 11:30:00"),  # guaranteed
            self._make_msg(5, "2026-02-23 12:00:00"),  # guaranteed (newest)
        ]
        result = _apply_recency_window_db(msgs, config)
        # guaranteed = [4,5], older = [1,2,3], within window = [2,3]
        assert len(result) == 4
        assert [m.id for m in result] == [2, 3, 4, 5]

    def test_unparseable_created_at_skips_filter(self):
        config = self._make_config(recency_hours=1.0, min_messages=2)
        msgs = [self._make_msg(i, "not-a-date") for i in range(5)]
        result = _apply_recency_window_db(msgs, config)
        # Can't parse newest, returns all
        assert len(result) == 5


# ---------------------------------------------------------------------------
# TestBuildPromptRecalledMemories
# ---------------------------------------------------------------------------


class TestBuildPromptRecalledMemories:
    def _make_task(self, **overrides):
        defaults = {
            "id": 1, "prompt": "test prompt", "user_id": "alice",
            "source_type": "talk", "status": "running",
        }
        defaults.update(overrides)
        return db.Task(**defaults)

    def test_recalled_section_included_when_provided(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(
            task, [], config,
            recalled_memories="- [memory_file] User prefers dark mode\n- [conversation] Discussed project X",
        ).user
        assert "Recalled memories (from search)" in prompt
        assert "User prefers dark mode" in prompt
        assert "Discussed project X" in prompt

    def test_recalled_section_absent_when_none(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(task, [], config, recalled_memories=None).user
        assert "Recalled memories" not in prompt

    def test_recalled_section_absent_when_empty_string(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(task, [], config, recalled_memories="").user
        assert "Recalled memories" not in prompt

    def test_recalled_section_after_dated_memories(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(
            task, [], config,
            dated_memories="- Dated memory entry",
            recalled_memories="- Recalled entry",
        ).user
        dated_pos = prompt.index("Recent context (from previous days)")
        recalled_pos = prompt.index("Recalled memories (from search)")
        assert dated_pos < recalled_pos


# ---------------------------------------------------------------------------
# TestRecallMemories
# ---------------------------------------------------------------------------


class TestRecallMemories:
    def test_returns_none_when_disabled(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=True, auto_recall=False))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task, task.prompt) is None

    def test_returns_none_when_search_not_enabled(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=False, auto_recall=True))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task, task.prompt) is None

    def test_returns_none_when_skip_memory(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=True, auto_recall=True))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task, task.prompt, skip_memory=True) is None

    @patch("istota.memory.search.search")
    def test_formats_results(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_result = MagicMock()
        mock_result.content = "User likes Python"
        mock_result.source_type = "memory_file"
        mock_search.return_value = [mock_result]

        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True, auto_recall_limit=5),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(id=1, prompt="what language?", user_id="alice", source_type="talk", status="running")

        conn = MagicMock()
        result = _recall_memories(config, conn, task, task.prompt)
        assert result is not None
        assert "[memory_file]" in result
        assert "User likes Python" in result

    @patch("istota.memory.search.search")
    def test_returns_none_when_no_results(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_search.return_value = []
        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, MagicMock(), task, task.prompt) is None

    @patch("istota.memory.search.search")
    def test_includes_channel_in_search(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_search.return_value = []
        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(
            id=1, prompt="test", user_id="alice", source_type="talk", status="running",
            conversation_token="room123",
        )
        _recall_memories(config, MagicMock(), task, task.prompt)
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["include_user_ids"] == ["channel:room123"]


# ---------------------------------------------------------------------------
# TestApplyMemoryCap
# ---------------------------------------------------------------------------


class TestApplyMemoryCap:
    def test_unlimited_when_zero(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=0)
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, "C" * 100, "D" * 100)
        assert len(u) == 100
        assert len(d) == 100
        assert len(c) == 100
        assert len(r) == 100

    def test_no_truncation_under_cap(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=500)
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, "C" * 100, "D" * 100)
        assert len(u) == 100
        assert len(d) == 100
        assert len(c) == 100
        assert len(r) == 100

    def test_truncates_recalled_first(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=200)
        # total = 300, cap = 200, over = 100, recalled = 100 → removed entirely
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is None

    def test_truncates_dated_after_recalled(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        # total = 300, cap = 100, over = 200
        # recalled (100) removed → over = 100
        # dated (100) removed → over = 0
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d is None
        assert r is None

    def test_partial_truncation(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=250)
        # total = 300, cap = 250, over = 50
        # recalled (100) → trim to 50 chars + truncation marker
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is not None
        assert "truncated" in r

    def test_handles_all_none(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        u, d, c, r, k, _pb = _apply_memory_cap(config, None, None, None, None)
        assert u is None and d is None and c is None and r is None


# ---------------------------------------------------------------------------
# TestDatedMemoriesAutoLoad
# ---------------------------------------------------------------------------


class TestDatedMemoriesAutoLoad:
    def _make_config(self, tmp_path, auto_load_days=3, sleep_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text("")
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        from istota.config import SleepCycleConfig
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount,
            sleep_cycle=SleepCycleConfig(
                enabled=sleep_enabled,
                auto_load_dated_days=auto_load_days,
            ),
        )

    def _make_task(self, conn, source_type="talk"):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type=source_type)
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_loaded_when_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        # Create a dated memory file
        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- User prefers dark mode")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "User prefers dark mode" in prompt_text
        assert "Recent context (from previous days)" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_skipped_for_briefing(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3)
        # Add briefing skill with exclude_memory so flag-based check works
        briefing_dir = config.skills_dir / "briefing"
        briefing_dir.mkdir(parents=True)
        (briefing_dir / "skill.toml").write_text(
            'description = "Briefing"\nsource_types = ["briefing"]\nexclude_memory = true\n'
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Should not appear")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="briefing")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Should not appear" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_none_when_zero_days(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=0)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Should not appear")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Recent context (from previous days)" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_none_when_sleep_disabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3, sleep_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Recent context (from previous days)" not in prompt_text


# =============================================================================
# TestConfirmationContext
# =============================================================================


class TestConfirmationContext:
    def _make_task(self, **kwargs):
        defaults = dict(
            id=1, status="running", source_type="email",
            user_id="carol", prompt="Emissary reply from bob@ext.com",
            conversation_token="room1",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def _make_config(self, tmp_path):
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def test_confirmation_context_included_in_prompt(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        previous_output = "I drafted a reply: 'How about Tuesday at 3pm?' Should I send this?"

        prompt = build_prompt(
            task, [], config,
            confirmation_context=previous_output,
        ).user

        assert "## Confirmed action" in prompt
        assert "How about Tuesday at 3pm?" in prompt
        assert "Do not re-draft" in prompt
        assert "`istota-skill email send`" in prompt

    def test_no_confirmation_context_when_none(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()

        prompt = build_prompt(task, [], config, confirmation_context=None).user

        assert "## Confirmed action" not in prompt

    def test_confirmation_context_appears_before_user_request(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()

        prompt = build_prompt(
            task, [], config,
            confirmation_context="Previous draft here",
        ).user

        confirmed_pos = prompt.index("## Confirmed action")
        request_pos = prompt.index("## User's request")
        assert confirmed_pos < request_pos


# ---------------------------------------------------------------------------
# TestDetectMalformedResult
# ---------------------------------------------------------------------------


class TestDetectMalformedResult:
    """Test detection of malformed model output (leaked XML, disproportionately short)."""

    def test_normal_text_passes(self):
        assert detect_malformed_result("Here are three painting studios in Lisbon...") is None

    def test_short_normal_text_passes(self):
        assert detect_malformed_result("Done.") is None

    def test_empty_string_passes(self):
        assert detect_malformed_result("") is None

    def test_none_passes(self):
        assert detect_malformed_result(None) is None

    def test_whitespace_only_passes(self):
        assert detect_malformed_result("   \n  ") is None

    def test_xml_parameter_close_detected(self):
        result = detect_malformed_result("</parameter>\n</invoke>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_invoke_close_detected(self):
        result = detect_malformed_result("</invoke>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_invoke_open_detected(self):
        result = detect_malformed_result("<invoke name='foo'>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_antml_prefix_detected(self):
        result = detect_malformed_result("</thinking>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_parameter_open_detected(self):
        result = detect_malformed_result("<parameter name='path'>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_in_long_response_passes(self):
        """XML patterns embedded in a substantive response should not trigger detection."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes the model to emit "
            "raw XML fragments instead of coherent responses. Here is the analysis..."
        )
        assert detect_malformed_result(text) is None

    # --- Strict mode (output_target="talk") ---

    def test_talk_xml_in_prose_detected(self):
        """XML patterns embedded in prose should be caught in strict Talk mode."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes problems."
        )
        # Non-strict: passes (enough non-syntax content)
        assert detect_malformed_result(text) is None
        # Strict (Talk): flagged
        result = detect_malformed_result(text, output_target="talk")
        assert result is not None
        assert "Talk output" in result

    def test_talk_xml_in_code_fence_passes(self):
        """XML patterns inside code fences should not trigger in strict mode."""
        text = (
            "Here's an example of the XML format:\n\n"
            "```xml\n<parameter name='path'>/foo</parameter>\n```\n\n"
            "This shows the structure."
        )
        assert detect_malformed_result(text, output_target="talk") is None

    def test_talk_clean_markdown_passes(self):
        """Normal markdown should not trigger strict mode."""
        text = "## Results\n\n- Item one\n- Item two\n\nHere's a **bold** conclusion."
        assert detect_malformed_result(text, output_target="talk") is None

    def test_talk_xml_outside_fence_with_fenced_xml_detected(self):
        """XML outside code fences should be caught even if fenced XML exists."""
        text = (
            "```xml\n<parameter>ok</parameter>\n```\n\n"
            "And then </invoke> happened."
        )
        result = detect_malformed_result(text, output_target="talk")
        assert result is not None

    def test_both_target_uses_strict_mode(self):
        text = "Something </invoke> happened"
        assert detect_malformed_result(text, output_target="both") is not None

    def test_all_target_uses_strict_mode(self):
        text = "Something </invoke> happened"
        assert detect_malformed_result(text, output_target="all") is not None

    def test_email_target_uses_lenient_mode(self):
        """Email target should use lenient mode (XML patterns allowed in longer text)."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes problems."
        )
        assert detect_malformed_result(text, output_target="email") is None


# ---------------------------------------------------------------------------
# TestComposeFullResult
# ---------------------------------------------------------------------------


def _make_task(
    *,
    source_type: str = "talk",
    heartbeat_silent: bool = False,
    scheduled_job_id=None,
    task_id: int = 1,
):
    """Build a Task for compose tests. Only the fields _is_automated_task
    actually reads need to be set."""
    return _db.Task(
        id=task_id,
        status="running",
        source_type=source_type,
        user_id="test_user",
        prompt="",
        conversation_token="",
        heartbeat_silent=heartbeat_silent,
        scheduled_job_id=scheduled_job_id,
    )


def _block(prefix: str, target_chars: int) -> str:
    """Build a substantive text block of approximately target_chars."""
    sentence = (
        f"{prefix} The data shows a clear pattern, with consistent measurements "
        f"across the observed window and reasonable confidence in the result. "
    )
    n = target_chars // len(sentence) + 1
    return (sentence * n).strip()


class TestComposeFullResult:
    """Mechanism B (terse-recovery) — tests against the redesigned function."""

    # --- pass-through cases ---

    def test_no_trace_returns_result_as_is(self):
        assert _compose_full_result("Done.", []) == "Done."

    def test_no_substantial_blocks_returns_result(self):
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "tool", "text": "Read file.py"},
            {"type": "text", "text": "Running the search."},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_result_not_overridden(self):
        """A non-terse result must never be replaced — the regression test
        for the 2026-05-08 incident: 5KB skill-doc preamble + 900-char real
        summary previously got concatenated."""
        preamble = _block("Preamble.", 5000)
        real_summary = _block("Summary.", 900)
        trace = [
            {"type": "text", "text": preamble},
            {"type": "tool", "text": "git log"},
            {"type": "tool", "text": "Read file"},
        ]
        result = _compose_full_result(real_summary, trace)
        assert result == real_summary

    def test_empty_trace_entries_ignored(self):
        trace = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "   "},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_no_tools_no_recovery(self):
        """A substantial result with no tool boundary in trace: still no
        override — gate is on terseness, not trace shape."""
        block = _block("Findings.", 800)
        trace = [{"type": "text", "text": block}]
        long_result = _block("Result.", 400)
        result = _compose_full_result(long_result, trace)
        assert result == long_result

    # --- terse-pattern recovery ---

    def test_see_above_with_substantial_pre_tool_region(self):
        """Canonical ISSUE-025 shape: substantial text → tool → terse result."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        assert result == findings

    def test_terse_short_result_does_not_reach_back_past_a_tool(self):
        """A short result that isn't an explicit back-reference is a real (if
        brief) answer. Reaching back past a tool call for it would promote
        mid-turn narration — ISSUE-211. Only the trailing region qualifies."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "Operation completed.", trace, task=_make_task(),
        )
        assert result == "Operation completed."

    def test_terse_short_result_with_substantial_trailing_region(self):
        """Result < 150 chars but not a known reference — the region *after*
        the last tool call is the model's final message, so it still wins."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "tool", "text": "Write file"},
            {"type": "text", "text": findings},
        ]
        result = _compose_full_result(
            "Operation completed.", trace, task=_make_task(),
        )
        assert result == findings

    def test_done_with_substantial_pre_tool_region(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        assert _compose_full_result("Done.", trace, task=_make_task()) == findings

    def test_empty_result_with_substantial_trailing_region(self):
        """An empty result with real text after the last tool call: the brain
        lost the final message, the trace still has it."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "tool", "text": "Write file"},
            {"type": "text", "text": findings},
        ]
        assert _compose_full_result("", trace, task=_make_task()) == findings

    # --- terse but no qualifying region ---

    def test_terse_result_short_trailing_region_no_override(self):
        """Trailing region must be ≥ TRAILING_REGION_MIN_CHARS to override."""
        short_block = "Brief note about the result. " * 5  # ~145 chars
        trace = [
            {"type": "text", "text": short_block},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        # Region < 500 chars → no override
        assert result == "See above."

    def test_terse_result_region_already_in_result(self):
        """If the trailing region appears verbatim in result_text, no override."""
        block = _block("Findings.", 800)
        trace = [{"type": "text", "text": block}]
        # Result already contains the region (followed by a tag) — no override
        embedded = block + "\n\n[done]"
        result = _compose_full_result(embedded, trace, task=_make_task())
        assert result == embedded

    # --- streaming fragment aggregation ---

    def test_streaming_fragments_aggregate_into_one_region(self):
        """Many small text events between trace boundaries should aggregate."""
        # 12 fragments × ~50 chars = ~600 chars total, joined with \n\n
        fragments = [
            f"Fragment {i}: more detail about the analysis goes here. "
            for i in range(12)
        ]
        trace = [
            *({"type": "text", "text": f} for f in fragments),
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        # Should be the joined fragments, not the terse result
        assert "Fragment 0" in result
        assert "Fragment 11" in result
        assert result != "See above."

    # --- automated-task gate ---

    def test_scheduled_task_no_terse_recovery(self):
        """Mechanism B is gated for scheduled tasks regardless of trace."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace, task=_make_task(source_type="scheduled"),
        )
        assert result == "See above."

    def test_briefing_task_no_terse_recovery(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace, task=_make_task(source_type="briefing"),
        )
        assert result == "See above."

    def test_heartbeat_silent_blocks_terse_recovery(self):
        """heartbeat_silent flag gates Mechanism B even when source_type
        isn't in the explicit set."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace,
            task=_make_task(source_type="cli", heartbeat_silent=True),
        )
        assert result == "See above."

    def test_scheduled_job_id_blocks_terse_recovery(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace,
            task=_make_task(source_type="cli", scheduled_job_id=42),
        )
        assert result == "See above."

    def test_no_task_means_no_automated_gate(self):
        """Backwards-compat: callers passing no task get the original gating
        behavior (no automated-task gate fires)."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace)
        assert result == findings

    # --- regression — 2026-05-08 incident ---

    def test_regression_5KB_preamble_900_char_summary_scheduled(self):
        """The 2026-05-08 cron incident: 5KB skill-doc preamble + 900-char
        real summary on a scheduled task. Both gates (substantial result AND
        scheduled source_type) must independently block override."""
        preamble = _block("Skill enumeration.", 5000)
        real_summary = _block("Daily devlog summary.", 900)
        trace = [
            {"type": "text", "text": preamble},
            {"type": "tool", "text": "git log"},
            {"type": "tool", "text": "Read DEVLOG.md"},
        ]
        result = _compose_full_result(
            real_summary, trace, task=_make_task(source_type="scheduled"),
        )
        assert result == real_summary
        assert "Skill enumeration." not in result


class TestComposeFullResultCM:
    """Mechanism A (CM-aware) — segmentation by cm_boundary."""

    def test_cm_boundary_uses_last_substantial_segment(self):
        pre_cm = _block("PreCM.", 450)
        post_cm = _block("PostCM.", 450)
        trace = [
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
        ]
        doubled_result = f"{pre_cm}\n\n{post_cm}"
        assert _compose_full_result(doubled_result, trace) == post_cm

    def test_cm_boundary_with_thin_last_segment_trusts_result(self):
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Read file"},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Now let me write the patch."},
        ]
        good_result = _block("Result.", 450)
        assert _compose_full_result(good_result, trace) == good_result

    def test_cm_boundary_with_tools_after_last_cm(self):
        real_response = _block("Response.", 450)
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Write file"},
            {"type": "tool", "text": "Edit config"},
        ]
        # Last segment has no text (only tools) → walk back to pre-CM real_response.
        # Equal to result_text (after strip), so we return result_text unchanged.
        assert _compose_full_result(real_response, trace) == real_response

    def test_cm_boundary_empty_last_segment_trusts_result(self):
        real_response = _block("Response.", 450)
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
        ]
        assert _compose_full_result(real_response, trace) == real_response

    def test_multiple_cm_boundaries_uses_last_substantial(self):
        block1 = _block("Block1.", 450)
        block2 = _block("Block2.", 450)
        trace = [
            {"type": "text", "text": block1},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Let me rethink."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block2},
            {"type": "cm_boundary"},
        ]
        doubled = f"{block1}\n\n{block2}"
        assert _compose_full_result(doubled, trace) == block2

    def test_cm_with_multiple_texts_in_last_segment(self):
        block1 = _block("BlockA.", 450)
        block2 = _block("BlockB.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block1},
            {"type": "text", "text": block2},
        ]
        # Adjacent text blocks with nothing between them are one streamed
        # message and are joined.
        result = _compose_full_result("Doubled.", trace)
        assert result == f"{block1}\n\n{block2}"

    def test_cm_segment_split_by_a_tool_keeps_only_the_trailing_part(self):
        """The original ISSUE-026 fixture, kept with its new expectation.

        "A tool is NOT a CM-mode delimiter" was the documented property before
        ISSUE-211; the finality rule deliberately revokes it, so the same shape
        now yields the post-tool block alone rather than both joined.
        """
        block1 = _block("BlockA.", 450)
        block2 = _block("BlockB.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": block2},
        ]
        assert _compose_full_result("Doubled.", trace) == block2

    def test_cm_answer_split_by_a_trailing_tool_falls_back_to_result(self):
        """The cost of the revocation, pinned deliberately: an answer split by
        a trailing tool call whose tail is under the CM floor recovers nothing
        and keeps the (CM-truncated) result rather than gluing the halves."""
        part_a = _block("PartA.", 450)
        trace = [
            {"type": "cm_boundary"},
            {"type": "text", "text": part_a},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": "Short tail."},
        ]
        assert _compose_full_result("CM-truncated result.", trace) == "CM-truncated result."

    def test_cm_recovery_stops_at_the_last_tool_call(self):
        """ISSUE-211: a block the model wrote *before* issuing another tool
        call is mid-turn narration, not part of the final message, so CM
        recovery must not glue it onto the answer."""
        narration = _block("Let me look this up.", 450)
        answer = _block("Answer.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": narration},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": answer},
        ]
        result = _compose_full_result("Doubled.", trace)
        assert result == answer

    def test_cm_real_pattern_pre_and_post_cm_responses(self):
        pre_cm = (
            "Found it. The issue is clear from the trace data. "
            "The current fix handles two things correctly: "
            "filtering CM replay events and deduplicating block IDs. "
            "But it misses the case where CM fires between two "
            "legitimate text events with different message IDs. "
            "Both get through because neither has context_management set."
        )
        post_cm = (
            "Found the issue. Let me trace through what happened. "
            "The trace has two text entries — the analysis and the "
            "conclusion. The result text from Claude Code contains "
            "everything concatenated. The compose function needs "
            "CM-aware segmentation to pick the right version."
        )
        trace = [
            {"type": "tool", "text": "Read stream_parser.py"},
            {"type": "tool", "text": "Read executor.py"},
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
            {"type": "cm_boundary"},
        ]
        doubled_result = f"{post_cm}\n\n{pre_cm}"
        assert _compose_full_result(doubled_result, trace) == post_cm

    def test_cm_aware_runs_for_scheduled_tasks(self):
        """The source-type gate is Mechanism-B-only; CM-aware always runs."""
        pre_cm = _block("PreCM.", 450)
        post_cm = _block("PostCM.", 450)
        trace = [
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
        ]
        doubled_result = f"{pre_cm}\n\n{post_cm}"
        result = _compose_full_result(
            doubled_result, trace, task=_make_task(source_type="scheduled"),
        )
        assert result == post_cm

    def test_cm_recovered_equals_result_no_override(self):
        """When the last substantial segment IS result_text after strip,
        no override (avoids no-op log entries)."""
        block = _block("Block.", 450)
        trace = [
            {"type": "text", "text": block},
            {"type": "cm_boundary"},
        ]
        # No segment after final CM has text; walking back finds `block`.
        # If result_text is exactly block, no override.
        assert _compose_full_result(block, trace) == block


class TestFinalAnswerGuard:
    """ISSUE-211 — mid-turn narration must never become the durable reply.

    The guidelines promise the model that text written between tool calls is a
    live progress indicator and is not the saved answer. Recovery may promote
    a region the model wrote *after* its last tool call (that is its final
    message, just missing from the brain's result), and may reach further back
    only when the result is an explicit back-reference ("see above") — there
    the model itself says the answer is earlier.
    """

    def test_short_answer_is_not_replaced_by_pre_tool_narration(self):
        narration = _block("Let me check the calendar.", 800)
        trace = [
            {"type": "text", "text": narration},
            {"type": "tool", "text": "Read calendar"},
        ]
        result = _compose_full_result(
            "Your meeting is at 3pm.", trace, task=_make_task(),
        )
        assert result == "Your meeting is at 3pm."

    def test_empty_answer_labels_narration_instead_of_promoting_it(self):
        narration = _block("Let me check the calendar.", 800)
        trace = [
            {"type": "text", "text": narration},
            {"type": "tool", "text": "Read calendar"},
        ]
        result = _compose_full_result("", trace, task=_make_task())
        assert result != narration
        assert result.startswith(_NO_FINAL_ANSWER_NOTICE)
        # The work isn't thrown away — it is labelled as progress, not answer.
        assert narration in result

    def test_trailing_region_after_last_tool_is_still_recovered(self):
        narration = _block("Checking.", 600)
        answer = _block("Here is the answer.", 600)
        trace = [
            {"type": "text", "text": narration},
            {"type": "tool", "text": "Read calendar"},
            {"type": "text", "text": answer},
        ]
        result = _compose_full_result("", trace, task=_make_task())
        assert result == answer

    def test_short_trailing_answer_is_adopted_not_labelled(self):
        """A brief final message the brain lost is still the answer — the
        size floors protect a non-empty result, and there is none here."""
        trace = [
            {"type": "text", "text": _block("Checking.", 600)},
            {"type": "tool", "text": "Read calendar"},
            {"type": "text", "text": "Your meeting is at 3pm."},
        ]
        result = _compose_full_result("", trace, task=_make_task())
        assert result == "Your meeting is at 3pm."

    def test_explicit_back_reference_still_reaches_past_a_tool(self):
        """ISSUE-025 stays fixed: "see above" points at earlier text."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        assert _compose_full_result("See above.", trace, task=_make_task()) == findings

    def test_cm_recovery_does_not_reach_back_past_a_tool(self):
        narration = _block("Let me look this up.", 450)
        trace = [
            {"type": "text", "text": narration},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("Saved.", trace, task=_make_task())
        assert result == "Saved."

    def test_empty_result_with_no_trace_yields_the_notice(self):
        assert _compose_full_result("", [], task=_make_task()) == _NO_FINAL_ANSWER_NOTICE

    def test_notice_carries_even_a_short_partial(self):
        trace = [
            {"type": "text", "text": "Let me check the calendar."},
            {"type": "tool", "text": "Read calendar"},
        ]
        result = _compose_full_result("", trace, task=_make_task())
        assert result.startswith(_NO_FINAL_ANSWER_NOTICE)
        assert "Let me check the calendar." in result

    def test_automated_task_empty_result_left_alone(self):
        """A briefing's body is parsed as JSON and an empty result flows to the
        existing quiet retry — a prose notice would be parsed as the body."""
        trace = [
            {"type": "text", "text": _block("Narration.", 800)},
            {"type": "tool", "text": "Read feed"},
        ]
        assert _compose_full_result(
            "", trace, task=_make_task(source_type="briefing"),
        ) == ""

    def test_real_answer_never_replaced_by_the_notice(self):
        assert _compose_full_result(
            "The answer.", [], task=_make_task(),
        ) == "The answer."

    def test_whitespace_only_answer_treated_as_empty(self):
        assert _compose_full_result(
            "   \n ", [], task=_make_task(),
        ) == _NO_FINAL_ANSWER_NOTICE


class TestComposeHelpers:
    """Direct tests for the helper predicates."""

    def test_is_terse_short(self):
        assert _is_terse("Done.")

    def test_is_terse_empty(self):
        assert _is_terse("")
        assert _is_terse("   ")

    def test_is_terse_pattern_see_above(self):
        assert _is_terse("See above.")
        assert _is_terse("see above")
        assert _is_terse("SEE ABOVE")

    def test_is_terse_pattern_done(self):
        assert _is_terse("Done.")
        assert _is_terse("Done")
        assert _is_terse("OK")
        assert _is_terse("✓")

    def test_is_terse_substantial_text_not_terse(self):
        long_text = "A" * (_TERSE_RESULT_MAX_CHARS + 1)
        assert not _is_terse(long_text)

    def test_is_automated_task_none(self):
        assert not _is_automated_task(None)

    def test_is_automated_task_scheduled(self):
        assert _is_automated_task(_make_task(source_type="scheduled"))

    def test_is_automated_task_briefing(self):
        assert _is_automated_task(_make_task(source_type="briefing"))

    def test_is_automated_task_talk_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="talk"))

    def test_is_automated_task_email_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="email"))

    def test_is_automated_task_subtask_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="subtask"))

    def test_is_automated_task_heartbeat_silent_flag(self):
        assert _is_automated_task(
            _make_task(source_type="cli", heartbeat_silent=True),
        )

    def test_is_automated_task_scheduled_job_id_flag(self):
        assert _is_automated_task(
            _make_task(source_type="cli", scheduled_job_id=42),
        )

    def test_last_substantial_region_empty_trace(self):
        assert _last_substantial_region([], {"tool"}, 100) is None

    def test_last_substantial_region_no_qualifying_region(self):
        trace = [
            {"type": "text", "text": "tiny"},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": "also tiny"},
        ]
        assert _last_substantial_region(trace, {"tool"}, 500) is None

    def test_last_substantial_region_returns_last_substantial(self):
        block1 = _block("Block1.", 600)
        block2 = _block("Block2.", 600)
        trace = [
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": block2},
        ]
        # With tool as delimiter, regions = [[block1], [block2]]
        # Reverse walk: block2 first → returned.
        assert _last_substantial_region(trace, {"tool"}, 500) == block2

    def test_last_substantial_region_walks_back_past_thin(self):
        block = _block("Block.", 600)
        trace = [
            {"type": "text", "text": block},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": "thin"},
        ]
        # Last region is "thin" (4 chars), walks back to the substantial one.
        assert _last_substantial_region(trace, {"tool"}, 500) == block

    def test_last_substantial_region_aggregates_within_region(self):
        trace = [
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two. "},
            {"type": "text", "text": "Part three. "},
            {"type": "tool", "text": "Read"},
        ]
        # Three text events form one region (no delimiter between them).
        # Joined with \n\n.
        result = _last_substantial_region(trace, {"tool"}, 20)
        assert result == "Part one.\n\nPart two.\n\nPart three."


# =============================================================================
# TestPerUserEmailInPrompt
# =============================================================================


class TestPerUserEmailInPrompt:
    """Verify per-user plus-addressed email appears in prompt header."""

    def _make_task(self, user_id="carol"):
        return db.Task(
            id=1, status="running", prompt="hello", user_id=user_id,
            source_type="talk", conversation_token="room1",
        )

    def test_per_user_email_shown_when_email_enabled(self):
        config = Config()
        config.email = AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="istota@example.com",
        )
        task = self._make_task(user_id="carol")
        result = build_prompt(task, [], config).system
        assert "istota+carol@example.com" in result

    def test_per_user_email_not_shown_when_email_disabled(self):
        config = Config()
        config.email = AppEmailConfig(enabled=False)
        task = self._make_task(user_id="carol")
        # Both halves: a plus-address appearing anywhere in the prompt is the
        # thing being ruled out, whichever section it came from.
        composed = build_prompt(task, [], config)
        assert "+carol@" not in composed.system + composed.user

    def test_per_user_email_not_shown_when_no_bot_email(self):
        config = Config()
        config.email = AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="",
        )
        task = self._make_task(user_id="carol")
        # Both halves: a plus-address appearing anywhere in the prompt is the
        # thing being ruled out, whichever section it came from.
        composed = build_prompt(task, [], config)
        assert "+carol@" not in composed.system + composed.user


# =============================================================================
# TestSmtpFromPlusAddress
# =============================================================================


class TestSmtpFrom:
    """Verify SMTP_FROM uses plain bot email (not plus-addressed)."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the email manifest is loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            email=AppEmailConfig(
                enabled=True,
                imap_host="imap.test", imap_port=993,
                imap_user="u", imap_password="p",
                smtp_host="smtp.test", smtp_port=587,
                bot_email="istota@example.com",
            ),
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="carol", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_smtp_from_uses_plain_bot_email(self, mock_run, tmp_path):
        """SMTP_FROM should be the plain bot email; plus-addressing is for inbound only."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "carol").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["SMTP_FROM"] == "istota@example.com"


class TestWorkspaceDirBwrap:
    """build_bwrap_cmd workspace_dir: RW bind + --chdir + blocklist validation."""

    def _cfg(self, tmp_path):
        # The DB lives in its own subdirectory, as it does everywhere real
        # (`{istota_home}/data/istota.db`). Putting it at tmp_path root would
        # make every sibling fixture dir a child of the now-protected DB
        # directory, which is a property of the fixture, not of the blocklist.
        db_path = tmp_path / "data" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db.init_db(db_path)
        return Config(
            db_path=db_path,
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(),
        )

    def _task(self, tmp_path):
        with _db.get_db((tmp_path / "data" / "test.db")) as conn:
            tid = _db.create_task(conn, prompt="x", user_id="alice", source_type="repl")
            return _db.get_task(conn, tid)

    def test_workspace_bind_and_chdir(self, tmp_path, monkeypatch):
        from istota import executor
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        cfg = self._cfg(tmp_path)
        task = self._task(tmp_path)
        ws = tmp_path / "project"
        ws.mkdir()
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        cmd = executor.build_bwrap_cmd(
            ["claude"], cfg, task, True, [], user_temp, workspace_dir=ws,
            profile=executor.SandboxProfile.CLAUDE,
        )
        joined = " ".join(cmd)
        # chdir targets the workspace, and the workspace is bound RW.
        assert "--chdir" in cmd
        chdir_idx = cmd.index("--chdir")
        assert cmd[chdir_idx + 1] == str(ws.resolve())
        assert str(ws.resolve()) in joined

    def test_workspace_blocklist_rejects_home_ssh(self, tmp_path, monkeypatch):
        from istota import executor
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        cfg = self._cfg(tmp_path)
        task = self._task(tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        ssh_dir = Path.home() / ".ssh"
        with pytest.raises(ValueError):
            executor.build_bwrap_cmd(
                ["claude"], cfg, task, True, [], user_temp, workspace_dir=ssh_dir,
                profile=executor.SandboxProfile.CLAUDE,
            )

    def test_validate_workspace_rejects_source_tree(self, tmp_path):
        from istota import executor
        cfg = self._cfg(tmp_path)
        # The istota package dir is inside the source tree → rejected.
        src_dir = Path(executor.__file__).resolve().parent
        with pytest.raises(ValueError):
            executor._validate_workspace_dir(cfg, src_dir)

    def test_validate_workspace_allows_arbitrary_dir(self, tmp_path):
        from istota import executor
        cfg = self._cfg(tmp_path)
        ok = tmp_path / "safe"
        ok.mkdir()
        assert executor._validate_workspace_dir(cfg, ok) == ok.resolve()


class TestReplInteractiveGate:
    def test_repl_in_interactive_source_types(self):
        from istota.executor import _INTERACTIVE_SOURCE_TYPES
        assert "repl" in _INTERACTIVE_SOURCE_TYPES
        assert "talk" in _INTERACTIVE_SOURCE_TYPES
        assert "email" in _INTERACTIVE_SOURCE_TYPES


class TestWorkspacePlaceholderDoesNotClobberSandboxBind:
    """Regression: the {workspace} display string must not clobber the
    execute_task `workspace_dir` parameter (the REPL --workspace bind path,
    blocklist-validated by build_bwrap_cmd).

    Commit f3ab4b6 ("Storage-agnostic prompt/skill vocabulary") reassigned the
    `workspace_dir` parameter to the user's on-mount workspace root just to fill
    the {workspace} placeholder. That value flowed into build_bwrap_cmd, where
    _validate_workspace_dir rejects anything under the Nextcloud mount root — so
    every sandboxed LLM task on the server failed with
    "workspace ... overlaps a protected path". The fix uses a separate local for
    the display string; the parameter stays None for a normal task.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        # An eager skill whose body references the {workspace} placeholder, so
        # the substitution block that clobbered the variable actually runs.
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("Your files live in {workspace}.")
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            nextcloud_mount_path=mount,
            security=SecurityConfig(sandbox_enabled=True, skill_proxy_enabled=False),
        )

    @patch("istota.executor.build_bwrap_cmd")
    @patch("istota.executor.subprocess.run")
    def test_normal_task_passes_none_workspace_dir_to_sandbox(
        self, mock_run, mock_bwrap, tmp_path
    ):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        # build_bwrap_cmd is a no-op wrapper here; we only inspect its kwargs.
        mock_bwrap.side_effect = lambda raw_cmd, *a, **k: raw_cmd

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="talk"
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task

            success, _result, _actions, _trace = execute_task(
                task, config, [], conn=conn
            )

        # The sandbox wrapper must have been invoked, and with workspace_dir=None
        # (the mount-subdir value belongs in the {workspace} display string, not
        # the REPL bind path).
        assert mock_bwrap.called
        assert mock_bwrap.call_args.kwargs["workspace_dir"] is None
        # And the {workspace} placeholder still resolved in the prompt. The
        # skill body it lives in is a standing instruction, so it is in the
        # system half — which reaches the CLI as a file rather than on stdin.
        composed = (
            tmp_path / "temp" / ".control" / "alice" / "task_1"
            / "system_prompt.txt"
        ).read_text(encoding="utf-8")
        prompt_text = mock_run.call_args.kwargs["input"]
        assert "{workspace}" not in composed
        assert "{workspace}" not in prompt_text
        assert str((config.nextcloud_mount_path / "Users" / "alice")) in composed


class TestImagePreparationWritesIntoTheControlDirectory:
    """The destination `execute_task` hands `prepare_image_attachments`.

    The prepared renditions used to land in `{temp_dir}/{user_id}/attachments/
    task_<id>/` — inside the sandbox's own working directory, where the model
    could rewrite the picture it was about to be asked about, and where the
    previous task's renditions were still readable. The function no longer
    derives that layout at all: it takes the directory to write into, and the
    caller is what names it.

    Asserted through the argument rather than through a written file, so the
    wiring is pinned on a deployment with no Pillow and on every case where an
    attachment is screened out before anything is written.
    `tests/test_executor_images.py` is where the real renditions are followed
    to disk.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            skills_dir=tmp_path / "_empty_skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(sandbox_enabled=False, skill_proxy_enabled=False),
        )

    def test_the_out_dir_is_inside_the_task_control_directory(self, tmp_path):
        from istota.executor import execute_task, get_task_control_dir, get_user_temp_dir
        from istota.image_attachments import ImagePreparation

        config = self._make_config(tmp_path)
        img = tmp_path / "inbox" / "shot.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"not really a png")

        with patch("istota.executor.prepare_image_attachments") as prep, \
                patch("istota.executor.subprocess.run") as mock_run:
            prep.return_value = ImagePreparation([str(img)], [], [])
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="what is this?", user_id="alice",
                    source_type="talk", attachments=[str(img)],
                )
                task = db.get_task(conn, task_id)
                execute_task(task, config, [], conn=conn, use_context=False)

        assert prep.called, "the image pass never ran"
        out_dir = prep.call_args.args[1]
        control = get_task_control_dir(config, "alice", task.id)
        assert out_dir == control / "attachments"
        # And not in the directory the sandbox binds read-write, which is the
        # whole point of the move.
        assert not out_dir.is_relative_to(
            get_user_temp_dir(config, "alice").resolve()
        )


class TestAnUnusableControlDirectoryFailsTheTask:
    """Fail-closed, and *how* it fails closed.

    A task whose control directory cannot be created has nowhere to put its
    standing instructions, so it must not run — but raising out of
    `execute_task` is not the way to say so. `process_one_task` has no handler
    of its own, so the exception reaches the worker's catch-all, which logs and
    moves on with the row still `running`: the task is then recovered only by
    the stuck-worker sweep, minutes later, with the reason nowhere but the
    daemon log. Returning the failure keeps the ordinary accounting and puts
    the path in front of whoever asked.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            skills_dir=tmp_path / "_empty_skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(sandbox_enabled=False, skill_proxy_enabled=False),
        )

    def test_a_control_root_that_is_a_file_fails_the_task_by_return(self, tmp_path):
        from istota.executor import CONTROL_DIR_NAME, execute_task

        config = self._make_config(tmp_path)
        config.temp_dir.mkdir(parents=True, exist_ok=True)
        # A real corrupt-state case rather than a patched one: `O_DIRECTORY`
        # is what refuses it, several layers below the assertion.
        (config.temp_dir / CONTROL_DIR_NAME).write_text("not a directory\n")

        with patch("istota.executor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="hi", user_id="alice", source_type="talk",
                )
                task = db.get_task(conn, task_id)
                success, result, actions, trace = execute_task(
                    task, config, [], conn=conn, use_context=False,
                )

        assert success is False
        assert (actions, trace) == (None, None)
        assert CONTROL_DIR_NAME in result, result
        # And the model was never reached: a task that cannot hold its own
        # standing instructions must not run without them.
        assert not mock_run.called


class TestTheControlDirectoryIsGuardedOnEveryShape:
    """`execute_task`'s two guard entries, read from the request it built.

    They are enforced under different conditions and neither subsumes the
    other, which is the one thing about this pair that is easy to get wrong:

    - `fs_read_roots` is `None` when confinement is off, and `None` means
      *unconfined* in `ToolEnv` — both root lists are then inert. A `read_only`
      entry alone protects nothing on the standalone install or the shipped
      Docker stack.
    - `fs_write_denied_roots` is checked ahead of that unconfined return, so it
      holds on every shape. That is why `execute_task` seeds it outside the
      `native_fs_confinement_active` branch.
    - Under confinement the control directory is inside no write root, so the
      `read_only` entry is what makes it readable while leaving it unwritable.

    `tests/test_sandbox.py::TestNativeFsRootsTaskControlDirectory` is the unit
    half; this is the wiring.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            skills_dir=tmp_path / "_empty_skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(sandbox_enabled=False, skill_proxy_enabled=False),
        )

    def _run(self, tmp_path, confined):
        from istota.executor import execute_task

        config = self._make_config(tmp_path)
        captured = {}

        class _Brain:
            model_namespace = "anthropic"
            supports_steering = False
            kind = "claude_code"

            def execute(self, req):
                captured["req"] = req
                return BrainResult(
                    success=True, result_text="answer", stop_reason="completed",
                )

            def resolve_model_name(self, name):
                return (name or "").strip()

            def resolve_alias(self, alias):
                return None

        with patch("istota.executor.make_brain", return_value=_Brain()), \
                patch(
                    "istota.executor.native_fs_confinement_active",
                    return_value=confined,
                ):
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="hi", user_id="alice", source_type="talk",
                )
                task = db.get_task(conn, task_id)
                execute_task(task, config, [], conn=conn, use_context=False)

        assert "req" in captured, "the brain was never called"
        return config, task, captured["req"]

    def test_the_unconditional_seed_is_the_control_directory(self, tmp_path):
        """The shapes with nothing else behind it. `build_bwrap_cmd` hands the
        command back unwrapped on macOS, on the standalone install and on the
        shipped Docker stack, and `native_fs_roots` is not called there at all
        — so this seed is the only guard the control directory has."""
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=False)

        control = get_task_control_dir(config, task.user_id, task.id)
        assert req.fs_read_roots is None, (
            "the fixture is not actually unconfined, so this asserts nothing "
            "about the shape it is named for"
        )
        assert req.fs_write_denied_roots == [control], req.fs_write_denied_roots

    def test_the_confined_shape_gets_both_entries_once(self, tmp_path):
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=True)

        control = get_task_control_dir(config, task.user_id, task.id)
        assert req.fs_write_denied_roots.count(control) == 1, (
            f"fs_write_denied_roots was {req.fs_write_denied_roots!r}; two "
            "producers seeding the same root is the shape of a drift"
        )
        assert control in (req.fs_read_roots or []), req.fs_read_roots
        assert not any(
            control == r or control.is_relative_to(r)
            for r in (req.fs_write_roots or [])
        ), req.fs_write_roots

    def test_the_guard_covers_the_framework_files_and_not_the_model_s(
        self, tmp_path,
    ):
        """The point of a per-directory guard, asserted in both directions.

        Read off the paths `execute_task` put on the *request* rather than off
        a `rglob` of the control directory: enumerating the directory and then
        asserting its contents are under the deny root that is the directory
        is true by construction, and would stay green on exactly the failure
        worth catching — a framework file written somewhere else.

        The result file is the discriminating half. It is written by the model
        from inside the sandbox and read back by the daemon, so it lives in the
        model's own working directory by definition; a guard that covered it
        would break every task, and a test that only checked the deny side
        would pass equally against a task whose whole temp tree was refused.
        """
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=True)

        control = get_task_control_dir(config, task.user_id, task.id)
        denied = req.fs_write_denied_roots

        composed = req.composed_system_prompt_path
        assert composed is not None, "nothing named the composed system prompt"
        assert any(composed.is_relative_to(root) for root in denied), (
            f"{composed} is under no deny root: {denied}"
        )
        # The user half, named by no request field, so read from the directory
        # the request's own path points into.
        assert (control / "prompt.txt").exists()

        assert req.result_file is not None
        assert not any(
            Path(req.result_file).is_relative_to(root) for root in denied
        ), f"the result file {req.result_file} was denied: {denied}"
