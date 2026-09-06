"""RSS source resolver — recent entries from a Feeds subscription/category.

Reuses the Feeds module (soft dependency). If Feeds is disabled/unavailable or
the referenced feed/category is gone, returns an empty result with a
provenance note; never raises.

Source config shape::

    {"feed_ref": {"kind": "subscription"|"category"|"url", "value": ...},
     "unread_only": true, "limit": 10, "lookback_hours": 24}
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from istota.briefings.sources import GatheredSource, SourceContext


logger = logging.getLogger(__name__)


def _cutoff_iso(lookback_hours: float, now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=lookback_hours)).isoformat()


def resolve(config: dict, ctx: SourceContext) -> GatheredSource:
    feed_ref = config.get("feed_ref") or {}
    ref_kind = feed_ref.get("kind")
    ref_value = feed_ref.get("value")
    limit = int(config.get("limit", 10) or 10)
    unread_only = bool(config.get("unread_only", False))
    lookback_hours = float(
        config.get("lookback_hours", ctx.module_config.default_lookback_hours)
    )
    title = f"RSS: {ref_value}" if ref_value else "RSS"

    try:
        from istota import feeds
        from istota.feeds import db as feeds_db
    except Exception:  # noqa: BLE001
        return GatheredSource(
            kind="rss", title=title,
            provenance="(RSS unavailable — Feeds module not installed)", ok=False,
        )

    try:
        fctx = feeds.resolve_for_user(ctx.user_id, ctx.app_config, conn=ctx.conn)
    except feeds.UserNotFoundError:
        return GatheredSource(
            kind="rss", title=title,
            provenance="(RSS unavailable — Feeds module off)", ok=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("rss source: feeds resolve failed for %s: %s", ctx.user_id, e)
        return GatheredSource(
            kind="rss", title=title, provenance="(RSS unavailable)", ok=False,
        )

    cutoff = _cutoff_iso(lookback_hours, ctx.now)

    try:
        with feeds_db.connect(fctx.db_path) as conn:
            feed_id, category_id = _resolve_ref(conn, feeds_db, ref_kind, ref_value)
            if ref_kind in ("subscription", "category") and feed_id is None and category_id is None:
                return GatheredSource(
                    kind="rss", title=title,
                    provenance=f"(RSS source '{ref_value}' not found)", ok=False,
                )
            entries = feeds_db.list_entries(
                conn,
                limit=max(limit * 3, limit),  # over-fetch, we window client-side
                feed_id=feed_id,
                category_id=category_id,
                status="unread" if unread_only else None,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("rss source: entry read failed: %s", e)
        return GatheredSource(
            kind="rss", title=title, provenance="(RSS read failed)", ok=False,
        )

    items: list[dict] = []
    for entry in entries:
        if entry.published_at and entry.published_at < cutoff:
            continue
        items.append({
            "title": entry.title or "(untitled)",
            "summary": (entry.content_text or entry.content_html or "")[:1000],
            "url": entry.url or "",
            "published_at": entry.published_at or "",
        })
        if len(items) >= limit:
            break

    if not items:
        return GatheredSource(
            kind="rss", title=title,
            provenance=f"(no recent RSS entries in the last {int(lookback_hours)}h)",
            ok=False,
        )
    return GatheredSource(
        kind="rss", title=title, items=items,
        provenance=f"{len(items)} entries",
    )


def _resolve_ref(conn, feeds_db, ref_kind, ref_value):
    """Map a feed_ref to (feed_id, category_id) filters for list_entries."""
    if ref_kind == "subscription" and ref_value is not None:
        # value may be a feed id (int) or a URL.
        if isinstance(ref_value, int):
            return ref_value, None
        # Two spellings, one subscription. New rows are stored canonically
        # (ISSUE-432) while a `feed_ref` is whatever the user typed, and rows
        # added before that fix hold the old form — so try the raw string
        # first, then its canonical form. Getting this wrong is not a narrow
        # miss: an unresolved ref returns `None`, which reads as *no filter*
        # below, so the block silently widens from one feed to every feed.
        # Imported here rather than at module scope, like `feeds_db` above:
        # the feeds module is optional and this file loads without it.
        from istota.feeds.models import normalize_feed_url

        raw = str(ref_value)
        feed = feeds_db.get_feed_by_url(conn, raw)
        if feed is None:
            canonical = normalize_feed_url(raw)
            if canonical and canonical != raw:
                feed = feeds_db.get_feed_by_url(conn, canonical)
        return (feed.id if feed else None), None
    if ref_kind == "category" and ref_value is not None:
        # value may be a category id (int) or a slug.
        if isinstance(ref_value, int):
            return None, ref_value
        cat = feeds_db.get_category_by_slug(conn, str(ref_value))
        return None, (cat.id if cat else None)
    # 'url' or unspecified → no filter (whole feed set within the window).
    return None, None
