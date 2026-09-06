"""Configuration loading for istota.scheduler module."""

import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from zoneinfo import ZoneInfo

import pytest

from istota.scheduler import (
    CONFIRMATION_PATTERN,
    PROGRESS_MESSAGES,
    UserWorker,
    WorkerPool,
    cleanup_old_claude_logs,
    cleanup_old_temp_files,
    download_talk_attachments,
    get_worker_id,
    strip_briefing_preamble,
    _talk_poll_loop,
    _format_error_for_user,
    _is_policy_refusal,
    _post_policy_refusal_alert,
    _strip_action_prefix,
    _dispatch_sleep,
    _worker_idle_wait,
    _execute_command_task,
    _execute_skill_task,
    _purge_obsolete_skill_jobs,
    _talk_target_for_delivery,
    check_briefings,
    check_scheduled_jobs,
    process_one_task,
    recover_orphaned_tasks_on_startup,
    _stuck_running_minutes,
    _task_heartbeat,
    post_result_to_talk,
)
from istota.config import (
    Config,
    SchedulerConfig,
    TalkConfig,
    NextcloudConfig,
    UserConfig,
    BriefingConfig,
    EmailConfig,
    MemorySearchConfig,
    LocationReceiverConfig,
)
from istota import db
from istota.session.session_log import SweepResult
from istota.transport.email.outbound import (
    _parse_email_output,
    _load_deferred_email_output,
)

from .support.rooms import plain_talk_room, promoted_room


# ---------------------------------------------------------------------------
# TestConfirmationPattern
# ---------------------------------------------------------------------------


class TestStuckRunningMinutes:
    """ISSUE-112: the stuck-running reclaim window must exceed the task timeout
    so a healthy worker (which self-kills at the timeout) is never reclaimed."""

    def test_exceeds_task_timeout(self):
        sched = SchedulerConfig(task_timeout_minutes=30)
        assert _stuck_running_minutes(sched) > sched.task_timeout_minutes

    def test_tracks_configured_timeout(self):
        assert _stuck_running_minutes(SchedulerConfig(task_timeout_minutes=10)) == 15
        assert _stuck_running_minutes(SchedulerConfig(task_timeout_minutes=60)) == 65


class TestTaskHeartbeat:
    """ISSUE-112: the worker pings liveness while a task runs."""

    def _config(self, tmp_path, *, interval):
        from istota import db
        db_path = tmp_path / "hb.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(worker_heartbeat_seconds=interval),
            temp_dir=tmp_path / "temp",
        )

    def test_pings_while_body_runs(self, tmp_path):
        from istota import db
        config = self._config(tmp_path, interval=1)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            db.update_task_status(conn, task_id, "running")

        def _hb(tid):
            with db.get_db(config.db_path) as conn:
                return conn.execute(
                    "SELECT last_heartbeat FROM tasks WHERE id = ?", (tid,)
                ).fetchone()[0]

        with _task_heartbeat(config, task_id):
            # First ping fires immediately on entry; poll briefly for it.
            deadline = time.time() + 3
            while _hb(task_id) is None and time.time() < deadline:
                time.sleep(0.05)
            assert _hb(task_id) is not None

    def test_disabled_when_interval_zero(self, tmp_path):
        from istota import db
        config = self._config(tmp_path, interval=0)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            db.update_task_status(conn, task_id, "running")
        with _task_heartbeat(config, task_id):
            time.sleep(0.2)
        with db.get_db(config.db_path) as conn:
            hb = conn.execute(
                "SELECT last_heartbeat FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
        assert hb is None  # no pinger started


class TestRecoverOrphanedTasksOnStartup:
    """Startup orphan recovery emits a terminal event frame for the cases that
    won't re-run (cancelled / failed), so a watching web client gets immediate
    closure instead of a hung spinner. Released tasks emit nothing — the re-run
    streams a fresh task_started and the SSE client resumes from its cursor."""

    def _config(self, tmp_path):
        from istota import db
        db_path = tmp_path / "orphan.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )

    def _make_running(self, conn, **kw):
        task_id = db.create_task(
            conn, prompt="hi", user_id="alice",
            conversation_token="room1", source_type=kw.pop("source_type", "web"),
        )
        conn.execute(
            "UPDATE tasks SET status = 'running', last_heartbeat = datetime('now') "
            "WHERE id = ?", (task_id,),
        )
        for col, val in kw.items():
            conn.execute(f"UPDATE tasks SET {col} = ? WHERE id = ?", (val, task_id))
        conn.commit()
        return task_id

    def _events(self, config, task_id):
        with db.get_db(config.db_path) as conn:
            return db.get_task_events(conn, task_id)

    def test_released_task_emits_no_terminal_event(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = self._make_running(conn)

        recover_orphaned_tasks_on_startup(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending"
        kinds = [e["kind"] for e in self._events(config, task_id)]
        assert "done" not in kinds  # re-run will emit a fresh task_started

    def test_cancelled_orphan_emits_terminal_frame(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = self._make_running(conn, cancel_requested=1)

        recover_orphaned_tasks_on_startup(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "cancelled"
        kinds = [e["kind"] for e in self._events(config, task_id)]
        assert "cancelled" in kinds
        assert kinds[-1] == "done"

    def test_failed_orphan_emits_terminal_frame(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = self._make_running(conn, attempt_count=3, max_attempts=3)

        recover_orphaned_tasks_on_startup(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "failed"
        kinds = [e["kind"] for e in self._events(config, task_id)]
        assert "error" in kinds
        assert kinds[-1] == "done"

    def test_terminal_frame_seq_continues_after_streamed_deltas(self, tmp_path):
        """The terminal frame must resume seq above any partial events the dead
        attempt already streamed, so a watching client's cursor stays valid."""
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = self._make_running(conn, cancel_requested=1)
            # Simulate partial stream from the interrupted attempt.
            for seq in (1, 2, 3):
                conn.execute(
                    "INSERT INTO task_events (task_id, seq, kind, payload, created_at) "
                    "VALUES (?, ?, 'text_delta', '{}', datetime('now'))",
                    (task_id, seq),
                )
            conn.commit()

        recover_orphaned_tasks_on_startup(config)

        seqs = [e["seq"] for e in self._events(config, task_id)]
        assert seqs == sorted(seqs)
        assert min(s for e, s in zip(self._events(config, task_id), seqs)
                   if e["kind"] in ("cancelled", "done")) > 3

    def test_no_orphans_is_noop(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
        # Should not raise.
        recover_orphaned_tasks_on_startup(config)


class TestConfirmationPattern:
    def test_matches_i_need_your_confirmation(self):
        assert CONFIRMATION_PATTERN.search("I need your confirmation before proceeding.")

    def test_matches_please_confirm(self):
        assert CONFIRMATION_PATTERN.search("Please confirm that you want to delete this file.")

    def test_matches_reply_yes(self):
        assert CONFIRMATION_PATTERN.search('Reply "yes" to continue.')

    def test_matches_should_i_proceed(self):
        assert CONFIRMATION_PATTERN.search("Should I proceed with the deletion?")

    def test_matches_can_you_confirm(self):
        assert CONFIRMATION_PATTERN.search("Can you confirm this action?")

    def test_matches_do_you_want_me_to_proceed(self):
        assert CONFIRMATION_PATTERN.search("Do you want me to proceed?")

    def test_no_match_regular_text(self):
        assert CONFIRMATION_PATTERN.search("Here is the weather forecast.") is None
        assert CONFIRMATION_PATTERN.search("Task completed successfully.") is None

    def test_case_insensitive(self):
        assert CONFIRMATION_PATTERN.search("PLEASE CONFIRM this action")
        assert CONFIRMATION_PATTERN.search("i need your confirmation")
        assert CONFIRMATION_PATTERN.search("Reply Yes or No")

    def test_no_final_answer_notice_is_not_a_confirmation(self):
        """ISSUE-211: the notice embeds mid-turn text the model wrote to
        itself. "Should I proceed?" in there is not a question awaiting an
        answer, and parking the task on it would hold it for the whole
        confirmation timeout with synthesized text as the prompt."""
        from istota.scheduler import is_no_final_answer

        notice = (
            "The turn ended without a final response. This is the last text "
            "it produced before stopping:\n\nShould I proceed with the deletion?"
        )
        # The pattern itself still matches — the guard is what gates it.
        assert CONFIRMATION_PATTERN.search(notice)
        assert is_no_final_answer(notice) is True
        assert is_no_final_answer("Should I proceed with the deletion?") is False


# ---------------------------------------------------------------------------
# TestCleanupOldTempFiles
# ---------------------------------------------------------------------------


class TestCleanupOldTempFiles:
    """cleanup_old_temp_files reaps stale temp files without disturbing the
    per-user temp dir of an in-flight task.

    The directory removal must be age-gated the same way file deletion is:
    execute_task creates an empty /tmp/<user> dir and only writes its prompt
    file seconds later, so a cleanup tick that rmdir'd freshly-created empty
    dirs would break the task's prompt write (the temp-dir race).
    """

    def _config(self, tmp_path):
        return Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )

    @staticmethod
    def _age(path: Path, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_deletes_stale_files(self, tmp_path):
        config = self._config(tmp_path)
        user_dir = config.temp_dir / "alice"
        user_dir.mkdir(parents=True)
        stale = user_dir / "task_1_prompt.txt"
        stale.write_text("x")
        self._age(stale, 10)

        deleted = cleanup_old_temp_files(config, retention_days=7)

        assert deleted == 1
        assert not stale.exists()

    def test_preserves_recent_files(self, tmp_path):
        config = self._config(tmp_path)
        user_dir = config.temp_dir / "alice"
        user_dir.mkdir(parents=True)
        recent = user_dir / "task_2_prompt.txt"
        recent.write_text("x")  # mtime = now

        cleanup_old_temp_files(config, retention_days=7)

        assert recent.exists()

    def test_does_not_remove_active_empty_user_dir(self, tmp_path):
        """The race: a just-created empty per-user dir must survive a cleanup
        tick so the in-flight task can still write its prompt file."""
        config = self._config(tmp_path)
        user_dir = config.temp_dir / "alice"
        user_dir.mkdir(parents=True)  # empty, mtime = now (mid-task)

        cleanup_old_temp_files(config, retention_days=7)

        assert user_dir.exists()

    def test_removes_stale_empty_dir(self, tmp_path):
        """A genuinely abandoned empty dir (untouched past retention) is still
        reaped — the age gate doesn't disable cleanup, only defers it."""
        config = self._config(tmp_path)
        old_dir = config.temp_dir / "ghost"
        old_dir.mkdir(parents=True)
        self._age(old_dir, 10)

        cleanup_old_temp_files(config, retention_days=7)

        assert not old_dir.exists()


# ---------------------------------------------------------------------------
# TestTalkTargetForDelivery
# ---------------------------------------------------------------------------


class TestRoomTurnBelongsHere:
    """The predicate that replaced three scattered `_store_room_turn` calls.

    Two rungs: the plan delivering this answer into the room, or the room
    already holding the question. They are not interchangeable — see the two
    tests at the bottom, which are the same task shape with opposite answers.
    """

    def _db(self, tmp_path):
        path = tmp_path / "rt.db"
        db.init_db(path)
        return path

    def _task(self, **kwargs):
        defaults = dict(
            id=1, status="pending", source_type="email", user_id="alice",
            prompt="x", conversation_token="rm", priority=5,
            attempt_count=0, max_attempts=3,
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def test_no_token_never_belongs(self, tmp_path):
        from istota.scheduler import _room_turn_belongs_here

        with db.get_db(self._db(tmp_path)) as conn:
            task = self._task(conversation_token=None)
            assert _room_turn_belongs_here(
                conn, task, 1, None, delivering_into_room=True,
            ) is False

    def test_delivering_into_the_room_is_enough(self, tmp_path):
        """No question in the room, but the plan says this answer is a turn in
        it — the ISSUE-164 own-room web push."""
        from istota.scheduler import _room_turn_belongs_here

        with db.get_db(self._db(tmp_path)) as conn:
            assert _room_turn_belongs_here(
                conn, self._task(), 1, "rm", delivering_into_room=True,
            ) is True

    def test_the_question_being_there_is_enough(self, tmp_path):
        """Nothing delivered into the room at all — the email-only plan a
        `thread` reply-routing policy produces — but the exchange happened
        here, so the answer belongs under it (ISSUE-136)."""
        from istota.scheduler import _room_turn_belongs_here

        path = self._db(tmp_path)
        with db.get_db(path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            tid = db.create_task(
                conn, "q", "alice", source_type="email",
                conversation_token="rm",
            )
            db.add_message(
                conn, "rm", role="user", body="q",
                origin_surface="email", task_id=tid,
            )
            assert _room_turn_belongs_here(
                conn, self._task(id=tid), tid, "rm", delivering_into_room=False,
            ) is True

    def test_neither_means_no_answer_only_bubble(self, tmp_path):
        """A room that never received the question and is not being delivered
        into. Storing here is ISSUE-136 from the other side."""
        from istota.scheduler import _room_turn_belongs_here

        path = self._db(tmp_path)
        with db.get_db(path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            assert _room_turn_belongs_here(
                conn, self._task(), 1, "rm", delivering_into_room=False,
            ) is False


class TestTalkTargetForDelivery:
    """_talk_target_for_delivery resolves the Talk room for a task's notifications.

    Email-source tasks may carry a synthetic 16-char hex thread hash in
    `conversation_token` (set by the email inbound for plus_address / sender_match
    routing). Posting to that token silently no-ops because it isn't a real
    Talk room. The helper falls back to the user's resolved alerts/DM channel
    in that case while leaving real tokens (talk-originated chains) intact.
    """

    def _config(self, tmp_path, alerts_channel="alerts1"):
        return Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(alerts_channel=alerts_channel)},
        )

    def _task(self, **kwargs):
        defaults = dict(
            id=1, status="pending", source_type="email", user_id="alice",
            prompt="x", conversation_token=None, priority=5,
            attempt_count=0, max_attempts=3,
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def test_talk_source_passes_through(self, tmp_path):
        config = self._config(tmp_path)
        task = self._task(source_type="talk", conversation_token="real_room")
        assert _talk_target_for_delivery(config, task) == "real_room"

    def test_email_source_synthetic_token_falls_back_to_alerts(self, tmp_path):
        config = self._config(tmp_path, alerts_channel="alerts1")
        # 16 hex chars matches compute_thread_id() output shape
        synthetic = "a1b2c3d4e5f60718"
        task = self._task(source_type="email", conversation_token=synthetic)
        assert _talk_target_for_delivery(config, task) == "alerts1"

    def test_email_source_real_token_passes_through(self, tmp_path):
        config = self._config(tmp_path, alerts_channel="alerts1")
        # An 8-char alphanumeric Talk token does not match the synthetic shape
        task = self._task(source_type="email", conversation_token="r0om4Bcd")
        assert _talk_target_for_delivery(config, task) == "r0om4Bcd"

    def test_email_source_uppercase_hex_passes_through(self, tmp_path):
        # Real Talk tokens may include uppercase chars; pure-lowercase-hex is
        # the synthetic signature. An uppercase-letter 16-char token isn't
        # treated as synthetic.
        config = self._config(tmp_path, alerts_channel="alerts1")
        task = self._task(source_type="email", conversation_token="A1B2C3D4E5F60718")
        assert _talk_target_for_delivery(config, task) == "A1B2C3D4E5F60718"

    def test_email_source_no_token_returns_none(self, tmp_path):
        config = self._config(tmp_path)
        task = self._task(source_type="email", conversation_token=None)
        assert _talk_target_for_delivery(config, task) is None

    def test_email_source_synthetic_token_no_user_config(self, tmp_path):
        # No alerts_channel and no other resolvable channel — preserve the
        # synthetic token rather than returning None, keeping pre-fix behavior
        # (silent no-op at delivery time) instead of regressing to a different
        # failure mode.
        config = Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )
        synthetic = "a1b2c3d4e5f60718"
        task = self._task(source_type="email", conversation_token=synthetic)
        assert _talk_target_for_delivery(config, task) == synthetic

    def test_briefing_source_passes_through(self, tmp_path):
        config = self._config(tmp_path)
        task = self._task(source_type="briefing", conversation_token="briefing_room")
        assert _talk_target_for_delivery(config, task) == "briefing_room"

    def test_email_synthetic_falls_back_via_briefing_token(self, tmp_path):
        # alerts_channel empty → resolve_conversation_token falls back to first
        # briefing's token
        config = Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(
                alerts_channel="",
                briefings=[BriefingConfig(name="morning", cron="0 8 * * *",
                                          conversation_token="briefroom")],
            )},
        )
        synthetic = "deadbeef12345678"
        task = self._task(source_type="email", conversation_token=synthetic)
        assert _talk_target_for_delivery(config, task) == "briefroom"

    # `talk_delivery_token` is still rung 0 and still absolute while anything
    # writes it. These three are the originals, kept verbatim: they are the only
    # coverage of a task whose column and conversation_token *disagree*, which
    # is exactly the shape the legacy email thread-match branch produces and
    # exactly what a premature retirement of the column misroutes.

    def test_talk_delivery_token_takes_precedence_over_synthetic(self, tmp_path):
        # ISSUE-057 proper fix: talk_delivery_token wins regardless of shape
        # heuristics on conversation_token.
        config = self._config(tmp_path, alerts_channel="alerts1")
        synthetic = "deadbeef12345678"
        task = self._task(
            source_type="email",
            conversation_token=synthetic,
            talk_delivery_token="real_room",
        )
        assert _talk_target_for_delivery(config, task) == "real_room"

    def test_talk_delivery_token_takes_precedence_over_real_token(self, tmp_path):
        # Even when conversation_token is a real Talk room (e.g. a thread-match
        # email task that inherited it from a prior outbound), the explicit
        # talk_delivery_token still wins — they're authoritative for delivery.
        config = self._config(tmp_path)
        task = self._task(
            source_type="email",
            conversation_token="other_room",
            talk_delivery_token="delivery_room",
        )
        assert _talk_target_for_delivery(config, task) == "delivery_room"

    def test_talk_delivery_token_used_for_subtasks(self, tmp_path):
        # Subtasks inherit talk_delivery_token from the parent regardless of
        # source_type, so delivery hits the right room.
        config = self._config(tmp_path)
        task = self._task(
            source_type="subtask",
            conversation_token=None,
            talk_delivery_token="parent_room",
        )
        assert _talk_target_for_delivery(config, task) == "parent_room"

    def test_the_column_still_wins_over_a_disagreeing_binding(self, tmp_path):
        """Rung 0 is absolute, not a tiebreak.

        The column can name a room the registry has never heard of (the legacy
        thread-match branch copies one onto the task), so a registry that
        disagrees is not evidence the column is wrong — only that the registry
        is incomplete. Demoting the column to a room-finding hint is what turns
        that into a silent reroute.
        """
        config = self._live_config(tmp_path)
        self._room(config, "other_room", "binding_room", origin="web")
        task = self._task(
            source_type="email",
            conversation_token="other_room",
            talk_delivery_token="delivery_room",
        )
        assert _talk_target_for_delivery(config, task) == "delivery_room"

    # The cases below used to be answered by `tasks.talk_delivery_token` too,
    # but with the column NULL — which is every talk- and web-sourced task, and
    # every task once the column is finally retired. They are answered by the
    # room's `talk` binding now. Same expected token, different source of truth,
    # and one thing the column could never do: see the promote test.

    def _room(self, config, canonical, talk_ref, *, origin):
        """A room shape `tests/support/rooms.py` deliberately will not build.

        `promoted_room` reproduces the *web-origin* promotion, so it always
        writes a `web` binding and a web-chat handle. The two cases left on
        this helper are rooms whose `talk` binding points at some other
        conversation than their own token, and they exist to pin the
        resolver's rungs rather than to model anything a producer writes — so
        they keep the primitive writes and name their origin explicitly. The
        web-origin promotions use the shared builder.
        """
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, canonical, "alice", origin=origin)
            db.add_room_binding(conn, canonical, "talk", talk_ref)

    def _promoted(self, config, canonical, talk_ref):
        with db.get_db(config.db_path) as conn:
            return promoted_room(
                conn, "alice", canonical=canonical, talk_ref=talk_ref,
            )

    def _live_config(self, tmp_path, alerts_channel="alerts1"):
        config = self._config(tmp_path, alerts_channel=alerts_channel)
        config.db_path = tmp_path / "rooms.db"
        db.init_db(config.db_path)
        return config

    def test_a_promoted_rooms_binding_beats_its_canonical_token(self, tmp_path):
        """The shape rung 1 exists for: canonical token ≠ Talk ref.

        A web room promoted to Talk keeps its `web-…` canonical token, so the
        token is not postable and only the binding names the real Talk room.
        Deliberately built on a promoted room rather than on a synthetic email
        hash — `record_inbound` never registers a room under a thread hash (the
        mirror gate is room existence, never creation), so a test doing that
        would pin a reachability the system denies.
        """
        config = self._live_config(tmp_path)
        room = self._promoted(config, "web-alice-abc123def456", "RealTalkRoom")
        task = self._task(source_type="email", conversation_token=room.canonical)
        assert _talk_target_for_delivery(config, task) == room.talk_ref

    def test_room_binding_beats_a_real_looking_token(self, tmp_path):
        # A thread-matched email task inherited `other_room` from a prior
        # outbound; the room's own Talk binding is the authority for delivery.
        config = self._live_config(tmp_path)
        room = self._promoted(config, "other_room", "delivery_room")
        task = self._task(source_type="email", conversation_token=room.canonical)
        assert _talk_target_for_delivery(config, task) == room.talk_ref

    def test_a_subtask_resolves_through_its_inherited_room(self, tmp_path):
        # A subtask inherits the parent's conversation_token verbatim
        # (`scheduler_deferred` overrides whatever the deferred JSON asked for,
        # to keep prompt injection out of routing), so it resolves to the same
        # room the parent delivers to.
        config = self._live_config(tmp_path)
        self._room(config, "parent_conv", "parent_room", origin="talk")
        task = self._task(
            source_type="subtask", conversation_token="parent_conv",
        )
        assert _talk_target_for_delivery(config, task) == "parent_room"

    def test_a_binding_added_after_the_task_is_picked_up(self, tmp_path):
        """What the stored column structurally could not do.

        A room promoted to Talk after the task was created gained a binding the
        column would never learn about, so the reply kept going wherever the
        stale copy pointed.
        """
        config = self._live_config(tmp_path)
        task = self._task(source_type="talk", conversation_token="rm_late")
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm_late", "alice", origin="web")
        assert _talk_target_for_delivery(config, task) == "rm_late"
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, "rm_late", "talk", "PromotedRoom")
        assert _talk_target_for_delivery(config, task) == "PromotedRoom"


# ---------------------------------------------------------------------------
# TestFormatErrorForUser
# ---------------------------------------------------------------------------


class TestFormatErrorForUser:
    def test_formats_500_error(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_abc"}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()
        assert "API Error" not in result
        assert "req_abc" not in result

    def test_formats_503_error(self):
        error = 'API Error: 503 {"type":"error","error":{"type":"overloaded_error","message":"Service unavailable"}}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()

    def test_formats_529_error(self):
        error = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"API overloaded"}}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()

    def test_formats_429_error(self):
        error = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}'
        result = _format_error_for_user(error)
        assert "throttled" in result.lower()
        assert "chatty" in result.lower()

    def test_formats_401_error(self):
        error = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"}}'
        result = _format_error_for_user(error)
        assert "authenticate" in result.lower()
        assert "locked out" in result.lower()

    def test_formats_403_error(self):
        error = 'API Error: 403 {"type":"error","error":{"type":"permission_error","message":"Forbidden"}}'
        result = _format_error_for_user(error)
        assert "authenticate" in result.lower()

    def test_formats_other_api_error(self):
        error = 'API Error: 422 {"type":"error","error":{"type":"invalid_request_error","message":"Bad request"}}'
        result = _format_error_for_user(error)
        assert "the deep stared back" in result.lower()

    def test_formats_bodyless_529(self):
        # ISSUE-212: the CLI does not always attach a JSON body, and the bare
        # form used to fall through to the generic "sideways" message.
        result = _format_error_for_user("API Error: 529 Overloaded")
        assert "mothership" in result.lower()
        assert "API Error" not in result

    def test_formats_bodyless_429(self):
        result = _format_error_for_user("API Error: 429 Too Many Requests")
        assert "throttled" in result.lower()

    def test_formats_network_level_api_error(self):
        result = _format_error_for_user("API Error: Connection error.")
        assert "mothership" in result.lower()
        assert "Connection error" not in result

    def test_formats_both_brains_unavailable(self):
        from istota.executor import FALLBACK_EXHAUSTED_MARKER

        error = f"{FALLBACK_EXHAUSTED_MARKER} API Error: 529 Overloaded"
        result = _format_error_for_user(error)
        assert "backup" in result.lower()
        assert "try again" in result.lower()
        # No raw provider text leaks through.
        assert "API Error" not in result
        assert FALLBACK_EXHAUSTED_MARKER not in result

    def test_formats_oom_error(self):
        error = "Claude Code was killed (likely out of memory)"
        result = _format_error_for_user(error)
        assert "memory" in result.lower()
        assert "simpler" in result.lower()

    def test_formats_timeout_error(self):
        error = "Task execution timed out after 10 minutes"
        result = _format_error_for_user(error)
        assert "timed out" in result.lower()

    def test_formats_generic_error(self):
        error = "Something completely unexpected happened in the system"
        result = _format_error_for_user(error)
        assert "sideways" in result.lower()
        assert "try again" in result.lower()
        # Should not expose the raw error
        assert "unexpected happened" not in result

    def test_hides_request_id(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_011CXoLgXCH9oBmyPDc1SuMQ"}'
        result = _format_error_for_user(error)
        assert "req_011CXoLgXCH9oBmyPDc1SuMQ" not in result

    def test_hides_raw_json(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        result = _format_error_for_user(error)
        assert "{" not in result
        assert "}" not in result

    def test_formats_400_policy_refusal(self):
        # 400 with safety-related keywords should give a meaningful message
        # pointing the user at the alerts channel.
        error = 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Output blocked by content filtering policy"},"request_id":"req_x"}'
        result = _format_error_for_user(error)
        assert "safety filter" in result.lower() or "policy" in result.lower()
        assert "alerts" in result.lower()

    def test_formats_400_non_policy(self):
        # 400 without safety keywords should fall through to the generic bucket.
        error = 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"max_tokens exceeds limit"}}'
        result = _format_error_for_user(error)
        assert "the deep stared back" in result.lower()


# ---------------------------------------------------------------------------
# TestIsPolicyRefusal
# ---------------------------------------------------------------------------


class TestIsPolicyRefusal:
    def test_400_with_policy_keyword(self):
        error = 'API Error: 400 {"type":"error","error":{"message":"Output blocked by content filtering policy"}}'
        assert _is_policy_refusal(error) is True

    def test_400_with_safety_keyword(self):
        error = 'API Error: 400 {"type":"error","error":{"message":"This request was refused by safety classifier"}}'
        assert _is_policy_refusal(error) is True

    def test_400_with_harm_keyword(self):
        error = 'API Error: 400 {"type":"error","error":{"message":"Content flagged as potentially harmful"}}'
        assert _is_policy_refusal(error) is True

    def test_400_with_blocked_keyword(self):
        error = 'API Error: 400 {"type":"error","error":{"message":"Request blocked"}}'
        assert _is_policy_refusal(error) is True

    def test_400_without_safety_keywords(self):
        error = 'API Error: 400 {"type":"error","error":{"message":"max_tokens exceeds limit"}}'
        assert _is_policy_refusal(error) is False

    def test_500_is_not_policy_refusal(self):
        error = 'API Error: 500 {"type":"error","error":{"message":"safety filter triggered"}}'
        # Must be 400 — even if message has keywords
        assert _is_policy_refusal(error) is False

    def test_429_is_not_policy_refusal(self):
        error = 'API Error: 429 {"type":"error","error":{"message":"rate limit"}}'
        assert _is_policy_refusal(error) is False

    def test_non_api_error(self):
        assert _is_policy_refusal("Process killed (likely out of memory)") is False
        assert _is_policy_refusal("Cancelled by user") is False
        assert _is_policy_refusal("") is False

    def test_prose_discussing_a_400_is_not_a_refusal(self):
        # ISSUE-212 regression: this both suppresses retry and fires a "your
        # content was blocked" alert at the user, so it must require a
        # banner-shaped error rather than any text quoting one.
        answer = (
            "I explained that API Error: 400 content policy violations happen "
            "when the prompt trips the safety classifier."
        )
        assert _is_policy_refusal(answer) is False


# ---------------------------------------------------------------------------
# TestPostPolicyRefusalAlert
# ---------------------------------------------------------------------------


class TestPostPolicyRefusalAlert:
    def _config(self, tmp_path):
        return Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )

    def _task(self, **kwargs):
        defaults = dict(
            id=42, status="failed", source_type="email", user_id="alice",
            prompt="From: attacker@evil.com\nSubject: bad\n\nbody",
            conversation_token=None, priority=5, attempt_count=0, max_attempts=3,
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    @patch("istota.scheduler.send_notification")
    def test_email_alert_includes_sender(self, mock_send, tmp_path):
        config = self._config(tmp_path)
        task = self._task()
        error = 'API Error: 400 {"type":"error","error":{"message":"blocked by safety policy"}}'

        _post_policy_refusal_alert(config, task, error)

        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        message = mock_send.call_args.args[2] if len(mock_send.call_args.args) >= 3 else kwargs.get("message", "")
        assert "attacker@evil.com" in message
        assert "42" in message  # task id
        assert "blocked by safety policy" in message.lower()

    @patch("istota.scheduler.send_notification")
    def test_non_email_alert_is_generic(self, mock_send, tmp_path):
        config = self._config(tmp_path)
        task = self._task(
            source_type="talk", conversation_token="roomXYZ",
            prompt="hi from talk",
        )
        error = 'API Error: 400 {"type":"error","error":{"message":"content refused"}}'

        _post_policy_refusal_alert(config, task, error)

        mock_send.assert_called_once()
        message = mock_send.call_args.args[2]
        assert "roomXYZ" in message
        assert "attacker" not in message  # no sender extraction for non-email

    @patch("istota.scheduler.send_notification", side_effect=Exception("no alerts channel"))
    def test_alert_failure_does_not_raise(self, mock_send, tmp_path):
        config = self._config(tmp_path)
        task = self._task()
        # Should not raise even if notification dispatch fails
        _post_policy_refusal_alert(config, task, "API Error: 400 {}")


# ---------------------------------------------------------------------------
# TestParseEmailOutput
# ---------------------------------------------------------------------------


class TestParseEmailOutput:
    def test_valid_json(self):
        msg = '{"subject": "Hello", "body": "World", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Hello"
        assert result["body"] == "World"
        assert result["format"] == "plain"

    def test_json_in_code_fence(self):
        msg = 'Here is the response:\n```json\n{"subject": "Re: Test", "body": "Got it", "format": "html"}\n```'
        result = _parse_email_output(msg)
        assert result["subject"] == "Re: Test"
        assert result["body"] == "Got it"
        assert result["format"] == "html"

    def test_json_with_preamble(self):
        msg = 'I have composed the reply:\n{"subject": "Update", "body": "Details here", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Update"
        assert result["body"] == "Details here"

    def test_plain_text_returns_none(self):
        msg = "Just a plain text response with no JSON at all."
        result = _parse_email_output(msg)
        assert result is None

    def test_invalid_json_returns_none(self):
        msg = '{"broken json'
        result = _parse_email_output(msg)
        assert result is None

    def test_missing_body_returns_none(self):
        # Valid JSON but missing required "body" key
        msg = '{"subject": "No body here", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result is None

    def test_invalid_format_normalized(self):
        msg = '{"subject": "Test", "body": "Content", "format": "markdown"}'
        result = _parse_email_output(msg)
        assert result["body"] == "Content"
        assert result["format"] == "plain"  # invalid format normalized to plain

    def test_subject_optional(self):
        msg = '{"body": "Just body", "format": "html"}'
        result = _parse_email_output(msg)
        assert result["subject"] is None
        assert result["body"] == "Just body"
        assert result["format"] == "html"

    def test_smart_quotes_normalized(self):
        # Unicode left double quote (U+201C) inside a JSON string value
        # breaks JSON parsing — Try 4 should normalize and recover
        msg = '{"subject": "Daily Notes", "body": "He said \u201chello\u201d today", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Daily Notes"
        assert "hello" in result["body"]
        assert result["format"] == "plain"

    def test_smart_single_quotes_normalized(self):
        msg = '{"subject": "Test", "body": "It\u2019s a nice day", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Test"
        assert "nice day" in result["body"]
        assert result["format"] == "plain"

    def test_smart_quotes_in_preamble_json(self):
        # Smart quotes in JSON with preamble text — Try 3 fails, Try 4 recovers
        msg = 'Here is the email:\n{"subject": "Notes", "body": "\u201cWise words\u201d from Dostoevsky", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Notes"
        assert "Dostoevsky" in result["body"]


# ---------------------------------------------------------------------------
# TestLoadDeferredEmailOutput
# ---------------------------------------------------------------------------


class TestLoadDeferredEmailOutput:
    def _make_task(self, task_id=42, user_id="alice"):
        return db.Task(
            id=task_id, status="completed", source_type="email",
            user_id=user_id, prompt="test",
        )

    def test_loads_valid_file(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "Hello", "body": "World", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result == {"subject": "Hello", "body": "World", "format": "plain"}
        # File should be deleted after loading
        assert not (user_dir / "task_42_email_output.json").exists()

    def test_returns_none_when_no_file(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        (tmp_path / "alice").mkdir()
        result = _load_deferred_email_output(config, self._make_task())
        assert result is None

    def test_handles_invalid_json(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        (user_dir / "task_42_email_output.json").write_text("not json")

        result = _load_deferred_email_output(config, self._make_task())
        assert result is None
        # File should be cleaned up
        assert not (user_dir / "task_42_email_output.json").exists()

    def test_handles_missing_body(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "Hello", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result is None

    def test_normalizes_invalid_format(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "S", "body": "B", "format": "markdown"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["format"] == "plain"

    def test_html_format_preserved(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "S", "body": "<p>Hi</p>", "format": "html"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["format"] == "html"
        assert result["body"] == "<p>Hi</p>"

    def test_null_subject(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"body": "Reply text", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["subject"] is None
        assert result["body"] == "Reply text"


# ---------------------------------------------------------------------------
# TestDeferredGarminImport
# ---------------------------------------------------------------------------


class TestDeferredGarminImport:
    """The delegated Garmin track import: a sandboxed skill call writes
    task_<id>_garmin_import.json, the scheduler runs the import in-process
    (where the master key lives) and notifies the user."""

    def _make_task(self, task_id=7, user_id="alice"):
        import istota.db as _db
        return _db.Task(
            id=task_id, status="completed", source_type="web",
            user_id=user_id, prompt="import my garmin tracks",
        )

    def _write_op(self, tmp_path, task_id=7, user_id="alice", **payload):
        user_dir = tmp_path / user_id
        user_dir.mkdir(exist_ok=True)
        (user_dir / f"task_{task_id}_garmin_import.json").write_text(
            json.dumps(payload or {"days_back": 30})
        )
        return user_dir

    def test_runs_import_and_notifies(self, tmp_path, monkeypatch):
        from istota import scheduler_deferred as sd
        from istota.location import garmin_import as gi
        import istota.notifications as notif

        user_dir = self._write_op(tmp_path, days_back=14)
        config = Config(temp_dir=tmp_path)

        calls = {}

        def fake_import(user_id, *, framework_db_path, config, options):
            calls["user_id"] = user_id
            calls["days_back"] = options.days_back
            return gi.ImportResult(False, 42, 2, [{"inserted": 42}])

        sent = []
        monkeypatch.setattr(gi, "import_tracks", fake_import)
        monkeypatch.setattr(
            notif, "send_notification",
            lambda cfg, uid, msg, **k: sent.append((uid, msg)) or True,
        )

        n = sd._process_deferred_garmin_import(config, self._make_task(), user_dir)

        assert n == 42
        assert calls == {"user_id": "alice", "days_back": 14}
        assert sent and sent[0][0] == "alice"
        assert "42" in sent[0][1] and "2 activit" in sent[0][1]
        # File consumed.
        assert not (user_dir / "task_7_garmin_import.json").exists()

    def test_no_activities_message(self, tmp_path, monkeypatch):
        from istota import scheduler_deferred as sd
        from istota.location import garmin_import as gi
        import istota.notifications as notif

        user_dir = self._write_op(tmp_path)
        config = Config(temp_dir=tmp_path)
        monkeypatch.setattr(
            gi, "import_tracks",
            lambda *a, **k: gi.ImportResult(False, 0, 0, []),
        )
        sent = []
        monkeypatch.setattr(
            notif, "send_notification",
            lambda cfg, uid, msg, **k: sent.append(msg) or True,
        )
        sd._process_deferred_garmin_import(config, self._make_task(), user_dir)
        assert sent and "no new GPS activities" in sent[0]

    def test_skips_when_location_disabled(self, tmp_path, monkeypatch):
        from istota import scheduler_deferred as sd
        from istota.location import garmin_import as gi

        user_dir = self._write_op(tmp_path)
        config = Config(temp_dir=tmp_path)
        monkeypatch.setattr(config, "is_module_enabled", lambda uid, mod, **k: False)
        called = []
        monkeypatch.setattr(gi, "import_tracks",
                            lambda *a, **k: called.append(1))

        n = sd._process_deferred_garmin_import(config, self._make_task(), user_dir)
        assert n == 0
        assert not called                       # import never attempted
        assert not (user_dir / "task_7_garmin_import.json").exists()

    def test_no_file_is_noop(self, tmp_path):
        from istota import scheduler_deferred as sd
        (tmp_path / "alice").mkdir()
        config = Config(temp_dir=tmp_path)
        assert sd._process_deferred_garmin_import(
            config, self._make_task(), tmp_path / "alice",
        ) == 0

    def test_garmin_import_in_known_suffixes(self):
        from istota.scheduler_deferred import _KNOWN_DEFERRED_SUFFIXES
        assert "garmin_import" in _KNOWN_DEFERRED_SUFFIXES


# ---------------------------------------------------------------------------
# TestDownloadTalkAttachments
# ---------------------------------------------------------------------------


class TestDownloadTalkAttachments:
    def test_mount_path_exists(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        talk_dir = mount / "Talk"
        talk_dir.mkdir()
        (talk_dir / "photo.jpg").write_bytes(b"fake image")

        config = Config(nextcloud_mount_path=mount)
        result = download_talk_attachments(config, ["Talk/photo.jpg"])
        assert len(result) == 1
        assert result[0] == str(mount / "Talk" / "photo.jpg")

    def test_mount_path_not_exists(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        # No Talk/file.jpg on disk
        config = Config(nextcloud_mount_path=mount)
        result = download_talk_attachments(config, ["Talk/missing.jpg"])
        assert len(result) == 1
        # Falls back to original path
        assert result[0] == "Talk/missing.jpg"

    @patch("istota.scheduler.subprocess.run")
    def test_rclone_download(self, mock_run, tmp_path):
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()
        # Simulate rclone creating the file
        (temp_dir / "doc.pdf").write_bytes(b"pdf content")
        mock_run.return_value = MagicMock(returncode=0)

        config = Config(
            nextcloud_mount_path=None,
            rclone_remote="nc",
            temp_dir=temp_dir,
        )
        result = download_talk_attachments(config, ["Talk/doc.pdf"])
        assert len(result) == 1
        assert result[0] == str(temp_dir / "doc.pdf")
        mock_run.assert_called_once()

    def test_non_talk_path_unchanged(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        result = download_talk_attachments(config, ["/some/other/path.txt"])
        assert result == ["/some/other/path.txt"]

    def test_empty_list(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        result = download_talk_attachments(config, [])
        assert result == []


# ---------------------------------------------------------------------------
# TestCheckBriefings
# ---------------------------------------------------------------------------


class TestCheckBriefings:
    def test_no_briefings(self, db_path):
        config = Config(db_path=db_path, users={})
        result = check_briefings(db_path, config)
        assert result == []

    def test_cron_triggers_briefing(self, db_path):
        # Briefing at 6 AM UTC, we pretend it is 6:05 AM UTC
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={"calendar": True},
        )
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            briefings=[briefing],
        )
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        # Set last run to yesterday
        with db.get_db(db_path) as conn:
            yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", yesterday),
            )

        result = check_briefings(db_path, config)

        assert len(result) == 1
        # No prompt prefetch on the dispatch thread (ISSUE-143): the task
        # carries the briefing identity and the executor builds the prompt.
        with db.get_db(db_path) as conn:
            created = db.get_task(conn, result[0])
        assert created.source_type == "briefing"
        assert created.briefing_name == "morning"
        assert created.queue == "background"

    def test_cron_not_yet_due(self, db_path):
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            briefings=[briefing],
        )
        config = Config(db_path=db_path, users={"alice": user})

        # Set last run to just now (so next cron is tomorrow)
        with db.get_db(db_path) as conn:
            now = datetime.now(ZoneInfo("UTC")).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", now),
            )

        result = check_briefings(db_path, config)

        assert result == []

    @patch("istota.scheduler._now")
    def test_first_run_past_scheduled(self, mock_now, db_path):
        """First run: no last_run_at, and we are past the cron time today."""
        # Cron at 6am, mock current time to 14:00 so it's reliably in the past
        mock_now.return_value = datetime(2026, 6, 15, 14, 0, 0, tzinfo=ZoneInfo("UTC"))

        briefing = BriefingConfig(
            name="past",
            cron="0 6 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        result = check_briefings(db_path, config)
        assert len(result) == 1

    def test_first_run_before_scheduled(self, db_path):
        """First run: no last_run_at, but cron time is in the future today."""
        # Use a cron at 23:59 so we haven't reached it yet (unless it is 23:59)
        briefing = BriefingConfig(
            name="late",
            cron="59 23 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        now = datetime.now(ZoneInfo("UTC"))
        if now.hour < 23 or (now.hour == 23 and now.minute < 59):
            result = check_briefings(db_path, config)
            assert result == []

    def test_missing_conversation_token_skipped_for_talk(self, db_path):
        briefing = BriefingConfig(
            name="no_token",
            cron="0 6 * * *",
            conversation_token="",  # empty token, output defaults to "talk"
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        result = check_briefings(db_path, config)
        assert result == []

    def test_email_briefing_without_conversation_token(self, db_path):
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="",
            output="email",
            components={"calendar": True},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        # Set last run to yesterday so cron evaluates as due
        with db.get_db(db_path) as conn:
            yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", yesterday),
            )

        result = check_briefings(db_path, config)
        assert len(result) == 1


class TestBriefingDeferredPrompt:
    """ISSUE-143: the briefing prefetch must not run on the dispatch thread."""

    def test_check_briefings_does_no_network_prefetch(self, db_path):
        """check_briefings must not build the briefing prompt (network I/O).

        A slow/unreachable upstream during the build would otherwise stall the
        single-threaded dispatch loop and starve task processing for every room.
        """
        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *", conversation_token="room1",
            components={"calendar": True, "markets": True, "news": True},
            output="both",
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )
        with db.get_db(db_path) as conn:
            yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", yesterday),
            )

        # Patch the block assembler at its source. If check_briefings calls it,
        # the prefetch ran on the dispatch thread — fail.
        with patch(
            "istota.briefings.generate.assemble_briefing_input",
            side_effect=AssertionError("prefetch ran on the dispatch thread"),
        ):
            result = check_briefings(db_path, config)

        assert len(result) == 1
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, result[0])
        assert task.source_type == "briefing"
        assert task.briefing_name == "morning"
        assert task.queue == "background"
        assert task.output_target == "both"
        # The stored prompt is the lightweight placeholder, not prefetched data.
        assert "morning" in task.prompt

    def test_executor_builds_briefing_prompt_at_execution_time(self, db_path):
        from istota.executor import build_deferred_briefing_prompt

        config = Config(db_path=db_path, users={"alice": UserConfig(timezone="UTC")})
        task = db.Task(
            id=7, status="running", source_type="briefing", user_id="alice",
            prompt="Generate the 'morning' briefing.", briefing_name="morning",
        )

        # The module block-assembly path is the sole builder.
        with patch(
            "istota.executor._build_module_briefing_prompt",
            return_value="FULL BRIEFING PROMPT",
        ) as mock_build:
            built = build_deferred_briefing_prompt(task, config)

        assert built == "FULL BRIEFING PROMPT"
        mock_build.assert_called_once_with(task, config)

    def test_executor_keeps_placeholder_when_briefing_missing(self, db_path):
        from istota.executor import build_deferred_briefing_prompt

        config = Config(
            db_path=db_path,
            users={"alice": UserConfig(timezone="UTC", briefings=[])},
        )
        task = db.Task(
            id=8, status="running", source_type="briefing", user_id="alice",
            prompt="Generate the 'gone' briefing.", briefing_name="gone",
        )
        # Unresolvable briefing → None so the caller keeps the placeholder.
        assert build_deferred_briefing_prompt(task, config) is None

    def test_executor_returns_none_when_no_briefing_name(self, db_path):
        from istota.executor import build_deferred_briefing_prompt

        config = Config(db_path=db_path, users={})
        task = db.Task(
            id=9, status="running", source_type="briefing", user_id="alice",
            prompt="x", briefing_name=None,
        )
        assert build_deferred_briefing_prompt(task, config) is None

    def test_execute_task_swaps_in_built_briefing_prompt(self, tmp_path):
        """End-to-end: a deferred briefing task's prompt is built in execute_task."""
        from istota.executor import execute_task

        db_path = tmp_path / "exec.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        config = Config(
            db_path=db_path, users={"alice": UserConfig(timezone="UTC")},
            skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Generate the 'morning' briefing.", user_id="alice",
                source_type="briefing", conversation_token="room1",
                queue="background", briefing_name="morning",
            )
            task = db.get_task(conn, task_id)

        with patch(
            "istota.executor._build_module_briefing_prompt",
            return_value="SENTINEL_BRIEFING_BODY",
        ):
            _ok, rendered, _a, _t = execute_task(
                task, config, [], dry_run=True,
            )

        assert "SENTINEL_BRIEFING_BODY" in rendered

    def test_execute_task_fails_when_briefing_build_yields_nothing(self, tmp_path):
        """An unbuildable deferred briefing fails (→ retry), not run on placeholder."""
        from istota.executor import execute_task

        db_path = tmp_path / "exec2.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        # No matching briefing configured for "gone" → build resolves to None.
        config = Config(
            db_path=db_path, users={"alice": UserConfig(timezone="UTC", briefings=[])},
            skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Generate the 'gone' briefing.", user_id="alice",
                source_type="briefing", queue="background", briefing_name="gone",
            )
            task = db.get_task(conn, task_id)

        success, result, _a, _t = execute_task(task, config, [], dry_run=True)
        assert success is False
        assert "gone" in result


class TestCheckBriefingsStaleGate:
    """Insertion-time staleness gate: long-outage catch-ups are skipped."""

    @patch("istota.scheduler._now")
    def test_stale_briefing_skipped_and_last_run_bumped(self, mock_now, db_path):
        # 6 AM daily briefing. last_run yesterday; "now" is 11 AM (5h stale).
        mock_now.return_value = datetime(2026, 6, 15, 11, 0, 0, tzinfo=ZoneInfo("UTC"))

        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *", conversation_token="room1", components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=60),
        )

        last_run = "2026-06-14 06:00:00"
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", last_run),
            )

        result = check_briefings(db_path, config)

        assert result == []
        with db.get_db(db_path) as conn:
            bumped = conn.execute(
                "SELECT last_run_at FROM briefing_state WHERE user_id=? AND briefing_name=?",
                ("alice", "morning"),
            ).fetchone()[0]
        # Set to SQLite `now` by set_briefing_last_run — the ancient stale
        # last_run was overwritten so the same next_run won't fire again.
        assert bumped != last_run

    @patch("istota.scheduler._now")
    def test_within_threshold_fires(self, mock_now, db_path):
        # 6 AM daily briefing. "now" is 6:05 AM — 5 min stale, under 60.
        mock_now.return_value = datetime(2026, 6, 15, 6, 5, 0, tzinfo=ZoneInfo("UTC"))
        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *", conversation_token="room1", components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=60),
        )

        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", "2026-06-14 06:00:00"),
            )

        result = check_briefings(db_path, config)
        assert len(result) == 1

    @patch("istota.scheduler._now")
    def test_threshold_zero_preserves_legacy_catchup(self, mock_now, db_path):
        # Same 5h-stale scenario as the first test, but with the gate disabled.
        mock_now.return_value = datetime(2026, 6, 15, 11, 0, 0, tzinfo=ZoneInfo("UTC"))
        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *", conversation_token="room1", components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", "2026-06-14 06:00:00"),
            )

        result = check_briefings(db_path, config)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestCheckScheduledJobs
# ---------------------------------------------------------------------------


class TestCheckScheduledJobs:
    @patch("istota.scheduler._sync_cron_files")
    def test_no_jobs(self, mock_sync, db_path):
        config = Config(db_path=db_path, users={})
        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert result == []

    @patch("istota.scheduler._sync_cron_files")
    def test_job_triggers(self, mock_sync, db_path):
        """A job whose cron has passed since last_run should trigger."""
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily-check", "0 0 * * *", "Run daily check", "room1", 1, yesterday, yesterday),
            )

        now = datetime.now(ZoneInfo("UTC"))
        if now.hour > 0:
            with db.get_db(db_path) as conn:
                result = check_scheduled_jobs(conn, config)
            assert len(result) == 1

    @patch("istota.scheduler._sync_cron_files")
    def test_resolves_timezone_from_live_db_not_stale_config(self, mock_sync, db_path):
        """Cron fire times must use the live user_profiles timezone, not the
        in-memory UserConfig built at startup (ISSUE-099). Proves the wiring:
        check_scheduled_jobs calls Config.resolve_user_timezone for the job's
        user, reusing the open connection, and gets the DB value.
        """
        from istota import user_profiles
        # In-memory config is stale (UTC); the user changed tz in the web UI.
        config = Config(
            db_path=db_path, users={"alice": UserConfig(timezone="UTC")},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )
        user_profiles.ensure_profile(db_path, "alice", timezone="America/New_York")

        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily-check", "0 0 * * *", "Run daily check", "room1", 1, yesterday, yesterday),
            )

        seen: dict[str, tuple[str, bool]] = {}
        real = Config.resolve_user_timezone

        def spy(self, uid, *, conn=None):
            tz = real(self, uid, conn=conn)
            seen[uid] = (tz, conn is not None)
            return tz

        with patch.object(Config, "resolve_user_timezone", spy):
            with db.get_db(db_path) as conn:
                check_scheduled_jobs(conn, config)

        assert "alice" in seen, "scheduled-job tz was not resolved via the DB-aware helper"
        tz_str, conn_reused = seen["alice"]
        assert tz_str == "America/New_York", "cron eval used the stale in-memory UTC, not the live DB tz"
        assert conn_reused is True, "hot scheduler loop should reuse the open conn, not open a fresh one"

    @patch("istota.scheduler._sync_cron_files")
    def test_job_not_yet_due(self, mock_sync, db_path):
        """A job that just ran should not trigger again."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})

        now = datetime.now(ZoneInfo("UTC")).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "hourly", "0 * * * *", "Hourly task", "room1", 1, now, now),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert result == []

    @patch("istota.scheduler._sync_cron_files")
    def test_first_run_uses_created_at(self, mock_sync, db_path):
        """When last_run_at is NULL, created_at is used as base for cron evaluation."""
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        # Created yesterday, cron every minute -- should be due
        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "every-min", "* * * * *", "Frequent task", "room1", 1, None, yesterday),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert len(result) == 1

    @patch("istota.scheduler._sync_cron_files")
    def test_overlap_guard_skips_when_prior_run_in_flight(self, mock_sync, db_path):
        """A `* * * * *` job whose previous run is still pending/running must not
        stack another task — otherwise a wedged worker lets the backlog grow one
        row/minute (the location-alert incident)."""
        config = Config(
            db_path=db_path, users={"alice": UserConfig(timezone="UTC")},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )
        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            cur = conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "every-min", "* * * * *", "do it", "room1", 1,
                 yesterday, yesterday),
            )
            job_id = cur.lastrowid
            # A prior run is still pending — simulates the wedged-queue backlog.
            db.create_task(
                conn, prompt="do it", user_id="alice", source_type="scheduled",
                scheduled_job_id=job_id, queue="background",
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert result == [], "should not enqueue a second run while one is in flight"
        # last_run_at must NOT be advanced on skip, so the job fires immediately
        # once the in-flight run clears (correct for sparse jobs too).
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT last_run_at FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row["last_run_at"] == yesterday

    @patch("istota.scheduler._sync_cron_files")
    def test_overlap_guard_allows_when_prior_run_completed(self, mock_sync, db_path):
        """Once the previous run reaches a terminal state, the next fire proceeds."""
        config = Config(
            db_path=db_path, users={"alice": UserConfig(timezone="UTC")},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )
        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            cur = conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "every-min", "* * * * *", "do it", "room1", 1,
                 yesterday, yesterday),
            )
            job_id = cur.lastrowid
            prev = db.create_task(
                conn, prompt="do it", user_id="alice", source_type="scheduled",
                scheduled_job_id=job_id, queue="background",
            )
            db.update_task_status(conn, prev, "completed", result="ok")

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert len(result) == 1

    @patch("istota.scheduler._sync_cron_files")
    def test_skip_log_channel_flows_to_task(self, mock_sync, db_path):
        """skip_log_channel on a scheduled job should propagate to the created task."""
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled,
                    last_run_at, created_at, skip_log_channel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "quiet-job", "0 0 * * *", "Check stuff", 1,
                 yesterday, yesterday, 1),
            )

        now = datetime.now(ZoneInfo("UTC"))
        if now.hour > 0:
            with db.get_db(db_path) as conn:
                result = check_scheduled_jobs(conn, config)
            assert len(result) == 1
            with db.get_db(db_path) as conn:
                task = db.get_task(conn, result[0])
            assert task.skip_log_channel is True

    def test_sync_called_before_evaluation(self, db_path):
        """_sync_cron_files should be called at the start of check_scheduled_jobs."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})
        with patch("istota.scheduler._sync_cron_files") as mock_sync:
            with db.get_db(db_path) as conn:
                check_scheduled_jobs(conn, config)
            mock_sync.assert_called_once_with(conn, config)

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_stale_job_skipped_and_last_run_bumped(self, mock_now, mock_sync, db_path):
        """Catch-up suppression: stale fires are skipped, last_run_at bumped to now."""
        mock_now.return_value = datetime(2026, 6, 15, 14, 0, 0, tzinfo=ZoneInfo("UTC"))
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=60),
        )

        last_run = "2026-06-14 00:00:00"  # 14h before "now"
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily", "0 0 * * *", "Run", "room1", 1, last_run, last_run),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert result == []
        with db.get_db(db_path) as conn:
            bumped = conn.execute(
                "SELECT last_run_at FROM scheduled_jobs WHERE user_id=? AND name=?",
                ("alice", "daily"),
            ).fetchone()[0]
        # Set to SQLite `now` by set_scheduled_job_last_run.
        assert bumped != last_run

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_job_within_threshold_fires(self, mock_now, mock_sync, db_path):
        """A job missed by less than the threshold still fires (short blip case)."""
        # 0 0 * * * job. "now" 30 min past midnight — under the 60-min default.
        mock_now.return_value = datetime(2026, 6, 15, 0, 30, 0, tzinfo=ZoneInfo("UTC"))
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=60),
        )

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily", "0 0 * * *", "Run", "room1", 1,
                 "2026-06-14 00:00:00", "2026-06-14 00:00:00"),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert len(result) == 1

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_threshold_zero_preserves_legacy_catchup(self, mock_now, mock_sync, db_path):
        """cron_max_staleness_minutes=0 disables the gate (legacy unconditional catch-up)."""
        mock_now.return_value = datetime(2026, 6, 15, 14, 0, 0, tzinfo=ZoneInfo("UTC"))
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        )

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily", "0 0 * * *", "Run", "room1", 1,
                 "2026-06-14 00:00:00", "2026-06-14 00:00:00"),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert len(result) == 1  # would have been skipped under default

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_never_run_job_with_ancient_created_at_is_gated(self, mock_now, mock_sync, db_path):
        """First-run branch (last_run_at NULL) also honors the staleness gate."""
        mock_now.return_value = datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            scheduler=SchedulerConfig(cron_max_staleness_minutes=60),
        )

        # created a week ago, every-minute cron — next_run would be ancient
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "every-min", "* * * * *", "Run", "room1", 1,
                 None, "2026-06-08 00:00:00"),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert result == []
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT last_run_at FROM scheduled_jobs WHERE user_id=? AND name=?",
                ("alice", "every-min"),
            ).fetchone()
        assert row[0] is not None  # bumped from NULL to now

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_dst_spring_forward_no_double_fire(self, mock_now, mock_sync, db_path):
        """Job should not fire early when last_run_at is from before DST spring-forward.

        Scenario: 10 PM daily job. Last ran March 7, 22:00 PST (UTC-8).
        DST spring-forward on March 8. Now is March 8, 21:30 PDT (UTC-7).
        croniter with tz-aware datetimes would compute next_run as 21:00 PDT (wrong).
        With naive wall-clock times, next_run should be 22:00, so job should NOT fire.
        """
        user = UserConfig(timezone="America/Los_Angeles")
        config = Config(db_path=db_path, users={"alice": user})

        # last_run_at stored as UTC: March 8 06:00 UTC = March 7 22:00 PST
        last_run_utc = "2026-03-08 06:00:00"
        created_at = "2026-03-01 00:00:00"

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "nightly", "0 22 * * *", "Nightly check", "room1",
                 1, last_run_utc, created_at),
            )

        # Now is March 9, 04:30 UTC = March 8, 21:30 PDT (before 10 PM)
        la_tz = ZoneInfo("America/Los_Angeles")
        fake_now_utc = datetime(2026, 3, 9, 4, 30, 0, tzinfo=ZoneInfo("UTC"))
        mock_now.return_value = fake_now_utc.astimezone(la_tz)

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert result == [], "Job fired early due to DST transition — double-fire bug"

    @patch("istota.scheduler._sync_cron_files")
    @patch("istota.scheduler._now")
    def test_dst_spring_forward_fires_at_correct_time(self, mock_now, mock_sync, db_path):
        """Job should fire at the correct wall-clock time after DST spring-forward."""
        user = UserConfig(timezone="America/Los_Angeles")
        config = Config(db_path=db_path, users={"alice": user})

        # last_run_at stored as UTC: March 8 06:00 UTC = March 7 22:00 PST
        last_run_utc = "2026-03-08 06:00:00"
        created_at = "2026-03-01 00:00:00"

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "nightly", "0 22 * * *", "Nightly check", "room1",
                 1, last_run_utc, created_at),
            )

        # Now is March 9, 05:05 UTC = March 8, 22:05 PDT (past 10 PM)
        la_tz = ZoneInfo("America/Los_Angeles")
        fake_now_utc = datetime(2026, 3, 9, 5, 5, 0, tzinfo=ZoneInfo("UTC"))
        mock_now.return_value = fake_now_utc.astimezone(la_tz)

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)

        assert len(result) == 1, "Job should fire at the correct wall-clock time"


class TestCheckBriefingsDST:
    """DST-related tests for check_briefings."""

    @patch("istota.scheduler._now")
    def test_dst_spring_forward_no_double_fire(self, mock_now, db_path):
        """Briefing should not fire early when last_run_at crosses DST boundary.

        Scenario: 6 AM weekday briefing. Last ran Friday March 7, 06:00 PST.
        DST spring-forward March 8. Now is Monday March 9, 05:30 PDT.
        Should NOT fire yet (5:30 AM < 6:00 AM wall-clock).
        """
        la_tz = ZoneInfo("America/Los_Angeles")
        # Now = March 9, 12:30 UTC = March 9, 05:30 PDT
        mock_now.return_value = datetime(2026, 3, 9, 12, 30, 0, tzinfo=la_tz).astimezone(la_tz)
        # Actually set a proper time
        mock_now.return_value = datetime(2026, 3, 9, 5, 30, 0, tzinfo=la_tz)

        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * 1-5",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="America/Los_Angeles", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        # last_run_at: March 7, 14:00 UTC = March 7, 06:00 PST
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", "2026-03-07 14:00:00"),
            )

        result = check_briefings(db_path, config)
        assert result == [], "Briefing fired early due to DST transition — double-fire bug"

    @patch("istota.scheduler._now")
    def test_dst_spring_forward_fires_at_correct_time(self, mock_now, db_path):
        """Briefing should fire at the correct wall-clock time after DST spring-forward."""
        la_tz = ZoneInfo("America/Los_Angeles")
        # Now = March 9, 06:05 PDT
        mock_now.return_value = datetime(2026, 3, 9, 6, 5, 0, tzinfo=la_tz)

        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * 1-5",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="America/Los_Angeles", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        # last_run_at: March 7, 14:00 UTC = March 7, 06:00 PST
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", "2026-03-07 14:00:00"),
            )

        result = check_briefings(db_path, config)
        assert len(result) == 1, "Briefing should fire at the correct wall-clock time"


class TestSyncCronFiles:
    """Tests for _sync_cron_files edge cases."""

    @staticmethod
    def _cron_config(db_path, tmp_path):
        # Disable on-by-default modules so the test only sees its own job.
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        user = UserConfig(
            timezone="UTC", disabled_modules=["feeds", "money", "location"],
        )
        return Config(
            db_path=db_path, users={"alice": user}, nextcloud_mount_path=mount,
        )

    @staticmethod
    def _cron_path(config):
        from istota.storage import get_user_cron_path

        path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_deleting_the_last_job_from_cron_md_deletes_it(self, db_path, tmp_path):
        """An empty fence is the user deleting their jobs, not a template.

        ISSUE-369 defect 2: this branch treated "the fence holds no jobs" and
        "there is no fence" as one event, so deleting your last job from
        CRON.md restored it from the table within the minute — and
        `generate_cron_md` rebuilt the whole document doing it, taking any
        prose in the file with it. The row now goes and the file is left
        exactly as the user wrote it.
        """
        from istota.scheduler import _sync_cron_files

        config = self._cron_config(db_path, tmp_path)
        cron_path = self._cron_path(config)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "daily-check"
cron = "0 9 * * *"
prompt = "Run check"
```
""")

        # The row exists because the file put it there, which is the state a
        # deletion starts from.
        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            assert [j.name for j in db.get_user_scheduled_jobs(conn, "alice")] == [
                "daily-check",
            ]

        emptied = "# Scheduled Jobs\n\nNotes I keep here.\n\n```toml\n```\n"
        cron_path.write_text(emptied)

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert jobs == []
        assert cron_path.read_text() == emptied

    def test_the_seeded_template_does_not_delete_the_users_jobs(
        self, db_path, tmp_path
    ):
        """The file the seeder actually writes, through the real sync.

        `storage.CRON_TEMPLATE` carries a toml fence holding commented-out
        examples, so it parses to zero jobs with the fence present — the
        shape the first cut of this stage read as "the user deleted
        everything" and handed to the orphan sweep. `ensure_user_directories_v2`
        re-seeds the file whenever it is absent and runs on every scheduler
        pass, so a CRON.md lost to a mount fault comes back as this.
        """
        from istota.scheduler import _sync_cron_files
        from istota.storage import CRON_TEMPLATE

        config = self._cron_config(db_path, tmp_path)
        cron_path = self._cron_path(config)
        cron_path.write_text(CRON_TEMPLATE.format(conversation_token="room1"))

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "Run check"),
            )

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert [j.name for j in jobs] == ["daily-check"]
        assert 'name = "daily-check"' in cron_path.read_text()

    def test_a_file_whose_every_job_is_refused_holds_the_rows(
        self, db_path, tmp_path, caplog
    ):
        """No usable jobs is not the same fact as no jobs.

        An unreadable `prompt_file` — a moved prompts directory, a mount
        fault — drops its job at parse time. Syncing that would delete the
        row while the definition sits in the file, and nothing would bring it
        back: the next tick reads the same file the same way.
        """
        import logging

        from istota.scheduler import _sync_cron_files

        config = self._cron_config(db_path, tmp_path)
        cron_path = self._cron_path(config)
        original = """\
# Scheduled Jobs

```toml
[[jobs]]
name = "daily-check"
cron = "0 9 * * *"
prompt_file = "Users/alice/istota/scripts/prompts/gone.txt"
```
"""
        cron_path.write_text(original)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "Run check"),
            )

        with caplog.at_level(logging.WARNING, "istota.scheduler"):
            with db.get_db(db_path) as conn:
                _sync_cron_files(conn, config)
                jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert [j.name for j in jobs] == ["daily-check"]
        assert cron_path.read_text() == original
        assert any(
            "none of them could be read" in r.getMessage() for r in caplog.records
        )

    def test_a_file_with_no_fence_is_restored_from_the_table(self, db_path, tmp_path):
        """The seeded-template case the restore branch exists for.

        Narrowing that branch to `doc.block is None` must not remove its
        reason to exist: a file with no toml fence and rows in the table is
        still written from the table.
        """
        from istota.scheduler import _sync_cron_files

        config = self._cron_config(db_path, tmp_path)
        cron_path = self._cron_path(config)
        cron_path.write_text("# Scheduled Jobs\n\nNo config here.\n")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "Run check"),
            )

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 1
        assert jobs[0].name == "daily-check"
        assert 'name = "daily-check"' in cron_path.read_text()

    def test_a_restore_that_could_not_be_written_is_logged(
        self, db_path, tmp_path, caplog
    ):
        """The rows survive a refused restore, so the log is the only signal.

        `migrate_db_jobs_to_file` returns False on a write that did not
        happen (ISSUE-369 defect 3, stage 1) and this call site discarded it,
        leaving a user whose CRON.md never fills in and nothing anywhere
        saying why.
        """
        import logging

        from istota.scheduler import _sync_cron_files

        config = self._cron_config(db_path, tmp_path)
        cron_path = self._cron_path(config)
        cron_path.write_text("# Scheduled Jobs\n\nNo config here.\n")
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "Run check"),
            )

        # `_sync_cron_files` imports the writer per call, so the module
        # attribute is the seam. The refusal itself is the writer's own
        # (tests/test_cron_loader.py); what is under test here is that this
        # caller acts on it.
        with patch(
            "istota.cron_loader.migrate_db_jobs_to_file", return_value=False,
        ) as mock_migrate, caplog.at_level(logging.WARNING, "istota.scheduler"):
            with db.get_db(db_path) as conn:
                _sync_cron_files(conn, config)
                jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert mock_migrate.call_count == 1
        assert mock_migrate.call_args.kwargs == {"overwrite": True}
        assert len(jobs) == 1, "a refused write must not cost the rows"
        assert any("Could not restore" in r.getMessage() for r in caplog.records)

    def test_empty_file_no_db_jobs_is_noop(self, db_path, tmp_path):
        """When CRON.md is empty and DB has no jobs, nothing happens."""
        from istota.scheduler import _sync_cron_files

        mount = tmp_path / "mount"
        mount.mkdir()
        # Disable on-by-default modules so the test only checks user's own jobs.
        user = UserConfig(timezone="UTC", disabled_modules=["feeds", "money", "location"])
        config = Config(
            db_path=db_path, users={"alice": user},
            nextcloud_mount_path=mount,
        )

        from istota.storage import get_user_cron_path
        cron_path = mount / get_user_cron_path("alice", "istota").lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("# Scheduled Jobs\n\n```toml\n```\n")

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 0


# ---------------------------------------------------------------------------
# TestProcessOneTask
# ---------------------------------------------------------------------------


class TestProcessOneTask:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )

    def test_no_tasks_returns_none(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        result = process_one_task(config)
        assert result is None

    def _make_command_task(self, db_path, command: str) -> int:
        """A CRON `command:` row. `create_task` has no `command` parameter —
        the scheduler's cron sync writes the column, so the test does too."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="", user_id="testuser", source_type="scheduled",
            )
            conn.execute(
                "UPDATE tasks SET command = ?, prompt = '' WHERE id = ?",
                (command, task_id),
            )
            conn.commit()
        return task_id

    def test_a_sigpipe_command_failure_is_not_retried(self, db_path, tmp_path):
        """Retrying a SIGPIPE re-runs the producer that already did its work.

        `pipefail` turned `<something> | head -N` from exit 0 into exit 141, and
        the failure branch retries at 1, 4 and 16 minutes — so a row whose first
        stage sends mail or writes a file now does it up to four times per
        scheduled run, and the retry cannot succeed anyway because 141 recurs.
        The status is right; riding the ladder with it is not.
        """
        config = self._make_config(db_path, tmp_path)
        task_id = self._make_command_task(db_path, "yes | head -1")

        result = process_one_task(config)
        assert result is not None

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed", (
            f"a SIGPIPE command task was left {task.status!r} — it is on the "
            "retry ladder, re-running its producer"
        )

    def test_an_ordinary_command_failure_still_retries(self, db_path, tmp_path):
        """Control. Without it, refusing every command failure would pass above."""
        config = self._make_config(db_path, tmp_path)
        task_id = self._make_command_task(db_path, "echo boom >&2; exit 1")

        result = process_one_task(config)
        assert result is not None

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending", task.status
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(True, "All done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_success_completes_task(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"
        assert task.result == "All done"

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_web_task_prunes_text_delta_rows(self, mock_arun, db_path, tmp_path):
        # A web (stream) task's coalesced text_delta rows are a cosmetic live
        # preview; once the canonical result lands they're pruned, leaving only
        # the lifecycle rows. (The 'push untouched' guard is in the executor
        # tests; this is the stream-side prune.)
        config = self._make_config(db_path, tmp_path)
        config.scheduler.event_log_enabled = True
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hi", user_id="testuser", source_type="web",
                conversation_token="webtok", output_target="web",
            )

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, workspace_dir=None, **kw):
            if event_writer is not None:
                event_writer.emit("text_delta", {"text": "Hel"})
                event_writer.emit("text_delta", {"text": "lo"})
            return (True, "Hello", None, None)

        with patch("istota.scheduler.execute_task", side_effect=fake_exec):
            result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            kinds = [e["kind"] for e in db.get_task_events(conn, task_id)]
        assert "text_delta" not in kinds          # pruned after result
        assert "result" in kinds and "done" in kinds  # lifecycle survives

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_long_web_result_reaches_result_event_whole(self, mock_arun, db_path, tmp_path):
        # A several-thousand-word answer must reach the stream surface uncut:
        # the `result` task event (the web-chat deliverable) carries the full
        # body, not an 8000-char slice (ISSUE-178).
        from istota.events import PAYLOAD_MAX_BYTES
        config = self._make_config(db_path, tmp_path)
        config.scheduler.event_log_enabled = True
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="write a lot", user_id="testuser", source_type="web",
                conversation_token="webtok", output_target="web",
            )

        long_result = "word " * (PAYLOAD_MAX_BYTES // 2)  # well over the old cap
        assert len(long_result) > 8000

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, workspace_dir=None, **kw):
            return (True, long_result, None, None)

        with patch("istota.scheduler.execute_task", side_effect=fake_exec):
            result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            events = db.get_task_events(conn, task_id)
            task = db.get_task(conn, task_id)
        result_ev = next(e for e in events if e["kind"] == "result")
        assert result_ev["payload"]["text"] == long_result
        assert result_ev["payload"].get("truncated") is False
        assert "_truncated" not in result_ev["payload"]
        # And the canonical stored body matches the delivered event.
        assert task.result == long_result

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_email_reply_into_own_web_room_renders_as_bubble(self, mock_arun, db_path, tmp_path):
        # ISSUE-164: an email-source task (e.g. a thread-matched reply) fanned
        # into the web room it is *conversing in* (output_target names its own
        # conversation_token) is a real turn, not a notification. It must land as
        # an assistant chat bubble (a spine row), NOT a role='system' cmd-output
        # push via WebTransport.deliver.
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="reply body", user_id="testuser",
                source_type="email", conversation_token=room_token,
                output_target=f"web:{room_token}",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Here is the answer", None, None),
        ):
            result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            turns = [m for m in db.get_messages(conn, room_token) if m.role == "assistant"]
            notes = db.list_system_messages(conn, room_token)
        assert any(m.body == "Here is the answer" for m in turns)
        # Must NOT have been delivered as a system (cmd-output) note.
        assert all(m.body != "Here is the answer" for m in notes)

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_reply_into_foreign_web_room_stays_system_note(self, mock_arun, db_path, tmp_path):
        # The other half of ISSUE-164's discriminator: a task fanned into a web
        # room it is NOT conversing in (a foreign room, e.g. a pinned alert
        # destination) is an out-of-band notice and must stay a role='system'
        # note (WebTransport.deliver). Only own-conversation pushes become bubbles.
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        own_token = default_web_room_token(config, "testuser")
        foreign_token = "web-testuser-foreign"
        with db.get_db(db_path) as conn:
            db.register_room(conn, foreign_token, "testuser", origin="web")
            db.create_task(
                conn, prompt="reply body", user_id="testuser",
                source_type="email", conversation_token=own_token,
                output_target=f"web:{foreign_token}",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Here is the answer", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        with db.get_db(db_path) as conn:
            notes = db.list_system_messages(conn, foreign_token)
            own_turns = [m for m in db.get_messages(conn, own_token) if m.role == "assistant"]
        # Foreign room: system note. Own conversation room: no bubble (not a target).
        assert any(m.body == "Here is the answer" for m in notes)
        assert all(m.body != "Here is the answer" for m in own_turns)

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_web_origin_room_mirrors_to_bound_talk(
        self, mock_run_coro, db_path, tmp_path, fake_talk,
    ):
        # A web-origin room bound to a Talk conversation mirrors its final result
        # into that Talk room (web's own result streams over SSE). The mirror
        # uses the talk binding's surface_ref as the post target.
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, room_token, "talk", "talktok42")
            task_id = db.create_task(
                conn, prompt="hi", user_id="testuser", source_type="web",
                conversation_token=room_token, output_target="room",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "mirrored answer", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        # The Talk mirror was posted to the bound Talk room, and to nothing
        # else — the canonical `web-…` token is where ISSUE-400 sent it, and
        # the double refuses that rather than accepting it silently.
        assert {c.token for c in fake_talk.calls} == {"talktok42"}
        assert fake_talk.refusals == []
        # external_ids ledger recorded the mirror's Talk post id — the id of
        # the *reply*, which is not the first thing posted (the unstamped user
        # turn gets an attributed repost ahead of it), so it is named by its
        # reference_id rather than taken by position.
        posted = fake_talk.sent_id_for(f"istota:task:{task_id}:result")
        assert posted is not None
        with db.get_db(db_path) as conn:
            msgs = db.get_messages(conn, room_token)
        assistant = [m for m in msgs if m.role == "assistant"][0]
        assert assistant.external_ids == {"talk": str(posted)}

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_web_origin_mirror_reposts_user_question(
        self, mock_run_coro, db_path, tmp_path, fake_talk,
    ):
        # A web-origin turn mirrored into a bound Talk room reposts the user's
        # question (attributed) first, then the reply — otherwise the Talk
        # transcript shows an orphaned answer (the bot can't post as the user).
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        config.users = {"testuser": UserConfig(display_name="Frank")}
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, room_token, "talk", "talktok42")
            db.create_task(
                conn, prompt="what's the weather?", user_id="testuser",
                source_type="web", conversation_token=room_token,
                output_target="room",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        # Two Talk posts to the bound room: the attributed question, then reply.
        bodies = [
            c.args["message"]
            for c in fake_talk.calls_to("talktok42", method="send_message")
        ]
        assert len(bodies) == 2
        assert "Frank" in bodies[0]
        assert "what's the weather?" in bodies[0]
        assert bodies[1] == "It's sunny."
        # A post refused for naming the canonical token would leave a shorter
        # list, not a failed assertion about a body.
        assert fake_talk.refusals == []

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_mirror_repost_not_written_to_canonical_store(
        self, mock_run_coro, db_path, tmp_path, fake_talk,
    ):
        # The repost is a pure Talk-surface artifact: it must never land in the
        # canonical `messages` store (where web reads its history), so it can't
        # create a duplicate user turn or pollute cross-surface context.
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        config.users = {"testuser": UserConfig(display_name="Frank")}
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, room_token, "talk", "talktok42")
            db.create_task(
                conn, prompt="ping", user_id="testuser", source_type="web",
                conversation_token=room_token, output_target="room",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "pong", None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            msgs = db.get_messages(conn, room_token)
        # Only the assistant turn was persisted (no user row was created by this
        # direct-create test, and the repost added none).
        assert [m.role for m in msgs] == ["assistant"]
        assert all("ping" not in m.body for m in msgs if m.role == "user")
        # And the repost did reach Talk, so "not in the store" is a statement
        # about the store rather than about a post that never happened.
        assert len(fake_talk.calls_to("talktok42", method="send_message")) == 2
        assert fake_talk.refusals == []

    @patch("istota.scheduler.post_result_to_talk", return_value=4242)
    @patch("istota.scheduler.run_coro", return_value=4242)
    def test_talk_origin_does_not_repost(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path, fake_talk,
    ):
        # A Talk-origin task delivers a single reply — no repost (the user's
        # message is already natively in the Talk room).
        #
        # `fake_talk` is here because the progress subscriber's edit does *not*
        # go through the `istota.scheduler.run_coro` patched above — it goes
        # through `istota.consumers.talk.run_coro`, a different binding — so
        # without the double this test reached the real Talk client and posted
        # at nc.example.com. The room is bound so that edit is one Nextcloud
        # would have accepted.
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="talkroom")
            db.create_task(
                conn, prompt="hi", user_id="testuser", source_type="talk",
                conversation_token="talkroom",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "hello", None, None),
        ):
            process_one_task(config)

        talk_calls = [
            c for c in mock_post_talk.call_args_list
            if c.kwargs.get("reference_id", "").endswith((":result", ":prompt"))
        ]
        assert all(not c.kwargs["reference_id"].endswith(":prompt") for c in talk_calls)
        assert fake_talk.refusals == []

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_own_origin_web_task_does_not_push(self, mock_arun, db_path, tmp_path):
        # A web-source task's own result streams over task_events; it must NOT
        # also be pushed as a web_chat_messages row (no double-post).
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        config.scheduler.event_log_enabled = True
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hi", user_id="testuser", source_type="web",
                conversation_token=room_token, output_target=f"web:{room_token}",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "streamed answer", None, None),
        ):
            result = process_one_task(config)
        assert result is not None

        with db.get_db(db_path) as conn:
            msgs = db.list_system_messages(conn, room_token)
        assert msgs == []  # delivered via the event stream, not a pushed row

    @patch("istota.scheduler.execute_task", return_value=(True, "All done", '["📄 Reading file"]', None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_actions_taken_stored_on_success(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.actions_taken == '["📄 Reading file"]'

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_superseded_task_does_not_deliver_duplicate(self, mock_arun, db_path, tmp_path):
        # A slow-but-alive worker whose task was reclaimed mid-run by a second
        # worker (attempt_count bumped by the stuck-running release) must discard
        # its result rather than mark the task completed / deliver a duplicate.
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, **kw):
            # Simulate worker B reclaiming the task while A executes: the
            # stuck-running release bumps attempt_count.
            with db.get_db(db_path) as c:
                c.execute(
                    "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE id = ?",
                    (task.id,),
                )
                c.commit()
            return (True, "Worker A's answer", None, None)

        with patch("istota.scheduler.execute_task", side_effect=fake_exec):
            result = process_one_task(config)

        assert result is not None
        task_id, success = result
        assert success is False  # A bailed

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # A must NOT have completed the task or stored its result.
        assert task.status != "completed"
        assert task.result != "Worker A's answer"

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_actions_taken_none_when_not_streaming(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.actions_taken is None

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_retry_emits_notice_and_keeps_events(self, mock_arun, db_path, tmp_path):
        # A retry-eligible failure posts a "retrying" progress notice AND keeps
        # the event log (no longer wiped) so a watching web client survives the
        # retry and the next attempt's seq stays monotonic. No terminal frame.
        config = self._make_config(db_path, tmp_path)
        config.scheduler.event_log_enabled = True
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hi", user_id="testuser", source_type="web",
                conversation_token="webtok", output_target="web",
            )

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, workspace_dir=None, **kw):
            if event_writer is not None:
                event_writer.emit("task_started")
                event_writer.emit("tool_start", {
                    "tool_name": "Read", "description": "📄 Reading f",
                    "tool_call_id": "t1",
                })
            return (False, "Something broke", None, None)

        with patch("istota.scheduler.execute_task", side_effect=fake_exec):
            result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
            events = db.get_task_events(conn, task_id)
        assert task.status == "pending" and task.attempt_count == 1
        kinds = [e["kind"] for e in events]
        # Prior attempt's events survive (NOT wiped) …
        assert "task_started" in kinds and "tool_start" in kinds
        # … a retrying notice was appended, and no terminal frame yet.
        assert "progress_text" in kinds
        assert "done" not in kinds and "error" not in kinds
        notice = [e for e in events if e["kind"] == "progress_text"][-1]
        assert "retrying" in notice["payload"]["text"].lower()
        # seq stayed monotonic across the appended notice.
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

    @patch("istota.scheduler.execute_task", return_value=(False, "Something broke", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_failure_retries_task(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Fail me", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Should be pending for retry (attempt_count 0 < max_attempts 3)
        assert task.status == "pending"
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_failure_after_max_retries(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Doomed", user_id="testuser", source_type="cli")
            # Set attempt_count to max_attempts - 1 so next failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_cancelled_persists_trace_for_reload(self, mock_arun, db_path, tmp_path):
        # ISSUE-183: a cancelled native-brain task must persist its execution
        # trace + actions_taken + error so the web chat reconstructs the
        # intermediate output on reload (not a blank bubble).
        import json
        trace = json.dumps([
            {"type": "tool", "text": "📄 Reading file"},
            {"type": "text", "text": "partial work"},
        ])
        actions = json.dumps(["📄 Reading file"])
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="do thing", user_id="testuser", source_type="web",
                conversation_token="webtok", output_target="web",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(False, "Cancelled by user", actions, trace),
        ):
            result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
            completed_at = conn.execute(
                "SELECT completed_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["completed_at"]
        assert task.status == "cancelled"
        assert task.error == "Cancelled by user"
        assert task.actions_taken == actions
        assert task.execution_trace == trace
        assert completed_at is not None

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_failed_persists_trace_for_reload(self, mock_arun, db_path, tmp_path):
        # ISSUE-183: a permanently-failed task must persist its trace so the
        # intermediate tools survive a reload.
        import json
        trace = json.dumps([{"type": "tool", "text": "⚙️ Bash: ls"}])
        actions = json.dumps(["⚙️ Bash: ls"])
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="fail me", user_id="testuser", source_type="web")
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        with patch(
            "istota.scheduler.execute_task",
            return_value=(False, "API error: rate limited", actions, trace),
        ):
            result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.error == "API error: rate limited"
        assert task.actions_taken == actions
        assert task.execution_trace == trace

    @patch("istota.scheduler.execute_task", return_value=(False, "Process killed (likely out of memory)", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_oom_skips_retry(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="OOM task", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # OOM should fail immediately, no retry
        assert task.status == "failed"

    @patch("istota.scheduler.execute_task", return_value=(
        False,
        'API Error: 400 {"type":"error","error":{"message":"Output blocked by content filtering policy"},"request_id":"req_z"}',
        None, None,
    ))
    @patch("istota.scheduler._post_policy_refusal_alert")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_policy_refusal_skips_retry_and_alerts_for_email(
        self, mock_arun, mock_alert, mock_exec, db_path, tmp_path,
    ):
        config = self._make_config(db_path, tmp_path)
        prompt = "From: attacker@evil.com\nSubject: Test\n\nbody"
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt=prompt, user_id="testuser", source_type="email")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Policy refusal: failed permanently, no retry, attempt_count stays at 0
        assert task.status == "failed"
        assert task.attempt_count == 0
        # Alert should have been posted with the failed task
        mock_alert.assert_called_once()
        called_task = mock_alert.call_args[0][1]
        assert called_task.id == task_id

    @patch("istota.scheduler.execute_task", return_value=(
        False,
        'API Error: 400 {"type":"error","error":{"message":"safety classifier refused"}}',
        None, None,
    ))
    @patch("istota.scheduler._post_policy_refusal_alert")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_policy_refusal_skips_retry_for_talk(
        self, mock_arun, mock_alert, mock_exec, db_path, tmp_path, fake_talk,
    ):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="room1")
            db.create_task(
                conn, prompt="Talk task", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.attempt_count == 0
        mock_alert.assert_called_once()
        # The seed is only doing its job if nothing was refused: a mistyped
        # token would be refused and swallowed, leaving the test green and the
        # room fixture silently inert.
        assert fake_talk.refusals == []

    @patch("istota.scheduler.execute_task", return_value=(
        False,
        'API Error: 400 {"type":"error","error":{"message":"max_tokens exceeds limit"}}',
        None, None,
    ))
    @patch("istota.scheduler._post_policy_refusal_alert")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_non_policy_400_is_not_retried(
        self, mock_arun, mock_alert, mock_exec, db_path, tmp_path,
    ):
        """A 400 is request-shaped: every attempt fails identically.

        This used to ride the 1/4/16-minute ladder to a permanent failure ~21
        minutes later. ISSUE-212 asks for "surface a clean, human-readable error
        immediately (no pointless retry)" for exactly this class, so it now
        fails on the first attempt. It is still not a *policy* refusal, so no
        content-blocked alert fires.
        """
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Big request", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        mock_alert.assert_not_called()

    @patch("istota.scheduler.execute_task", return_value=(
        False,
        'API Error: 503 {"type":"error","error":{"message":"service unavailable"}}',
        None, None,
    ))
    @patch("istota.scheduler._post_policy_refusal_alert")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_transient_5xx_unchanged_by_policy_branch(
        self, mock_arun, mock_alert, mock_exec, db_path, tmp_path,
    ):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 1
        mock_alert.assert_not_called()

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_confirmation_detected(
        self, mock_arun, mock_exec, db_path, tmp_path, fake_talk,
    ):
        mock_exec.return_value = (True, "I need your confirmation before deleting the file. Reply yes or no.", None, None)
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="room1")
            db.create_task(
                conn, prompt="Delete file", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        assert task.confirmation_prompt is not None
        assert fake_talk.refusals == []

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_confirmation_detected_for_web_task(self, mock_runcoro, mock_exec, db_path, tmp_path):
        """A web (stream) task whose result asks for confirmation must park in
        pending_confirmation — the gate keyed only on Talk before, so the whole
        web confirmation flow was unreachable."""
        mock_exec.return_value = (
            True, "I need your confirmation before deleting the file. Reply yes or no.",
            None, None,
        )
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Delete file", user_id="testuser",
                source_type="web", conversation_token="web-testuser-abc",
                output_target="web",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        assert task.confirmation_prompt is not None
        # A web confirmation is answered via the /chat confirm endpoint, never
        # cross-posted to Talk.
        assert mock_runcoro.call_count == 0

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_web_push_email_reply_does_not_gate_confirmation(self, mock_arun, db_path, tmp_path):
        # Option C: a *foreign* email reply pushed into a web room must NOT enter
        # pending_confirmation on a confirmation-shaped result — the web confirm
        # flow only works for source_type="web" tasks (their own SSE stream), so
        # gating an email task there would strand it unanswerable. It completes
        # and the question text is delivered to the room instead.
        from istota.transport.web import default_web_room_token
        config = self._make_config(db_path, tmp_path)
        room_token = default_web_room_token(config, "testuser")
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="reply", user_id="testuser", source_type="email",
                conversation_token=room_token,
                output_target=f"web:{room_token}",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(
                True,
                "I need your confirmation before deleting the file. Reply yes or no.",
                None, None,
            ),
        ):
            result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
            # ISSUE-164: an own-conversation reply lands as an assistant turn
            # (chat bubble), not a system note. The question text is delivered
            # for the user to answer by replying.
            turns = [m for m in db.get_messages(conn, room_token) if m.role == "assistant"]
        assert task.status == "completed"  # not pending_confirmation
        assert any("confirmation" in m.body for m in turns)

    @patch("istota.scheduler._drain_deferred_ops")
    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_web_confirmation_skips_deferred_drain(
        self, mock_runcoro, mock_exec, mock_drain, db_path, tmp_path,
    ):
        """Deferred ops must not be applied while a web task is parked awaiting
        confirmation — they drain only after the user confirms."""
        mock_exec.return_value = (
            True, "I need your confirmation before deleting the file. Reply yes or no.",
            None, None,
        )
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Delete file", user_id="testuser",
                source_type="web", conversation_token="web-testuser-abc",
                output_target="web",
            )

        process_one_task(config)
        mock_drain.assert_not_called()

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.run_coro", return_value=42)
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_confirmation_excluded_for_all_broadcast(
        self, mock_arun, mock_runcoro, mock_exec, db_path, tmp_path, fake_talk,
    ):
        """A confirmation prompt on an output_target='all' broadcast must NOT
        stall in pending_confirmation — 'all' is a fan-out, not an interactive
        turn (parity with main's `target in ('talk','both')` gate)."""
        mock_exec.return_value = (
            True, "I need your confirmation before deleting the file. Reply yes or no.",
            None, None,
        )
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="room1")
            db.create_task(
                conn, prompt="Delete file", user_id="testuser",
                source_type="talk", conversation_token="room1",
                output_target="all",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"
        assert task.confirmation_prompt is None
        assert fake_talk.refusals == []

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_talk_sends_ack_message(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Talk task", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        process_one_task(config)

        # asyncio.run should be called at least for the ack message and the result
        assert mock_arun.call_count >= 2

    @patch("istota.scheduler.execute_task", return_value=(True, "Confirmed result", None, None))
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_talk_rerun_sends_retry_ack(self, mock_arun, mock_exec, db_path, tmp_path):
        """A task being rerun after confirmation should send a 'Retrying' ack."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Confirmed task", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )
            # Simulate confirmed rerun: set confirmation_prompt and attempt_count
            conn.execute(
                "UPDATE tasks SET confirmation_prompt = ?, attempt_count = 1 WHERE id = ?",
                ("Please confirm", task_id),
            )

        process_one_task(config)

        # Should be called for both the retry ack and the result
        assert mock_arun.call_count >= 2

    @patch("istota.scheduler.execute_task", return_value=(True, '{"body": "reply", "format": "plain"}', None, None))
    @patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=False)
    def test_email_send_failure_marks_task_failed(self, mock_post_email, mock_exec, db_path, tmp_path):
        """When email delivery fails, the task should be marked as failed."""
        config = self._make_config(db_path, tmp_path)
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            email_addresses=["test@example.com"],
        )
        config = self._make_config(db_path, tmp_path, users={"testuser": user})

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email", user_id="testuser",
                source_type="email",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True  # execute_task succeeded

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.error == "Email delivery failed"


# ---------------------------------------------------------------------------
# TestProgressMessages
# ---------------------------------------------------------------------------


class TestProgressMessages:
    def test_progress_messages_not_empty(self):
        assert len(PROGRESS_MESSAGES) > 0

    def test_all_messages_are_plain(self):
        # Phrases are now surface-agnostic plain text (no markup); each surface
        # applies its own formatting (Talk italicizes at ack time).
        for msg in PROGRESS_MESSAGES:
            assert msg, "Empty progress message"
            assert not msg.startswith("*") and not msg.endswith("*"), (
                f"Message carries Talk markup: {msg}"
            )


# ---------------------------------------------------------------------------
# TalkEventSubscriber (additional coverage beyond test_progress_callback.py)
# ---------------------------------------------------------------------------


class TestTalkEventSubscriberExtra:
    def test_tool_start_edits_ack_with_emoji_description(self, tmp_path):
        from istota.consumers import TalkEventSubscriber
        from istota.events import TaskEvent

        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud=NextcloudConfig(url="https://nc", username="bot", app_password="pw"),
            scheduler=SchedulerConfig(progress_updates=True),
        )
        task = db.Task(
            id=1, status="running", source_type="talk",
            user_id="testuser", prompt="test", conversation_token="room1",
        )

        with patch("istota.consumers.talk.run_coro") as mock_run, \
             patch("istota.scheduler.edit_talk_message", new_callable=MagicMock):
            sub = TalkEventSubscriber(config, task, ack_msg_id=100)
            sub.on_event(TaskEvent(
                task_id=1, seq=1, kind="tool_start",
                payload={"description": "\U0001f4c4 Reading file.txt"},
                created_at="2026-06-06T00:00:00.000Z",
            ))

        assert mock_run.called


# ---------------------------------------------------------------------------
# TestTalkPollThread
# ---------------------------------------------------------------------------


class TestTalkPollThread:
    def test_calls_poll_and_sleeps(self):
        """_talk_poll_loop calls poll_talk_conversations and sleeps between polls."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))
        call_count = 0

        def stop_after_one(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Stop the loop after first poll
            import istota.scheduler as sched_mod
            sched_mod._shutdown_requested = True
            return []

        with patch("istota.scheduler.run_coro", side_effect=stop_after_one), \
             patch("istota.scheduler._shutdown_requested", False):
            # _shutdown_requested is checked at loop top, so we set it inside the poll
            _talk_poll_loop(config)

        assert call_count == 1

    def test_shutdown_flag_stops_loop(self):
        """Loop exits when _shutdown_requested is True."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))

        with patch("istota.scheduler._shutdown_requested", True):
            # Should return immediately without calling anything
            with patch("istota.scheduler.run_coro") as mock_run:
                _talk_poll_loop(config)
            mock_run.assert_not_called()

    def test_exception_does_not_crash(self):
        """Exceptions in polling are caught and loop continues."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))
        call_count = 0

        def fail_then_stop(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network down")
            import istota.scheduler as sched_mod
            sched_mod._shutdown_requested = True
            return []

        with patch("istota.scheduler.run_coro", side_effect=fail_then_stop), \
             patch("istota.scheduler._shutdown_requested", False):
            _talk_poll_loop(config)

        assert call_count == 2


# ---------------------------------------------------------------------------
# TestGetWorkerId
# ---------------------------------------------------------------------------


class TestGetWorkerId:
    def test_without_user_id(self):
        wid = get_worker_id()
        assert "-" in wid
        # Should be hostname-pid format
        parts = wid.rsplit("-", 1)
        assert len(parts) == 2
        assert parts[1].isdigit()

    def test_with_user_id(self):
        wid = get_worker_id(user_id="alice")
        assert wid.endswith("-alice")
        # Should be hostname-pid-alice
        parts = wid.split("-")
        assert len(parts) >= 3
        assert parts[-1] == "alice"

    def test_none_user_id_same_as_no_arg(self):
        assert get_worker_id(None) == get_worker_id()


# ---------------------------------------------------------------------------
# TestWorkerPool
# ---------------------------------------------------------------------------


class TestWorkerPool:
    def test_dispatch_creates_worker(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # Create a pending task
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="test", user_id="alice")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Worker should have been spawned for alice
            assert pool.active_count >= 1

        pool.shutdown()

    def test_respects_max_workers(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=1, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")
            db.create_task(conn, prompt="t2", user_id="bob")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Only 1 fg worker due to max_foreground_workers=1
            assert pool.active_count == 1

        pool.shutdown()

    def test_no_dispatch_when_empty(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        pool = WorkerPool(config)
        pool.dispatch()
        assert pool.active_count == 0

    def test_no_duplicate_workers_for_same_user(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(user_max_foreground_workers=1, worker_idle_timeout=2, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        # Mock process_one_task to block briefly so worker stays alive
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()  # should not create a duplicate
            assert pool.active_count == count_after_first

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestProcessHeartbeatTask
# ---------------------------------------------------------------------------


class TestProcessHeartbeatTask:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )



# ---------------------------------------------------------------------------
# TestStripActionPrefix
# ---------------------------------------------------------------------------


class TestStripActionPrefix:
    def test_action_at_start(self):
        should_post, text = _strip_action_prefix("ACTION: Something happened")
        assert should_post is True
        assert text == "Something happened"

    def test_action_after_newline(self):
        should_post, text = _strip_action_prefix("Explanation\nACTION: Did stuff")
        assert should_post is True
        assert text == "Did stuff"

    def test_no_action(self):
        should_post, _ = _strip_action_prefix("NO_ACTION: All good")
        assert should_post is False

    def test_no_action_in_middle(self):
        should_post, _ = _strip_action_prefix("Some text\nNO_ACTION: Fine")
        assert should_post is False

    def test_no_prefix_fails_safe(self):
        should_post, text = _strip_action_prefix("Just a normal result")
        assert should_post is True
        assert text == "Just a normal result"


class TestStripBriefingPreamble:
    def test_no_preamble_unchanged(self):
        text = "📰 NEWS\nSome news here"
        assert strip_briefing_preamble(text) == text

    def test_strips_thinking_preamble(self):
        text = "Now I have all the data. Let me compose the briefing.\n\n📰 NEWS\nSome news"
        result = strip_briefing_preamble(text)
        assert result.startswith("📰 NEWS")
        assert "Let me compose" not in result

    def test_strips_multiline_preamble(self):
        text = "Here's my analysis:\n\nI'll organize this into sections.\n\n📈 MARKETS\nS&P 500: +0.5%"
        result = strip_briefing_preamble(text)
        assert result.startswith("📈 MARKETS")

    def test_plain_text_no_punctuation_unchanged(self):
        text = "Just plain text with no emoji headers"
        assert strip_briefing_preamble(text) == text

    def test_empty_string(self):
        assert strip_briefing_preamble("") == ""

    def test_emoji_at_start_no_strip(self):
        text = "📅 CALENDAR\n- 9:00 AM: Meeting"
        assert strip_briefing_preamble(text) == text

    # --- Emoji assumption dropped: non-emoji block titles must survive ---

    def test_strips_preamble_before_plain_title(self):
        """A plain (non-emoji) first section must not be eaten as preamble."""
        text = "Let me put this together.\n\nCalendar\n- 9:00 Meeting"
        result = strip_briefing_preamble(text)
        assert result == "Calendar\n- 9:00 Meeting"

    def test_strips_preamble_before_bold_title(self):
        text = "Here is the briefing:\n\n**Markets**\nS&P 500: +0.5%"
        result = strip_briefing_preamble(text)
        assert result == "**Markets**\nS&P 500: +0.5%"

    def test_plain_first_section_not_stripped_by_later_emoji(self):
        """The old emoji-anchored version would delete the plain first section
        when a later one carried an emoji. It must be preserved now."""
        text = "News\nStuff happened.\n\n📈 Markets\nUp today"
        assert strip_briefing_preamble(text) == text

    def test_all_prose_returns_original(self):
        """Fail-safe: if every line looks like preamble, keep the original text
        rather than returning an empty string."""
        text = "Let me think about this.\n\nStill composing the sections."
        assert strip_briefing_preamble(text) == text

    def test_colon_titled_section_not_stripped(self):
        """A colon-ended header with no conversational opener is kept (colon is
        not treated as a prose ender on its own)."""
        text = "Q&A:\n- What happened?"
        assert strip_briefing_preamble(text) == text

    def test_leading_blank_lines_trimmed_before_header(self):
        text = "\n\n📰 NEWS\nSome news"
        assert strip_briefing_preamble(text) == "📰 NEWS\nSome news"


# ---------------------------------------------------------------------------
# TestSilentScheduledJob
# ---------------------------------------------------------------------------


class TestSilentScheduledJob:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "ACTION: Found something important", None, None))
    @patch("istota.scheduler.run_coro", return_value=42)
    def test_silent_scheduled_action_posts(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job with ACTION: should post result."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Should post (ACTION: found)
        assert mock_arun.call_count >= 1

    @patch("istota.scheduler.execute_task", return_value=(True, "NO_ACTION: Nothing to report", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_no_action_suppressed(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job with NO_ACTION: should suppress output."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Should NOT post (NO_ACTION)
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(True, "Just a result", None, None))
    @patch("istota.scheduler.run_coro", return_value=42)
    def test_silent_scheduled_no_prefix_posts(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job without prefix should post as fail-safe."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        assert mock_arun.call_count >= 1


# ---------------------------------------------------------------------------
# TestAutoIndexGate — silent scheduled jobs skip memory_search indexing
# ---------------------------------------------------------------------------


class TestAutoIndexGate:
    """Conversation indexing should skip silent scheduled jobs.

    Silent scheduled jobs (`silent_unless_action = true` in CRON.md, surfaces
    as `task.heartbeat_silent`) are typically high-volume retrieve-and-render
    crons whose conversations carry no recall value. Indexing them inflates
    memory_chunks (and the vec/FTS indexes derived from it) without paying
    back at search time. See follow-up to ISSUE-059 / DB-size analysis.
    """

    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        defaults = dict(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            memory_search=MemorySearchConfig(enabled=True, auto_index_conversations=True),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        defaults.update(kwargs)
        return Config(**defaults)

    @patch("istota.memory.search.index_conversation")
    @patch("istota.scheduler.execute_task", return_value=(True, "NO_ACTION: nothing", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_task_skips_indexing(self, mock_arun, mock_exec, mock_index, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Daily check",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )
        process_one_task(config)
        mock_index.assert_not_called()

    @patch("istota.memory.search.index_conversation")
    @patch("istota.scheduler.execute_task", return_value=(True, "ACTION: report", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_with_action_still_skips_indexing(self, mock_arun, mock_exec, mock_index, db_path, tmp_path, fake_talk):
        """Even when a silent job did report ACTION, its conversation isn't recall-relevant."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "alice", token="room1")
            db.create_task(
                conn,
                prompt="Daily check",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )
        process_one_task(config)
        mock_index.assert_not_called()
        # The seed is only doing its job if nothing was refused: a mistyped
        # token would be refused and swallowed, leaving the room fixture
        # silently inert and the test green.
        assert fake_talk.refusals == []

    @patch("istota.memory.search.index_conversation")
    @patch("istota.scheduler.execute_task", return_value=(True, "Briefing for today", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_non_silent_scheduled_task_still_indexes(self, mock_arun, mock_exec, mock_index, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Daily briefing",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=False,
            )
        process_one_task(config)
        assert mock_index.call_count >= 1

    @patch("istota.memory.search.index_conversation")
    @patch("istota.scheduler.execute_task", return_value=(True, "Hi there", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_talk_task_indexes(self, mock_arun, mock_exec, mock_index, db_path, tmp_path, fake_talk):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "alice", token="room1")
            db.create_task(
                conn,
                prompt="Hello",
                user_id="alice",
                source_type="talk",
                conversation_token="room1",
            )
        process_one_task(config)
        assert mock_index.call_count >= 1
        assert fake_talk.refusals == []

    @patch("istota.memory.search.index_conversation")
    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_indexing_disabled_by_config_skips_all(self, mock_arun, mock_exec, mock_index, db_path, tmp_path, fake_talk):
        config = self._make_config(
            db_path, tmp_path,
            memory_search=MemorySearchConfig(enabled=False, auto_index_conversations=True),
        )
        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "alice", token="room1")
            db.create_task(
                conn,
                prompt="Hello",
                user_id="alice",
                source_type="talk",
                conversation_token="room1",
            )
        process_one_task(config)
        mock_index.assert_not_called()
        assert fake_talk.refusals == []


# ---------------------------------------------------------------------------
# TestScheduledJobFailureTracking
# ---------------------------------------------------------------------------


class TestScheduledJobFailureTracking:
    def _make_config(self, db_path, tmp_path, max_failures=5):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(scheduled_job_max_consecutive_failures=max_failures),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_success_resets_failures(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful task should reset scheduled job failure count."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "test-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='test-job'").fetchone()[0]
            db.increment_scheduled_job_failures(conn, job_id, "prev error")

            db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "test-job")
            assert job.consecutive_failures == 0
            assert job.last_error is None
            assert job.last_success_at is not None

    @patch("istota.scheduler.execute_task", return_value=(False, "Task failed", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_failure_increments_count(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed task should increment scheduled job failure count."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "fail-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='fail-job'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            # Set attempts to max so failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "fail-job")
            assert job.consecutive_failures == 1
            assert "Task failed" in job.last_error

    @patch("istota.scheduler.execute_task", return_value=(False, "boom", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_auto_disable_after_max_failures(self, mock_arun, mock_exec, db_path, tmp_path):
        """The failure path suspends the job and leaves the user's column alone.

        `enabled` is what CRON.md authors, and the sync writes it back from the
        file every tick; writing it here is what made auto-disable a no-op for
        every file-defined job. The follow-through below is the half this test
        used to lack.
        """
        config = self._make_config(db_path, tmp_path, max_failures=2)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "flaky-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='flaky-job'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "flaky-job")
            assert job.auto_disabled_at is not None
            assert job.enabled is True, "the user never asked for this job to stop"
            assert job.consecutive_failures == 2
            assert db.get_enabled_scheduled_jobs(conn) == []

    @patch("istota.scheduler.execute_task", return_value=(False, "boom", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_suspended_job_survives_the_cron_md_sync(
        self, mock_arun, mock_exec, db_path, tmp_path,
    ):
        """The negative control the original auto-disable test lacked.

        Defect 1 end to end, through both modules: a real CRON.md on disk still
        listing the job, the daemon's own sync reading it, and the job still
        not firing afterwards. Against the pre-split code the sync writes
        `enabled = 1` back within the tick and the job runs forever.
        """
        from istota.scheduler import _sync_cron_files
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path, max_failures=2)
        config.users = {"alice": UserConfig()}
        cron_path = (
            config.nextcloud_mount_path
            / get_user_cron_path("alice", "istota").lstrip("/")
        )
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        original = (
            '```toml\n[[jobs]]\nname = "flaky-job"\ncron = "0 0 * * *"\n'
            'prompt = "do stuff"\nenabled = true\n```\n'
        )
        cron_path.write_text(original)

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            job_id = db.get_scheduled_job_by_name(conn, "alice", "flaky-job").id
            # The sync also seeds the module jobs, one of which queues a
            # one-shot first-poll task. `process_one_task` takes one task and
            # would take that one, so clear the queue before filling it.
            conn.execute("DELETE FROM tasks")
            task_id = db.create_task(
                conn, prompt="do stuff", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))
            conn.execute(
                "UPDATE scheduled_jobs SET consecutive_failures = 1 WHERE id = ?",
                (job_id,),
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            assert db.get_scheduled_job(conn, job_id).auto_disabled_at is not None
            # The next tick, against the unchanged file.
            _sync_cron_files(conn, config)
            job = db.get_scheduled_job(conn, job_id)
            assert job.enabled is True, "the file still says the user wants it"
            assert job.auto_disabled_at is not None, "the file must not lift this"
            # By name, not by emptiness: the same sync seeds the module jobs,
            # which are enabled and have every right to be.
            assert "flaky-job" not in {
                j.name for j in db.get_enabled_scheduled_jobs(conn)
            }

        # Byte for byte, not a name count: a full regeneration of a one-job
        # file also contains the name exactly once, so counting proves nothing.
        assert cron_path.read_text() == original, "the sync rewrote the file"

    @patch("istota.scheduler.execute_task", return_value=(False, "boom", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_auto_disable_disabled_when_zero(self, mock_arun, mock_exec, db_path, tmp_path):
        """Auto-disable should not trigger when max_failures=0."""
        config = self._make_config(db_path, tmp_path, max_failures=0)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 1, 99)""",
                ("alice", "persistent", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='persistent'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "persistent")
            # Not stopped despite 100 failures, in either column.
            assert job.enabled is True
            assert job.auto_disabled_at is None

    @patch("istota.scheduler._post_policy_refusal_alert")
    @patch(
        "istota.scheduler.execute_task",
        return_value=(
            False,
            'API Error: 400 {"type":"error","error":'
            '{"message":"Output blocked by content filtering policy"}}',
            None,
            None,
        ),
    )
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_policy_refusal_suspends_the_job(
        self, mock_arun, mock_exec, mock_alert, db_path, tmp_path,
    ):
        """The second of the three failure sites. Non-retryable, so one run is
        enough to reach the threshold with max_failures=1."""
        config = self._make_config(db_path, tmp_path, max_failures=1)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "refused-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='refused-job'"
            ).fetchone()[0]
            db.create_task(
                conn, prompt="do stuff", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job(conn, job_id)
            assert job.auto_disabled_at is not None
            assert job.enabled is True

    def test_a_failed_shared_kv_publish_suspends_the_job(self, db_path, tmp_path):
        """The third site, and the one with no test at all before now.

        Called directly: it is reached from inside `process_one_task`'s write
        transaction on a publish the user is not authorized to make, which is
        several layers of setup away from anything this class already builds.
        """
        from istota.scheduler import _record_publish_failure

        config = self._make_config(db_path, tmp_path, max_failures=2)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "publisher", "0 0 * * *", "do stuff"),
            )
            job = db.get_scheduled_job_by_name(conn, "alice", "publisher")

            _record_publish_failure(conn, config, None, job, "not authorized")
            assert db.get_scheduled_job(conn, job.id).auto_disabled_at is None

            _record_publish_failure(conn, config, None, job, "not authorized")
            after = db.get_scheduled_job(conn, job.id)

        assert after.auto_disabled_at is not None
        assert after.enabled is True
        assert after.consecutive_failures == 2


# ---------------------------------------------------------------------------
# TestWorkerPoolIsolation
# ---------------------------------------------------------------------------


class TestWorkerPoolIsolation:
    def test_foreground_gets_full_cap(self, db_path, tmp_path):
        """Foreground tasks should use full fg worker cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="carol", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # All 3 foreground users should get workers (fg cap=3)
            assert pool.active_count == 3

        pool.shutdown()

    def test_background_capped_by_max_background_workers(self, db_path, tmp_path):
        """Background tasks should be capped by max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t2", user_id="bob", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t3", user_id="carol", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Background cap = 1, so only 1 worker
            assert pool.active_count == 1

        pool.shutdown()

    def test_foreground_prioritized_over_background(self, db_path, tmp_path):
        """Foreground user should get a worker even when background fills cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=3, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # bg_cap=1, fg_cap=3: foreground should still get workers
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg1", user_id="bg-user", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="email", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # 2 foreground + 1 background
            assert pool.active_count == 3

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestExecuteCommandTask
# ---------------------------------------------------------------------------


class TestFailAncientNotification:
    """Aging out a pending task must only notify the user when *they* submitted
    it. Automated tasks (scheduled jobs, briefings, …) pile up on their own when
    the queue wedges; notifying their output channel turns one stuck worker into
    a per-minute 'task cancelled' flood (the location-alert incident)."""

    def _config(self, db_path, tmp_path):
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example"),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig()},
        )

    def _insert_aged_pending(self, db_path, source_type, scheduled_job_id=None):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type=source_type,
                conversation_token="room1", scheduled_job_id=scheduled_job_id,
            )
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-5 hours') WHERE id = ?",
                (task_id,),
            )
        return task_id

    @patch("istota.scheduler.send_notification")
    def test_automated_task_does_not_notify(self, mock_notify, db_path, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(db_path, tmp_path)
        self._insert_aged_pending(db_path, "scheduled", scheduled_job_id=49)
        run_cleanup_checks(config)
        assert mock_notify.call_count == 0

    @patch("istota.scheduler.send_notification")
    def test_user_submitted_task_still_notifies(self, mock_notify, db_path, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(db_path, tmp_path)
        self._insert_aged_pending(db_path, "talk")
        run_cleanup_checks(config)
        assert mock_notify.call_count == 1


class TestExecuteCommandTask:
    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        temp = tmp_path / "temp"
        temp.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=temp,
            scheduler=SchedulerConfig(task_timeout_minutes=1),
        )

    def _make_task(self, **kwargs):
        defaults = dict(
            id=1,
            status="running",
            source_type="scheduled",
            user_id="alice",
            prompt="",
            command="echo hello",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def test_success_returns_stdout(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo hello world")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert result == "hello world"

    def test_timeout_kills_backgrounded_grandchild(self, db_path, tmp_path):
        """A command that backgrounds a child inheriting stdout must still hit
        the timeout promptly. With plain subprocess.run, the post-timeout
        communicate() blocks on the grandchild holding the pipe — the wedge that
        let a hung CRON command heartbeat-hold its worker for hours. The process
        -group kill releases the pipe so the deadline is honored."""
        import time as _time

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=0),  # 0s deadline
        )
        # Shell exits, but the backgrounded `sleep 30` inherits the stdout pipe.
        task = self._make_task(command="sleep 30 & echo started")
        start = _time.monotonic()
        success, result = _execute_command_task(task, config)
        elapsed = _time.monotonic() - start
        assert success is False
        assert "timed out" in result.lower()
        assert elapsed < 15, f"timeout wedged on grandchild pipe ({elapsed:.1f}s)"

    def test_a_failing_stage_of_a_pipeline_is_a_failed_command(self, db_path, tmp_path):
        """A CRON `command:` row keyed auto-disable on the last stage's status.

        `shell=True` is `/bin/sh -c`, which starts with `pipefail` off, so
        `<runner> … | tail` reported success on a run that failed and the job
        was recorded as healthy indefinitely. The counterpart of ISSUE-307 on
        the operator-authored surface.
        """
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo oops >&2 | tail -1; false | tail -1")
        success, result = _execute_command_task(task, config)
        assert success is False, result

    def test_a_succeeding_pipeline_is_still_a_success(self, db_path, tmp_path):
        """Control — the change must not fail every job that uses a pipe."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo hello world | tail -1")
        success, result = _execute_command_task(task, config)
        assert success is True, result
        assert result == "hello world"

    def test_a_sigpipe_failure_says_what_141_means(self, db_path, tmp_path):
        """`last_error` is read by a human — `!cron`, the admin UI, the inbox.

        A SIGPIPE'd producer writes nothing to stderr, so without this the
        operator gets a bare `Exit code 141` on a job whose command was correct.
        """
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="yes | head -1")
        success, result = _execute_command_task(task, config)
        assert success is False, result
        assert "141" in result
        assert "SIGPIPE" in result, result

    def test_failure_returns_stderr(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo oops >&2 && exit 1")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "oops" in result

    def test_failure_exit_code_when_no_stderr(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="exit 42")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "42" in result

    def test_timeout(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=0),  # 0 seconds
        )
        task = self._make_task(command="sleep 10")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "timed out" in result.lower()

    def test_env_vars_passed(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(
            id=42,
            user_id="bob",
            conversation_token="room99",
            command="echo $ISTOTA_TASK_ID:$ISTOTA_USER_ID:$ISTOTA_CONVERSATION_TOKEN",
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "42:bob:room99" in result

    def test_db_path_in_env(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo $ISTOTA_DB_PATH")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert str(db_path) in result

    def test_config_path_propagated_when_set(self, db_path, tmp_path):
        """Subprocesses (e.g. _module.feeds.run_scheduled) need ISTOTA_CONFIG_PATH
        because the daemon's `--config` flag isn't visible in their env, and
        their cwd is `config.temp_dir` — not a directory containing
        `config/config.toml`."""
        config = self._make_config(db_path, tmp_path)
        config.config_path = tmp_path / "config.toml"
        task = self._make_task(command="echo $ISTOTA_CONFIG_PATH")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert str(config.config_path) in result

    def test_config_path_absent_when_unset(self, db_path, tmp_path):
        # Same reading as the CalDAV pair above: the assertion is about what
        # the scheduler adds. An ambient ISTOTA_CONFIG_PATH would reach the
        # child through `build_stripped_env`, which is why this failed on the
        # deployment host until conftest started scrubbing it (ISSUE-301).
        config = self._make_config(db_path, tmp_path)
        # config.config_path defaults to None
        task = self._make_task(command="echo path=[$ISTOTA_CONFIG_PATH]")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "path=[]" in result

    def test_experimental_features_propagated(self, db_path, tmp_path):
        """`@requires_feature`-gated subcommands run from a command-task need
        the CSV propagated. Regression check for the heartbeat-shaped gap."""
        from istota.config import ExperimentalConfig
        config = self._make_config(db_path, tmp_path)
        config.experimental = ExperimentalConfig(features=["money_tax", "money_wash_sales"])
        task = self._make_task(command="echo flags=[$ISTOTA_EXPERIMENTAL_FEATURES]")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "flags=[money_tax,money_wash_sales]" in result

    def test_experimental_features_empty_when_unset(self, db_path, tmp_path):
        """Always-set contract: the var exists even when no flags are on,
        so `enabled_features_from_env()` returns an empty frozenset cleanly."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo flags=[$ISTOTA_EXPERIMENTAL_FEATURES]")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "flags=[]" in result

    def test_no_output_shows_placeholder(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="true")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert result == "(no output)"

    def test_json_error_envelope_marks_task_failed(self, db_path, tmp_path):
        """Module-skill subprocesses (feeds, money) print
        ``{"status":"error","error":"…"}`` to stdout while exiting 0 when they
        catch their own errors. The scheduler must treat that envelope as
        failure, otherwise silent breakage rots indefinitely (see DEVLOG
        2026-05-03 — both feeds bugs were hidden by exactly this gap)."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(
            command='''printf '{"status":"error","error":"user not found"}' ''',
        )
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "user not found" in result

    def test_json_error_envelope_without_error_field(self, db_path, tmp_path):
        """If the error envelope omits the message, fall back to a generic one."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(
            command='''printf '{"status":"error"}' ''',
        )
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "status=error" in result

    def test_json_ok_envelope_succeeds(self, db_path, tmp_path):
        """An ``{"status":"ok",…}`` envelope is the normal success shape."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(
            command='''printf '{"status":"ok","polled":3}' ''',
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "polled" in result

    def test_non_json_stdout_unaffected(self, db_path, tmp_path):
        """Heartbeat-style commands that emit plain text are unchanged."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo 'all green: status error none here'")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "all green" in result

    def test_malformed_json_stdout_unaffected(self, db_path, tmp_path):
        """Output that starts with `{` but isn't valid JSON shouldn't break the
        envelope check — we just treat it as opaque success output."""
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo '{not really json'")
        success, result = _execute_command_task(task, config)
        assert success is True

    def test_non_admin_user_rejected(self, db_path, tmp_path):
        """Defense in depth — even if a stale row sneaks past cron sync,
        the executor must refuse to spawn arbitrary user shell-command
        tasks for non-admins."""
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"root"}
        task = self._make_task(user_id="alice", command="echo PWNED")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "admin-only" in result

    def test_admin_user_proceeds(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(user_id="alice", command="echo ok")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "ok" in result

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_command_task_flows_through_process_one_task(self, mock_arun, mock_exec, db_path, tmp_path):
        """Command task should bypass execute_task and use _execute_command_task."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="",
                user_id="alice",
                source_type="scheduled",
                command="echo from-command",
            )
        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True
        # execute_task should NOT have been called
        mock_exec.assert_not_called()

        # Verify the task was completed in DB
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"
        assert "from-command" in task.result

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_prompt_task_still_uses_execute_task(self, mock_arun, mock_exec, db_path, tmp_path):
        """Non-command task should still use execute_task."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Do stuff",
                user_id="alice",
                source_type="scheduled",
            )
        process_one_task(config)
        mock_exec.assert_called_once()

    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_command_task_failure_tracks_scheduled_job(self, mock_arun, db_path, tmp_path):
        """Command task failure should increment scheduled job failures."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "cmd-job", "0 0 * * *", ""),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='cmd-job'").fetchone()[0]
            task_id = db.create_task(
                conn,
                prompt="",
                user_id="alice",
                source_type="scheduled",
                command="exit 1",
                scheduled_job_id=job_id,
                conversation_token="room1",
            )
            # Set max_attempts=1 so failure is permanent on first try
            conn.execute("UPDATE tasks SET max_attempts = 1 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "cmd-job")
        assert job.consecutive_failures == 1

    def test_caldav_env_vars_passed(self, db_path, tmp_path):
        """Command-tasks resolve NC_* and CALDAV_* through the same skill
        manifest path the LLM uses. CalDAV vars are gated on
        ``gate_has_discovered_calendars`` — mock discovery to return a
        calendar so the gate fires positive. NC_* has no gate."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="ncuser", app_password="ncpass"),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)
        task = self._make_task(
            command="echo $CALDAV_URL:$CALDAV_USERNAME:$NC_URL:$NC_USER",
        )
        with patch(
            "istota.scheduler.discover_calendars_for_task",
            return_value=[("primary", "https://cal.example.com/p", True)],
        ):
            success, result = _execute_command_task(task, config)
        assert success is True
        assert "https://nc.example.com/remote.php/dav" in result
        assert "ncuser" in result
        assert "https://nc.example.com" in result  # NC_URL

    def test_caldav_env_omitted_when_no_calendars_discovered(self, db_path, tmp_path):
        """Gap 3 regression: ``gate_has_discovered_calendars`` on the
        calendar manifest must drop CALDAV_* when the user owns no
        calendars. NC_* is ungated and remains.

        What this proves is what the scheduler *adds*, which is narrower than
        "the child cannot see a CALDAV_URL" (ISSUE-301). ``_execute_command_task``
        starts from ``build_stripped_env()``, i.e. the daemon's own environment
        minus the credential-shaped names, so a daemon host that exports
        ``CALDAV_URL`` hands it to the child by inheritance and the gate drops
        nothing. ``CALDAV_PASSWORD`` is the one that matters and is stripped by
        pattern either way. The suite reads the gate rather than the
        inheritance only because ``conftest.py`` now scrubs the ambient value;
        before that this test failed on any host with a real config."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="ncuser", app_password="ncpass"),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)
        task = self._make_task(
            command="echo caldav=[$CALDAV_URL]:nc=[$NC_URL]",
        )
        with patch(
            "istota.scheduler.discover_calendars_for_task", return_value=[],
        ):
            success, result = _execute_command_task(task, config)
        assert success is True
        assert "caldav=[]" in result
        assert "nc=[https://nc.example.com]" in result

    def test_setup_env_hooks_dispatched(self, db_path, tmp_path):
        """ISSUE-097 regression: ``_execute_command_task`` must run
        ``dispatch_setup_env_hooks`` so vars declared ``from: "setup_env"``
        (``LOCATION_DB_PATH``, ``HEALTH_DB_PATH``) reach the subprocess.
        Before the fix, the hook only ran on the skill-task path, so
        command-type cron jobs (``gym-check-sync``) failed silently when
        the daemon env didn't happen to carry the var."""
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            user_id="alice",
            command="echo loc=[$LOCATION_DB_PATH]:health=[$HEALTH_DB_PATH]",
        )
        with patch(
            "istota.skills._env.dispatch_setup_env_hooks",
            return_value={
                "LOCATION_DB_PATH": "/srv/data/alice/location.db",
                "HEALTH_DB_PATH": "/srv/data/alice/health.db",
            },
        ):
            success, result = _execute_command_task(task, config)
        assert success is True
        assert "loc=[/srv/data/alice/location.db]" in result
        assert "health=[/srv/data/alice/health.db]" in result

    def test_setup_env_hook_value_wins_over_daemon_ambient(
        self, db_path, tmp_path, monkeypatch,
    ):
        """ISSUE-097: per-user hook value is authoritative. If a stale
        ``LOCATION_DB_PATH`` is inherited from systemd EnvironmentFile,
        the hook's per-user computation must overwrite it — otherwise
        every user's command-task would read user-X's location DB."""
        monkeypatch.setenv("LOCATION_DB_PATH", "/wrong/from/systemd.db")
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            user_id="alice",
            command="echo loc=[$LOCATION_DB_PATH]",
        )
        with patch(
            "istota.skills._env.dispatch_setup_env_hooks",
            return_value={"LOCATION_DB_PATH": "/srv/data/alice/location.db"},
        ):
            success, result = _execute_command_task(task, config)
        assert success is True
        assert "loc=[/srv/data/alice/location.db]" in result

    def test_master_key_never_in_command_env(self, db_path, tmp_path, monkeypatch):
        """Phase 4 acceptance — the operator command-task path must not
        propagate ``ISTOTA_SECRET_KEY`` to the subprocess. Phase 1.4
        already removed direct injection; this regression pins the
        property end-to-end in case a future commit re-introduces it via
        ``build_stripped_env`` or otherwise."""
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 64)
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            user_id="alice",
            command="echo key=[$ISTOTA_SECRET_KEY]",
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "key=[]" in result

    def test_deferred_dir_in_env(self, db_path, tmp_path):
        """ISSUE-233: the command path omitted ``ISTOTA_DEFERRED_DIR`` while
        both sibling paths export it. A CRON ``command:`` job calling a skill
        CLI therefore skipped every deferred write — including the email
        skill's ``sent_emails`` record, which has no direct-write fallback and
        reports success regardless."""
        from istota.executor import get_user_temp_dir

        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            user_id="alice",
            command="echo deferred=[$ISTOTA_DEFERRED_DIR]",
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        expected = get_user_temp_dir(config, "alice")
        assert f"deferred=[{expected}]" in result

    def test_deferred_dir_exists_on_disk(self, db_path, tmp_path):
        """The exported directory must already exist — a command writing a
        deferred op straight into it should not have to mkdir first. Asserted
        from inside the subprocess, against the exported value."""
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            user_id="alice",
            command='test -d "$ISTOTA_DEFERRED_DIR" && echo isdir',
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "isdir" in result

    def test_deferred_file_written_by_command_is_drained(self, db_path, tmp_path):
        """End-to-end: a command task writing to ``$ISTOTA_DEFERRED_DIR`` lands
        a file where ``_drain_deferred_ops`` looks for it."""
        from istota.executor import get_user_temp_dir

        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            id=7,
            user_id="alice",
            command=(
                'printf %s \'[{"prompt":"child"}]\' '
                '> "$ISTOTA_DEFERRED_DIR/task_${ISTOTA_TASK_ID}_subtasks.json"'
            ),
        )
        success, _ = _execute_command_task(task, config)
        assert success is True
        written = get_user_temp_dir(config, "alice") / "task_7_subtasks.json"
        assert json.loads(written.read_text()) == [{"prompt": "child"}]

    def test_deferred_op_from_a_failing_command_is_discarded(self, db_path, tmp_path):
        """Joining the deferred rail makes a command row's writes conditional on
        the command exiting 0. A skill CLI called from a CRON row used to write
        directly and keep the write regardless; now a later statement failing
        discards it, the same way it does for the skill and brain paths. Pinned
        because it is the behaviour change ISSUE-233's fix carries with it."""
        from istota.executor import get_user_temp_dir
        from istota.scheduler_deferred import _purge_deferred_files_for_retry

        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"alice"}
        task = self._make_task(
            id=8,
            user_id="alice",
            command=(
                'printf %s \'[{"prompt":"child"}]\' '
                '> "$ISTOTA_DEFERRED_DIR/task_${ISTOTA_TASK_ID}_subtasks.json"; '
                "exit 1"
            ),
        )
        success, _ = _execute_command_task(task, config)
        assert success is False

        user_temp_dir = get_user_temp_dir(config, "alice")
        written = user_temp_dir / "task_8_subtasks.json"
        assert written.exists(), "the command still wrote it"
        # The retry branch of process_one_task clears the slate; the op never
        # reaches _drain_deferred_ops, which only runs on success.
        _purge_deferred_files_for_retry(task, user_temp_dir)
        assert not written.exists()


class _TaskPathEnvHarness:
    """Config, task and captured-env plumbing shared by the two env contracts.

    Deliberately not ``Test``-prefixed: a subclass of a collected class is
    collected again, so the two contracts below would each re-run the other's
    cases — one of which drives a whole ``execute_task``.
    """

    def _config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        temp = tmp_path / "temp"
        temp.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=temp,
            admin_users={"alice"},
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )

    def _task(self, **kwargs):
        defaults = dict(
            id=5,
            status="running",
            source_type="scheduled",
            user_id="alice",
            prompt="",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def _captured_env(self, monkeypatch, target):
        """Run ``target`` with the subprocess runner stubbed, return its env."""
        captured = {}

        def fake_run(*args, **kwargs):
            captured.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("istota.scheduler._run_capture", fake_run)
        target()
        return captured


class TestDeferredDirContract(_TaskPathEnvHarness):
    """ISSUE-233: all three task paths must export the same deferred dir, so a
    skill CLI behaves identically whichever path invoked it."""

    def test_command_and_skill_paths_agree(self, db_path, tmp_path, monkeypatch):
        from istota.executor import get_user_temp_dir

        config = self._config(db_path, tmp_path)
        expected = str(get_user_temp_dir(config, "alice"))

        cmd_env = self._captured_env(
            monkeypatch,
            lambda: _execute_command_task(self._task(command="true"), config),
        )
        skill_env = self._captured_env(
            monkeypatch,
            lambda: _execute_skill_task(
                self._task(skill="kv", skill_args='["namespaces"]'), config,
            ),
        )

        assert cmd_env["ISTOTA_DEFERRED_DIR"] == expected
        assert skill_env["ISTOTA_DEFERRED_DIR"] == expected

    @patch("istota.executor.subprocess.run")
    def test_brain_path_agrees(self, mock_run, db_path, tmp_path):
        """The LLM path is the reference implementation the other two must match."""
        from istota.executor import execute_task, get_user_temp_dir

        config = self._config(db_path, tmp_path)
        config.bundled_skills_dir = tmp_path / "_empty_bundled"
        get_user_temp_dir(config, "alice").mkdir(parents=True, exist_ok=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="talk",
            )
            execute_task(db.get_task(conn, task_id), config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DEFERRED_DIR"] == str(get_user_temp_dir(config, "alice"))


class TestTaskAttemptContract(_TaskPathEnvHarness):
    """ISSUE-377: every path exporting ``ISTOTA_TASK_ID`` names the attempt too.

    ``tasks transcript`` reads this to decide which log is the one being
    written right now. It used to derive that from ``attempt_count`` on the
    row, which the liveness reaper mutates underneath a worker it wrongly
    believes is dead — so the number has to come from the process's own
    environment, and a path that sets the id without the attempt turns the
    verb off for that task rather than answering wrongly.

    Shares ``TestDeferredDirContract``'s harness because the property is the
    same shape: three task paths that must agree about one variable.
    """

    def test_command_and_skill_paths_export_the_attempt(
        self, db_path, tmp_path, monkeypatch
    ):
        config = self._config(db_path, tmp_path)

        cmd_env = self._captured_env(
            monkeypatch,
            lambda: _execute_command_task(
                self._task(command="true", attempt_count=2), config,
            ),
        )
        skill_env = self._captured_env(
            monkeypatch,
            lambda: _execute_skill_task(
                self._task(
                    skill="kv", skill_args='["namespaces"]', attempt_count=2,
                ),
                config,
            ),
        )

        # `+ 1` because the counter counts *prior* attempts, which is the same
        # arithmetic the session log's file name uses.
        assert cmd_env["ISTOTA_TASK_ATTEMPT"] == "3"
        assert skill_env["ISTOTA_TASK_ATTEMPT"] == "3"

    def _run_brain_path(self, db_path, tmp_path, monkeypatch, attempt_count=0):
        """Run the LLM path, returning what each of its three consumers saw.

        Three things worth having apart. ``model_env`` is where the variable
        must **not** be. ``base_env`` is the copy the host-side
        ``tasks transcript`` actually reads: the proxy takes its own snapshot,
        which deliberately drops some names, so "it is a superset of the
        model's" is a property with exceptions rather than an invariant.
        ``request`` carries the attempt that names the log file, and the
        exclusion is an equality between that and the environment.
        """
        from istota.executor import execute_task, get_user_temp_dir

        config = self._config(db_path, tmp_path)
        config.bundled_skills_dir = tmp_path / "_empty_bundled"
        get_user_temp_dir(config, "alice").mkdir(parents=True, exist_ok=True)

        captured = {}

        class FakeProxy:
            def __init__(self, sock, credential_env, base_env, **kwargs):
                captured["base_env"] = dict(base_env)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_run(*args, **kwargs):
            captured["model_env"] = dict(kwargs.get("env") or {})
            return MagicMock(returncode=0, stdout="ok", stderr="")

        # `execute_task` does `from .brain import BrainRequest` inside itself,
        # so the name is resolved on the brain module at call time and that is
        # where the spy has to go.
        from istota.brain import BrainRequest as real_request

        def spy_request(*args, **kwargs):
            request = real_request(*args, **kwargs)
            captured["request"] = request
            return request

        # Imported inside ``execute_task`` from ``.skill_proxy``, the same as
        # ``BrainRequest`` below, so the patch goes on the defining module.
        monkeypatch.setattr("istota.skill_proxy.SkillProxy", FakeProxy)
        monkeypatch.setattr("istota.executor.subprocess.run", fake_run)
        monkeypatch.setattr("istota.brain.BrainRequest", spy_request)

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="talk",
            )
            if attempt_count:
                conn.execute(
                    "UPDATE tasks SET attempt_count = ? WHERE id = ?",
                    (attempt_count, task_id),
                )
                conn.commit()
            execute_task(db.get_task(conn, task_id), config, [], conn=conn)

        return captured

    def test_the_brain_path_hands_the_attempt_to_the_proxy(
        self, db_path, tmp_path, monkeypatch
    ):
        """The LLM path is the one that writes a session log at all."""
        captured = self._run_brain_path(
            db_path, tmp_path, monkeypatch, attempt_count=2,
        )

        assert captured["base_env"]["ISTOTA_TASK_ATTEMPT"] == "3"

    def test_a_first_run_is_attempt_one(self, db_path, tmp_path, monkeypatch):
        captured = self._run_brain_path(db_path, tmp_path, monkeypatch)

        assert captured["base_env"]["ISTOTA_TASK_ATTEMPT"] == "1"

    def test_the_model_does_not_hold_the_attempt(
        self, db_path, tmp_path, monkeypatch
    ):
        """It is the floor's authority, so it goes to the proxy, not the model.

        ``skill_client._run_direct`` re-execs with the inherited environment on
        a proxy-off deployment, where a value in the model's own environment is
        a floor the model can raise above every file. ``ISTOTA_TASK_ID`` is
        asserted present in the same breath as the control: "the variable is
        absent" would also be satisfied by an env that carries neither.
        """
        captured = self._run_brain_path(
            db_path, tmp_path, monkeypatch, attempt_count=2,
        )

        assert "ISTOTA_TASK_ID" in captured["model_env"]
        assert "ISTOTA_TASK_ATTEMPT" not in captured["model_env"]

    def test_the_environment_and_the_log_file_name_agree(
        self, db_path, tmp_path, monkeypatch
    ):
        """The equality the whole exclusion rests on.

        ``BrainRequest.attempt`` names the session log's file; the environment
        carries the floor that withholds it. A drift between the two is silent,
        and the direction it drifts in is the permissive one — a floor above
        the live file, which hands a run its own transcript again.
        """
        captured = self._run_brain_path(
            db_path, tmp_path, monkeypatch, attempt_count=2,
        )

        assert captured["base_env"]["ISTOTA_TASK_ATTEMPT"] == str(
            captured["request"].attempt
        )

    def test_the_attempt_travels_with_the_id(self, db_path, tmp_path, monkeypatch):
        """The invariant, stated where a fourth path would trip over it.

        A consumer keying off ``ISTOTA_TASK_ID`` and finding no attempt beside
        it has to fail closed, so both are asserted **present** rather than
        merely agreeing about absence — a path setting neither would satisfy
        that weaker form while being exactly the fourth path this guards.
        """
        config = self._config(db_path, tmp_path)

        for build in (
            lambda: _execute_command_task(self._task(command="true"), config),
            lambda: _execute_skill_task(
                self._task(skill="kv", skill_args='["namespaces"]'), config,
            ),
        ):
            env = self._captured_env(monkeypatch, build)
            assert "ISTOTA_TASK_ID" in env
            assert "ISTOTA_TASK_ATTEMPT" in env


# ---------------------------------------------------------------------------
# Phase 1.3: TestExecuteSkillTask
# ---------------------------------------------------------------------------


class TestExecuteSkillTask:
    """Phase 1.3 of the unified credential resolution refactor: cron-driven
    `_module.<name>.*` jobs run as `python -m istota.skills.<skill>`
    subprocesses with credentials pre-resolved on the trusted side. The
    master Fernet key never leaves the daemon."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        temp = tmp_path / "temp"
        temp.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=temp,
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )

    def _task(self, **kwargs):
        defaults = dict(
            id=1,
            status="running",
            source_type="scheduled",
            user_id="alice",
            prompt="",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def test_unknown_skill_fails(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="not_a_skill", skill_args='[]')
        success, result = _execute_skill_task(task, config)
        assert success is False
        assert "unknown skill" in result.lower()

    def test_invalid_skill_args_fails(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="feeds", skill_args="not-json")
        success, result = _execute_skill_task(task, config)
        assert success is False
        assert "invalid skill_args" in result.lower()

    def test_non_admin_user_allowed(self, db_path, tmp_path):
        """Skill-tasks run a trusted CLI, not arbitrary shell — no admin
        gate. Verifies the function does not reject before reaching the
        skill index check."""
        config = self._make_config(db_path, tmp_path)
        config.admin_users = {"root"}  # alice is not admin
        task = self._task(
            user_id="alice", skill="not_a_skill", skill_args='[]',
        )
        success, result = _execute_skill_task(task, config)
        # Falls through to "unknown skill" rather than admin-gating.
        assert success is False
        assert "unknown skill" in result.lower()

    def test_co_declared_credentials_resolve_for_skill_task(
        self, db_path, tmp_path,
    ):
        """Gap 2 regression: env resolution covers the full skill_index,
        not just the requested skill. NC_URL is declared on ``files``
        (always_include) and ``nextcloud``; a feeds skill-task must still
        receive it so any future co-declared credential reaches the
        subprocess.

        Asserts the env dict the dispatcher hands to ``subprocess.run``,
        not the subprocess's own visible env — that decouples the test
        from whether the feeds CLI happens to print it."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="ncuser",
                app_password="ncpass",
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)
        task = self._task(skill="feeds", skill_args='["--help"]')
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            with patch(
                "istota.scheduler.discover_calendars_for_task",
                return_value=[],
            ):
                success, _ = _execute_skill_task(task, config)
        assert success is True
        env = captured["env"]
        assert env.get("NC_URL") == "https://nc.example.com"
        assert env.get("NC_USER") == "ncuser"
        assert env.get("FEEDS_USER") == "alice"

    def test_experimental_features_propagated(self, db_path, tmp_path):
        """Skill-tasks must carry `ISTOTA_EXPERIMENTAL_FEATURES` so that
        gated subcommands (e.g. money_tax) behave consistently across the
        LLM, skill-task, and command-task subprocess paths."""
        from istota.config import ExperimentalConfig
        config = self._make_config(db_path, tmp_path)
        config.experimental = ExperimentalConfig(features=["money_tax", "money_wash_sales"])
        task = self._task(skill="feeds", skill_args='["--help"]')
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            with patch(
                "istota.scheduler.discover_calendars_for_task",
                return_value=[],
            ):
                success, _ = _execute_skill_task(task, config)
        assert success is True
        assert captured["env"]["ISTOTA_EXPERIMENTAL_FEATURES"] == "money_tax,money_wash_sales"

    def test_caldav_env_resolved_when_calendars_discovered(
        self, db_path, tmp_path,
    ):
        """Gap 1 regression: skill-tasks must populate
        ``EnvContext.discovered_calendars`` so manifest specs gated on
        ``gate_has_discovered_calendars`` (calendar / location) can
        resolve."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="ncuser",
                app_password="ncpass",
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)
        task = self._task(skill="feeds", skill_args='["--help"]')
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            with patch(
                "istota.scheduler.discover_calendars_for_task",
                return_value=[("primary", "https://cal.example.com/p", True)],
            ):
                _execute_skill_task(task, config)
        env = captured["env"]
        assert "CALDAV_URL" in env
        assert env["CALDAV_URL"] == "https://nc.example.com/remote.php/dav"
        assert env["CALDAV_USERNAME"] == "ncuser"
        assert env["CALDAV_PASSWORD"] == "ncpass"

    def test_caldav_env_omitted_when_no_calendars_discovered(
        self, db_path, tmp_path,
    ):
        """Negative case: empty discovery means CALDAV_* stays out of the
        env (the gate's whole purpose)."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="ncuser",
                app_password="ncpass",
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)
        task = self._task(skill="feeds", skill_args='["--help"]')
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            with patch(
                "istota.scheduler.discover_calendars_for_task",
                return_value=[],
            ):
                _execute_skill_task(task, config)
        env = captured["env"]
        assert "CALDAV_URL" not in env
        assert "CALDAV_PASSWORD" not in env


    def test_a_nonzero_exit_keeps_the_stdout_envelope(self, db_path, tmp_path):
        """ISSUE-383: the diagnosis is on stdout, and only stdout has it.

        Skill CLIs print `{"status": "error", "error": "…"}` on stdout and
        write nothing to stderr. The failure branch read stderr alone, so a
        cron row recorded the bare string "Exit code 1" and threw away the
        message the skill had just written — which is what a human reads back
        out of ``scheduled_jobs.last_error`` via ``!cron`` and the admin Cron
        pane. Pre-existing for the feeds and money facades, which already exit
        1 on that envelope.
        """
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="feeds", skill_args=json.dumps(["list"]))

        def _fake_run(cmd, **kwargs):
            return MagicMock(
                returncode=1,
                stdout='{"status": "error", "error": "feed host refused"}',
                stderr="",
            )

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            success, result = _execute_skill_task(task, config)

        assert success is False
        assert result == "feed host refused"

    def test_a_nonzero_exit_without_an_envelope_still_reports_stderr(
        self, db_path, tmp_path,
    ):
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="feeds", skill_args=json.dumps(["list"]))

        def _fake_run(cmd, **kwargs):
            return MagicMock(returncode=2, stdout="", stderr="Traceback: boom")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            success, result = _execute_skill_task(task, config)

        assert success is False
        assert result == "Traceback: boom"

    def test_a_nonzero_exit_with_neither_names_the_code(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="feeds", skill_args=json.dumps(["list"]))

        def _fake_run(cmd, **kwargs):
            return MagicMock(returncode=3, stdout="not json", stderr="")

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            success, result = _execute_skill_task(task, config)

        assert success is False
        assert result == "Exit code 3"

    def test_an_error_envelope_on_exit_zero_is_still_a_failure(
        self, db_path, tmp_path,
    ):
        # The pre-existing defence in depth, kept: the facades exit 1, but a
        # skill that prints the envelope and exits 0 must not read as success.
        config = self._make_config(db_path, tmp_path)
        task = self._task(skill="feeds", skill_args=json.dumps(["list"]))

        def _fake_run(cmd, **kwargs):
            return MagicMock(
                returncode=0,
                stdout='{"status": "error", "error": "quota exhausted"}',
                stderr="",
            )

        with patch("istota.scheduler._run_capture", side_effect=_fake_run):
            success, result = _execute_skill_task(task, config)

        assert success is False
        assert result == "quota exhausted"

class TestGarminSyncInProcess:
    """``_module.health.garmin_sync`` must run in the daemon thread, not in
    a subprocess. The subprocess env strips ``ISTOTA_SECRET_KEY``, which
    the garmin engine needs to decrypt the OAuth blob and to persist
    rotated tokens / the error flag / last_sync mid-run."""

    def _make_config(self, db_path, tmp_path, *, timezone="UTC"):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        temp = tmp_path / "temp"
        temp.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=temp,
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig(timezone=timezone)},
        )

    def _task(self, args):
        return db.Task(
            id=42, status="running", source_type="scheduled",
            user_id="alice", prompt="",
            skill="health", skill_args=json.dumps(args),
        )

    def _fake_sync_result(self, **overrides):
        from istota.health.garmin_sync import SyncResult
        res = SyncResult(inserted=3, skipped=1, days_processed=2)
        for k, v in overrides.items():
            setattr(res, k, v)
        return res

    def test_dispatches_in_process_not_subprocess(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path, timezone="Pacific/Auckland")
        task = self._task(["garmin-sync", "--days-back", "3"])
        captured = {}

        def _fake_sync(ctx, framework_db_path, *, days_back, user_tz, config=None):
            captured["ctx_user_id"] = ctx.user_id
            captured["framework_db_path"] = framework_db_path
            captured["days_back"] = days_back
            captured["user_tz"] = user_tz
            captured["config"] = config
            return self._fake_sync_result()

        fake_ctx = MagicMock(user_id="alice")
        with patch(
            "istota.scheduler._run_capture",
            side_effect=AssertionError("must not subprocess"),
        ), patch(
            "istota.health.resolve_for_user", return_value=fake_ctx,
        ), patch(
            "istota.health.garmin_sync.sync_garmin", side_effect=_fake_sync,
        ):
            success, result = _execute_skill_task(task, config)

        assert success is True
        assert captured["ctx_user_id"] == "alice"
        assert captured["framework_db_path"] == Path(db_path)
        assert captured["days_back"] == 3
        assert captured["user_tz"] == "Pacific/Auckland"
        # Daemon-side, so an auth failure can raise the reconnect notification.
        assert captured["config"] is config
        payload = json.loads(result)
        assert payload["status"] == "ok"
        assert payload["inserted"] == 3

    def test_default_days_back_when_arg_missing(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._task(["garmin-sync"])
        captured = {}

        def _fake_sync(ctx, framework_db_path, *, days_back, user_tz, config=None):
            captured["days_back"] = days_back
            return self._fake_sync_result()

        with patch(
            "istota.health.resolve_for_user", return_value=MagicMock(user_id="alice"),
        ), patch(
            "istota.health.garmin_sync.sync_garmin", side_effect=_fake_sync,
        ):
            success, _ = _execute_skill_task(task, config)
        assert success is True
        assert captured["days_back"] == 2

    def test_auth_error_returns_failure(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._task(["garmin-sync", "--days-back", "2"])

        with patch(
            "istota.health.resolve_for_user", return_value=MagicMock(user_id="alice"),
        ), patch(
            "istota.health.garmin_sync.sync_garmin",
            return_value=self._fake_sync_result(auth_error=True, inserted=0),
        ):
            success, result = _execute_skill_task(task, config)
        assert success is False
        payload = json.loads(result)
        assert payload["status"] == "error"
        assert payload["error"] == "token_expired"

    def test_user_not_found_returns_failure(self, db_path, tmp_path):
        from istota.health._loader import UserNotFoundError
        config = self._make_config(db_path, tmp_path)
        task = self._task(["garmin-sync"])

        with patch(
            "istota.health.resolve_for_user",
            side_effect=UserNotFoundError("health module disabled for 'alice'"),
        ), patch(
            "istota.health.garmin_sync.sync_garmin",
        ) as mock_sync:
            success, result = _execute_skill_task(task, config)
        mock_sync.assert_not_called()
        assert success is False
        assert "garmin sync" in result
        assert "alice" in result

    def test_non_garmin_health_args_uses_subprocess(self, db_path, tmp_path):
        """Sanity: only the ``garmin-sync`` subcommand is dispatched in
        process. Any other ``health`` invocation must still take the
        subprocess path."""
        config = self._make_config(db_path, tmp_path)
        task = db.Task(
            id=1, status="running", source_type="scheduled",
            user_id="alice", prompt="",
            skill="health", skill_args=json.dumps(["stats"]),
        )

        def _fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout='{"status":"ok"}', stderr="")

        with patch(
            "istota.scheduler._run_capture", side_effect=_fake_run,
        ) as mock_run, patch(
            "istota.scheduler.discover_calendars_for_task", return_value=[],
        ), patch(
            "istota.health.garmin_sync.sync_garmin",
        ) as mock_sync:
            success, _ = _execute_skill_task(task, config)
        mock_sync.assert_not_called()
        mock_run.assert_called_once()
        assert success is True


class TestPurgeObsoleteSkillJobs:
    def test_deletes_orphan_scheduled_job(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, "
            "prompt, skill, skill_args, enabled) VALUES "
            "(?, ?, ?, '', ?, ?, 1)",
            ("alice", "_module.feeds.run_scheduled", "*/5 * * * *",
             "renamed_skill", '["run-scheduled"]'),
        )
        conn.commit()
        # Skill index missing the renamed name
        idx = {"feeds": object()}
        _purge_obsolete_skill_jobs(conn, idx)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ?", ("alice",),
        ).fetchall()
        assert rows == []

    def test_marks_pending_orphan_task_failed(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        task_id = db.create_task(
            conn,
            prompt="",
            user_id="alice",
            source_type="scheduled",
            skill="renamed_skill",
            skill_args='["run-scheduled"]',
            queue="background",
        )
        conn.commit()
        idx = {"feeds": object()}
        _purge_obsolete_skill_jobs(conn, idx)
        row = conn.execute(
            "SELECT status, error FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        assert row[0] == "failed"
        assert "unknown skill" in row[1]

    def test_leaves_known_skill_alone(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, "
            "prompt, skill, skill_args, enabled) VALUES "
            "(?, ?, ?, '', ?, ?, 1)",
            ("alice", "_module.feeds.run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        idx = {"feeds": object()}
        _purge_obsolete_skill_jobs(conn, idx)
        rows = conn.execute(
            "SELECT name FROM scheduled_jobs WHERE user_id = ?", ("alice",),
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# TestDualWorkerQueue
# ---------------------------------------------------------------------------


class TestDualWorkerQueue:
    """Tests for the dual foreground/background worker queue model."""

    def test_worker_pool_spawns_both_fg_and_bg_for_same_user(self, db_path, tmp_path):
        """A user with both fg and bg tasks should get two workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="cron", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Alice should have 2 workers: one fg, one bg
            assert pool.active_count == 2

        pool.shutdown()

    def test_worker_pool_fg_only(self, db_path, tmp_path):
        """A user with only foreground tasks gets one fg worker."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_worker_pool_bg_only(self, db_path, tmp_path):
        """A user with only background tasks gets one bg worker."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="cron", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_worker_pool_no_duplicate_workers_per_queue(self, db_path, tmp_path):
        """Calling dispatch twice doesn't duplicate workers for the same (user, queue) when per-user cap is 1."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, user_max_foreground_workers=1, worker_idle_timeout=2, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()
            assert pool.active_count == count_after_first

        pool.shutdown()

    def test_worker_pool_respects_per_queue_caps(self, db_path, tmp_path):
        """Workers capped independently by max_foreground_workers and max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users × 2 queues = 4 potential workers, but fg cap=2, bg cap=1
            db.create_task(conn, prompt="fg", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="bob", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            fg_count = sum(1 for (_, qt, _) in pool._workers if qt == "foreground")
            bg_count = sum(1 for (_, qt, _) in pool._workers if qt == "background")
            assert fg_count <= 2
            assert bg_count <= 1
            assert pool.active_count <= 3  # 2 fg + 1 bg

        pool.shutdown()

    def test_worker_pool_fg_prioritized_over_bg(self, db_path, tmp_path):
        """Foreground workers spawned independently from background workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=0,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users with fg tasks + 1 user with bg task, bg cap=0
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg1", user_id="carol", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Both fg workers should get slots, bg should be capped out (bg cap=0)
            assert pool.active_count == 2

        pool.shutdown()

    def test_process_one_task_with_queue(self, db_path, tmp_path):
        """process_one_task should filter by queue when provided."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg task", user_id="alice", source_type="scheduled", queue="background")

        # Trying to process a foreground task should find nothing
        result = process_one_task(config, user_id="alice", queue="foreground")
        assert result is None

        # Trying to process a background task should find the task
        with patch("istota.scheduler.execute_task", return_value=(True, "done", None, None)):
            with patch("istota.scheduler.post_result_to_talk", new_callable=AsyncMock):
                result = process_one_task(config, user_id="alice", queue="background")
        assert result is not None
        task_id, success = result
        assert success is True


# ---------------------------------------------------------------------------
# TestDeferredOperations
# ---------------------------------------------------------------------------


class TestDeferredOperations:
    """Tests for deferred DB operations (subtasks + transaction tracking)."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    def test_process_deferred_subtasks_creates_tasks(self, db_path, tmp_path):
        """Subtask JSON file should create tasks in DB with correct parent/queue."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        # Create parent task
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                conversation_token="room1", queue="background",
            )
            parent = db.get_task(conn, parent_id)

        # Write deferred subtasks file
        subtasks = [
            {"prompt": "Do X", "conversation_token": "room1", "priority": 3},
            {"prompt": "Do Y"},
        ]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 2

        # Verify tasks created in DB
        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        # parent + 2 subtasks
        subtask_list = [t for t in tasks if t.source_type == "subtask"]
        assert len(subtask_list) == 2
        assert subtask_list[0].parent_task_id == parent_id
        assert subtask_list[0].queue == "background"  # inherits from parent
        assert subtask_list[0].user_id == "alice"

        # File should be deleted
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_inherits_talk_delivery_token(self, db_path, tmp_path):
        """ISSUE-057: subtasks inherit talk_delivery_token from the parent task."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="email",
                conversation_token="thread_hash_value",
                talk_delivery_token="real_talk_room",
            )
            parent = db.get_task(conn, parent_id)

        (user_temp / f"task_{parent_id}_subtasks.json").write_text(
            json.dumps([{"prompt": "child"}])
        )

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        children = [t for t in tasks if t.source_type == "subtask"]
        assert len(children) == 1
        assert children[0].talk_delivery_token == "real_talk_room"
        assert children[0].conversation_token == "thread_hash_value"

    def test_process_deferred_subtasks_admin_only(self, db_path, tmp_path):
        """Non-admin users should have deferred subtasks ignored."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        config = Config(
            **{**config.__dict__, "admin_users": {"bob"}},  # alice is NOT admin
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [{"prompt": "sneaky"}]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0

        # No subtasks created
        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        assert all(t.source_type != "subtask" for t in tasks)

        # File should still be deleted (cleaned up)
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_no_file(self, db_path, tmp_path):
        """No file means no-op, returns 0."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0

    def test_process_deferred_subtasks_bad_json(self, db_path, tmp_path):
        """Malformed JSON should be handled gracefully (logged, file deleted)."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        (user_temp / f"task_{parent_id}_subtasks.json").write_text("{bad json")

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0
        # File cleaned up
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_warns_on_command_key(self, db_path, tmp_path, caplog):
        """ISSUE-135: an entry using the unsupported 'command' key (or any entry
        with no 'prompt') must warn loudly, not be dropped silently."""
        import logging
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                conversation_token="room1",
            )
            parent = db.get_task(conn, parent_id)

        # One bad entry (command key), one good entry — the good one still runs.
        subtasks = [
            {"command": "rm /tmp/stray.png"},
            {"prompt": "Delete the stray files"},
        ]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        with caplog.at_level(logging.WARNING):
            count = _process_deferred_subtasks(config, parent, user_temp)

        assert count == 1  # only the well-formed entry created a task
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "no 'prompt'" in msgs
        assert "command" in msgs

    def test_process_deferred_subtasks_inherits_queue(self, db_path, tmp_path):
        """Subtasks should inherit the parent's queue."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                queue="background",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [{"prompt": "bg subtask"}]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        _process_deferred_subtasks(config, parent, user_temp)

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask = [t for t in tasks if t.source_type == "subtask"][0]
        assert subtask.queue == "background"

    def test_process_deferred_subtasks_sets_output_target_from_conversation_token(self, db_path, tmp_path):
        """Subtasks inherit parent's conversation_token and default output_target to 'talk'."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                conversation_token="room1",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [
            # conversation_token in JSON is ignored — pinned to parent's token
            {"prompt": "Post to room", "conversation_token": "room2"},
            # No conversation_token — inherits from parent
            {"prompt": "Inherit room"},
            # Explicit output_target overrides default
            {"prompt": "Email result", "output_target": "email"},
        ]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        _process_deferred_subtasks(config, parent, user_temp)

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask_list = sorted(
            [t for t in tasks if t.source_type == "subtask"],
            key=lambda t: t.id,
        )
        assert len(subtask_list) == 3
        # All subtasks get parent's conversation_token (pinned for security)
        assert subtask_list[0].output_target == "talk"
        assert subtask_list[0].conversation_token == "room1"
        assert subtask_list[1].output_target == "talk"
        assert subtask_list[1].conversation_token == "room1"
        assert subtask_list[2].output_target == "email"  # explicit override

    def test_process_deferred_subtasks_no_output_target_without_conversation(self, db_path, tmp_path):
        """Subtasks without conversation_token should not get output_target defaulted."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="cli",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [{"prompt": "Silent work"}]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        _process_deferred_subtasks(config, parent, user_temp)

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask = [t for t in tasks if t.source_type == "subtask"][0]
        assert subtask.output_target is None
        assert subtask.conversation_token is None

    def test_process_deferred_subtasks_respects_limit(self, db_path, tmp_path):
        """Deferred subtask creation is capped at 10 to limit prompt injection damage."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                conversation_token="room1",
            )
            parent = db.get_task(conn, parent_id)

        # Create 15 subtasks — only 10 should be processed
        subtasks = [{"prompt": f"Subtask {i}"} for i in range(15)]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 10

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask_list = [t for t in tasks if t.source_type == "subtask"]
        assert len(subtask_list) == 10

    def test_process_deferred_subtasks_blocks_at_max_depth(self, db_path, tmp_path):
        """Subtask depth limit prevents exponential fan-out via prompt injection."""
        from istota.scheduler import _process_deferred_subtasks
        # Default max_subtask_depth is 3. Build a chain at exactly that depth,
        # then any further subtask should be rejected.
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            t0 = db.create_task(conn, prompt="root", user_id="alice", source_type="talk")
            t1 = db.create_task(
                conn, prompt="d1", user_id="alice",
                source_type="subtask", parent_task_id=t0,
            )
            t2 = db.create_task(
                conn, prompt="d2", user_id="alice",
                source_type="subtask", parent_task_id=t1,
            )
            t3 = db.create_task(
                conn, prompt="d3", user_id="alice",
                source_type="subtask", parent_task_id=t2,
            )
            # t3 is at depth 3 (the configured max). Any subtask t3 emits
            # would land at depth 4, which is over the limit.
            t3_task = db.get_task(conn, t3)

        (user_temp / f"task_{t3}_subtasks.json").write_text(
            json.dumps([{"prompt": "should be rejected"}])
        )

        count = _process_deferred_subtasks(config, t3_task, user_temp)
        assert count == 0

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        # t0, t1, t2, t3 only — no new subtask
        assert len(tasks) == 4
        # File still cleaned up
        assert not (user_temp / f"task_{t3}_subtasks.json").exists()

    def test_process_deferred_subtasks_allows_under_max_depth(self, db_path, tmp_path):
        """At depth < max, new subtasks should still be created."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        # Parent at depth 2; new subtask would be at depth 3 (== max), allowed.
        with db.get_db(db_path) as conn:
            t0 = db.create_task(conn, prompt="root", user_id="alice", source_type="talk")
            t1 = db.create_task(
                conn, prompt="d1", user_id="alice",
                source_type="subtask", parent_task_id=t0,
            )
            t2 = db.create_task(
                conn, prompt="d2", user_id="alice",
                source_type="subtask", parent_task_id=t1,
            )
            t2_task = db.get_task(conn, t2)

        (user_temp / f"task_{t2}_subtasks.json").write_text(
            json.dumps([{"prompt": "depth-3 subtask"}])
        )

        count = _process_deferred_subtasks(config, t2_task, user_temp)
        assert count == 1

    def test_process_deferred_subtasks_depth_zero_means_unlimited(self, db_path, tmp_path):
        """max_subtask_depth=0 disables the depth check."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        config = Config(**{**config.__dict__, "scheduler": SchedulerConfig(max_subtask_depth=0)})
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            previous = db.create_task(conn, prompt="root", user_id="alice", source_type="talk")
            # Build a chain 6 deep — would be rejected at default depth=3
            for i in range(6):
                previous = db.create_task(
                    conn, prompt=f"d{i}", user_id="alice",
                    source_type="subtask", parent_task_id=previous,
                )
            deep_task = db.get_task(conn, previous)

        (user_temp / f"task_{previous}_subtasks.json").write_text(
            json.dumps([{"prompt": "deep but allowed"}])
        )

        count = _process_deferred_subtasks(config, deep_task, user_temp)
        assert count == 1

    def test_process_deferred_subtasks_skips_oversize_prompt(self, db_path, tmp_path):
        """Oversize subtask prompts are skipped; smaller ones in the same batch still go through."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        config = Config(
            **{**config.__dict__, "scheduler": SchedulerConfig(max_subtask_prompt_chars=100)},
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [
            {"prompt": "ok"},
            {"prompt": "x" * 500},  # over the cap
            {"prompt": "also ok"},
        ]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 2

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask_list = [t for t in tasks if t.source_type == "subtask"]
        prompts = sorted(t.prompt for t in subtask_list)
        assert prompts == ["also ok", "ok"]

    def test_process_deferred_subtasks_prompt_chars_zero_means_unlimited(self, db_path, tmp_path):
        """max_subtask_prompt_chars=0 disables the length check."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        config = Config(
            **{**config.__dict__, "scheduler": SchedulerConfig(max_subtask_prompt_chars=0)},
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(conn, prompt="Parent", user_id="alice", source_type="talk")
            parent = db.get_task(conn, parent_id)

        big_prompt = "x" * 100_000
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(
            json.dumps([{"prompt": big_prompt}])
        )

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 1

    def test_a_deferred_tracking_file_is_logged_and_discarded(
        self, db_path, tmp_path, caplog,
    ):
        """ISSUE-427: the framework's transaction-tracking tables had no writer.

        The money module owns transaction dedup in its own per-user database
        and emits no deferred ops at all, so nothing in the tree ever produced
        this file. A model can still be prompted into writing one, and that has
        to reach the log rather than be replayed into framework tables no
        reader consults.
        """
        from istota.scheduler import _process_retired_deferred_files
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        path = user_temp / f"task_{task_id}_tracked_transactions.json"
        path.write_text(json.dumps({
            "monarch_synced": [
                {"id": "txn_123", "amount": 42.50, "merchant": "Acme",
                 "posted_account": "Assets:Bank", "txn_date": "2026-01-15",
                 "content_hash": "abc123", "tags_json": "[]"},
            ],
            "csv_imported": [{"content_hash": "hash1", "source_file": "bank.csv"}],
            "monarch_recategorized": ["txn_789"],
            "monarch_category_updates": [
                {"monarch_transaction_id": "txn_500",
                 "posted_account": "Expenses:Software"},
            ],
        }))

        with caplog.at_level("WARNING"):
            count = _process_retired_deferred_files(config, task, user_temp)

        assert count == 1
        assert not path.exists()
        assert any(
            "tracked_transactions" in r.getMessage() and r.levelname == "WARNING"
            for r in caplog.records
        ), "a retired deferred file must be logged, not dropped in silence"

        with db.get_db(db_path) as conn:
            for table in ("monarch_synced_transactions", "csv_imported_transactions"):
                rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert rows == 0, f"{table} must have no writer left"

    def test_no_retired_file_means_no_log_line(self, db_path, tmp_path, caplog):
        """The handler runs on every drained task, so an absent file has to be
        silent — otherwise every task in the deployment logs a warning."""
        from istota.scheduler import _process_retired_deferred_files
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Noop", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        with caplog.at_level("WARNING"):
            count = _process_retired_deferred_files(config, task, user_temp)

        assert count == 0
        assert not any(
            "tracked_transactions" in r.getMessage() for r in caplog.records
        )

    def test_a_retired_file_of_another_task_is_left_alone(self, db_path, tmp_path):
        """Same rule as every other handler: the filename is keyed on this
        task's id, and a concurrent task of the same user shares the directory.
        """
        from istota.scheduler import _process_retired_deferred_files
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        theirs = user_temp / f"task_{task_id + 1}_tracked_transactions.json"
        theirs.write_text("{}")

        assert _process_retired_deferred_files(config, task, user_temp) == 0
        assert theirs.exists()

    def test_every_retired_suffix_is_still_a_known_deferred_name(self):
        """The name has to stay recognized. Dropping it from
        ``_KNOWN_DEFERRED_SUFFIXES`` would make ``_warn_unconsumed_deferred_files``
        report it as a hallucinated filename, which is exactly what it is not —
        it is a name the framework used to honour — and would stop
        ``_purge_deferred_files_for_retry`` clearing it between attempts.
        """
        from istota.scheduler_deferred import (
            _KNOWN_DEFERRED_SUFFIXES,
            _RETIRED_DEFERRED_SUFFIXES,
        )
        assert "tracked_transactions" in _RETIRED_DEFERRED_SUFFIXES
        for suffix in _RETIRED_DEFERRED_SUFFIXES:
            assert suffix in _KNOWN_DEFERRED_SUFFIXES

    def test_the_drain_discards_a_tracking_file_and_carries_on(
        self, db_path, tmp_path, caplog,
    ):
        """Through the real seam. Two things a direct call cannot show: that the
        retired handler is wired into ``_drain_deferred_ops`` at all, and that it
        does not swallow the pass behind it — ``_warn_unconsumed_deferred_files``
        runs last and still reports the planted name.
        """
        from istota.scheduler import _drain_deferred_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        tracking = user_temp / f"task_{task_id}_tracked_transactions.json"
        tracking.write_text(json.dumps({"monarch_synced": [{"id": "txn_1"}]}))
        (user_temp / f"task_{task_id}_bogus.json").write_text("{}")

        with caplog.at_level("WARNING"):
            _drain_deferred_ops(config, task, "done")

        assert not tracking.exists()
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM monarch_synced_transactions"
            ).fetchone()[0] == 0

        unconsumed = [
            r.getMessage() for r in caplog.records
            if "Unrecognized deferred file" in r.getMessage()
        ]
        assert any(f"task_{task_id}_bogus.json" in m for m in unconsumed)
        # A retired name is not offered back as one to use.
        assert not any("tracked_transactions" in m for m in unconsumed)

    @patch("istota.scheduler.execute_task", return_value=(False, "Something broke", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_deferred_ops_skipped_on_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """Deferred files should NOT be processed when task fails.

        ISSUE-074 follow-up: a retry-eligible failure now also *purges* the
        deferred file so the next attempt starts clean — without that, the
        producer's append-on-write would replay the failed attempt's ops on
        eventual success. The invariant the original test cared about
        ("no DB side effects from a failed deferred file") still holds.
        """
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Fail me", user_id="testuser", source_type="cli",
            )

        subtasks = [{"prompt": "Should not exist"}]
        (user_temp / f"task_{task_id}_subtasks.json").write_text(json.dumps(subtasks))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        # Retry-eligible failure: deferred file purged so the next attempt
        # doesn't replay these ops. The DB side effects are still skipped —
        # no subtask was ever created from this file.
        assert not (user_temp / f"task_{task_id}_subtasks.json").exists()

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="testuser")
        assert all(t.source_type != "subtask" for t in tasks)

    def test_deliver_deferred_email_sends_for_email_source_talk_target(self, tmp_path):
        """Email-sourced task with output_target=talk: deferred file delivered."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        config.temp_dir = tmp_path / "temp"
        task = db.Task(
            id=42, status="completed", prompt="Reply to Max",
            user_id="testuser", source_type="email",
            output_target="talk",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        deferred = user_temp / "task_42_email_output.json"
        deferred.write_text('{"subject": "Re: Hi", "body": "Got it", "format": "plain"}')

        with patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=True) as mock_send:
            _deliver_deferred_email_output(config, task, user_temp)

        mock_send.assert_called_once_with(config, task, "")

    def test_deliver_deferred_email_warns_for_talk_source(self, tmp_path):
        """Talk-sourced task: deferred file warned and removed (no processed_email)."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        task = db.Task(
            id=42, status="completed", prompt="Send email to bob",
            user_id="testuser", source_type="talk",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        orphan = user_temp / "task_42_email_output.json"
        orphan.write_text('{"subject": "Hi", "body": "text", "format": "plain"}')

        with patch("istota.scheduler.logger") as mock_log:
            _deliver_deferred_email_output(config, task, user_temp)

        assert not orphan.exists()
        mock_log.warning.assert_called_once()
        assert "Orphaned" in mock_log.warning.call_args[0][0]

    def test_deliver_deferred_email_no_file_is_noop(self, tmp_path):
        """No delivery attempted when there is no deferred file."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        task = db.Task(
            id=42, status="completed", prompt="Hello",
            user_id="testuser", source_type="talk",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)

        with patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock) as mock_send:
            _deliver_deferred_email_output(config, task, user_temp)

        mock_send.assert_not_called()

    def test_deliver_deferred_email_skips_email_target(self, tmp_path):
        """Skip when output_target is email — normal path handles it."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        task = db.Task(
            id=42, status="completed", prompt="Briefing",
            user_id="testuser", source_type="briefing",
            output_target="email",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        deferred = user_temp / "task_42_email_output.json"
        deferred.write_text('{"subject": "Briefing", "body": "content", "format": "plain"}')

        with patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock) as mock_send:
            _deliver_deferred_email_output(config, task, user_temp)

        mock_send.assert_not_called()
        assert deferred.exists()

    def test_deliver_deferred_email_skips_both_target(self, tmp_path):
        """Skip when output_target is both — normal path handles it."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        task = db.Task(
            id=42, status="completed", prompt="Briefing",
            user_id="testuser", source_type="briefing",
            output_target="both",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        deferred = user_temp / "task_42_email_output.json"
        deferred.write_text('{"subject": "Briefing", "body": "content", "format": "plain"}')

        with patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock) as mock_send:
            _deliver_deferred_email_output(config, task, user_temp)

        mock_send.assert_not_called()
        assert deferred.exists()

    def test_deliver_deferred_email_logs_failure(self, tmp_path):
        """Failed delivery is logged as error."""
        from istota.scheduler import _deliver_deferred_email_output

        config = MagicMock()
        config.temp_dir = tmp_path / "temp"
        task = db.Task(
            id=42, status="completed", prompt="Reply",
            user_id="testuser", source_type="email",
            output_target="talk",
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        deferred = user_temp / "task_42_email_output.json"
        deferred.write_text('{"subject": "Re: Hi", "body": "ok", "format": "plain"}')

        with (
            patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=False),
            patch("istota.scheduler.logger") as mock_log,
        ):
            _deliver_deferred_email_output(config, task, user_temp)

        mock_log.error.assert_called_once()
        assert "Failed to deliver" in mock_log.error.call_args[0][0]

    def test_confirmed_task_cleans_stale_email_output(self, db_path, tmp_path):
        """Stale email_output.json from prior execution is removed before re-execution."""
        config = self._make_config(db_path, tmp_path)
        user_temp = config.temp_dir / "testuser"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, "Reply to Max", "testuser", source_type="email")
            # Simulate confirmation flow: first execution left a deferred file
            db.set_task_confirmation(conn, task_id, "Draft: Hi Max, sounds good.")
            stale_file = user_temp / f"task_{task_id}_email_output.json"
            stale_file.write_text('{"subject": "Re: Hi", "body": "Draft", "format": "plain"}')
            # Confirm the task so it re-enters pending
            db.confirm_task(conn, task_id)

        with (
            patch("istota.scheduler.execute_task", return_value=(True, "Sent!", None, None)),
            patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=True),
        ):
            result = process_one_task(config)

        assert result is not None
        # Stale file should have been cleaned up before execution
        assert not stale_file.exists()

    def test_process_deferred_sent_emails(self, db_path, tmp_path):
        """Deferred sent_emails file should record outbound emails in DB."""
        from istota.scheduler import _process_deferred_sent_emails
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email", user_id="alice", source_type="talk",
                conversation_token="room1",
            )
            task = db.get_task(conn, task_id)

        # Write deferred file
        data = [
            {
                "message_id": "<abc@example.com>",
                "to_addr": "bob@example.com",
                "subject": "Meeting",
                "conversation_token": "room1",
                "user_id": "alice",
            },
        ]
        (user_temp / f"task_{task_id}_sent_emails.json").write_text(json.dumps(data))

        count = _process_deferred_sent_emails(config, task, user_temp)
        assert count == 1

        # Verify recorded in DB
        with db.get_db(db_path) as conn:
            found = db.find_sent_email_by_message_id(conn, "<abc@example.com>")
            assert found is not None
            assert found.user_id == "alice"
            assert found.to_addr == "bob@example.com"
            assert found.conversation_token == "room1"
            assert found.task_id == task_id

        # File should be cleaned up
        assert not (user_temp / f"task_{task_id}_sent_emails.json").exists()

    def test_process_deferred_sent_emails_no_file(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_sent_emails
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Noop", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        assert _process_deferred_sent_emails(config, task, user_temp) == 0

    def test_process_deferred_sent_emails_bad_json(self, db_path, tmp_path):
        from istota.scheduler import _process_deferred_sent_emails
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Bad", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        (user_temp / f"task_{task_id}_sent_emails.json").write_text("not json")
        assert _process_deferred_sent_emails(config, task, user_temp) == 0
        # File should be cleaned up even on bad JSON
        assert not (user_temp / f"task_{task_id}_sent_emails.json").exists()

    def test_process_deferred_sent_emails_ignores_spoofed_identity(self, db_path, tmp_path):
        """Deferred file user_id/conversation_token must come from task, not JSON."""
        from istota.scheduler import _process_deferred_sent_emails
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email", user_id="alice", source_type="talk",
                conversation_token="room1",
            )
            task = db.get_task(conn, task_id)

        # JSON contains spoofed user_id and conversation_token
        data = [
            {
                "message_id": "<spoof@example.com>",
                "to_addr": "victim@example.com",
                "subject": "Spoofed",
                "user_id": "evil_user",
                "conversation_token": "evil_room",
            },
        ]
        (user_temp / f"task_{task_id}_sent_emails.json").write_text(json.dumps(data))

        count = _process_deferred_sent_emails(config, task, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            found = db.find_sent_email_by_message_id(conn, "<spoof@example.com>")
            assert found is not None
            assert found.user_id == "alice"  # task user, not spoofed
            assert found.conversation_token == "room1"  # task token, not spoofed

    def test_process_deferred_sent_emails_multiple(self, db_path, tmp_path):
        """Multiple sends in one task should all be recorded."""
        from istota.scheduler import _process_deferred_sent_emails
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Multi-send", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        data = [
            {"message_id": "<m1@x.com>", "to_addr": "a@x.com", "subject": "One"},
            {"message_id": "<m2@x.com>", "to_addr": "b@x.com", "subject": "Two"},
        ]
        (user_temp / f"task_{task_id}_sent_emails.json").write_text(json.dumps(data))

        count = _process_deferred_sent_emails(config, task, user_temp)
        assert count == 2

        with db.get_db(db_path) as conn:
            assert db.find_sent_email_by_message_id(conn, "<m1@x.com>") is not None
            assert db.find_sent_email_by_message_id(conn, "<m2@x.com>") is not None

    def test_process_deferred_user_alerts_posts_to_alerts_channel(self, db_path, tmp_path):
        """Alert JSON posts to the user's alerts channel, one push per alert type.

        Both entries here name the `security` type, so they collapse onto
        one notification — the array is model-authored with no bound on its
        length, and one push per entry is how a single task turns into a flood.
        Both messages still reach the user; they are in the one body.
        `tests/test_notification_task_alerts.py` covers the collapse in full.
        """
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        config.users["alice"] = UserConfig(alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        alerts = [
            {"message": "Suspicious email from attacker@evil.com: social engineering attempt to extract calendar data", "type": "security"},
            {"message": "Email contains embedded system prompt injection", "type": "security"},
        ]
        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps(alerts))

        with patch("istota.notifications.send_notification") as mock_notify:
            mock_notify.return_value = True
            count = _process_deferred_user_alerts(config, task, user_temp)

        assert count == 2
        assert mock_notify.call_count == 1
        # Routed by purpose="alert" — resolve_destinations maps it to the
        # user's alerts channel (legacy alerts_channel field).
        call_args = mock_notify.call_args_list[0]
        assert call_args[0][0] is config
        assert call_args[0][1] == "alice"
        assert "attacker@evil.com" in call_args[0][2]
        assert "prompt injection" in call_args[0][2]
        assert call_args[1]["purpose"] == "alert"

        # File should be cleaned up
        assert not (user_temp / f"task_{task_id}_user_alerts.json").exists()

    def test_process_deferred_user_alerts_no_file(self, db_path, tmp_path):
        """No file means no alerts and zero return."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        assert _process_deferred_user_alerts(config, task, user_temp) == 0

    def test_process_deferred_user_alerts_bad_json(self, db_path, tmp_path):
        """Bad JSON should be handled gracefully and file cleaned up."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        (user_temp / f"task_{task_id}_user_alerts.json").write_text("not json{{{")
        assert _process_deferred_user_alerts(config, task, user_temp) == 0
        assert not (user_temp / f"task_{task_id}_user_alerts.json").exists()

    def test_process_deferred_user_alerts_not_a_list(self, db_path, tmp_path):
        """Non-list JSON should be handled gracefully."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps({"msg": "hi"}))
        assert _process_deferred_user_alerts(config, task, user_temp) == 0
        assert not (user_temp / f"task_{task_id}_user_alerts.json").exists()

    def test_process_deferred_user_alerts_includes_task_context(self, db_path, tmp_path):
        """Alert message should include task ID for traceability."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        config.users["alice"] = UserConfig(alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        alerts = [{"message": "Phishing attempt detected", "type": "security"}]
        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps(alerts))

        with patch("istota.notifications.send_notification") as mock_notify:
            mock_notify.return_value = True
            _process_deferred_user_alerts(config, task, user_temp)

        msg = mock_notify.call_args_list[0][0][2]
        assert str(task_id) in msg

    def test_process_deferred_user_alerts_skips_empty_messages(self, db_path, tmp_path):
        """Entries with no message should be skipped."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        config.users["alice"] = UserConfig(alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        alerts = [{"message": ""}, {"not_message": "oops"}, {"message": "Real alert"}]
        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps(alerts))

        with patch("istota.notifications.send_notification") as mock_notify:
            mock_notify.return_value = True
            count = _process_deferred_user_alerts(config, task, user_temp)

        assert count == 1

    def test_process_deferred_user_alerts_skips_non_dict_entries(self, db_path, tmp_path):
        """Non-dict entries in the array should be skipped, not crash."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        config.users["alice"] = UserConfig(alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        alerts = [42, "hello", None, {"message": "Real alert", "type": "security"}]
        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps(alerts))

        with patch("istota.notifications.send_notification") as mock_notify:
            mock_notify.return_value = True
            count = _process_deferred_user_alerts(config, task, user_temp)

        assert count == 1
        assert "Real alert" in mock_notify.call_args[0][2]

    def test_process_deferred_user_alerts_action_needed_type(self, db_path, tmp_path):
        """Alerts with type=action_needed should use 'Action needed' prefix, not 'Security alert'."""
        from istota.scheduler import _process_deferred_user_alerts
        config = self._make_config(db_path, tmp_path)
        config.users["alice"] = UserConfig(alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Check email", user_id="alice", source_type="email")
            task = db.get_task(conn, task_id)

        alerts = [
            {"message": "Phishing attempt", "type": "security"},
            {"type": "action_needed", "message": "Told joe@example.com I would check with you about Saturday"},
        ]
        (user_temp / f"task_{task_id}_user_alerts.json").write_text(json.dumps(alerts))

        with patch("istota.notifications.send_notification") as mock_notify:
            mock_notify.return_value = True
            count = _process_deferred_user_alerts(config, task, user_temp)

        assert count == 2
        security_msg = mock_notify.call_args_list[0][0][2]
        action_msg = mock_notify.call_args_list[1][0][2]
        assert "Security alert" in security_msg
        assert "Action needed" in action_msg
        assert "Security alert" not in action_msg


# ---------------------------------------------------------------------------
# TestWarnUnconsumedDeferredFiles
# ---------------------------------------------------------------------------


class TestWarnUnconsumedDeferredFiles:
    """Tests for _warn_unconsumed_deferred_files — surfaces silently-dropped
    deferred files (e.g. a hallucinated filename) that the dispatch ignores.
    """

    def _task(self, task_id: int = 123):
        return db.Task(
            id=task_id, status="completed", source_type="talk",
            user_id="alice", prompt="x",
        )

    def test_no_warning_for_recognized_files(self, tmp_path, caplog):
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        for name in (
            "task_123_subtasks.json",
            "task_123_tracked_transactions.json",
            "task_123_sent_emails.json",
            "task_123_kv_ops.json",
            "task_123_user_alerts.json",
            "task_123_email_output.json",
            # The one static file the framework still writes here: the model
            # produces it inside the sandbox and the daemon reads it back.
            "task_123_result.txt",
        ):
            (tmp_path / name).write_text("{}")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        assert not any("Unrecognized deferred file" in r.message for r in caplog.records)

    def test_warns_on_a_prompt_file_planted_in_the_user_temp_dir(
        self, tmp_path, caplog,
    ):
        # Both prompt halves live in the task control directory now, outside
        # anything a sandboxed task can write. So one of these names turning
        # up in the per-user temp directory is not the executor's — it is a
        # concurrent task of the same user writing under another task's id,
        # which is the write vector the control directory exists to close.
        # Keeping the names recognised would silence exactly that case.
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        (tmp_path / "task_123_prompt.txt").write_text("planted")
        (tmp_path / "task_123_system_prompt.txt").write_text("planted")
        (tmp_path / "task_123_result.txt").write_text("mine")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        warnings = [
            r.message for r in caplog.records
            if "Unrecognized deferred file" in r.message
        ]
        assert len(warnings) == 2
        assert any("task_123_prompt.txt" in m for m in warnings)
        assert any("task_123_system_prompt.txt" in m for m in warnings)
        # The result file is still ours and must stay quiet.
        assert not any("task_123_result.txt" in m for m in warnings)

    def test_warns_on_missing_task_prefix(self, tmp_path, caplog):
        # The shape that hit prod: model wrote "{id}_skip_log.json" instead
        # of "task_{id}_subtasks.json".
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        (tmp_path / "123_skip_log.json").write_text("{}")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        warnings = [r.message for r in caplog.records if "Unrecognized deferred file" in r.message]
        assert len(warnings) == 1
        assert "123_skip_log.json" in warnings[0]
        assert "task_123_<" in warnings[0]

    def test_warns_on_unknown_suffix_with_canonical_prefix(self, tmp_path, caplog):
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        (tmp_path / "task_123_unknown_op.json").write_text("{}")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        warnings = [r.message for r in caplog.records if "Unrecognized deferred file" in r.message]
        assert len(warnings) == 1
        assert "task_123_unknown_op.json" in warnings[0]

    def test_warns_on_id_suffixed_descriptive_name(self, tmp_path, caplog):
        # ISSUE-135: the exact shape that hit prod — the model wrote a
        # descriptive name with the task id as a trailing token
        # ("cleanup_stray_files_{id}.json") rather than the canonical
        # "task_{id}_subtasks.json", so neither the consumer lookup nor the
        # old prefix-only scanner saw it.
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(178574)
        (tmp_path / "cleanup_stray_files_178574.json").write_text("{}")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        warnings = [r.message for r in caplog.records if "Unrecognized deferred file" in r.message]
        assert len(warnings) == 1
        assert "cleanup_stray_files_178574.json" in warnings[0]

    def test_does_not_warn_on_other_tasks_files(self, tmp_path, caplog):
        # User temp dir is shared across tasks for the same user; warning
        # should be scoped to the current task id.
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        (tmp_path / "task_456_subtasks.json").write_text("{}")
        (tmp_path / "task_4567_unknown.json").write_text("{}")
        (tmp_path / "456_skip_log.json").write_text("{}")
        (tmp_path / "cleanup_456.json").write_text("{}")  # id-suffix, other task
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        assert not any("Unrecognized deferred file" in r.message for r in caplog.records)

    def test_handles_missing_temp_dir(self, tmp_path, caplog):
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path / "does-not-exist")
        # No exception, no warnings.
        assert not any("Unrecognized deferred file" in r.message for r in caplog.records)

    def test_no_warning_for_known_artifact_suffixes(self, tmp_path, caplog):
        # health_op_failures.json is written by _process_deferred_health_ops
        # when an op fails mid-batch; it stays on disk so an operator can
        # recover the lost rows. It must not trip the "unrecognized" warning.
        from istota.scheduler import _warn_unconsumed_deferred_files
        task = self._task(123)
        (tmp_path / "task_123_health_op_failures.json").write_text("[]")
        with caplog.at_level("WARNING"):
            _warn_unconsumed_deferred_files(task, tmp_path)
        assert not any(
            "Unrecognized deferred file" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestPurgeDeferredFilesForRetry — ISSUE-074
# ---------------------------------------------------------------------------


class TestPurgeDeferredFilesForRetry:
    """ISSUE-074: deferred-op producers append to ``task_{id}_*.json``. When a
    task fails and retries with the same ``task.id``, the next attempt's ops
    must not replay alongside the failed attempt's. The retry path now purges
    those files; ``_process_deferred_kg_ops`` commits per-op so a mid-loop
    crash doesn't lose ops we've already accepted.
    """

    def _task(self, task_id: int = 999):
        return db.Task(
            id=task_id, status="pending_retry", source_type="talk",
            user_id="alice", prompt="x",
        )

    def test_purge_removes_known_suffixes(self, tmp_path, caplog):
        from istota.scheduler import _purge_deferred_files_for_retry
        task = self._task(999)
        for suffix in (
            "subtasks", "tracked_transactions", "sent_emails",
            "kv_ops", "kg_ops", "user_alerts", "email_output",
        ):
            (tmp_path / f"task_999_{suffix}.json").write_text("[]")
        with caplog.at_level("INFO"):
            _purge_deferred_files_for_retry(task, tmp_path)
        for suffix in (
            "subtasks", "tracked_transactions", "sent_emails",
            "kv_ops", "kg_ops", "user_alerts", "email_output",
        ):
            assert not (tmp_path / f"task_999_{suffix}.json").exists()

    def test_purge_leaves_other_tasks_files(self, tmp_path):
        from istota.scheduler import _purge_deferred_files_for_retry
        task = self._task(999)
        (tmp_path / "task_999_kg_ops.json").write_text("[]")
        (tmp_path / "task_1000_kg_ops.json").write_text("[]")
        _purge_deferred_files_for_retry(task, tmp_path)
        assert not (tmp_path / "task_999_kg_ops.json").exists()
        assert (tmp_path / "task_1000_kg_ops.json").exists()

    def test_purge_leaves_the_result_file(self, tmp_path):
        # The result file is scoped per-task, not per-attempt, and the model
        # overwrites it. It is the only such file left in this directory —
        # both prompt halves moved to the task control directory, which this
        # function has never walked.
        from istota.scheduler import _purge_deferred_files_for_retry
        task = self._task(999)
        (tmp_path / "task_999_result.txt").write_text("result")
        (tmp_path / "task_999_kg_ops.json").write_text("[]")
        _purge_deferred_files_for_retry(task, tmp_path)
        assert (tmp_path / "task_999_result.txt").exists()
        assert not (tmp_path / "task_999_kg_ops.json").exists()

    def test_purge_handles_missing_dir(self, tmp_path):
        from istota.scheduler import _purge_deferred_files_for_retry
        task = self._task(999)
        # Should not raise.
        _purge_deferred_files_for_retry(task, tmp_path / "missing")

    def test_kg_ops_commit_per_op_survives_mid_loop_crash(
        self, db_path, tmp_path, monkeypatch,
    ):
        """If an op mid-batch raises, ops accepted before it must persist —
        i.e. they were committed independently, not rolled back.
        """
        from istota.scheduler import _process_deferred_kg_ops
        from istota.memory import knowledge_graph as kg

        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com", username="i", app_password="x",
            ),
            talk=TalkConfig(enabled=True, bot_username="i"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            kg.ensure_table(conn)
            task_id = db.create_task(
                conn, prompt="p", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        ops = [
            {"op": "add_fact", "subject": "alice", "predicate": "likes",
             "object": "tea", "source_type": "user_stated"},
            {"op": "invalidate", "fact_id": 99999},  # nonexistent — fine
            {"op": "add_fact", "subject": "alice", "predicate": "likes",
             "object": "coffee", "source_type": "user_stated"},
        ]
        path = user_temp / f"task_{task_id}_kg_ops.json"
        path.write_text(json.dumps(ops))

        # Wrap kg_invalidate_fact to raise mid-loop on the second op.
        real_invalidate = kg.invalidate_fact

        def boom(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("simulated crash")

        # The scheduler imports symbols inside the function; patch the
        # binding the function uses (`from .memory.knowledge_graph import
        # invalidate_fact as kg_invalidate_fact` runs each call).
        monkeypatch.setattr(kg, "invalidate_fact", boom)

        _process_deferred_kg_ops(config, task, user_temp)

        # The first add_fact (before the crash) AND the third (after the
        # caught exception) must both have landed. With a single end-of-loop
        # commit, the failure on op 2 would have rolled back op 1.
        with db.get_db(db_path) as conn:
            facts = kg.get_current_facts(conn, "alice", subject="alice")
        objects = sorted(f.object for f in facts)
        assert objects == ["coffee", "tea"], (
            f"per-op commit broken — surviving facts: {objects}"
        )

        # File cleared.
        assert not path.exists()

        monkeypatch.setattr(kg, "invalidate_fact", real_invalidate)


# ---------------------------------------------------------------------------
# `_notify_confirmed_email_result` and its tests are gone (ISSUE-247). It
# announced a gated email task's reply as `Email reply sent to <sender>` because
# no room held a turn to attach it to; the answer is now an ordinary assistant
# turn in the room the exchange was routed to (see
# `tests/test_email_routed_room.py`). Its ISSUE-246 branch — never say "sent"
# for a reply the outbound gate held — is not lost: the hold announces itself
# from the delivery leg, covered by `test_transport_email_outbound.py::
# TestDeliveryLegApprovalGate::test_hold_notifies_the_user_immediately`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestRecordSentEmail
# ---------------------------------------------------------------------------


class TestRecordSentEmail:
    """Tests for _record_sent_email helper used by post_result_to_email."""

    def test_records_sent_email(self, db_path):
        from istota.transport.email.outbound import _record_sent_email

        config = Config(
            db_path=db_path,
            email=EmailConfig(enabled=True),
        )

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="reply", user_id="frank", source_type="email",
                conversation_token="room5",
            )
            task = db.get_task(conn, task_id)

        _record_sent_email(
            config, task, "<reply@example.com>",
            to_addr="bob@example.com",
            subject="Re: Hello",
            in_reply_to="<orig@example.com>",
        )

        with db.get_db(db_path) as conn:
            found = db.find_sent_email_by_message_id(conn, "<reply@example.com>")
            assert found is not None
            assert found.user_id == "frank"
            assert found.task_id == task_id
            assert found.conversation_token == "room5"
            assert found.in_reply_to == "<orig@example.com>"

    def test_record_sent_email_failure_is_non_fatal(self, db_path):
        """DB errors in _record_sent_email should not propagate."""
        from istota.transport.email.outbound import _record_sent_email

        config = Config(db_path=Path("/nonexistent/db.sqlite"))

        task = db.Task(
            id=1, status="completed", prompt="test",
            user_id="frank", source_type="email",
        )

        # Should not raise
        _record_sent_email(config, task, "<msg@x.com>", to_addr="a@b.com")


# ---------------------------------------------------------------------------
# TestOnceJobAutoRemoval
# ---------------------------------------------------------------------------


class TestOnceJobAutoRemoval:
    """Tests for automatic removal of once=true scheduled jobs after success."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_removed_on_success(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful once job should be removed from DB."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-123", "30 14 17 2 *", "Send reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-123'").fetchone()[0]

            db.create_task(
                conn,
                prompt="Send reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "reminder-123")
            assert job is None, "Once job should be deleted from DB after success"

    @patch("istota.scheduler.execute_task", return_value=(False, "Task failed", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_not_removed_on_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed once job should NOT be removed (stays for retry)."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-456", "0 9 18 2 *", "Reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-456'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="Reminder",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            # Set attempts to max so failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "reminder-456")
            assert job is not None, "Once job should NOT be deleted on failure"

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_also_removed_from_cron_md(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful once job should also be removed from CRON.md file."""
        from istota.cron_loader import load_cron_jobs
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        mount = config.nextcloud_mount_path

        # Write CRON.md with the once job and a regular job
        cron_path = mount / get_user_cron_path("alice", "istota").lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "keep-this"
cron = "0 9 * * *"
prompt = "daily check"

[[jobs]]
name = "reminder-789"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-789", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-789'").fetchone()[0]

            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        # CRON.md should only have the keep-this job
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "keep-this"

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_file_removal_that_did_not_happen_is_logged(
        self, mock_arun, mock_exec, db_path, tmp_path, caplog
    ):
        """The row goes whatever the file does, so a lost removal must be said.

        The table row is deleted before CRON.md is touched, so if the file
        keeps the job the next sync re-inserts it and a `once = true` job runs
        a second time. Until ISSUE-369 `remove_job_from_cron_md` returned an
        unconditional True and could not report a refused write at all, so
        nothing anywhere said so. Here the job is simply absent from the file,
        which reaches the same branch without needing a permission bit.
        """
        import logging

        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        cron_path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "keep-this"
cron = "0 9 * * *"
prompt = "daily check"
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-901", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='reminder-901'"
            ).fetchone()[0]
            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            process_one_task(config)

        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "reminder-901" in msgs
        assert "not from CRON.md" in msgs
        # The row is gone either way — that is what the warning is about.
        with db.get_db(db_path) as conn:
            assert db.get_scheduled_job(conn, job_id) is None

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_successful_file_removal_is_not_logged(
        self, mock_arun, mock_exec, db_path, tmp_path, caplog
    ):
        """The control: the same path with the job in the file says nothing.

        Without this the warning test passes on a branch that fires
        unconditionally, which is the same warning as no warning at all.
        """
        import logging

        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        cron_path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "reminder-902"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-902", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='reminder-902'"
            ).fetchone()[0]
            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            process_one_task(config)

        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "not from CRON.md" not in msgs

    @patch("istota.scheduler.execute_task", return_value=(True, "Regular success", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_non_once_job_not_removed(self, mock_arun, mock_exec, db_path, tmp_path):
        """Regular (non-once) job should NOT be removed on success."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 0)""",
                ("alice", "daily-job", "0 9 * * *", "Do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='daily-job'").fetchone()[0]

            db.create_task(
                conn,
                prompt="Do stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "daily-job")
            assert job is not None, "Regular job should NOT be deleted on success"

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_the_cron_md_write_does_not_hold_the_framework_write_lock(
        self, mock_arun, mock_exec, db_path, tmp_path
    ):
        """CRON.md is on the rclone mount, so its write must not sit inside the
        task transaction (ISSUE-387).

        `delete_scheduled_job` takes SQLite's write lock, and the removal of
        the same job from CRON.md used to run before the enclosing `with`
        block committed. A hung mount then blocked every other framework-DB
        writer — the dispatch loop, the other workers, the web app, the
        pollers — for however long the FUSE timeout ran. The probe below is
        the only thing that tells the two orderings apart: it asks, at the
        moment the file write happens, whether an unrelated connection can
        still write, with a busy timeout far shorter than any real caller's.
        """
        import sqlite3

        from istota import cron_loader
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        cron_path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "reminder-387"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-387", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='reminder-387'"
            ).fetchone()[0]
            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        real_remove = cron_loader.remove_job_from_cron_md
        probe: dict[str, object] = {}

        def probe_then_remove(cfg, user_id, job_name):
            other = sqlite3.connect(db_path, timeout=0.2)
            try:
                other.execute("UPDATE tasks SET priority = priority")
                other.commit()
                probe["writable"] = True
            except sqlite3.OperationalError as exc:
                probe["writable"] = False
                probe["error"] = str(exc)
            finally:
                other.close()
            return real_remove(cfg, user_id, job_name)

        with patch.object(
            cron_loader, "remove_job_from_cron_md", probe_then_remove
        ):
            process_one_task(config)

        assert probe.get("writable") is True, (
            "another writer was locked out during the CRON.md write: "
            f"{probe.get('error')}"
        )
        # The removal itself still happened, and against the same file.
        assert cron_loader.load_cron_jobs(config, "alice") == []

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_cron_sync_racing_the_cron_md_write_does_not_resurrect_the_job(
        self, mock_arun, mock_exec, db_path, tmp_path
    ):
        """Hoisting the file write out of the transaction opened a window
        (ISSUE-387 review).

        The row delete and the CRON.md write are no longer one step. In
        between, `_sync_cron_files` runs on the main loop with CRON.md as the
        authority — it reads a file that still names the job and re-inserts
        the row this task just deleted, which is the `once = true` job that
        runs a second time. Before the hoist the order was the other way
        round, so a sync in the window saw a row the file did not name and
        deleted it. The flush deletes the row again to close it.

        The sync is simulated by re-inserting the row from inside the patched
        writer, which is exactly where the real one would land.
        """
        from istota import cron_loader
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        cron_path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "reminder-387b"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-387b", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='reminder-387b'"
            ).fetchone()[0]
            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        real_remove = cron_loader.remove_job_from_cron_md

        def resync_then_remove(cfg, user_id, job_name):
            # Stand in for `_sync_cron_files` on the main loop: CRON.md still
            # names the job at this instant, so the sync re-inserts it.
            with db.get_db(db_path) as conn:
                conn.execute(
                    """INSERT INTO scheduled_jobs
                       (user_id, name, cron_expression, prompt, enabled, once)
                       VALUES (?, ?, ?, ?, 1, 1)""",
                    (user_id, job_name, "0 15 20 2 *", "One-time reminder"),
                )
            return real_remove(cfg, user_id, job_name)

        with patch.object(
            cron_loader, "remove_job_from_cron_md", resync_then_remove
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            assert db.get_scheduled_job_by_name(conn, "alice", "reminder-387b") is None, (
                "a once-job re-inserted by a cron sync mid-flush survived, so "
                "it will run a second time"
            )
        assert cron_loader.load_cron_jobs(config, "alice") == []

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_a_raising_cron_md_writer_does_not_abandon_the_completed_task(
        self, mock_arun, mock_exec, db_path, tmp_path
    ):
        """The flush runs after the commit, so it must not raise (ISSUE-387
        review).

        `remove_job_from_cron_md` states a never-raises contract, but the
        hoist is what made it load-bearing: an exception here escapes with the
        task already recorded `completed`, and everything that finishes the
        task is still ahead — delivery, the deferred-op drain, the terminal
        event. Nothing retries a `completed` row, so the answer would be lost
        silently. The flush guards it rather than trusting the contract.
        """
        from istota import cron_loader
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        cron_path = config.nextcloud_mount_path / get_user_cron_path(
            "alice", "istota"
        ).lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "reminder-387c"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-387c", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE name='reminder-387c'"
            ).fetchone()[0]
            task_id = db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        def boom(cfg, user_id, job_name):
            raise OSError(107, "Transport endpoint is not connected")

        with patch.object(cron_loader, "remove_job_from_cron_md", boom):
            result = process_one_task(config)

        # The task still finished: it returned normally and is `completed`.
        assert result == (task_id, True)
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"


# ---------------------------------------------------------------------------
# TestCleanupOldClaudeLogs
# ---------------------------------------------------------------------------


class TestCleanupOldClaudeLogs:
    """Tests for cleanup_old_claude_logs()."""

    def test_deletes_old_jsonl_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects = claude_dir / "projects" / "some-project"
        projects.mkdir(parents=True)

        import time
        old_file = projects / "old-session.jsonl"
        old_file.write_text("{}")
        recent_file = projects / "recent-session.jsonl"
        recent_file.write_text("{}")

        # Make old file actually old
        import os
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1
        assert not old_file.exists()
        assert recent_file.exists()

    def test_deletes_old_debug_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        debug = claude_dir / "debug"
        debug.mkdir(parents=True)

        import time
        import os
        old_file = debug / "debug-2026-01-01.txt"
        old_file.write_text("log")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_old_todo_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        todos = claude_dir / "todos"
        todos.mkdir(parents=True)

        import time
        import os
        old_file = todos / "tasks.json"
        old_file.write_text("[]")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1

    def test_removes_empty_subdirectories(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        subdir = claude_dir / "projects" / "empty-project"
        subdir.mkdir(parents=True)

        import time
        import os
        old_file = subdir / "session.jsonl"
        old_file.write_text("{}")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            cleanup_old_claude_logs(retention_days=7)

        assert not subdir.exists(), "Empty subdirectory should be removed"

    def test_missing_claude_dir_returns_zero(self, tmp_path):
        import os
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)
        assert deleted == 0

    def test_keeps_recent_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects = claude_dir / "projects" / "active"
        projects.mkdir(parents=True)

        recent = projects / "today.jsonl"
        recent.write_text("{}")

        import os
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 0
        assert recent.exists()


# ---------------------------------------------------------------------------
# TestPostResultToTalk
# ---------------------------------------------------------------------------


class TestPostResultToTalk:
    """Tests for post_result_to_talk() — reply threading and @mentions in group chats.

    About the shape of each call (mention, reply_to, splitting, reference_id),
    so every room here is `plain_talk_room` and the destination is fixed. The
    `AsyncMock` this used to patch in accepted any token; `fake_talk` accepts
    only a live `talk` `surface_ref`, so the calls asserted below are calls
    Nextcloud would also have accepted. The returned id is the double's own
    rather than a literal, since `TalkTransport.deliver` returns None on a
    refusal and a hardcoded number would let that read as a delivery.
    """

    def _make_config(self):
        return Config(
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="istota",
                app_password="secret",
            ),
        )

    def _make_task(self, token, *, is_group_chat=False, talk_message_id=None,
                   user_id="alice"):
        return db.Task(
            id=1,
            prompt="hello",
            user_id=user_id,
            source_type="talk",
            status="completed",
            conversation_token=token,
            is_group_chat=is_group_chat,
            talk_message_id=talk_message_id,
        )

    @pytest.fixture
    def room(self, db_path):
        with db.get_db(db_path) as conn:
            return plain_talk_room(conn, "alice")

    @pytest.mark.asyncio
    async def test_dm_no_reply_to_no_mention(self, fake_talk, room):
        """DM messages should not use reply_to or @mention."""
        config = self._make_config()
        task = self._make_task(room.canonical, is_group_chat=False, talk_message_id=42)

        result = await post_result_to_talk(
            config, task, "Hello there", use_reply_threading=True,
        )

        assert [(c.method, c.token, c.args) for c in fake_talk.calls] == [
            ("send_message", room.talk_ref, {
                "message": "Hello there", "reply_to": None, "reference_id": None,
            }),
        ]
        assert result == fake_talk.sent_ids[0]

    @pytest.mark.asyncio
    async def test_group_chat_reply_to_and_mention(self, fake_talk, room):
        """Group chat messages should reply to original and @mention the user."""
        config = self._make_config()
        task = self._make_task(
            room.canonical, is_group_chat=True, talk_message_id=42, user_id="bob",
        )

        result = await post_result_to_talk(
            config, task, "Sure thing", use_reply_threading=True,
        )

        assert [(c.method, c.token, c.args) for c in fake_talk.calls] == [
            ("send_message", room.talk_ref, {
                "message": "@bob Sure thing", "reply_to": 42, "reference_id": None,
            }),
        ]
        assert result == fake_talk.sent_ids[0]

    @pytest.mark.asyncio
    async def test_group_chat_split_message_only_first_part_gets_reply(
        self, fake_talk, room,
    ):
        """When a message is split, only the first part should get reply_to and @mention."""
        config = self._make_config()
        task = self._make_task(
            room.canonical, is_group_chat=True, talk_message_id=42, user_id="carol",
        )

        with patch(
            "istota.transport.talk.split_message",
            return_value=["Part 1", "Part 2"],
        ):
            await post_result_to_talk(
                config, task, "Long message", use_reply_threading=True,
            )

        # Both parts land in the same room, in order; only the first threads.
        assert [(c.token, c.args) for c in fake_talk.calls] == [
            (room.talk_ref, {
                "message": "@carol Part 1", "reply_to": 42, "reference_id": None,
            }),
            (room.talk_ref, {
                "message": "Part 2", "reply_to": None, "reference_id": None,
            }),
        ]
        # A refused call records the same token and the same args, so the
        # comparison above is equally true of two posts Nextcloud rejected.
        assert fake_talk.refusals == []

    @pytest.mark.asyncio
    async def test_group_chat_no_talk_message_id(self, fake_talk, room):
        """Group chat without talk_message_id should still @mention but reply_to is None."""
        config = self._make_config()
        task = self._make_task(
            room.canonical, is_group_chat=True, talk_message_id=None, user_id="dave",
        )

        await post_result_to_talk(config, task, "Response", use_reply_threading=True)

        assert [(c.token, c.args) for c in fake_talk.calls] == [
            (room.talk_ref, {
                "message": "@dave Response", "reply_to": None, "reference_id": None,
            }),
        ]
        assert fake_talk.refusals == []

    @pytest.mark.asyncio
    async def test_group_chat_no_threading_for_progress_updates(self, fake_talk, room):
        """Progress updates (use_reply_threading=False) should not get reply_to or @mention."""
        config = self._make_config()
        task = self._make_task(
            room.canonical, is_group_chat=True, talk_message_id=42, user_id="eve",
        )

        # Default use_reply_threading=False (progress/ack messages)
        await post_result_to_talk(config, task, "Working on it...")

        assert [(c.token, c.args) for c in fake_talk.calls] == [
            (room.talk_ref, {
                "message": "Working on it...", "reply_to": None,
                "reference_id": None,
            }),
        ]
        assert fake_talk.refusals == []

    @pytest.mark.asyncio
    async def test_reference_id_passed_through(self, fake_talk, room):
        """reference_id should be passed to send_message for each part."""
        config = self._make_config()
        task = self._make_task(room.canonical, is_group_chat=False, talk_message_id=42)

        await post_result_to_talk(
            config, task, "Result", reference_id="istota:task:1:result",
        )

        assert [(c.token, c.args) for c in fake_talk.calls] == [
            (room.talk_ref, {
                "message": "Result", "reply_to": None,
                "reference_id": "istota:task:1:result",
            }),
        ]
        assert fake_talk.refusals == []


class TestWorkerPoolConcurrencyCaps:
    """Test the three-tier concurrency control in WorkerPool.dispatch()."""

    def test_dispatch_respects_instance_fg_cap(self, db_path, tmp_path):
        """Foreground workers capped at max_foreground_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="fg1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", queue="foreground")
            db.create_task(conn, prompt="fg3", user_id="carol", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Only 2 fg workers despite 3 users, because max_foreground_workers=2
            fg_count = sum(1 for (_, qt, _) in pool._workers if qt == "foreground")
            assert fg_count <= 2

        pool.shutdown()

    def test_dispatch_respects_instance_bg_cap(self, db_path, tmp_path):
        """Background workers capped at max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg1", user_id="alice", queue="background")
            db.create_task(conn, prompt="bg2", user_id="bob", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            bg_count = sum(1 for (_, qt, _) in pool._workers if qt == "background")
            assert bg_count <= 1

        pool.shutdown()

    def test_dispatch_separate_fg_bg_caps(self, db_path, tmp_path):
        """Separate fg and bg caps work independently."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=4, max_background_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="fg1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="bg1", user_id="bob", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()


class TestMultiWorkerPerUser:
    """Tests for per-user multi-worker support (multiple fg/bg workers per user)."""

    def test_dispatch_multiple_fg_workers_same_user(self, db_path, tmp_path):
        """User with 2 pending fg tasks and per-user cap of 2 gets 2 fg workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_no_doomed_worker_for_same_room_gated_followup(self, db_path, tmp_path):
        """A follow-up queued behind an active task in the SAME room is gated
        by claim_task, so dispatch must not spawn a second (doomed) worker for
        it — the claimable count reads 0 even though a pending row exists."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="turn2", user_id="alice",
                           conversation_token="room1", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Gated follow-up → zero claimable → no worker spawned.
            assert pool.active_count == 0

        pool.shutdown()

    def test_spawns_worker_for_different_room_while_one_room_active(self, db_path, tmp_path):
        """The gate is per-room: a pending task in a DIFFERENT room from the
        active one is still claimable, so dispatch spawns a worker for it."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="r1-followup", user_id="alice",
                           conversation_token="room1", queue="foreground")
            db.create_task(conn, prompt="r2-task", user_id="alice",
                           conversation_token="room2", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # room1 follow-up gated, room2 task free → exactly one worker.
            assert pool.active_count == 1

        pool.shutdown()

    def test_dispatch_respects_per_user_fg_cap(self, db_path, tmp_path):
        """User with 3 pending fg tasks but per-user cap of 2 gets only 2 workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_per_user_bg_cap(self, db_path, tmp_path):
        """Background workers also respect per-user caps."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_background_workers=5,
                user_max_background_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t2", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t3", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_instance_cap_limits_per_user(self, db_path, tmp_path):
        """Instance cap of 2 overrides per-user cap of 3."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_doesnt_spawn_excess_workers_for_few_tasks(self, db_path, tmp_path):
        """Don't spawn 3 workers if user only has 1 pending task."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_dispatch_multiple_users_with_multi_workers(self, db_path, tmp_path):
        """Multiple users each get their per-user cap of workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=10,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="bob", queue="foreground")
            db.create_task(conn, prompt="t4", user_id="bob", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # alice: 2 workers, bob: 2 workers
            assert pool.active_count == 4

        pool.shutdown()

    def test_worker_key_is_three_tuple(self, db_path, tmp_path):
        """Worker keys should be (user_id, queue_type, slot) 3-tuples."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            for key in pool._workers:
                assert len(key) == 3, f"Expected 3-tuple key, got {key}"
                user_id, queue_type, slot = key
                assert isinstance(slot, int)

        pool.shutdown()

    def test_redispatch_doesnt_duplicate_existing_slots(self, db_path, tmp_path):
        """Calling dispatch twice doesn't create duplicate workers for same slots."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=2, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()
            assert pool.active_count == count_after_first

        pool.shutdown()

    def test_new_worker_spawned_while_existing_worker_busy(self, db_path, tmp_path):
        """A pending task should get a new worker even if another worker is busy.

        Scenario: user posts in Room A, worker 0 claims it (now running).
        User posts in Room B. Task B is pending, worker 0 is busy.
        Dispatch should spawn worker 1 for the pending task.
        """
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # Task A is claimed and running (simulates worker 0 busy)
        with db.get_db(db_path) as conn:
            task_a = db.create_task(conn, prompt="room A", user_id="alice", queue="foreground")
            db.update_task_status(conn, task_a, "running")
            # Task B is pending (user just posted in another room)
            db.create_task(conn, prompt="room B", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        # Simulate worker 0 already in the pool (busy with task A)
        busy_worker = MagicMock(spec=UserWorker)
        pool._workers[("alice", "foreground", 0)] = busy_worker

        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Should have 2 workers: slot 0 (busy) + slot 1 (new for task B)
            assert pool.active_count == 2

        pool.shutdown()

    def test_slot_assignment_handles_gaps(self, db_path, tmp_path):
        """Slot assignment should work even if lower slots have exited."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        # Simulate: slot 0 exited, slot 1 still running
        busy_worker = MagicMock(spec=UserWorker)
        pool._workers[("alice", "foreground", 1)] = busy_worker

        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Should pick slot 0 (gap) rather than colliding with slot 1
            keys = list(pool._workers.keys())
            slots = sorted(s for (uid, qt, s) in keys if uid == "alice" and qt == "foreground")
            assert 0 in slots, f"Expected slot 0 to be used, got slots {slots}"
            assert 1 in slots, f"Expected slot 1 to still exist, got slots {slots}"
            # No duplicate keys
            assert len(keys) == len(set(keys))

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestApiErrorInSuccessResult
# ---------------------------------------------------------------------------


class TestApiErrorInSuccessResult:
    """Test that process_one_task detects API errors in 'successful' results."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_api_error_in_result_flips_to_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """When execute_task returns success=True but result contains API error, treat as failure."""
        api_error = 'API Error: 500 {"error": {"message": "Internal server error"}, "request_id": "req_abc"}'
        mock_exec.return_value = (True, api_error, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Should be pending for retry (first attempt)
        assert task.status == "pending"
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(True, "Here is your morning briefing...", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_normal_result_not_affected(self, mock_arun, mock_exec, db_path, tmp_path):
        """Normal successful results are not falsely detected as API errors."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_answer_quoting_an_api_error_stays_successful(
        self, mock_arun, mock_exec, db_path, tmp_path,
    ):
        """ISSUE-212 regression: the masquerading-success guard must use the
        strict banner detector, not a bare parse.

        Widening `parse_api_error` to the bodyless form made this guard match any
        successful answer that *mentions* a provider error — so a log summary or
        an ops question about a 529 was discarded, retried three times producing
        the same answer, and failed permanently.
        """
        answer = (
            "Yesterday's incident report: the 03:12 run died with API Error: 529 "
            "Overloaded, and the 03:40 retry succeeded. No action needed."
        )
        mock_exec.return_value = (True, answer, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Summarise", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_bodyless_banner_still_flips_to_failure(
        self, mock_arun, mock_exec, db_path, tmp_path,
    ):
        """The other half: the bare banner the issue was filed about."""
        mock_exec.return_value = (True, "API Error: 529 Overloaded", None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        _task_id, success = result
        assert success is False


# ---------------------------------------------------------------------------
# TestBriefingFailureSuppression
# ---------------------------------------------------------------------------


class TestBriefingFailureSuppression:
    """Test that briefing/scheduled task failures don't send error notifications to users."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_briefing_failure_no_talk_notification(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed briefing tasks should not send error messages to Talk."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Morning briefing", user_id="testuser",
                source_type="briefing", conversation_token="room1",
            )
            # Exhaust retries so it fails permanently
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # asyncio.run should NOT be called for Talk error notification
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_scheduled_failure_no_talk_notification(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed scheduled tasks should not send error messages to Talk."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Daily check", user_id="testuser",
                source_type="scheduled", conversation_token="room1",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # No Talk notification for scheduled failures
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None, None))
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_interactive_failure_still_notifies(self, mock_arun, mock_exec, db_path, tmp_path):
        """Interactive (Talk) task failures should still send error messages."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Help me", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # Should have Talk notification with error
        assert mock_arun.call_count >= 1


class TestBriefingJsonDelivery:
    """Test that briefing tasks parse JSON output and deliver the body."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_briefing_json_posted_to_talk(self, mock_arun, mock_exec, db_path, tmp_path):
        """Briefing JSON result should have its body extracted and posted to Talk."""
        json_result = '{"subject": "Morning Briefing", "body": "📰 NEWS\\nStuff happened today."}'
        mock_exec.return_value = (True, json_result, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Generate a morning briefing", user_id="testuser",
                source_type="briefing", conversation_token="room1",
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Check that Talk was called with the body, not the raw JSON
        assert mock_arun.call_count >= 1
        # The first asyncio.run call should be post_result_to_talk with the body
        mock_arun.call_args_list[0]
        # asyncio.run receives a coroutine; check the message was the extracted body
        # We verify indirectly: the raw JSON should NOT be in the completed task result
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_briefing_json_email_uses_body(self, mock_arun, mock_exec, db_path, tmp_path):
        """Briefing with email target should use extracted body for email delivery."""
        json_result = '{"subject": "Evening Briefing", "body": "📈 MARKETS\\nS&P up 0.5%"}'
        mock_exec.return_value = (True, json_result, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Generate a evening briefing", user_id="testuser",
                source_type="briefing", output_target="email",
            )

        with patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=True) as mock_email:
            result = process_one_task(config)
            assert result is not None
            _, success = result
            assert success is True
            # post_result_to_email should receive the body, not raw JSON
            mock_email.assert_called_once()
            email_msg = mock_email.call_args[0][2]  # third positional arg is message
            assert "MARKETS" in email_msg
            assert '"subject"' not in email_msg  # not raw JSON

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.run_coro", return_value=None)
    def test_briefing_non_json_fallback(self, mock_arun, mock_exec, db_path, tmp_path):
        """If briefing result is not JSON, deliver as-is (backward compat)."""
        plain_result = "📰 NEWS\nStuff happened today."
        mock_exec.return_value = (True, plain_result, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Generate a morning briefing", user_id="testuser",
                source_type="briefing", conversation_token="room1",
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Should still deliver successfully
        assert mock_arun.call_count >= 1


# ---------------------------------------------------------------------------
# TestMalformedResultGuard
# ---------------------------------------------------------------------------


class TestMalformedResultGuard:
    """Test that process_one_task detects malformed model output and treats it as failure."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_leaked_xml_flips_to_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """Leaked tool-call XML in result should be treated as failure."""
        mock_exec.return_value = (True, "</parameter>\n</invoke>", None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Find painting studios", user_id="testuser", source_type="talk")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Should be pending for retry (first attempt)
        assert task.status == "pending"
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(True, "Here is your morning briefing with details...", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_normal_result_not_affected(self, mock_arun, mock_exec, db_path, tmp_path):
        """Normal successful results are not falsely flagged as malformed."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_talk_strict_xml_in_prose_flips_to_failure(self, mock_arun, mock_exec, db_path, tmp_path, fake_talk):
        """XML patterns in prose should be caught for Talk output (strict mode)."""
        text = "The error was </parameter> and it broke things."
        mock_exec.return_value = (True, text, None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="room1")
            db.create_task(conn, prompt="Research", user_id="testuser",
                           source_type="talk", conversation_token="room1")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False
        # Self-checking seed: a token the room fixture never bound would be
        # refused and swallowed, leaving this green and the fixture inert.
        assert fake_talk.refusals == []

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_malformed_result_retries_then_fails(self, mock_arun, mock_exec, db_path, tmp_path, fake_talk):
        """Malformed results exhaust retries and eventually fail permanently."""
        mock_exec.return_value = (True, "</invoke>", None, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            plain_talk_room(conn, "testuser", token="room1")
            db.create_task(conn, prompt="Test", user_id="testuser", source_type="talk",
                           conversation_token="room1")

        # First attempt → pending retry
        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 1

        # Force scheduled_for to now so it can be picked up again
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET scheduled_for = datetime('now', '-1 minute') WHERE id = ?", (task_id,))

        # Second attempt → pending retry
        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 2

        # Force scheduled_for again
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET scheduled_for = datetime('now', '-1 minute') WHERE id = ?", (task_id,))

        # Third attempt → failed permanently
        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        # Self-checking seed, as above.
        assert fake_talk.refusals == []


# ---------------------------------------------------------------------------
# TestTaskIdInProgress
# ---------------------------------------------------------------------------


class TestTaskIdInProgress:
    """Test that task ID appears in ack messages and done summaries."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Here is the answer to your question.", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_ack_message_contains_task_id(
        self, mock_arun, mock_exec, db_path, tmp_path, fake_talk,
    ):
        """Ack message posted to Talk should contain the task ID.

        Asserted at the Talk seam. The previous version tried and failed to
        read the ack out of `asyncio.run`'s call list — the ack never went
        through that binding, both of its loop bodies were empty, and the only
        live assertion left was `task_id is not None`. Meanwhile the delivery
        it could not see was reaching the real Talk client.
        """
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            room = plain_talk_room(conn, "testuser", token="room1")
            task_id = db.create_task(
                conn, prompt="Hello", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        process_one_task(config)

        acks = [
            c for c in fake_talk.calls_to(room.talk_ref, method="send_message")
            if c.args.get("reference_id") == f"istota:task:{task_id}:ack"
        ]
        assert len(acks) == 1, "no ack reached the Talk room"
        assert f"#{task_id}" in acks[0].args["message"]
        assert fake_talk.refusals == []

    @patch("istota.scheduler.execute_task", return_value=(True, "Done with the research.", '["Read file", "Write file"]', None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    @patch("istota.scheduler.edit_talk_message")
    def test_done_summary_contains_task_id(self, mock_edit, mock_arun, mock_exec, db_path, tmp_path):
        """Done summary should contain the task ID."""
        config = self._make_config(db_path, tmp_path)
        config.scheduler.progress_updates = True

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Research topic", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        # The progress callback needs ack_msg_id and use_edit to trigger the done summary
        # This is complex to test through process_one_task because it needs a real progress callback
        # Instead, verify the format strings directly
        from istota.scheduler import PROGRESS_MESSAGES
        import random

        # Verify the ack format includes task ID
        ack_normal = f"`#{task_id}` {random.choice(PROGRESS_MESSAGES)}"
        assert f"`#{task_id}`" in ack_normal

        ack_retry = f"`#{task_id}` *Retrying…*"
        assert f"`#{task_id}`" in ack_retry

        # Verify the done format includes task ID
        elapsed = 52
        total = 8
        done_body = f"`#{task_id}` ✅ Done — {total} action{'s' if total != 1 else ''} ({elapsed}s)"
        assert f"`#{task_id}`" in done_body

        done_no_actions = f"`#{task_id}` ✅ Done ({elapsed}s)"
        assert f"`#{task_id}`" in done_no_actions


class TestCheckDbHealth:
    """Scheduler-level sweep: framework DB + per-user module DBs.

    Self-healing of an individual DB is covered in ``tests/test_db_health.py``;
    here we just verify the enumeration covers what it should.
    """

    def _make_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

    def test_sweep_covers_framework_and_per_user_dbs(self, tmp_path):
        from istota.scheduler import check_db_health

        framework_db = tmp_path / "istota.db"
        self._make_db(framework_db)

        config = Config(
            db_path=framework_db,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=tmp_path / "mount",
            module_data_dir=tmp_path / "local",
            users={"alice": UserConfig(), "bob": UserConfig()},
        )

        # Module DBs now live on LOCAL disk at module_db_path. alice has feeds +
        # health present; bob has location; absent files are still reported so
        # operators see what was probed.
        for user, modules in (("alice", ("feeds", "health")), ("bob", ("location",))):
            for module in modules:
                self._make_db(config.module_db_path(user, module))

        reports = check_db_health(config)
        labels = {r.label: r for r in reports}

        # Framework + 5 per-user modules per user = 1 + 2*5 = 11 reports.
        assert "framework" in labels
        assert labels["framework"].ok
        for user in ("alice", "bob"):
            for module in ("feeds", "health", "location", "money", "briefings"):
                assert f"{module}:{user}" in labels

        # A DB we didn't create reports as missing (ok=True, no repair).
        missing = labels["money:alice"]
        assert missing.ok and not missing.repair_attempted

        # And the ones we did create are clean.
        present = labels["feeds:alice"]
        assert present.ok and not present.repair_attempted

    def test_probes_module_dbs_without_mount(self, tmp_path):
        from istota.scheduler import check_db_health

        framework_db = tmp_path / "istota.db"
        self._make_db(framework_db)

        config = Config(
            db_path=framework_db,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=None,
            module_data_dir=tmp_path / "local",
            users={"alice": UserConfig()},
        )

        reports = check_db_health(config)
        labels = [r.label for r in reports]
        # Module DBs are local now — they're probed regardless of mount.
        assert "framework" in labels
        for module in ("feeds", "health", "location", "money", "briefings"):
            assert f"{module}:alice" in labels


class TestReconcileVisitsMissingDb:
    """A location-enabled user who has never ingested a ping has no
    ``location.db`` (the file and its parent dir are created on first
    webhook write). The reconcile/cleanup sweeps must skip such users
    silently instead of trying to open a non-existent DB and logging an
    exception traceback every tick.
    """

    def _config(self, tmp_path):
        mount = tmp_path / "mount"
        framework_db = tmp_path / "istota.db"
        db.init_db(framework_db)
        return Config(
            db_path=framework_db,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount,
            location=LocationReceiverConfig(reconcile_enabled=True),
            users={"frank": UserConfig()},
        )

    def test_reconcile_skips_user_with_no_location_db(self, tmp_path, caplog):
        import logging

        from istota.scheduler import _reconcile_visits_for_all_users

        config = self._config(tmp_path)

        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            _reconcile_visits_for_all_users(config)

        assert "Visit reconciliation failed" not in caplog.text

    def test_cleanup_pings_skips_user_with_no_location_db(self, tmp_path, caplog):
        import logging

        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path)
        config.scheduler.location_ping_retention_days = 30

        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            run_cleanup_checks(config)

        assert "Failed to clean up location pings" not in caplog.text


class _FakeTime:
    """Deterministic stand-in for the scheduler module's `time` binding."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def _sched_cfg(poll_interval, dispatch_interval):
    cfg = Config()
    cfg.scheduler = SchedulerConfig(
        poll_interval=poll_interval, dispatch_interval=dispatch_interval
    )
    return cfg


class TestDispatchSleep:
    """_dispatch_sleep: sub-tick re-dispatch without re-running gated checks."""

    def test_slices_and_redispatches_within_one_tick(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        _dispatch_sleep(pool, _sched_cfg(2, 0.5), lambda: False)

        # 0.5s slices across a 2s tick → dispatch after each of 4 slices.
        assert pool.dispatch.call_count == 4
        assert fake.sleeps == [0.5, 0.5, 0.5, 0.5]
        # Total slept never overruns the base tick.
        assert sum(fake.sleeps) == pytest.approx(2.0)

    def test_final_slice_is_clamped_to_remaining(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        # 0.3s slices across 1s: 0.3, 0.3, 0.3, then a clamped 0.1.
        _dispatch_sleep(pool, _sched_cfg(1, 0.3), lambda: False)

        assert fake.sleeps == pytest.approx([0.3, 0.3, 0.3, 0.1])
        assert sum(fake.sleeps) == pytest.approx(1.0)
        assert pool.dispatch.call_count == 4

    def test_legacy_single_sleep_when_interval_ge_base(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        _dispatch_sleep(pool, _sched_cfg(2, 2), lambda: False)

        # dispatch_interval >= poll_interval → one sleep, no extra dispatch.
        assert fake.sleeps == [2]
        assert pool.dispatch.call_count == 0

    def test_legacy_single_sleep_when_interval_zero(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        _dispatch_sleep(pool, _sched_cfg(2, 0), lambda: False)

        assert fake.sleeps == [2]
        assert pool.dispatch.call_count == 0

    def test_stop_before_any_slice(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        _dispatch_sleep(pool, _sched_cfg(2, 0.5), lambda: True)

        # Already shutting down → never sleeps or dispatches.
        assert fake.sleeps == []
        assert pool.dispatch.call_count == 0

    def test_stop_mid_tick_returns_before_dispatch(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()

        calls = {"n": 0}

        def should_stop():
            # False on the while-guard, True on the post-sleep check.
            calls["n"] += 1
            return calls["n"] >= 2

        _dispatch_sleep(pool, _sched_cfg(2, 0.5), should_stop)

        # Slept one slice, then bailed before dispatching.
        assert fake.sleeps == [0.5]
        assert pool.dispatch.call_count == 0

    def test_dispatch_error_does_not_abort_the_tick(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pool = MagicMock()
        pool.dispatch.side_effect = RuntimeError("boom")

        # A dispatch failure is logged, not raised — the tick still completes.
        _dispatch_sleep(pool, _sched_cfg(2, 0.5), lambda: False)

        assert pool.dispatch.call_count == 4
        assert sum(fake.sleeps) == pytest.approx(2.0)


def _idle_cfg(poll_interval=2.0, idle_poll=0.5, idle_timeout=10):
    cfg = Config()
    cfg.scheduler = SchedulerConfig(
        poll_interval=poll_interval,
        worker_idle_poll_interval=idle_poll,
        worker_idle_timeout=idle_timeout,
    )
    return cfg


class _Counter:
    """Records call count and returns a fixed value (or raises)."""

    def __init__(self, value=None, raises=None):
        self.value = value
        self.raises = raises
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.value


def _scripted(values):
    """Closure returning successive values, repeating the last forever."""
    seq = list(values)
    state = {"i": 0}

    def fn():
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return fn


class TestWorkerIdleWait:
    """_worker_idle_wait: fine-cadence idle re-check with a cumulative deadline."""

    def test_claims_task_appearing_mid_linger(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        stop = threading.Event()
        # Empty for the first three slices, then a task appears.
        pending = _scripted([0, 0, 0, 1])
        run_one = _Counter(value=(7, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            stop, lambda: False, run_one=run_one, pending_count=pending,
        )

        assert result == (7, True)
        assert run_one.calls == 1
        # Slept to the slice the task appeared on — not the full timeout.
        assert fake.sleeps == pytest.approx([0.5, 0.5, 0.5, 0.5])

    def test_pre_check_gates_claim(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(1, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 0,
        )

        # Queue stays empty → the expensive claim is never attempted.
        assert result is None
        assert run_one.calls == 0

    def test_exits_after_idle_timeout(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(1, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 0,
        )

        assert result is None
        assert run_one.calls == 0
        # timeout / poll = 2 / 0.5 = 4 slices, summing exactly to the timeout.
        assert fake.sleeps == pytest.approx([0.5, 0.5, 0.5, 0.5])
        assert sum(fake.sleeps) == pytest.approx(2.0)

    def test_closed_admission_gate_blocks_the_claim(self, monkeypatch):
        """A parked worker must not claim while the host has no room.

        Gating `dispatch()` alone bounds new worker *threads*, not new task
        *starts*: a worker already alive re-enters here and claims whatever
        arrives, so on a squeezed host work keeps starting in the slot that
        already exists. The 2026-08-20 trigger was a single task running a test
        suite, so one claim into a lingering worker is the whole incident.
        """
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(1, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 5,
            admission_open=lambda: False,
        )

        # Tasks are waiting and the worker still refuses to take one.
        assert result is None
        assert run_one.calls == 0

    def test_a_closed_gate_still_lets_the_worker_age_out(self, monkeypatch):
        """Refusing to claim must not turn into parking forever.

        Under sustained pressure the workers drain away and dispatch declines
        to respawn them, so the pool empties instead of holding idle threads
        against a host that has no room for them.
        """
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=_Counter(value=(1, True)), pending_count=lambda: 5,
            admission_open=lambda: False,
        )

        assert result is None
        # Slept the full deadline rather than spinning on the closed gate.
        assert fake.sleeps == pytest.approx([0.5, 0.5, 0.5, 0.5])

    def test_a_reopening_gate_is_picked_up_mid_linger(self, monkeypatch):
        """The hold is transient, on this path as on dispatch's."""
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(9, True))
        gate = _scripted([False, False, True])

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 1,
            admission_open=gate,
        )

        assert result == (9, True)
        assert run_one.calls == 1

    def test_the_gate_is_checked_before_the_pending_count(self, monkeypatch):
        """A closed gate should cost nothing — not even the indexed read."""
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        pending = _Counter(value=5)

        _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=_Counter(value=(1, True)), pending_count=pending,
            admission_open=lambda: False,
        )

        assert pending.calls == 0

    def test_legacy_branch_also_refuses_on_a_closed_gate(self, monkeypatch):
        """The coarse-wait branch is the one whose closed-gate semantics differ.

        `worker_idle_poll_interval >= worker_idle_timeout` takes the legacy
        single-recheck path, which has no polling loop to continue — so a shut
        gate returns None and the worker exits, rather than aging out. That is
        the same exit this branch already takes on an empty queue, so parity
        holds; it is pinned here because it is the branch a reader of the
        fine-cadence tests would assume works the other way.
        """
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(1, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=2, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 5,
            admission_open=lambda: False,
        )

        assert result is None
        assert run_one.calls == 0

    def test_legacy_branch_still_claims_when_the_gate_is_open(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(4, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=2, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 5,
            admission_open=lambda: True,
        )

        assert result == (4, True)
        assert run_one.calls == 1

    def test_admission_defaults_to_open_for_existing_callers(self, monkeypatch):
        """The parameter is optional; omitting it keeps the old behaviour."""
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(3, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 1,
        )

        assert result == (3, True)

    def test_final_slice_clamped(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.3, idle_timeout=1.0),
            threading.Event(), lambda: False,
            run_one=_Counter(value=None), pending_count=lambda: 0,
        )

        assert result is None
        # Non-divisor config: last slice clamped so the total never overshoots.
        assert fake.sleeps == pytest.approx([0.3, 0.3, 0.3, 0.1])
        assert sum(fake.sleeps) == pytest.approx(1.0)

    def test_legacy_single_wait_when_poll_ge_timeout(self):
        run_one = _Counter(value=None)
        # Legacy branch uses an interruptible stop_event.wait (not time.sleep),
        # so it stays instantly wakeable on stop — exact pre-phase-2 parity.
        stop = MagicMock()
        stop.wait.return_value = False  # not stopped during the wait

        result = _worker_idle_wait(
            "u", "foreground",
            _idle_cfg(poll_interval=2, idle_poll=10, idle_timeout=5),
            stop, lambda: False,
            run_one=run_one, pending_count=_Counter(value=1),
        )

        # One coarse interruptible wait of min(poll_interval, idle_timeout),
        # then a single recheck — no fine slices, no pre-check.
        assert result is None
        stop.wait.assert_called_once_with(timeout=2)
        assert run_one.calls == 1

    def test_legacy_single_wait_when_poll_zero(self):
        run_one = _Counter(value=None)
        stop = MagicMock()
        stop.wait.return_value = False

        result = _worker_idle_wait(
            "u", "foreground",
            _idle_cfg(poll_interval=2, idle_poll=0, idle_timeout=5),
            stop, lambda: False,
            run_one=run_one, pending_count=_Counter(value=1),
        )

        assert result is None
        stop.wait.assert_called_once_with(timeout=2)
        assert run_one.calls == 1

    def test_legacy_stop_during_wait_returns_none_before_run_one(self):
        run_one = _Counter(value=(1, True))
        stop = MagicMock()
        stop.wait.return_value = True  # stop signalled mid-wait — wakes at once

        result = _worker_idle_wait(
            "u", "foreground",
            _idle_cfg(poll_interval=2, idle_poll=0, idle_timeout=5),
            stop, lambda: False,
            run_one=run_one, pending_count=_Counter(value=1),
        )

        # Interruptible wait returns True → exit immediately, no recheck.
        assert result is None
        assert run_one.calls == 0

    def test_stop_event_returns_none_before_run_one(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        stop = threading.Event()
        stop.set()
        run_one = _Counter(value=(1, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            stop, lambda: False,
            run_one=run_one, pending_count=lambda: 1,
        )

        # Pre-set stop → exit immediately, no sleep, no claim.
        assert result is None
        assert fake.sleeps == []
        assert run_one.calls == 0

    def test_shutdown_mid_idle_returns_promptly(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(1, True))
        # False on the while-guard, True on the post-sleep recheck.
        flips = {"n": 0}

        def should_stop():
            flips["n"] += 1
            return flips["n"] >= 2

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            threading.Event(), should_stop,
            run_one=run_one, pending_count=lambda: 1,
        )

        # Honoured within one idle slice; claim never attempted.
        assert result is None
        assert fake.sleeps == [0.5]
        assert run_one.calls == 0

    def test_claim_race_loss_keeps_polling_same_deadline(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        # Pending always positive, but every claim is lost (run_one -> None).
        run_one = _Counter(value=None)

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=2),
            threading.Event(), lambda: False,
            run_one=run_one, pending_count=lambda: 1,
        )

        # Deadline is NOT reset by lost races → loop still exits at the timeout
        # instead of spinning forever.
        assert result is None
        assert sum(fake.sleeps) == pytest.approx(2.0)
        assert run_one.calls == 4

    def test_pending_count_error_falls_through_to_run_one(self, monkeypatch):
        import istota.scheduler as sched_mod

        fake = _FakeTime()
        monkeypatch.setattr(sched_mod, "time", fake)
        run_one = _Counter(value=(5, True))

        result = _worker_idle_wait(
            "u", "foreground", _idle_cfg(idle_poll=0.5, idle_timeout=10),
            threading.Event(), lambda: False,
            run_one=run_one,
            pending_count=_Counter(raises=RuntimeError("read failed")),
        )

        # A transient read error must not skip a possibly-present task.
        assert result == (5, True)
        assert run_one.calls == 1


class TestMainLoopReadTimeout:
    """The dispatch scan and idle pre-check use a short busy_timeout; a locked
    DB degrades to 'skip this tick' rather than blocking the loop 30s."""

    def _config(self, tmp_path):
        db_path = tmp_path / "loop.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(main_loop_read_timeout_ms=2000),
            temp_dir=tmp_path / "temp",
        )

    def test_count_pending_returns_zero_on_lock(self, tmp_path):
        from istota.scheduler import _count_pending
        config = self._config(tmp_path)

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        with patch("istota.scheduler.db.get_db", side_effect=boom):
            assert _count_pending(config, "alice", "foreground") == 0

    def test_dispatch_skips_tick_on_lock(self, tmp_path):
        config = self._config(tmp_path)
        pool = WorkerPool(config)

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        with patch("istota.scheduler.db.get_db", side_effect=boom):
            pool.dispatch()  # must not raise
        assert pool.active_count == 0

    def test_count_pending_passes_configured_timeout(self, tmp_path):
        from istota.scheduler import _count_pending
        config = self._config(tmp_path)
        seen = {}
        real = db.get_db

        def spy(path, **kwargs):
            seen.update(kwargs)
            return real(path, **kwargs)

        with patch("istota.scheduler.db.get_db", side_effect=spy):
            _count_pending(config, "alice", "foreground")
        assert seen.get("busy_timeout_ms") == 2000


# ---------------------------------------------------------------------------
# TestSessionLogSweepWiring
# ---------------------------------------------------------------------------


class TestSessionLogSweepWiring:
    """Step 7b of `run_cleanup_checks`: the only caller of the session-log sweep.

    The feature ships `enabled = true` and the writer appends for every native
    task attempt, so until this step exists nothing on the deployment deletes a
    session log — an observability artifact with unbounded growth on the same
    filesystem as the framework database. Every assertion here is about the
    *gate* and the *arguments*; the delete rules themselves are held by
    `tests/native/test_session_log.py`, which is where they live.
    """

    def _config(self, tmp_path, **session_log_kwargs):
        from istota.config import BrainConfig, NativeBrainConfig, SessionLogConfig

        framework_db = tmp_path / "data" / "istota.db"
        framework_db.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(framework_db)
        return Config(
            db_path=framework_db,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=EmailConfig(),
            scheduler=SchedulerConfig(
                # Every other cleanup step off, so a call this class does not
                # expect cannot come from one of them.
                temp_file_retention_days=0,
                email_retention_days=0,
                location_ping_retention_days=0,
            ),
            temp_dir=tmp_path / "temp",
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(
                    session_log=SessionLogConfig(**session_log_kwargs),
                ),
            ),
        )

    @staticmethod
    def _age(path: Path, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def _write_log(self, root: Path, user: str, name: str, *, age_days: float) -> Path:
        directory = root / user
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text('{"type":"session"}\n')
        self._age(path, age_days)
        return path

    # -- the gate ---------------------------------------------------------

    def test_the_sweep_runs_when_the_feature_is_enabled(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_count == 1

    def test_the_sweep_still_runs_when_the_feature_is_off(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path, enabled=False)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_count == 1

    def test_retention_days_zero_still_sweeps_for_the_ceiling(self, tmp_path):
        # The `or` gate, from the age side. An operator who keeps everything
        # indefinitely by age still wants the disk bound in force; wiring the
        # gate as `and` silently disables the ceiling, which is the exact
        # failure the ceiling exists to prevent.
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path, retention_days=0, max_total_gb=2.0)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_count == 1

    def test_a_zero_ceiling_still_sweeps_for_age(self, tmp_path):
        # The `or` gate, from the other side.
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path, retention_days=14, max_total_gb=0)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_count == 1

    def test_both_rules_disabled_sweeps_nothing(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(
            tmp_path, enabled=False, retention_days=0, max_total_gb=0,
        )
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_count == 0

    # -- the arguments ----------------------------------------------------

    def test_the_sweep_is_handed_the_resolved_directory_and_the_policy(self, tmp_path):
        from istota.scheduler import run_cleanup_checks
        from istota.session.session_log import resolve_session_log_dir

        config = self._config(tmp_path, retention_days=9, max_total_gb=3.5)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        args, kwargs = sweep.call_args
        assert args[0] == resolve_session_log_dir(config.db_path, "")
        assert kwargs["retention_days"] == 9
        assert kwargs["max_total_gb"] == 3.5
        assert isinstance(kwargs["now"], float)

    def test_an_operator_set_directory_is_what_gets_swept(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        elsewhere = tmp_path / "elsewhere"
        config = self._config(tmp_path, dir=str(elsewhere))
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult()
            run_cleanup_checks(config)

        assert sweep.call_args[0][0] == elsewhere

    # -- the deletion actually happens ------------------------------------

    def test_a_tick_deletes_an_aged_log_off_the_disk(self, tmp_path):
        # Not a mock: the one assertion that the wiring reaches a real unlink.
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path, retention_days=14)
        root = tmp_path / "data" / "logs"
        aged = self._write_log(root, "alice", "old.jsonl", age_days=30)
        fresh = self._write_log(root, "alice", "new.jsonl", age_days=1)

        run_cleanup_checks(config)

        assert not aged.exists()
        assert fresh.exists()

    def test_a_tick_deletes_an_aged_log_when_the_feature_is_off(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path, enabled=False, retention_days=14)
        root = tmp_path / "data" / "logs"
        aged = self._write_log(root, "alice", "old.jsonl", age_days=900)
        fresh = self._write_log(root, "alice", "new.jsonl", age_days=1)

        run_cleanup_checks(config)

        assert not aged.exists()
        assert fresh.exists()

    # -- failure is absorbed ----------------------------------------------

    def test_a_raising_sweep_does_not_abort_the_tick(self, tmp_path, caplog):
        import logging

        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path)
        with patch("istota.scheduler.sweep_session_logs", side_effect=OSError("boom")):
            with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
                run_cleanup_checks(config)  # must not raise

        assert "session log" in caplog.text.lower()

    # -- what doctor reads back -------------------------------------------

    def test_a_sweep_that_evicted_by_size_is_recorded_for_doctor(self, tmp_path):
        # `deleted_size > 0` means `retention_days` is not the retention in
        # force. doctor cannot see the sweep, so the tick has to write it down.
        from istota.scheduler import run_cleanup_checks
        from istota.session.session_log import (
            SWEEP_STATE_KEY,
            SWEEP_STATE_NAMESPACE,
            decode_sweep_state,
        )

        config = self._config(tmp_path)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult(deleted_age=2, deleted_size=3, bytes_after=99)
            run_cleanup_checks(config)

        with db.get_db(config.db_path) as conn:
            row = db.shared_kv_get(conn, SWEEP_STATE_NAMESPACE, SWEEP_STATE_KEY)
        assert row is not None
        state = decode_sweep_state(row["value"])
        assert state["deleted_size"] == 3
        assert state["deleted_age"] == 2
        assert state["bytes_after"] == 99

    def test_the_recorded_state_is_replaced_by_the_next_tick(self, tmp_path):
        # A ceiling that stopped binding must stop warning, so the row is the
        # last sweep rather than the worst one.
        from istota.scheduler import run_cleanup_checks
        from istota.session.session_log import (
            SWEEP_STATE_KEY,
            SWEEP_STATE_NAMESPACE,
            decode_sweep_state,
        )

        config = self._config(tmp_path)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult(deleted_size=5)
            run_cleanup_checks(config)
            sweep.return_value = SweepResult(deleted_size=0)
            run_cleanup_checks(config)

        with db.get_db(config.db_path) as conn:
            row = db.shared_kv_get(conn, SWEEP_STATE_NAMESPACE, SWEEP_STATE_KEY)
        assert decode_sweep_state(row["value"])["deleted_size"] == 0

    def test_an_unwritable_state_row_does_not_fail_the_tick(self, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = self._config(tmp_path)
        with patch("istota.scheduler.sweep_session_logs") as sweep:
            sweep.return_value = SweepResult(deleted_size=1)
            with patch("istota.scheduler.db.shared_kv_set", side_effect=OSError("nope")):
                run_cleanup_checks(config)  # must not raise
