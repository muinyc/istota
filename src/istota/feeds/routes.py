"""FastAPI router for the native feeds backend.

Mounted by the host application at ``/istota/api/feeds``. Reads/writes the
per-user workspace SQLite populated by the native poller.

Auth, CSRF, and per-user resolution mirror :mod:`istota.money.routes`: the
host overrides ``require_auth`` and ``verify_origin`` via
``app.dependency_overrides`` and the istota config is read off
``request.app.state.istota_config``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import JSONResponse, PlainTextResponse

from istota.feeds import db as feeds_db
from istota.feeds._loader import UserNotFoundError, resolve_for_user
from istota.feeds._migrate import ensure_initialised
from istota.feeds.image_dedupe import (
    DEFAULT_WINDOW_DAYS,
    PageEntry,
    entry_seen_ts,
    plan_suppression,
)
from istota.feeds.models import (
    FeedsContext,
    default_poll_interval_for,
    detect_source_type,
    normalize_feed_url,
)
from istota.feeds.retention import resolve_max_entries_per_feed
from istota.feeds.sanitize import image_identity
from istota.web_router_stubs import (  # noqa: F401
    make_get_user_context,
    require_auth,  # re-exported: `web_app.py` keys `dependency_overrides` on it
    verify_origin,
)


logger = logging.getLogger("istota.feeds.routes")


# ---------------------------------------------------------------------------
# Auth dependency — host app overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


# `ensure_initialised` also runs the legacy-toml importer; it has its own
# cross-process sentinel so subsequent calls in other workers are O(1).
get_user_context = make_get_user_context(
    cache_attr="feeds_initialised_dbs",
    resolve=resolve_for_user,
    ensure=lambda ctx, cfg: ensure_initialised(ctx),
    not_found=UserNotFoundError,
)


# ---------------------------------------------------------------------------
# Mappers — DB rows → JSON wire-format consumed by the SvelteKit reader
# ---------------------------------------------------------------------------


def _map_feed(feed, cat_by_id: dict) -> dict:
    cat = cat_by_id.get(feed.category_id)
    return {
        "id": feed.id,
        "title": feed.title or feed.url,
        "site_url": feed.site_url or "",
        "category": {
            "id": cat.id if cat else 0,
            "title": cat.title if cat else "",
        },
    }


def _map_entry(
    entry, feed_by_id: dict, cat_by_id: dict, suppressed: set[str] | None = None,
) -> dict:
    feed = feed_by_id.get(entry.feed_id)
    cat = cat_by_id.get(feed.category_id) if feed else None
    hidden = suppressed or set()
    images = [url for url in (entry.image_urls or []) if url not in hidden]
    return {
        "id": entry.id,
        "title": entry.title or "",
        "url": entry.url or "",
        "content": entry.content_html or "",
        "images": images,
        # Images dropped because a newer entry already showed them. Reported
        # rather than silently omitted so the reader can still treat the entry
        # as an image post and say what it hid (ISSUE-162).
        "duplicate_image_count": len(entry.image_urls or []) - len(images),
        # Set for playable media (an Are.na Embed block). The reader turns it
        # into an inline player; empty for everything else.
        "embed_url": entry.embed_url or "",
        # Set for an attached document (an Are.na Attachment — usually a PDF).
        # The reader opens it instead of lightboxing the cover page.
        "file_url": entry.file_url or "",
        # Set for a media file the reader plays inline with a native
        # <video>/<audio> — a Mastodon video attachment, a podcast enclosure.
        # This is the field that keeps such a URL out of `images`, where it
        # rendered as an <img> that never decodes (ISSUE-356).
        "media_url": entry.media_url or "",
        "media_type": entry.media_type or "",
        "feed": {
            "id": feed.id if feed else 0,
            "title": (feed.title or feed.url) if feed else "",
            "site_url": (feed.site_url or "") if feed else "",
            "category": {
                "id": cat.id if cat else 0,
                "title": cat.title if cat else "",
            },
        },
        "status": entry.status,
        "starred": bool(entry.starred),
        "starred_at": entry.starred_at or "",
        "published_at": entry.published_at or "",
        "created_at": entry.fetched_at or "",
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get("")
async def api_feeds(
    ctx: FeedsContext = Depends(get_user_context),
    limit: int = Query(default=500, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="published_at"),
    direction: str = Query(default="desc"),
    status: str = Query(default=""),
    category_id: int = Query(default=0),
    feed_id: int = Query(default=0),
    starred: int = Query(default=-1),
    before: int = Query(default=0, ge=0),
):
    """List feeds and entries.

    ``starred`` is ``-1`` (default, no filter), ``1`` (only starred), or ``0``
    (only unstarred). Independent of ``status`` so a starred unread entry
    appears in both views.
    """
    starred_filter: bool | None
    if starred == 1:
        starred_filter = True
    elif starred == 0:
        starred_filter = False
    else:
        starred_filter = None

    def _query():
        with feeds_db.connect(ctx.db_path) as conn:
            cats = feeds_db.list_categories(conn)
            feeds = feeds_db.list_feeds(conn)
            entries = feeds_db.list_entries(
                conn,
                limit=limit,
                offset=offset,
                status=status or None,
                feed_id=feed_id or None,
                category_id=category_id or None,
                starred=starred_filter,
                before_published_ts=before or None,
                order=order,
                direction=direction,
            )
            total = feeds_db.count_entries(
                conn,
                status=status or None,
                feed_id=feed_id or None,
                category_id=category_id or None,
                starred=starred_filter,
            )
            suppressed = _plan_image_suppression(
                conn, entries,
                feed_id=feed_id or None,
                category_id=category_id or None,
                starred=starred_filter,
            )
        return cats, feeds, entries, total, suppressed

    cats, feeds, entries, total, suppressed = await asyncio.to_thread(_query)
    cat_by_id = {c.id: c for c in cats}
    feed_by_id = {f.id: f for f in feeds}

    return {
        "feeds": [_map_feed(f, cat_by_id) for f in feeds],
        "entries": [
            _map_entry(e, feed_by_id, cat_by_id, suppressed.get(e.id))
            for e in entries
        ],
        "total": total,
    }


def _plan_image_suppression(
    conn,
    entries: list,
    *,
    feed_id: int | None,
    category_id: int | None,
    starred: bool | None = None,
) -> dict[int, set[str]]:
    """Decide which image tiles this page should hide (ISSUE-162).

    A reblog wave repeats the same picture across nearby entries; the newest
    carrier keeps the tile and the rest drop it, bounded by the user's
    look-back window. Purely a read-time display decision — nothing is
    written, and entries are never dropped.

    Best-effort: any failure returns "suppress nothing" so a reader page can
    never fail on a cosmetic feature.
    """
    window_days = feeds_db.get_image_dedupe_window_days(conn)
    if window_days is None:
        window_days = DEFAULT_WINDOW_DAYS
    if window_days <= 0:
        return {}

    page = [
        PageEntry(
            entry_id=e.id,
            seen_ts=entry_seen_ts(e.published_at, e.fetched_at),
            image_urls=list(e.image_urls or []),
        )
        for e in entries
        if e.image_urls
    ]
    dated = [p for p in page if p.seen_ts is not None]
    if not dated:
        return {}

    keys = [image_identity(url) for p in dated for url in p.image_urls]
    window_seconds = window_days * 86400
    try:
        owners = feeds_db.image_key_owners(
            conn,
            keys,
            # Only owners newer than the oldest entry on the page can suppress
            # anything, and only up to one window past the newest.
            min_ts=min(p.seen_ts for p in dated),
            max_ts=max(p.seen_ts for p in dated) + window_seconds,
            feed_id=feed_id,
            category_id=category_id,
            starred=starred,
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning("feeds image suppression skipped: %s", exc)
        return {}
    return plan_suppression(dated, owners, window_days=window_days)


@router.put("/entries/batch")
async def api_update_entries_batch(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
):
    body = await request.json()
    entry_ids = body.get("entry_ids", [])
    if not entry_ids or not isinstance(entry_ids, list):
        return JSONResponse(
            {"error": "entry_ids must be a non-empty list"}, status_code=400,
        )

    has_status = "status" in body
    has_starred = "starred" in body
    if not has_status and not has_starred:
        # Back-compat: older clients always send a status. Default-mark-read
        # so the existing batch behaviour stays unchanged.
        has_status = True
        body["status"] = "read"

    new_status = body.get("status")
    if has_status and new_status not in ("read", "unread", "removed"):
        return JSONResponse(
            {"error": "status must be one of: read, unread, removed"}, status_code=400,
        )

    new_starred = body.get("starred")
    if has_starred and not isinstance(new_starred, bool):
        return JSONResponse(
            {"error": "starred must be a boolean"}, status_code=400,
        )

    def _update():
        with feeds_db.connect(ctx.db_path) as conn:
            ids = list(entry_ids)
            count = 0
            if has_status:
                count = feeds_db.update_entry_status(conn, ids, new_status)
            if has_starred:
                star_count = feeds_db.update_entry_starred(conn, ids, new_starred)
                if not has_status:
                    count = star_count
            conn.commit()
        return count

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


@router.put("/entries/{entry_id}")
async def api_update_entry(
    entry_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
):
    body = await request.json()
    has_status = "status" in body
    has_starred = "starred" in body
    if not has_status and not has_starred:
        # Back-compat: older clients send only `status`. Treat a body with
        # neither field as the legacy default of "mark read".
        has_status = True
        body["status"] = "read"

    new_status = body.get("status")
    if has_status and new_status not in ("read", "unread", "removed"):
        return JSONResponse(
            {"error": "status must be one of: read, unread, removed"}, status_code=400,
        )
    new_starred = body.get("starred")
    if has_starred and not isinstance(new_starred, bool):
        return JSONResponse(
            {"error": "starred must be a boolean"}, status_code=400,
        )

    def _update():
        with feeds_db.connect(ctx.db_path) as conn:
            count = 0
            if has_status:
                count = feeds_db.update_entry_status(conn, [entry_id], new_status)
            if has_starred:
                star_count = feeds_db.update_entry_starred(conn, [entry_id], new_starred)
                if not has_status:
                    count = star_count
            conn.commit()
        return count

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


@router.post("/mark-as-read")
async def api_mark_as_read(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
):
    """Bulk mark unread entries as read.

    Body: ``{"scope": "all|feed|category", "id": int?, "before_id": int?}``.
    ``scope=all`` ignores ``id``; the other scopes require it. ``before_id``
    caps the operation so concurrent infinite-scroll loads don't get clobbered.
    """
    body = await request.json()
    scope = body.get("scope")
    if scope not in ("all", "feed", "category"):
        return JSONResponse(
            {"error": "scope must be one of: all, feed, category"},
            status_code=400,
        )
    raw_id = body.get("id")
    if scope in ("feed", "category"):
        if not isinstance(raw_id, int) or raw_id <= 0:
            return JSONResponse(
                {"error": f"scope={scope} requires a positive integer 'id'"},
                status_code=400,
            )
    raw_before = body.get("before_id")
    if raw_before is not None and (not isinstance(raw_before, int) or raw_before <= 0):
        return JSONResponse(
            {"error": "before_id must be a positive integer"}, status_code=400,
        )

    def _update():
        with feeds_db.connect(ctx.db_path) as conn:
            n = feeds_db.mark_as_read(
                conn,
                scope=scope,
                scope_id=raw_id if scope != "all" else None,
                before_id=raw_before,
            )
            conn.commit()
        return n

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


# ---------------------------------------------------------------------------
# Settings — DB-backed CRUD + OPML
# ---------------------------------------------------------------------------


@router.get("/config")
async def api_get_config(ctx: FeedsContext = Depends(get_user_context)):
    """Return the user's subscription config (built from the DB) plus
    runtime diagnostics. Wire shape mirrors what the SvelteKit settings
    page sends back on PUT."""

    def _read():
        with feeds_db.connect(ctx.db_path) as conn:
            cats = feeds_db.list_categories(conn)
            feeds = feeds_db.list_feeds(conn)
            default_interval = feeds_db.get_default_poll_interval(conn)
            image_dedupe_window = feeds_db.get_image_dedupe_window_days(conn)
            retention_days = feeds_db.get_entry_retention_days(conn)
            max_entries = feeds_db.get_max_entries_per_feed(conn)
            total_entries = feeds_db.count_entries(conn)
            unread = feeds_db.count_entries(conn, status="unread")
        cfg = _config_payload_from_db(
            cats,
            feeds,
            default_interval,
            image_dedupe_window,
            entry_retention_days=retention_days,
            max_entries_per_feed=max_entries,
        )
        diagnostics = {
            "total_feeds": len(feeds),
            "total_entries": total_entries,
            "unread_entries": unread,
            "error_feeds": sum(1 for f in feeds if f.error_count > 0),
            # Distinct from error_feeds on purpose: a throttled channel is
            # healthy, but it is not fetching either, and before this it showed
            # on no surface at all (ISSUE-347).
            "throttled_feeds": sum(1 for f in feeds if f.last_throttled_at),
            "last_poll_at": max(
                (f.last_fetched_at for f in feeds if f.last_fetched_at),
                default=None,
            ),
        }
        feed_state = [
            {
                "url": f.url,
                "last_fetched_at": f.last_fetched_at,
                "last_error": f.last_error,
                "error_count": f.error_count,
                "last_throttled_at": f.last_throttled_at,
            }
            for f in feeds
        ]
        return cfg, diagnostics, feed_state

    cfg, diagnostics, feed_state = await asyncio.to_thread(_read)
    return {
        "config": cfg,
        "diagnostics": diagnostics,
        "feed_state": feed_state,
    }


@router.put("/config")
async def api_put_config(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
):
    """Replace the user's subscriptions with the request body. Wholesale-
    replace semantics: feeds and categories not in the payload are removed.
    """
    body = await request.json()
    config_payload = body.get("config")
    if not isinstance(config_payload, dict):
        return JSONResponse(
            {"error": "body must be {'config': {...}}"}, status_code=400,
        )

    err = _validate_feeds_config(config_payload)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    summary = await asyncio.to_thread(_apply_config_to_db, ctx, config_payload)
    return {"status": "ok", "sync": summary}


@router.get("/export-opml", response_class=PlainTextResponse)
async def api_export_opml(ctx: FeedsContext = Depends(get_user_context)):
    from istota.feeds.opml import export_opml

    text = await asyncio.to_thread(export_opml, ctx)
    return PlainTextResponse(
        text,
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="feeds.opml"'},
    )


@router.post("/refresh")
async def api_refresh(
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
):
    """Mark every feed due now. The scheduled job ``_module.feeds.run_scheduled``
    (cron ``*/5``) picks them up out-of-process — keeping the long sequential
    poll out of the web request lifecycle so shutdown is fast and one user's
    refresh can't tie up uvicorn workers.
    """

    def _reset() -> int:
        with feeds_db.connect(ctx.db_path) as conn:
            cur = conn.execute("UPDATE feeds SET next_poll_at = NULL")
            conn.commit()
            return cur.rowcount or 0

    queued = await asyncio.to_thread(_reset)
    return {"status": "queued", "feeds_queued": queued}


@router.post("/import-opml")
async def api_import_opml(
    _csrf: None = Depends(verify_origin),
    ctx: FeedsContext = Depends(get_user_context),
    file: UploadFile = FastAPIFile(...),
):
    from istota.feeds.opml import import_opml

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    if len(raw) > 5 * 1024 * 1024:
        return JSONResponse({"error": "OPML too large (>5MB)"}, status_code=413)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "OPML must be UTF-8"}, status_code=400)

    try:
        result = await asyncio.to_thread(import_opml, ctx, text)
    except Exception as e:
        return JSONResponse({"error": f"OPML parse failed: {e}"}, status_code=400)

    return {
        "status": "ok",
        "feeds_added": result.feeds_added,
        "feeds_updated": result.feeds_updated,
        "categories_added": result.categories_added,
        "rewritten_bridger_urls": result.rewritten_bridger_urls,
    }


# ---------------------------------------------------------------------------
# Helpers — config validation + DB sync (parallel to cli._sync_config_to_db)
# ---------------------------------------------------------------------------


def _validate_feeds_config(cfg: dict) -> str | None:
    """Return an error string if ``cfg`` is malformed; ``None`` if OK."""
    if "feeds" in cfg and not isinstance(cfg["feeds"], list):
        return "feeds must be a list"
    if "categories" in cfg and not isinstance(cfg["categories"], list):
        return "categories must be a list"
    for f in cfg.get("feeds") or []:
        if not isinstance(f, dict):
            return "each feed must be an object"
        raw_url = str(f.get("url") or "").strip()
        if not raw_url:
            return "every feed needs a non-empty url"
        # A provider identifier that normalizes away — `arena:`, `arena:/` —
        # is refused here rather than skipped in the apply loop (ISSUE-432).
        # The save is wholesale-replace, so a skipped feed is also a *deleted*
        # feed: mistyping the URL of a subscription already on file would drop
        # it and its entries on a 200.
        if not normalize_feed_url(raw_url):
            return f"unusable feed url: {raw_url[:120]}"
    for c in cfg.get("categories") or []:
        if not isinstance(c, dict):
            return "each category must be an object"
        if not str(c.get("slug") or "").strip():
            return "every category needs a non-empty slug"
    raw_settings = cfg.get("settings")
    # Checked before the truthiness gate below, because a *falsy* non-dict —
    # `[]`, `0`, `""`, `false` — otherwise collapses to `{}` and skips
    # validation entirely, and `_apply_config_to_db` reads the payload the same
    # way and clears every stored setting on a 200. That silently turns an
    # `entry_retention_days` of 0 ("never prune") back into the 90-day default,
    # which is deletion switched on. `null` still means absent, as it did.
    if raw_settings is not None and not isinstance(raw_settings, dict):
        return "settings must be an object"
    settings = raw_settings or {}
    if settings:
        interval = settings.get("default_poll_interval_minutes")
        if interval is not None and not isinstance(interval, int):
            return "settings.default_poll_interval_minutes must be int"
        for key in (
            "image_dedupe_window_days",
            "entry_retention_days",
            "max_entries_per_feed",
        ):
            err = _non_negative_int(settings, key)
            if err:
                return err
    return None


# A thousand years of days, and far more entries than any feed holds. The
# ceiling is about what the code can express rather than what a user might
# want: above roughly 739,000 the age cutoff `now - timedelta(days=N)`
# overflows `datetime`, and a maximum at or above 2**63 cannot be bound as a
# SQLite integer. Either raises out of `prune_feeds` on *every* run, so the
# daily `_module.feeds.prune` job fails until it auto-disables after five
# consecutive failures — retention then stops for that user, permanently, with
# nothing on any surface saying why. Anyone wanting more than this means "no
# limit", which is what 0 already says.
MAX_RETENTION_SETTING = 365_000


def _non_negative_int(settings: dict, key: str) -> str | None:
    """Validate one optional non-negative integer setting.

    ``bool`` is an ``int`` subclass, so it is rejected explicitly: a stray
    ``true`` would otherwise be stored as ``1`` — a one-day retention window,
    or a one-entry-per-feed maximum, either of which deletes almost everything
    on the next prune. ``0`` is a real value on all three settings and is
    accepted.
    """
    value = settings.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"settings.{key} must be int"
    if value < 0:
        return f"settings.{key} must be >= 0"
    if value > MAX_RETENTION_SETTING:
        return f"settings.{key} must be <= {MAX_RETENTION_SETTING}"
    return None


def _optional_count(settings: dict, key: str) -> int | None:
    """Read one optional count. Absent or blank clears it; ``0`` is a value.

    Clearing the stored row *is* how a setting returns to its constant, so the
    blank and unparseable branches below must never be reached by a value the
    user meant as a number. They are not: `_validate_feeds_config` answers 400
    to a blank string and to every non-int before this runs, which leaves only
    absent and `null` arriving here from the route. The branches stay for a
    direct caller, which has no validator in front of it.
    """
    raw = settings.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _config_payload_from_db(
    cats,
    feeds,
    default_interval,
    image_dedupe_window=None,
    *,
    entry_retention_days=None,
    max_entries_per_feed=None,
) -> dict:
    """Project DB rows to the wire shape the settings page expects."""
    cat_by_id = {c.id: c for c in cats}
    settings: dict = {}
    if default_interval is not None:
        settings["default_poll_interval_minutes"] = default_interval
    if image_dedupe_window is not None:
        settings["image_dedupe_window_days"] = image_dedupe_window
    # Omitted when unset, so the page shows its placeholder default rather
    # than a number the user never chose.
    if entry_retention_days is not None:
        settings["entry_retention_days"] = entry_retention_days
    if max_entries_per_feed is not None:
        settings["max_entries_per_feed"] = max_entries_per_feed
    feed_payload: list[dict] = []
    for f in feeds:
        entry: dict = {"url": f.url}
        if f.title:
            entry["title"] = f.title
        if f.site_url:
            entry["site_url"] = f.site_url
        if f.category_id and f.category_id in cat_by_id:
            entry["category"] = cat_by_id[f.category_id].slug
        if f.poll_interval_minutes != default_poll_interval_for(f.source_type):
            entry["poll_interval_minutes"] = f.poll_interval_minutes
        feed_payload.append(entry)
    return {
        "settings": settings,
        "categories": [{"slug": c.slug, "title": c.title} for c in cats],
        "feeds": feed_payload,
    }


def _apply_config_to_db(ctx: FeedsContext, payload: dict) -> dict:
    """Replace the user's feeds + categories with ``payload``.

    Wholesale-replace semantics: feeds and categories that disappeared
    from the payload are removed from the DB. Without this, a feed
    deleted in the UI would keep showing in the sidebar because its row
    never went away.
    """
    cats_added = 0
    feeds_added = 0
    feeds_updated = 0
    feeds_removed = 0
    categories_removed = 0

    with feeds_db.connect(ctx.db_path) as conn:
        settings_payload = payload.get("settings") or {}
        # Read before anything is written: the comparison below is between the
        # maximum the feeds were last polled under and the one they will be
        # polled under next.
        old_max_entries = resolve_max_entries_per_feed(conn)

        slug_to_id: dict[str, int] = {}
        payload_slugs: set[str] = set()
        for c in payload.get("categories") or []:
            slug = str(c.get("slug") or "").strip()
            if not slug:
                continue
            raw_title = str(c.get("title") or "").strip()
            payload_slugs.add(slug)
            existing = feeds_db.get_category_by_slug(conn, slug)
            # This payload is the settings page's whole document, so it is
            # authoritative about the title where it carries the key at all —
            # including when the user cleared the field. `ensure_category` is
            # for the callers that only know a slug (the CLI's `--category`, an
            # OPML import with no title) and deliberately does not stomp a
            # title set elsewhere; taking that branch on a blank value made
            # clearing a title in the table a silent no-op, which read as the
            # save having been lost. A cleared title resets to the slug rather
            # than storing empty: the column is NOT NULL and the reader's
            # sidebar files a falsy title under "uncategorized".
            if raw_title:
                cat_id = feeds_db.upsert_category(conn, slug, raw_title)
            elif "title" in c:
                cat_id = feeds_db.upsert_category(conn, slug, slug)
            else:
                cat_id = feeds_db.ensure_category(conn, slug)
            slug_to_id[slug] = cat_id
            if existing is None:
                cats_added += 1

        explicit_default_raw = (payload.get("settings") or {}).get(
            "default_poll_interval_minutes"
        )
        try:
            explicit_default = (
                int(explicit_default_raw) if explicit_default_raw else None
            )
        except (TypeError, ValueError):
            explicit_default = None
        feeds_db.set_default_poll_interval(conn, explicit_default)

        # 0 is meaningful here ("never suppress"), so only an absent/blank
        # value clears the setting back to the default window.
        feeds_db.set_image_dedupe_window_days(
            conn, _optional_count(settings_payload, "image_dedupe_window_days"),
        )

        # Same rule for both retention settings: an absent key clears the
        # stored row back to the constant, and 0 is stored as "off".
        feeds_db.set_entry_retention_days(
            conn, _optional_count(settings_payload, "entry_retention_days"),
        )
        feeds_db.set_max_entries_per_feed(
            conn, _optional_count(settings_payload, "max_entries_per_feed"),
        )
        max_entries_changed = (
            resolve_max_entries_per_feed(conn) != old_max_entries
        )

        # The page renders each feed's *stored* URL and PUTs it back, so a row
        # added before ISSUE-432 arrives here spelled the old way. Canonicalize
        # it and this handler would upsert a second row and then delete the
        # original — with its entries, stars and read state, by cascade — on a
        # 200, and change the feed id every bookmark and `--id` refers to.
        # Writing to the spelling already on file is what keeps the row.
        variants = feeds_db.stored_url_variants(conn)

        payload_urls: set[str] = set()
        for f in payload.get("feeds") or []:
            # Canonical form before the existence check and before the delete
            # sweep below, so the two agree on what a feed's URL is
            # (ISSUE-432).
            url = normalize_feed_url(f.get("url"))
            if not url:
                continue
            url = variants.get(url, url)
            payload_urls.add(url)
            cat_slug = f.get("category")
            cat_id = slug_to_id.get(cat_slug) if cat_slug else None
            if cat_slug and cat_id is None:
                existing_cat = feeds_db.get_category_by_slug(conn, cat_slug)
                cat_id = feeds_db.ensure_category(conn, cat_slug)
                slug_to_id[cat_slug] = cat_id
                payload_slugs.add(cat_slug)
                if existing_cat is None:
                    cats_added += 1
            source_type = detect_source_type(url)
            per_feed = f.get("poll_interval_minutes")
            if per_feed:
                interval = int(per_feed)
            elif explicit_default is not None:
                interval = explicit_default
            else:
                interval = default_poll_interval_for(source_type)
            existing = feeds_db.get_feed_by_url(conn, url)
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=f.get("title"),
                site_url=f.get("site_url"),
                source_type=source_type,
                category_id=cat_id,
                poll_interval_minutes=interval,
            )
            if existing is None:
                feeds_added += 1
            else:
                feeds_updated += 1

        for feed in feeds_db.list_feeds(conn):
            if feed.url not in payload_urls:
                feeds_db.delete_feed(conn, feed.url)
                feeds_removed += 1

        for cat in feeds_db.list_categories(conn):
            if cat.slug not in payload_slugs:
                feeds_db.delete_category(conn, cat.slug)
                categories_removed += 1

        if max_entries_changed:
            # A new maximum only takes effect through a response carrying
            # items, and a conditional request answered 304 carries none — so
            # a raised maximum would sit unused until the feed happened to
            # publish. Clearing the validators with the schedule makes the
            # next poll fetch a full body once, which is what lets admission
            # fill the new budget. The age window needs no such reset: it
            # deletes on a stored clock and inserts nothing.
            #
            # The validators go on every feed, because clearing them costs a
            # throttled feed nothing: it only decides what the *next* request
            # asks for, whenever that turns out to be.
            #
            # The schedule does not. `next_poll_at` on a throttled or erroring
            # feed is a standoff that ISSUE-347 put there, and that issue's
            # stated invariant is that a 429 never schedules sooner than a
            # success would. A settings save is user-triggered and repeatable,
            # so clearing it there hands the user a way to stampede a host that
            # has just turned us away, one save at a time. Those feeds keep
            # their backoff and pick the new budget up at their own next poll,
            # which is late rather than wrong.
            #
            # `_migrate_v7_to_v8` clears the schedule unconditionally and is
            # right to: it runs once per upgrade, not once per click.
            conn.execute("UPDATE feeds SET etag = NULL, last_modified = NULL")
            conn.execute(
                "UPDATE feeds SET next_poll_at = NULL "
                "WHERE last_throttled_at IS NULL AND error_count = 0"
            )

        conn.commit()

    return {
        "categories_added": cats_added,
        "categories_removed": categories_removed,
        "feeds_added": feeds_added,
        "feeds_updated": feeds_updated,
        "feeds_removed": feeds_removed,
    }
