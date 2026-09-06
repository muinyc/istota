"""Tests for db module functions."""

import sqlite3

import pytest

from istota import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


class TestCountInflightTasksForScheduledJob:
    def test_zero_when_none(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.count_inflight_tasks_for_scheduled_job(conn, 49) == 0

    def test_counts_pending_and_running(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="cron", user_id="alice",
                source_type="scheduled", scheduled_job_id=49, queue="background",
            )
            running = db.create_task(
                conn, prompt="cron", user_id="alice",
                source_type="scheduled", scheduled_job_id=49, queue="background",
            )
            db.update_task_status(conn, running, "running")
            assert db.count_inflight_tasks_for_scheduled_job(conn, 49) == 2

    def test_ignores_terminal_states(self, db_path):
        with db.get_db(db_path) as conn:
            done = db.create_task(
                conn, prompt="cron", user_id="alice",
                source_type="scheduled", scheduled_job_id=49, queue="background",
            )
            db.update_task_status(conn, done, "completed", result="ok")
            failed = db.create_task(
                conn, prompt="cron", user_id="alice",
                source_type="scheduled", scheduled_job_id=49, queue="background",
            )
            db.update_task_status(conn, failed, "failed", error="x")
            assert db.count_inflight_tasks_for_scheduled_job(conn, 49) == 0

    def test_scoped_to_job_id(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="cron", user_id="alice",
                source_type="scheduled", scheduled_job_id=49, queue="background",
            )
            assert db.count_inflight_tasks_for_scheduled_job(conn, 72) == 0


class TestUpdateTaskStatusPersistsTrace:
    """ISSUE-183: a failed/cancelled task must persist its execution trace +
    actions_taken + error + completed_at so the web chat can reconstruct the
    intermediate tool/text output on reload. Previously the ``failed`` branch
    dropped the trace and ``cancelled`` fell through to a status-only update
    (losing even the error), leaving a blank agent bubble on reload."""

    def test_failed_persists_trace_actions_error_completed_at(self, db_path):
        import json
        trace = json.dumps([{"type": "tool", "text": "Bash: ls"}])
        actions = json.dumps(["Bash: ls"])
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="p", user_id="alice")
            db.update_task_status(conn, tid, "running")
            db.update_task_status(
                conn, tid, "failed", error="boom",
                actions_taken=actions, execution_trace=trace,
            )
            row = conn.execute(
                "SELECT status, error, actions_taken, execution_trace, completed_at "
                "FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "boom"
        assert row["actions_taken"] == actions
        assert row["execution_trace"] == trace
        assert row["completed_at"] is not None

    def test_cancelled_persists_trace_actions_error_completed_at(self, db_path):
        import json
        trace = json.dumps([
            {"type": "tool", "text": "Read a.txt"},
            {"type": "text", "text": "partial analysis"},
        ])
        actions = json.dumps(["Read a.txt"])
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="p", user_id="alice")
            db.update_task_status(conn, tid, "running")
            db.update_task_status(
                conn, tid, "cancelled", error="Cancelled by user",
                actions_taken=actions, execution_trace=trace,
            )
            row = conn.execute(
                "SELECT status, error, actions_taken, execution_trace, completed_at "
                "FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["status"] == "cancelled"
        assert row["error"] == "Cancelled by user"
        assert row["actions_taken"] == actions
        assert row["execution_trace"] == trace
        assert row["completed_at"] is not None

    def test_failed_without_trace_stores_nulls(self, db_path):
        # Callers without a trace (skill/command tasks) still work — NULLs are
        # stored, not raised.
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="p", user_id="alice")
            db.update_task_status(conn, tid, "failed", error="boom")
            row = conn.execute(
                "SELECT actions_taken, execution_trace FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["actions_taken"] is None
        assert row["execution_trace"] is None

    def test_cancelled_sets_completed_at_for_retention(self, db_path):
        # cleanup_old_tasks reaps by completed_at; a NULL completed_at stranded
        # cancelled rows forever. The fix sets it.
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="p", user_id="alice")
            db.update_task_status(conn, tid, "cancelled", error="Cancelled by user")
            row = conn.execute(
                "SELECT completed_at FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["completed_at"] is not None


class TestHasActiveForegroundTaskForChannel:
    def test_true_when_pending_fg_task_exists(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_false_when_no_active_fg_task(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_ignores_completed_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_ignores_background_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="cron job", user_id="alice",
                conversation_token="room1", queue="background",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_true_for_locked_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "locked")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_true_for_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "running")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_different_channel_not_counted(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room2") is False

    def test_false_when_cancel_requested(self, db_path):
        """A running task with cancel_requested should not block new messages."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "running")
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False


class TestClaimTaskChannelGate:
    def test_claim_skips_channel_blocked_fg_tasks(self, db_path):
        """claim_task should not return a fg task if another fg task in the
        same channel is already running/locked."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            t3 = db.create_task(
                conn, prompt="do thing 3", user_id="user1",
                source_type="talk", conversation_token="room2",
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t3

    def test_claim_unblocks_after_channel_task_completes(self, db_path):
        """Once the active task completes, the next one in the same channel
        becomes claimable."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )

            claimed = db.claim_task(
                conn, "worker-1", queue="foreground", user_id="user1",
            )
            assert claimed is None

            db.update_task_status(conn, t1, "completed", result="ok")

            claimed = db.claim_task(
                conn, "worker-2", queue="foreground", user_id="user1",
            )
            assert claimed is not None
            assert claimed.id == t2

    def test_channel_gate_ignores_background_queue(self, db_path):
        """Background tasks are not subject to the per-channel gate."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="fg thing", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="bg thing", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="background",
            )

            claimed = db.claim_task(conn, "worker-1", queue="background")
            assert claimed is not None
            assert claimed.id == t2

    def test_channel_gate_ignores_null_token(self, db_path):
        """Tasks without a conversation_token (email, cron) are never
        channel-blocked."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="cron thing", user_id="user1",
                source_type="cron", conversation_token=None,
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="email thing", user_id="user1",
                source_type="email", conversation_token=None,
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t2

    def test_cancelled_task_does_not_block(self, db_path):
        """A cancel_requested task in the channel should not block the next."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (t1,),
            )
            conn.commit()

            t2 = db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t2

    def test_pending_confirmation_blocks_channel(self, db_path):
        """A task parked awaiting confirmation owns its channel — the next
        queued message in the same room must wait (web chat single-active-per-
        room), but a different room stays claimable."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="web", conversation_token="room1",
                queue="foreground",
            )
            db.set_task_confirmation(conn, t1, "Proceed?")

            db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="web", conversation_token="room1",
                queue="foreground",
            )
            other = db.create_task(
                conn, prompt="other room", user_id="user1",
                source_type="web", conversation_token="room2",
                queue="foreground",
            )

            # Same-room message is blocked behind the parked confirmation;
            # the other room's task is claimed instead.
            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == other

            # Resolving the confirmation (confirm → pending) unblocks room1.
            db.confirm_task(conn, t1)
            claimed = db.claim_task(conn, "worker-2", queue="foreground")
            assert claimed is not None
            assert claimed.id == t1


class TestClaimTaskInlineOnlyExclusion:
    """REPL tasks run inline via run_task_inline and must never be claimed by a
    daemon worker — otherwise a running daemon double-executes the pending row
    (second brain run + second deferred-op drain)."""

    def test_claim_skips_repl_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            repl_id = db.create_task(
                conn, prompt="hi", user_id="user1", source_type="repl",
                conversation_token="repl-user1-abcd1234", output_target="stream",
                queue="foreground",
            )
            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is None
            # The REPL row is still pending, untouched.
            assert db.get_task(conn, repl_id).status == "pending"

    def test_claim_returns_nonrepl_when_both_pending(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hi", user_id="user1", source_type="repl",
                conversation_token="repl-user1-abcd1234", output_target="stream",
                queue="foreground",
            )
            talk_id = db.create_task(
                conn, prompt="do", user_id="user1", source_type="talk",
                conversation_token="room1", queue="foreground",
            )
            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == talk_id

    def test_pending_user_discovery_excludes_repl(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hi", user_id="reploner", source_type="repl",
                conversation_token="repl-reploner-abcd1234",
                output_target="stream", queue="foreground",
            )
            assert "reploner" not in db.get_users_with_pending_tasks(conn)
            assert "reploner" not in db.get_users_with_pending_background_tasks(conn)
            assert "reploner" not in db.get_users_with_pending_fg_queue_tasks(conn)


class TestTaskColumnRoundTrip:
    """Every helper that returns a `Task` must preserve dispatch-relevant
    columns. A missing column in `claim_task`'s RETURNING clause masked
    `task.skill` and routed module-poller rows
    (`_module.feeds.run_scheduled`) through the LLM path with an empty
    prompt — producing unsolicited ntfy notifications every 5 minutes.

    Without `_TASK_COLUMNS` plus the strict `_row_to_task` (no `if X in
    row.keys()` fallback), the next column-conditional dispatch could
    repeat the bug. These tests trip immediately if a Task-returning
    SELECT/RETURNING omits any column from `_TASK_COLUMNS`.
    """

    # Columns that gate execution behavior — if any of these silently
    # become None on read, the scheduler routes the task incorrectly.
    DISPATCH_FIELDS = (
        "skill", "skill_args", "command", "model", "effort",
        "talk_delivery_token", "scheduled_job_id", "skip_log_channel",
        "queue", "heartbeat_silent", "output_target",
    )

    @staticmethod
    def _create_rich(conn, **overrides):
        """Create a task with every dispatch-relevant column set."""
        defaults = dict(
            prompt="rich",
            user_id="user1",
            source_type="scheduled",
            conversation_token="room1",
            queue="background",
            output_target="talk",
            skill="feeds",
            skill_args='["run-scheduled"]',
            command=None,
            model="claude-sonnet-4-6",
            effort="high",
            talk_delivery_token="real-talk-room",
            scheduled_job_id=42,
            heartbeat_silent=True,
            skip_log_channel=True,
        )
        defaults.update(overrides)
        return db.create_task(conn, **defaults)

    def _assert_dispatch_preserved(self, task):
        assert task is not None
        assert task.skill == "feeds"
        assert task.skill_args == '["run-scheduled"]'
        assert task.model == "claude-sonnet-4-6"
        assert task.effort == "high"
        assert task.talk_delivery_token == "real-talk-room"
        assert task.scheduled_job_id == 42
        assert task.heartbeat_silent is True
        assert task.skip_log_channel is True
        assert task.queue == "background"
        assert task.output_target == "talk"

    def test_claim_task(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            claimed = db.claim_task(conn, "worker-1", queue="background")
            assert claimed.id == tid
            self._assert_dispatch_preserved(claimed)

    def test_get_task(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            self._assert_dispatch_preserved(db.get_task(conn, tid))

    def test_get_pending_confirmation(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn, conversation_token="conf-room")
            conn.execute(
                "UPDATE tasks SET status='pending_confirmation' WHERE id=?", (tid,),
            )
            self._assert_dispatch_preserved(
                db.get_pending_confirmation(conn, "conf-room"),
            )

    def test_list_pending_confirmations_for_user(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            conn.execute(
                "UPDATE tasks SET status='pending_confirmation' WHERE id=?", (tid,),
            )
            self._assert_dispatch_preserved(
                db.list_pending_confirmations_for_user(conn, "user1")[0],
            )

    def test_get_pending_confirmation_by_response_id(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            conn.execute(
                "UPDATE tasks SET status='pending_confirmation', "
                "talk_response_id=12345 WHERE id=?",
                (tid,),
            )
            self._assert_dispatch_preserved(
                db.get_pending_confirmation_by_response_id(conn, 12345),
            )

    def test_get_reply_parent_task(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(
                conn, conversation_token="reply-room", source_type="talk",
            )
            conn.execute(
                "UPDATE tasks SET status='completed', result='ok', "
                "talk_message_id=777 WHERE id=?",
                (tid,),
            )
            self._assert_dispatch_preserved(
                db.get_reply_parent_task(conn, "reply-room", 777),
            )

    def test_get_stale_pending_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            conn.execute(
                "UPDATE tasks SET created_at=datetime('now', '-10 minutes') "
                "WHERE id=?",
                (tid,),
            )
            stale = db.get_stale_pending_tasks(conn, warn_minutes=5)
            assert len(stale) == 1
            self._assert_dispatch_preserved(stale[0])

    def test_get_completed_channel_tasks_since(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn, conversation_token="ch-room")
            conn.execute(
                "UPDATE tasks SET status='completed', result='ok', "
                "completed_at=datetime('now') WHERE id=?",
                (tid,),
            )
            tasks = db.get_completed_channel_tasks_since(
                conn, "ch-room", "2000-01-01T00:00:00",
            )
            assert len(tasks) == 1
            self._assert_dispatch_preserved(tasks[0])

    def test_get_completed_tasks_since(self, db_path):
        with db.get_db(db_path) as conn:
            tid = self._create_rich(conn)
            conn.execute(
                "UPDATE tasks SET status='completed', result='ok', "
                "completed_at=datetime('now') WHERE id=?",
                (tid,),
            )
            tasks = db.get_completed_tasks_since(
                conn, "user1", "2000-01-01T00:00:00",
            )
            assert len(tasks) == 1
            self._assert_dispatch_preserved(tasks[0])

    def test_list_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_rich(conn)
            tasks = db.list_tasks(conn, user_id="user1")
            assert len(tasks) == 1
            self._assert_dispatch_preserved(tasks[0])


class TestCreateTaskTalkDedup:
    def test_duplicate_talk_message_id_returns_existing(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            task2 = db.create_task(
                conn, prompt="hello again", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            assert task2 == task1

    def test_same_message_id_different_conversation_creates_new(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            task2 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room2", talk_message_id=999,
            )
            assert task2 != task1

    def test_null_talk_message_id_allows_duplicates(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="job1", user_id="alice",
            )
            task2 = db.create_task(
                conn, prompt="job2", user_id="alice",
            )
            assert task2 != task1


class TestCountPendingTasksForUserQueue:
    def test_counts_pending_fg_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 2

    def test_ignores_other_user(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="bob", queue="foreground")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_ignores_other_queue(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="background")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_ignores_completed_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.update_task_status(conn, task_id, "completed", result="done")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 0

    def test_zero_when_no_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 0


class TestCountClaimableTasksForUserQueue:
    """count_claimable_tasks_for_user_queue mirrors claim_task's claimability,
    diverging from the raw pending count exactly where claim_task would refuse."""

    def test_matches_raw_when_nothing_gated(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice",
                           conversation_token="room1", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice",
                           conversation_token="room2", queue="foreground")
            # Two ungated rooms — both claimable, same as the raw count.
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 2
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 2

    def test_same_room_followup_behind_active_is_not_claimable(self, db_path):
        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="turn2", user_id="alice",
                           conversation_token="room1", queue="foreground")
            # Raw sees the queued follow-up; claimable sees it's gated → 0.
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 0

    def test_other_room_task_still_counts_while_one_room_active(self, db_path):
        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="r1-followup", user_id="alice",
                           conversation_token="room1", queue="foreground")
            db.create_task(conn, prompt="r2-task", user_id="alice",
                           conversation_token="room2", queue="foreground")
            # room1 follow-up gated, room2 task free → exactly 1 claimable.
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_pending_confirmation_parks_the_room(self, db_path):
        with db.get_db(db_path) as conn:
            parked = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, parked, "pending_confirmation")
            db.create_task(conn, prompt="turn2", user_id="alice",
                           conversation_token="room1", queue="foreground")
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 0

    def test_cancelled_active_does_not_gate(self, db_path):
        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            conn.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (active,))
            conn.commit()
            db.create_task(conn, prompt="turn2", user_id="alice",
                           conversation_token="room1", queue="foreground")
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_unblocks_after_active_completes(self, db_path):
        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="turn1", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="turn2", user_id="alice",
                           conversation_token="room1", queue="foreground")
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 0
            db.update_task_status(conn, active, "completed", result="ok")
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_background_queue_ignores_gate(self, db_path):
        with db.get_db(db_path) as conn:
            active = db.create_task(conn, prompt="fg", user_id="alice",
                                    conversation_token="room1", queue="foreground")
            db.update_task_status(conn, active, "running")
            db.create_task(conn, prompt="bg", user_id="alice",
                           conversation_token="room1", queue="background")
            # The fg gate never applies to the background queue.
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "background") == 1

    def test_excludes_inline_only_source_types(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="repl line", user_id="alice",
                           source_type="repl", queue="foreground")
            # REPL tasks run inline and are never claimed by the daemon.
            assert db.count_claimable_tasks_for_user_queue(conn, "alice", "foreground") == 0
            # Raw count does not exclude them — that's the divergence.
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1


class TestGetPreviousTasks:
    """Tests for get_previous_tasks (returns last N tasks unfiltered by source_type)."""

    def _create_completed(self, conn, prompt, token="room1", source_type="talk"):
        task_id = db.create_task(
            conn, prompt=prompt, user_id="alice",
            conversation_token=token, source_type=source_type,
        )
        db.update_task_status(conn, task_id, "completed", result=f"result-{task_id}")
        return task_id

    def test_returns_empty_list_when_no_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_previous_tasks(conn, "room1")
            assert result == []

    def test_returns_tasks_in_oldest_first_order(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1, id2, id3]

    def test_respects_limit(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", limit=2)
            assert [m.id for m in result] == [id2, id3]

    def test_respects_exclude_task_id(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", exclude_task_id=id3, limit=3)
            assert [m.id for m in result] == [id1, id2]

    def test_scoped_by_conversation_token(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "other room", token="room2")
            id2 = self._create_completed(conn, "this room")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id2]

    def test_includes_scheduled_and_briefing_source_types(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "scheduled", source_type="scheduled")
            id2 = self._create_completed(conn, "briefing", source_type="briefing")
            id3 = self._create_completed(conn, "talk", source_type="talk")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1, id2, id3]

    def test_returns_fewer_than_limit_when_not_enough(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "only one")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1]

    def test_default_limit_is_three(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "t1")
            self._create_completed(conn, "t2")
            id3 = self._create_completed(conn, "t3")
            id4 = self._create_completed(conn, "t4")
            id5 = self._create_completed(conn, "t5")
            # Default limit=3 should return the last 3
            result = db.get_previous_tasks(conn, "room1")
            assert [m.id for m in result] == [id3, id4, id5]


class TestTalkMessageCache:
    """Tests for the talk_messages cache DB functions."""

    def _make_msg(self, id, actor_id="alice", message="hello", timestamp=1000,
                  message_params=None, deleted=False, parent_id=None,
                  reference_id=None, actor_display_name="Alice",
                  actor_type="users", message_type="comment"):
        msg = {
            "id": id,
            "actorId": actor_id,
            "actorDisplayName": actor_display_name,
            "actorType": actor_type,
            "message": message,
            "messageType": message_type,
            "messageParameters": message_params if message_params is not None else {},
            "timestamp": timestamp,
            "referenceId": reference_id,
            "deleted": deleted,
        }
        if parent_id is not None:
            msg["parent"] = {"id": parent_id}
        return msg

    def test_upsert_and_retrieve(self, db_path):
        with db.get_db(db_path) as conn:
            msgs = [
                self._make_msg(1, timestamp=100, message="first"),
                self._make_msg(2, timestamp=200, message="second"),
            ]
            count = db.upsert_talk_messages(conn, "room1", msgs)
            assert count == 2

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 2
            # Oldest first
            assert result[0]["id"] == 1
            assert result[0]["message"] == "first"
            assert result[1]["id"] == 2
            assert result[1]["message"] == "second"

    def test_upsert_replaces_on_conflict(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="original"),
            ])
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="updated"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            assert result[0]["message"] == "updated"

    def test_upsert_preserves_result_reference_id(self, db_path):
        """Poller upserts should not overwrite :result tags set by scheduler."""
        with db.get_db(db_path) as conn:
            # Scheduler caches a result message
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="Done!", reference_id="istota:task:5:result"),
            ])
            # Poller later upserts the same message with :progress tag
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="Done!", reference_id="istota:task:5:progress"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            # :result tag should be preserved
            assert result[0]["referenceId"] == "istota:task:5:result"

    def test_upsert_updates_non_result_reference_id(self, db_path):
        """Upserts should update reference_id when existing is not :result."""
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, reference_id="istota:task:5:progress"),
            ])
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, reference_id="istota:task:5:result"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["referenceId"] == "istota:task:5:result"

    def test_get_cached_limit_and_order(self, db_path):
        with db.get_db(db_path) as conn:
            msgs = [self._make_msg(i, timestamp=i * 100) for i in range(1, 21)]
            db.upsert_talk_messages(conn, "room1", msgs)

            result = db.get_cached_talk_messages(conn, "room1", limit=10)
            assert len(result) == 10
            # Should be the 10 most recent, in oldest-first order
            assert result[0]["id"] == 11
            assert result[-1]["id"] == 20

    def test_reconstructed_dict_format(self, db_path):
        """Verify returned dicts match raw API format for build_talk_context()."""
        with db.get_db(db_path) as conn:
            msg = self._make_msg(
                42,
                actor_id="bob",
                actor_display_name="Bob",
                message="test msg",
                timestamp=1700000000,
                reference_id="istota:task:5:result",
                message_params={"file0": {"name": "photo.jpg", "type": "file"}},
                parent_id=40,
                deleted=False,
            )
            db.upsert_talk_messages(conn, "room1", [msg])

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            r = result[0]
            assert r["id"] == 42
            assert r["actorId"] == "bob"
            assert r["actorDisplayName"] == "Bob"
            assert r["message"] == "test msg"
            assert r["timestamp"] == 1700000000
            assert r["referenceId"] == "istota:task:5:result"
            assert r["messageParameters"] == {"file0": {"name": "photo.jpg", "type": "file"}}
            assert r["parent"] == {"id": 40}
            assert r["deleted"] is False

    def test_has_cached_talk_messages(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.has_cached_talk_messages(conn, "room1") is False
            db.upsert_talk_messages(conn, "room1", [self._make_msg(1)])
            assert db.has_cached_talk_messages(conn, "room1") is True
            # Different room still empty
            assert db.has_cached_talk_messages(conn, "room2") is False

    def test_cleanup_old_messages(self, db_path):
        with db.get_db(db_path) as conn:
            # Insert 5 messages for room1
            msgs = [self._make_msg(i, timestamp=i * 100) for i in range(1, 6)]
            db.upsert_talk_messages(conn, "room1", msgs)

            # Cap at 3 per conversation — should delete the 2 oldest
            deleted = db.cleanup_old_talk_messages(conn, max_per_conversation=3)
            assert deleted == 2

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 3
            assert result[0]["id"] == 3
            assert result[1]["id"] == 4
            assert result[2]["id"] == 5

    def test_cleanup_per_conversation_independent(self, db_path):
        with db.get_db(db_path) as conn:
            # 4 messages in room1, 2 in room2
            db.upsert_talk_messages(conn, "room1",
                [self._make_msg(i, timestamp=i * 100) for i in range(1, 5)])
            db.upsert_talk_messages(conn, "room2",
                [self._make_msg(10 + i, timestamp=i * 100) for i in range(1, 3)])

            # Cap at 2 per conversation
            deleted = db.cleanup_old_talk_messages(conn, max_per_conversation=2)
            assert deleted == 2  # only room1 has excess

            r1 = db.get_cached_talk_messages(conn, "room1")
            assert len(r1) == 2
            assert r1[0]["id"] == 3
            assert r1[1]["id"] == 4

            r2 = db.get_cached_talk_messages(conn, "room2")
            assert len(r2) == 2  # unchanged

    def test_message_parameters_json_roundtrip(self, db_path):
        """Both dict and list messageParameters survive serialization."""
        with db.get_db(db_path) as conn:
            # Dict params
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message_params={"key": "value"}),
            ])
            # List params (Talk API can return empty list)
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(2, message_params=[]),
            ])

            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["messageParameters"] == {"key": "value"}
            assert result[1]["messageParameters"] == []

    def test_upsert_empty_list_returns_zero(self, db_path):
        with db.get_db(db_path) as conn:
            count = db.upsert_talk_messages(conn, "room1", [])
            assert count == 0

    def test_deleted_message_flag(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, deleted=True),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["deleted"] is True

    def test_no_parent_omits_key(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1),  # No parent_id
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert "parent" not in result[0]


# =============================================================================
# TestSentEmails
# =============================================================================


class TestSentEmails:
    def test_record_and_find_by_message_id(self, db_path):
        with db.get_db(db_path) as conn:
            rid = db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<abc123@example.com>",
                to_addr="bob@example.com",
                subject="Meeting request",
                task_id=None,
                conversation_token="room42",
            )
            assert rid > 0

            found = db.find_sent_email_by_message_id(conn, "<abc123@example.com>")
            assert found is not None
            assert found.user_id == "carol"
            assert found.to_addr == "bob@example.com"
            assert found.subject == "Meeting request"
            assert found.conversation_token == "room42"

    def test_find_by_message_id_not_found(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.find_sent_email_by_message_id(conn, "<nope@nope>") is None

    def test_find_by_references(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<msg1@example.com>",
                to_addr="alice@example.com",
                subject="Hello",
                conversation_token="room1",
            )
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<msg2@example.com>",
                to_addr="bob@example.com",
                subject="Other",
                conversation_token="room2",
            )

            # References list containing one of our sent message IDs
            found = db.find_sent_email_by_references(
                conn, ["<unknown@x.com>", "<msg1@example.com>"]
            )
            assert found is not None
            assert found.message_id == "<msg1@example.com>"

    def test_find_by_references_empty_list(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.find_sent_email_by_references(conn, []) is None

    def test_find_by_references_no_match(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<msg1@example.com>",
                to_addr="alice@example.com",
                subject="Hello",
            )
            assert db.find_sent_email_by_references(conn, ["<other@x.com>"]) is None

    def test_origin_target_round_trips(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<origin1@example.com>",
                to_addr="bob@example.com",
                conversation_token="rm_web123",
                origin_target="web:rm_web123",
            )
            by_id = db.find_sent_email_by_message_id(conn, "<origin1@example.com>")
            assert by_id is not None
            assert by_id.origin_target == "web:rm_web123"
            by_ref = db.find_sent_email_by_references(conn, ["<origin1@example.com>"])
            assert by_ref is not None
            assert by_ref.origin_target == "web:rm_web123"

    def test_origin_target_defaults_none(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<noorigin@example.com>",
                to_addr="bob@example.com",
            )
            found = db.find_sent_email_by_message_id(conn, "<noorigin@example.com>")
            assert found is not None
            assert found.origin_target is None

    def test_find_by_references_returns_most_recent(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<old@example.com>",
                to_addr="alice@example.com",
                subject="Old",
                conversation_token="room1",
            )
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<new@example.com>",
                to_addr="alice@example.com",
                subject="New",
                conversation_token="room2",
            )

            # Both match — should return the more recent one
            found = db.find_sent_email_by_references(
                conn, ["<old@example.com>", "<new@example.com>"]
            )
            assert found is not None
            assert found.message_id == "<new@example.com>"

    def test_record_with_all_fields(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="send email", user_id="carol")
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<full@example.com>",
                to_addr="bob@example.com",
                subject="Re: Meeting",
                task_id=task_id,
                thread_id="abc123",
                in_reply_to="<original@example.com>",
                references="<original@example.com>",
                conversation_token="room5",
            )

            found = db.find_sent_email_by_message_id(conn, "<full@example.com>")
            assert found.task_id == task_id
            assert found.thread_id == "abc123"
            assert found.in_reply_to == "<original@example.com>"
            assert found.references == "<original@example.com>"

    def test_record_with_talk_delivery_token(self, db_path):
        # ISSUE-057: sent_emails carries the originating task's resolved Talk
        # room so thread-match follow-ups inherit a real channel without
        # re-resolving.
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<dt@example.com>",
                to_addr="bob@example.com",
                subject="Hi",
                conversation_token="thread_hash",
                talk_delivery_token="real_room",
            )

            found = db.find_sent_email_by_message_id(conn, "<dt@example.com>")
            assert found.conversation_token == "thread_hash"
            assert found.talk_delivery_token == "real_room"

            via_refs = db.find_sent_email_by_references(conn, ["<dt@example.com>"])
            assert via_refs is not None
            assert via_refs.talk_delivery_token == "real_room"


class TestListSentMessageIds:
    """ISSUE-252: the message ids feeding the read-side `--scope mine` thread arm."""

    def _record(self, conn, user_id, message_id, sent_at=None):
        db.record_sent_email(
            conn, user_id=user_id, message_id=message_id, to_addr="peer@example.com",
        )
        if sent_at is not None:
            conn.execute(
                "UPDATE sent_emails SET sent_at = ? WHERE message_id = ?",
                (sent_at, message_id),
            )

    def test_returns_only_the_named_users_ids(self, db_path):
        with db.get_db(db_path) as conn:
            self._record(conn, "carol", "<c1@example.com>")
            self._record(conn, "dave", "<d1@example.com>")
            ids = db.list_sent_message_ids(conn, "carol")
            assert ids == ["<c1@example.com>"]

    def test_most_recent_first_and_capped_by_limit(self, db_path):
        with db.get_db(db_path) as conn:
            self._record(conn, "carol", "<old@example.com>", "2026-01-01 00:00:00")
            self._record(conn, "carol", "<mid@example.com>", "2026-02-01 00:00:00")
            self._record(conn, "carol", "<new@example.com>", "2026-03-01 00:00:00")
            assert db.list_sent_message_ids(conn, "carol", limit=2) == [
                "<new@example.com>", "<mid@example.com>",
            ]

    def test_age_alone_never_excludes_an_id(self, db_path):
        # The limit is the only bound. A date window on top of it could only
        # remove ids the limit would have kept, so there isn't one.
        with db.get_db(db_path) as conn:
            self._record(conn, "carol", "<ancient@example.com>", "2019-01-01 00:00:00")
            assert db.list_sent_message_ids(conn, "carol") == ["<ancient@example.com>"]

    def test_a_repeated_id_spends_one_slot_not_two(self, db_path):
        # The cap should mean N distinct threads; message_id has no unique
        # constraint, so a re-recorded id would otherwise consume the budget twice.
        with db.get_db(db_path) as conn:
            self._record(conn, "carol", "<dup@example.com>", "2026-01-01 00:00:00")
            self._record(conn, "carol", "<dup@example.com>", "2026-03-01 00:00:00")
            self._record(conn, "carol", "<other@example.com>", "2026-02-01 00:00:00")
            assert db.list_sent_message_ids(conn, "carol", limit=2) == [
                "<dup@example.com>", "<other@example.com>",
            ]

    def test_blank_message_ids_are_skipped(self, db_path):
        # A blank id would become `HEADER "References" ""`, which matches every
        # message carrying the header at all.
        with db.get_db(db_path) as conn:
            self._record(conn, "carol", "")
            self._record(conn, "carol", "<real@example.com>")
            assert db.list_sent_message_ids(conn, "carol") == ["<real@example.com>"]

    def test_no_sends_returns_empty(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.list_sent_message_ids(conn, "carol") == []


class TestTaskTalkDeliveryToken:
    """ISSUE-057: tasks carry talk_delivery_token separate from conversation_token."""

    def test_create_task_persists_talk_delivery_token(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice",
                source_type="email",
                conversation_token="deadbeef12345678",  # synthetic email-thread hash
                talk_delivery_token="real_room",
            )
            task = db.get_task(conn, task_id)
            assert task.conversation_token == "deadbeef12345678"
            assert task.talk_delivery_token == "real_room"

    def test_create_task_default_talk_delivery_token_is_none(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            task = db.get_task(conn, task_id)
            assert task.talk_delivery_token is None


# =============================================================================
# TestConfirmedAt
# =============================================================================


class TestConfirmedAt:
    def test_confirmed_at_roundtrip(self, db_path):
        """confirm_task sets confirmed_at, get_task reads it back."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                conversation_token="room1",
            )
            # Set to pending_confirmation first (confirm_task requires this status)
            db.set_task_confirmation(conn, task_id, "Should I proceed?")
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is None
            assert task.confirmation_prompt == "Should I proceed?"

            # Confirm it
            db.confirm_task(conn, task_id)
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is not None
            assert task.status == "pending"

    def test_unconfirmed_task_has_none(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Normal", user_id="alice")
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is None


# =============================================================================
# TestCancelPendingConfirmations
# =============================================================================


class TestCancelPendingConfirmations:
    def test_cancels_pending_confirmation_in_conversation(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Draft email", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Send this?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            task = db.get_task(conn, task_id)
            assert task.status == "cancelled"

    def test_does_not_cancel_other_users(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Alice draft", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Send?")

            t2 = db.create_task(
                conn, prompt="Bob draft", user_id="bob",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t2, "Send?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            # Alice's cancelled, Bob's still pending
            assert db.get_task(conn, t1).status == "cancelled"
            assert db.get_task(conn, t2).status == "pending_confirmation"

    def test_does_not_cancel_other_conversations(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Draft", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Send?")

            t2 = db.create_task(
                conn, prompt="Other draft", user_id="alice",
                conversation_token="room2",
            )
            db.set_task_confirmation(conn, t2, "Send?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            assert db.get_task(conn, t1).status == "cancelled"
            assert db.get_task(conn, t2).status == "pending_confirmation"

    def test_returns_zero_when_nothing_to_cancel(self, db_path):
        with db.get_db(db_path) as conn:
            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 0


class TestListPendingConfirmationsForUser:
    """Replaced `get_pending_confirmation_for_user`, which returned only the
    newest — the shape that let a bare "yes" answer the wrong gate during a
    burst (ISSUE-241). Same scoping properties, plural, oldest first."""

    def test_returns_every_open_question_oldest_first(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="older", user_id="alice", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, t1, "Confirm older?")
            t2 = db.create_task(
                conn, prompt="newer", user_id="alice", conversation_token="thread2",
            )
            db.set_task_confirmation(conn, t2, "Confirm newer?")

            result = db.list_pending_confirmations_for_user(conn, "alice")
            assert [t.id for t in result] == [t1, t2]

    def test_returns_empty_when_no_pending(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.list_pending_confirmations_for_user(conn, "alice") == []

    def test_ignores_other_users(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="bob's task", user_id="bob", conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")

            assert db.list_pending_confirmations_for_user(conn, "alice") == []

    def test_ignores_non_confirmation_statuses(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="pending task", user_id="alice", conversation_token="room1",
            )
            # Task stays in 'pending' status, not 'pending_confirmation'
            assert db.list_pending_confirmations_for_user(conn, "alice") == []


class TestGetPendingConfirmationByResponseId:
    def test_returns_matching_task(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="gated email", user_id="alice", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")
            db.update_talk_response_id(conn, t1, 42)

            result = db.get_pending_confirmation_by_response_id(conn, 42)
            assert result is not None
            assert result.id == t1

    def test_returns_none_when_no_match(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_pending_confirmation_by_response_id(conn, 999)
            assert result is None

    def test_ignores_non_confirmation_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="completed task", user_id="alice", conversation_token="room1",
            )
            db.update_talk_response_id(conn, t1, 42)
            # Task is 'pending', not 'pending_confirmation'
            result = db.get_pending_confirmation_by_response_id(conn, 42)
            assert result is None


class TestFailStuckLockedRunningTasks:
    def test_fails_old_locked_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            # Simulate a task locked 60+ minutes ago, created 90+ minutes ago
            conn.execute(
                """UPDATE tasks SET status = 'locked',
                   locked_at = datetime('now', '-45 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 1
            assert failed[0]["id"] == task_id
            assert db.get_task(conn, task_id).status == "failed"

    def test_releases_recent_locked_task_for_retry(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            # Locked 45 min ago but created recently (within max_retry_age)
            conn.execute(
                """UPDATE tasks SET status = 'locked',
                   locked_at = datetime('now', '-45 minutes'),
                   created_at = datetime('now', '-30 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 0  # released, not failed
            assert db.get_task(conn, task_id).status == "pending"

    def test_fails_old_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-20 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 1
            task = db.get_task(conn, task_id)
            assert task.status == "failed"

    def test_no_stuck_tasks_returns_empty(self, db_path):
        with db.get_db(db_path) as conn:
            # Create a normal pending task
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            failed = db.fail_stuck_locked_running_tasks(conn)
            assert failed == []

    def test_unblocks_channel_gate(self, db_path):
        """After recovering a stuck task, the channel gate should be clear."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-20 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            assert db.has_active_foreground_task_for_channel(conn, "room1") is True
            db.fail_stuck_locked_running_tasks(conn)
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False


class TestRecoverOrphanedTasks:
    """Startup orphan recovery: under the daemon flock every 'running'/'locked'
    row is an orphan of a dead instance, recovered immediately rather than
    waiting out worker_stuck_minutes."""

    def _make_running(self, conn, **kw):
        defaults = dict(
            prompt="hello", user_id="alice",
            conversation_token="room1", queue="foreground",
        )
        defaults.update(kw)
        source_type = defaults.pop("source_type", "web")
        task_id = db.create_task(conn, source_type=source_type, **defaults)
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = datetime('now'), "
            "last_heartbeat = datetime('now'), locked_at = datetime('now'), "
            "locked_by = 'dead-host-123', worker_pid = 999 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        return task_id

    def test_releases_retry_eligible_task_to_pending(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn)

            recovered = db.recover_orphaned_tasks(conn)

            assert len(recovered) == 1
            assert recovered[0]["id"] == task_id
            assert recovered[0]["action"] == "released"
            task = db.get_task(conn, task_id)
            assert task.status == "pending"
            assert task.attempt_count == 1  # bumped from 0
            # Liveness columns cleared so the stuck predicate can't re-fire.
            row = conn.execute(
                "SELECT last_heartbeat, started_at, locked_at, locked_by, worker_pid "
                "FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert tuple(row) == (None, None, None, None, None)

    def test_locked_orphan_also_recovered(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice", queue="foreground",
            )
            conn.execute(
                "UPDATE tasks SET status = 'locked', locked_at = datetime('now'), "
                "locked_by = 'dead-host' WHERE id = ?", (task_id,),
            )
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn)
            assert len(recovered) == 1
            assert db.get_task(conn, task_id).status == "pending"

    def test_cancelled_orphan_marked_cancelled_not_requeued(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn)
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,)
            )
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn)

            assert recovered[0]["action"] == "cancelled"
            task = db.get_task(conn, task_id)
            assert task.status == "cancelled"
            assert task.attempt_count == 0  # not bumped — never re-queued

    def test_exhausted_orphan_marked_failed(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn)
            conn.execute(
                "UPDATE tasks SET attempt_count = 3, max_attempts = 3 WHERE id = ?",
                (task_id,),
            )
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn)

            assert recovered[0]["action"] == "failed"
            assert db.get_task(conn, task_id).status == "failed"

    def test_too_old_orphan_marked_failed(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn)
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-120 minutes') "
                "WHERE id = ?", (task_id,),
            )
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn, max_retry_age_minutes=60)

            assert recovered[0]["action"] == "failed"
            assert db.get_task(conn, task_id).status == "failed"

    def test_inline_only_orphan_failed_not_released(self, db_path):
        """REPL tasks run inline in a separate process; the daemon never claims
        them, so a released one would sit pending forever. Fail instead."""
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn, source_type="repl", conversation_token=None)

            recovered = db.recover_orphaned_tasks(conn)

            assert recovered[0]["action"] == "failed"
            assert db.get_task(conn, task_id).status == "failed"

    def test_pending_confirmation_untouched(self, db_path):
        """A task awaiting the user's confirmation is not an orphan."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            conn.execute(
                "UPDATE tasks SET status = 'pending_confirmation' WHERE id = ?",
                (task_id,),
            )
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn)
            assert recovered == []
            assert db.get_task(conn, task_id).status == "pending_confirmation"

    def test_pending_and_terminal_tasks_untouched(self, db_path):
        with db.get_db(db_path) as conn:
            pending = db.create_task(conn, prompt="a", user_id="alice")
            done = db.create_task(conn, prompt="b", user_id="alice")
            db.update_task_status(conn, done, "completed", result="ok")
            conn.commit()

            recovered = db.recover_orphaned_tasks(conn)
            assert recovered == []
            assert db.get_task(conn, pending).status == "pending"
            assert db.get_task(conn, done).status == "completed"

    def test_no_orphans_returns_empty(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
            assert db.recover_orphaned_tasks(conn) == []


class TestStuckRunningThreshold:
    """ISSUE-112: a 'running' task must not be reclaimed before it has had a
    chance to hit its own timeout. The reclaim window is configurable
    (task_timeout_minutes + grace) instead of a flat 15 min that sits below the
    30-min default timeout — otherwise a slow-but-healthy worker (notably the
    in-process native brain, which has no killable PID) gets reclaimed and a
    second worker runs a duplicate.
    """

    def _make_running(self, conn, *, started_min_ago, created_min_ago=10):
        task_id = db.create_task(
            conn, prompt="hello", user_id="alice",
            conversation_token="room1", queue="foreground",
        )
        conn.execute(
            """UPDATE tasks SET status = 'running',
               started_at = datetime('now', ? || ' minutes'),
               created_at = datetime('now', ? || ' minutes')
            WHERE id = ?""",
            (f"-{started_min_ago}", f"-{created_min_ago}", task_id),
        )
        conn.commit()
        return task_id

    def test_maintenance_keeps_healthy_long_running_task(self, db_path):
        # 20 min in, threshold 35 (30 timeout + 5 grace): still healthy.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn, started_min_ago=20)
            failed = db.fail_stuck_locked_running_tasks(
                conn, stuck_running_minutes=35,
            )
            assert failed == []
            task = db.get_task(conn, task_id)
            assert task.status == "running"
            assert task.attempt_count == 0

    def test_maintenance_reclaims_past_threshold(self, db_path):
        # 40 min in, past 35-min threshold, created recently → released for retry.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn, started_min_ago=40)
            db.fail_stuck_locked_running_tasks(conn, stuck_running_minutes=35)
            task = db.get_task(conn, task_id)
            assert task.status == "pending"
            assert task.attempt_count == 1

    def test_claim_task_keeps_healthy_long_running_task(self, db_path):
        # claim_task's inline recovery must not reclaim a healthy 20-min task.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn, started_min_ago=20)
            db.claim_task(conn, "worker-1", stuck_running_minutes=35)
            task = db.get_task(conn, task_id)
            assert task.status == "running"
            assert task.attempt_count == 0

    def test_default_threshold_preserves_legacy_behavior(self, db_path):
        # Callers that don't pass the param keep the old 15-min behavior.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(conn, started_min_ago=20)
            db.fail_stuck_locked_running_tasks(conn)  # default 15
            task = db.get_task(conn, task_id)
            assert task.status == "pending"  # reclaimed at the legacy threshold
            assert task.attempt_count == 1


class TestReclaimedTaskNotDoubleClaimed:
    """A task reclaimed from the stuck-running path must not be re-claimed by a
    second concurrent worker during the window before its new worker's first
    heartbeat ping.

    A restart left a task 'running' with a stale last_heartbeat. Two fg workers
    called claim_task within milliseconds: A reclaimed + ran it, but the row
    still carried the dead worker's old last_heartbeat, so the
    _STUCK_RUNNING_PREDICATE kept firing and B re-stole and re-ran it — two
    answers for one task id. claim_task must clear last_heartbeat (+ started_at)
    on claim so the new owner starts with a clean liveness slate.
    """

    def _make_stale_running(self, conn):
        task_id = db.create_task(
            conn, prompt="hello", user_id="alice",
            conversation_token="room1", queue="foreground",
        )
        # Dead worker: running, heartbeat 10 min silent (> default 5 min gate),
        # created recently so it's release-eligible (not fail-too-old).
        conn.execute(
            """UPDATE tasks SET status = 'running',
               started_at = datetime('now', '-10 minutes'),
               last_heartbeat = datetime('now', '-10 minutes'),
               created_at = datetime('now', '-10 minutes')
            WHERE id = ?""",
            (task_id,),
        )
        conn.commit()
        return task_id

    def test_second_claim_does_not_resteal_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_stale_running(conn)

            # Worker A reclaims the stuck task.
            a = db.claim_task(conn, "worker-A", queue="foreground")
            assert a is not None and a.id == task_id
            # Liveness slate is clean after claim (the fix).
            hb, started = conn.execute(
                "SELECT last_heartbeat, started_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert hb is None
            assert started is None

            # A transitions it to running (sets started_at=now), before any ping.
            db.update_task_status(conn, task_id, "running")

            # Worker B claims immediately — must NOT re-steal the same task.
            b = db.claim_task(conn, "worker-B", queue="foreground")
            assert b is None or b.id != task_id, (
                "second worker re-stole the running task (duplicate execution)"
            )
            # The task is still running under worker A, untouched by B.
            after = db.get_task(conn, task_id)
            assert after.status == "running"

    def test_retry_path_does_not_double_claim(self, db_path):
        """The retry route (set_task_pending_retry), not just the stuck-running
        path: a task that ran (recorded a heartbeat), failed, and was scheduled
        for retry must not carry its stale heartbeat into the next claim and get
        re-stolen mid-run. Scully reproduced this as a distinct double-claim path
        the claim-site liveness reset uniquely defends.
        """
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            # Simulate a first attempt that ran and recorded a (now stale) ping.
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-10 minutes'),
                   last_heartbeat = datetime('now', '-10 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()
            # Attempt fails → scheduled for immediate retry (0 min delay).
            db.set_task_pending_retry(conn, task_id, "boom", 0)

            a = db.claim_task(conn, "worker-A", queue="foreground")
            assert a is not None and a.id == task_id
            db.update_task_status(conn, task_id, "running")

            b = db.claim_task(conn, "worker-B", queue="foreground")
            assert b is None or b.id != task_id, (
                "retry-path task re-stolen by a second worker (duplicate run)"
            )

    def test_fail_stuck_release_clears_heartbeat(self, db_path):
        """The maintenance pass's stuck-running release must clear last_heartbeat
        so the released row can't immediately re-qualify as stuck on re-claim."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-40 minutes'),
                   last_heartbeat = datetime('now', '-40 minutes'),
                   created_at = datetime('now', '-40 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()
            db.fail_stuck_locked_running_tasks(conn, stuck_running_minutes=35)
            status, hb, started = conn.execute(
                "SELECT status, last_heartbeat, started_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert status == "pending"
            assert hb is None
            assert started is None


class TestHeartbeatReclaim:
    """ISSUE-112 heartbeat: reclaim keys on worker liveness (last_heartbeat),
    not raw runtime. A live worker that keeps pinging is never reclaimed no
    matter how long it runs; a worker whose heartbeat goes silent is reclaimed
    quickly, independent of the task timeout."""

    def _make_running(self, conn, *, started_min_ago, heartbeat_min_ago, created_min_ago=10):
        task_id = db.create_task(
            conn, prompt="hi", user_id="alice",
            conversation_token="room1", queue="foreground",
        )
        hb = (
            f"datetime('now', '-{heartbeat_min_ago} minutes')"
            if heartbeat_min_ago is not None else "NULL"
        )
        conn.execute(
            f"""UPDATE tasks SET status = 'running',
               started_at = datetime('now', ? || ' minutes'),
               created_at = datetime('now', ? || ' minutes'),
               last_heartbeat = {hb}
            WHERE id = ?""",
            (f"-{started_min_ago}", f"-{created_min_ago}", task_id),
        )
        conn.commit()
        return task_id

    def _status(self, conn, task_id):
        return db.get_task(conn, task_id).status

    def test_fresh_heartbeat_survives_long_runtime(self, db_path):
        # Running 40 min (past the 35-min fallback), but pinged 1 min ago.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(
                conn, started_min_ago=40, heartbeat_min_ago=1,
            )
            failed = db.fail_stuck_locked_running_tasks(
                conn, stuck_running_minutes=35, heartbeat_stuck_minutes=5,
            )
            assert failed == []
            assert self._status(conn, task_id) == "running"
            assert db.get_task(conn, task_id).attempt_count == 0

    def test_stale_heartbeat_reclaimed_quickly(self, db_path):
        # Only 10 min in (under the 35-min fallback) but silent for 8 min.
        with db.get_db(db_path) as conn:
            task_id = self._make_running(
                conn, started_min_ago=10, heartbeat_min_ago=8,
            )
            db.fail_stuck_locked_running_tasks(
                conn, stuck_running_minutes=35, heartbeat_stuck_minutes=5,
            )
            assert self._status(conn, task_id) == "pending"  # released for retry
            assert db.get_task(conn, task_id).attempt_count == 1

    def test_null_heartbeat_uses_started_at_fallback(self, db_path):
        # No heartbeat ever recorded → fall back to the started_at window.
        with db.get_db(db_path) as conn:
            kept = self._make_running(
                conn, started_min_ago=20, heartbeat_min_ago=None,
            )
            reclaimed = self._make_running(
                conn, started_min_ago=40, heartbeat_min_ago=None,
            )
            db.fail_stuck_locked_running_tasks(
                conn, stuck_running_minutes=35, heartbeat_stuck_minutes=5,
            )
            assert self._status(conn, kept) == "running"
            assert self._status(conn, reclaimed) == "pending"

    def test_claim_task_respects_fresh_heartbeat(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = self._make_running(
                conn, started_min_ago=40, heartbeat_min_ago=1,
            )
            db.claim_task(
                conn, "worker-1", stuck_running_minutes=35, heartbeat_stuck_minutes=5,
            )
            assert self._status(conn, task_id) == "running"


class TestTouchTaskHeartbeat:
    def _heartbeat(self, conn, task_id):
        return conn.execute(
            "SELECT last_heartbeat FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]

    def test_updates_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            db.update_task_status(conn, task_id, "running")
            assert self._heartbeat(conn, task_id) is None
            db.touch_task_heartbeat(conn, task_id)
            assert self._heartbeat(conn, task_id) is not None

    def test_ignores_non_running_task(self, db_path):
        # A ping racing completion must not resurrect the heartbeat.
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="hi", user_id="alice")
            db.update_task_status(conn, task_id, "completed", result="done")
            db.touch_task_heartbeat(conn, task_id)
            assert self._heartbeat(conn, task_id) is None


class TestTrustedEmailSendersDB:
    def test_add_trusted_sender(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.add_trusted_sender(conn, "alice", "joe@example.com") is True

    def test_add_duplicate_returns_false(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.add_trusted_sender(conn, "alice", "joe@example.com") is False

    def test_add_is_case_insensitive(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "Joe@Example.COM")
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    def test_remove_trusted_sender(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.remove_trusted_sender(conn, "alice", "joe@example.com") is True
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is False

    def test_remove_nonexistent_returns_false(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.remove_trusted_sender(conn, "alice", "nobody@example.com") is False

    def test_list_trusted_senders(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "bob@example.com")
            db.add_trusted_sender(conn, "alice", "alice@example.com")
            senders = db.list_trusted_senders(conn, "alice")
            assert len(senders) == 2
            assert senders[0]["sender_email"] == "alice@example.com"  # sorted
            assert senders[1]["sender_email"] == "bob@example.com"

    def test_list_empty(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.list_trusted_senders(conn, "alice") == []

    def test_is_sender_trusted_in_db(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is False
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    def test_user_isolation(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.is_sender_trusted_in_db(conn, "bob", "joe@example.com") is False


class TestSaveAndGetRecentConversationSkills:
    def test_empty_when_no_prior_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()

    def test_returns_skills_from_recent_completed_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="list gmail", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["files", "google_workspace"])
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == {"files", "google_workspace"}

    def test_excludes_current_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="list gmail", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            result = db.get_recent_conversation_skills(
                conn, "room1", exclude_task_id=task_id,
            )
            assert result == set()

    def test_respects_max_age(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="old task", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            # Backdate the task
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-60 minutes') WHERE id = ?",
                (task_id,),
            )
            result = db.get_recent_conversation_skills(
                conn, "room1", max_age_minutes=30,
            )
            assert result == set()

    def test_respects_limit(self, db_path):
        with db.get_db(db_path) as conn:
            for i in range(3):
                tid = db.create_task(
                    conn, prompt=f"task {i}", user_id="alice",
                    conversation_token="room1",
                )
                db.update_task_status(conn, tid, "completed", result="done")
                db.save_task_selected_skills(conn, tid, [f"skill_{i}"])
            # limit=1 should only get the most recent
            result = db.get_recent_conversation_skills(conn, "room1", limit=1)
            assert result == {"skill_2"}

    def test_unions_skills_from_multiple_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            for skills in [["calendar"], ["google_workspace", "email"]]:
                tid = db.create_task(
                    conn, prompt="test", user_id="alice",
                    conversation_token="room1",
                )
                db.update_task_status(conn, tid, "completed", result="done")
                db.save_task_selected_skills(conn, tid, skills)
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == {"calendar", "google_workspace", "email"}

    def test_ignores_null_selected_skills(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            # No save_task_selected_skills — column stays NULL
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()

    def test_ignores_different_conversation(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice",
                conversation_token="room2",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()


class TestKnowledgeFactsDedupMigration:
    """The init_db pipeline must tolerate pre-existing duplicate current KG facts.

    Production knowledge_facts has no UNIQUE constraint historically, so two
    concurrent sleep cycles could both insert the same current triple. The new
    partial unique index in schema.sql would fail to apply via executescript()
    if duplicates exist — that breaks every deploy that ran an update. The
    migration in _run_migrations() must invalidate older duplicates (keep the
    newest id per group) before schema.sql runs.
    """

    def _prep_legacy_db(self, path):
        """Create a legacy knowledge_facts table (no unique index) with duplicates."""
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE knowledge_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                temporary INTEGER DEFAULT 0,
                confidence REAL DEFAULT 1.0,
                source_task_id INTEGER,
                source_type TEXT DEFAULT 'extracted',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

    def test_init_db_succeeds_with_duplicate_current_facts(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._prep_legacy_db(path)
        conn = sqlite3.connect(str(path))
        # Two duplicate current rows for the same triple
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "carol", "knows", "python"),
        )
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "carol", "knows", "python"),
        )
        conn.commit()
        conn.close()

        # The original failure: executescript of schema.sql aborts on
        # IntegrityError when CREATE UNIQUE INDEX hits existing duplicates.
        # With the dedup migration in _run_migrations, init_db must succeed.
        db.init_db(path)

        conn = sqlite3.connect(str(path))
        current_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts "
            "WHERE user_id=? AND subject=? AND predicate=? AND object=? "
            "AND valid_until IS NULL",
            ("user1", "carol", "knows", "python"),
        ).fetchone()[0]
        assert current_count == 1  # Duplicate invalidated

        total = conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0]
        assert total == 2  # Older row kept as historical, not deleted

        # Unique index now exists and blocks a fresh duplicate insert
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
                "VALUES (?, ?, ?, ?)",
                ("user1", "carol", "knows", "python"),
            )
        conn.close()

    def test_migration_keeps_newest_row_per_group(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._prep_legacy_db(path)
        conn = sqlite3.connect(str(path))
        # Three dupes — the one with the highest id should survive as current
        for _ in range(3):
            conn.execute(
                "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
                "VALUES (?, ?, ?, ?)",
                ("user1", "carol", "knows", "python"),
            )
        conn.commit()
        conn.close()

        db.init_db(path)

        conn = sqlite3.connect(str(path))
        rows = conn.execute(
            "SELECT id, valid_until FROM knowledge_facts "
            "WHERE user_id=? AND subject=? AND predicate=? AND object=? "
            "ORDER BY id",
            ("user1", "carol", "knows", "python"),
        ).fetchall()
        assert len(rows) == 3
        # Oldest two invalidated
        assert rows[0][1] is not None
        assert rows[1][1] is not None
        # Newest kept
        assert rows[2][1] is None
        conn.close()

    def test_migration_only_touches_current_duplicates(self, tmp_path):
        """Rows that differ in any of (user_id, subject, predicate, object) are untouched."""
        path = tmp_path / "legacy.db"
        self._prep_legacy_db(path)
        conn = sqlite3.connect(str(path))
        # Two distinct triples — not duplicates
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "carol", "knows", "python"),
        )
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "carol", "knows", "rust"),
        )
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user2", "carol", "knows", "python"),
        )
        conn.commit()
        conn.close()

        db.init_db(path)

        conn = sqlite3.connect(str(path))
        current_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts WHERE valid_until IS NULL"
        ).fetchone()[0]
        assert current_count == 3
        conn.close()

    def test_migration_ignores_historical_duplicates(self, tmp_path):
        """Rows with valid_until set aren't affected — duplicates are allowed in history."""
        path = tmp_path / "legacy.db"
        self._prep_legacy_db(path)
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user1", "carol", "knows", "python", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user1", "carol", "knows", "python", "2026-02-01"),
        )
        conn.commit()
        conn.close()

        db.init_db(path)

        conn = sqlite3.connect(str(path))
        historical_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts WHERE valid_until IS NOT NULL"
        ).fetchone()[0]
        assert historical_count == 2  # Both preserved
        current_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts WHERE valid_until IS NULL"
        ).fetchone()[0]
        assert current_count == 0
        conn.close()

    def test_migration_idempotent_on_clean_db(self, tmp_path):
        """Running init_db twice on a fresh DB with no dupes is a no-op."""
        path = tmp_path / "clean.db"
        db.init_db(path)
        # Insert a unique current fact
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "carol", "knows", "python"),
        )
        conn.commit()
        conn.close()
        # Second init_db run must not touch the fact
        db.init_db(path)
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT valid_until FROM knowledge_facts"
        ).fetchone()
        assert row[0] is None
        conn.close()

    def test_migration_tolerates_missing_table(self, tmp_path):
        """init_db on a brand-new DB (no knowledge_facts table yet) works fine."""
        path = tmp_path / "fresh.db"
        db.init_db(path)  # No pre-existing table at all
        conn = sqlite3.connect(str(path))
        # Table created by schema.sql, unique index in place
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='knowledge_facts'"
        ).fetchall()
        index_names = {r[0] for r in rows}
        assert "idx_kf_unique_current" in index_names
        conn.close()


class TestGetSubtaskDepth:
    def test_root_task_has_depth_zero(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="root", user_id="alice")
            assert db.get_subtask_depth(conn, task_id) == 0

    def test_first_subtask_has_depth_one(self, db_path):
        with db.get_db(db_path) as conn:
            root = db.create_task(conn, prompt="root", user_id="alice")
            child = db.create_task(
                conn, prompt="child", user_id="alice",
                source_type="subtask", parent_task_id=root,
            )
            assert db.get_subtask_depth(conn, child) == 1

    def test_walks_full_chain(self, db_path):
        with db.get_db(db_path) as conn:
            t0 = db.create_task(conn, prompt="t0", user_id="alice")
            t1 = db.create_task(
                conn, prompt="t1", user_id="alice",
                source_type="subtask", parent_task_id=t0,
            )
            t2 = db.create_task(
                conn, prompt="t2", user_id="alice",
                source_type="subtask", parent_task_id=t1,
            )
            t3 = db.create_task(
                conn, prompt="t3", user_id="alice",
                source_type="subtask", parent_task_id=t2,
            )
            assert db.get_subtask_depth(conn, t3) == 3

    def test_caps_traversal_to_avoid_pathological_chains(self, db_path):
        # A pathological self-referencing or very deep chain shouldn't loop
        # forever — the helper caps at a sane bound and returns it as the
        # observed depth.
        with db.get_db(db_path) as conn:
            previous = db.create_task(conn, prompt="root", user_id="alice")
            for i in range(60):
                previous = db.create_task(
                    conn, prompt=f"level-{i}", user_id="alice",
                    source_type="subtask", parent_task_id=previous,
                )
            depth = db.get_subtask_depth(conn, previous)
            # Whatever cap is chosen, the helper must terminate and return
            # an int >= the cap (>= 50 is enough to prove it didn't bail at 0).
            assert depth >= 50


class TestUserResourceExtras:
    def test_add_without_extras_round_trips_as_empty_dict(self, db_path):
        with db.get_db(db_path) as conn:
            rid = db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Documents", display_name="Docs",
            )
            rows = db.get_user_resources(conn, "alice")
            assert len(rows) == 1
            assert rows[0].id == rid
            assert rows[0].extras == {}

    def test_add_with_extras_round_trips(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "abc123", "default_radius": 75},
            )
            rows = db.get_user_resources(conn, "alice")
            assert rows[0].extras == {"ingest_token": "abc123", "default_radius": 75}

    def test_upsert_overwrites_extras_when_provided(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "old"},
            )
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "new", "default_radius": 50},
            )
            rows = db.get_user_resources(conn, "alice")
            assert len(rows) == 1
            assert rows[0].extras == {"ingest_token": "new", "default_radius": 50}

    def test_upsert_preserves_extras_when_not_provided(self, db_path):
        # Operator first sets extras, then a later call without --extras
        # should leave them alone (mirrors the `user ensure` partial-update
        # contract: omitted fields are unchanged).
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "preserved"},
            )
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
            )
            rows = db.get_user_resources(conn, "alice")
            assert rows[0].extras == {"ingest_token": "preserved"}

    def test_explicit_empty_dict_clears_extras(self, db_path):
        # Distinct from "not provided": passing an empty dict explicitly
        # means the operator wants the extras cleared.
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "to-clear"},
            )
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={},
            )
            rows = db.get_user_resources(conn, "alice")
            assert rows[0].extras == {}

    def test_corrupt_json_in_extras_falls_back_to_empty(self, db_path):
        # If the extras column ever holds non-JSON (manual edit, partial
        # write), get_user_resources must not raise.
        with db.get_db(db_path) as conn:
            rid = db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/x", display_name="X",
            )
            conn.execute(
                "UPDATE user_resources SET extras = ? WHERE id = ?",
                ("{not valid json", rid),
            )
            rows = db.get_user_resources(conn, "alice")
            assert rows[0].extras == {}


class TestUnreadMessageCounts:
    """Stage 1 — web-chat unread room indicators: count helpers over the
    canonical `messages` store + per-surface `room_read_state` cursor."""

    def _seed_room(self, conn, token="r1", user="alice"):
        db.register_room(conn, token, user, origin="web", name="Room")
        return token

    def test_max_id_empty_room_is_zero(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            assert db.room_max_message_id(conn, "r1") == 0

    def test_max_id_returns_highest(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            db.add_message(conn, "r1", role="user", body="hi", origin_surface="web")
            top = db.add_message(
                conn, "r1", role="assistant", body="yo", origin_surface="web",
            )
            assert db.room_max_message_id(conn, "r1") == top

    def test_count_zero_when_no_messages_no_cursor(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 0

    def test_count_excludes_user_role(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            # cursor at 0 (no row) → everything past 0 is candidate
            db.add_message(conn, "r1", role="user", body="q", origin_surface="web")
            db.add_message(conn, "r1", role="assistant", body="a", origin_surface="web")
            db.add_message(conn, "r1", role="system", body="alert", origin_surface="web")
            # assistant + system count; user excluded
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 2

    def test_count_respects_cursor(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            db.add_message(conn, "r1", role="assistant", body="old", origin_surface="web")
            mid = db.add_message(
                conn, "r1", role="assistant", body="seen", origin_surface="web",
            )
            db.set_room_read_state(conn, "r1", "web", mid, "alice")
            db.add_message(conn, "r1", role="assistant", body="new", origin_surface="web")
            # only the one message with id > mid counts
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 1

    def test_per_surface_isolation(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            top = db.add_message(
                conn, "r1", role="assistant", body="a", origin_surface="web",
            )
            db.set_room_read_state(conn, "r1", "web", top, "alice")
            # web is caught up; talk cursor is independent (still 0)
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 0
            assert db.count_unread_messages(conn, "r1", "talk", "alice") == 1

    def test_per_user_isolation(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            top = db.add_message(
                conn, "r1", role="assistant", body="a", origin_surface="web",
            )
            db.set_room_read_state(conn, "r1", "web", top, "alice")
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 0
            assert db.count_unread_messages(conn, "r1", "web", "bob") == 1

    def test_initialize_seeds_cursor_to_max_when_absent(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            db.add_message(conn, "r1", role="assistant", body="old", origin_surface="web")
            top = db.add_message(
                conn, "r1", role="assistant", body="old2", origin_surface="web",
            )
            did = db.initialize_room_read_state(conn, "r1", "web", "alice")
            assert did is True
            assert db.get_room_read_state(conn, "r1", "web", "alice") == top
            # backlog now reads as caught-up
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 0

    def test_initialize_noop_when_row_exists(self, db_path):
        with db.get_db(db_path) as conn:
            self._seed_room(conn)
            db.set_room_read_state(conn, "r1", "web", 0, "alice")
            db.add_message(conn, "r1", role="assistant", body="x", origin_surface="web")
            did = db.initialize_room_read_state(conn, "r1", "web", "alice")
            assert did is False
            # existing cursor (0) preserved → the message stays unread
            assert db.get_room_read_state(conn, "r1", "web", "alice") == 0
            assert db.count_unread_messages(conn, "r1", "web", "alice") == 1


class TestConnectionPragmas:
    """WAL journal mode is set once at init_db and persists in the file
    header; get_db must NOT re-issue it (re-issuing takes a write lock on
    every open and races sibling readers — the stall root cause). get_db
    sets synchronous=NORMAL per connection (cheap, safe under WAL)."""

    def test_init_db_sets_wal(self, db_path):
        # A raw connection (not get_db) sees WAL already set by init_db.
        conn = sqlite3.connect(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == "wal"

    def test_get_db_does_not_reissue_journal_mode(self, db_path, monkeypatch):
        seen: list[str] = []
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(seen.append)
            return conn

        monkeypatch.setattr(db.sqlite3, "connect", spy_connect)
        with db.get_db(db_path) as conn:
            conn.execute("SELECT 1")

        assert not any("journal_mode" in s.lower() for s in seen), (
            f"get_db re-issued journal_mode: {seen}"
        )

    def test_get_db_sets_synchronous_normal(self, db_path):
        with db.get_db(db_path) as conn:
            # NORMAL == 1
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


class TestBusyTimeoutParam:
    def test_busy_timeout_override(self, db_path):
        with db.get_db(db_path, busy_timeout_ms=2000) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000

    def test_default_keeps_30s(self, db_path):
        # connect(timeout=30.0) => busy_timeout 30000; not overridden.
        with db.get_db(db_path) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


class TestHeartbeatAlertSignatureMigration:
    """`heartbeat_state.last_alert_signature` reaches an existing database.

    The column carries the transition gate that stops a standing failure paging
    once per cooldown for ever. A deployment upgrading into it has a
    `heartbeat_state` table already, so the additive migration is the only thing
    that puts the column there — `schema.sql`'s `CREATE TABLE IF NOT EXISTS` is
    a no-op against it, which is the trap this test exists for.
    """

    def _legacy_db(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute(
            """
            CREATE TABLE heartbeat_state (
                user_id TEXT NOT NULL,
                check_name TEXT NOT NULL,
                last_check_at TEXT,
                last_alert_at TEXT,
                last_healthy_at TEXT,
                last_error_at TEXT,
                consecutive_errors INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, check_name)
            )
            """
        )
        conn.execute(
            "INSERT INTO heartbeat_state (user_id, check_name, last_alert_at) "
            "VALUES ('alice', 'self', datetime('now'))"
        )
        conn.commit()
        conn.close()

    def test_the_column_is_added_to_an_existing_table(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)

        db.init_db(path)

        conn = sqlite3.connect(str(path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(heartbeat_state)")}
        conn.close()
        assert "last_alert_signature" in cols

    def test_the_pre_existing_row_reads_as_no_signature(self, tmp_path):
        """Not as an empty string, which would suppress a real first alert.

        A row written before the column existed has never had an alert
        attributed to it, so it must compare unequal to every signature a check
        can produce.
        """
        path = tmp_path / "legacy.db"
        self._legacy_db(path)
        db.init_db(path)

        with db.get_db(path) as conn:
            state = db.get_heartbeat_state(conn, "alice", "self")

        assert state is not None
        assert state.last_alert_signature is None


class TestTheFrameworkCarriesNoTransactionTracking:
    """ISSUE-427: `istota.db` used to carry a second copy of the money module's
    transaction dedup — the same table names over a different database, with the
    same function names exported from both and nothing saying which was
    authoritative. The framework copy had no reader: the only thing that touched
    those tables was the `tracked_transactions` deferred-op handler, which wrote
    rows nothing read back — and nothing in `src/` ever wrote the file that drove
    it.

    The guard is a name check rather than a behaviour one because the failure it
    prevents is somebody reaching for `db.track_monarch_transactions_batch`
    instead of `istota.money.db.track_monarch_transactions_batch` and writing
    rows nothing reads back.
    """

    RETIRED = (
        "MonarchSyncedTransaction",
        "is_monarch_transaction_synced",
        "track_monarch_transaction",
        "track_monarch_transactions_batch",
        "is_content_hash_synced",
        "get_active_monarch_synced_transactions",
        "mark_monarch_transaction_recategorized",
        "update_monarch_transaction_posted_account",
        "compute_transaction_hash",
        "is_csv_transaction_imported",
        "track_csv_transaction",
        "track_csv_transactions_batch",
    )

    def test_the_names_are_gone_from_the_framework_db_module(self):
        present = [name for name in self.RETIRED if hasattr(db, name)]
        assert present == [], (
            "the money module owns these; a framework copy writes rows "
            f"nothing reads: {present}"
        )

    def test_the_money_module_still_owns_them(self):
        """The other half of the claim. Removing the framework copy is only
        safe because the live implementation is elsewhere, so assert it is.
        """
        from istota.money import db as money_db
        from istota.money.core import dedup

        assert hasattr(dedup, "compute_transaction_hash")
        for name in (
            "MonarchSyncedTransaction",
            "is_monarch_transaction_synced",
            "track_monarch_transactions_batch",
            "is_content_hash_synced",
            "get_active_monarch_synced_transactions",
            "mark_monarch_transaction_recategorized",
            "update_monarch_transaction_posted_account",
            "track_csv_transactions_batch",
        ):
            assert hasattr(money_db, name), name


class TestInitDbAgainstALegacyMonarchTable:
    """ISSUE-427: `schema.sql` keeps a *partial* index on
    `monarch_synced_transactions` filtered on `recategorized_at`, so the column
    that index names has to exist before `executescript` runs. `init_db` orders
    `_run_migrations` first for exactly that reason.

    Removing the ALTER loop as dead code — which is how the issue described it —
    breaks this: SQLite resolves a partial index's WHERE clause at CREATE time,
    `executescript` aborts on `no such column`, and **every table declared after
    that index in the file is silently not created**. The failure lands inside
    the bare `python -c` an Ansible deploy runs, not in a log line.

    Nothing in the suite exercised a pre-migration database shape before this.
    """

    LEGACY = """
        CREATE TABLE monarch_synced_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            monarch_transaction_id TEXT NOT NULL,
            synced_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, monarch_transaction_id)
        );
    """

    def _legacy_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(self.LEGACY)
        conn.execute(
            "INSERT INTO monarch_synced_transactions (user_id, monarch_transaction_id)"
            " VALUES ('alice', 'txn-legacy')"
        )
        conn.commit()
        conn.close()

    def test_init_db_migrates_the_column_the_partial_index_needs(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)

        db.init_db(path)

        with db.get_db(path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(monarch_synced_transactions)")
            }
        assert "recategorized_at" in cols

    def test_every_table_after_that_index_is_still_created(self, tmp_path):
        """The assertion that would have caught the regression. A schema script
        that aborts part-way leaves a database that looks fine until something
        reads one of the tables below the cut.
        """
        path = tmp_path / "legacy.db"
        self._legacy_db(path)

        db.init_db(path)

        with db.get_db(path) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        # Both are declared after `idx_monarch_synced_active` in schema.sql.
        for table in ("csv_imported_transactions", "channel_sleep_cycle_state"):
            assert table in names, f"{table} is declared after the partial index"

    def test_the_legacy_rows_survive(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)

        db.init_db(path)

        with db.get_db(path) as conn:
            row = conn.execute(
                "SELECT monarch_transaction_id, recategorized_at"
                " FROM monarch_synced_transactions"
            ).fetchone()
        assert row["monarch_transaction_id"] == "txn-legacy"
        assert row["recategorized_at"] is None
