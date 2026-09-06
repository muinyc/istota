"""Live room-event stream (live-web-chat-room-stream spec).

Stage 1 — `db.list_room_events_since` / `db.max_message_id`: the tail query,
sharing its visibility predicate with the cross-room aggregate views.
Stage 2 — `GET /chat/events` (snapshot / polling fallback) and `GET /chat/stream`
(SSE), plus the gap threshold and the admin connection gauge.
"""

import json
import sqlite3

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebChatConfig, WebConfig

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
    from unittest.mock import AsyncMock, MagicMock

    from httpx import ASGITransport, AsyncClient


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


def _room(conn, token: str, user: str, *, name: str = "room", origin: str = "web"):
    """A registry room the user is a member of — the visibility unit."""
    db.register_room(conn, token, origin=origin, user_id=user, name=name)
    db.add_room_member(conn, token, user)
    return token


# ---------------------------------------------------------------------------
# Stage 1 — the tail query
# ---------------------------------------------------------------------------


class TestListRoomEventsSince:
    def test_includes_member_room_excludes_foreign(self, conn):
        mine = _room(conn, "web-alice-1", "alice")
        theirs = _room(conn, "web-bob-1", "bob")
        db.add_message(conn, mine, role="assistant", body="mine", origin_surface="web")
        db.add_message(conn, theirs, role="assistant", body="theirs", origin_surface="web")
        rows = db.list_room_events_since(conn, "alice", since_id=0)
        assert [r["body"] for r in rows] == ["mine"]
        assert rows[0]["room_token"] == mine

    def test_dismissed_room_excluded(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        db.add_message(conn, token, role="assistant", body="x", origin_surface="web")
        db.dismiss_room(conn, token, "alice")
        assert db.list_room_events_since(conn, "alice", since_id=0) == []

    def test_archived_room_excluded(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        db.add_message(conn, token, role="assistant", body="x", origin_surface="web")
        db.set_room_archived(conn, token, True)
        assert db.list_room_events_since(conn, "alice", since_id=0) == []

    def test_cursor_is_strictly_greater_and_ordered_ascending(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        ids = [
            db.add_message(conn, token, role="assistant", body=f"m{i}",
                           origin_surface="web")
            for i in range(4)
        ]
        rows = db.list_room_events_since(conn, "alice", since_id=ids[1])
        assert [r["msg_id"] for r in rows] == ids[2:]

    def test_limit_truncates_from_the_oldest_end(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        for i in range(5):
            db.add_message(conn, token, role="assistant", body=f"m{i}",
                           origin_surface="web")
        rows = db.list_room_events_since(conn, "alice", since_id=0, limit=2)
        assert [r["body"] for r in rows] == ["m0", "m1"]

    def test_system_rows_present(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        db.add_message(conn, token, role="system", body="alert", origin_surface="web",
                       title="Heads up")
        rows = db.list_room_events_since(conn, "alice", since_id=0)
        assert [(r["role"], r["title"]) for r in rows] == [("system", "Heads up")]

    def test_surface_filter_matches_the_history_endpoint(self, conn):
        """A user row from a non-conversational surface never renders as a turn,
        so it must not stream either — same TRANSCRIPT_SURFACE_FILTER rule."""
        token = _room(conn, "web-alice-1", "alice")
        db.add_message(conn, token, role="user", body="cron prompt",
                       origin_surface="scheduled")
        db.add_message(conn, token, role="user", body="typed", origin_surface="talk")
        rows = db.list_room_events_since(conn, "alice", since_id=0)
        assert [r["body"] for r in rows] == ["typed"]

    def test_task_enrichment_joined(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        tid = db.create_task(conn, prompt="hi", user_id="alice", source_type="web",
                             conversation_token=token)
        db.update_task_status(conn, tid, "completed", result="done")
        db.add_message(conn, token, role="assistant", body="done",
                       origin_surface="web", task_id=tid)
        row = db.list_room_events_since(conn, "alice", since_id=0)[0]
        assert row["status"] == "completed"
        assert row["task_id"] == tid

    def test_starred_flag_is_per_user(self, conn):
        token = _room(conn, "web-shared", "alice")
        db.add_room_member(conn, token, "bob")
        mid = db.add_message(conn, token, role="assistant", body="x",
                             origin_surface="web")
        db.set_message_starred(conn, mid, "alice", True)
        assert db.list_room_events_since(conn, "alice", since_id=0)[0]["starred"]
        assert not db.list_room_events_since(conn, "bob", since_id=0)[0]["starred"]


class TestMaxMessageId:
    def test_empty_store_is_zero(self, conn):
        assert db.max_message_id(conn) == 0

    def test_tracks_the_newest_row(self, conn):
        token = _room(conn, "web-alice-1", "alice")
        mid = db.add_message(conn, token, role="assistant", body="x",
                             origin_surface="web")
        assert db.max_message_id(conn) == mid

    def test_is_global_not_per_user(self, conn):
        """The gate is deliberately global — it only decides whether the
        per-user join is worth running."""
        theirs = _room(conn, "web-bob-1", "bob")
        mid = db.add_message(conn, theirs, role="assistant", body="x",
                             origin_surface="web")
        assert db.max_message_id(conn) == mid


# ---------------------------------------------------------------------------
# Stage 2 — the endpoints
# ---------------------------------------------------------------------------

ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path, **chat_kwargs):
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
            chat=WebChatConfig(**chat_kwargs),
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
async def stream_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


def _db_path():
    import istota.web_app as mod
    return mod._config.db_path


async def _default_room(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return resp.json()["rooms"][0]


def _post(token, *, role="assistant", body="hello", task_id=None, title=None,
          origin="web"):
    with db.get_db(_db_path()) as conn:
        return db.add_message(
            conn, token, role=role, body=body, origin_surface=origin,
            task_id=task_id, title=title,
        )


def _hold_draft(room_token, *, user_id="alice", subject="Re: Invite"):
    """An outbound email the approval gate held, rendered in ``room_token``."""
    from istota import outbound_drafts

    with db.get_db(_db_path()) as conn:
        return outbound_drafts.hold(
            conn,
            user_id=user_id,
            task_id=None,
            room_token=room_token,
            to_addrs=["stranger@example.invalid"],
            cc_addrs=[],
            bcc_addrs=[],
            subject=subject,
            body="Wednesday at two works.",
            html=False,
            in_reply_to=None,
            references=None,
            attachments=[],
            origin_target=None,
            hold_reason="untrusted_recipient",
        )


def _raise_notification(*, user_id="alice", task_id=4242):
    """An open inbox row, written through the source the producers use.

    Actionable, so one row moves both halves of the frame's payload and a
    handler that reported only `open` would fail here.
    """
    from istota.notification_resolvers import confirmation as confirmation_source

    with db.get_db(_db_path()) as conn:
        return confirmation_source.write(
            conn, user_id, task_id=task_id, title="a question", body="",
        )


@_needs_web_deps
class TestRoomEventsSnapshot:
    async def test_requires_auth(self, stream_client):
        resp = await stream_client.get("/istota/api/chat/events")
        assert resp.status_code == 401

    async def test_returns_rows_in_history_shape(self, stream_client):
        cookies = await _login(stream_client, "alice")
        room = await _default_room(stream_client, cookies)
        mid = _post(room["token"], body="hi there")
        resp = await stream_client.get(
            "/istota/api/chat/events?since_id=0", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["gap"] is False
        assert data["cursor"] >= mid
        [ev] = data["events"]
        assert ev["role"] == "assistant"
        assert ev["text"] == "hi there"
        assert ev["msg_id"] == mid
        assert ev["room_token"] == room["token"]
        # Explicit ISO 8601 UTC, matching the history endpoints (a naive value
        # would be parsed as *local* time by the browser).
        assert ev["created_at"].endswith("Z")

    async def test_cursor_advances_past_invisible_rows(self, stream_client):
        """A user with few visible messages on a busy instance must not re-scan
        the same range every tick, so the cursor advances to the max id scanned
        even when nothing was visible."""
        cookies = await _login(stream_client, "alice")
        await _default_room(stream_client, cookies)
        with db.get_db(_db_path()) as conn:
            db.register_room(conn, "web-bob-9", origin="web", user_id="bob",
                             name="bob's")
            db.add_room_member(conn, "web-bob-9", "bob")
            foreign = db.add_message(conn, "web-bob-9", role="assistant",
                                     body="not yours", origin_surface="web")
        resp = await stream_client.get(
            "/istota/api/chat/events?since_id=0", cookies=cookies,
        )
        data = resp.json()
        assert data["events"] == []
        assert data["cursor"] == foreign

    async def test_since_id_is_exclusive(self, stream_client):
        cookies = await _login(stream_client, "alice")
        room = await _default_room(stream_client, cookies)
        first = _post(room["token"], body="one")
        second = _post(room["token"], body="two")
        resp = await stream_client.get(
            f"/istota/api/chat/events?since_id={first}", cookies=cookies,
        )
        assert [e["msg_id"] for e in resp.json()["events"]] == [second]

    async def test_no_new_rows_returns_cursor_unchanged(self, stream_client):
        cookies = await _login(stream_client, "alice")
        room = await _default_room(stream_client, cookies)
        mid = _post(room["token"])
        resp = await stream_client.get(
            f"/istota/api/chat/events?since_id={mid}", cookies=cookies,
        )
        # The deletion tail rides the same response (per-message delete), so
        # this asserts the message half rather than the whole payload.
        body = resp.json()
        assert (body["events"], body["cursor"], body["gap"]) == ([], mid, False)

    async def test_row_cap_trips_a_gap_carrying_the_scanned_max(self, stream_client):
        cookies = await _login(stream_client, "alice")
        room = await _default_room(stream_client, cookies)
        ids = [_post(room["token"], body=f"m{i}") for i in range(4)]
        # limit=2 → 4 pending rows overflow it.
        resp = await stream_client.get(
            "/istota/api/chat/events?since_id=0&limit=2", cookies=cookies,
        )
        data = resp.json()
        assert data["gap"] is True
        assert data["events"] == []
        # The MAX SCANNED id, not the last one that would have been sent —
        # sending the latter would strand the truncated rows.
        assert data["cursor"] == ids[-1]

    async def test_byte_budget_trips_a_gap(self, tmp_path):
        config = _make_config(tmp_path, room_stream_max_bytes=200)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com",
        ) as client:
            cookies = await _login(client, "alice")
            room = await _default_room(client, cookies)
            for _ in range(3):
                _post(room["token"], body="x" * 300)
            resp = await client.get(
                "/istota/api/chat/events?since_id=0", cookies=cookies,
            )
            data = resp.json()
            assert data["gap"] is True
            assert data["events"] == []

    async def test_limit_one_yields_a_fresh_cursor_without_a_backlog(
        self, stream_client,
    ):
        """What the client asks for after a recovery reload: a cursor, not the
        page it is about to discard."""
        cookies = await _login(stream_client, "alice")
        room = await _default_room(stream_client, cookies)
        ids = [_post(room["token"], body=f"m{i}") for i in range(5)]
        resp = await stream_client.get(
            "/istota/api/chat/events?since_id=0&limit=1", cookies=cookies,
        )
        data = resp.json()
        assert data["cursor"] == ids[-1]
        assert data["events"] == []  # gap → nothing replayed

    async def test_foreign_rooms_never_leak(self, stream_client):
        cookies = await _login(stream_client, "alice")
        await _default_room(stream_client, cookies)
        bob_cookies = await _login(stream_client, "bob")
        bob_room = await _default_room(stream_client, bob_cookies)
        _post(bob_room["token"], body="bob's secret")
        alice_cookies = await _login(stream_client, "alice")
        resp = await stream_client.get(
            "/istota/api/chat/events?since_id=0", cookies=alice_cookies,
        )
        assert all(e["text"] != "bob's secret" for e in resp.json()["events"])


@_needs_web_deps
class TestRoomEventsBatchHelper:
    """Direct unit coverage of the batch helper the SSE generator drives."""

    def test_gate_short_circuits_without_touching_the_join(self, tmp_path, monkeypatch):
        import istota.web_app as mod
        _patch_app(_make_config(tmp_path))
        called = []
        real = db.list_room_events_since

        def spy(*a, **kw):
            called.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(db, "list_room_events_since", spy)
        out = mod._room_events_batch("alice", 0)
        assert out == {"events": [], "cursor": 0, "gap": False}
        assert called == []  # empty store → MAX(id) gate answered it


@_needs_web_deps
class TestRoomSnapshotAndDelta:
    def test_snapshot_is_read_only_and_skips_handleless_rooms(self, tmp_path):
        import istota.web_app as mod
        config = _make_config(tmp_path)
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", origin="web", user_id="alice",
                             name="general")
            db.add_room_member(conn, "web-alice-1", "alice")
        # No web_chat_rooms handle yet → no frontend id → skipped, and the
        # snapshot must not create one (that is the rooms endpoint's job).
        assert mod._room_snapshot("alice") == {}
        with db.get_db(config.db_path) as conn:
            assert db.list_web_chat_rooms(conn, "alice") == []
            handle = db.ensure_web_chat_handle(conn, "alice", "web-alice-1", "general")
        snap = mod._room_snapshot("alice")
        assert snap["web-alice-1"]["id"] == handle.id
        assert snap["web-alice-1"]["name"] == "general"

    def test_snapshot_carries_the_room_colour(self, tmp_path):
        """ISSUE-433. `_room_snapshot` builds its own dict rather than calling
        `_room_to_dict`, so a field added to the listing does not arrive here
        for free — and this dict is what `_room_delta_frames` diffs. Omit the
        key and a colour change never reaches the user's other tab.

        The colour is read off the *handle*, not the registry row, because it
        is per-user; every other field in this dict comes from the registry.
        """
        import istota.web_app as mod
        config = _make_config(tmp_path)
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", origin="web", user_id="alice",
                             name="general")
            db.add_room_member(conn, "web-alice-1", "alice")
            handle = db.ensure_web_chat_handle(conn, "alice", "web-alice-1", "general")
            db.update_web_chat_room(conn, handle.id, color="teal")
        snap = mod._room_snapshot("alice")
        assert snap["web-alice-1"]["color"] == "teal"

    def test_a_colour_change_produces_a_delta_frame(self, tmp_path):
        """The half the snapshot key exists for: `_room_delta_frames` compares
        whole dicts, so carrying the key is exactly what makes the change
        propagate."""
        import istota.web_app as mod
        config = _make_config(tmp_path)
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", origin="web", user_id="alice",
                             name="general")
            db.add_room_member(conn, "web-alice-1", "alice")
            handle = db.ensure_web_chat_handle(conn, "alice", "web-alice-1", "general")
        before = mod._room_snapshot("alice")
        with db.get_db(config.db_path) as conn:
            db.update_web_chat_room(conn, handle.id, color="rose")
        after = mod._room_snapshot("alice")
        frames = mod._room_delta_frames(before, after)
        assert [f for f in frames if f.get("action") == "upsert"
                and f["room"]["color"] == "rose"]

    def test_delta_reports_rename_add_and_remove(self):
        import istota.web_app as mod
        before = {"a": {"id": 1, "token": "a", "name": "old", "origin": "web",
                        "model": None, "effort": None},
                  "b": {"id": 2, "token": "b", "name": "b", "origin": "talk",
                        "model": None, "effort": None}}
        after = {"a": {"id": 1, "token": "a", "name": "new", "origin": "web",
                       "model": None, "effort": None},
                 "c": {"id": 3, "token": "c", "name": "c", "origin": "web",
                       "model": None, "effort": None}}
        frames = mod._room_delta_frames(before, after)
        assert {"action": "upsert", "room": after["a"]} in frames
        assert {"action": "upsert", "room": after["c"]} in frames
        assert {"action": "remove", "token": "b", "id": 2} in frames

    def test_identical_snapshots_produce_no_frames(self):
        import istota.web_app as mod
        snap = {"a": {"id": 1, "token": "a", "name": "a", "origin": "web",
                      "model": None, "effort": None}}
        assert mod._room_delta_frames(snap, dict(snap)) == []


class _FakeRequest:
    """Minimal Request stand-in for driving the SSE generator directly.

    The room stream never terminates on its own, and an ASGI-transport
    `client.stream(...)` can't close a generator that ignores the client going
    away — so the endpoint is called as a function and the disconnect is what
    ends the loop. ``on_check`` fires once per loop iteration, which is the
    hook for mutating the DB *between* ticks.
    """

    def __init__(self, *, headers=None, disconnect_after=2, on_check=None):
        self.headers = headers or {}
        self._checks = 0
        self._limit = disconnect_after
        self._on_check = on_check

    async def is_disconnected(self) -> bool:
        self._checks += 1
        if self._on_check is not None:
            self._on_check(self._checks)
        return self._checks > self._limit


async def _drain(request, **kwargs) -> str:
    """Run the room-stream generator to its (disconnect-driven) end."""
    import istota.web_app as mod
    resp = await mod.chat_room_stream(request, user={"username": "alice"}, **kwargs)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["cache-control"] == "no-cache"
    out = ""
    async for chunk in resp.body_iterator:
        out += chunk if isinstance(chunk, str) else chunk.decode()
    return out


def _frame(buf: str, kind: str) -> dict:
    return json.loads(buf.split(f"event: {kind}\ndata: ", 1)[1].split("\n\n", 1)[0])


@_needs_web_deps
class TestRoomStreamSSE:
    async def test_requires_auth(self, stream_client):
        resp = await stream_client.get("/istota/api/chat/stream")
        assert resp.status_code == 401

    async def _setup(self, tmp_path, **chat_kwargs):
        chat_kwargs.setdefault("room_stream_poll_interval_ms", 5)
        chat_kwargs.setdefault("room_stream_room_check_seconds", 0)
        chat_kwargs.setdefault("room_stream_keepalive_seconds", 3600)
        config = _make_config(tmp_path, **chat_kwargs)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com",
        ) as client:
            cookies = await _login(client, "alice")
            return await _default_room(client, cookies)

    async def test_streams_a_message_frame(self, tmp_path):
        room = await self._setup(tmp_path)
        mid = _post(room["token"], body="live one")
        buf = await _drain(_FakeRequest(disconnect_after=1), since_id=0)
        assert f"id: {mid}" in buf
        payload = _frame(buf, "message")
        assert payload["text"] == "live one"
        assert payload["room_token"] == room["token"]

    async def test_message_written_mid_stream_is_delivered(self, tmp_path):
        """The fast-turn hole: a turn that starts *and* finishes between two
        old 5s polls still writes rows, and both are tailed."""
        room = await self._setup(tmp_path)
        posted: list[int] = []

        def on_check(n):
            if n == 1:
                posted.append(_post(room["token"], body="landed mid-stream"))

        buf = await _drain(
            _FakeRequest(disconnect_after=3, on_check=on_check), since_id=0,
        )
        assert "landed mid-stream" in buf
        assert f"id: {posted[0]}" in buf

    async def test_gap_frame_carries_scanned_max(self, tmp_path):
        room = await self._setup(tmp_path, room_stream_max_batch=2)
        ids = [_post(room["token"], body=f"m{i}") for i in range(5)]
        buf = await _drain(_FakeRequest(disconnect_after=1), since_id=0)
        assert _frame(buf, "gap") == {"cursor": ids[-1]}
        assert "event: message" not in buf

    async def test_keepalive_is_a_comment_frame_with_no_id(self, tmp_path):
        """An auxiliary frame carrying an `id:` would move EventSource's resume
        cursor to the wrong place on reconnect."""
        await self._setup(tmp_path, room_stream_keepalive_seconds=0.001)
        buf = await _drain(_FakeRequest(disconnect_after=3), since_id=0)
        assert ": ping" in buf
        assert "id:" not in buf

    async def test_last_event_id_header_resumes(self, tmp_path):
        room = await self._setup(tmp_path)
        first = _post(room["token"], body="already seen")
        second = _post(room["token"], body="new one")
        buf = await _drain(
            _FakeRequest(headers={"last-event-id": str(first)}, disconnect_after=1),
        )
        assert "already seen" not in buf
        assert "new one" in buf
        assert f"id: {second}" in buf

    async def test_room_frame_on_rename_carries_no_id(self, tmp_path):
        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)

        def on_check(n):
            if n == 2:  # after the first pass established the baseline
                with db.get_db(_db_path()) as c:
                    db.rename_room(c, room["token"], "renamed elsewhere")

        buf = await _drain(_FakeRequest(disconnect_after=4, on_check=on_check))
        frame = _frame(buf, "room")
        assert frame["action"] == "upsert"
        assert frame["room"]["name"] == "renamed elsewhere"
        assert "id:" not in buf

    async def test_first_room_check_emits_no_frames(self, tmp_path):
        """The baseline pass must stay silent — the client already has the room
        list it just loaded."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        buf = await _drain(_FakeRequest(disconnect_after=1))
        assert "event: room" not in buf

    async def test_a_held_draft_is_pushed_as_a_drafts_frame(self, tmp_path):
        """The "appears live without a reload" half of the approval card.

        A draft is written by the email skill in a different process, so the web
        tier learns about it the same way it learns about a Talk turn: by
        polling. It rides the room-check cadence rather than the 1s message
        poll, since it is a snapshot diff rather than a tail.
        """
        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)

        def on_check(n):
            if n == 2:
                _hold_draft(room["token"])

        buf = await _drain(_FakeRequest(disconnect_after=4, on_check=on_check))

        frame = _frame(buf, "drafts")
        [draft] = frame["drafts"]
        assert draft["subject"] == "Re: Invite"
        assert draft["room_token"] == room["token"]
        # Auxiliary, like `room` and `message_deleted` — an `id:` here would move
        # EventSource's resume cursor off the message tail.
        assert "id:" not in buf

    async def test_an_unchanged_draft_set_is_not_re_pushed(self, tmp_path):
        """The whole set rides each frame, so re-sending an unchanged one would
        put a full email body on the wire every room-check tick."""
        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _hold_draft(room["token"])

        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert buf.count("event: drafts") == 1

    async def test_a_resolved_draft_pushes_the_shrunken_set(self, tmp_path):
        """Sent and discarded are the transitions an id-ordered cursor cannot
        carry, and the reason this is a snapshot diff rather than a tail."""
        from istota import outbound_drafts

        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        draft_id = _hold_draft(room["token"])

        def on_check(n):
            if n == 2:
                with db.get_db(_db_path()) as c:
                    outbound_drafts.discard(c, draft_id)

        buf = await _drain(_FakeRequest(disconnect_after=4, on_check=on_check))

        frames = [
            json.loads(chunk.split("data: ", 1)[1].split("\n\n", 1)[0])
            for chunk in buf.split("event: drafts\n")[1:]
        ]
        assert frames[0]["drafts"][0]["id"] == draft_id
        assert frames[-1]["drafts"] == []

    async def test_an_open_notification_is_pushed_as_a_notifications_frame(
        self, tmp_path,
    ):
        """The fast path for the bell, on the one route already holding a
        stream open. The contract is the root layout's 30-second poll, which
        works from every route; this is what makes a question parked while the
        user is reading a room light the bell in about a second instead."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)

        def on_check(n):
            if n == 2:
                _raise_notification()

        buf = await _drain(_FakeRequest(disconnect_after=4, on_check=on_check))

        assert _frame(buf, "notifications") == {"open": 1, "actionable": 1}
        # Auxiliary, like `room` and `drafts` — an `id:` here would move
        # EventSource's resume cursor off the message tail.
        assert "id:" not in buf

    async def test_the_notifications_frame_is_added_beside_drafts(self, tmp_path):
        """Added, not a replacement. The inline `DraftCard` under the turn that
        composed a draft survives the inbox, and `GET /chat/events` carries the
        same `drafts` payload for the documented polling fallback."""
        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)

        def on_check(n):
            if n == 2:
                _hold_draft(room["token"])
                _raise_notification()

        buf = await _drain(_FakeRequest(disconnect_after=4, on_check=on_check))

        assert _frame(buf, "drafts")["drafts"][0]["subject"] == "Re: Invite"
        assert _frame(buf, "notifications")["open"] == 1

    async def test_an_unchanged_count_is_not_re_pushed(self, tmp_path):
        """It rides every room-check tick of every open connection, so an
        unchanged pair must cost nothing."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _raise_notification()

        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert buf.count("event: notifications") == 1

    async def test_a_resolved_notification_pushes_the_lower_count(self, tmp_path):
        """The badge has to come back down without a reload — an answer given
        in Talk closes the row and nothing else would say so here."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _raise_notification()

        def on_check(n):
            if n == 2:
                from istota import notification_store

                with db.get_db(_db_path()) as c:
                    notification_store.resolve_notification(
                        c, "alice", "confirmation", "task:4242", by="talk",
                    )

        buf = await _drain(_FakeRequest(disconnect_after=6, on_check=on_check))

        frames = [
            json.loads(chunk.split("data: ", 1)[1].split("\n\n", 1)[0])
            for chunk in buf.split("event: notifications\n")[1:]
        ]
        assert frames[0]["open"] == 1
        assert frames[-1] == {"open": 0, "actionable": 0}

    async def test_another_users_notification_is_not_counted(self, tmp_path):
        """The count is scoped by the session's user, never by anything on the
        request — rooms are shared, and the bell is not."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _raise_notification(user_id="bob")

        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert "event: notifications" not in buf

    async def test_a_failed_read_skips_the_tick_rather_than_blanking_the_bell(
        self, tmp_path, monkeypatch,
    ):
        """Zero is a claim here, not an absence.

        `notification_store.counts` swallows and answers `{0, 0}` by default,
        which is right for a producer and wrong for a pair that gets *pushed*:
        the frame is a diff against a baseline, so a zero returned for a
        two-second busy timeout differs from the real counts, goes out, and
        blanks the bell over items still waiting. The 2s timeout makes that
        likelier, not rarer.
        """
        import istota.web_app as mod

        await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _raise_notification()

        def boom(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(mod, "_notifications_snapshot", boom)
        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert "event: notifications" not in buf
        # And the rest of the tick is unharmed — one reader failing must not
        # take the stream down with it.
        assert "ping" in buf or "event:" in buf or buf == ""

    async def test_the_room_check_knob_disables_the_frame_too(self, tmp_path):
        """It rides the same tick as the room and draft diffs, which is why
        nothing may be tuned down on its account: the poll is the contract."""
        await self._setup(tmp_path, room_stream_room_check_seconds=0)
        _raise_notification()

        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert "event: notifications" not in buf

    async def test_another_users_draft_is_not_streamed(self, tmp_path):
        """Rooms are shared. A co-member must not be handed the body and
        recipients of someone else's held mail."""
        room = await self._setup(tmp_path, room_stream_room_check_seconds=0.001)
        _hold_draft(room["token"], user_id="bob", subject="Bob's private reply")

        buf = await _drain(_FakeRequest(disconnect_after=4))

        assert "Bob's private reply" not in buf
        if "event: drafts" in buf:
            assert _frame(buf, "drafts")["drafts"] == []

    async def test_connection_gauge_released_on_disconnect(self, tmp_path):
        import istota.web_app as mod
        await self._setup(tmp_path)
        before = mod._room_stream_conn_delta(0)
        await _drain(_FakeRequest(disconnect_after=1))
        assert mod._room_stream_conn_delta(0) == before

    async def test_generator_cursor_advances_past_invisible_rows(self, tmp_path):
        """The gate must keep short-circuiting on a busy multi-user instance.

        `_room_events_batch` advances its cursor past rows this user can't see;
        the generator has to adopt that (not `events[-1]`), or `max_id > cursor`
        stays permanently true and the per-user visibility join runs every tick
        instead of the O(1) MAX(id) probe.
        """
        import istota.web_app as mod
        room = await self._setup(tmp_path)
        _post(room["token"], body="mine")
        with db.get_db(_db_path()) as conn:
            db.register_room(conn, "web-bob-9", origin="web", user_id="bob",
                             name="bob's")
            db.add_room_member(conn, "web-bob-9", "bob")
            db.add_message(conn, "web-bob-9", role="assistant",
                           body="not yours", origin_surface="web")
        seen: list[int] = []
        real = mod._room_events_batch

        def spy(username, since_id, limit=None):
            seen.append(since_id)
            return real(username, since_id, limit)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_room_events_batch", spy)
        try:
            await _drain(_FakeRequest(disconnect_after=3), since_id=0)
        finally:
            monkeypatch.undo()
        with db.get_db(_db_path()) as conn:
            top = db.max_message_id(conn)
        # The last tick asked from the global max, not from alice's own last
        # visible row — so its MAX(id) gate short-circuits.
        assert seen[-1] == top

    async def test_db_lock_skips_the_tick_without_killing_the_stream(
        self, tmp_path, monkeypatch,
    ):
        import sqlite3 as _sqlite3

        import istota.web_app as mod
        room = await self._setup(tmp_path)
        _post(room["token"], body="after the lock")
        calls = {"n": 0}
        real = mod._room_events_batch

        def flaky(username, since_id, limit=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _sqlite3.OperationalError("database is locked")
            return real(username, since_id, limit)

        monkeypatch.setattr(mod, "_room_events_batch", flaky)
        buf = await _drain(_FakeRequest(disconnect_after=2), since_id=0)
        assert "after the lock" in buf


class TestRoomStreamConfig:
    """The knobs must actually reach WebChatConfig — a field added to the
    dataclass but not to the `[web.chat]` parse is silently inert."""

    def test_toml_knobs_are_parsed(self, tmp_path):
        from istota.config import load_config
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            "[web]\nenabled = true\n\n"
            "[web.chat]\n"
            "room_stream_poll_interval_ms = 250\n"
            "room_stream_keepalive_seconds = 7\n"
            "room_stream_max_batch = 42\n"
            "room_stream_max_bytes = 1234\n"
            "room_stream_room_check_seconds = 0\n",
        )
        chat = load_config(cfg_file).web.chat
        assert chat.room_stream_poll_interval_ms == 250
        assert chat.room_stream_keepalive_seconds == 7
        assert chat.room_stream_max_batch == 42
        assert chat.room_stream_max_bytes == 1234
        assert chat.room_stream_room_check_seconds == 0

    def test_defaults_when_block_absent(self, tmp_path):
        from istota.config import load_config
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("[web]\nenabled = true\n")
        chat = load_config(cfg_file).web.chat
        assert chat.room_stream_poll_interval_ms == 1000
        assert chat.room_stream_max_batch == 500


@_needs_web_deps
class TestAdminChatGauge:
    def test_connection_gauge_round_trips(self, tmp_path):
        import istota.web_app as mod
        _patch_app(_make_config(tmp_path))
        before = mod._admin_chat_section()["room_stream_connections"]
        mod._room_stream_conn_delta(1)
        assert mod._admin_chat_section()["room_stream_connections"] == before + 1
        mod._room_stream_conn_delta(-1)
        assert mod._admin_chat_section()["room_stream_connections"] == before

    def test_gauge_never_goes_negative(self, tmp_path):
        import istota.web_app as mod
        _patch_app(_make_config(tmp_path))
        for _ in range(5):
            mod._room_stream_conn_delta(-1)
        assert mod._admin_chat_section()["room_stream_connections"] == 0


@_needs_web_deps
class TestRoomStreamShutdown:
    """The session-lived stream a browser tab always holds open.

    Nothing server-side ends it, so before `istota.web_shutdown` every shutdown
    ran the graceful window out and then cancelled this generator — which
    uvicorn logs as `ERROR: Exception in ASGI application` with a
    `CancelledError` traceback, on an ordinary Ctrl-C.
    """

    @pytest.fixture(autouse=True)
    def _clean_shutdown_state(self):
        from istota import web_shutdown
        web_shutdown.reset_for_tests()
        yield
        web_shutdown.reset_for_tests()

    async def _setup(self, tmp_path, **chat_kwargs):
        chat_kwargs.setdefault("room_stream_poll_interval_ms", 5)
        chat_kwargs.setdefault("room_stream_room_check_seconds", 0)
        chat_kwargs.setdefault("room_stream_keepalive_seconds", 3600)
        config = _make_config(tmp_path, **chat_kwargs)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com",
        ) as client:
            cookies = await _login(client, "alice")
            return await _default_room(client, cookies)

    async def test_a_stop_signal_ends_the_stream(self, tmp_path):
        import asyncio

        from istota import web_shutdown

        await self._setup(tmp_path)

        def on_check(n):
            if n == 2:
                web_shutdown.begin_shutdown()

        # The client never disconnects: the only thing that can end this is the
        # stop notice. A generous limit rather than an unbounded one so a
        # regression fails on the timeout below rather than running forever.
        await asyncio.wait_for(
            _drain(_FakeRequest(disconnect_after=100_000, on_check=on_check)),
            timeout=10,
        )

    async def test_the_wake_beats_the_poll_interval(self, tmp_path):
        """The stop notice must reach a stream *while it sleeps*, not on its
        next tick — a poll interval is up to a second of the graceful window,
        and the log tail's is longer.

        The sibling test above fires the notice from the disconnect check, so
        the generator sees it on the same iteration at the top of the loop and
        the sleep is never exercised. Here the interval is 30s and the deadline
        is 5s, so only a wake can make it.
        """
        import asyncio

        from istota import web_shutdown

        await self._setup(tmp_path, room_stream_poll_interval_ms=30_000)

        async def _drain_it():
            return await _drain(_FakeRequest(disconnect_after=100_000))

        drain = asyncio.create_task(_drain_it())
        await asyncio.sleep(0.3)  # let the generator reach its sleep
        web_shutdown.begin_shutdown()
        await asyncio.wait_for(drain, timeout=5)
