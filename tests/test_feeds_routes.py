"""Tests for the native feeds FastAPI router.

Uses ``fastapi.testclient.TestClient`` against a minimal app that mounts
``istota.feeds.routes.router`` and overrides the auth + context
dependencies to inject a tmp-path-backed FeedsContext. This mirrors how
``web_app.py`` mounts the router under the native backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.feeds import db as feeds_db
from istota.feeds import routes
from istota.feeds.models import EntryRecord, FeedsContext
from istota.feeds.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.feeds.workspace import synthesize_feeds_context


def _seed(ctx: FeedsContext) -> dict:
    """Seed a minimal feeds DB; return ids for assertions."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cat_id = feeds_db.upsert_category(conn, "tumblr", "Tumblr")
        feed_id = feeds_db.upsert_feed(
            conn,
            url="tumblr:nemfrog",
            title="Nemfrog",
            site_url="https://nemfrog.tumblr.com",
            source_type="tumblr",
            category_id=cat_id,
            poll_interval_minutes=30,
        )
        rss_feed_id = feeds_db.upsert_feed(
            conn,
            url="https://example.com/feed.xml",
            title="Example Blog",
            site_url="https://example.com",
            source_type="rss",
            category_id=None,
            poll_interval_minutes=30,
        )
        feeds_db.insert_entries(conn, feed_id, [
            EntryRecord(
                id=0, feed_id=feed_id, guid="post-1", title="Post One",
                url="https://nemfrog.tumblr.com/post/1", author=None,
                content_html="<p>hello world</p>", content_text="hello world",
                image_urls=["https://img.example.com/a.jpg"],
                published_at="2026-05-01T10:00:00+00:00",
                fetched_at="2026-05-02T00:00:00+00:00",
                status="unread",
            ),
            EntryRecord(
                id=0, feed_id=feed_id, guid="post-2", title="Post Two",
                url="https://nemfrog.tumblr.com/post/2", author=None,
                content_html="<p>second</p>", content_text="second",
                image_urls=[], published_at="2026-04-30T10:00:00+00:00",
                fetched_at="2026-05-02T00:00:00+00:00",
                status="read",
            ),
        ])
        feeds_db.insert_entries(conn, rss_feed_id, [
            EntryRecord(
                id=0, feed_id=rss_feed_id, guid="rss-1", title="RSS One",
                url="https://example.com/post/1", author="Alice",
                content_html="<p>rss</p>", content_text="rss", image_urls=[],
                published_at="2026-05-02T08:00:00+00:00",
                fetched_at="2026-05-02T09:00:00+00:00",
                status="unread",
            ),
        ])
        conn.commit()
    return {"cat_id": cat_id, "tumblr_feed_id": feed_id, "rss_feed_id": rss_feed_id}


@pytest.fixture
def ctx(tmp_path: Path) -> FeedsContext:
    c = synthesize_feeds_context("bob", tmp_path)
    c.ensure_dirs()
    feeds_db.init_db(c.db_path)
    return c


@pytest.fixture
def client(ctx: FeedsContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/feeds")
    app.dependency_overrides[require_auth] = lambda: {"username": "bob"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /feeds — response shape consumed by the SvelteKit reader
# ---------------------------------------------------------------------------


class TestGetFeeds:
    def test_returns_feeds_entries_total(self, ctx, client):
        _seed(ctx)
        resp = client.get("/istota/api/feeds")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"feeds", "entries", "total"}
        assert isinstance(body["feeds"], list)
        assert isinstance(body["entries"], list)

    def test_feed_shape(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        # Pick the tumblr feed deterministically.
        feed = next(f for f in body["feeds"] if f["title"] == "Nemfrog")
        assert set(feed.keys()) == {"id", "title", "site_url", "category"}
        assert set(feed["category"].keys()) == {"id", "title"}
        assert feed["category"]["title"] == "Tumblr"

    def test_entry_shape(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry = body["entries"][0]
        expected = {
            "id", "title", "url", "content", "images", "duplicate_image_count",
            "embed_url", "file_url", "media_url", "media_type", "feed",
            "status", "starred", "starred_at", "published_at", "created_at",
        }
        assert set(entry.keys()) == expected
        assert set(entry["feed"].keys()) == {"id", "title", "site_url", "category"}

    def test_a_video_attachment_is_served_as_media_not_as_an_image(
        self, ctx, client,
    ):
        """ISSUE-356. The reader decides between ``<img>`` and ``<video>`` on
        these two fields, so a video that arrives in ``images`` is a broken
        hero no amount of frontend work can fix."""
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feed_id = feeds_db.get_feed_by_url(
                conn, "https://example.com/feed.xml",
            ).id
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="rss-vid", title="Clip",
                    url="https://example.com/post/2", author=None,
                    content_html="<p>a clip</p>", content_text="a clip",
                    image_urls=[],
                    media_url="https://assets.example.com/clip.mp4",
                    media_type="video/mp4",
                    published_at="2026-05-03T08:00:00+00:00",
                    fetched_at="2026-05-03T09:00:00+00:00",
                    status="unread",
                ),
            ])
            conn.commit()

        body = client.get("/istota/api/feeds").json()
        entry = next(e for e in body["entries"] if e["title"] == "Clip")
        assert entry["media_url"] == "https://assets.example.com/clip.mp4"
        assert entry["media_type"] == "video/mp4"
        assert entry["images"] == []

    def test_an_entry_without_media_serves_empty_strings(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry = next(e for e in body["entries"] if e["title"] == "Post One")
        assert entry["media_url"] == ""
        assert entry["media_type"] == ""

    def test_status_filter(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        assert {e["status"] for e in body["entries"]} == {"unread"}
        assert body["total"] == 2  # post-1 + rss-1

    def test_feed_id_filter(self, ctx, client):
        ids = _seed(ctx)
        body = client.get(f"/istota/api/feeds?feed_id={ids['tumblr_feed_id']}").json()
        assert all(e["feed"]["id"] == ids["tumblr_feed_id"] for e in body["entries"])
        assert body["total"] == 2  # both tumblr posts

    def test_category_id_filter(self, ctx, client):
        ids = _seed(ctx)
        body = client.get(f"/istota/api/feeds?category_id={ids['cat_id']}").json()
        # Only the tumblr feed sits under the tumblr category.
        for e in body["entries"]:
            assert e["feed"]["category"]["id"] == ids["cat_id"]

    def test_before_filter(self, ctx, client):
        _seed(ctx)
        cutoff_ts = int(
            datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
        )
        body = client.get(f"/istota/api/feeds?before={cutoff_ts}").json()
        # Only post-2 (2026-04-30) is strictly before the 2026-05-01 cutoff.
        titles = [e["title"] for e in body["entries"]]
        assert "Post Two" in titles
        assert "Post One" not in titles
        assert "RSS One" not in titles


# ---------------------------------------------------------------------------
# PUT /feeds/entries/{id} + batch — writes hit SQLite
# ---------------------------------------------------------------------------


class TestUpdateEntries:
    def test_single_entry_marks_read(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        entry = next(e for e in body["entries"] if e["title"] == "Post One")

        resp = client.put(
            f"/istota/api/feeds/entries/{entry['id']}",
            json={"status": "read"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

        # Verify SQLite was actually mutated.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM feed_entries WHERE id = ?", (entry["id"],),
            ).fetchone()
            assert row["status"] == "read"

    def test_batch_marks_read(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        ids = [e["id"] for e in body["entries"]]
        assert len(ids) == 2

        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": ids, "status": "read"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        # All previously-unread entries are now read.
        body2 = client.get("/istota/api/feeds?status=unread").json()
        assert body2["total"] == 0

    def test_batch_rejects_empty_list(self, client):
        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": [], "status": "read"},
        )
        assert resp.status_code == 400

    def test_rejects_invalid_status(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/entries/1",
            json={"status": "archived"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Starring + GET ?starred=
# ---------------------------------------------------------------------------


class TestStarring:
    def test_entry_response_includes_starred_fields(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        for e in body["entries"]:
            assert "starred" in e
            assert "starred_at" in e
            assert e["starred"] is False

    def test_put_single_toggles_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry_id = body["entries"][0]["id"]

        resp = client.put(
            f"/istota/api/feeds/entries/{entry_id}",
            json={"starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT starred, starred_at FROM feed_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            assert row["starred"] == 1
            assert row["starred_at"] is not None

    def test_put_combined_status_and_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        entry_id = body["entries"][0]["id"]

        resp = client.put(
            f"/istota/api/feeds/entries/{entry_id}",
            json={"status": "read", "starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT status, starred FROM feed_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            assert row["status"] == "read"
            assert row["starred"] == 1

    def test_batch_combined_status_and_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        ids = [e["id"] for e in body["entries"]]

        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": ids, "starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM feed_entries WHERE starred = 1"
            ).fetchone()["c"]
            assert count == len(ids)

    def test_put_rejects_non_bool_starred(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/entries/1", json={"starred": "yes"},
        )
        assert resp.status_code == 400

    def test_get_with_starred_filter(self, ctx, client):
        ids = _seed(ctx)
        # Star one tumblr entry.
        with feeds_db.connect(ctx.db_path) as conn:
            target = conn.execute(
                "SELECT id FROM feed_entries WHERE feed_id = ? LIMIT 1",
                (ids["tumblr_feed_id"],),
            ).fetchone()["id"]
            feeds_db.update_entry_starred(conn, [target], True)
            conn.commit()

        body = client.get("/istota/api/feeds?starred=1").json()
        assert body["total"] == 1
        assert body["entries"][0]["id"] == target
        assert body["entries"][0]["starred"] is True

        # starred=0 returns the unstarred remainder.
        body0 = client.get("/istota/api/feeds?starred=0").json()
        assert body0["total"] == 2

        # Default (no starred param) returns everything.
        body_all = client.get("/istota/api/feeds").json()
        assert body_all["total"] == 3


# ---------------------------------------------------------------------------
# POST /feeds/mark-as-read
# ---------------------------------------------------------------------------


class TestMarkAsReadRoute:
    def test_scope_all(self, ctx, client):
        _seed(ctx)
        resp = client.post("/istota/api/feeds/mark-as-read", json={"scope": "all"})
        assert resp.status_code == 200
        # Two unread entries pre-existed.
        assert resp.json()["updated"] == 2
        body = client.get("/istota/api/feeds?status=unread").json()
        assert body["total"] == 0

    def test_scope_feed(self, ctx, client):
        ids = _seed(ctx)
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "feed", "id": ids["tumblr_feed_id"]},
        )
        assert resp.status_code == 200
        # Only the unread tumblr entry (post-1) flipped; rss-1 still unread.
        assert resp.json()["updated"] == 1
        body = client.get("/istota/api/feeds?status=unread").json()
        assert body["total"] == 1

    def test_scope_category(self, ctx, client):
        ids = _seed(ctx)
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "category", "id": ids["cat_id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1  # post-1 only

    def test_before_id_caps(self, ctx, client):
        _seed(ctx)
        # Find current unread max id.
        body = client.get("/istota/api/feeds?status=unread").json()
        sorted_ids = sorted(e["id"] for e in body["entries"])
        cap = sorted_ids[0]  # only the first
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "all", "before_id": cap},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

    def test_rejects_unknown_scope(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read", json={"scope": "global"},
        )
        assert resp.status_code == 400

    def test_feed_scope_requires_id(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read", json={"scope": "feed"},
        )
        assert resp.status_code == 400

    def test_negative_before_id_rejected(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "all", "before_id": -1},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET/PUT /feeds/config — round-trip
# ---------------------------------------------------------------------------


class TestConfigEndpoint:
    def test_get_returns_empty_for_fresh_workspace(self, ctx, client):
        resp = client.get("/istota/api/feeds/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"] == {"settings": {}, "categories": [], "feeds": []}
        assert body["diagnostics"]["total_feeds"] == 0

    def test_put_persists_to_db(self, ctx, client):
        payload = {
            "config": {
                "settings": {"default_poll_interval_minutes": 45},
                "categories": [{"slug": "blogs", "title": "Blogs"}],
                "feeds": [
                    {
                        "url": "https://example.com/feed.xml",
                        "title": "Example",
                        "category": "blogs",
                    },
                ],
            }
        }
        resp = client.put("/istota/api/feeds/config", json=payload)
        assert resp.status_code == 200
        assert resp.json()["sync"]["feeds_added"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["title"] == "Example"
            cat = conn.execute(
                "SELECT title FROM feed_categories WHERE slug = ?",
                ("blogs",),
            ).fetchone()
            assert cat["title"] == "Blogs"
            assert feeds_db.get_default_poll_interval(conn) == 45

        # GET round-trip: the wire shape coming back matches what was sent.
        body = client.get("/istota/api/feeds/config").json()
        assert body["config"]["settings"] == {"default_poll_interval_minutes": 45}
        assert body["config"]["categories"] == [
            {"slug": "blogs", "title": "Blogs"},
        ]
        urls = [f["url"] for f in body["config"]["feeds"]]
        assert urls == ["https://example.com/feed.xml"]

    def test_put_clearing_a_category_title_resets_it_to_the_slug(self, ctx, client):
        """A cleared title used to be silently discarded (found reviewing ISSUE-346).

        The settings payload is the page's whole document, so it is
        authoritative about a title where it carries the key — a blank one
        included. The blank branch took ``ensure_category``, which exists for
        callers that only know a slug and deliberately does not stomp a title
        set elsewhere, so the save came back 200 with the old title still in
        place. It resets to the slug rather than storing empty because the
        column is NOT NULL and the reader files a falsy title under
        "uncategorized".
        """
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_category(conn, "blogs", "Blogs")
            conn.commit()

        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"categories": [{"slug": "blogs", "title": ""}], "feeds": []}},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feed_categories WHERE slug = ?", ("blogs",),
            ).fetchone()
        assert row["title"] == "blogs"

    def test_put_without_a_title_key_leaves_an_existing_title_alone(self, ctx, client):
        """The other half of the same branch, and why it is keyed on the key.

        A payload that never mentions a title is not asserting one — that is
        the CLI / OPML shape ``ensure_category`` was written for — so it must
        keep what is on file rather than resetting it to the slug.
        """
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_category(conn, "blogs", "Blogs")
            conn.commit()

        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"categories": [{"slug": "blogs"}], "feeds": []}},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feed_categories WHERE slug = ?", ("blogs",),
            ).fetchone()
        assert row["title"] == "Blogs"

    def test_put_rejects_malformed_body(self, client):
        resp = client.put("/istota/api/feeds/config", json={"oops": "no"})
        assert resp.status_code == 400

    def test_put_rejects_feed_without_url(self, client):
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"feeds": [{"title": "no url"}]}},
        )
        assert resp.status_code == 400

    def test_put_removes_feeds_and_categories_dropped_from_payload(self, ctx, client):
        """Wholesale-replace: feeds removed in the UI must not linger in the DB.

        Regression: previously the sidebar still showed the old RSS row after
        re-subscribing as ``tumblr:`` because ``_sync_config_to_db`` was
        upsert-only.
        """
        client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "categories": [
                        {"slug": "rss", "title": "RSS"},
                        {"slug": "tumblr", "title": "Tumblr"},
                    ],
                    "feeds": [
                        {
                            "url": "https://nemfrog.tumblr.com/rss",
                            "title": "Nemfrog RSS",
                            "category": "rss",
                        },
                    ],
                }
            },
        )
        resp = client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "categories": [{"slug": "tumblr", "title": "Tumblr"}],
                    "feeds": [
                        {
                            "url": "tumblr:nemfrog",
                            "title": "Nemfrog",
                            "category": "tumblr",
                        },
                    ],
                }
            },
        )
        assert resp.status_code == 200
        sync = resp.json()["sync"]
        assert sync["feeds_removed"] == 1
        assert sync["categories_removed"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {row["url"] for row in conn.execute("SELECT url FROM feeds")}
            slugs = {
                row["slug"] for row in conn.execute("SELECT slug FROM feed_categories")
            }
        assert urls == {"tumblr:nemfrog"}
        assert slugs == {"tumblr"}

    def test_diagnostics_reflect_seeded_state(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds/config").json()
        diag = body["diagnostics"]
        assert diag["total_feeds"] == 2
        assert diag["total_entries"] == 3
        assert diag["unread_entries"] == 2  # post-1, rss-1


# ---------------------------------------------------------------------------
# OPML import/export
# ---------------------------------------------------------------------------


_SAMPLE_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test export</title></head>
  <body>
    <outline text="Tumblr" title="Tumblr">
      <outline type="rss" text="Nemfrog"
               xmlUrl="http://127.0.0.1:8900/tumblr/nemfrog/feed.xml"
               htmlUrl="https://nemfrog.tumblr.com" />
    </outline>
    <outline type="rss" text="Example"
             xmlUrl="https://example.com/feed.xml"
             htmlUrl="https://example.com" />
  </body>
</opml>
"""


class TestOpml:
    def test_import_rewrites_bridger_urls(self, ctx, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("export.opml", _SAMPLE_OPML, "text/x-opml")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["feeds_added"] == 2
        assert body["rewritten_bridger_urls"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {row["url"] for row in conn.execute("SELECT url FROM feeds")}
        assert "tumblr:nemfrog" in urls
        assert "https://example.com/feed.xml" in urls

    def test_import_rejects_empty(self, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("empty.opml", b"", "text/x-opml")},
        )
        assert resp.status_code == 400

    def test_import_rejects_too_large(self, client):
        big = b"<opml>" + b"x" * (5 * 1024 * 1024 + 1) + b"</opml>"
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("big.opml", big, "text/x-opml")},
        )
        assert resp.status_code == 413

    def test_import_rejects_malformed_xml(self, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("bad.opml", b"<not-xml", "text/x-opml")},
        )
        assert resp.status_code == 400

    def test_export_returns_opml(self, ctx, client):
        # Seed via PUT config, then export.
        client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "feeds": [{"url": "tumblr:nemfrog", "title": "Nemfrog"}],
                }
            },
        )
        resp = client.get("/istota/api/feeds/export-opml")
        assert resp.status_code == 200
        assert "opml" in resp.text.lower()
        assert "tumblr:nemfrog" in resp.text
        assert "attachment" in resp.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# GET /feeds — cross-entry image suppression (ISSUE-162)
# ---------------------------------------------------------------------------


IMG = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
IMG_BIG = "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash.jpg"
OTHER_IMG = "https://64.media.tumblr.com/ccc/ddd-01/s500x750/other.jpg"


def _seed_reblog_pair(ctx, *, older_published: str, newer_published: str,
                      same_feed: bool = True, window_days: int | None = None):
    """Two entries carrying the same picture, newest first by publication."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cat_id = feeds_db.upsert_category(conn, "art", "Art")
        feed_a = feeds_db.upsert_feed(
            conn, url="tumblr:a", title="A", site_url=None,
            source_type="tumblr", category_id=cat_id, poll_interval_minutes=60,
        )
        feed_b = feed_a if same_feed else feeds_db.upsert_feed(
            conn, url="tumblr:b", title="B", site_url=None,
            source_type="tumblr", category_id=None, poll_interval_minutes=60,
        )
        feeds_db.insert_entries(conn, feed_a, [
            EntryRecord(
                id=0, feed_id=feed_a, guid="newer", title="Newer", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[IMG_BIG], published_at=newer_published,
                fetched_at=newer_published, status="unread",
            ),
        ])
        feeds_db.insert_entries(conn, feed_b, [
            EntryRecord(
                id=0, feed_id=feed_b, guid="older", title="Older", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[IMG, OTHER_IMG], published_at=older_published,
                fetched_at=older_published, status="unread",
            ),
        ])
        if window_days is not None:
            feeds_db.set_image_dedupe_window_days(conn, window_days)
        conn.commit()
    return {"feed_a": feed_a, "feed_b": feed_b, "cat_id": cat_id}


def _by_title(body) -> dict:
    return {e["title"]: e for e in body["entries"]}


class TestImageSuppression:
    def test_repeat_inside_window_is_hidden_on_the_older_entry(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        # Both entries still render — only the repeated tile goes.
        assert set(entries) == {"Newer", "Older"}
        assert entries["Newer"]["images"] == [IMG_BIG]
        assert entries["Newer"]["duplicate_image_count"] == 0
        assert entries["Older"]["images"] == [OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 1

    def test_repeat_outside_window_renders(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-05-01T10:00:00+00:00",
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_window_zero_disables_suppression(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            window_days=0,
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_configured_window_is_honoured(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-06-20T10:00:00+00:00",  # 26 days
            window_days=30,
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [OTHER_IMG]

    def test_feed_filter_scopes_the_lookup(self, ctx, client):
        """Viewing one blog must not hide tiles because of another blog."""
        ids = _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            same_feed=False,
        )
        entries = _by_title(
            client.get(f"/istota/api/feeds?feed_id={ids['feed_b']}").json()
        )

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_category_filter_scopes_the_lookup(self, ctx, client):
        ids = _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            same_feed=False,
        )
        # Only feed A is in the category, so within that view nothing repeats.
        entries = _by_title(
            client.get(f"/istota/api/feeds?category_id={ids['cat_id']}").json()
        )

        assert set(entries) == {"Newer"}
        assert entries["Newer"]["images"] == [IMG_BIG]

    def test_owner_outside_the_page_still_suppresses(self, ctx, client):
        """Paging must not resurrect a tile the previous page already showed."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        body = client.get("/istota/api/feeds?limit=1&offset=1").json()

        assert [e["title"] for e in body["entries"]] == ["Older"]
        assert body["entries"][0]["images"] == [OTHER_IMG]

    def test_read_state_does_not_change_suppression(self, ctx, client):
        """Marking the newer entry read (as the reader does while scrolling)
        must not make the hidden tile pop back into view."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        with feeds_db.connect(ctx.db_path) as conn:
            newer = next(
                e for e in feeds_db.list_entries(conn) if e.guid == "newer"
            )
            feeds_db.update_entry_status(conn, [newer.id], "read")
            conn.commit()

        entries = _by_title(client.get("/istota/api/feeds?status=unread").json())

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [OTHER_IMG]

    def test_starred_view_keeps_the_image_you_starred(self, ctx, client):
        """A starred post must not lose its picture to an unstarred repeat."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        with feeds_db.connect(ctx.db_path) as conn:
            older = next(
                e for e in feeds_db.list_entries(conn) if e.guid == "older"
            )
            feeds_db.update_entry_starred(conn, [older.id], True)
            conn.commit()

        entries = _by_title(client.get("/istota/api/feeds?starred=1").json())

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0


class TestImageDedupeWindowConfig:
    def test_get_config_reports_the_window(self, ctx, client):
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_image_dedupe_window_days(conn, 21)
            conn.commit()

        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert settings["image_dedupe_window_days"] == 21

    def test_put_config_round_trips_the_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": 7},
                "categories": [],
                "feeds": [],
            }},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_image_dedupe_window_days(conn) == 7

    def test_put_config_accepts_zero_as_off(self, ctx, client):
        _seed(ctx)
        client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": 0},
                "categories": [], "feeds": [],
            }},
        )
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_image_dedupe_window_days(conn) == 0

    def test_put_config_rejects_a_non_int_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": "soon"},
                "categories": [], "feeds": [],
            }},
        )
        assert resp.status_code == 400

    def test_put_config_rejects_a_negative_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": -1},
                "categories": [], "feeds": [],
            }},
        )
        assert resp.status_code == 400


class TestRetentionSettingsConfig:
    """The ``entry_retention_days`` / ``max_entries_per_feed`` wire shape.

    Both are per-user settings stored in ``schema_meta``. ``0`` is a real
    value on each ("no age pruning", "no maximum") and is distinct from unset,
    which resolves to the constant — so every assertion here has to tell those
    two apart rather than treating a falsy read as absent.
    """

    def _put(self, client, settings: dict, feeds: list[dict] | None = None):
        return client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": settings,
                "categories": [],
                "feeds": feeds if feeds is not None else [],
            }},
        )

    # -- GET ----------------------------------------------------------------

    def test_get_omits_both_settings_when_unset(self, ctx, client):
        # A fresh database stores neither row, and the page's placeholder is
        # what shows the default. Sending a number the user never chose would
        # make the next save store it.
        _seed(ctx)
        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert "entry_retention_days" not in settings
        assert "max_entries_per_feed" not in settings

    def test_get_reports_stored_values(self, ctx, client):
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 30)
            feeds_db.set_max_entries_per_feed(conn, 250)
            conn.commit()

        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert settings["entry_retention_days"] == 30
        assert settings["max_entries_per_feed"] == 250

    def test_get_reports_zero_rather_than_omitting_it(self, ctx, client):
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()

        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert settings["entry_retention_days"] == 0
        assert settings["max_entries_per_feed"] == 0

    # -- PUT ----------------------------------------------------------------

    def test_put_round_trips_both_settings(self, ctx, client):
        _seed(ctx)
        resp = self._put(
            client, {"entry_retention_days": 45, "max_entries_per_feed": 1200},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) == 45
            assert feeds_db.get_max_entries_per_feed(conn) == 1200

    def test_put_stores_zero_as_a_value(self, ctx, client):
        _seed(ctx)
        assert self._put(
            client, {"entry_retention_days": 0, "max_entries_per_feed": 0},
        ).status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) == 0
            assert feeds_db.get_max_entries_per_feed(conn) == 0

    def test_put_without_the_keys_clears_both_stored_rows(self, ctx, client):
        # How the page clears a field: blanking the input deletes the key from
        # the payload, and the setting then falls back to the constant. The
        # wire has no separate "reset" verb.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 45)
            feeds_db.set_max_entries_per_feed(conn, 1200)
            conn.commit()

        assert self._put(client, {}).status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) is None
            assert feeds_db.get_max_entries_per_feed(conn) is None

    def test_put_preserves_the_other_settings(self, ctx, client):
        # The four settings share one object, and the save is wholesale — so a
        # retention edit must not drop the poll interval or the image window.
        _seed(ctx)
        resp = self._put(client, {
            "default_poll_interval_minutes": 45,
            "image_dedupe_window_days": 21,
            "entry_retention_days": 30,
            "max_entries_per_feed": 900,
        })
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_default_poll_interval(conn) == 45
            assert feeds_db.get_image_dedupe_window_days(conn) == 21
            assert feeds_db.get_entry_retention_days(conn) == 30
            assert feeds_db.get_max_entries_per_feed(conn) == 900

        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert settings == {
            "default_poll_interval_minutes": 45,
            "image_dedupe_window_days": 21,
            "entry_retention_days": 30,
            "max_entries_per_feed": 900,
        }

    # -- Validation ---------------------------------------------------------

    @pytest.mark.parametrize("key", [
        "entry_retention_days", "max_entries_per_feed",
    ])
    @pytest.mark.parametrize("bad", [-1, True, False, "90", "", 1.5, []])
    def test_put_rejects_a_malformed_value_without_storing_anything(
        self, ctx, client, key, bad,
    ):
        # Re-reading afterwards is the load-bearing half: a rejected payload
        # must not have written the feeds or categories half of the document
        # either, and a 400 alone cannot show that.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 90)
            feeds_db.set_max_entries_per_feed(conn, 5000)
            conn.commit()

        resp = self._put(client, {key: bad})
        assert resp.status_code == 400
        assert key in resp.json()["error"]
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) == 90
            assert feeds_db.get_max_entries_per_feed(conn) == 5000

    @pytest.mark.parametrize("key", [
        "entry_retention_days", "max_entries_per_feed",
    ])
    def test_put_treats_an_explicit_null_as_absent(self, ctx, client, key):
        # JSON `null` is the one non-integer that is not a 400: it means the
        # same thing an absent key does, so it clears rather than failing.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 90)
            feeds_db.set_max_entries_per_feed(conn, 5000)
            conn.commit()

        assert self._put(client, {key: None}).status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            reader = (
                feeds_db.get_entry_retention_days
                if key == "entry_retention_days"
                else feeds_db.get_max_entries_per_feed
            )
            assert reader(conn) is None

    def test_put_rejects_a_boolean_even_though_python_calls_it_an_int(
        self, ctx, client,
    ):
        # The control for the `isinstance(value, bool)` guard: without it
        # `True` stores as `1`, which is a one-entry-per-feed maximum.
        _seed(ctx)
        resp = self._put(client, {"max_entries_per_feed": True})
        assert resp.status_code == 400
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_max_entries_per_feed(conn) is None

    # -- The count-change refetch -------------------------------------------

    STALE = ('"abc"', "Wed, 01 Apr 2026 00:00:00 GMT", "2099-01-01T00:00:00+00:00")

    def _stale_validators(self, ctx) -> None:
        with feeds_db.connect(ctx.db_path) as conn:
            conn.execute(
                "UPDATE feeds SET etag = ?, last_modified = ?, next_poll_at = ?",
                self.STALE,
            )
            conn.commit()

    def _feed_payload(self, ctx) -> list[dict]:
        # The PUT is wholesale-replace, so a payload that omits a feed deletes
        # it — and a deleted feed would satisfy "no stale validator" for the
        # wrong reason.
        with feeds_db.connect(ctx.db_path) as conn:
            return [{"url": f.url} for f in feeds_db.list_feeds(conn)]

    def _validator_state(self, ctx) -> list[tuple]:
        with feeds_db.connect(ctx.db_path) as conn:
            return [
                (f.etag, f.last_modified, f.next_poll_at)
                for f in feeds_db.list_feeds(conn)
            ]

    def test_a_changed_maximum_clears_validators_and_makes_feeds_due(
        self, ctx, client,
    ):
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)
        assert len(feeds) == 2

        resp = self._put(client, {"max_entries_per_feed": 100}, feeds=feeds)
        assert resp.status_code == 200
        assert self._validator_state(ctx) == [(None, None, None)] * 2

    def test_a_throttled_feed_keeps_its_standoff_through_a_maximum_change(
        self, ctx, client,
    ):
        # ISSUE-347's invariant is that a 429 never schedules sooner than a
        # success would. A settings save is user-triggered and repeatable, so
        # clearing `next_poll_at` here would hand the user a way to stampede a
        # host that has just turned us away, one save at a time. The validators
        # still go, because they only decide what the next request asks for.
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            first = feeds_db.list_feeds(conn)[0]
            conn.execute(
                "UPDATE feeds SET last_throttled_at = ? WHERE id = ?",
                ("2026-09-01T00:00:00+00:00", first.id),
            )
            conn.commit()

        resp = self._put(client, {"max_entries_per_feed": 100}, feeds=feeds)
        assert resp.status_code == 200

        with feeds_db.connect(ctx.db_path) as conn:
            by_url = {f.url: f for f in feeds_db.list_feeds(conn)}
        throttled = by_url[first.url]
        healthy = [f for u, f in by_url.items() if u != first.url][0]
        # The standoff survives; the validators do not.
        assert throttled.next_poll_at == self.STALE[2]
        assert (throttled.etag, throttled.last_modified) == (None, None)
        # Its neighbour is still made due, so the narrowing is targeted rather
        # than a blanket refusal to reset.
        assert healthy.next_poll_at is None

    def test_an_erroring_feed_keeps_its_backoff_through_a_maximum_change(
        self, ctx, client,
    ):
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            first = feeds_db.list_feeds(conn)[0]
            conn.execute(
                "UPDATE feeds SET error_count = 3 WHERE id = ?", (first.id,),
            )
            conn.commit()

        resp = self._put(client, {"max_entries_per_feed": 100}, feeds=feeds)
        assert resp.status_code == 200

        with feeds_db.connect(ctx.db_path) as conn:
            by_url = {f.url: f for f in feeds_db.list_feeds(conn)}
        assert by_url[first.url].next_poll_at == self.STALE[2]

    def test_an_unchanged_effective_maximum_leaves_the_validators_alone(
        self, ctx, client,
    ):
        # Sending the default explicitly resolves to the same effective
        # maximum, so nothing has to be refetched. Without the comparison
        # every save would force a full body from every feed.
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        resp = self._put(client, {"max_entries_per_feed": 5000}, feeds=feeds)
        assert resp.status_code == 200
        assert self._validator_state(ctx) == [self.STALE] * 2

    def test_an_age_only_change_leaves_the_validators_alone(self, ctx, client):
        # The age window deletes on a stored clock and admits nothing, so a
        # refetch would buy it nothing.
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        resp = self._put(client, {"entry_retention_days": 30}, feeds=feeds)
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) == 30
        assert self._validator_state(ctx) == [self.STALE] * 2

    def test_clearing_a_non_default_maximum_back_to_the_default_refetches(
        self, ctx, client,
    ):
        # Clearing a lowered maximum raises the effective one, so the next
        # poll has to fetch a full body to fill the restored budget. The
        # comparison is between *resolved* values for this reason: a raw
        # stored-value comparison would also fire on a save that merely wrote
        # the default down explicitly.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 100)
            conn.commit()
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        assert self._put(client, {}, feeds=feeds).status_code == 200
        assert self._validator_state(ctx) == [(None, None, None)] * 2

    def test_writing_the_default_down_explicitly_does_not_refetch(
        self, ctx, client,
    ):
        # The control for the test above: the stored row changes from absent
        # to `5000` while the effective maximum does not, so nothing is due.
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        assert self._put(
            client, {"max_entries_per_feed": 5000}, feeds=feeds,
        ).status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_max_entries_per_feed(conn) == 5000
        assert self._validator_state(ctx) == [self.STALE] * 2

    def test_turning_the_maximum_off_refetches(self, ctx, client):
        _seed(ctx)
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        assert self._put(
            client, {"max_entries_per_feed": 0}, feeds=feeds,
        ).status_code == 200
        assert self._validator_state(ctx) == [(None, None, None)] * 2

    @pytest.mark.parametrize("key", [
        "entry_retention_days", "max_entries_per_feed",
    ])
    def test_put_rejects_a_value_the_prune_could_not_express(
        self, ctx, client, key,
    ):
        # The ceiling is not taste. `now - timedelta(days=1_000_000)` raises
        # OverflowError, and a maximum at or above 2**63 cannot be bound as a
        # SQLite integer — either raises out of `prune_feeds` on every run, so
        # the daily prune job fails until it auto-disables and retention stops
        # for that user with nothing saying why. Stored once, it is unreachable
        # from the API that stored it.
        _seed(ctx)
        for bad in (routes.MAX_RETENTION_SETTING + 1, 10**6, 2**63, 10**9):
            resp = self._put(client, {key: bad})
            assert resp.status_code == 400, bad
            assert key in resp.json()["error"]

        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) is None
            assert feeds_db.get_max_entries_per_feed(conn) is None

    @pytest.mark.parametrize("key", [
        "entry_retention_days", "max_entries_per_feed",
    ])
    def test_put_accepts_the_ceiling_itself(self, ctx, client, key):
        # The control for the test above: the bound is inclusive, so it rejects
        # what the prune cannot express and nothing else.
        _seed(ctx)
        resp = self._put(client, {key: routes.MAX_RETENTION_SETTING})
        assert resp.status_code == 200

    def test_the_ceiling_is_a_value_the_prune_can_actually_run_on(self, ctx):
        # Ties the number to the failure it exists to prevent rather than to a
        # comment: at the ceiling both passes complete, one above the age
        # cutoff is still expressible but the point is that nothing between
        # here and the overflow is reachable through the API.
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=routes.MAX_RETENTION_SETTING,
        )
        assert cutoff.isoformat()

        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.prune_entries_to_feed_cap(
                conn, max_entries_per_feed=routes.MAX_RETENTION_SETTING,
            )

    @pytest.mark.parametrize("bad", [[], 0, "", False, "nope", 5])
    def test_put_rejects_a_non_object_settings_rather_than_wiping_them(
        self, ctx, client, bad,
    ):
        # A *falsy* non-dict used to collapse to `{}` before the isinstance
        # check could see it, skip validation whole, and then clear every
        # stored setting on a 200 — which turns an `entry_retention_days` of 0
        # ("never prune") into the 90-day default, i.e. deletion switched on by
        # a malformed request.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()

        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"settings": bad, "categories": [], "feeds": []}},
        )
        assert resp.status_code == 400
        assert "settings must be an object" in resp.json()["error"]
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_entry_retention_days(conn) == 0
            assert feeds_db.get_max_entries_per_feed(conn) == 0

    def test_put_still_treats_a_null_settings_as_absent(self, ctx, client):
        # `null` meant "no settings" before the check above went in and still
        # does — the check narrows falsy non-dicts, not the absent case.
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"settings": None, "categories": [], "feeds": []}},
        )
        assert resp.status_code == 200

    def test_a_raised_maximum_refetches_too(self, ctx, client):
        # The rule is "changed", not "raised" — but raising is the direction
        # the reset exists for, and it was the one with no test of its own.
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 100)
            conn.commit()
        self._stale_validators(ctx)
        feeds = self._feed_payload(ctx)

        assert self._put(
            client, {"max_entries_per_feed": 400}, feeds=feeds,
        ).status_code == 200
        assert self._validator_state(ctx) == [(None, None, None)] * 2


# ---------------------------------------------------------------------------
# The web add seam normalizes too (ISSUE-432)
# ---------------------------------------------------------------------------


class TestTheConfigEndpointNormalizesFeedUrls:
    """The settings save is one of the four places a feed URL is stored.

    Normalizing here only ever rewrites a provider identifier that could not
    have fetched, so no working feed's URL moves under the wholesale-replace
    delete sweep at the end of the same handler.
    """

    def _put(self, client, feeds):
        return client.put(
            "/istota/api/feeds/config",
            json={"config": {"settings": {}, "categories": [], "feeds": feeds}},
        )

    def test_a_stray_leading_slash_is_stored_canonically(self, ctx, client):
        resp = self._put(client, [{"url": "arena:/example-channel"}])
        assert resp.status_code == 200

        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == [
                "arena:example-channel",
            ]

    def test_an_identifier_with_nothing_left_is_a_400(self, ctx, client):
        """Not a skip: the save deletes anything the payload omits, so a
        dropped feed is a deleted feed."""
        _seed(ctx)
        resp = self._put(client, [{"url": "arena:/"}])
        assert resp.status_code == 400
        assert "unusable feed url" in resp.json()["error"]

        with feeds_db.connect(ctx.db_path) as conn:
            assert len(feeds_db.list_feeds(conn)) == 2

    def test_an_rss_url_survives_the_round_trip_unchanged(self, ctx, client):
        url = "https://example.com/deep/path/feed.xml?x=1"
        assert self._put(client, [{"url": url}]).status_code == 200

        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == [url]

    def test_saving_the_page_does_not_delete_a_row_stored_before_the_fix(
        self, ctx, client,
    ):
        """The one that costs data if it is wrong.

        The page renders each feed's *stored* URL and PUTs it back, so a
        pre-ISSUE-432 row arrives spelled the old way. Canonicalizing it here
        would upsert a second row and then sweep the original — entries, stars
        and read state with it, by cascade — on a 200, and change the feed id
        every bookmark and `--id` refers to.
        """
        with feeds_db.connect(ctx.db_path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="arena:/legacy-channel", title="Legacy", site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="block-1", title="Block One",
                    url=None, author=None, content_html="<p>x</p>",
                    content_text="x", image_urls=[],
                    published_at="2026-09-01T00:00:00+00:00",
                    fetched_at="2026-09-01T00:00:00+00:00", status="read",
                ),
            ])
            conn.commit()

        # Exactly what the page sends back: the stored spelling.
        payload = client.get("/istota/api/feeds/config").json()["config"]["feeds"]
        assert [f["url"] for f in payload] == ["arena:/legacy-channel"]

        resp = self._put(client, [{"url": f["url"]} for f in payload])
        assert resp.status_code == 200
        assert resp.json()["sync"]["feeds_removed"] == 0

        with feeds_db.connect(ctx.db_path) as conn:
            feeds = feeds_db.list_feeds(conn)
            entries = feeds_db.list_entries(conn)
        assert [(f.id, f.url) for f in feeds] == [(feed_id, "arena:/legacy-channel")]
        assert [e.guid for e in entries] == ["block-1"]

    def test_a_non_string_url_is_not_a_500(self, ctx, client):
        """`_validate_feeds_config` coerces with `str()`, so a numeric url
        reaches the apply loop; it used to be coerced there too."""
        resp = self._put(client, [{"url": 12345}])
        assert resp.status_code == 200

        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == ["12345"]

    def test_a_feed_whose_url_normalizes_is_not_deleted_and_recreated_twice(
        self, ctx, client,
    ):
        """Saving the same page twice is idempotent once the row is canonical."""
        assert self._put(client, [{"url": "arena:/example-channel"}]).status_code == 200
        resp = self._put(client, [{"url": "arena:example-channel"}])
        assert resp.status_code == 200
        assert resp.json()["sync"]["feeds_removed"] == 0

        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == [
                "arena:example-channel",
            ]
