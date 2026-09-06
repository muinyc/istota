"""Tests for the web chat surface (Phase 1 backend)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import (
    Config,
    SiteConfig,
    UserConfig,
    WebChatConfig,
    WebConfig,
    load_config,
)
from istota.transport.registry import make_registry
from istota.transport.routing import plan_has_surface, resolve_delivery_plan

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# _trace_segments — ordered history segment reconstruction
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestTraceSegments:
    def _fn(self):
        from istota.web_app import _trace_segments
        return _trace_segments

    def test_ordered_trace_skips_cm_boundary_and_canonicalizes_answer(self):
        import json
        trace = json.dumps([
            {"type": "text", "text": "Let me check."},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "calendar list"},
            {"type": "text", "text": "draft answer"},
        ])
        segs = self._fn()(trace, None, "final answer")
        assert segs == [
            {"kind": "text", "text": "Let me check."},
            {"kind": "tool", "text": "calendar list"},
            {"kind": "text", "text": "final answer"},
        ]

    def test_trace_ending_in_tool_appends_result(self):
        import json
        trace = json.dumps([
            {"type": "text", "text": "narration"},
            {"type": "tool", "text": "ran a thing"},
        ])
        segs = self._fn()(trace, None, "the answer")
        assert segs == [
            {"kind": "text", "text": "narration"},
            {"kind": "tool", "text": "ran a thing"},
            {"kind": "text", "text": "the answer"},
        ]

    def test_no_trace_falls_back_to_actions_taken(self):
        import json
        actions = json.dumps(["Read a.txt", "Grep b"])
        segs = self._fn()(None, actions, "result text")
        assert segs == [
            {"kind": "tool", "text": "Read a.txt"},
            {"kind": "tool", "text": "Grep b"},
            {"kind": "text", "text": "result text"},
        ]

    def test_neither_trace_nor_actions_returns_result_only(self):
        assert self._fn()(None, None, "just the answer") == [
            {"kind": "text", "text": "just the answer"},
        ]

    def test_empty_result_with_nothing_is_empty(self):
        assert self._fn()(None, None, "") == []
        assert self._fn()(None, None, None) == []

    def test_malformed_trace_falls_back_without_raising(self):
        segs = self._fn()("{not json", '["Tool A"]', "answer")
        assert segs == [
            {"kind": "tool", "text": "Tool A"},
            {"kind": "text", "text": "answer"},
        ]

    def test_empty_result_keeps_trace_text(self):
        import json
        trace = json.dumps([{"type": "text", "text": "streamed"}])
        # An empty result leaves the trace's trailing text as the answer.
        assert self._fn()(trace, None, "") == [
            {"kind": "text", "text": "streamed"},
        ]

    def test_cancelled_appends_notice_without_overwriting_trace(self):
        import json
        trace = json.dumps([
            {"type": "tool", "text": "Read a.txt"},
            {"type": "text", "text": "partial analysis"},
        ])
        # ISSUE-183: a cancelled task's terminal notice is appended after the
        # trace's intermediate content — the trailing text is intermediate, not
        # a draft answer, so overwriting it (the completed-task path) would lose it.
        segs = self._fn()(trace, None, "Cancelled by user", status="cancelled")
        assert segs == [
            {"kind": "tool", "text": "Read a.txt"},
            {"kind": "text", "text": "partial analysis"},
            {"kind": "text", "text": "Cancelled by user"},
        ]

    def test_failed_appends_error_without_overwriting_trace(self):
        import json
        trace = json.dumps([
            {"type": "text", "text": "Let me check."},
            {"type": "tool", "text": "Bash: grep foo"},
        ])
        segs = self._fn()(trace, None, "API error: rate limited", status="failed")
        assert segs == [
            {"kind": "text", "text": "Let me check."},
            {"kind": "tool", "text": "Bash: grep foo"},
            {"kind": "text", "text": "API error: rate limited"},
        ]

    def test_cancelled_with_no_trace_appends_notice(self):
        # No trace + cancelled → just the cancel notice (not blank).
        assert self._fn()(None, None, "Cancelled by user", status="cancelled") == [
            {"kind": "text", "text": "Cancelled by user"},
        ]

    def test_failed_empty_error_keeps_trace_text(self):
        import json
        trace = json.dumps([{"type": "text", "text": "streamed"}])
        # An empty error leaves the trace intact (no blank trailing segment).
        assert self._fn()(trace, None, "", status="failed") == [
            {"kind": "text", "text": "streamed"},
        ]

    def test_trace_text_none_value_becomes_empty(self):
        import json
        # Robustness: a trace entry with an explicit null ``text`` must not
        # render as the literal string "None".
        trace = json.dumps([{"type": "text", "text": None}, {"type": "tool", "text": None}])
        segs = self._fn()(trace, None, "", status="cancelled")
        assert segs == [
            {"kind": "text", "text": ""},
            {"kind": "tool", "text": ""},
        ]


# ---------------------------------------------------------------------------
# DB layer: rooms + rate-limit counter
# ---------------------------------------------------------------------------


class TestWebChatRoomsDB:
    def test_ensure_default_creates_general(self, conn):
        room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.name == "general"
        assert room.user_id == "alice"
        assert room.token.startswith("web-alice-")
        assert not room.archived

    def test_ensure_default_idempotent(self, conn):
        first = db.ensure_default_web_chat_room(conn, "alice")
        second = db.ensure_default_web_chat_room(conn, "alice")
        assert first.id == second.id

    def test_create_and_list_rooms_oldest_first(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "alice", "ideas")
        rooms = db.list_web_chat_rooms(conn, "alice")
        assert [r.name for r in rooms] == ["general", "ideas"]

    def test_rooms_are_per_user(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "bob", "general")
        assert len(db.list_web_chat_rooms(conn, "alice")) == 1
        assert len(db.list_web_chat_rooms(conn, "bob")) == 1

    def test_tokens_are_unique(self, conn):
        a = db.create_web_chat_room(conn, "alice", "one")
        b = db.create_web_chat_room(conn, "alice", "two")
        assert a.token != b.token

    def test_rename_room(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        updated = db.update_web_chat_room(conn, room.id, name="renamed")
        assert updated.name == "renamed"

    def test_archive_hides_from_default_list(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.update_web_chat_room(conn, room.id, archived=True)
        assert db.list_web_chat_rooms(conn, "alice") == []
        assert len(db.list_web_chat_rooms(conn, "alice", include_archived=True)) == 1

    def test_get_by_token(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        found = db.get_web_chat_room_by_token(conn, room.token)
        assert found.id == room.id

    def test_count_recent_web_tasks(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        for _ in range(3):
            db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room.token, output_target="web",
            )
        # A non-web task for the same user must not be counted.
        db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
        assert db.count_recent_web_tasks(conn, "alice", 300) == 3


def _add_system(conn, token: str, text: str, title: str | None = None) -> int:
    """A bot-delivered room message, the way WebTransport.deliver writes one."""
    return db.add_message(
        conn, token, role="system", body=text, origin_surface="web", title=title,
    )


class TestWebChatMessagesDB:
    """Bot-delivered (unsolicited) room messages — the `web` delivery surface.

    These live in the canonical `messages` store as role='system' rows now; the
    legacy `web_chat_messages` accessors are gone (live-web-chat-room-stream
    Stage 6), though the table itself survives for the delete cascade."""

    def test_add_and_list_oldest_first(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        _add_system(conn, room.token, "first")
        _add_system(conn, room.token, "second", title="T")
        msgs = db.list_system_messages(conn, room.token)
        assert [m.body for m in msgs] == ["first", "second"]
        assert msgs[0].role == "system"
        assert msgs[1].title == "T"

    def test_add_returns_id(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        mid = _add_system(conn, room.token, "x")
        assert isinstance(mid, int) and mid > 0

    def test_scoped_by_token(self, conn):
        a = db.create_web_chat_room(conn, "alice", "one")
        b = db.create_web_chat_room(conn, "alice", "two")
        _add_system(conn, a.token, "in-a")
        assert [m.body for m in db.list_system_messages(conn, a.token)] == ["in-a"]
        assert db.list_system_messages(conn, b.token) == []

    def test_limit_keeps_most_recent(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        for i in range(5):
            _add_system(conn, room.token, f"m{i}")
        msgs = db.list_system_messages(conn, room.token, limit=2)
        assert [m.body for m in msgs] == ["m3", "m4"]


def _seed_task_event(conn, task_id: int, seq: int = 1) -> None:
    """Insert a bare task_events row for a task (mirrors EventWriter.emit)."""
    conn.execute(
        "INSERT INTO task_events (task_id, seq, kind, payload, created_at) "
        "VALUES (?, ?, 'result', '{}', datetime('now'))",
        (task_id, seq),
    )


class TestWebChatRoomDelete:
    """Hard delete + cascade across every table keyed on a room's token."""

    def test_delete_removes_room(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        assert db.delete_web_chat_room(conn, room.id, "alice") is True
        assert db.list_web_chat_rooms(conn, "alice") == []

    def test_delete_cascades_tasks_and_events(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "keep")
        tid = db.create_task(
            conn, prompt="hi", user_id="alice", source_type="web",
            conversation_token=room.token, output_target="web",
        )
        _seed_task_event(conn, tid)
        # A task in another room must survive.
        keep_tid = db.create_task(
            conn, prompt="stay", user_id="alice", source_type="web",
            conversation_token=other.token, output_target="web",
        )
        _seed_task_event(conn, keep_tid)

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.get_task(conn, tid) is None
        assert db.get_task_events(conn, tid) == []
        assert db.get_task(conn, keep_tid) is not None
        assert len(db.get_task_events(conn, keep_tid)) == 1

    def test_delete_cascades_web_chat_messages(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "keep")
        _add_system(conn, room.token, "gone")
        _add_system(conn, other.token, "stays")

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.list_system_messages(conn, room.token) == []
        assert [m.body for m in db.list_system_messages(conn, other.token)] == ["stays"]

    def test_delete_cascades_channel_sleep_state(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.set_channel_sleep_cycle_last_run(conn, room.token, None)
        assert db.get_channel_sleep_cycle_last_run(conn, room.token)[0] is not None

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.get_channel_sleep_cycle_last_run(conn, room.token)[0] is None

    def test_delete_wrong_user_returns_false(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        assert db.delete_web_chat_room(conn, room.id, "bob") is False
        assert len(db.list_web_chat_rooms(conn, "alice")) == 1

    def test_delete_unknown_id_returns_false(self, conn):
        assert db.delete_web_chat_room(conn, 9999, "alice") is False

    def test_count_active_web_tasks(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "other")
        # Two non-terminal tasks on the room token.
        for _ in range(2):
            db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room.token, output_target="web",
            )
        # A terminal task on the same token must not be counted.
        done = db.create_task(
            conn, prompt="done", user_id="alice", source_type="web",
            conversation_token=room.token, output_target="web",
        )
        db.update_task_status(conn, done, "completed", result="ok")
        # A task in another room must not be counted.
        db.create_task(
            conn, prompt="elsewhere", user_id="alice", source_type="web",
            conversation_token=other.token, output_target="web",
        )

        assert db.count_active_web_tasks(conn, room.token, "alice") == 2

    def test_count_active_web_tasks_includes_foreign_push(self, conn):
        # An email reply routed INTO a room (source_type="email") targets the
        # room via conversation_token and will write to it — the busy-room
        # delete guard must count it, not just source_type="web" tasks.
        room = db.create_web_chat_room(conn, "alice", "general")
        db.create_task(
            conn, prompt="email reply", user_id="alice", source_type="email",
            conversation_token=room.token, output_target=f"web:{room.token},email",
        )
        assert db.count_active_web_tasks(conn, room.token, "alice") == 1


# ---------------------------------------------------------------------------
# Delivery routing: web is a stream surface (no Talk/email push)
# ---------------------------------------------------------------------------


class TestWebDeliveryRouting:
    def _config(self, tmp_path):
        return Config(db_path=tmp_path / "istota.db")

    def test_web_output_target_resolves_to_stream(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target="web",
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")
        assert not plan_has_surface(plan, "email")
        assert all(d.kind == "stream" for d in plan)

    def test_web_default_plan_when_target_unset(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target=None,
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestWebChatConfig:
    def test_defaults(self):
        chat = WebChatConfig()
        assert chat.max_prompt_chars == 32000
        assert chat.rate_limit_messages == 30
        assert "pdf" in chat.attachment_extensions

    def test_camera_formats_are_accepted(self):
        """The formats a phone actually produces.

        `heic` is what an iPhone photo is before anything re-encodes it, and
        `webm` is what MediaRecorder hands the voice button on Chrome and
        Firefox. Either one falling out of this list is a 400 on the extension
        alone, which says nothing about the format having been the problem.
        """
        chat = WebChatConfig()
        for ext in ("heic", "jpeg", "webm", "m4a"):
            assert ext in chat.attachment_extensions, ext

    def test_parsed_from_toml(self, tmp_path):
        toml = tmp_path / "config.toml"
        toml.write_text(
            "[web]\nenabled = true\n\n"
            "[web.chat]\nmax_prompt_chars = 1000\nrate_limit_messages = 5\n"
        )
        config = load_config(toml)
        assert config.web.chat.max_prompt_chars == 1000
        assert config.web.chat.rate_limit_messages == 5
        # Untouched knobs keep defaults.
        assert config.web.chat.max_attachment_mb == 25

    @_needs_web_deps
    def test_sse_poll_interval_wired(self, tmp_path):
        """The SSE generator's poll cadence must come from config, not a
        hardcoded constant."""
        import istota.web_app as mod
        config = _make_config(tmp_path)
        config.web.chat.sse_poll_interval_ms = 750
        mod._config = config
        assert mod._sse_poll_seconds() == 0.75


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def chat_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


@_needs_web_deps
class TestChatRoomsApi:
    async def test_rooms_requires_auth(self, chat_client):
        resp = await chat_client.get("/istota/api/chat/rooms")
        assert resp.status_code == 401

    async def test_list_rooms_autocreates_general(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/rooms", cookies=cookies)
        assert resp.status_code == 200
        rooms = resp.json()["rooms"]
        assert len(rooms) == 1
        assert rooms[0]["name"] == "general"

    async def test_create_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "ideas"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "ideas"
        # Carries the sidebar's sort key, so a just-created room sorts to the
        # top instead of below every room the user hasn't touched in weeks.
        assert resp.json()["last_activity"].endswith("Z")

    async def test_rename_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "old"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "new"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new"

    async def test_cannot_touch_other_users_room(self, chat_client):
        alice = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "secret"}, cookies=alice,
            headers={"origin": "https://example.com"},
        )).json()
        bob = await _login(chat_client, "bob")
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "x"},
            cookies=bob, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_set_room_model_default(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "claude-opus-5", "effort": "high"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "claude-opus-5"
        assert body["effort"] == "high"
        # Surfaced in the room listing too.
        rooms = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"]
        room = next(r for r in rooms if r["id"] == created["id"])
        assert room["model"] == "claude-opus-5"
        assert room["effort"] == "high"

    async def test_clear_room_model_default(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "claude-opus-5"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "", "effort": ""}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["model"] is None
        assert resp.json()["effort"] is None

    async def test_rename_preserves_model_default(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "claude-opus-5"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        # A name-only edit must not clobber the model default.
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "renamed"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.json()["model"] == "claude-opus-5"

    async def test_unknown_model_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "gpt-9"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_invalid_effort_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"effort": "turbo"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400


@_needs_web_deps
class TestRoomColour:
    """Per-user room colour (ISSUE-433).

    The colour lives on the `web_chat_rooms` handle rather than the canonical
    `rooms` registry, so two members of one shared Talk room can tint it
    differently. That is the whole reason it is not beside `model` / `effort` /
    `brain`, every one of which is deliberately room-global.
    """

    @pytest.fixture
    async def client_and_config(self, tmp_path):
        config = _make_config(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    async def _room(self, client, cookies, name="r"):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": name}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()

    async def test_colour_round_trips_through_patch_and_listing(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = await self._room(chat_client, cookies)
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "teal"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["color"] == "teal"
        # And it survives into the listing, which is a separate payload built
        # by the same `_room_to_dict`.
        listing = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"]
        row = next(r for r in listing if r["id"] == created["id"])
        assert row["color"] == "teal"

    async def test_unknown_colour_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = await self._room(chat_client, cookies)
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "chartreuse"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_a_raw_hex_is_rejected(self, chat_client):
        """The palette is ours, not the caller's. A free-form value is exactly
        what the design constraint rules out, and the route is the only thing
        standing between the database and a hex the theme cannot render."""
        cookies = await _login(chat_client, "alice")
        created = await self._room(chat_client, cookies)
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "#ff0000"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_empty_string_clears_the_colour(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = await self._room(chat_client, cookies)
        await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "plum"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": ""}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["color"] is None

    async def test_a_rename_leaves_the_colour_alone(self, chat_client):
        """Key-absence contract, same as `model`: absent leaves it untouched."""
        cookies = await _login(chat_client, "alice")
        created = await self._room(chat_client, cookies)
        await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "sky"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "renamed"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.json()["color"] == "sky"

    async def test_the_colour_is_per_user_on_a_shared_room(self, client_and_config):
        """The reason it is not on the canonical registry. Two members of one
        room hold two handles, so one member's tint must not reach the other.

        Bob's handle is given a colour of its own first, and that is what makes
        the assertion mean anything: asserting it is still `None` after alice's
        PATCH proves nothing, since it was `None` before the request and would
        read the same with no guard at all.
        """
        from istota import db
        client, config = client_and_config
        alice = await _login(client, "alice")
        created = await self._room(client, alice, name="shared")
        with db.get_db(config.db_path) as conn:
            room = db.get_web_chat_room(conn, created["id"])
            db.add_room_member(conn, room.token, "bob")
            bob_handle = db.ensure_web_chat_handle(conn, "bob", room.token, "shared")
            db.update_web_chat_room(conn, bob_handle.id, color="sky")
        await client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"color": "rose"}, cookies=alice,
            headers={"origin": "https://example.com"},
        )
        with db.get_db(config.db_path) as conn:
            assert db.get_web_chat_room(conn, created["id"]).color == "rose"
            assert db.get_web_chat_room(conn, bob_handle.id).color == "sky"

    async def test_a_member_cannot_write_another_members_colour(
        self, client_and_config,
    ):
        """The guard the test above cannot reach. `room_id` is a handle id, and
        a shared room has one per member, so alice naming bob's id is the way
        the per-user boundary is actually attacked. `_chat_update_room`'s
        `room.user_id != username` is what refuses it, and it reads as 404
        rather than 403 so the endpoint is no id oracle."""
        from istota import db
        client, config = client_and_config
        alice = await _login(client, "alice")
        created = await self._room(client, alice, name="shared")
        with db.get_db(config.db_path) as conn:
            room = db.get_web_chat_room(conn, created["id"])
            db.add_room_member(conn, room.token, "bob")
            bob_handle = db.ensure_web_chat_handle(conn, "bob", room.token, "shared")
            db.update_web_chat_room(conn, bob_handle.id, color="sky")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{bob_handle.id}",
            json={"color": "rose"}, cookies=alice,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404
        with db.get_db(config.db_path) as conn:
            assert db.get_web_chat_room(conn, bob_handle.id).color == "sky"


@_needs_web_deps
class TestChatMessagesApi:
    async def _room(self, client, cookies):
        return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"][0]

    async def test_send_creates_web_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "what's on my calendar?"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is not None
        assert "stream_url" in body
        # The task is a source_type=web task on the room token.
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, body["task_id"])
        assert task.source_type == "web"
        # output_target="room" fans out by the room's live bindings (a web-only
        # room resolves to just the web stream, same as the old "web").
        assert task.output_target == "room"
        assert task.conversation_token == room["token"]

    async def _send(self, client, cookies, room_id: int, **payload):
        return await client.post(
            f"/istota/api/chat/rooms/{room_id}/messages",
            json=payload, cookies=cookies,
            headers={"origin": "https://example.com"},
        )

    async def test_repeat_client_msg_id_replays_the_first_task(self, chat_client):
        """A retry of a send we accepted but never got to report resolves to the
        turn it already created, rather than a second one."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        first = await self._send(
            chat_client, cookies, room["id"], text="hello", client_msg_id="k-1",
        )
        second = await self._send(
            chat_client, cookies, room["id"], text="hello", client_msg_id="k-1",
        )
        assert first.status_code == second.status_code == 200
        assert second.json()["task_id"] == first.json()["task_id"]

        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            rows = c.execute(
                "SELECT id FROM messages WHERE room_token = ? AND role = 'user'",
                (room["token"],),
            ).fetchall()
            tasks = c.execute(
                "SELECT id FROM tasks WHERE conversation_token = ?",
                (room["token"],),
            ).fetchall()
        assert len(rows) == 1
        assert len(tasks) == 1

    async def test_empty_client_msg_id_is_stored_as_null(self, chat_client):
        """An empty string is a valid unique key, so left as-is it would
        collapse a room's whole history onto its first send."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        first = await self._send(chat_client, cookies, room["id"], text="one", client_msg_id="")
        second = await self._send(chat_client, cookies, room["id"], text="two", client_msg_id="")
        assert first.json()["task_id"] != second.json()["task_id"]

        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            stored = [
                r["client_msg_id"] for r in c.execute(
                    "SELECT client_msg_id FROM messages WHERE room_token = ? "
                    "AND role = 'user'",
                    (room["token"],),
                ).fetchall()
            ]
        assert stored == [None, None]

    async def test_same_client_msg_id_in_two_rooms_is_two_messages(self, chat_client):
        """The key is scoped to a room, so it cannot claim another room's turn."""
        cookies = await _login(chat_client, "alice")
        first_room = await self._room(chat_client, cookies)
        other = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "second"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()

        a = await self._send(chat_client, cookies, first_room["id"], text="hi", client_msg_id="k")
        b = await self._send(chat_client, cookies, other["id"], text="hi", client_msg_id="k")
        assert a.json()["task_id"] != b.json()["task_id"]

    async def test_another_members_key_does_not_swallow_the_message(self, chat_client):
        """Rooms are shared and the key is arbitrary client text, so a
        co-member reusing one must not have their message dropped — nor be
        handed a task they aren't authorized to read."""
        import istota.web_app as mod
        from istota import db as _db

        alice = await _login(chat_client, "alice")
        room = await self._room(chat_client, alice)
        first = await self._send(chat_client, alice, room["id"], text="mine", client_msg_id="k")

        # Bob shares the room. He is not the sender of the stored turn.
        with db.get_db(mod._config.db_path) as c:
            _db.add_room_member(c, room["token"], "bob")
            _db.ensure_web_chat_handle(c, "bob", room["token"], "shared")
        bob = await _login(chat_client, "bob")
        bob_room = next(
            r for r in (await chat_client.get(
                "/istota/api/chat/rooms", cookies=bob,
            )).json()["rooms"] if r["token"] == room["token"]
        )
        second = await self._send(
            chat_client, bob, bob_room["id"], text="also mine", client_msg_id="k",
        )

        assert second.status_code == 200
        assert second.json()["task_id"] != first.json()["task_id"]
        # Bob's send lands as its own turn; it simply gives up the key rather
        # than colliding on the room-scoped unique index.
        with db.get_db(mod._config.db_path) as c:
            bodies = [
                r["body"] for r in c.execute(
                    "SELECT body FROM messages WHERE room_token = ? AND role = 'user' "
                    "ORDER BY id",
                    (room["token"],),
                ).fetchall()
            ]
        assert bodies == ["mine", "also mine"]

    async def test_over_long_client_msg_id_is_ignored_not_truncated(self, chat_client):
        """Truncating would change the identity, so two keys sharing a 64-char
        prefix would resolve to one another and the second message would be
        silently lost."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        a = await self._send(
            chat_client, cookies, room["id"], text="one", client_msg_id="x" * 70 + "A",
        )
        b = await self._send(
            chat_client, cookies, room["id"], text="two", client_msg_id="x" * 70 + "B",
        )
        assert a.json()["task_id"] != b.json()["task_id"]

        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            stored = [
                r["client_msg_id"] for r in c.execute(
                    "SELECT client_msg_id FROM messages WHERE room_token = ? "
                    "AND role = 'user' ORDER BY id",
                    (room["token"],),
                ).fetchall()
            ]
        assert stored == [None, None]

    async def test_send_without_a_client_msg_id_stores_none(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await self._send(chat_client, cookies, room["id"], text="no key here")
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            row = c.execute(
                "SELECT client_msg_id FROM messages WHERE room_token = ? "
                "AND role = 'user'",
                (room["token"],),
            ).fetchone()
        assert row["client_msg_id"] is None

    async def test_empty_text_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "   "}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def _upload_path(self, username: str, filename: str) -> str:
        """A real file under the user's web-chat upload root, so the send's
        attachment validation accepts it."""
        import istota.web_app as mod
        root = mod._chat_upload_roots(username)[0]
        root.mkdir(parents=True, exist_ok=True)
        path = root / filename
        path.write_bytes(b"\x00\x01")
        return str(path)

    async def test_voice_memo_only_send_is_accepted(self, chat_client):
        """A composer voice memo with nothing typed is the message — the send
        creates a task whose prompt describes the recording (the executor's
        pre-transcription later folds in the spoken words)."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        audio = await self._upload_path("alice", "voice-abc123.webm")
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "", "attachments": [audio]}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]
        assert task_id is not None
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, task_id)
        assert task.attachments == [audio]
        assert "Voice message" in task.prompt

    async def test_attachment_only_send_describes_the_files(self, chat_client):
        """A non-audio attachment sent with no text gets a descriptor naming
        it, so the stored turn isn't blank in history or LLM context."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        image = await self._upload_path("alice", "receipt-99.png")
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "", "attachments": [image]}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, resp.json()["task_id"])
        assert "receipt-99.png" in task.prompt

    async def test_attachment_only_turn_round_trips_in_history(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        audio = await self._upload_path("alice", "voice-xyz.webm")
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "", "attachments": [audio]}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        user_msgs = [m for m in data["messages"] if m["role"] == "user"]
        assert user_msgs and "Voice message" in user_msgs[0]["text"]

    async def test_history_round_trip(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hello there"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["text"] == "hello there"
        assert data["active_task"] is not None  # task is pending

    async def test_delivered_notification_surfaces_in_history(self, chat_client):
        """A message posted to the `web` surface (alert / log) shows up in the
        room transcript as a system message with a stable notif_id and no
        task_id — the user sees it on the next room load."""
        from istota import db
        from istota.web_app import _config
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        # Bot-delivered notifications now land in the canonical messages store
        # (role='system') — the same lane WebTransport.deliver writes.
        with db.get_db(_config.db_path) as conn:
            db.add_message(
                conn, room["token"], role="system", body="disk almost full",
                origin_surface="web", title="Alert",
            )
        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        sys_msgs = [m for m in data["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1
        assert "disk almost full" in sys_msgs[0]["text"]
        assert "Alert" in sys_msgs[0]["text"]
        assert "notif_id" in sys_msgs[0]
        assert "task_id" not in sys_msgs[0]

    async def test_completed_task_history_carries_trace_and_duration(self, chat_client):
        """A completed web task surfaces its tool trace and wall-clock duration
        in history, so the action strip and timing persist as an inspectable
        done state across reloads / room switches (ISSUE-122)."""
        import json

        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        trace = json.dumps([
            {"type": "tool", "text": "Read config.toml"},
            {"type": "text", "text": "thinking"},
            {"type": "tool", "text": "Grep for foo"},
        ])
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="do the thing", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(
                conn, tid, "completed", result="done!",
                actions_taken=json.dumps(["Read config.toml", "Grep for foo"]),
                execution_trace=trace,
            )
            # Stamp a deterministic 7-second wall clock.
            conn.execute(
                "UPDATE tasks SET started_at = '2026-06-07 10:00:00', "
                "completed_at = '2026-06-07 10:00:07' WHERE id = ?",
                (tid,),
            )

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["text"] == "done!"
        # Tool descriptions persist (in order) so the action strip can rebuild.
        assert assistant["tools"] == ["Read config.toml", "Grep for foo"]
        assert assistant["duration_seconds"] == 7.0
        # Ordered, interleaved segments reconstruct the live layout: tool, the
        # mid-turn narration, tool, then the canonical answer as a trailing text.
        assert assistant["segments"] == [
            {"kind": "tool", "text": "Read config.toml"},
            {"kind": "text", "text": "thinking"},
            {"kind": "tool", "text": "Grep for foo"},
            {"kind": "text", "text": "done!"},
        ]

    async def test_completed_task_history_carries_model(self, chat_client):
        """A completed web task surfaces the model that produced it, so the
        chat-message meta shows it on reload (verification-added test)."""
        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(conn, tid, "completed", result="hello")
            db.set_task_model_used(conn, tid, "claude-opus-4-8")

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["model"] == "claude-opus-4-8"

    async def test_history_completed_task_without_model_returns_null(self, chat_client):
        """A completed web task with no recorded model returns model=None, not
        an error or a missing key (verification-added test)."""
        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(conn, tid, "completed", result="hello")

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["model"] is None

    async def test_history_multiple_in_flight_ordered(self, chat_client):
        """Several queued messages each surface a user msg + an in-flight
        assistant placeholder, and active_tasks lists them oldest-first so the
        client can resume one and queue the rest in order."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        task_ids = []
        for text in ("first", "second", "third"):
            r = await chat_client.post(
                f"/istota/api/chat/rooms/{room['id']}/messages",
                json={"text": text}, cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            task_ids.append(r.json()["task_id"])

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()

        # Oldest-first, all three in-flight.
        assert [t["id"] for t in data["active_tasks"]] == task_ids
        assert data["active_task"]["id"] == task_ids[0]

        # Each task contributes a user message and an in-flight assistant slot,
        # interleaved in order.
        roles = [(m["role"], m["task_id"]) for m in data["messages"]]
        assert roles == [
            ("user", task_ids[0]), ("assistant", task_ids[0]),
            ("user", task_ids[1]), ("assistant", task_ids[1]),
            ("user", task_ids[2]), ("assistant", task_ids[2]),
        ]
        assistants = [m for m in data["messages"] if m["role"] == "assistant"]
        assert all(m["text"] == "" and m["status"] == "pending" for m in assistants)

    async def test_rate_limit_returns_429(self, chat_client):
        import istota.web_app as mod
        mod._config.web.chat.rate_limit_messages = 2
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        for _ in range(2):
            ok = await chat_client.post(
                f"/istota/api/chat/rooms/{room['id']}/messages",
                json={"text": "hi"}, cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            assert ok.status_code == 200
        blocked = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hi"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        mod._config.web.chat.rate_limit_messages = 30

    async def test_send_to_archived_room_rejected(self, chat_client):
        """An archived room must not accept new messages — it's hidden from the
        UI and shouldn't keep spawning tasks / churning its channel memory."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await chat_client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"archived": True}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "anyone there?"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 409

    async def test_command_runs_inline_no_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!help"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert "inline_result" in body
        assert "!help" in body["inline_result"]
        # A non-structured command carries no command_data payload.
        assert body.get("command_data") is None

    async def test_search_command_returns_structured_data(self, chat_client):
        """!search on the web surface forwards a structured `command_data`
        payload alongside the plain-text fallback."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!search nonexistenttermxyz"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert body.get("command_data") is not None
        assert body["command_data"]["kind"] == "search_results"

    async def test_model_prefix_creates_task_with_override(self, chat_client):
        """`!model <alias> <prompt>` must create a real task carrying the model
        override — it's a prefix, not a command (this was broken before)."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!model opus summarize my day"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is not None
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, body["task_id"])
        assert task.model  # canonical Opus id
        assert task.prompt == "summarize my day"  # prefix stripped

    async def test_model_prefix_unknown_alias_returns_usage_inline(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!model bogus do something"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert "Aliases" in body["inline_result"]

    async def test_send_attaches_uploaded_file_to_task(self, chat_client):
        """An uploaded attachment's path must land on the task's attachments
        column so the brain actually sees the file."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        path = up.json()["path"]
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "summarize this", "attachments": [path]},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, resp.json()["task_id"])
        assert task.attachments == [path]

    async def test_send_drops_foreign_attachment_path(self, chat_client):
        """A path outside the user's web-chat upload root is rejected — a client
        can't get the brain to read arbitrary host paths."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "read this", "attachments": ["/etc/passwd"]},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400


@_needs_web_deps
class TestChatAttachmentPersistence:
    """An attachment chip must survive leaving the room and coming back.

    The chip is rendered from the *history payload*, so the display names have
    to be persisted on the canonical `messages` row — the composer's in-memory
    names are gone the moment the transcript is rebuilt, and `tasks` (the only
    place the paths lived) is GC'd by retention.
    """

    async def _room(self, client, cookies):
        return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"][0]

    async def _upload_path(self, username: str, filename: str) -> str:
        import istota.web_app as mod
        root = mod._chat_upload_roots(username)[0]
        root.mkdir(parents=True, exist_ok=True)
        path = root / filename
        path.write_bytes(b"\x00\x01")
        return str(path)

    async def _send(self, client, cookies, room, payload):
        return await client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json=payload, cookies=cookies,
            headers={"origin": "https://example.com"},
        )

    async def _history(self, client, cookies, room):
        return (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()["messages"]

    async def test_history_carries_the_client_supplied_display_names(self, chat_client):
        """The stored file is `note-a1b2c3d4.txt`; the chip must still read
        `note.txt`, so the client's display names are what get persisted."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        path = up.json()["path"]
        resp = await self._send(chat_client, cookies, room, {
            "text": "summarize this",
            "attachments": [path],
            "attachment_names": ["note.txt"],
        })
        assert resp.status_code == 200
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs and user_msgs[0]["attachments"] == ["note.txt"]

    async def test_names_fall_back_to_the_stored_basename(self, chat_client):
        """A surface that supplies no display names (Talk, or an older web
        client) still gets a chip — derived from the path."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        path = await self._upload_path("alice", "receipt-99.png")
        await self._send(chat_client, cookies, room, {
            "text": "look", "attachments": [path],
        })
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs and user_msgs[0]["attachments"] == ["receipt-99.png"]

    async def test_mismatched_name_count_falls_back_to_basenames(self, chat_client):
        """Names are display-only and positional; a client that sends the wrong
        number of them can't shift a label onto the wrong file."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        one = await self._upload_path("alice", "a-1.png")
        two = await self._upload_path("alice", "b-2.png")
        await self._send(chat_client, cookies, room, {
            "text": "two files", "attachments": [one, two],
            "attachment_names": ["only-one.png"],
        })
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs[0]["attachments"] == ["a-1.png", "b-2.png"]

    async def test_chip_survives_task_retention_cleanup(self, chat_client):
        """`cleanup_old_tasks` deletes the `tasks` row that holds the paths, so
        a names-on-`tasks` fix would lose the chip after a few days."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        path = await self._upload_path("alice", "invoice-7.pdf")
        resp = await self._send(chat_client, cookies, room, {
            "text": "file this", "attachments": [path],
            "attachment_names": ["invoice.pdf"],
        })
        task_id = resp.json()["task_id"]
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs and user_msgs[0]["attachments"] == ["invoice.pdf"]

    async def test_turn_without_attachments_omits_the_field(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await self._send(chat_client, cookies, room, {"text": "just text"})
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs and not user_msgs[0].get("attachments")

    async def test_aggregate_view_carries_attachments(self, chat_client):
        """The cross-room stream/aggregate rows go through the same client
        builder, so they must carry the same field."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        path = await self._upload_path("alice", "shot-3.png")
        await self._send(chat_client, cookies, room, {
            "text": "see this", "attachments": [path],
            "attachment_names": ["shot.png"],
        })
        data = (await chat_client.get(
            "/istota/api/chat/messages?view=all", cookies=cookies,
        )).json()
        user_msgs = [m for m in data["messages"] if m["role"] == "user"]
        assert user_msgs and user_msgs[0]["attachments"] == ["shot.png"]


@_needs_web_deps
class TestChatAttachmentLinks:
    """An attachment chip should open the file it names.

    The link is the session-scoped `/chat/files` endpoint — the user opening a
    file they already own — never a minted Nextcloud share, which would turn a
    private file public to solve a display problem. A file the endpoint can't
    serve (another user's, or one outside the workspace) carries no path, and
    the chip stays inert rather than becoming a dead link.
    """

    async def _room(self, client, cookies):
        return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"][0]

    async def _send(self, client, cookies, room, payload):
        return await client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json=payload, cookies=cookies,
            headers={"origin": "https://example.com"},
        )

    async def _history(self, client, cookies, room):
        return (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()["messages"]

    async def _upload(self, client, cookies, filename, body=b"hello world"):
        return (await client.post(
            "/istota/api/chat/attachments",
            files={"file": (filename, body, "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )).json()

    async def test_upload_returns_the_workspace_path(self, chat_client):
        """The composer needs it to link the chip it renders optimistically —
        the host path it already gets back is not what `/chat/files` takes."""
        cookies = await _login(chat_client, "alice")
        up = await self._upload(chat_client, cookies, "note.txt")
        assert up["workspace_path"].startswith("/Users/alice/inbox/web-chat/")
        assert up["workspace_path"].endswith(".txt")

    async def test_history_carries_the_link_path(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await self._upload(chat_client, cookies, "note.txt")
        await self._send(chat_client, cookies, room, {
            "text": "read this", "attachments": [up["path"]],
            "attachment_names": ["note.txt"],
        })
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs[0]["attachments"] == ["note.txt"]
        assert user_msgs[0]["attachment_paths"] == [up["workspace_path"]]

    async def test_the_link_path_actually_downloads(self, chat_client):
        """End to end: what history hands the chip is what `/chat/files` takes."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await self._upload(chat_client, cookies, "note.txt", b"file bytes")
        await self._send(chat_client, cookies, room, {
            "text": "read this", "attachments": [up["path"]],
            "attachment_names": ["note.txt"],
        })
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": user_msgs[0]["attachment_paths"][0]}, cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.content == b"file bytes"

    async def test_link_survives_task_retention_cleanup(self, chat_client):
        """The paths live only on the `tasks` row retention deletes, so the
        link has to be persisted beside the display names or it rots."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await self._upload(chat_client, cookies, "invoice.pdf")
        resp = await self._send(chat_client, cookies, room, {
            "text": "file this", "attachments": [up["path"]],
            "attachment_names": ["invoice.pdf"],
        })
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            c.execute("DELETE FROM tasks WHERE id = ?", (resp.json()["task_id"],))
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs[0]["attachment_paths"] == [up["workspace_path"]]

    async def test_attachment_outside_the_workspace_has_no_link(self, chat_client):
        """The temp-dir upload root (a mountless deployment's fallback) is not
        under `/Users/<uid>/`, so the endpoint can't serve it."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        import istota.web_app as mod
        root = mod._chat_upload_roots("alice")[1]
        root.mkdir(parents=True, exist_ok=True)
        (root / "stray.png").write_bytes(b"\x00")
        await self._send(chat_client, cookies, room, {
            "text": "look", "attachments": [str(root / "stray.png")],
        })
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs[0]["attachments"] == ["stray.png"]
        assert user_msgs[0].get("attachment_paths") is None

    async def test_turn_without_attachments_omits_the_field(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await self._send(chat_client, cookies, room, {"text": "just text"})
        user_msgs = [m for m in await self._history(chat_client, cookies, room)
                     if m["role"] == "user"]
        assert user_msgs and "attachment_paths" not in user_msgs[0]

    async def test_aggregate_view_carries_the_link_path(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await self._upload(chat_client, cookies, "shot.png")
        await self._send(chat_client, cookies, room, {
            "text": "see this", "attachments": [up["path"]],
            "attachment_names": ["shot.png"],
        })
        data = (await chat_client.get(
            "/istota/api/chat/messages?view=all", cookies=cookies,
        )).json()
        user_msgs = [m for m in data["messages"] if m["role"] == "user"]
        assert user_msgs[0]["attachment_paths"] == [up["workspace_path"]]

    async def test_a_co_members_attachment_is_not_linked(self, chat_client):
        """Rooms are shared. Bob may see alice's chip, but `/chat/files` is
        scoped to the caller's own workspace and would refuse the path — so it
        must not be offered as a link to him."""
        import istota.web_app as mod
        alice = await _login(chat_client, "alice")
        room = await self._room(chat_client, alice)
        up = await self._upload(chat_client, alice, "note.txt")
        await self._send(chat_client, alice, room, {
            "text": "shared", "attachments": [up["path"]],
            "attachment_names": ["note.txt"],
        })
        with db.get_db(mod._config.db_path) as c:
            db.add_room_member(c, room["token"], "bob")
            db.ensure_web_chat_handle(c, "bob", room["token"], "general")
        bob = await _login(chat_client, "bob")
        bob_room = next(
            r for r in (await chat_client.get(
                "/istota/api/chat/rooms", cookies=bob,
            )).json()["rooms"] if r["token"] == room["token"]
        )
        user_msgs = [m for m in await self._history(chat_client, bob, bob_room)
                     if m["role"] == "user"]
        assert user_msgs[0]["attachments"] == ["note.txt"]
        assert user_msgs[0].get("attachment_paths") is None


@_needs_web_deps
class TestChatDeleteApi:
    async def _create_room(self, client, cookies, name):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": name}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()

    async def test_delete_room_ok(self, chat_client):
        cookies = await _login(chat_client, "alice")
        # Establish the default `general` room so deleting `scratch` leaves a
        # room behind (deleting the only room just gets it auto-recreated).
        await chat_client.get("/istota/api/chat/rooms", cookies=cookies)
        room = await self._create_room(chat_client, cookies, "scratch")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        rooms = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"]
        assert room["id"] not in [r["id"] for r in rooms]

    async def test_delete_room_with_active_task_409(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"][0]
        # Sending a message creates a pending (non-terminal) task.
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "do a thing"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 409
        assert "progress" in resp.json()["error"]

    async def test_cannot_delete_other_users_room(self, chat_client):
        alice = await _login(chat_client, "alice")
        room = await self._create_room(chat_client, alice, "secret")
        bob = await _login(chat_client, "bob")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=bob,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_delete_unknown_room_404(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.delete(
            "/istota/api/chat/rooms/99999", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_delete_requires_csrf(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._create_room(chat_client, cookies, "scratch")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
        )
        assert resp.status_code == 403


@_needs_web_deps
class TestChatTaskActions:
    async def _seed_task(self, username, status="running"):
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            room = db.ensure_default_web_chat_room(c, username)
            tid = db.create_task(
                c, prompt="do a thing", user_id=username, source_type="web",
                conversation_token=room.token, output_target="web",
            )
            db.update_task_status(c, tid, status)
        return tid

    async def _seed_parked_pass(self, tid):
        """The event log a task parked at `pending_confirmation` leaves behind.

        Mirrors what `scheduler` emits on that path: the work, then the question
        and the terminal frame (`:2441`, `:2452`).
        """
        import istota.web_app as mod
        kinds = ("task_started", "tool_start", "tool_end", "confirmation", "done")
        with db.get_db(mod._config.db_path) as c:
            for seq, kind in enumerate(kinds, start=1):
                c.execute(
                    "INSERT INTO task_events (task_id, seq, kind, payload)"
                    " VALUES (?,?,?,'{}')",
                    (tid, seq, kind),
                )

    async def test_confirm_preserves_the_parked_attempts_work(self, chat_client):
        """Confirming keeps what the agent did before it asked (ISSUE-235).

        For a task parked at `pending_confirmation` these rows are the only
        durable record of that first pass: the park path writes no
        `execution_trace` (only the completion path does), and the re-run
        overwrites that column with its own. The whole log used to be deleted
        here, which lost the pre-permission tools everywhere at once.
        """
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        await self._seed_parked_pass(tid)
        import istota.web_app as mod
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        with db.get_db(mod._config.db_path) as c:
            assert db.get_task(c, tid).status == "pending"
            kinds = [e["kind"] for e in db.get_task_events(c, tid)]
        assert kinds == ["task_started", "tool_start", "tool_end"]

    async def test_confirm_drops_the_frames_that_would_end_a_replay(
        self, chat_client,
    ):
        """The parked attempt's `confirmation` and `done` must not survive.

        A client streams a confirmed task from seq 0 — the confirm path passes
        no `since_seq`, and neither does a reload that picks the task back up —
        so a surviving `done` closes the stream in `chat_task_stream` before any
        of the re-run reaches the client, and a surviving `confirmation` puts
        the answered card back with nothing to clear it. The question itself
        stays on `tasks.confirmation_prompt`, so nothing is lost by dropping it.
        """
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        await self._seed_parked_pass(tid)
        import istota.web_app as mod
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        # What a replaying client would be served, straight off the stream's
        # own loader rather than a hand-built query.
        replayed = mod._load_task_events(tid, 0)
        assert [e["kind"] for e in replayed] == [
            "task_started", "tool_start", "tool_end",
        ]

    async def test_confirmed_rerun_appends_above_kept_events(self, chat_client):
        """The re-run resumes the seq counter rather than colliding with it.

        This is the claim the removed `delete_task_events` call rested on, so it
        gets pinned: `EventWriter._resume_seq` seeds from
        `get_max_task_event_seq`, so UNIQUE(task_id, seq) holds with the prior
        attempt's rows still in place. Restore the delete and the re-run starts
        at 1 again.
        """
        from istota.events import EventWriter
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        await self._seed_parked_pass(tid)
        import istota.web_app as mod
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        # What the confirmed re-run does: a fresh writer over the same task.
        # `_resume_seq` reads MAX(seq), so the pruned `confirmation`/`done` at 4
        # and 5 free those numbers and the re-run takes 4 — which is only safe
        # because they really are gone from the unique index.
        writer = EventWriter(tid, mod._config.db_path)
        assert writer.emit("task_started").seq == 4
        with db.get_db(mod._config.db_path) as c:
            assert [e["seq"] for e in db.get_task_events(c, tid)] == [1, 2, 3, 4]

    async def test_cancel_pending_confirmation_cancels(self, chat_client):
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/cancel", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            assert db.get_task(c, tid).status == "cancelled"

    async def test_cancel_running_sets_flag(self, chat_client):
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="running")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/cancel", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            flag = c.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0]
            assert flag == 1

    async def test_cannot_confirm_other_users_task(self, chat_client):
        await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        cookies = await _login(chat_client, "bob")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 403

    async def test_confirm_on_running_task_is_a_noop(self, chat_client):
        """Confirming a task that is NOT pending_confirmation approves nothing.

        `db.confirm_task` does not check the status itself, so the gate in
        `_chat_confirm_task` is the whole of it: without it a duplicate click on
        a task already re-running flips a live row back to `pending`.
        """
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="running")
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            c.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) VALUES (?,1,'tool_start','{}')",
                (tid,),
            )
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        with db.get_db(mod._config.db_path) as c:
            # Status untouched, and none of `approve`'s side effects ran.
            assert db.get_task(c, tid).status == "running"
            approvals = c.execute(
                "SELECT COUNT(*) FROM task_logs WHERE task_id = ?"
                " AND message = 'User confirmed task'",
                (tid,),
            ).fetchone()[0]
            assert approvals == 0
            assert len(db.get_task_events(c, tid)) == 1


@_needs_web_deps
class TestChatAttachments:
    async def test_upload_saves_file(self, chat_client):
        import os
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "note.txt"
        assert body["size"] == 11
        assert os.path.exists(body["path"])
        assert "inbox/web-chat" in body["path"].replace(os.sep, "/")

    async def test_stored_name_keeps_the_uploaded_stem(self, chat_client):
        """The inbox is human-browsable: a voice message reads as
        `voice-…-<rand>.webm`, not a bare UUID."""
        import os
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("voice-20260726-131512.webm", b"OggS", "audio/webm")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        stored = os.path.basename(resp.json()["path"])
        assert stored.startswith("voice-20260726-131512-")
        assert stored.endswith(".webm")

    async def test_two_same_named_uploads_do_not_collide(self, chat_client):
        cookies = await _login(chat_client, "alice")
        paths = []
        for _ in range(2):
            resp = await chat_client.post(
                "/istota/api/chat/attachments",
                files={"file": ("note.txt", b"hello", "text/plain")},
                cookies=cookies, headers={"origin": "https://example.com"},
            )
            assert resp.status_code == 200
            paths.append(resp.json()["path"])
        assert paths[0] != paths[1]

    async def test_hostile_filename_cannot_escape_the_attachment_dir(self, chat_client):
        """The name comes from the client, so it is sanitised rather than
        trusted: no separators, no traversal, no leading dot."""
        import os
        from istota.web_app import _attachment_stem

        assert _attachment_stem("../../../etc/passwd") == "passwd"
        assert _attachment_stem("..") == ""
        assert _attachment_stem("....") == ""
        # A Windows-style name reaches a POSIX host with its backslashes intact
        # (they are ordinary characters here, not separators) — the point is
        # that nothing path-shaped survives, not that the name stays pretty.
        for hostile in ("a/b\\c.txt", r"C:\Users\x\file.txt", "x/y.txt", "\u0000null.txt"):
            stem = _attachment_stem(hostile)
            assert "/" not in stem and "\\" not in stem and ".." not in stem

        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("../../escape.txt", b"x", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        path = resp.json()["path"].replace(os.sep, "/")
        assert "inbox/web-chat" in path
        assert ".." not in path

    async def test_unnamed_upload_still_stores(self, chat_client):
        """A name that sanitises to nothing falls back to a plain random one."""
        import os
        from istota.web_app import _save_chat_attachment
        path = _save_chat_attachment("alice", "...", b"x")
        assert os.path.exists(path)
        assert not os.path.basename(path).startswith("-")

    async def test_disallowed_extension_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_oversize_rejected(self, chat_client):
        import istota.web_app as mod
        mod._config.web.chat.max_attachment_mb = 0  # everything is too big
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("a.txt", b"x", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 413
        mod._config.web.chat.max_attachment_mb = 25


# ---------------------------------------------------------------------------
# GET /chat/files — authenticated file handover
# ---------------------------------------------------------------------------


def _workspace_file(tmp_root, username, relative, body="payload\n"):
    """Write a file into a user's workspace and return its Nextcloud path."""
    dest = tmp_root / "mount" / "Users" / username / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    return f"/Users/{username}/{relative}"


@_needs_web_deps
class TestChatFileDownload:
    """Web chat has no outbound attachment channel, so this is how a task hands
    a file over. It exists specifically so the alternative — minting a public
    Nextcloud link to show a user their own file — is never the default."""

    async def test_requires_auth(self, chat_client):
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": "/Users/alice/a.txt"},
        )
        assert resp.status_code == 401

    async def test_serves_a_file_from_the_callers_workspace(self, chat_client, tmp_path):
        nc_path = _workspace_file(tmp_path, "alice", "istota/report.csv", "a,b\n1,2\n")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": nc_path}, cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.text == "a,b\n1,2\n"

    async def test_served_as_an_attachment_never_inline(self, chat_client, tmp_path):
        """Workspace HTML/SVG rendered inline would execute on the app's own
        origin, against the session cookie that just authorized the read."""
        nc_path = _workspace_file(tmp_path, "alice", "page.html", "<script>x</script>")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": nc_path}, cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.headers["x-content-type-options"] == "nosniff"

    async def test_relative_path_resolves_inside_the_workspace(self, chat_client, tmp_path):
        _workspace_file(tmp_path, "alice", "istota/notes.md", "hi\n")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": "istota/notes.md"}, cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.text == "hi\n"

    async def test_cannot_read_another_users_workspace(self, chat_client, tmp_path):
        secret = _workspace_file(tmp_path, "bob", "private.txt", "bob's data\n")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": secret}, cookies=cookies,
        )
        assert resp.status_code == 403
        assert "bob's data" not in resp.text

    async def test_traversal_is_refused(self, chat_client, tmp_path):
        _workspace_file(tmp_path, "bob", "private.txt", "bob's data\n")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": "../bob/private.txt"}, cookies=cookies,
        )
        assert resp.status_code == 403

    async def test_symlink_out_of_the_workspace_is_refused(self, chat_client, tmp_path):
        """A lexical scope check cannot see this one — only realpath can."""
        import os
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("not yours\n")
        link_dir = tmp_path / "mount" / "Users" / "alice"
        link_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link_dir / "escape.txt")

        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": "/Users/alice/escape.txt"}, cookies=cookies,
        )
        assert resp.status_code == 403
        assert "not yours" not in resp.text

    async def test_missing_file_is_404(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": "/Users/alice/nope.txt"}, cookies=cookies,
        )
        assert resp.status_code == 404

    async def test_directory_is_refused(self, chat_client, tmp_path):
        (tmp_path / "mount" / "Users" / "alice" / "istota").mkdir(parents=True)
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": "/Users/alice/istota"}, cookies=cookies,
        )
        assert resp.status_code == 400

    async def test_workspace_root_itself_is_refused(self, chat_client, tmp_path):
        (tmp_path / "mount" / "Users" / "alice").mkdir(parents=True)
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files",
            params={"path": "/Users/alice"}, cookies=cookies,
        )
        assert resp.status_code == 400

    async def test_empty_path_is_refused(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": "  "}, cookies=cookies,
        )
        assert resp.status_code == 400

    async def test_no_mount_says_so_rather_than_500ing(self, chat_client):
        """An rclone deployment has no local workspace; the refusal has to name
        the alternative instead of surfacing as a crash."""
        import istota.web_app as mod
        saved = mod._config.nextcloud_mount_path
        mod._config.nextcloud_mount_path = None
        try:
            cookies = await _login(chat_client, "alice")
            resp = await chat_client.get(
                "/istota/api/chat/files",
                params={"path": "/Users/alice/a.txt"}, cookies=cookies,
            )
            assert resp.status_code == 503
            assert "share link" in resp.json()["error"]
        finally:
            mod._config.nextcloud_mount_path = saved

    async def test_filename_survives_spaces(self, chat_client, tmp_path):
        nc_path = _workspace_file(tmp_path, "alice", "Q3 report.csv", "x\n")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/files", params={"path": nc_path}, cookies=cookies,
        )
        assert resp.status_code == 200
        # RFC 5987 encoding — the browser decodes it back to the real name.
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert "Q3%20report.csv" in disposition


@_needs_web_deps
class TestTheRoomPatchValidatesInTheRoomsNamespace:
    """D5 Rule 2, the web writer.

    `PATCH /api/chat/rooms/{id}` writes `rooms.model`, so an id from the wrong
    model namespace accepted here is the same standing-default defect `!room
    model` had. Both directions in one class, because a rejection alone passes
    against a validator that rejects everything.
    """

    @pytest.fixture
    async def pinned_client(self, tmp_path):
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", room_selectable=["native"])
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    async def _room(self, client, cookies):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()

    async def test_a_native_room_rejects_an_anthropic_id(self, pinned_client):
        client, config = pinned_client
        cookies = await _login(client, "alice")
        created = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, created["token"], "native")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "claude-opus-5"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, created["token"]).model is None

    async def test_the_same_id_is_accepted_when_the_room_is_not_pinned(
        self, pinned_client,
    ):
        """The control. Same deployment, same payload, same endpoint — the only
        difference is the room's brain, which is what the assertion is about."""
        client, config = pinned_client
        cookies = await _login(client, "alice")
        created = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{created['id']}",
            json={"model": "claude-opus-5"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-opus-5"


@_needs_web_deps
class TestKnownRoomModels:
    def test_it_lists_the_brain_it_is_given(self, tmp_path):
        import istota.web_app as mod
        from istota.config import BrainConfig, NativeBrainConfig

        _patch_app(_make_config(tmp_path))
        anthropic = mod._known_room_models(BrainConfig(kind="claude_code"))
        native = mod._known_room_models(
            BrainConfig(kind="native", native=NativeBrainConfig(model="endpoint/m")),
        )
        assert "claude-opus-5" in anthropic
        assert "claude-opus-5" not in native
        assert "endpoint/m" in native

    def test_an_unbuildable_brain_rejects_everything(self, tmp_path):
        """The validator degrades to "reject all" rather than to "accept all" —
        an unusable answer must not widen what may be written."""
        import istota.web_app as mod
        from istota.config import BrainConfig

        _patch_app(_make_config(tmp_path))
        assert mod._known_room_models(BrainConfig(kind="no-such-brain")) == set()


@_needs_web_deps
class TestTheRoomPatchWritesTheBrain:
    """`PATCH /api/chat/rooms/{id}` is the web writer of `rooms.brain`.

    Three questions, each with its own branch and its own case: is the caller an
    admin (D8), is the kind one the operator offers (`room_selectable_kinds`),
    and does the change cross a model namespace (D5 Rule 1). The command layer
    answers all three already, so what these assert is that the endpoint reaches
    the same answers rather than growing a second copy of the rules.
    """

    HDRS = {"origin": "https://example.com"}

    async def _client(self, tmp_path, **brain_kwargs):
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.brain = BrainConfig(**brain_kwargs)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    @pytest.fixture
    async def selectable(self, tmp_path):
        async for pair in self._client(
            tmp_path, kind="claude_code",
            room_selectable=["native", "tmux_claude", "claude_code"],
        ):
            yield pair

    @pytest.fixture
    async def feature_off(self, tmp_path):
        async for pair in self._client(tmp_path, kind="claude_code"):
            yield pair

    async def _room(self, client, cookies):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers=self.HDRS,
        )).json()

    def _brain(self, config, token):
        with db.get_db(config.db_path) as conn:
            return db.get_room(conn, token).brain

    async def test_it_sets_the_column_and_publishes_it(self, selectable):
        client, config = selectable
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        assert resp.json()["brain"] == "native"
        assert self._brain(config, room["token"]) == "native"

    async def test_an_absent_key_leaves_it_alone(self, selectable):
        """The key-presence contract `model` and `effort` already use: a
        name-only rename must not clear a pin the user set on another device."""
        client, config = selectable
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"name": "renamed"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        assert self._brain(config, room["token"]) == "native"

    @pytest.mark.parametrize("value", ["", None])
    async def test_an_empty_value_clears_it(self, selectable, value):
        client, config = selectable
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": value},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        assert resp.json()["brain"] is None
        assert self._brain(config, room["token"]) is None

    async def test_an_unknown_kind_is_refused(self, selectable):
        client, config = selectable
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "no-such-brain"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 400
        assert self._brain(config, room["token"]) is None

    async def test_a_buildable_kind_the_operator_did_not_list_is_refused(
        self, tmp_path,
    ):
        """A separate branch from the unknown-kind one: `tmux_claude` builds
        fine and is simply not on offer here. Refusing only unknown names would
        pass every case above while leaving the allowlist inert."""
        async for client, config in self._client(
            tmp_path, kind="claude_code", room_selectable=["native"],
        ):
            cookies = await _login(client, "alice")
            room = await self._room(client, cookies)
            resp = await client.patch(
                f"/istota/api/chat/rooms/{room['id']}",
                json={"brain": "tmux_claude"}, cookies=cookies, headers=self.HDRS,
            )
            assert resp.status_code == 400
            assert self._brain(config, room["token"]) is None

    async def test_the_shipped_default_refuses_every_kind(self, feature_off):
        """`room_selectable` is empty out of the box, so the feature ships
        inert and the endpoint offers nothing — including the deployment's own
        kind, which is otherwise the most plausible thing to let through."""
        client, config = feature_off
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "claude_code"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 400
        assert self._brain(config, room["token"]) is None


@_needs_web_deps
class TestTheRoomPatchBrainIsAdminGated:
    """D8. The gate is on the *presence* of the key, so a non-admin is refused
    on the clear as well as on the set — and the ownership check that already
    guards this route is not it: every member of a shared room owns their own
    `web_chat_rooms` handle."""

    HDRS = {"origin": "https://example.com"}

    @pytest.fixture
    async def two_users(self, tmp_path):
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.admin_users = {"alice"}
        config.brain = BrainConfig(kind="claude_code", room_selectable=["native"])
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    async def _room(self, client, cookies):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers=self.HDRS,
        )).json()

    async def test_a_non_admin_cannot_set_it(self, two_users):
        client, config = two_users
        cookies = await _login(client, "bob")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).brain is None

    async def test_a_non_admin_cannot_clear_it_either(self, two_users):
        client, config = two_users
        cookies = await _login(client, "bob")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": ""},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).brain == "native"

    async def test_a_non_admin_may_still_set_the_model(self, two_users):
        """The control. The gate is on the `brain` key alone, so the rest of the
        route is unchanged for a non-admin — a 403 on every PATCH would pass
        both assertions above."""
        client, config = two_users
        cookies = await _login(client, "bob")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"model": "claude-opus-5"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).model == "claude-opus-5"

    async def test_an_admin_can_set_it(self, two_users):
        """The other control: the same request from an admin on the same
        deployment succeeds, so the 403s above are about the caller."""
        client, config = two_users
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).brain == "native"


@_needs_web_deps
class TestBrainAndModelInOneBody:
    """D5 Rule 1 on the web writer, and the precedence the spec states: the
    `model` key is applied first, then the brain change clears it if it crossed
    a namespace. The settings modal sends both keys in one PATCH, so this is the
    common path rather than an edge one."""

    HDRS = {"origin": "https://example.com"}

    @pytest.fixture
    async def client_and_config(self, tmp_path):
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.brain = BrainConfig(
            kind="claude_code", room_selectable=["native", "tmux_claude"],
        )
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    async def _room(self, client, cookies):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers=self.HDRS,
        )).json()

    async def test_a_model_for_the_outgoing_brain_is_refused(
        self, client_and_config,
    ):
        """`model` is validated against the brain the request is moving *to*.

        This body used to be accepted and the model silently cleared, because
        the model was applied first and the brain change then took it back out.
        Since the two can now be chosen together (ISSUE-417), an id the incoming
        brain cannot run is a refusal rather than a silent drop — the picker
        offers that brain's own models, so this shape is a client bug rather
        than the ordinary edit it used to be.
        """
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "native", "model": "claude-opus-5"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 400
        # Nothing was written: the refusal happens before `_chat_update_room`.
        with db.get_db(config.db_path) as conn:
            stored = db.get_room(conn, room["token"])
        assert stored.brain is None
        assert stored.model is None

    async def test_a_brain_and_its_own_model_are_set_together(
        self, client_and_config,
    ):
        """The one-save flow this ordering exists for.

        The modal offers the incoming brain's models, so a crossing change and
        a model picked from that brain arrive in one body and both stick —
        where the user previously had to save the brain, wait, and come back to
        pick a model.
        """
        client, config = client_and_config
        config.brain.native.model = "vendor/some-model"
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "native", "model": "vendor/some-model"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["brain"] == "native"
        assert body["model"] == "vendor/some-model"
        # Replaced, not lost, so the report says nothing was cleared.
        assert "cleared" not in body
        with db.get_db(config.db_path) as conn:
            stored = db.get_room(conn, room["token"])
        assert (stored.brain, stored.model) == ("native", "vendor/some-model")

    async def test_a_stored_pin_still_goes_when_the_body_replaces_nothing(
        self, client_and_config,
    ):
        """The clearing rule's own case, unchanged: a brain-only crossing PATCH
        over a room already holding an id from the outgoing namespace."""
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_model_effort(conn, room["token"], "claude-opus-5", None)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["brain"] == "native"
        assert body["model"] is None
        assert body["cleared"] == ["model"]

    async def test_a_move_inside_one_namespace_keeps_the_pin(
        self, client_and_config,
    ):
        """The converse, which is what stops the assertion above passing against
        an implementation that clears unconditionally. `claude_code` and
        `tmux_claude` share the `anthropic` namespace, so the same id runs under
        both and there is nothing to lose."""
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "tmux_claude", "model": "claude-opus-5"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["brain"] == "tmux_claude"
        assert body["model"] == "claude-opus-5"
        assert "cleared" not in body
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).model == "claude-opus-5"

    async def test_it_clears_a_pin_that_was_already_stored(
        self, client_and_config,
    ):
        """A brain-only PATCH still applies the rule — the modal's model select
        is disabled while a crossing change is pending, so this is the shape it
        actually sends."""
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_model_effort(conn, room["token"], "claude-opus-5", "high")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        # Both, because the rule moves the pair it was set as — and naming the
        # effort is what stops the caller finding it silently gone.
        assert resp.json()["cleared"] == ["model", "effort"]
        with db.get_db(config.db_path) as conn:
            stored = db.get_room(conn, room["token"])
        assert stored.model is None
        assert stored.effort is None

    async def test_a_room_with_no_pin_reports_nothing_cleared(
        self, client_and_config,
    ):
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"brain": "native"},
            cookies=cookies, headers=self.HDRS,
        )
        assert "cleared" not in resp.json()

    async def test_an_effort_in_the_body_survives_the_brain_change(
        self, client_and_config,
    ):
        """An effort the caller asked for in *this* request is a fresh choice.

        The stored pair still goes — the model was resolved in the outgoing
        namespace and the effort was set alongside it — but the effort named in
        the body is not part of that pair, and an effort level is a semantic
        rung every brain reads. So it is written after the clear and reported as
        replaced rather than lost.

        This is the ordering change (ISSUE-417) rather than a new rule: the
        model block used to run *first*, so the body's effort was written and
        then taken out again with the stored model, and the response named it in
        `cleared` to stop the caller finding their explicit value silently gone.
        """
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_model_effort(conn, room["token"], "claude-opus-5", "low")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "native", "effort": "high"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        # The model went and is named; the effort was replaced, so it is not.
        assert resp.json()["cleared"] == ["model"]
        with db.get_db(config.db_path) as conn:
            stored = db.get_room(conn, room["token"])
        assert stored.model is None
        assert stored.effort == "high"

    async def test_a_bare_effort_survives_a_brain_change(self, client_and_config):
        """The converse, and the command layer's own rule: with no model pin
        there is nothing namespaced to lose, so an effort level — a semantic
        rung every brain reads — is kept."""
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "native", "effort": "high"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 200
        assert "cleared" not in resp.json()
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).effort == "high"

    async def test_the_model_is_validated_against_the_outgoing_brain(
        self, client_and_config,
    ):
        """The order has a visible consequence: `model` is checked against the
        brain the room has *now*, so an anthropic id sent alongside a move to
        native is accepted by the validator and then cleared by the rule. The
        400 comes only when the room was already pinned."""
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"brain": "claude_code", "model": "claude-opus-5"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.status_code == 400
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room["token"]).brain == "native"


@_needs_web_deps
class TestTheBrainRidesEveryRoomPayload:
    """The client merges a room payload into its own record, so a key one
    producer sends and another omits reads as absent to any consumer that
    replaces rather than spreads (ISSUE-342, the same argument `model` and
    `talk_token` are here for). Four producers, one assertion each."""

    HDRS = {"origin": "https://example.com"}

    @pytest.fixture
    async def client_and_config(self, tmp_path):
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", room_selectable=["native"])
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c, config

    async def _pinned_room(self, client, config, cookies):
        room = (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers=self.HDRS,
        )).json()
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        return room

    async def test_the_listing_carries_it(self, client_and_config):
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._pinned_room(client, config, cookies)
        listing = (await client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"]
        entry = next(r for r in listing if r["id"] == room["id"])
        assert entry["brain"] == "native"

    async def test_the_stream_snapshot_carries_it(self, client_and_config):
        import istota.web_app as mod

        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._pinned_room(client, config, cookies)
        # The snapshot is read-only and skips a registry room with no handle;
        # the listing above minted one.
        await client.get("/istota/api/chat/rooms", cookies=cookies)
        snap = mod._room_snapshot("alice")
        assert snap[room["token"]]["brain"] == "native"

    async def test_the_patch_response_carries_it(self, client_and_config):
        client, config = client_and_config
        cookies = await _login(client, "alice")
        room = await self._pinned_room(client, config, cookies)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room['id']}", json={"name": "renamed"},
            cookies=cookies, headers=self.HDRS,
        )
        assert resp.json()["brain"] == "native"

    def test_the_promote_response_carries_it(self, tmp_path):
        """A fourth producer the spec's list predates. `_promoted_room_dict`'s
        own docstring gives the reason `model` and `effort` ride it, and the
        brain is the same kind of standing room default."""
        import istota.web_app as mod
        from istota.config import BrainConfig

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", room_selectable=["native"])
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", origin="web", user_id="alice",
                             name="general")
            db.add_room_member(conn, "web-alice-1", "alice")
            db.set_room_brain(conn, "web-alice-1", "native")
            handle = db.ensure_web_chat_handle(
                conn, "alice", "web-alice-1", "general",
            )
        payload = mod._promoted_room_dict(handle.id, "web-alice-1", "tk123")
        assert payload["brain"] == "native"
