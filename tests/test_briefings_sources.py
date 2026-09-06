"""Tests for the briefings source resolvers (fail-soft contract)."""

import time
from datetime import datetime, timezone

from istota.briefings.sources import SourceContext, resolve_source
from istota.config import BrowserConfig, Config, EmailConfig, UserConfig


def _ctx(tmp_path, *, conn=None, now=None, browser=False, users=("alice",),
         briefings=None):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        browser=BrowserConfig(enabled=browser, api_url="http://browser:9223"),
        users={u: UserConfig(timezone="UTC") for u in users},
    )
    if briefings is not None:
        cfg.briefings = briefings
    return SourceContext(app_config=cfg, user_id="alice", conn=conn, now=now)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_kind_fails_soft(self, tmp_path):
        gs = resolve_source("bogus", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "unknown" in gs.provenance.lower()

    def test_resolver_exception_is_caught(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def boom(config, ctx):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(browse_mod, "resolve", boom)
        # Force cache rebuild so the patched resolver is picked up.
        from istota.briefings import sources as srcpkg
        srcpkg._RESOLVERS._cache = None
        gs = resolve_source("browse", {}, _ctx(tmp_path))
        srcpkg._RESOLVERS._cache = None
        assert gs.ok is False


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class TestRss:
    def test_feeds_off_returns_note(self, tmp_path):
        # Feeds module disabled for the user → soft-degrade.
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig(disabled_modules=["feeds"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice")
        gs = resolve_source("rss", {"feed_ref": {"kind": "category", "value": "world"}}, ctx)
        assert gs.ok is False
        assert "feeds" in gs.provenance.lower()

    def test_reads_recent_entries(self, tmp_path):
        # Real feeds DB with one recent entry.
        from istota.feeds import db as fdb
        from istota.feeds.models import EntryRecord

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig()},
        )
        fctx_db = cfg.module_db_path("alice", "feeds")
        fdb.init_db(fctx_db)
        with fdb.connect(fctx_db) as conn:
            cat = fdb.upsert_category(conn, "world", "World")
            feed_id = fdb.upsert_feed(
                conn, url="http://x/feed", title="X", site_url="http://x",
                source_type="rss", category_id=cat, poll_interval_minutes=30,
            )
            now = datetime.now(timezone.utc).isoformat()
            fdb.insert_entries(conn, feed_id, [
                EntryRecord(id=0, feed_id=feed_id, guid="g1", title="Recent",
                            url="http://x/1", author=None, content_html=None,
                            content_text="body", published_at=now, fetched_at=now),
            ])
            conn.commit()

        ctx = SourceContext(app_config=cfg, user_id="alice")
        gs = resolve_source(
            "rss",
            {"feed_ref": {"kind": "category", "value": "world"}, "limit": 5},
            ctx,
        )
        assert gs.ok is True
        assert gs.items[0]["title"] == "Recent"

    def test_a_subscription_ref_matches_either_spelling(self, tmp_path):
        """A `feed_ref` is whatever the user typed, while a feed added since
        ISSUE-432 is stored canonically. An unresolved subscription ref reads
        as *no filter* below, so a miss widens the block to every feed rather
        than returning nothing."""
        from istota.feeds import db as fdb
        from istota.feeds.models import EntryRecord

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig()},
        )
        fctx_db = cfg.module_db_path("alice", "feeds")
        fdb.init_db(fctx_db)
        now = datetime.now(timezone.utc).isoformat()
        with fdb.connect(fctx_db) as conn:
            wanted = fdb.upsert_feed(
                conn, url="arena:example-channel", title="Wanted", site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            other = fdb.upsert_feed(
                conn, url="http://other/feed", title="Other", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=30,
            )
            fdb.insert_entries(conn, wanted, [
                EntryRecord(id=0, feed_id=wanted, guid="w1", title="Wanted item",
                            url=None, author=None, content_html=None,
                            content_text="body", published_at=now, fetched_at=now),
            ])
            fdb.insert_entries(conn, other, [
                EntryRecord(id=0, feed_id=other, guid="o1", title="Other item",
                            url=None, author=None, content_html=None,
                            content_text="body", published_at=now, fetched_at=now),
            ])
            conn.commit()

        ctx = SourceContext(app_config=cfg, user_id="alice")
        gs = resolve_source(
            "rss",
            {"feed_ref": {"kind": "subscription", "value": "arena:/example-channel"}},
            ctx,
        )
        assert [item["title"] for item in gs.items] == ["Wanted item"]

    def test_missing_category_note(self, tmp_path):
        from istota.feeds import db as fdb

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig()},
        )
        fdb.init_db(cfg.module_db_path("alice", "feeds"))
        ctx = SourceContext(app_config=cfg, user_id="alice")
        gs = resolve_source(
            "rss", {"feed_ref": {"kind": "category", "value": "ghost"}}, ctx,
        )
        assert gs.ok is False
        assert "not found" in gs.provenance.lower()


# ---------------------------------------------------------------------------
# Email (shared pool)
# ---------------------------------------------------------------------------


class _Env:
    """Minimal envelope duck-type for ownership resolution + rendering."""

    def __init__(self, uid, sender, subject="s", to=(), cc=(), references=None):
        self.id = uid
        self.sender = sender
        self.subject = subject
        self.date = "2026-07-20"
        self.snippet = "snippet"
        self.to = to
        self.cc = cc
        self.references = references


class _Full:
    def __init__(self, uid, body):
        self.id = uid
        self.body = body


class TestEmail:
    def test_fail_closed_without_conn(self, tmp_path):
        gs = resolve_source("email", {"mode": "shared"}, _ctx(tmp_path, conn=None))
        assert gs.ok is False
        assert "ownership" in gs.provenance.lower()

    def test_shared_pool_filters_owned(self, tmp_path, monkeypatch):
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig(email_addresses=["alice@x.com"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

        shared = _Env("1", "news@semafor.com")
        owned = _Env("2", "alice@x.com")  # owned by a configured user

        # The resolver imports these lazily from their source modules, so patch
        # at the source (the from-import at call time binds the patched name).
        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails",
            lambda **kw: [shared, owned],
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "Semafor body")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None if e.sender == "news@semafor.com" else "alice",
        )

        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        assert gs.ok is True
        assert len(gs.items) == 1
        assert gs.items[0]["sender"] == "news@semafor.com"
        assert "Semafor body" in gs.items[0]["body"]

    def test_senders_mode_narrows(self, tmp_path, monkeypatch):
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())
        e1 = _Env("1", "news@semafor.com")
        e2 = _Env("2", "digest@axios.com")

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", lambda **kw: [e1, e2])
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "b1"), _Full("2", "b2")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )
        gs = _call_email(
            email_mod,
            {"mode": "senders", "senders": ["*@semafor.com"]},
            ctx,
        )
        assert gs.ok is True
        assert [i["sender"] for i in gs.items] == ["news@semafor.com"]

    def test_windowed_fetch_no_message_cap(self, tmp_path, monkeypatch):
        """Regression: the shared-pool fetch must use a date window with NO
        fixed message cap — a newsletter beyond the old 100th recent message
        still surfaces."""
        import istota.briefings.sources.email as email_mod

        captured = {}

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

        many = [_Env(str(i), f"n{i}@x.com") for i in range(150)]

        def fake_list(**kw):
            captured["limit"] = kw.get("limit")
            captured["criteria"] = kw.get("criteria")
            return many

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", fake_list)
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full(str(i), f"body{i}") for i in range(150)],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )
        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        # No fixed cap: all 150 windowed messages kept, limit passed as None.
        assert captured["limit"] is None
        assert len(gs.items) == 150

    def test_hour_window_trims_day_granular_surplus(self, tmp_path, monkeypatch):
        """IMAP date_gte is day-granular, so the server fetch is over-inclusive.
        A message older than the exact hour cutoff is trimmed client-side so the
        'past Nh' provenance stays honest (a datetime-dated envelope, unlike the
        string-dated mock used elsewhere, exercises the filter)."""
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig()},
        )
        now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object(), now=now)

        recent = _Env("1", "fresh@x.com")
        recent.date = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)  # within 12h
        stale = _Env("2", "stale@x.com")
        stale.date = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)  # >12h, day surplus

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails", lambda **kw: [recent, stale]
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "fresh body")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )

        gs = _call_email(email_mod, {"mode": "shared", "lookback_hours": 12}, ctx)
        assert gs.ok is True
        assert [i["sender"] for i in gs.items] == ["fresh@x.com"]  # stale trimmed
        assert "past 12h" in gs.provenance


def _call_email(email_mod, config, ctx):
    """Invoke the email resolver directly (bypassing the lazy dispatcher cache
    so monkeypatched names are used)."""
    return email_mod.resolve(config, ctx)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------


class TestBrowse:
    def test_browser_off_note(self, tmp_path):
        gs = resolve_source("browse", {"preset": "ap"}, _ctx(tmp_path, browser=False))
        assert gs.ok is False
        assert "browser" in gs.provenance.lower()

    def test_preset_fetch_uses_markdown(self, tmp_path, monkeypatch):
        """Markdown, so a headline keeps its URL (ISSUE-192)."""
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "status": "ok",
                    "markdown": "## Top\n\n* [Headline one](https://apnews.com/a)",
                }

        def _post(url, **kwargs):
            calls.append((url, kwargs["json"]))
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "AP News" in gs.text
        assert "[Headline one](https://apnews.com/a)" in gs.text
        assert calls[0][0].endswith("/render")
        assert calls[0][1]["mode"] == "full"
        assert calls[0][1]["max_chars"] == browse_mod._MARKDOWN_MAX_CHARS

    def test_article_mode_forwarded(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        browse_mod.resolve(
            {"url": "https://example.com/story", "mode": "article", "max_chars": 4000},
            _ctx(tmp_path, browser=True),
        )
        assert calls[0]["mode"] == "article"
        assert calls[0]["max_chars"] == 4000

    def test_unknown_mode_falls_back_to_full(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        browse_mod.resolve(
            {"url": "https://example.com", "mode": "readable"},
            _ctx(tmp_path, browser=True),
        )
        assert calls[0]["mode"] == "full"

    def test_operator_budget_caps_the_markdown_request(self, tmp_path, monkeypatch):
        """[briefings] max_browse_chars is the knob; a source's own wins over it."""
        from istota.config import BriefingsModuleConfig

        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        ctx = _ctx(
            tmp_path, browser=True,
            briefings=BriefingsModuleConfig(max_browse_chars=3000),
        )
        browse_mod.resolve({"preset": "ap"}, ctx)
        assert calls[0]["max_chars"] == 3000

        browse_mod.resolve({"preset": "ap", "max_chars": 8000}, ctx)
        assert calls[1]["max_chars"] == 8000

    def test_truncation_footer_is_kept_out_of_the_prompt(self, tmp_path, monkeypatch):
        """/render's footer names CLI flags that mean nothing to the synthesis model."""
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "status": "ok",
                    "truncated": True,
                    "markdown": (
                        "## Top\n\n* [Headline](https://apnews.com/a)\n\n"
                        "[Markdown truncated at 20000 characters — "
                        "raise --max-chars or switch to --mode article]"
                    ),
                }

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "[Headline](https://apnews.com/a)" in gs.text
        assert "--max-chars" not in gs.text
        assert "Markdown truncated" not in gs.text
        # The fact survives, as provenance rather than an instruction.
        assert "truncated" in gs.provenance

    def test_content_is_marked_untrusted(self, tmp_path, monkeypatch):
        """An arbitrary web page — assembly wraps it in the do-not-follow frame."""
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top"}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.untrusted is True

    def test_client_timeout_outlives_the_container_watchdog(self):
        """Else the client gives up first and the container works on a dead request."""
        import istota.briefings.sources.browse as browse_mod

        # BROWSE_WATCHDOG_DEADLINE_S in docker/browser/browse_api.py.
        assert browse_mod._FETCH_TIMEOUT > 90

    def test_fetches_are_serialized_against_the_single_threaded_browser(
        self, tmp_path, monkeypatch,
    ):
        import threading

        import istota.briefings.sources.browse as browse_mod

        concurrent = []
        active = 0
        guard = threading.Lock()

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top"}

        def _post(*a, **k):
            nonlocal active
            with guard:
                active += 1
                concurrent.append(active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        ctx = _ctx(tmp_path, browser=True)
        threads = [
            threading.Thread(target=browse_mod.resolve, args=({"preset": "ap"}, ctx))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert concurrent, "no fetch ran"
        assert max(concurrent) == 1

    def test_a_source_that_never_gets_the_browser_fails_soft(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        monkeypatch.setattr(browse_mod, "_QUEUE_WAIT_TIMEOUT", 0.01)
        browse_mod._BROWSER_LOCK.acquire()
        try:
            gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        finally:
            browse_mod._BROWSER_LOCK.release()

        assert gs.ok is False
        assert "busy" in gs.provenance

    def test_the_lock_is_released_when_a_fetch_raises(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def _boom(*a, **k):
            raise RuntimeError("browser down")

        monkeypatch.setattr(browse_mod.httpx, "post", _boom)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert browse_mod._BROWSER_LOCK.acquire(timeout=1) is True
        browse_mod._BROWSER_LOCK.release()

    def test_untruncated_render_has_a_plain_provenance(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top", "truncated": False}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.provenance == "frontpage of AP News"

    def test_falls_back_to_text_on_old_browser_image(self, tmp_path, monkeypatch):
        """A container predating /render 404s — degrade, don't fail the source."""
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def _post(url, **kwargs):
            calls.append(url)
            if url.endswith("/render"):
                return _Resp(404, {})
            return _Resp(200, {"status": "ok", "text": "Headline one. Headline two."})

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "Headline one" in gs.text
        assert [c.rsplit("/", 1)[-1] for c in calls] == ["render", "browse"]

    def test_custom_url(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "custom page"}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"url": "https://example.com"}, _ctx(tmp_path, browser=True))
        assert gs.ok is True
        assert "example.com" in gs.text

    def test_empty_render_is_not_ok(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "   "}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert "no content" in gs.provenance

    def test_fetch_failure_is_soft(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def _boom(*a, **k):
            raise RuntimeError("browser down")

        monkeypatch.setattr(browse_mod.httpx, "post", _boom)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert "fetch failed" in gs.provenance

    def test_unknown_preset(self, tmp_path):
        gs = resolve_source("browse", {"preset": "nope"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False

    def test_presets_well_formed(self):
        from istota.briefings.sources.browse import BROWSE_PRESETS

        assert BROWSE_PRESETS, "expected bundled presets"
        for key, preset in BROWSE_PRESETS.items():
            assert key == key.lower() and " " not in key, f"bad slug {key!r}"
            assert preset["name"], f"{key} missing name"
            assert preset["url"].startswith("https://"), f"{key} url not https"
        # The core reputable set must remain available as pick-list keys.
        assert {"ap", "reuters", "guardian", "bbc"} <= set(BROWSE_PRESETS)


# ---------------------------------------------------------------------------
# Builtins — todos / reminders / notes (path is a source property)
# ---------------------------------------------------------------------------


def _write_user_file(ctx, rel: str, content: str):
    """Write a file relative to the user's own /Users/<uid>/ folder."""
    path = ctx.app_config.nextcloud_mount_path / "Users" / ctx.user_id / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestResolveUserPath:
    """The path a user types is relative to their own /Users/<uid>/ folder."""

    def test_relative_scoped_to_user_folder(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert _resolve_user_path("alice", "shared/x.md") == "Users/alice/shared/x.md"
        assert (
            _resolve_user_path("alice", "istota/config/TODO.md")
            == "Users/alice/istota/config/TODO.md"
        )

    def test_blank_is_none(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert _resolve_user_path("alice", "") is None
        assert _resolve_user_path("alice", "   ") is None
        assert _resolve_user_path("alice", None) is None

    def test_own_full_path_passthrough(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert (
            _resolve_user_path("alice", "Users/alice/shared/x.md")
            == "Users/alice/shared/x.md"
        )
        assert (
            _resolve_user_path("alice", "/Users/alice/shared/x.md")
            == "Users/alice/shared/x.md"
        )

    def test_parent_escape_stripped(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        # `..` segments are dropped — can never climb above the user folder.
        assert (
            _resolve_user_path("alice", "../../etc/passwd")
            == "Users/alice/etc/passwd"
        )

    def test_other_user_path_not_honored(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        # A path naming another user is treated as a subpath under *your own*
        # folder (nonexistent), never a cross-user read.
        assert (
            _resolve_user_path("alice", "Users/dana/shared/secret.md")
            == "Users/alice/Users/dana/shared/secret.md"
        )


class TestBuiltinTodos:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("todos", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_todo_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_user_folder_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "istota/config/TODO.md", "- [ ] custom item\n")
        gs = resolve_source("todos", {"path": "istota/config/TODO.md"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["text"] == "- [ ] custom item"

    def test_path_reads_shared_folder_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "shared/team-todo.md", "- [ ] shared item\n")
        gs = resolve_source("todos", {"path": "shared/team-todo.md"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["text"] == "- [ ] shared item"

    def test_plain_dash_bullets(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- buy milk\n- call bank\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- buy milk", "- call bank"]

    def test_star_and_plus_bullets(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "* star item\n+ plus item\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["* star item", "+ plus item"]

    def test_numbered_lists(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "1. first\n2) second\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["1. first", "2) second"]

    def test_checked_items_excluded_unchecked_kept(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "- [ ] pending one\n- [x] done one\n- [X] done two\n* [ ] pending two\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- [ ] pending one", "* [ ] pending two"]

    def test_all_checked_returns_not_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- [x] done one\n- [X] done two\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False

    def test_headings_and_rules_and_blanks_skipped(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "# My todos\n\n- real item\n\n---\n\n## Later\n* another\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- real item", "* another"]

    def test_indented_items_supported(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- parent\n    - child\n\t* deep\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- parent", "- child", "* deep"]

    def test_prose_lines_without_markers_ignored(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "Just some prose.\nAnother sentence.\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False


class TestTodoSectionMembership:
    """Each item records the heading it fell under (ISSUE-207).

    The extractor used to skip heading lines and flatten every bullet into one
    undifferentiated list, so a block directive like "only show items under
    ### NOW" was impossible to honour — by synthesis time the section boundary
    was already gone.
    """

    def test_items_carry_their_section(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "### NOW\n- [ ] ship it\n\n### BACKLOG\n- [ ] someday\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [(i["text"], i["section"]) for i in gs.items] == [
            ("- [ ] ship it", "NOW"),
            ("- [ ] someday", "BACKLOG"),
        ]

    def test_items_before_any_heading_have_no_section(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- loose item\n\n## NOW\n- under now\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == [None, "NOW"]

    def test_most_recent_heading_wins_regardless_of_level(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "# My Todos\n- titled\n## Work\n- work item\n### NOW\n- now item\n"
            "## Personal\n- personal item\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == [
            "My Todos", "Work", "NOW", "Personal",
        ]

    def test_closing_hashes_stripped_from_label(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "### NOW ###\n- item\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.items[0]["section"] == "NOW"

    def test_unnamed_heading_clears_the_section(self, tmp_path):
        ctx = _ctx(tmp_path)
        # A bare "###" names nothing; don't attribute later items to the
        # previous section just because the file had a divider-ish heading.
        _write_user_file(ctx, "TODO.md", "## NOW\n- a\n###\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == ["NOW", None]

    def test_heading_line_is_never_an_item(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "### NOW\n- only item\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- only item"]

    def test_provenance_reports_section_count(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "### NOW\n- a\n### LATER\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.provenance == "2 pending in 2 sections"

    def test_provenance_unchanged_without_sections(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- a\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.provenance == "2 pending"


class TestTodoHeadingDialects:
    """Not every todo file marks its sections with ``###``.

    A file is parsed in a single heading dialect, chosen by what it actually
    contains (ATX > setext > bold > label). One dialect per file is what keeps
    a stray ``Blockers:`` line in a ``###``-headed file from stealing items
    from the section above it.
    """

    def _sections(self, ctx, content):
        _write_user_file(ctx, "TODO.md", content)
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        return [i["section"] for i in gs.items]

    def test_setext_underlined_headings(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert self._sections(
            ctx, "NOW\n===\n- a\n\nBACKLOG\n-------\n- b\n",
        ) == ["NOW", "BACKLOG"]

    def test_setext_underline_is_not_an_item_or_a_rule(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "NOW\n---\n- only item\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- only item"]

    def test_bold_only_lines_as_headings(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert self._sections(
            ctx, "**NOW**\n- a\n\n__LATER__\n- b\n",
        ) == ["NOW", "LATER"]

    def test_label_lines_as_headings(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert self._sections(ctx, "NOW:\n- a\n\nBACKLOG:\n- b\n") == [
            "NOW", "BACKLOG",
        ]

    def test_atx_wins_over_other_styles_in_the_same_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        # The bold line and the colon line are prose here, not section
        # changes — otherwise "only show NOW" would lose the last two items.
        assert self._sections(
            ctx, "### NOW\n- a\n**Reminder**\n- b\nBlockers:\n- c\n",
        ) == ["NOW", "NOW", "NOW"]

    def test_hashtag_lines_do_not_make_a_file_atx(self, tmp_path):
        ctx = _ctx(tmp_path)
        # "#urgent" is a tag, not a heading — it has no space. A file whose
        # only hashes are tags is still parsed in its real dialect.
        assert self._sections(ctx, "**NOW**\n#urgent\n- a\n") == ["NOW"]

    def test_unspaced_hash_still_labels_inside_an_atx_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        # Detection is strict (a real heading establishes the dialect),
        # labelling is lenient (a file that clearly uses ATX gets the benefit
        # of the doubt on a sloppy one).
        assert self._sections(ctx, "# Todos\n- a\n#NOW\n- b\n") == [
            "Todos", "NOW",
        ]

    def test_list_item_is_never_read_as_a_heading(self, tmp_path):
        ctx = _ctx(tmp_path)
        # "- NOW:" is an item; it must not also open a section.
        _write_user_file(ctx, "TODO.md", "- NOW:\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- NOW:", "- b"]
        assert [i["section"] for i in gs.items] == [None, None]

    def test_plain_file_with_no_headings_has_no_sections(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert self._sections(ctx, "- a\n- b\nsome prose\n- c\n") == [
            None, None, None,
        ]


class TestTodoFrontmatter:
    """A todo file kept in a notes vault opens with YAML frontmatter.

    Its sequence values (``tags:\\n  - personal``) read as bullets and its
    mapping keys read as label-style headings, so the block would otherwise
    open with two todos named after the file's own tags.
    """

    def test_frontmatter_values_are_not_todo_items(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "---\ncreated: 2026-07-28\ntags:\n  - todos\n  - personal\n---\n\n"
            "### NOW\n- [ ] real task\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- [ ] real task"]
        assert gs.items[0]["section"] == "NOW"

    def test_frontmatter_keys_are_not_sections(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx, "TODO.md", "---\ntags:\n  - x\n---\n**NOW**\n- [ ] task\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == ["NOW"]

    def test_unterminated_leading_rule_keeps_the_content(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "---\n- a\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- a", "- b"]

    def test_frontmatter_only_stripped_at_the_top(self, tmp_path):
        ctx = _ctx(tmp_path)
        # A rule further down the file is still a rule, not a block opener.
        _write_user_file(ctx, "TODO.md", "- a\n---\nnotes: x\n---\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- a", "- b"]

    def test_rule_delimited_list_is_not_mistaken_for_frontmatter(self, tmp_path):
        ctx = _ctx(tmp_path)
        # Opens with a horizontal rule and closes with another, but carries no
        # mapping key — it's a list, not frontmatter, so nothing is dropped.
        _write_user_file(ctx, "TODO.md", "---\n- a\n---\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- a", "- b"]

    def test_vault_links_flattened_in_items_and_section_labels(self, tmp_path):
        """ISSUE-215: a todo file in the same vault carries the same note-links."""
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx, "TODO.md",
            "### [[NOW]]\n"
            "- reply to [Jane](People/Jane%20Doe.md)\n"
            "- read [the memo](https://example.com/memo)\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == [
            "- reply to Jane",
            "- read [the memo](https://example.com/memo)",
        ]
        assert {i["section"] for i in gs.items} == {"NOW"}

    def test_a_link_cannot_masquerade_as_a_checked_checkbox(self, tmp_path):
        """Parsing reads the file as written; only emitted text is flattened.

        Sanitising the input first turned `- [x](Done.md) ship it` into
        `- x ship it`, so a done item came back as pending.
        """
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- [x](Done.md) ship it\n- [ ] real one\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- [ ] real one"]

    def test_a_link_cannot_establish_the_label_heading_dialect(self, tmp_path):
        """`[Project X:](Project%20X.md)` flattens to a `NOW:`-shaped label.

        Detecting the dialect from the flattened text would invent a section
        and re-attribute every item below it.
        """
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx, "TODO.md", "- alpha\n[Project X:](Project%20X.md)\n- beta\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == [None, None]


class TestTodoSizeCap:
    """``max_source_chars`` applies to todos, dropping whole items.

    The other sources cut mid-string, which is fine for prose and wrong for a
    list — a half-line renders as a todo that says something the file doesn't.
    """

    def _capped_ctx(self, tmp_path, max_chars):
        from istota.config import BriefingsModuleConfig

        return _ctx(
            tmp_path,
            briefings=BriefingsModuleConfig(max_source_chars=max_chars),
        )

    def test_under_the_cap_keeps_everything(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 1000)
        _write_user_file(ctx, "TODO.md", "- a\n- b\n- c\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["text"] for i in gs.items] == ["- a", "- b", "- c"]
        assert gs.provenance == "3 pending"

    def test_over_the_cap_drops_whole_items_from_the_tail(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 30)
        _write_user_file(
            ctx, "TODO.md", "".join(f"- item number {n}\n" for n in range(10)),
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert 0 < len(gs.items) < 10
        # Document order preserved, and every kept line is a whole original.
        assert [i["text"] for i in gs.items] == [
            f"- item number {n}" for n in range(len(gs.items))
        ]

    def test_no_item_is_ever_split(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 25)
        lines = ["- a short one", "- a considerably longer item here", "- x"]
        _write_user_file(ctx, "TODO.md", "\n".join(lines) + "\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        for item in gs.items:
            assert item["text"] in lines

    def test_at_least_one_item_survives_a_tiny_cap(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 1)
        _write_user_file(ctx, "TODO.md", "- a reasonably long first item\n- b\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        # Reporting "no pending todos" for a file full of them would be a lie.
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- a reasonably long first item"]

    def test_provenance_reports_what_was_omitted(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 30)
        _write_user_file(
            ctx, "TODO.md", "".join(f"- item number {n}\n" for n in range(10)),
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        omitted = 10 - len(gs.items)
        assert f"{omitted} more omitted" in gs.provenance
        assert "size cap" in gs.provenance

    def test_zero_means_unlimited(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 0)
        _write_user_file(
            ctx, "TODO.md", "".join(f"- item number {n}\n" for n in range(50)),
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert len(gs.items) == 50
        assert "omitted" not in gs.provenance

    def test_section_count_reflects_kept_items_only(self, tmp_path):
        ctx = self._capped_ctx(tmp_path, 20)
        _write_user_file(
            ctx, "TODO.md", "### NOW\n- keep me\n### BACKLOG\n- dropped\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert [i["section"] for i in gs.items] == ["NOW"]
        assert "2 sections" not in gs.provenance


class TestCapTodoItems:
    """Unit coverage for the budget itself."""

    def _items(self, *pairs):
        return [{"text": t, "section": s} for t, s in pairs]

    def test_section_labels_count_against_the_budget(self):
        from istota.briefings.sources.builtins import _cap_todo_items

        # Items alone are 2 x ("- a" + newline) = 8 chars; the two section
        # labels a renderer emits cost another 12. A budget of 10 fits the
        # items but not the labels, so the second item goes.
        labelled = self._items(("- a", "ALPHA"), ("- b", "BRAVO"))
        kept, dropped = _cap_todo_items(labelled, 10)
        assert (len(kept), dropped) == (1, 1)
        # The same items with no labels fit comfortably.
        kept, dropped = _cap_todo_items(
            self._items(("- a", None), ("- b", None)), 10,
        )
        assert (len(kept), dropped) == (2, 0)

    def test_repeated_section_is_charged_once(self):
        from istota.briefings.sources.builtins import _cap_todo_items

        same = self._items(("- a", "NOW"), ("- b", "NOW"), ("- c", "NOW"))
        kept, dropped = _cap_todo_items(same, 21)  # 5 (label) + 3 x 4 = 17
        assert (len(kept), dropped) == (3, 0)

    def test_empty_input_is_not_reported_as_truncated(self):
        from istota.briefings.sources.builtins import _cap_todo_items

        assert _cap_todo_items([], 100) == ([], 0)


class TestBuiltinReminders:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("reminders", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_reminders_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("reminders", {"path": "reminders.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        _write_user_file(ctx, "shared/reminders.md", "Drink water\n\nStand up straight\n")
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        assert gs.ok is True
        assert gs.text in ("Drink water", "Stand up straight")

    def test_vault_note_link_is_flattened(self, tmp_path):
        """ISSUE-215: the picked reminder reaches synthesis with no dead link."""
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        _write_user_file(
            ctx, "shared/reminders.md",
            "Know when to move on.\n"
            "-- Oliver Burkeman, [Eight secrets to a fairly fulfilled life]"
            "(Eight%20secrets%20to%20a%20fairly%20fulfilled%20life.md)\n",
        )
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        assert gs.ok is True
        assert ".md)" not in gs.text
        assert "-- Oliver Burkeman, Eight secrets to a fairly fulfilled life" in gs.text

    def test_real_link_in_a_reminder_survives(self, tmp_path):
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        _write_user_file(
            ctx, "shared/reminders.md",
            "Read this.\n-- [The Author](https://example.com/essay)\n",
        )
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        assert "[The Author](https://example.com/essay)" in gs.text

    def test_reminder_that_flattens_to_nothing_is_not_a_live_source(self, tmp_path):
        """An empty verbatim block renders a bare header with no body.

        `ok=False` omits the source instead, the same as a missing file.
        """
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        _write_user_file(ctx, "shared/reminders.md", "[[Note|]]\n")
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        # The empty alias falls back to the target, so this one survives.
        assert gs.ok is True and gs.text == "Note"
        _write_user_file(ctx, "shared/reminders.md", "![](diagram.png)\n")
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        assert gs.ok is False
        assert gs.text == ""

    def test_flattening_does_not_reset_the_shuffle_queue(self, tmp_path):
        """The queue is keyed on the *raw* file content, not the flattened text.

        Flattening before hashing would reset every user's queue once on
        upgrade, and again on any future change to the sanitiser.
        """
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        content = "One.\n\nTwo, see [A Note](A%20Note.md)\n"
        _write_user_file(ctx, "shared/reminders.md", content)
        resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
        with db.get_db(ctx.app_config.db_path) as conn:
            state = db.get_reminder_state(conn, ctx.user_id)
        import hashlib
        assert state.content_hash == hashlib.sha256(
            content.encode()).hexdigest()[:16]


class TestBuiltinNotes:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("notes", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_notes(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("notes", {"path": "NOTES.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "istota/notes/agenda.md", "agenda item")
        gs = resolve_source("notes", {"path": "istota/notes/agenda.md"}, ctx)
        assert gs.ok is True
        assert "agenda item" in gs.text

    def test_vault_links_are_flattened(self, tmp_path):
        """ISSUE-215: notes share the reminders exposure — same vault, same links."""
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx, "NOTES.md",
            "Follow up on [Q3 planning](Q3%20planning.md) and [[Budget]].\n"
            "Source: [the report](https://example.com/report)\n",
        )
        gs = resolve_source("notes", {"path": "NOTES.md"}, ctx)
        assert "Follow up on Q3 planning and Budget." in gs.text
        assert "[the report](https://example.com/report)" in gs.text

    def test_size_cap_measures_the_flattened_text(self, tmp_path):
        """The budget has to count what is emitted, not the pre-flatten source."""
        ctx = _ctx(tmp_path)
        ctx.module_config.max_source_chars = 40
        _write_user_file(
            ctx, "NOTES.md",
            "[short](A%20Very%20Long%20Vault%20Note%20Name%20Indeed.md) tail\n",
        )
        gs = resolve_source("notes", {"path": "NOTES.md"}, ctx)
        assert gs.text == "short tail"
        assert "truncated" not in gs.text


# ---------------------------------------------------------------------------
# Builtins — markets (byte-identical wrap)
# ---------------------------------------------------------------------------


class TestBuiltinMarkets:
    def test_wraps_market_data(self, tmp_path, monkeypatch):
        import istota.briefings.sources.builtins as bi

        # A weekday morning so quotes are fetched.
        monday_morning = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = _ctx(tmp_path, now=monday_morning)

        import istota.skills.briefing as briefing_mod
        monkeypatch.setattr(
            briefing_mod, "_fetch_market_data",
            lambda mc, mode, tz_str=None: "📈 MARKETS\nES=F +0.5%",
        )
        gs = bi.resolve_markets({"futures": ["ES=F"]}, ctx)
        assert gs.ok is True
        assert "ES=F" in gs.text

    def test_weekend_no_quotes(self, tmp_path):
        import istota.briefings.sources.builtins as bi
        saturday = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
        ctx = _ctx(tmp_path, now=saturday)
        gs = bi.resolve_markets({}, ctx)
        assert gs.ok is False
        assert "weekend" in gs.provenance.lower()


class TestCleanBody:
    """_clean_body routes HTML newsletters through the link-preserving converter."""

    def test_html_body_keeps_article_links(self):
        from istota.briefings.sources.email import _clean_body
        body = (
            '<html><body><div>'
            '<a href="https://semafor.com/a/iran">Iran tensions</a>'
            '</div><p>Body text.</p></body></html>'
        )
        out = _clean_body(body)
        assert "[Iran tensions](https://semafor.com/a/iran)" in out
        assert "Body text." in out

    def test_plain_body_passes_through(self):
        from istota.briefings.sources.email import _clean_body
        assert _clean_body("just words\nsecond line") == "just words\nsecond line"

    def test_max_links_is_threaded(self):
        from istota.briefings.sources.email import _clean_body
        body = "<html><body>" + "".join(
            f'<div><a href="https://x.com/{i}">item {i}</a></div>' for i in range(5)
        ) + "</body></html>"
        out = _clean_body(body, max_links=2)
        assert out.count("](https://x.com/") == 2

    def test_converter_failure_falls_back_to_strip_html(self, monkeypatch):
        from istota.briefings.sources import email as email_mod

        def boom(*a, **kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(email_mod, "html_to_markdown", boom)
        out = email_mod._clean_body("<html><body><p>Body text.</p></body></html>")
        assert "Body text." in out
        assert "<p>" not in out

    def test_resolve_threads_the_config_cap(self, tmp_path, monkeypatch):
        """The `[briefings] newsletter_max_links_per_source` knob reaches the body."""
        import istota.briefings.sources.email as email_mod
        from istota.config import BriefingsModuleConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig()},
        )
        cfg.briefings = BriefingsModuleConfig(newsletter_max_links_per_source=1)
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

        html = "<html><body>" + "".join(
            f'<div><a href="https://x.com/{i}">item {i}</a></div>' for i in range(4)
        ) + "</body></html>"

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails", lambda **kw: [_Env("1", "n@semafor.com")],
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full", lambda **kw: [_Full("1", html)],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner", lambda config, conn, e: None,
        )

        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["body"].count("](https://x.com/") == 1
