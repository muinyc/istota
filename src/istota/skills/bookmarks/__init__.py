"""Karakeep bookmarks skill — search, browse, and manage bookmarks.

Usage:
    python -m istota.skills.bookmarks search "query" [--limit N] [--sort relevance|asc|desc]
    python -m istota.skills.bookmarks list [--limit N] [--favourited] [--archived] [--tag TAG] [--in-list NAME]
    python -m istota.skills.bookmarks get BOOKMARK_ID [--include-content]
    python -m istota.skills.bookmarks add URL_OR_TEXT [--text] [--title T] [--tags t1,t2] [--note N]
    python -m istota.skills.bookmarks tags [--search NAME]
    python -m istota.skills.bookmarks tag BOOKMARK_ID "tag1,tag2"
    python -m istota.skills.bookmarks untag BOOKMARK_ID "tag1"
    python -m istota.skills.bookmarks lists
    python -m istota.skills.bookmarks list-bookmarks LIST_ID [--limit N]
    python -m istota.skills.bookmarks summarize BOOKMARK_ID
    python -m istota.skills.bookmarks stats
    python -m istota.skills.bookmarks highlights [--bookmark BOOKMARK_ID] [--limit N]

Environment variables:
    KARAKEEP_BASE_URL  — API base URL (e.g. https://keep.example.com/api/v1)
    KARAKEEP_API_KEY   — Bearer token for authentication
"""

import argparse
import os

import httpx

from istota.skills._cli import run_skill_cli


REQUEST_TIMEOUT = 30.0


class KarakeepClient:
    """Thin httpx client for the Karakeep REST API."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"

        resp = httpx.request(
            method, url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        if resp.status_code == 204:
            return {"status": "ok"}
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params: dict, limit: int, key: str = "bookmarks") -> list:
        """Fetch paginated results, following cursors up to limit."""
        results = []
        params = dict(params)
        # includeContent only applies to bookmark payloads; other endpoints
        # (tags, highlights) have no content field and may reject the param.
        if key == "bookmarks":
            params["includeContent"] = False
        if limit:
            params["limit"] = min(limit, 100)

        while True:
            data = self._request("GET", path, params=params)
            page_items = data.get(key, [])
            results.extend(page_items)

            if limit and len(results) >= limit:
                return results[:limit]

            next_cursor = data.get("nextCursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor

        return results

    # --- Bookmarks ---

    def search(self, query: str, limit: int = 20, sort: str = "relevance") -> list[dict]:
        params = {"q": query, "sortOrder": sort}
        return self._paginate("/bookmarks/search", params, limit)

    def get_bookmark(self, bookmark_id: str, include_content: bool = True) -> dict:
        params = {"includeContent": include_content}
        return self._request("GET", f"/bookmarks/{bookmark_id}", params=params)

    def list_bookmarks(
        self,
        limit: int = 20,
        archived: bool | None = None,
        favourited: bool | None = None,
    ) -> list[dict]:
        params: dict = {}
        if archived is not None:
            params["archived"] = archived
        if favourited is not None:
            params["favourited"] = favourited
        return self._paginate("/bookmarks", params, limit)

    def create_bookmark(
        self,
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        note: str | None = None,
    ) -> dict:
        if url:
            body: dict = {"type": "link", "url": url}
        elif text:
            body = {"type": "text", "text": text}
        else:
            raise ValueError("Must provide either url or text")

        if title:
            body["title"] = title
        if note:
            body["note"] = note

        return self._request("POST", "/bookmarks", json=body)

    def update_bookmark(self, bookmark_id: str, **fields) -> dict:
        return self._request("PATCH", f"/bookmarks/{bookmark_id}", json=fields)

    def delete_bookmark(self, bookmark_id: str) -> dict:
        return self._request("DELETE", f"/bookmarks/{bookmark_id}")

    def summarize(self, bookmark_id: str) -> dict:
        return self._request("POST", f"/bookmarks/{bookmark_id}/summarize")

    # --- Tags ---

    def list_tags(self, name_contains: str | None = None) -> list[dict]:
        params: dict = {}
        if name_contains:
            params["nameContains"] = name_contains
        return self._paginate("/tags", params, limit=0, key="tags")

    def tag_bookmark(self, bookmark_id: str, tag_names: list[str]) -> dict:
        body = {
            "tags": [{"tagName": name, "attachedBy": "human"} for name in tag_names],
        }
        return self._request("POST", f"/bookmarks/{bookmark_id}/tags", json=body)

    def untag_bookmark(self, bookmark_id: str, tag_names: list[str]) -> dict:
        body = {
            "tags": [{"tagName": name} for name in tag_names],
        }
        return self._request("DELETE", f"/bookmarks/{bookmark_id}/tags", json=body)

    def get_bookmarks_by_tag(self, tag_name: str, limit: int = 20) -> list[dict]:
        tags = self.list_tags(name_contains=tag_name)
        # Find exact match
        tag = next((t for t in tags if t["name"] == tag_name), None)
        if not tag:
            return []
        return self._paginate(f"/tags/{tag['id']}/bookmarks", {}, limit)

    # --- Lists ---

    def list_lists(self) -> list[dict]:
        data = self._request("GET", "/lists")
        return data.get("lists", [])

    def get_list_bookmarks(self, list_id: str, limit: int = 20) -> list[dict]:
        return self._paginate(f"/lists/{list_id}/bookmarks", {}, limit)

    def get_list_by_name(self, name: str) -> dict | None:
        """Find a list by name (case-insensitive)."""
        lists = self.list_lists()
        name_lower = name.lower()
        return next((lst for lst in lists if lst["name"].lower() == name_lower), None)

    # --- Stats ---

    def stats(self) -> dict:
        return self._request("GET", "/users/me/stats")

    # --- Highlights ---

    def list_highlights(self, limit: int = 0) -> list[dict]:
        # limit=0 means "all", following the list_tags convention.
        return self._paginate("/highlights", {}, limit, key="highlights")

    def get_bookmark_highlights(self, bookmark_id: str) -> list[dict]:
        return self._paginate(
            f"/bookmarks/{bookmark_id}/highlights", {}, limit=0, key="highlights"
        )


def get_client() -> KarakeepClient:
    """Create a client from environment variables."""
    base_url = os.environ.get("KARAKEEP_BASE_URL")
    api_key = os.environ.get("KARAKEEP_API_KEY")
    if not base_url or not api_key:
        raise ValueError("KARAKEEP_BASE_URL and KARAKEEP_API_KEY must be set")
    return KarakeepClient(base_url, api_key)


def format_bookmark(bm: dict) -> dict:
    """Format a raw API bookmark into a concise summary dict."""
    content = bm.get("content", {})
    result: dict = {
        "id": bm["id"],
        "title": bm.get("title"),
        "tags": [t["name"] for t in bm.get("tags", [])],
        "favourited": bm.get("favourited", False),
        "archived": bm.get("archived", False),
        "summary": bm.get("summary"),
        "note": bm.get("note"),
        "created": bm.get("createdAt"),
    }
    content_type = content.get("type")
    if content_type == "link":
        result["url"] = content.get("url")
    elif content_type == "text":
        result["text"] = content.get("text")
    return result


def format_highlight(h: dict) -> dict:
    """Format a raw API highlight into a concise summary dict."""
    return {
        "id": h["id"],
        "bookmark_id": h.get("bookmarkId"),
        "text": h.get("text"),
        "note": h.get("note"),
        "color": h.get("color"),
        "created": h.get("createdAt"),
        "start_offset": h.get("startOffset"),
        "end_offset": h.get("endOffset"),
    }


# --- Command handlers ---


def cmd_search(args) -> dict:
    client = get_client()
    bookmarks = client.search(args.query, limit=args.limit, sort=args.sort)
    return {
        "status": "ok",
        "query": args.query,
        "count": len(bookmarks),
        "bookmarks": [format_bookmark(bm) for bm in bookmarks],
    }


def cmd_list(args) -> dict:
    client = get_client()
    list_name = getattr(args, "in_list", None)
    if list_name:
        lst = client.get_list_by_name(list_name)
        if not lst:
            return {"status": "error", "error": f"List '{list_name}' not found"}
        bookmarks = client.get_list_bookmarks(lst["id"], limit=args.limit)
    elif args.tag:
        bookmarks = client.get_bookmarks_by_tag(args.tag, limit=args.limit)
    else:
        bookmarks = client.list_bookmarks(
            limit=args.limit,
            archived=True if args.archived else None,
            favourited=True if args.favourited else None,
        )
    return {
        "status": "ok",
        "count": len(bookmarks),
        "bookmarks": [format_bookmark(bm) for bm in bookmarks],
    }


def cmd_get(args) -> dict:
    client = get_client()
    bookmark = client.get_bookmark(args.bookmark_id, include_content=args.include_content)
    return {
        "status": "ok",
        "bookmark": format_bookmark(bookmark) if not args.include_content else bookmark,
    }


def cmd_add(args) -> dict:
    client = get_client()
    kwargs: dict = {}
    if args.text:
        kwargs["text"] = args.url_or_text
    else:
        kwargs["url"] = args.url_or_text
    if args.title:
        kwargs["title"] = args.title
    if args.note:
        kwargs["note"] = args.note

    bookmark = client.create_bookmark(**kwargs)

    if args.tags:
        tag_list = [t.strip() for t in args.tags.split(",") if t.strip()]
        if tag_list:
            client.tag_bookmark(bookmark["id"], tag_list)

    return {
        "status": "ok",
        "bookmark": format_bookmark(bookmark),
    }


def cmd_tags(args) -> dict:
    client = get_client()
    tags = client.list_tags(name_contains=getattr(args, "search", None))
    return {
        "status": "ok",
        "count": len(tags),
        "tags": [
            {"id": t["id"], "name": t["name"], "count": t["numBookmarks"]}
            for t in tags
        ],
    }


def cmd_tag(args) -> dict:
    client = get_client()
    tag_list = [t.strip() for t in args.tag_names.split(",") if t.strip()]
    result = client.tag_bookmark(args.bookmark_id, tag_list)
    return {"status": "ok", **result}


def cmd_untag(args) -> dict:
    client = get_client()
    tag_list = [t.strip() for t in args.tag_names.split(",") if t.strip()]
    result = client.untag_bookmark(args.bookmark_id, tag_list)
    return {"status": "ok", **result}


def cmd_lists(args) -> dict:
    client = get_client()
    lists = client.list_lists()
    return {
        "status": "ok",
        "count": len(lists),
        "lists": [
            {"id": lst["id"], "name": lst["name"], "icon": lst.get("icon", ""), "type": lst.get("type", "manual")}
            for lst in lists
        ],
    }


def cmd_list_bookmarks(args) -> dict:
    client = get_client()
    bookmarks = client.get_list_bookmarks(args.list_id, limit=args.limit)
    return {
        "status": "ok",
        "count": len(bookmarks),
        "bookmarks": [format_bookmark(bm) for bm in bookmarks],
    }


def cmd_summarize(args) -> dict:
    client = get_client()
    result = client.summarize(args.bookmark_id)
    return {"status": "ok", **result}


def cmd_stats(args) -> dict:
    client = get_client()
    stats = client.stats()
    return {"status": "ok", "stats": stats}


def cmd_highlights(args) -> dict:
    client = get_client()
    if args.bookmark:
        highlights = client.get_bookmark_highlights(args.bookmark)
    else:
        highlights = client.list_highlights(limit=args.limit)
    return {
        "status": "ok",
        "count": len(highlights),
        "highlights": [format_highlight(h) for h in highlights],
    }


# --- CLI ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.bookmarks",
        description="Karakeep bookmarks — search, browse, and manage",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p_search = sub.add_parser("search", help="Full-text search bookmarks")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=20, help="Max results")
    p_search.add_argument("--sort", default="relevance", choices=["relevance", "asc", "desc"])

    # list
    p_list = sub.add_parser("list", help="List bookmarks")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--favourited", action="store_true")
    p_list.add_argument("--archived", action="store_true")
    p_list.add_argument("--tag", help="Filter by tag name")
    p_list.add_argument("--in-list", dest="in_list", help="Filter by list name (case-insensitive)")

    # get
    p_get = sub.add_parser("get", help="Get a single bookmark")
    p_get.add_argument("bookmark_id", help="Bookmark ID")
    p_get.add_argument("--include-content", action="store_true", help="Include full content")

    # add
    p_add = sub.add_parser("add", help="Add a bookmark")
    p_add.add_argument("url_or_text", help="URL or text content")
    p_add.add_argument("--text", action="store_true", help="Treat input as text, not URL")
    p_add.add_argument("--title", help="Bookmark title")
    p_add.add_argument("--tags", help="Comma-separated tags")
    p_add.add_argument("--note", help="Bookmark note")

    # tags
    p_tags = sub.add_parser("tags", help="List all tags")
    p_tags.add_argument("--search", help="Filter tags by name substring")

    # tag
    p_tag = sub.add_parser("tag", help="Attach tags to a bookmark")
    p_tag.add_argument("bookmark_id")
    p_tag.add_argument("tag_names", help="Comma-separated tag names")

    # untag
    p_untag = sub.add_parser("untag", help="Detach tags from a bookmark")
    p_untag.add_argument("bookmark_id")
    p_untag.add_argument("tag_names", help="Comma-separated tag names")

    # lists
    sub.add_parser("lists", help="List all lists")

    # list-bookmarks
    p_lb = sub.add_parser("list-bookmarks", help="Get bookmarks in a list")
    p_lb.add_argument("list_id", help="List ID")
    p_lb.add_argument("--limit", type=int, default=20)

    # summarize
    p_sum = sub.add_parser("summarize", help="Trigger AI summarization")
    p_sum.add_argument("bookmark_id")

    # stats
    sub.add_parser("stats", help="User stats")

    # highlights
    p_hl = sub.add_parser("highlights", help="List highlights (all, or for one bookmark)")
    p_hl.add_argument("--bookmark", help="Only highlights on this bookmark ID")
    p_hl.add_argument("--limit", type=int, default=0, help="Max results (0 = all)")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "search": cmd_search,
        "list": cmd_list,
        "get": cmd_get,
        "add": cmd_add,
        "tags": cmd_tags,
        "tag": cmd_tag,
        "untag": cmd_untag,
        "lists": cmd_lists,
        "list-bookmarks": cmd_list_bookmarks,
        "summarize": cmd_summarize,
        "stats": cmd_stats,
        "highlights": cmd_highlights,
    }

    run_skill_cli(commands, args)


if __name__ == "__main__":
    main()
