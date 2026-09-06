"""Are.na API provider (v3).

Reads ``GET /v3/channels/{slug}/contents`` and emits :class:`FetchedItem`.

Why v3 rather than v2: v3 hands back ``content`` / ``description`` as
``{markdown, html, plain}``, so we render real HTML instead of v2's
HTML-escaped markdown (a quote arrived as a literal ``&gt;``), and its
``image.src`` is the original with no ``?bc=0`` cache-buster to strip. Block
ids are identical across both versions, so ``guid`` is stable and switching
re-inserts nothing.

Every block type maps to something renderable. That is the point of the
module rather than a nicety: the reader paints a card from title / body /
images, so a type we don't map produces a *blank card*, which is what
``Embed`` (YouTube, Vimeo — v2 called it ``Media``) used to do. Unknown and
future types fall through to :func:`_build_generic`, and
:func:`_ensure_renderable` is the backstop that guarantees a body.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from istota.feeds.models import FeedRateLimited, FetchedItem, retry_after_from_headers
from istota.feeds.sanitize import html_to_text, sanitize_html


logger = logging.getLogger("istota.feeds.providers.arena")


PROVIDER_NAME = "arena"
ARENA_HOST = "api.are.na"
ARENA_API_BASE = f"https://{ARENA_HOST}/v3/channels"
ARENA_BLOCK_URL = "https://www.are.na/block/{id}"
ARENA_CHANNEL_URL = "https://www.are.na/channel/{slug}"

USER_AGENT = "istota-feeds/0.1 (+https://github.com/istota-project/istota)"

# The API rejects a larger page with a 400.
MAX_PER_PAGE = 100

# v3 takes one combined token; v2's `sort=position&direction=desc` pair is a
# 400 here. Position-desc is newest-connected-first.
SORT_NEWEST_FIRST = "position_desc"


def fetch(identifier: str, *, limit: int = 50) -> list[FetchedItem]:
    """Fetch recent blocks from an Are.na channel.

    Args:
        identifier: Channel slug (e.g. ``"my-channel"``).
        limit: Max blocks to fetch (the API caps a page at 100).
    """
    slug = (identifier or "").strip()
    if not slug:
        # An identifier that normalizes away used to build `channels//contents`
        # and come back 404, which reads exactly like a channel that has been
        # deleted — the confusion ISSUE-432 is about. Name the real cause; the
        # poller turns this into the feed's `last_error`.
        raise ValueError("arena feed has no channel identifier")

    per = max(1, min(int(limit), MAX_PER_PAGE))
    # Quoted, never interpolated raw. The slug is user-typed and reaches the
    # path unescaped otherwise, where `..` walks up the API and a `?` retargets
    # the request — and that second one fails *silently*, because `params=`
    # replaces the query, so the poll gets a 200 carrying the channel object
    # instead of its contents. `safe=""` leaves a real slug untouched: `quote`
    # never encodes letters, digits or `_.-~`.
    url = f"{ARENA_API_BASE}/{quote(slug, safe='')}/contents"

    resp = httpx.get(
        url,
        params={"per": per, "sort": SORT_NEWEST_FIRST},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
    )
    # 429 before raise_for_status: the generic HTTPStatusError keeps the
    # message text and drops the headers, so the server's own answer to "when
    # may I come back" was being thrown away and the channel took the ordinary
    # error doubling instead (ISSUE-347).
    # Both reads are `getattr`, matching how `_poll_rss` reads the same
    # response: the object is whatever the injected client returns, and a stub
    # standing in for one need only carry what the mapping below reads.
    # `retry_after_from_headers` folds header case — `httpx` and `requests` both
    # hand back a case-insensitive mapping, a plain dict does not, and a lookup
    # that quietly missed would put the feed back on the generic doubling.
    if getattr(resp, "status_code", 0) == 429:
        raise FeedRateLimited(
            retry_after_from_headers(getattr(resp, "headers", {}) or {}),
            host=ARENA_HOST,
        )
    resp.raise_for_status()
    payload = resp.json()

    # A payload that is not a block collection must not read as "no blocks"
    # (ISSUE-388). The old `payload.get("data") or []` turned every malformed
    # shape into an empty list indistinguishable from an empty channel, so a
    # broken API, a captcha page decoded as JSON or a v4 rename all reported a
    # clean poll of an empty channel: `last_error` cleared, the ordinary
    # cadence kept, and nothing anywhere saying the channel had stopped
    # arriving. Raised instead; `poll_feed` turns it into the ordinary error
    # result, which backs off and advances no observation state. A malformed
    # *block* is still dropped individually below — one bad block must not
    # cost the channel its poll — but a malformed *document* is not evidence
    # about any block.
    if not isinstance(payload, dict):
        raise ValueError("are.na payload is not a JSON object")
    blocks = payload.get("data")
    if not isinstance(blocks, list):
        raise ValueError("are.na payload `data` is missing or not a list")

    items: list[FetchedItem] = []
    for block in blocks:
        item = _block_to_item(block)
        if item is not None:
            items.append(item)
    return items


def _block_to_item(block: dict) -> FetchedItem | None:
    """Map one block, or None when it can't be identified.

    A malformed block is dropped with a warning rather than raised: one bad
    block must not cost the channel its entire poll.
    """
    if not isinstance(block, dict):
        return None
    block_id = block.get("id")
    if block_id in (None, ""):
        return None

    block_type = str(block.get("type") or "")
    builder = _BUILDERS.get(block_type, _build_generic)
    try:
        item = builder(block, str(block_id))
    except Exception as exc:  # noqa: BLE001 — one block must not fail the poll
        logger.warning(
            "arena_block_map_failed id=%s type=%s err=%s", block_id, block_type, exc,
        )
        item = _build_generic(block, str(block_id), _safe=True)
    return _ensure_renderable(item, block, str(block_id))


# -- per-type builders --------------------------------------------------------


def _build_text(block: dict, block_id: str) -> FetchedItem:
    """A written block: the text itself, then whatever it's attributed to.

    Text is the one type where ``content`` and ``description`` are *both*
    body copy rather than body-and-metadata — the block holds a quote and the
    description names its source ("Theodore Roosevelt, Sorbonne, 1910"). On a
    channel of quotations the citation is the half that says what you're
    reading, so dropping it (which reading only ``content`` does) loses the
    more useful field. Everywhere else ``description`` *is* the body and
    ``content`` is empty, which is why the other builders read the opposite
    field.
    """
    body_html, body_text = _rich_text(block.get("content"))
    note_html, note_text = _rich_text(block.get("description"))

    # Are.na's editor lets the same words end up in both fields; printing the
    # block twice reads as a bug.
    if note_text and body_text and note_text.strip() == body_text.strip():
        note_html, note_text = None, None

    return _item(
        block, block_id,
        content_html="\n".join(p for p in (body_html, note_html) if p) or None,
        content_text=_join_text(body_text, note_text),
    )


def _build_image(block: dict, block_id: str) -> FetchedItem:
    # The curator's note on *why* they saved the picture is real content; v2
    # dropped it for Image blocks entirely.
    body_html, body_text = _rich_text(block.get("description"))
    return _item(
        block, block_id,
        content_html=body_html,
        content_text=body_text,
        image_urls=_image_urls(block),
    )


def _build_link(block: dict, block_id: str) -> FetchedItem:
    body_html, body_text = _rich_text(block.get("description"))
    source_url = _source_url(block)
    html_parts = [p for p in (body_html, _source_link(block)) if p]
    return _item(
        block, block_id,
        content_html="\n".join(html_parts) or None,
        content_text=_join_text(body_text, source_url),
        image_urls=_image_urls(block),
    )


def _build_embed(block: dict, block_id: str) -> FetchedItem:
    """YouTube / Vimeo and friends — v2's ``Media``.

    The thumbnail Are.na already resolved becomes the card image and the
    watch URL rides on ``embed_url`` so the reader can build its own player.
    The provider's ``embed.html`` iframe is deliberately discarded: keeping it
    would mean allowing ``<iframe>`` through a sanitizer every RSS feed also
    passes through, and Are.na frequently wraps the frame in a third-party
    ``cdn.embedly.com`` document.
    """
    body_html, body_text = _rich_text(block.get("description"))
    source_url = _source_url(block)
    html_parts = [
        p for p in (body_html, _source_link(block, verb="Watch on")) if p
    ]
    return _item(
        block, block_id,
        content_html="\n".join(html_parts) or None,
        content_text=_join_text(body_text, source_url),
        image_urls=_image_urls(block),
        embed_url=source_url,
    )


def _build_attachment(block: dict, block_id: str) -> FetchedItem:
    """An uploaded file — in practice a PDF almost every time.

    ``file_url`` is what stops the reader treating this like a picture post:
    Are.na renders a cover page for a PDF, so without it the card looks like
    an image and a click zooms page 1 rather than opening the document.
    """
    body_html, body_text = _rich_text(block.get("description"))
    attachment = block.get("attachment")
    attachment = attachment if isinstance(attachment, dict) else {}
    file_url = attachment.get("url")

    parts = [p for p in (body_html,) if p]
    label = None
    if file_url:
        label = _file_label(block, attachment)
        parts.append(
            f'<p><a href="{html.escape(file_url, quote=True)}">'
            f"{html.escape(label)}</a></p>"
        )
    return _item(
        block, block_id,
        content_html="\n".join(parts) or None,
        content_text=_join_text(body_text, label),
        # Are.na renders a cover page for PDFs; without it the card is text-only.
        image_urls=_image_urls(block),
        file_url=str(file_url) if file_url else None,
    )


def _build_channel(block: dict, block_id: str) -> FetchedItem:
    """A nested channel connected into this one — not a Block at all."""
    body_html, body_text = _rich_text(block.get("description"))
    slug = block.get("slug")
    url = ARENA_CHANNEL_URL.format(slug=slug) if slug else _block_url(block_id)

    counts = block.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    total = counts.get("contents")
    note = f"Channel — {total} blocks" if total is not None else "Channel"

    parts = [p for p in (body_html,) if p]
    parts.append(
        f'<p><a href="{html.escape(url, quote=True)}">{html.escape(note)}</a></p>'
    )
    return _item(
        block, block_id,
        url=url,
        content_html="\n".join(parts),
        content_text=_join_text(body_text, note),
    )


def _build_generic(block: dict, block_id: str, *, _safe: bool = False) -> FetchedItem:
    """Fallback for an unrecognised (or newly introduced) block type.

    Takes the fields every block shares, so a type we've never heard of still
    lands as a readable card. ``_safe`` skips the rich-text walk for a block
    that already blew up in its own builder.
    """
    body_html: str | None = None
    body_text: str | None = None
    images: list[str] = []
    if not _safe:
        body_html, body_text = _rich_text(
            block.get("content") or block.get("description")
        )
        images = _image_urls(block)

    source_link = _source_link(block) if not _safe else None
    parts = [p for p in (body_html, source_link) if p]
    return _item(
        block, block_id,
        content_html="\n".join(parts) or None,
        content_text=_join_text(body_text, _source_url(block) if not _safe else None),
        image_urls=images,
    )


_BUILDERS = {
    "Text": _build_text,
    "Image": _build_image,
    "Link": _build_link,
    "Embed": _build_embed,
    "Attachment": _build_attachment,
    "Channel": _build_channel,
}


# -- assembly helpers ---------------------------------------------------------


def _item(
    block: dict,
    block_id: str,
    *,
    url: str | None = None,
    content_html: str | None = None,
    content_text: str | None = None,
    image_urls: list[str] | None = None,
    embed_url: str | None = None,
    file_url: str | None = None,
) -> FetchedItem:
    """Fill in the fields every block type shares."""
    return FetchedItem(
        guid=block_id,
        title=_title(block),
        url=url or _block_url(block_id),
        author=_author(block),
        content_html=content_html,
        content_text=content_text,
        image_urls=image_urls or [],
        embed_url=embed_url,
        file_url=file_url,
        published_at=_published_at(block),
    )


def _ensure_renderable(
    item: FetchedItem, block: dict, block_id: str,
) -> FetchedItem:
    """Guarantee the card has something to paint.

    A block with no title, no body and no image renders as an empty rectangle
    carrying only a feed name and a date. When everything else came back
    empty, fall back to naming the block and linking it.
    """
    if item.content_html or item.content_text or item.image_urls or item.title:
        return item

    block_type = str(block.get("type") or "Block")
    label = f"{block_type} on Are.na"
    link = item.url or _block_url(block_id)
    item.content_html = (
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(label)}</a></p>'
    )
    item.content_text = f"{label} — {link}"
    return item


def _rich_text(node) -> tuple[str | None, str | None]:
    """Unpack a v3 ``{markdown, html, plain}`` field to ``(html, text)``.

    The HTML is sanitised — a block body is user-authored content from an
    arbitrary Are.na account.
    """
    if not isinstance(node, dict):
        return None, None
    cleaned = sanitize_html(node.get("html"))
    plain = node.get("plain") or node.get("markdown")
    if not plain:
        plain = html_to_text(cleaned)
    return cleaned or None, (plain or None)


def _image_urls(block: dict) -> list[str]:
    """The block's picture, if it has one.

    v3's ``image.src`` is already the original, so unlike v2 there is no
    ``?bc=0`` cache-buster to strip and no display/original preference to
    express. The sized renditions (``small``/``large``/…) are ignored: the
    reader lays out its own grid.
    """
    image = block.get("image")
    if not isinstance(image, dict):
        return []
    src = image.get("src")
    if not src:
        # Defensive: a rendition is better than nothing if `src` ever goes away.
        for key in ("large", "display", "medium"):
            variant = image.get(key)
            if isinstance(variant, dict) and variant.get("src"):
                src = variant["src"]
                break
    return [str(src)] if src else []


def _source(block: dict) -> dict:
    source = block.get("source")
    return source if isinstance(source, dict) else {}


def _source_url(block: dict) -> str | None:
    url = _source(block).get("url")
    return str(url) if url else None


def _source_link(block: dict, *, verb: str = "Source") -> str | None:
    """An anchor to wherever the block came from, named by its provider."""
    url = _source_url(block)
    if not url:
        return None
    provider = _source(block).get("provider")
    provider_name = provider.get("name") if isinstance(provider, dict) else None
    label = f"{verb} {provider_name}" if provider_name else url
    return (
        f'<p><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></p>'
    )


def _file_label(block: dict, attachment: dict) -> str:
    """Link text for the file: ``Open PDF (18.1 MB)``.

    The block title is nearly always curated rather than a filename ("The
    Cognitive Style of PowerPoint", not ``dewey-1934.pdf``) and the card
    already shows it above the body, so repeating it as the link text tells
    the reader nothing. Name the action and the cost instead — the format and
    the size are the two things that decide whether you click.

    A block with *no* title falls back to naming the file, but never to v3's
    ``attachment.filename``: that is the hashed storage name
    (``63d0752ac2e5….pdf``), not anything a person wrote.
    """
    kind = _file_kind(attachment)
    size = _human_size(attachment.get("file_size"))

    if block.get("title"):
        base = f"Open {kind}"
    else:
        # v2 carried a human `file_name`; v3 does not. Accept it when present.
        base = str(attachment.get("file_name") or kind)

    return f"{base} ({size})" if size else base


def _file_kind(attachment: dict) -> str:
    """A short uppercase format name: ``PDF``, ``MP3``, else ``file``."""
    ext = attachment.get("file_extension")
    if not ext:
        content_type = str(attachment.get("content_type") or "")
        ext = content_type.rpartition("/")[2] or ""
    ext = str(ext).strip().lstrip(".")
    # Guard against a content-type subtype that isn't a format name
    # ("octet-stream", "vnd.openxmlformats-…").
    if ext and ext.isalnum() and len(ext) <= 5:
        return ext.upper()
    return "file"


def _human_size(num) -> str | None:
    """Binary-prefixed file size, matching how Are.na's own UI reads."""
    try:
        size = float(num)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return None


def _title(block: dict) -> str | None:
    title = block.get("title")
    return str(title) if title else None


def _author(block: dict) -> str | None:
    user = block.get("user")
    if not isinstance(user, dict):
        return None
    # v3 users carry `name`; v2 carried `full_name`. Accept both so a fixture
    # or an older cached payload still names its author.
    name = user.get("name") or user.get("full_name") or user.get("slug")
    return str(name) if name else None


def _published_at(block: dict) -> str | None:
    """When this block entered *this* channel, falling back to its creation.

    Connection time is the right sort key for a reader: a decade-old block
    connected today is new to the channel.
    """
    connection = block.get("connection")
    connected_at = (
        connection.get("connected_at") if isinstance(connection, dict) else None
    )
    return _parse_datetime(connected_at) or _parse_datetime(block.get("created_at"))


def _join_text(*parts: str | None) -> str | None:
    joined = "\n".join(p for p in parts if p)
    return joined or None


def _block_url(block_id: str) -> str:
    return ARENA_BLOCK_URL.format(id=block_id)


def _parse_datetime(value: str | None) -> str | None:
    """Parse an Are.na timestamp to a UTC ISO 8601 string.

    v3 stamps ``Z``, which ``fromisoformat`` only learned to parse in 3.11.
    """
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None
