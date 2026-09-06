"""Tests for the browse skill CLI client."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from istota.skills.browse import (
    _links_from_extract,
    build_parser,
    cmd_close,
    cmd_extract,
    cmd_get,
    cmd_interact,
    cmd_links,
    cmd_render,
    cmd_screenshot,
    get_api_url,
    main,
)

#: A body that `image_sniff.sniff_raster` admits. `screenshot` refuses to write
#: anything else now, so a `b"fake image data"` stand-in is no longer a
#: screenshot as far as the verb is concerned.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake image data"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A mount with the calling user's workspace, as the executor builds it.

    `screenshot` writes through `skill_host_paths`, whose roots come out of the
    environment, so a test that captures anything has to say who is calling and
    where their workspace is. Returns `{mount}/Users/alice`.
    """
    mount = tmp_path / "mount"
    own = mount / "Users" / "alice"
    own.mkdir(parents=True)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    return own


class TestGetApiUrl:
    def test_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_api_url() == "http://localhost:9223"

    def test_from_env(self):
        with patch.dict("os.environ", {"BROWSER_API_URL": "http://custom:1234"}):
            assert get_api_url() == "http://custom:1234"


class TestBuildParser:
    def test_get_command(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        assert args.command == "get"
        assert args.url == "https://example.com"
        assert args.keep_session is False
        assert args.timeout == 30

    def test_get_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "get", "https://example.com",
            "--keep-session", "--timeout", "60", "--wait-for", "article",
        ])
        assert args.keep_session is True
        assert args.timeout == 60
        assert args.wait_for == "article"
        assert args.skip_behavior is False

    def test_get_with_skip_behavior(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--skip-behavior"])
        assert args.skip_behavior is True

    def test_get_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--session", "abc123"])
        assert args.session == "abc123"

    def test_screenshot_with_url(self):
        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com", "-o", "/tmp/out.png"])
        assert args.command == "screenshot"
        assert args.url == "https://example.com"
        assert args.output == "/tmp/out.png"

    def test_screenshot_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["screenshot", "--session", "abc123"])
        assert args.session == "abc123"
        assert args.url is None

    def test_extract_command(self):
        parser = build_parser()
        args = parser.parse_args(["extract", "https://example.com", "-s", "article"])
        assert args.command == "extract"
        assert args.selector == "article"
        assert args.max_chars is None
        assert args.limit is None

    def test_extract_with_budgets(self):
        parser = build_parser()
        args = parser.parse_args([
            "extract", "https://example.com", "-s", "article",
            "--max-chars", "80000", "--limit", "50",
        ])
        assert args.max_chars == 80000
        assert args.limit == 50

    def test_render_command(self):
        parser = build_parser()
        args = parser.parse_args(["render", "https://example.com"])
        assert args.command == "render"
        assert args.url == "https://example.com"
        assert args.mode == "full"
        assert args.keep_session is False
        assert args.timeout == 30
        assert args.max_chars is None

    def test_render_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "render", "https://example.com", "--mode", "article",
            "--keep-session", "--max-chars", "250000", "--wait-for", "main",
        ])
        assert args.mode == "article"
        assert args.keep_session is True
        assert args.max_chars == 250000
        assert args.wait_for == "main"

    def test_render_session_only(self):
        parser = build_parser()
        args = parser.parse_args(["render", "--session", "abc123"])
        assert args.url is None
        assert args.session == "abc123"

    def test_render_rejects_unknown_mode(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["render", "https://example.com", "--mode", "readable"])

    def test_interact_click(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--click", ".btn", "--click", "#submit"])
        assert args.command == "interact"
        assert args.session_id == "sess1"
        assert args.click == [".btn", "#submit"]

    def test_interact_fill(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--fill", "#name=Alice"])
        assert args.fill == ["#name=Alice"]

    def test_interact_scroll(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--scroll", "down", "--scroll-amount", "1000"])
        assert args.scroll == "down"
        assert args.scroll_amount == 1000

    def test_close_command(self):
        parser = build_parser()
        args = parser.parse_args(["close", "sess1"])
        assert args.command == "close"
        assert args.session_id == "sess1"

    def test_links_command(self):
        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        assert args.command == "links"
        assert args.url == "https://example.com"
        assert args.selector is None
        assert args.session is None
        assert args.timeout == 30

    def test_links_with_selector(self):
        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com", "-s", "nav a"])
        assert args.selector == "nav a"

    def test_links_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["links", "--session", "abc123", "-s", ".links"])
        assert args.session == "abc123"
        assert args.selector == ".links"
        assert args.url is None


class TestCmdGet:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_basic_get(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Example",
            "text": "Hello world",
            "url": "https://example.com",
            "links": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        result = cmd_get(args)

        assert result["status"] == "ok"
        assert result["title"] == "Example"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["url"] == "https://example.com"
        assert call_args[1]["json"]["keep_session"] is False

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_with_session(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "abc123"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--session", "abc123"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "abc123"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_with_skip_behavior(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--skip-behavior"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["skip_behavior"] is True

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_without_skip_behavior(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert "skip_behavior" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_captcha_response(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "captcha",
            "session_id": "xyz789",
            "vnc_url": "https://vnc.example.com",
            "message": "Captcha detected.",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://protected.com", "--keep-session"])
        result = cmd_get(args)

        assert result["status"] == "captcha"
        assert result["session_id"] == "xyz789"
        assert result["vnc_url"] == "https://vnc.example.com"


class TestCmdRender:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_returns_markdown(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com/world",
            "title": "World",
            "mode": "full",
            "requested_mode": "full",
            "markdown": "## Top stories\n\n* [Headline](https://news.example.com/a)",
            "chars": 54,
            "truncated": False,
            "notes": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://news.example.com/world"])
        result = cmd_render(args)

        assert result["status"] == "ok"
        assert "[Headline](https://news.example.com/a)" in result["markdown"]
        assert mock_post.call_args[0][0] == "http://test:9223/render"
        payload = mock_post.call_args[1]["json"]
        assert payload["url"] == "https://news.example.com/world"
        assert payload["mode"] == "full"
        assert payload["keep_session"] is False

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_article_mode_payload(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "body", "mode": "article"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args([
            "render", "https://news.example.com/story", "--mode", "article",
            "--max-chars", "250000", "--wait-for", "main", "--skip-behavior",
        ])
        cmd_render(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["mode"] == "article"
        assert payload["max_chars"] == 250000
        assert payload["wait_for"] == "main"
        assert payload["skip_behavior"] is True

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_within_a_session(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "x", "session_id": "sess1"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "--session", "sess1"])
        cmd_render(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert "url" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_without_url_or_session_errors_locally(self, mock_url, mock_post):
        parser = build_parser()
        args = parser.parse_args(["render"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "URL or --session" in result["error"]
        mock_post.assert_not_called()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_on_old_container_says_so(self, mock_url, mock_post):
        # Flask's route-miss 404 is HTML, so .json() raises.
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.side_effect = ValueError("not json")
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://example.com"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "browse get" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_expired_session_404_is_not_reported_as_missing_endpoint(
        self, mock_url, mock_post,
    ):
        # The endpoint's own 404 carries a JSON error body. Reporting it as
        # "no render endpoint" would send the agent back to `browse get`.
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"error": "session sess1 not found or expired"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "--session", "sess1"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "not found or expired" in result["error"]
        assert "browse get" not in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_captcha_passthrough(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "captcha",
            "session_id": "sess1",
            "vnc_url": "https://vnc.example.com",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://protected.example"])
        result = cmd_render(args)

        assert result["status"] == "captcha"
        assert result["vnc_url"] == "https://vnc.example.com"


class TestCmdScreenshot:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_screenshot_saves_file(self, mock_url, mock_post, workspace):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/png"}
        mock_resp.content = PNG_BYTES
        mock_post.return_value = mock_resp

        output = str(workspace / "shot.png")
        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com", "-o", output])
        result = cmd_screenshot(args)

        assert result["status"] == "ok"
        assert result["path"] == output
        assert (workspace / "shot.png").read_bytes() == PNG_BYTES

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_screenshot_error(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"status": "error", "error": "timeout"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com"])
        result = cmd_screenshot(args)

        assert result["status"] == "error"


class TestCmdExtract:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_extract(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://example.com",
            "selector": "article",
            "count": 1,
            "elements": [{"text": "Article content", "html": "<p>Article content</p>"}],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["extract", "https://example.com", "-s", "article"])
        result = cmd_extract(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        payload = mock_post.call_args[1]["json"]
        assert payload["selector"] == "article"
        assert "max_chars" not in payload
        assert "limit" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_extract_budgets_forwarded(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "count": 0, "elements": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args([
            "extract", "https://example.com", "-s", "article",
            "--max-chars", "80000", "--limit", "50",
        ])
        cmd_extract(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["max_chars"] == 80000
        assert payload["limit"] == 50


class TestCmdInteract:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_click_actions(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "session_id": "sess1",
            "actions": [{"action": "click", "ok": True}],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--click", ".btn"])
        result = cmd_interact(args)

        assert result["status"] == "ok"
        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert payload["actions"] == [{"type": "click", "selector": ".btn"}]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_fill_actions(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "sess1", "actions": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--fill", "#email=test@example.com"])
        cmd_interact(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [{"type": "fill", "selector": "#email", "value": "test@example.com"}]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_scroll_action(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "sess1", "actions": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--scroll", "down", "--scroll-amount", "1000"])
        cmd_interact(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [{"type": "scroll", "direction": "down", "amount": 1000}]


class TestCmdClose:
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_close(self, mock_url, mock_delete):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "closed", "session_id": "sess1"}
        mock_delete.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["close", "sess1"])
        result = cmd_close(args)

        assert result["status"] == "closed"
        mock_delete.assert_called_once_with(
            "http://test:9223/sessions/sess1", timeout=30.0
        )


class TestMain:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_main_outputs_json(self, mock_url, mock_post, capsys):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "title": "Test"}
        mock_post.return_value = mock_resp

        main(["get", "https://example.com"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_main_connection_error(self, mock_url, mock_post, capsys):
        import httpx
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(SystemExit) as exc_info:
            main(["get", "https://example.com"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"
        assert "Cannot connect" in output["error"]


class TestCmdLinks:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_basic(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Hub Page",
            "url": "https://news.example.com",
            "text": "Lots of text...",
            "links": [
                {"text": "Article One", "href": "/article/one"},
                {"text": "Article Two", "href": "/article/two"},
            ],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["url"] == "https://news.example.com"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "Article One", "href": "/article/one"},
            {"text": "Article Two", "href": "/article/two"},
        ]
        # Should not contain text field
        assert "text" not in result or result.get("text") is None
        assert "title" not in result

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_selector_href_attr(self, mock_url, mock_post, mock_delete):
        """When extract returns href on elements directly (Guardian-style)."""
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "session_id": "sess1",
            "text": "...",
            "links": [],
        }
        extract_resp = MagicMock()
        extract_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": "a[data-link-name='article']",
            "count": 2,
            "elements": [
                {
                    "text": "Russia can keep fighting",
                    "html": '<span class="dcr-n509ks">Russia can keep fighting</span>',
                    "href": "/world/2026/feb/24/russia-fighting",
                },
                {
                    "text": "Louvre president resigns",
                    "html": '<span>Louvre president resigns</span>',
                    "href": "/world/2026/feb/24/louvre-president",
                },
            ],
        }
        mock_post.side_effect = [browse_resp, extract_resp]
        mock_delete_resp = MagicMock()
        mock_delete_resp.json.return_value = {"status": "closed"}
        mock_delete.return_value = mock_delete_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com", "-s", "a[data-link-name='article']"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "Russia can keep fighting", "href": "/world/2026/feb/24/russia-fighting"},
            {"text": "Louvre president resigns", "href": "/world/2026/feb/24/louvre-president"},
        ]
        mock_delete.assert_called_once()

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_selector_nested_anchors(self, mock_url, mock_post, mock_delete):
        """When extract returns elements containing nested <a> tags (fallback)."""
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "session_id": "sess1",
            "text": "...",
            "links": [],
        }
        extract_resp = MagicMock()
        extract_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": "nav",
            "count": 1,
            "elements": [
                {
                    "text": "World News Sports",
                    "html": '<a href="/world" class="nav-link">World News</a> <a href="/sports"><span>Sports</span></a>',
                },
            ],
        }
        mock_post.side_effect = [browse_resp, extract_resp]
        mock_delete_resp = MagicMock()
        mock_delete_resp.json.return_value = {"status": "closed"}
        mock_delete.return_value = mock_delete_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com", "-s", "nav"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "World News", "href": "/world"},
            {"text": "Sports", "href": "/sports"},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_session_and_selector(self, mock_url, mock_post):
        """Session + selector uses extract with href attribute."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": ".headlines a",
            "count": 1,
            "elements": [
                {
                    "text": "Breaking News",
                    "html": '<span>Breaking News</span>',
                    "href": "/breaking/123",
                },
            ],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "--session", "sess1", "-s", ".headlines a"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["links"] == [{"text": "Breaking News", "href": "/breaking/123"}]
        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert payload["selector"] == ".headlines a"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_empty(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Empty Page",
            "url": "https://example.com",
            "text": "No links here",
            "links": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["links"] == []

    def test_links_from_extract_prefers_href_attr(self):
        """_links_from_extract uses href attr when present."""
        data = {
            "elements": [
                {"text": "Article A", "html": "<span>Article A</span>", "href": "/a"},
                {"text": "Article B", "html": "<span>Article B</span>", "href": "/b"},
            ]
        }
        links = _links_from_extract(data)
        assert links == [
            {"text": "Article A", "href": "/a"},
            {"text": "Article B", "href": "/b"},
        ]

    def test_links_from_extract_falls_back_to_inner_html(self):
        """_links_from_extract parses <a> tags when no href attr."""
        data = {
            "elements": [
                {
                    "text": "Nav section",
                    "html": '<a href="/x">Link X</a> and <a href="/y"><b>Link Y</b></a>',
                },
            ]
        }
        links = _links_from_extract(data)
        assert links == [
            {"text": "Link X", "href": "/x"},
            {"text": "Link Y", "href": "/y"},
        ]

    def test_links_from_extract_mixed(self):
        """Mix of elements with and without href attr."""
        data = {
            "elements": [
                {"text": "Direct link", "html": "<span>Direct</span>", "href": "/direct"},
                {"text": "Container", "html": '<a href="/nested">Nested</a>'},
            ]
        }
        links = _links_from_extract(data)
        assert len(links) == 2
        assert links[0] == {"text": "Direct link", "href": "/direct"}
        assert links[1] == {"text": "Nested", "href": "/nested"}

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_error_passthrough(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": "timeout",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        result = cmd_links(args)

        assert result["status"] == "error"
        assert result["error"] == "timeout"


def _non_json_response(status_code, body, url="http://test:9223/browse"):
    """A response whose body is not JSON, the way httpx presents one.

    `resp.json()` raises `ValueError` (json.JSONDecodeError is a subclass) and
    `resp.text` still holds whatever came back.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8") if isinstance(body, str) else body
    resp.encoding = "utf-8"
    resp.url = url
    resp.headers = {"content-type": "text/html"}
    resp.json.side_effect = json.JSONDecodeError("Expecting value", body or "", 0)
    return resp


FLASK_500 = (
    "<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n"
    "<h1>Internal Server Error</h1>\n<p>The server encountered an internal error "
    "and was unable to complete your request.</p>\n"
)


def _run_verb(argv):
    """Run a verb through the same command table `main` uses."""
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "get": cmd_get,
        "render": cmd_render,
        "screenshot": cmd_screenshot,
        "extract": cmd_extract,
        "interact": cmd_interact,
        "links": cmd_links,
        "close": cmd_close,
    }
    return commands[args.command](args)


class TestNonJsonResponsesAreReported:
    """ISSUE-383: a body that will not decode must name the status and the body.

    Before this, every verb called `resp.json()` with no check, the
    `json.JSONDecodeError` reached `main`'s catch-all, and the whole report was
    the string "Expecting value: line 1 column 1 (char 0)" — which names no
    status code, no URL and no part of the body, so a 500 from the container, a
    502 in front of it and an empty response were indistinguishable.
    """

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_flask_html_500_names_the_status_and_the_body(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(500, FLASK_500)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "Internal Server Error" in result["error"]
        assert "Expecting value" not in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_request_url_is_named(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(
            502,
            "<html><body>502 Bad Gateway</body></html>",
            url="http://test:9223/browse",
        )

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "http://test:9223/browse" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_empty_body_says_so(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(502, "")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "502" in result["error"]
        assert "empty" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_excerpt_is_capped_and_says_it_was(self, mock_url, mock_post):
        from istota.skills.browse import MAX_BODY_EXCERPT

        mock_post.return_value = _non_json_response(500, "A" * 10000)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        # The marker is the point: a truncated body that drops it reads as a
        # complete one, and a length assertion alone cannot see that.
        excerpt = result["error"].split("not a JSON object: ", 1)[1]
        assert excerpt == "A" * MAX_BODY_EXCERPT + "…"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unreadable_body_is_not_called_empty(self, mock_url, mock_post):
        # "empty response" and "40 KB of something unreadable" are different
        # outages; reporting the second as the first is a false statement.
        resp = MagicMock()
        resp.status_code = 500
        resp.url = "http://test:9223/browse"
        resp.json.side_effect = ValueError("no")
        type(resp).content = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("stream consumed")),
        )
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "(empty)" not in result["error"]
        assert "unreadable" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_bogus_charset_still_produces_an_excerpt(self, mock_url, mock_post):
        resp = _non_json_response(500, "boom")
        resp.encoding = "utf-42"  # no such codec
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "boom" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_control_characters_do_not_survive_the_excerpt(self, mock_url, mock_post):
        # The body is whatever the container or an intermediary produced. An
        # ANSI escape reaching a terminal is why it is not passed through as-is.
        mock_post.return_value = _non_json_response(
            500, "boom \x1b[31mRED\x1b[0m \x00 done\r\nnext line",
        )

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "\x1b" not in result["error"]
        assert "\x00" not in result["error"]
        assert "\n" not in result["error"]
        assert "RED" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_bidi_and_zero_width_characters_do_not_survive(self, mock_url, mock_post):
        # U+202E reverses the display order of everything after it, so a body
        # could otherwise render as a different message inside a line the
        # model reads as this tool's own diagnostic voice. json.dumps with
        # ensure_ascii=False emits these raw, so stripping is the only guard.
        hostile = (
            "start \u202eesrever\u202c \u200bzero \ufeffbom "
            "\u2066iso\u2069 end"
        )
        mock_post.return_value = _non_json_response(500, hostile)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        for ch in "\u202e\u202c\u200b\ufeff\u2066\u2069":
            assert ch not in result["error"], hex(ord(ch))
        assert "esrever" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_line_separators_are_collapsed(self, mock_url, mock_post):
        # U+2028/U+2029 are handled by the whitespace collapse rather than by
        # the control class, which is why the class does not name them.
        mock_post.return_value = _non_json_response(500, "a\u2028b\u2029c")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "\u2028" not in result["error"]
        assert "\u2029" not in result["error"]
        assert "a b c" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_json_body_that_is_not_an_object_is_an_error_too(self, mock_url, mock_post):
        # Well-formed JSON, wrong shape. Every verb calls .get() on what comes
        # back, so a list reaching them is an AttributeError one frame later.
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '["unexpected"]'
        resp.content = b'["unexpected"]'
        resp.encoding = "utf-8"
        resp.url = "http://test:9223/browse"
        resp.json.return_value = ["unexpected"]
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "unexpected" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_non_json_503_carries_the_retry_hint(self, mock_url, mock_post):
        # The message the unreachable `except httpx.HTTPStatusError` branch used
        # to hold. It fires here now, where it can actually be reached.
        mock_post.return_value = _non_json_response(503, "<html>503 Service Unavailable</html>")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "503" in result["error"]
        assert "retry" in result["error"].lower()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_apis_own_json_error_body_still_passes_through(self, mock_url, mock_post):
        # The API reports its own failures as JSON with a non-2xx status, and
        # those bodies carry the diagnosis. A bare raise_for_status() would
        # throw them away, which is why the check is on the body, not the status.
        resp = MagicMock()
        resp.status_code = 503
        resp.json.return_value = {
            "status": "error",
            "error": "Chrome unavailable: CDP connect failed",
        }
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result == {
            "status": "error",
            "error": "Chrome unavailable: CDP connect failed",
        }

    @pytest.mark.parametrize(
        "argv",
        [
            ["get", "https://example.com"],
            ["render", "https://example.com"],
            ["extract", "https://example.com", "--selector", "article"],
            ["interact", "sess1", "--click", ".btn"],
            ["links", "https://example.com"],
            ["links", "https://example.com", "--selector", "nav a"],
            ["screenshot", "https://example.com"],
        ],
    )
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_every_verb_reports_a_non_json_body(
        self, mock_url, mock_post, mock_delete, argv, workspace,
    ):
        # `workspace` is only load-bearing for the screenshot row: with no
        # workspace resolvable that verb refuses before it ever posts, so
        # without the fixture the case would assert against the wrong refusal.
        # The blast radius: the entry named one verb, the decode call was in all
        # of them.
        mock_post.return_value = _non_json_response(500, FLASK_500)

        result = _run_verb(argv)

        assert result["status"] == "error", argv
        assert "500" in result["error"], argv
        assert "Expecting value" not in result["error"], argv

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_close_reports_a_non_json_body(self, mock_url, mock_delete):
        mock_delete.return_value = _non_json_response(
            500, FLASK_500, url="http://test:9223/sessions/sess1",
        )

        parser = build_parser()
        result = cmd_close(parser.parse_args(["close", "sess1"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "Expecting value" not in result["error"]


class TestMainExitStatus:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_error_result_exits_one(self, mock_url, mock_post, capsys):
        # Without this the fix would report a 500 on exit 0, where the unhandled
        # decode error at least exited 1.
        mock_post.return_value = _non_json_response(500, FLASK_500)

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "500" in output["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_api_reported_error_exits_one_too(self, mock_url, mock_post, capsys):
        resp = MagicMock()
        resp.json.return_value = {"status": "error", "error": "navigation timeout"}
        mock_post.return_value = resp

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        assert json.loads(capsys.readouterr().out)["error"] == "navigation timeout"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_ok_result_exits_zero(self, mock_url, mock_post, capsys):
        resp = MagicMock()
        resp.json.return_value = {"status": "ok", "title": "Test"}
        mock_post.return_value = resp

        main(["get", "https://example.com"])

        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_closed_session_is_not_an_error(self, mock_url, mock_delete, capsys):
        # Only "error" exits 1. A status naming any other outcome is an answer
        # rather than a failure, and a caller branching on it would read a
        # non-zero exit wrong. DELETE /sessions/<id> always answers "closed",
        # whether or not the session was there.
        resp = MagicMock()
        resp.json.return_value = {"status": "closed", "session_id": "sess1"}
        mock_delete.return_value = resp

        main(["close", "sess1"])

        assert json.loads(capsys.readouterr().out)["status"] == "closed"


class TestTheDeadHttpStatusErrorBranchIsGone:
    def test_nothing_raises_or_handles_an_http_status_error(self):
        # The `except httpx.HTTPStatusError` arm in `main` was unreachable
        # because `raise_for_status()` is called nowhere, so its 503 message had
        # never been printed. Dead code that reads as a working feature; the
        # message now lives on the reachable path in `_decode`.
        #
        # Read structurally rather than as text: the module's prose explains
        # why raise_for_status() is the wrong tool here, and a substring scan
        # would fail on the explanation.
        import ast
        import inspect

        import istota.skills.browse as browse

        tree = ast.parse(inspect.getsource(browse))

        handled = [
            ast.unparse(h.type)
            for h in ast.walk(tree)
            if isinstance(h, ast.ExceptHandler) and h.type is not None
        ]
        assert not any("HTTPStatusError" in name for name in handled), handled

        called = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "raise_for_status" not in called


class TestBareErrorBodiesAreClassified:
    """The API has two spellings for a reported failure; both must classify.

    `browse_api.py` says `{"status": "error", ...}` for its 500s and 503s, but
    its argument and lookup failures — nine paths, two of them 500s — say a
    bare `{"error": ...}` with no `status` key. Every caller here branches on
    `status`, so before ISSUE-383 those read as successes: `main` exited 0 and
    `cmd_links` treated one as a page. `cmd_render` rewrote the shape by hand
    for its own 404, so two verbs disagreed about one server response.
    """

    @staticmethod
    def _bare_error(status_code, message):
        resp = MagicMock()
        resp.status_code = status_code
        resp.url = "http://test:9223/browse"
        resp.json.return_value = {"error": message}
        return resp

    @pytest.mark.parametrize(
        ("argv", "code"),
        [
            (["get", "https://example.com"], 400),
            (["get", "https://example.com", "--session", "gone"], 404),
            (["render", "https://example.com"], 400),
            (["extract", "https://example.com", "--selector", "article"], 400),
            (["interact", "sess1", "--click", ".btn"], 404),
            (["links", "https://example.com"], 400),
            (["links", "https://example.com", "--selector", "nav a"], 400),
        ],
    )
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_every_verb_classifies_a_bare_error_body(
        self, mock_url, mock_post, mock_delete, argv, code,
    ):
        mock_post.return_value = self._bare_error(code, "url is required")

        result = _run_verb(argv)

        assert result["status"] == "error", argv
        assert result["error"] == "url is required", argv

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_and_render_agree_on_an_expired_session(self, mock_url, mock_post):
        # One condition, one server response, two verbs. These used to differ:
        # render rewrote the body and exited 1, get passed it through and
        # exited 0.
        body = "session sess1 not found or expired"
        mock_post.return_value = self._bare_error(404, body)

        parser = build_parser()
        got = cmd_get(parser.parse_args(["get", "https://example.com", "--session", "sess1"]))
        rendered = cmd_render(parser.parse_args(["render", "--session", "sess1"]))

        assert got["status"] == rendered["status"] == "error"
        assert got["error"] == rendered["error"] == body

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_bare_error_exits_one(self, mock_url, mock_post, capsys):
        mock_post.return_value = self._bare_error(400, "url is required")

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        assert json.loads(capsys.readouterr().out)["error"] == "url is required"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_body_already_carrying_a_status_is_untouched(self, mock_url, mock_post):
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {"status": "not_found"}
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result == {"status": "not_found"}


class TestScreenshotDoesNotTrustTheContentType:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_non_200_labelled_as_an_image_is_not_saved(
        self, mock_url, mock_post, workspace,
    ):
        # An intermediary answering 502 while labelling it image/png used to
        # have its error page written to disk as a .png and reported ok.
        resp = MagicMock()
        resp.status_code = 502
        resp.headers = {"content-type": "image/png"}
        resp.content = b"<html>502 Bad Gateway</html>"
        mock_post.return_value = resp

        output = workspace / "shot.png"
        parser = build_parser()
        result = cmd_screenshot(
            parser.parse_args(["screenshot", "https://example.com", "-o", str(output)]),
        )

        assert result["status"] == "error"
        assert "502" in result["error"]
        assert not output.exists()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_empty_image_body_is_not_a_screenshot(self, mock_url, mock_post, workspace):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b""
        mock_post.return_value = resp

        output = workspace / "shot.png"
        parser = build_parser()
        result = cmd_screenshot(
            parser.parse_args(["screenshot", "https://example.com", "-o", str(output)]),
        )

        assert result["status"] == "error"
        assert not output.exists()


class TestLinksCleansUpItsSession:
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_session_is_closed_when_the_extract_leg_fails(
        self, mock_url, mock_post, mock_delete,
    ):
        # The old code raised out of resp.json() on the second leg, skipping
        # the cleanup entirely and stranding one of only two browser tabs for
        # the full session TTL. Returning an error dict runs the delete.
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok", "url": "https://example.com", "session_id": "sess1", "links": [],
        }
        mock_post.side_effect = [browse_resp, _non_json_response(500, FLASK_500)]

        parser = build_parser()
        result = cmd_links(
            parser.parse_args(["links", "https://example.com", "--selector", "nav a"]),
        )

        mock_delete.assert_called_once()
        assert "sessions/sess1" in mock_delete.call_args[0][0]
        assert result["status"] == "error"
        assert "500" in result["error"]


class TestTheCatchAllNamesTheFailure:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_read_timeout_is_not_reported_as_two_bare_words(
        self, mock_url, mock_post, capsys,
    ):
        # httpx's ReadTimeout stringifies to "timed out" and several of its
        # siblings to "", naming no verb, no URL and no class — the same
        # defect ISSUE-383 fixed one layer down.
        mock_post.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        error = json.loads(capsys.readouterr().out)["error"]
        assert "ReadTimeout" in error
        assert "get" in error
        assert "http://test:9223" in error

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_exception_with_no_message_still_names_its_class(
        self, mock_url, mock_post, capsys,
    ):
        mock_post.side_effect = httpx.RemoteProtocolError("")

        with pytest.raises(SystemExit):
            main(["get", "https://example.com"])

        error = json.loads(capsys.readouterr().out)["error"]
        assert "RemoteProtocolError" in error
        assert not error.rstrip().endswith(":")
