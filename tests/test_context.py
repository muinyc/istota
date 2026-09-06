"""Tests for conversation context selection module."""

import json
import os
import subprocess
from unittest.mock import patch

from istota.config import Config, ConversationConfig
from istota.context import format_context_for_prompt, select_relevant_context
from istota.db import ConversationMessage


def _make_config(**conv_overrides) -> Config:
    """Create a Config with optional conversation config overrides."""
    conv = ConversationConfig(**conv_overrides)
    return Config(conversation=conv)


def _msg(id: int, prompt: str, result: str, created_at: str = "2025-01-26 12:00", actions_taken: str | None = None, source_type: str = "talk", user_id: str | None = None) -> ConversationMessage:
    return ConversationMessage(id=id, prompt=prompt, result=result, created_at=created_at, actions_taken=actions_taken, source_type=source_type, user_id=user_id)


def _history(n: int) -> list[ConversationMessage]:
    """Create a history of n messages."""
    return [_msg(i, f"q{i}", f"a{i}") for i in range(1, n + 1)]


class TestSelectRelevantContext:
    def test_empty_history(self):
        config = _make_config()
        assert select_relevant_context("hello", [], config) == []

    def test_short_history_all_included(self):
        config = _make_config(skip_selection_threshold=3)
        history = _history(2)
        result = select_relevant_context("what's up", history, config)
        assert result == history

    def test_short_history_at_threshold(self):
        config = _make_config(skip_selection_threshold=3)
        history = _history(3)
        result = select_relevant_context("g", history, config)
        assert result == history

    def test_single_message(self):
        config = _make_config(skip_selection_threshold=3)
        history = [_msg(1, "hi", "hello")]
        result = select_relevant_context("bye", history, config)
        assert result == history

    def test_selection_disabled_includes_all(self):
        config = _make_config(use_selection=False)
        history = _history(10)
        result = select_relevant_context("test", history, config)
        assert result == history

    def test_within_always_include_recent_no_triage(self):
        """History fits entirely in always_include_recent — no triage needed."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=5)
        history = _history(4)
        result = select_relevant_context("test", history, config)
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_hybrid_triage_selects_from_older(self, mock_run):
        """Older messages triaged, recent guaranteed."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = [
            _msg(1, "about cats", "cats are great"),    # older[0] - triageable
            _msg(2, "about dogs", "dogs are great"),    # older[1] - triageable
            _msg(3, "about fish", "fish are great"),    # older[2] - triageable
            _msg(4, "weather", "sunny"),                # guaranteed recent
            _msg(5, "latest", "latest response"),       # guaranteed recent
        ]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"relevant_ids": [0]}',
            stderr="",
        )
        result = select_relevant_context("tell me about cats", history, config)
        # triaged: cats (selected) + guaranteed: weather, latest
        assert len(result) == 3
        assert result[0] == history[0]  # cats (triaged in)
        assert result[1] == history[3]  # weather (guaranteed)
        assert result[2] == history[4]  # latest (guaranteed)

    @patch("istota.brain.claude_code.subprocess.run")
    def test_hybrid_empty_triage_keeps_recent(self, mock_run):
        """No older messages selected, but recent still included."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"relevant_ids": []}',
            stderr="",
        )
        result = select_relevant_context("g", history, config)
        # Only guaranteed recent
        assert result == history[-2:]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_injected_completer_used_instead_of_cli(self, mock_run):
        """When a completer is supplied (native brain), the `claude` CLI is not invoked."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)  # older = [0,1,2], recent = last 2
        calls = []

        def completer(prompt):
            calls.append(prompt)
            return '{"relevant_ids": [0, 2]}'

        result = select_relevant_context("test", history, config, completer=completer)

        mock_run.assert_not_called()  # never shells out to `claude`
        assert len(calls) == 1
        # triaged 0, 2 + guaranteed last 2
        assert result == [history[0], history[2], history[3], history[4]]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_cli_triage_does_not_inherit_daemon_credentials(self, mock_run):
        """ISSUE-232 — the CLI triage path spawns `claude` with an explicit
        env, so the daemon's credentials never reach a subprocess driven by
        user-influenced conversation history."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"relevant_ids": []}', stderr="",
        )
        with patch.dict(
            os.environ,
            {"PATH": "/usr/bin", "ISTOTA_SECRET_KEY": "k" * 64, "NC_PASS": "pw"},
            clear=True,
        ):
            select_relevant_context("test", _history(5), config)

        env = mock_run.call_args.kwargs.get("env")
        assert env is not None
        assert "ISTOTA_SECRET_KEY" not in env
        assert "NC_PASS" not in env

    @patch("istota.brain.claude_code.subprocess.run")
    def test_completer_returning_none_fails_open(self, mock_run):
        """Native completer unavailable (returns None) → include all older, no CLI."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        result = select_relevant_context(
            "test", history, config, completer=lambda _p: None
        )
        mock_run.assert_not_called()
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_completer_raising_fails_open(self, mock_run):
        """A completer that raises is caught and triage fails open."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)

        def completer(_prompt):
            raise RuntimeError("transport boom")

        result = select_relevant_context("test", history, config, completer=completer)
        mock_run.assert_not_called()
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_completer_empty_array_keeps_recent_only(self, mock_run):
        """Completer judges nothing relevant → only guaranteed recent (not fail-open)."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        result = select_relevant_context(
            "test", history, config, completer=lambda _p: '{"relevant_ids": []}'
        )
        mock_run.assert_not_called()
        assert result == history[-2:]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_invalid_json_fails_open(self, mock_run):
        """On unparseable output, fail open: include ALL older + recent."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="not valid json",
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # Fail open: older messages included rather than silently dropped
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_timeout_fails_open(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        result = select_relevant_context("test", history, config)
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_cli_not_found_fails_open(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.side_effect = FileNotFoundError("claude not found")
        result = select_relevant_context("test", history, config)
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_nonzero_return_fails_open(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="",
            stderr="error occurred",
        )
        result = select_relevant_context("test", history, config)
        assert result == history

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_markdown_code_fence(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = [
            _msg(1, "about cats", "cats are great"),
            _msg(2, "about dogs", "dogs are great"),
            _msg(3, "about fish", "fish are great"),
            _msg(4, "recent1", "r1"),
            _msg(5, "recent2", "r2"),
        ]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='```json\n{"relevant_ids": [1]}\n```',
            stderr="",
        )
        result = select_relevant_context("dogs again", history, config)
        assert len(result) == 3
        assert result[0] == history[1]  # dogs (triaged in)
        assert result[1] == history[3]  # recent1 (guaranteed)
        assert result[2] == history[4]  # recent2 (guaranteed)

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_json_in_explanation_text(self, mock_run):
        """Model returns explanation text with embedded JSON."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='Looking at these messages...\n\n```json\n{"relevant_ids": [0, 2]}\n```\n\nThese are relevant.',
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # triaged: 0, 2 + guaranteed: last 2
        assert len(result) == 4
        assert result[0] == history[0]
        assert result[1] == history[2]
        assert result[2] == history[3]
        assert result[3] == history[4]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_only_the_fence_can_parse_this_triage_reply(self, mock_run):
        """The discriminating case the two above are missing.

        Both of them pass with the fence handling removed entirely, because
        ``_parse_relevant_ids``' own widest-``{...}`` fallback rescues them
        — so neither can fail for the reason it was written. Found by
        making ``llm_json.find_fenced_block`` return ``None`` as a control:
        528 tests across this file, ``test_scheduler.py`` and
        ``test_transport_email_outbound.py`` stayed green.

        Here the explanation carries a ``{`` of its own, so the fallback
        span reaches from that brace into the JSON and is invalid. A wrong
        answer is not silent — the triage fails open and every older
        message is included — but nothing was checking.
        """
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                'Message {2} is the closest match.\n\n'
                '```json\n{"relevant_ids": [0, 2]}\n```\n'
            ),
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # Failing open would return all five.
        assert len(result) == 4
        assert result[0] == history[0]
        assert result[1] == history[2]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_fence_whose_body_sits_on_the_opener_line(self, mock_run):
        """An empty fence body must not be taken as the answer.

        ``find_fenced_block`` returns ``''`` here, and reading that as a
        result skips the ``{...}`` fallback and fails the triage open. The
        expression this replaced reached the JSON through its own closer, so
        this shape used to work.
        """
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='```json {"relevant_ids": [0, 2]}\n```',
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        assert len(result) == 4
        assert result[0] == history[0]
        assert result[1] == history[2]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_out_of_range_ids_filtered(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        # older_history has 3 items (indices 0, 1, 2). Index 5 and -1 are out of range.
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"relevant_ids": [0, 5, -1]}',
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # Only index 0 valid from older, plus 2 guaranteed recent
        assert len(result) == 3
        assert result[0] == history[0]
        assert result[1] == history[3]
        assert result[2] == history[4]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_non_integer_ids_filtered(self, mock_run):
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(5)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"relevant_ids": ["zero", 0, null]}',
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # Only integer index 0 valid
        assert len(result) == 3
        assert result[0] == history[0]
        assert result[1] == history[3]
        assert result[2] == history[4]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_results_in_chronological_order(self, mock_run):
        """Triaged messages should be in chronological order before recent."""
        config = _make_config(skip_selection_threshold=2, always_include_recent=2)
        history = _history(8)
        # Select out of order — should be returned sorted
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"relevant_ids": [4, 1, 3]}',
            stderr="",
        )
        result = select_relevant_context("test", history, config)
        # older is history[0..5], recent is history[6..7]
        assert len(result) == 5
        assert result[0] == history[1]  # triaged, chronological
        assert result[1] == history[3]
        assert result[2] == history[4]
        assert result[3] == history[6]  # guaranteed recent
        assert result[4] == history[7]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_custom_threshold(self, mock_run):
        config = _make_config(skip_selection_threshold=5)
        history = _history(5)
        # At threshold=5 with exactly 5 messages, should return all without CLI call
        result = select_relevant_context("test", history, config)
        assert result == history
        mock_run.assert_not_called()

    @patch("istota.brain.claude_code.subprocess.run")
    def test_triage_not_called_when_disabled(self, mock_run):
        config = _make_config(use_selection=False)
        history = _history(10)
        result = select_relevant_context("test", history, config)
        assert result == history
        mock_run.assert_not_called()


class TestFormatContextForPrompt:
    def test_empty(self):
        assert format_context_for_prompt([]) == ""

    def test_single_message(self):
        msgs = [_msg(1, "hello", "hi there", "2025-01-26 12:00:00")]
        result = format_context_for_prompt(msgs)
        assert "[2025-01-26 12:00] User: hello" in result
        assert "[2025-01-26 12:00] Bot: hi there" in result

    def test_multiple_messages(self):
        msgs = [
            _msg(1, "first", "response1", "2025-01-26 10:00:00"),
            _msg(2, "second", "response2", "2025-01-26 11:00:00"),
        ]
        result = format_context_for_prompt(msgs)
        lines = result.split("\n")
        assert len(lines) == 4
        assert "User: first" in lines[0]
        assert "Bot: response1" in lines[1]
        assert "User: second" in lines[2]
        assert "Bot: response2" in lines[3]

    def test_truncates_long_result(self):
        long_result = "x" * 4000
        msgs = [_msg(1, "q", long_result)]
        result = format_context_for_prompt(msgs, truncation=3000)
        assert "...[truncated]" in result
        bot_line = result.split("\n")[1]
        assert "x" * 3000 in bot_line
        assert bot_line.endswith("...[truncated]")

    def test_preserves_short_result(self):
        short_result = "x" * 2999
        msgs = [_msg(1, "q", short_result)]
        result = format_context_for_prompt(msgs, truncation=3000)
        assert "...[truncated]" not in result
        assert short_result in result

    def test_no_truncation_when_zero(self):
        long_result = "x" * 10000
        msgs = [_msg(1, "q", long_result)]
        result = format_context_for_prompt(msgs, truncation=0)
        assert "...[truncated]" not in result
        assert long_result in result

    def test_actions_taken_appended(self):
        actions = '["📄 Reading CRON.md", "✏️ Editing CRON.md"]'
        msgs = [_msg(1, "edit cron", "Done", actions_taken=actions)]
        result = format_context_for_prompt(msgs)
        assert "[Actions:" in result
        assert "Reading CRON.md" in result
        assert "Editing CRON.md" in result

    def test_actions_taken_none_omitted(self):
        msgs = [_msg(1, "hello", "hi")]
        result = format_context_for_prompt(msgs)
        assert "[Actions:" not in result

    def test_actions_taken_capped_at_15(self):
        actions_list = [f"action {i}" for i in range(20)]
        actions = json.dumps(actions_list)
        msgs = [_msg(1, "q", "a", actions_taken=actions)]
        result = format_context_for_prompt(msgs)
        # Should contain max 15 actions and an indicator of more
        assert "[Actions:" in result
        assert "action 0" in result
        assert "action 14" in result
        assert "action 15" not in result

    def test_scheduled_source_type_labeled_as_scheduled(self):
        """Scheduled/cron messages should be labeled [Scheduled:] not [User:]."""
        msgs = [_msg(1, "Send water reminder", "Reminder sent.", "2025-01-26 09:00:00", source_type="scheduled")]
        result = format_context_for_prompt(msgs)
        assert "[2025-01-26 09:00] Scheduled: Send water reminder" in result
        assert "User:" not in result

    def test_cron_source_type_labeled_as_scheduled(self):
        """Cron source_type messages should be labeled [Scheduled:]."""
        msgs = [_msg(1, "Daily digest", "Done.", "2025-01-26 08:00:00", source_type="cron")]
        result = format_context_for_prompt(msgs)
        assert "Scheduled: Daily digest" in result
        assert "User:" not in result

    def test_briefing_source_type_labeled_as_scheduled(self):
        """Briefing messages should be labeled [Scheduled:]."""
        msgs = [_msg(1, "Morning briefing", "Here is your briefing.", "2025-01-26 07:00:00", source_type="briefing")]
        result = format_context_for_prompt(msgs)
        assert "Scheduled: Morning briefing" in result
        assert "User:" not in result

    def test_heartbeat_source_type_labeled_as_scheduled(self):
        """Heartbeat messages should be labeled [Scheduled:]."""
        msgs = [_msg(1, "heartbeat check", "ok", "2025-01-26 10:00:00", source_type="heartbeat")]
        result = format_context_for_prompt(msgs)
        assert "Scheduled: heartbeat check" in result
        assert "User:" not in result

    def test_talk_source_type_labeled_as_user(self):
        """Regular talk messages should still be labeled [User:]."""
        msgs = [_msg(1, "Hello", "Hi there.", "2025-01-26 12:00:00", source_type="talk")]
        result = format_context_for_prompt(msgs)
        assert "User: Hello" in result
        assert "Scheduled:" not in result

    def test_default_source_type_uses_user_label(self):
        """Default source_type (talk) should produce [User:] label."""
        msgs = [_msg(1, "hello", "hi", "2025-01-26 12:00:00")]
        result = format_context_for_prompt(msgs)
        assert "User: hello" in result
        assert "Scheduled:" not in result

    def test_mixed_source_types_labeled_correctly(self):
        """Mixed history with scheduled and user messages labels each correctly."""
        msgs = [
            _msg(1, "Send water reminder", "Reminder sent.", "2025-01-26 09:00:00", source_type="scheduled"),
            _msg(2, "They have been reminded", "Great!", "2025-01-26 09:05:00", source_type="talk"),
        ]
        result = format_context_for_prompt(msgs)
        assert "Scheduled: Send water reminder" in result
        assert "User: They have been reminded" in result

    def test_user_id_shown_as_speaker(self):
        """When user_id is set, it should be used as the speaker label."""
        msgs = [ConversationMessage(id=1, prompt="hello", result="hi", created_at="2025-01-26 12:00", user_id="alice")]
        result = format_context_for_prompt(msgs)
        assert "alice: hello" in result
        assert "User:" not in result

    def test_user_id_none_falls_back_to_user(self):
        """When user_id is None, label should be 'User'."""
        msgs = [ConversationMessage(id=1, prompt="hello", result="hi", created_at="2025-01-26 12:00", user_id=None)]
        result = format_context_for_prompt(msgs)
        assert "User: hello" in result

    def test_scheduled_overrides_user_id(self):
        """Scheduled source_type should show 'Scheduled' even if user_id is set."""
        msgs = [ConversationMessage(id=1, prompt="daily job", result="done", created_at="2025-01-26 12:00", source_type="scheduled", user_id="alice")]
        result = format_context_for_prompt(msgs)
        assert "Scheduled: daily job" in result
        assert "alice:" not in result

    def test_multi_user_attribution(self):
        """Multiple users in a group chat should each show their username."""
        msgs = [
            ConversationMessage(id=1, prompt="q from alice", result="a1", created_at="2025-01-26 10:00", user_id="alice"),
            ConversationMessage(id=2, prompt="q from bob", result="a2", created_at="2025-01-26 11:00", user_id="bob"),
        ]
        result = format_context_for_prompt(msgs)
        assert "alice: q from alice" in result
        assert "bob: q from bob" in result

    def test_user_tz_renders_created_at_in_local(self):
        """ISSUE-056: created_at (UTC from sqlite) should render in user_tz when provided."""
        from zoneinfo import ZoneInfo

        # 2026-04-26 05:36:00 UTC → 2026-04-25 22:36 PT
        msg = _msg(1, "q", "a", created_at="2026-04-26 05:36:00")

        utc_result = format_context_for_prompt([msg])
        assert "[2026-04-26 05:36]" in utc_result

        pt_result = format_context_for_prompt([msg], user_tz=ZoneInfo("America/Los_Angeles"))
        assert "[2026-04-25 22:36]" in pt_result
        assert "[2026-04-26" not in pt_result

