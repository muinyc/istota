"""Feed URL / provider-identifier normalization (ISSUE-432).

A provider identifier used to be stored exactly as typed and interpolated
straight into an API path, so ``arena:/example-channel`` — one stray leading
slash — built ``https://api.are.na/v3/channels//example-channel/contents`` and
404'd on every poll, for a channel that was alive. The failure looked identical
to a dead channel and there was no way to tell the two apart.

Two enforcement points, and neither substitutes for the other: the four add
seams normalize so nothing malformed is stored, and ``provider_identifier``
normalizes again so a row stored before this change fetches without a
migration.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from istota.feeds import db as feeds_db
from istota.feeds.cli import cli
from istota.feeds.models import (
    FeedRecord,
    detect_source_type,
    normalize_feed_url,
    provider_identifier,
)
from istota.feeds.poller import poll_feed
from istota.feeds.providers import arena as arena_provider
from istota.feeds.providers import tumblr as tumblr_provider
from istota.feeds.workspace import synthesize_feeds_context


@pytest.fixture
def ctx(tmp_path):
    fctx = synthesize_feeds_context("alice", tmp_path)
    fctx.ensure_dirs()
    feeds_db.init_db(fctx.db_path)
    return fctx


def _invoke(ctx, args):
    runner = CliRunner()
    return runner.invoke(cli, args, obj=ctx, standalone_mode=False, catch_exceptions=False)


class TestNormalizeFeedUrl:
    def test_a_leading_slash_is_stripped_from_an_arena_identifier(self):
        assert normalize_feed_url("arena:/example-channel") == "arena:example-channel"

    def test_a_trailing_slash_is_stripped(self):
        assert normalize_feed_url("arena:example-channel/") == "arena:example-channel"

    def test_surrounding_whitespace_goes(self):
        assert normalize_feed_url("  arena: example-channel  ") == "arena:example-channel"

    def test_a_pasted_channel_url_yields_the_slug(self):
        pasted = "arena:https://www.are.na/example-user/example-channel"
        assert normalize_feed_url(pasted) == "arena:example-channel"

    def test_a_pasted_channel_url_with_a_query_yields_the_slug(self):
        pasted = "arena:https://www.are.na/example-user/example-channel?view=grid"
        assert normalize_feed_url(pasted) == "arena:example-channel"

    def test_a_tumblr_identifier_is_stripped(self):
        assert normalize_feed_url("tumblr:/example-blog") == "tumblr:example-blog"

    def test_a_pasted_tumblr_url_yields_the_blog_host(self):
        pasted = "tumblr:https://example-blog.tumblr.com/"
        assert normalize_feed_url(pasted) == "tumblr:example-blog.tumblr.com"

    def test_the_scheme_is_lower_cased(self):
        """`feeds.url` is UNIQUE with a binary `=`, so `Arena:x` and `arena:x`
        would be two rows polling one channel."""
        assert normalize_feed_url("Arena:/example-channel") == "arena:example-channel"
        assert normalize_feed_url("TUMBLR:Example-Blog") == "tumblr:Example-Blog"

    @pytest.mark.parametrize(
        "raw",
        [
            "", "   ", "arena:", "arena:/", "arena:  ", "arena:///", "tumblr:/",
            # The segment rule can construct these itself out of `arena:a/..`,
            # so the normalizer has to refuse what it builds.
            "arena:..", "arena:.", "arena:a/..", "arena:foo/../..",
            # A Tumblr identifier is a blog name or a host; a slash makes it a
            # path, and the path it walks to carries the API key.
            "tumblr:a/b", "tumblr:../../v2/user/info",
        ],
    )
    def test_an_identifier_with_nothing_left_is_rejected(self, raw):
        assert normalize_feed_url(raw) == ""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("arena:example-channel?x=1", "arena:example-channel"),
            ("arena:example-channel#frag", "arena:example-channel"),
            ("tumblr:example-blog?x=1", "tumblr:example-blog"),
        ],
    )
    def test_a_query_or_fragment_marker_is_cut(self, raw, expected):
        """Not cosmetic. `httpx` replaces the query from `params=`, so a `?`
        in the slug retargets the poll and answers 200 with the wrong body."""
        assert normalize_feed_url(raw) == expected

    @pytest.mark.parametrize("raw", [123, True, ["a"], 0, None, 1.5])
    def test_a_non_string_does_not_raise(self, raw):
        """The web settings payload and a legacy TOML are both untyped, and
        `_validate_feeds_config` coerces with `str()` before this sees it."""
        assert isinstance(normalize_feed_url(raw), str)

    def test_a_well_formed_identifier_is_untouched(self):
        assert normalize_feed_url("arena:example-channel") == "arena:example-channel"
        assert normalize_feed_url("tumblr:example-blog") == "tumblr:example-blog"

    def test_an_rss_url_is_only_stripped(self):
        assert normalize_feed_url("https://example.com/feed.xml") == "https://example.com/feed.xml"
        assert normalize_feed_url("  https://example.com/feed.xml\n") == "https://example.com/feed.xml"

    def test_an_rss_url_keeps_its_path_slashes(self):
        """The slash rule is about a provider identifier, never a whole URL."""
        assert normalize_feed_url("https://example.com/feed/") == "https://example.com/feed/"

    def test_none_is_not_a_crash(self):
        assert normalize_feed_url(None) == ""

    def test_a_non_string_coerces_the_way_the_old_seams_did(self):
        assert normalize_feed_url(123) == "123"
        assert normalize_feed_url(0) == ""


class TestProviderIdentifier:
    """The second enforcement point: a row stored malformed still fetches."""

    def test_a_stored_leading_slash_does_not_reach_the_api_path(self):
        assert provider_identifier("arena:/example-channel") == "example-channel"

    def test_a_stored_pasted_url_yields_the_slug(self):
        assert provider_identifier(
            "arena:https://www.are.na/example-user/example-channel"
        ) == "example-channel"

    def test_a_stored_tumblr_identifier_is_stripped(self):
        assert provider_identifier("tumblr:/example-blog") == "example-blog"

    def test_a_well_formed_identifier_is_untouched(self):
        assert provider_identifier("arena:example-channel") == "example-channel"

    def test_a_non_provider_url_is_returned_as_is(self):
        assert provider_identifier("https://example.com/feed.xml") == "https://example.com/feed.xml"


class TestDetectSourceType:
    def test_leading_whitespace_does_not_hide_the_scheme(self):
        assert detect_source_type("  arena:example-channel") == "arena"
        assert detect_source_type("  tumblr:example-blog") == "tumblr"

    def test_an_rss_url_is_still_rss(self):
        assert detect_source_type("https://example.com/feed.xml") == "rss"


class TestThePollReachesTheRealChannel:
    """The reported symptom, through the real dispatch: no double slash."""

    def _record(self, url: str) -> FeedRecord:
        return FeedRecord(
            id=1, url=url, title=None, site_url=None, category_id=None,
            source_type="arena", etag=None, last_modified=None,
            last_fetched_at=None, last_error=None, error_count=0,
            poll_interval_minutes=60, next_poll_at=None,
        )

    def test_a_stored_leading_slash_builds_a_single_slash_path(self, monkeypatch):
        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [], "meta": {}}

        def _get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            return _Resp()

        monkeypatch.setattr(arena_provider.httpx, "get", _get)

        result = poll_feed(self._record("arena:/example-channel"))

        assert result.error is None
        assert captured["url"] == "https://api.are.na/v3/channels/example-channel/contents"
        assert "//contents" not in captured["url"]
        assert "channels//" not in captured["url"]


class TestTheIdentifierCannotSteerTheRequest:
    """The identifier reaches an external API path, so quoting is the boundary.

    Normalization is a canonicalizer and is deliberately not trusted as the
    guard: these assert on the URL the provider actually builds, from an
    identifier handed straight to `fetch`, so they hold whatever a future
    normalizer lets through.
    """

    def _arena_url(self, monkeypatch, identifier: str) -> str:
        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [], "meta": {}}

        def _get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        monkeypatch.setattr(arena_provider.httpx, "get", _get)
        arena_provider.fetch(identifier)
        return captured["url"]

    @pytest.mark.parametrize("hostile", ["..", "a/../b", "foo?x=1", "foo#f", "a b"])
    def test_the_slug_cannot_leave_the_channels_path(self, monkeypatch, hostile):
        url = self._arena_url(monkeypatch, hostile)
        assert url.startswith("https://api.are.na/v3/channels/")
        assert url.endswith("/contents")
        # One path segment between the two, whatever was handed in.
        middle = url[len("https://api.are.na/v3/channels/"):-len("/contents")]
        assert "/" not in middle
        assert "?" not in middle
        assert "#" not in middle

    def test_a_real_slug_is_not_mangled(self, monkeypatch):
        assert self._arena_url(monkeypatch, "example-channel") == (
            "https://api.are.na/v3/channels/example-channel/contents"
        )

    def test_an_empty_identifier_says_what_is_wrong(self, monkeypatch):
        """It used to build `channels//contents` and 404, which reads as a
        deleted channel — the confusion the issue is about."""
        with pytest.raises(ValueError, match="identifier"):
            arena_provider.fetch("")

    def test_a_tumblr_blog_cannot_walk_to_another_endpoint(self, monkeypatch):
        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"posts": []}}

        def _get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        monkeypatch.setattr(tumblr_provider.requests, "get", _get)
        tumblr_provider.fetch("../../v2/user/info", api_key="test-key")

        # The API key rides in the query, so a traversal here would carry the
        # credential to another endpoint.
        assert captured["url"].startswith("https://api.tumblr.com/v2/blog/")
        assert captured["url"].endswith("/posts")
        assert "/v2/user/info" not in captured["url"]

    def test_a_real_blog_name_is_not_mangled(self, monkeypatch):
        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"posts": []}}

        monkeypatch.setattr(
            tumblr_provider.requests, "get",
            lambda url, params=None, timeout=None: (
                captured.__setitem__("url", url), _Resp(),
            )[1],
        )
        tumblr_provider.fetch("example-blog.tumblr.com", api_key="test-key")
        assert captured["url"] == (
            "https://api.tumblr.com/v2/blog/example-blog.tumblr.com/posts"
        )

    def test_an_empty_tumblr_identifier_says_what_is_wrong(self):
        with pytest.raises(ValueError, match="identifier"):
            tumblr_provider.fetch("", api_key="test-key")


class TestALegacyRowIsNotDuplicated:
    """A row stored before the fix keeps its spelling, and every seam that
    writes a feed has to find it rather than storing a canonical twin — both
    of which now fetch the same channel."""

    def _seed_legacy(self, ctx, url="arena:/example-channel"):
        with feeds_db.connect(ctx.db_path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url=url, title="Legacy", site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.commit()
        return feed_id

    def test_add_finds_the_legacy_row_instead_of_inserting_a_twin(self, ctx):
        self._seed_legacy(ctx)
        result = _invoke(ctx, ["add", "--url", "arena:example-channel"])
        assert json.loads(result.output)["status"] == "error"
        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == [
                "arena:/example-channel",
            ]

    def test_remove_works_in_the_canonical_direction_too(self, ctx):
        self._seed_legacy(ctx)
        result = _invoke(ctx, ["remove", "--url", "arena:example-channel"])
        assert json.loads(result.output)["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.list_feeds(conn) == []

    def test_opml_reimport_updates_the_legacy_row(self, ctx):
        from istota.feeds.opml import import_opml

        self._seed_legacy(ctx)
        doc = """<?xml version="1.0"?>
        <opml version="2.0"><body>
          <outline text="Group" title="Group">
            <outline type="rss" text="C" xmlUrl="arena:example-channel"/>
          </outline>
        </body></opml>"""
        result = import_opml(ctx, doc)

        assert result.feeds_added == 0
        assert result.feeds_updated == 1
        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == [
                "arena:/example-channel",
            ]

    def test_a_mixed_case_scheme_row_is_found_too(self, ctx):
        self._seed_legacy(ctx, url="Arena:example-channel")
        result = _invoke(ctx, ["add", "--url", "arena:example-channel"])
        assert json.loads(result.output)["status"] == "error"
        with feeds_db.connect(ctx.db_path) as conn:
            assert len(feeds_db.list_feeds(conn)) == 1

    def test_a_canonical_row_already_present_is_not_shadowed(self, ctx):
        """Both spellings on file: the variant map must not point the
        canonical string at the legacy row, or the other would be swept."""
        self._seed_legacy(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_feed(
                conn, url="arena:example-channel", title=None, site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.commit()
            assert feeds_db.stored_url_variants(conn) == {}


class TestTheAddSeamStoresCanonically:
    def test_cli_add_normalizes_before_storing(self, ctx):
        result = _invoke(ctx, ["add", "--url", "arena:/example-channel"])
        payload = json.loads(result.output)
        assert payload["status"] == "ok"

        with feeds_db.connect(ctx.db_path) as conn:
            urls = [f.url for f in feeds_db.list_feeds(conn)]
        assert urls == ["arena:example-channel"]

    def test_cli_add_refuses_an_identifier_with_nothing_left(self, ctx):
        result = _invoke(ctx, ["add", "--url", "arena:/"])
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "url" in payload["error"].lower()

        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.list_feeds(conn) == []

    def test_cli_add_detects_the_source_type_after_normalizing(self, ctx):
        _invoke(ctx, ["add", "--url", "  arena:/example-channel  "])
        with feeds_db.connect(ctx.db_path) as conn:
            feeds = feeds_db.list_feeds(conn)
        assert [(f.url, f.source_type) for f in feeds] == [
            ("arena:example-channel", "arena"),
        ]

    def test_adding_both_spellings_is_one_feed(self, ctx):
        _invoke(ctx, ["add", "--url", "arena:example-channel"])
        result = _invoke(ctx, ["add", "--url", "arena:/example-channel"])
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        with feeds_db.connect(ctx.db_path) as conn:
            assert len(feeds_db.list_feeds(conn)) == 1

    def test_remove_still_accepts_the_spelling_that_was_typed(self, ctx):
        """Normalizing on add must not make the same string unremovable."""
        _invoke(ctx, ["add", "--url", "arena:/example-channel"])
        result = _invoke(ctx, ["remove", "--url", "arena:/example-channel"])
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.list_feeds(conn) == []

    def test_remove_still_finds_a_row_stored_before_the_fix(self, ctx):
        """A legacy malformed row is removable by the string it is stored as."""
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_feed(
                conn, url="arena:/example-channel", title=None, site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.commit()
        result = _invoke(ctx, ["remove", "--url", "arena:/example-channel"])
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.list_feeds(conn) == []


class TestTheOpmlSeam:
    def test_import_normalizes_a_provider_identifier(self, ctx):
        from istota.feeds.opml import import_opml

        doc = """<?xml version="1.0"?>
        <opml version="2.0"><body>
          <outline type="rss" text="C" xmlUrl="arena:/example-channel"/>
        </body></opml>"""
        result = import_opml(ctx, doc)

        assert result.feeds_added == 1
        with feeds_db.connect(ctx.db_path) as conn:
            assert [f.url for f in feeds_db.list_feeds(conn)] == ["arena:example-channel"]

    def test_import_skips_an_identifier_with_nothing_left(self, ctx):
        from istota.feeds.opml import import_opml

        doc = """<?xml version="1.0"?>
        <opml version="2.0"><body>
          <outline type="rss" text="C" xmlUrl="arena:/"/>
        </body></opml>"""
        result = import_opml(ctx, doc)

        assert result.feeds_added == 0
        assert result.feeds_skipped == 1
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.list_feeds(conn) == []
