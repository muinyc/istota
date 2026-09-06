"""OPML import/export.

OPML 2.0 is the lingua franca for feed reader exports. The importer here
understands two variants of feed URL:

1. Plain RSS/Atom URLs — stored as ``source_type = 'rss'``.
2. Legacy bridger-proxy URLs (``http://127.0.0.1:8900/{provider}/{id}/feed.xml``)
   — rewritten to ``{provider}:{id}`` and stored as ``source_type =
   'tumblr'`` or ``'arena'`` so the native poller dispatches to its own
   provider modules. Lets old exports import cleanly.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    FeedsContext,
    default_poll_interval_for,
    detect_source_type,
    normalize_feed_url,
)


_BRIDGER_RE = re.compile(
    r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?/(tumblr|arena)/([^/]+)/feed\.xml/?$",
    re.IGNORECASE,
)


@dataclass
class ImportResult:
    feeds_added: int = 0
    feeds_updated: int = 0
    feeds_skipped: int = 0
    categories_added: int = 0
    rewritten_bridger_urls: int = 0


def rewrite_bridger_url(url: str) -> str:
    """Translate ``http://127.0.0.1:8900/{provider}/{id}/feed.xml`` to the
    native ``{provider}:{id}`` scheme. Returns the input unchanged if it
    isn't a bridger URL.
    """
    m = _BRIDGER_RE.match(url.strip())
    if not m:
        return url
    return f"{m.group(1).lower()}:{m.group(2)}"


def import_opml(ctx: FeedsContext, opml_text: str | bytes) -> ImportResult:
    """Import feeds from an OPML 2.0 document.

    Reads OPML ``outline`` elements. Top-level outlines without an
    ``xmlUrl`` become categories; nested outlines with ``xmlUrl`` become
    feeds. Existing feeds (matched by URL after bridger-rewrite) are
    updated; new ones are inserted.
    """
    if isinstance(opml_text, bytes):
        root = ET.fromstring(opml_text)
    else:
        root = ET.fromstring(opml_text.encode("utf-8"))

    body = root.find("body")
    if body is None:
        return ImportResult()

    result = ImportResult()
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        # Read once rather than per outline: an export written before
        # ISSUE-432 carries the old spelling of every provider identifier, and
        # storing the canonical form beside the row already on file would make
        # a second subscription to the same channel out of a re-import.
        variants = feeds_db.stored_url_variants(conn)
        for child in body:
            _import_outline(
                conn, child, parent_category=None, result=result,
                variants=variants,
            )
        conn.commit()
    return result


def _import_outline(
    conn: sqlite3.Connection,
    outline: ET.Element,
    *,
    parent_category: int | None,
    result: ImportResult,
    variants: dict[str, str] | None = None,
) -> None:
    if outline.tag != "outline":
        return

    xml_url = outline.get("xmlUrl") or outline.get("xmlurl")
    if xml_url:
        # Leaf — a feed.
        title = outline.get("title") or outline.get("text") or None
        site_url = outline.get("htmlUrl") or outline.get("htmlurl")
        rewritten = rewrite_bridger_url(xml_url)
        if rewritten != xml_url:
            result.rewritten_bridger_urls += 1
        rewritten = normalize_feed_url(rewritten)
        if rewritten and variants:
            rewritten = variants.get(rewritten, rewritten)
        if not rewritten:
            # An identifier with nothing left after normalization can never
            # resolve, and storing it produces a subscription that fails
            # exactly like a dead channel (ISSUE-432). Counted rather than
            # dropped in silence.
            result.feeds_skipped += 1
            return
        source_type = detect_source_type(rewritten)

        existing = feeds_db.get_feed_by_url(conn, rewritten)
        feeds_db.upsert_feed(
            conn,
            url=rewritten,
            title=title,
            site_url=site_url,
            source_type=source_type,
            category_id=parent_category,
            poll_interval_minutes=(
                existing.poll_interval_minutes if existing
                else default_poll_interval_for(source_type)
            ),
        )
        if existing:
            result.feeds_updated += 1
        else:
            result.feeds_added += 1
        return

    # Branch — a category. Use the title as both slug source and display.
    raw_title = outline.get("title") or outline.get("text") or ""
    if not raw_title.strip():
        # Anonymous group (rare); descend without a category.
        for child in outline:
            _import_outline(
                conn, child, parent_category=parent_category, result=result,
                variants=variants,
            )
        return

    slug = _slugify(raw_title)
    existing_cat = feeds_db.get_category_by_slug(conn, slug)
    cat_id = feeds_db.upsert_category(conn, slug, raw_title.strip())
    if not existing_cat:
        result.categories_added += 1

    for child in outline:
        _import_outline(
            conn, child, parent_category=cat_id, result=result, variants=variants,
        )


def export_opml(ctx: FeedsContext) -> str:
    """Serialise the user's subscriptions as OPML 2.0.

    Categories become top-level outlines; feeds nest under their category.
    Feeds whose URL uses the ``tumblr:`` / ``arena:`` scheme are exported
    in that bare form; re-importing into istota round-trips fine.
    """
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cats = feeds_db.list_categories(conn)
        feeds = feeds_db.list_feeds(conn)

    cat_by_id = {c.id: c for c in cats}
    feeds_by_cat: dict[int | None, list] = {None: []}
    for c in cats:
        feeds_by_cat[c.id] = []
    for f in feeds:
        feeds_by_cat.setdefault(f.category_id, []).append(f)

    opml = ET.Element("opml", attrib={"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = f"istota feeds — {ctx.user_id}"
    body = ET.SubElement(opml, "body")

    for cat_id, cat_feeds in feeds_by_cat.items():
        if cat_id is None:
            for f in cat_feeds:
                _feed_outline(body, f)
        else:
            cat = cat_by_id.get(cat_id)
            if not cat:
                continue
            cat_node = ET.SubElement(
                body, "outline",
                attrib={"text": cat.title, "title": cat.title},
            )
            for f in cat_feeds:
                _feed_outline(cat_node, f)

    return ET.tostring(opml, encoding="unicode", xml_declaration=True)


def _feed_outline(parent: ET.Element, feed) -> None:
    attrib = {
        "type": "rss",
        "text": feed.title or feed.url,
        "xmlUrl": feed.url,
    }
    if feed.title:
        attrib["title"] = feed.title
    if feed.site_url:
        attrib["htmlUrl"] = feed.site_url
    ET.SubElement(parent, "outline", attrib=attrib)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "uncategorized"
