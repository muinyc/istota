"""Tumblr API v2 provider.

Emits :class:`FetchedItem` straight into the native poller; no Atom XML
intermediate.

Uses ``requests`` rather than ``httpx`` because Tumblr's API edge
disconnects httpx clients without sending a response (likely a TLS / JA3
fingerprint difference). curl, urllib, and requests all work fine.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from istota.feeds.models import FeedRateLimited, FetchedItem, retry_after_from_headers
from istota.feeds.sanitize import dedupe_image_variants


PROVIDER_NAME = "tumblr"
TUMBLR_HOST = "api.tumblr.com"
TUMBLR_API_BASE = f"https://{TUMBLR_HOST}/v2/blog"


def fetch(identifier: str, *, api_key: str = "", limit: int = 50) -> list[FetchedItem]:
    """Fetch recent posts from a Tumblr blog.

    Args:
        identifier: Blog name (e.g. ``"nemfrog"`` for ``nemfrog.tumblr.com``).
        api_key: Tumblr API key. Falls back to ``TUMBLR_API_KEY`` env var.
        limit: Max posts to fetch (Tumblr caps at 50 per call).
    """
    key = api_key or os.environ.get("TUMBLR_API_KEY", "")
    if not key:
        raise ValueError("TUMBLR_API_KEY not set")

    blog = (identifier or "").strip()
    if not blog:
        # Same reason as the Are.na provider: an empty identifier built
        # `blog//posts` and 404'd, which is indistinguishable from a blog that
        # is gone (ISSUE-432).
        raise ValueError("tumblr feed has no blog identifier")

    limit = min(int(limit), 50)
    # Quoted for the reason the Are.na provider is, with one more consequence
    # here: the API key rides in the query, so a `..` walking up the path takes
    # the user's credential to another endpoint with it. `safe=""` leaves an
    # ordinary blog name or custom domain untouched.
    url = f"{TUMBLR_API_BASE}/{quote(blog, safe='')}/posts"
    params = {"api_key": key, "limit": limit, "npf": "true"}

    resp = requests.get(url, params=params, timeout=30.0)
    # Same treatment as Are.na: a 429 carries the server's own answer in a
    # header that raise_for_status discards (ISSUE-347). Tumblr's limit is
    # per-key as well as per-IP, so it is the same failure by another route.
    # Both reads are `getattr`, matching how `_poll_rss` reads the same
    # response: the object is whatever the injected client returns, and a stub
    # standing in for one need only carry what the mapping below reads.
    # `retry_after_from_headers` folds header case — `httpx` and `requests` both
    # hand back a case-insensitive mapping, a plain dict does not, and a lookup
    # that quietly missed would put the feed back on the generic doubling.
    if getattr(resp, "status_code", 0) == 429:
        raise FeedRateLimited(
            retry_after_from_headers(getattr(resp, "headers", {}) or {}),
            host=TUMBLR_HOST,
        )
    resp.raise_for_status()
    data = resp.json()

    # A payload that is not a post collection must not read as "no posts"
    # (ISSUE-388). The old `.get("response", {}).get("posts", [])` turned every
    # malformed shape — an error object, a captcha page decoded as JSON, an
    # API change — into an empty list indistinguishable from a blog with
    # nothing on it, so a broken API reported a clean poll: `last_error`
    # cleared, the ordinary cadence kept, and nothing saying the blog had
    # stopped arriving. Raised instead; `poll_feed` turns it into the ordinary
    # error result, which backs off and advances no observation state.
    if not isinstance(data, dict):
        raise ValueError("tumblr payload is not a JSON object")
    response = data.get("response")
    if not isinstance(response, dict):
        raise ValueError("tumblr payload has no `response` object")
    posts = response.get("posts")
    if not isinstance(posts, list):
        raise ValueError("tumblr `response.posts` is missing or not a list")

    items: list[FetchedItem] = []

    for post in posts:
        post_id = str(post.get("id", ""))
        post_url = post.get("post_url") or None
        title = post.get("summary") or post.get("slug") or None

        published_iso: str | None = None
        raw_date = post.get("date")
        if raw_date:
            try:
                published_iso = datetime.strptime(
                    raw_date, "%Y-%m-%d %H:%M:%S %Z"
                ).replace(tzinfo=timezone.utc).isoformat()
            except (ValueError, TypeError):
                published_iso = None

        # NPF content blocks plus reblog trail. A reblog-with-commentary
        # repeats the original's photos in both places, so the collected
        # images are de-duplicated below (ISSUE-162).
        all_blocks = list(post.get("content", []))
        for trail_entry in post.get("trail", []):
            all_blocks.extend(trail_entry.get("content", []))

        image_urls: list[str] = []
        text_parts: list[str] = []
        for block in all_blocks:
            block_type = block.get("type", "")
            if block_type == "image":
                media = block.get("media") or []
                if media:
                    img = media[0].get("url", "")
                    if img:
                        image_urls.append(img)
            elif block_type == "text":
                text_parts.append(block.get("text", ""))

        items.append(FetchedItem(
            guid=post_id,
            title=(title[:200] if title else None),
            url=post_url,
            content_text=("\n".join(text_parts) if text_parts else None),
            image_urls=dedupe_image_variants(image_urls),
            author=identifier,
            published_at=published_iso,
        ))

    return items
