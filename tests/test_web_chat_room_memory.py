"""Web endpoints for a room's channel memory (`CHANNEL.md`) — ISSUE-248.

- GET /istota/api/chat/rooms/{id}/memory
- PUT /istota/api/chat/rooms/{id}/memory

The file already shaped every reply in the room but had no human-facing read
or write path outside the sandboxed skill CLI. These cover the boundary rules
the endpoints carry: owner scoping, the busy refusal a delete already sets,
and the revision check that turns a concurrent agent write into a visible
refusal instead of a silent clobber.
"""

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

pytestmark = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        # Keep the flock anchor inside the test's tmp dir rather than the
        # /tmp/istota default, so parallel workers don't share one.
        temp_dir=tmp_path / "temp",
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
async def chat_env(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c, config


async def _first_room(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return resp.json()["rooms"][0]


def _memory_file(config, token):
    return config.nextcloud_mount_path / "Channels" / token / "CHANNEL.md"


class TestRoomMemoryRead:
    async def test_requires_auth(self, chat_env):
        client, _ = chat_env
        resp = await client.get("/istota/api/chat/rooms/1/memory")
        assert resp.status_code == 401

    async def test_absent_file_reports_empty_and_offers_template(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        resp = await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is False
        assert body["content"] == ""
        # The pane offers to initialize rather than showing a blank box, and
        # the template ships from the server so it can't drift from the one
        # `init_channel_memory` writes.
        assert body["template"].startswith("# Channel Memory")
        assert body["token"] == room["token"]
        assert body["shared"] is False

    async def test_reads_existing_file(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        path = _memory_file(config, room["token"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Channel Memory\n\nAlways answer in Polish.\n")
        resp = await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is True
        assert "Polish" in body["content"]
        assert body["revision"]

    async def test_other_users_room_is_404(self, chat_env):
        client, _ = chat_env
        alice = await _login(client, "alice")
        room = await _first_room(client, alice)
        bob = await _login(client, "bob")
        resp = await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=bob,
        )
        assert resp.status_code == 404

    async def test_unknown_room_is_404(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        resp = await client.get(
            "/istota/api/chat/rooms/99999/memory", cookies=cookies,
        )
        assert resp.status_code == 404


class TestRoomMemoryWrite:
    async def test_save_creates_the_file(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        current = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "# Channel Memory\n\n## Notes\n\n- Ship on Fridays.\n",
                  "revision": current["revision"]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] != current["revision"]
        assert "Ship on Fridays" in _memory_file(config, room["token"]).read_text()

    async def test_save_requires_csrf(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "x", "revision": ""}, cookies=cookies,
        )
        assert resp.status_code == 403

    async def test_cannot_write_another_users_room(self, chat_env):
        client, config = chat_env
        alice = await _login(client, "alice")
        room = await _first_room(client, alice)
        bob = await _login(client, "bob")
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "bob was here", "revision": ""},
            cookies=bob, headers=ORIGIN,
        )
        assert resp.status_code == 404
        assert not _memory_file(config, room["token"]).exists()

    async def test_stale_revision_is_a_conflict_not_a_clobber(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        loaded = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        # An agent write lands between the load and the save.
        path = _memory_file(config, room["token"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Channel Memory\n\nwritten by the agent\n")
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "written by the human", "revision": loaded["revision"]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"
        # The agent's write survives.
        assert "written by the agent" in path.read_text()

    async def test_save_refused_while_a_task_is_running(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        current = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        # Sending a message creates a pending (non-terminal) task in the room.
        await client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "do a thing"}, cookies=cookies, headers=ORIGIN,
        )
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "# Channel Memory\n", "revision": current["revision"]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "busy"
        assert not _memory_file(config, room["token"]).exists()

    async def test_oversized_body_refused(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        current = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "x" * (300 * 1024), "revision": current["revision"]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 413
        assert not _memory_file(config, room["token"]).exists()

    async def test_round_trip_revision_allows_consecutive_saves(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]
        for text in ("first\n", "second\n", "third\n"):
            resp = await client.put(
                f"/istota/api/chat/rooms/{room['id']}/memory",
                json={"content": text, "revision": rev},
                cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200, resp.text
            rev = resp.json()["revision"]
        assert _memory_file(config, room["token"]).read_text() == "third\n"

    async def test_empty_content_clears_without_deleting_the_file(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "", "revision": rev}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        path = _memory_file(config, room["token"])
        assert path.exists() and path.read_text() == ""
        # `read_channel_memory` reports a whitespace-only file as absent, so a
        # cleared file must read back as the empty state rather than as content.
        body = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        assert body["exists"] is False
        assert body["content"] == ""


class TestRoomMemoryAuthorization:
    """The handle is not the membership check — `_chat_memory_room` is."""

    async def test_leaving_a_shared_room_revokes_write(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        # A Talk-origin room is hidden per-user rather than destroyed: the
        # handle row survives with `archived=1` and keeps its integer id, and
        # `get_web_chat_room` applies no archived filter. Membership is what
        # actually goes away.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, room["token"], "alice", origin="talk", name="shared")
            db.remove_room_member(conn, room["token"], "alice")

        read = await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )
        assert read.status_code == 404
        write = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "left but still writing", "revision": ""},
            cookies=cookies, headers=ORIGIN,
        )
        assert write.status_code == 404
        assert not _memory_file(config, room["token"]).exists()

    async def test_talk_origin_room_reports_shared(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE rooms SET origin = 'talk' WHERE token = ?", (room["token"],),
            )
            conn.commit()
        body = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()
        assert body["shared"] is True

    async def test_busy_guard_counts_another_members_task(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]
        # Bob is a member of the same room and has a task in flight against it.
        # CHANNEL.md is one file for the whole room, so his worker may be
        # appending to the file Alice is about to replace.
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, room["token"], "bob")
            db.create_task(
                conn, user_id="bob", prompt="something long-running",
                source_type="web", conversation_token=room["token"],
            )
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "# Channel Memory\n", "revision": rev},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "busy"
        assert not _memory_file(config, room["token"]).exists()


class TestRoomMemoryValidation:
    async def test_non_string_content_is_400(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": 17, "revision": ""}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 400

    async def test_missing_revision_is_400(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        # Required, not optional: a client that may omit it can clobber, which
        # is the whole failure the revision guards.
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "x"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 400

    async def test_malformed_body_is_400_not_500(self, chat_env):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            content=b"{not json", headers={**ORIGIN, "content-type": "application/json"},
            cookies=cookies,
        )
        assert resp.status_code == 400


class TestRoomMemoryFailurePaths:
    async def test_lock_timeout_is_409_locked(self, chat_env, monkeypatch):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]

        from istota.memory.curation.file_lock import MemoryMdLocked

        def _locked(*a, **kw):
            raise MemoryMdLocked("held")

        # The handler imports the name inside the function, so the module
        # attribute is the one that has to move.
        import istota.memory.curation.file_lock as fl
        monkeypatch.setattr(fl, "memory_md_lock", _locked)

        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "held up", "revision": rev},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "locked"
        assert not _memory_file(config, room["token"]).exists()

    async def test_write_failure_is_500_failed(self, chat_env, monkeypatch):
        client, _ = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]

        import istota.storage as storage_mod

        def _boom(*a, **kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(storage_mod, "write_channel_memory", _boom)
        resp = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "doomed", "revision": rev},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 500
        assert resp.json()["code"] == "failed"

    async def test_whitespace_only_save_does_not_wedge_on_conflict(self, chat_env):
        client, config = chat_env
        cookies = await _login(client, "alice")
        room = await _first_room(client, cookies)
        rev = (await client.get(
            f"/istota/api/chat/rooms/{room['id']}/memory", cookies=cookies,
        )).json()["revision"]
        # `read_channel_memory` reports a whitespace-only file as absent, so a
        # revision hashed over the submitted bytes could never be reproduced by
        # the next read and every later save would 409 forever.
        first = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "   \n", "revision": rev}, cookies=cookies, headers=ORIGIN,
        )
        assert first.status_code == 200
        second = await client.put(
            f"/istota/api/chat/rooms/{room['id']}/memory",
            json={"content": "real content\n", "revision": first.json()["revision"]},
            cookies=cookies, headers=ORIGIN,
        )
        assert second.status_code == 200, second.text
        assert _memory_file(config, room["token"]).read_text() == "real content\n"


class TestChannelMemoryStorage:
    """`storage.write_channel_memory` — the mount-side write the endpoint uses."""

    def test_write_is_atomic_and_creates_parents(self, tmp_path):
        from istota import storage
        config = _make_config(tmp_path)
        assert storage.write_channel_memory(config, "web-alice-abc", "hello\n")
        path = config.nextcloud_mount_path / "Channels" / "web-alice-abc" / "CHANNEL.md"
        assert path.read_text() == "hello\n"
        # No staging sibling left behind — os.replace, not a copy. Every
        # entry rather than a `*.tmp` glob, which the staging name no longer
        # matches and which would therefore pass either way.
        assert [p.name for p in path.parent.iterdir()] == ["CHANNEL.md"]

    def test_write_rejects_an_unsafe_token(self, tmp_path):
        from istota import storage
        config = _make_config(tmp_path)
        with pytest.raises(ValueError):
            storage.write_channel_memory(config, "../escape", "x")

    def test_staging_name_is_unique_per_writer(self, tmp_path, monkeypatch):
        """A fixed `CHANNEL.md.tmp` is shared with the memory skill CLI, whose
        lock anchor is per-user — so two members of a shared room stage into one
        file and publish a mixture. The names must not collide."""
        from istota import atomic_write, storage
        config = _make_config(tmp_path)
        seen: list[str] = []
        real_mkstemp = atomic_write.tempfile.mkstemp

        def _record(*a, **kw):
            fd, name = real_mkstemp(*a, **kw)
            # Scoped to this room's directory: `tempfile` is a shared module,
            # so anything else in the process staging a file during the window
            # would otherwise be counted here.
            if pathlib.Path(name).parent.name == "web-alice-abc":
                seen.append(name)
            return fd, name

        # The staging name is minted in `atomic_write` now, not in `storage`.
        monkeypatch.setattr(atomic_write.tempfile, "mkstemp", _record)
        storage.write_channel_memory(config, "web-alice-abc", "one\n")
        storage.write_channel_memory(config, "web-alice-abc", "two\n")
        assert len(seen) == 2 and seen[0] != seen[1]

    def test_write_round_trips_non_ascii(self, tmp_path):
        from istota import storage
        config = _make_config(tmp_path)
        text = "# Channel Memory\n\n- Wysyłaj odpowiedzi po polsku. 🇵🇱\n"
        assert storage.write_channel_memory(config, "web-alice-abc", text)
        # UTF-8 both ways, so the revision the web save hashes matches the read.
        assert storage.read_channel_memory(config, "web-alice-abc") == text

    def test_failed_write_reports_false_and_leaves_no_staging_file(
        self, tmp_path, monkeypatch,
    ):
        from istota import storage
        config = _make_config(tmp_path)
        target = config.nextcloud_mount_path / "Channels" / "web-alice-abc"

        def _boom(*a, **kw):
            raise OSError("no space left on device")

        # `atomic_write` is what calls `os.replace` now. Patched by dotted
        # path rather than through `storage.os`, which reaches the same module
        # object only incidentally and would go quiet if `atomic_write` ever
        # imported `replace` by name.
        monkeypatch.setattr("istota.atomic_write.os.replace", _boom)
        assert storage.write_channel_memory(config, "web-alice-abc", "x") is False
        # Every entry, not `*.tmp`: the staging name carries no suffix now, so
        # a glob for one would pass whether or not anything was left behind.
        assert list(target.iterdir()) == []
