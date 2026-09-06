"""Click CLI for the native feeds module.

Operates against a single resolved :class:`FeedsContext`. The skill facade
builds the context up front (via :func:`istota.feeds.resolve_for_user`) and
injects it through ``CliRunner.invoke(obj=...)``; standalone invocation
falls back to ``synthesize_feeds_context`` rooted at ``$FEEDS_WORKSPACE``
or the current working directory.

Output is JSON on stdout, with ``status: ok | error``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from istota.feeds import db as feeds_db
from istota.feeds._migrate import ensure_initialised
from istota.feeds.models import (
    default_poll_interval_for,
    FeedsContext,
    detect_source_type,
    normalize_feed_url,
)
from istota.feeds.workspace import synthesize_feeds_context


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _output(result) -> None:
    click.echo(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if isinstance(result, dict) and result.get("status") == "error":
        sys.exit(1)


def _ok(**kwargs) -> dict:
    return {"status": "ok", **kwargs}


def _err(message: str, **kwargs) -> dict:
    return {"status": "error", "error": message, **kwargs}


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


pass_ctx = click.make_pass_decorator(FeedsContext, ensure=False)


def _resolve_default_context(user_id: str | None) -> FeedsContext:
    """Build a FeedsContext for standalone CLI use (no skill-side injection)."""
    user_id = user_id or os.environ.get("FEEDS_USER", "") or "default"
    workspace = Path(os.environ.get("FEEDS_WORKSPACE", "")) or Path.cwd()
    if not workspace.is_absolute():
        workspace = workspace.resolve()
    tumblr_key = os.environ.get("TUMBLR_API_KEY", "")
    return synthesize_feeds_context(
        user_id, workspace, tumblr_api_key=tumblr_key,
    )


@click.group()
@click.option("--user", "-u", "user_key", help="User key (defaults to $FEEDS_USER)")
@click.pass_context
def cli(ctx: click.Context, user_key: str | None) -> None:
    """Native feeds — subscriptions, polling, OPML."""
    if isinstance(ctx.obj, FeedsContext):
        ensure_initialised(ctx.obj)
        return
    fctx = _resolve_default_context(user_key)
    ctx.obj = fctx
    ensure_initialised(fctx)


# ---------------------------------------------------------------------------
# Interval resolution
# ---------------------------------------------------------------------------


def _resolve_interval(
    conn, source_type: str, override: int | None,
) -> int:
    """Per-feed override > user-set default > per-source-type default."""
    if override is not None:
        return int(override)
    user_default = feeds_db.get_default_poll_interval(conn)
    if user_default is not None:
        return user_default
    return default_poll_interval_for(source_type)


# ---------------------------------------------------------------------------
# list / categories / entries
# ---------------------------------------------------------------------------


@cli.command("list")
@pass_ctx
def cmd_list(ctx: FeedsContext) -> None:
    """List subscribed feeds."""
    with feeds_db.connect(ctx.db_path) as conn:
        cats = {c.id: c for c in feeds_db.list_categories(conn)}
        feeds = feeds_db.list_feeds(conn)
    rows = [
        {
            "id": f.id,
            "url": f.url,
            "title": f.title,
            "site_url": f.site_url,
            "source_type": f.source_type,
            "category": cats[f.category_id].title if f.category_id and f.category_id in cats else None,
            "category_slug": cats[f.category_id].slug if f.category_id and f.category_id in cats else None,
            "poll_interval_minutes": f.poll_interval_minutes,
            "last_fetched_at": f.last_fetched_at,
            "last_error": f.last_error,
            "error_count": f.error_count,
            "next_poll_at": f.next_poll_at,
        }
        for f in feeds
    ]
    _output({"status": "ok", "count": len(rows), "feeds": rows})


@cli.command("categories")
@pass_ctx
def cmd_categories(ctx: FeedsContext) -> None:
    """List categories."""
    with feeds_db.connect(ctx.db_path) as conn:
        cats = feeds_db.list_categories(conn)
    rows = [{"id": c.id, "slug": c.slug, "title": c.title} for c in cats]
    _output({"status": "ok", "count": len(rows), "categories": rows})


@cli.command("entries")
@click.option("--status", type=click.Choice(["unread", "read", "removed"]), help="Filter by status")
@click.option("--feed-id", type=int, help="Filter by feed id")
@click.option("--category-id", type=int, help="Filter by category id")
@click.option("--category", help="Filter by category slug")
@click.option("--limit", type=int, default=25, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--before", type=int, help="Only entries with published_at < this Unix ts")
@click.option("--order", type=click.Choice(["published_at", "created_at", "id"]), default="published_at", show_default=True)
@click.option("--direction", type=click.Choice(["asc", "desc"]), default="desc", show_default=True)
@pass_ctx
def cmd_entries(
    ctx: FeedsContext, status, feed_id, category_id, category, limit, offset,
    before, order, direction,
) -> None:
    """List entries with the same filter knobs as the HTTP API."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        if category and category_id is None:
            cat = feeds_db.get_category_by_slug(conn, category)
            if cat is not None:
                category_id = cat.id
        entries = feeds_db.list_entries(
            conn,
            limit=limit,
            offset=offset,
            status=status,
            feed_id=feed_id,
            category_id=category_id,
            before_published_ts=before,
            order=order,
            direction=direction,
        )
        total = feeds_db.count_entries(
            conn, status=status, feed_id=feed_id, category_id=category_id,
        )

    rows = [
        {
            "id": e.id,
            "feed_id": e.feed_id,
            "guid": e.guid,
            "title": e.title,
            "url": e.url,
            "author": e.author,
            "image_urls": e.image_urls,
            # An mp4 attachment used to appear here as an image_url. Naming it
            # for what it is keeps the model's view of an entry honest, and it
            # is the only place a video shows at all now (ISSUE-356).
            "media_url": e.media_url,
            "media_type": e.media_type,
            "published_at": e.published_at,
            "fetched_at": e.fetched_at,
            "status": e.status,
            "starred": e.starred,
            "starred_at": e.starred_at,
        }
        for e in entries
    ]
    _output({"status": "ok", "total": total, "count": len(rows), "entries": rows})


# ---------------------------------------------------------------------------
# starring + bulk mark-read
# ---------------------------------------------------------------------------


def _parse_id_list(raw: str) -> list[int]:
    """Turn ``"1,2,3"`` into ``[1, 2, 3]``. Raises on non-integer tokens."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [int(p) for p in parts]


@cli.command("star")
@click.option("--id", "entry_id", type=int, help="Single entry id to star")
@click.option("--ids", help="Comma-separated entry ids (alternative to --id)")
@click.option("--unstar", is_flag=True, help="Unstar instead of star")
@pass_ctx
def cmd_star(ctx: FeedsContext, entry_id, ids, unstar) -> None:
    """Toggle the star flag on one or more entries."""
    feeds_db.init_db(ctx.db_path)
    entry_ids: list[int] = []
    if entry_id:
        entry_ids.append(entry_id)
    if ids:
        try:
            entry_ids.extend(_parse_id_list(ids))
        except ValueError:
            _output(_err("--ids must be a comma-separated list of integers"))
            return
    if not entry_ids:
        _output(_err("specify --id or --ids"))
        return

    with feeds_db.connect(ctx.db_path) as conn:
        n = feeds_db.update_entry_starred(conn, entry_ids, not unstar)
        conn.commit()
    _output(_ok(updated=n, starred=not unstar, ids=entry_ids))


@cli.command("starred")
@click.option("--limit", type=int, default=25, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--feed-id", type=int, help="Filter by feed id")
@click.option("--category-id", type=int, help="Filter by category id")
@click.option("--category", help="Filter by category slug")
@click.option(
    "--order",
    type=click.Choice(["starred_at", "published_at", "created_at", "id"]),
    default="starred_at", show_default=True,
)
@click.option("--direction", type=click.Choice(["asc", "desc"]), default="desc",
              show_default=True)
@pass_ctx
def cmd_starred(
    ctx: FeedsContext, limit, offset, feed_id, category_id, category,
    order, direction,
) -> None:
    """List starred entries (independent of read/unread/removed status)."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        if category and category_id is None:
            cat = feeds_db.get_category_by_slug(conn, category)
            if cat is not None:
                category_id = cat.id
        entries = feeds_db.list_entries(
            conn,
            limit=limit, offset=offset,
            feed_id=feed_id, category_id=category_id,
            starred=True, order=order, direction=direction,
        )
        total = feeds_db.count_entries(
            conn, feed_id=feed_id, category_id=category_id, starred=True,
        )
    rows = [
        {
            "id": e.id,
            "feed_id": e.feed_id,
            "title": e.title,
            "url": e.url,
            "published_at": e.published_at,
            "starred_at": e.starred_at,
            "status": e.status,
        }
        for e in entries
    ]
    _output({"status": "ok", "total": total, "count": len(rows), "entries": rows})


@cli.command("mark-read")
@click.option("--all", "mark_all", is_flag=True, help="Mark every unread entry read")
@click.option("--feed", "feed_id", type=int, help="Limit to one feed id")
@click.option("--category", "category_slug", help="Limit to a category slug")
@click.option("--category-id", type=int, help="Limit to a category id")
@click.option("--before-id", type=int,
              help="Cap to entries with id <= before-id (stable mark-visible)")
@pass_ctx
def cmd_mark_read(
    ctx: FeedsContext, mark_all, feed_id, category_slug, category_id, before_id,
) -> None:
    """Bulk mark unread entries as read by scope."""
    feeds_db.init_db(ctx.db_path)
    selected = sum(1 for x in (mark_all, feed_id, category_slug, category_id) if x)
    if selected == 0:
        _output(_err("specify --all, --feed, --category, or --category-id"))
        return
    if selected > 1:
        _output(_err("--all / --feed / --category / --category-id are mutually exclusive"))
        return

    with feeds_db.connect(ctx.db_path) as conn:
        if mark_all:
            scope = "all"
            scope_id = None
        elif feed_id:
            scope = "feed"
            scope_id = feed_id
        else:
            scope = "category"
            if category_id:
                scope_id = category_id
            else:
                cat = feeds_db.get_category_by_slug(conn, category_slug)
                if cat is None:
                    _output(_err(f"unknown category slug: {category_slug}"))
                    return
                scope_id = cat.id
        n = feeds_db.mark_as_read(
            conn, scope=scope, scope_id=scope_id, before_id=before_id,
        )
        conn.commit()
    _output(_ok(updated=n, scope=scope, scope_id=scope_id, before_id=before_id))


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------


@cli.command("add")
@click.option("--url", required=True, help="Feed URL or tumblr:/arena: identifier")
@click.option("--title", help="Display title")
@click.option("--category", help="Category slug (creates if missing)")
@click.option("--poll-interval-minutes", type=int, help="Override per-feed poll interval")
@pass_ctx
def cmd_add(ctx: FeedsContext, url, title, category, poll_interval_minutes) -> None:
    """Subscribe to a feed."""
    # Canonicalize before anything looks at it, including the existence check:
    # `arena:/x` and `arena:x` are one channel, and storing both is two rows
    # polling the same API path (ISSUE-432).
    normalized = normalize_feed_url(url)
    if not normalized:
        _output(_err(f"unusable feed url: {url!r}"))
        return
    url = normalized

    with feeds_db.connect(ctx.db_path) as conn:
        # A row added before ISSUE-432 is stored under whatever was typed, so
        # the canonical form misses it and the insert below would make a second
        # subscription to the same channel — both of which now fetch, since
        # `provider_identifier` normalizes on the way to the request.
        url = feeds_db.stored_url_variants(conn).get(url, url)
        existing = feeds_db.get_feed_by_url(conn, url)
        if existing is not None:
            _output(_err(f"feed already exists: {url}"))
            return

        cat_id = feeds_db.ensure_category(conn, category) if category else None
        source_type = detect_source_type(url)
        interval = _resolve_interval(conn, source_type, poll_interval_minutes)
        feeds_db.upsert_feed(
            conn,
            url=url,
            title=title,
            site_url=None,
            source_type=source_type,
            category_id=cat_id,
            poll_interval_minutes=interval,
        )
        conn.commit()

    feed_payload: dict = {"url": url}
    if title:
        feed_payload["title"] = title
    if category:
        feed_payload["category"] = category
    if poll_interval_minutes is not None:
        feed_payload["poll_interval_minutes"] = poll_interval_minutes
    _output(_ok(feed=feed_payload))


@cli.command("remove")
@click.option("--url", help="Feed URL to unsubscribe")
@click.option("--id", "feed_id", type=int, help="DB feed id")
@pass_ctx
def cmd_remove(ctx: FeedsContext, url, feed_id) -> None:
    """Unsubscribe by URL or DB id."""
    if not url and not feed_id:
        _output(_err("specify --url or --id"))
        return

    with feeds_db.connect(ctx.db_path) as conn:
        if feed_id and not url:
            row = conn.execute(
                "SELECT url FROM feeds WHERE id = ?", (feed_id,),
            ).fetchone()
            if row is None:
                _output(_err(f"no feed with id {feed_id}"))
                return
            url = row["url"]

        if feeds_db.get_feed_by_url(conn, url) is None:
            # Three spellings can name one feed and only one of them is stored.
            # A row added since ISSUE-432 holds the canonical form, so the
            # string that was typed to add it no longer matches; a row added
            # before it holds whatever was typed, so the canonical form does
            # not match either. Try the raw string first, so a legacy row stays
            # removable by exactly what `feeds list` shows, then the canonical
            # form, then the stored spelling that canonical form belongs to.
            canonical = normalize_feed_url(url)
            resolved = None
            if canonical and canonical != url:
                if feeds_db.get_feed_by_url(conn, canonical) is not None:
                    resolved = canonical
            if resolved is None and canonical:
                resolved = feeds_db.stored_url_variants(conn).get(canonical)
            if resolved is None:
                _output(_err(f"no feed with url {url}"))
                return
            url = resolved

        feeds_db.delete_feed(conn, url)
        conn.commit()

    _output(_ok(removed_url=url))


# ---------------------------------------------------------------------------
# refresh / poll / run-scheduled
# ---------------------------------------------------------------------------


@cli.command("refresh")
@click.option("--id", "feed_id", type=int, help="Feed id (omit to mark all due)")
@pass_ctx
def cmd_refresh(ctx: FeedsContext, feed_id) -> None:
    """Mark feeds due for the next poll cycle by clearing ``next_poll_at``.

    The poll claim goes with it. A poll killed mid-run — a skill-task timeout,
    a container restart — leaves a lease behind, and `feeds_due_for_poll`
    excludes a feed under one, so clearing only `next_poll_at` would report
    the feed made due while it stayed unpollable for up to five minutes. This
    is the one command whose whole purpose is "poll this now", so it is also
    the manual release for a lease whose process is gone.
    """
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        if feed_id:
            cur = conn.execute(
                "UPDATE feeds SET next_poll_at = NULL, poll_claimed_until = NULL "
                "WHERE id = ?",
                (feed_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE feeds SET next_poll_at = NULL, poll_claimed_until = NULL"
            )
        conn.commit()
    _output(_ok(reset_count=cur.rowcount))


def _poll_due(ctx: FeedsContext, limit) -> None:
    from istota.feeds.poller import poll_due_feeds

    api_key = ctx.tumblr_api_key or os.environ.get("TUMBLR_API_KEY", "")

    with feeds_db.connect(ctx.db_path) as conn:
        outcomes = poll_due_feeds(
            conn, tumblr_api_key=api_key, limit=limit,
            now=datetime.now(timezone.utc),
        )

    summary = []
    new_total = 0
    error_total = 0
    throttled_total = 0
    for feed, result, new_count in outcomes:
        new_total += new_count
        if result.error:
            error_total += 1
        # Counted separately from errors, and reported: a 429 is not a feed
        # failure, but a run that was turned away everywhere is not a clean run
        # either. Without this it was byte-identical to a successful poll that
        # found nothing (ISSUE-347).
        if result.rate_limited:
            throttled_total += 1
        summary.append({
            "feed_id": feed.id,
            "url": feed.url,
            "new_entries": new_count,
            "not_modified": result.not_modified,
            "error": result.error,
            "rate_limited": result.rate_limited,
            "retry_after_seconds": result.retry_after_seconds,
        })

    # Roll up per-source errors to the outer envelope so the scheduler's
    # JSON-error detector and any alerting layer can see them. All-errors →
    # hard failure (likely network outage or config breakage); some errors →
    # partial_error, surfaced in logs but not treated as a task failure.
    polled = len(outcomes)
    if polled and error_total == polled:
        _output(_err(
            f"all {polled} feed poll(s) failed",
            polled=polled,
            new_entries=new_total,
            errors=error_total,
            throttled=throttled_total,
            feeds=summary,
        ))
        return
    # A wholly throttled run is a hard failure too. It is not a *feed* failure,
    # so it does not go through `error_total`, but reporting it as a success
    # that found nothing is the silence this counter exists to break.
    if polled and throttled_total == polled:
        _output(_err(
            f"all {polled} feed poll(s) were rate-limited",
            polled=polled,
            new_entries=new_total,
            errors=error_total,
            throttled=throttled_total,
            feeds=summary,
        ))
        return
    payload = _ok(
        polled=polled,
        new_entries=new_total,
        errors=error_total,
        throttled=throttled_total,
        feeds=summary,
    )
    if error_total:
        payload["status"] = "partial_error"
    _output(payload)


@cli.command("poll")
@click.option("--limit", type=int, help="Cap how many feeds to poll this run")
@pass_ctx
def cmd_poll(ctx: FeedsContext, limit) -> None:
    """Poll every feed whose ``next_poll_at`` is in the past."""
    _poll_due(ctx, limit)


@cli.command("run-scheduled")
@click.option("--limit", type=int, help="Cap how many feeds to poll this run")
@pass_ctx
def cmd_run_scheduled(ctx: FeedsContext, limit) -> None:
    """Periodic entry point used by the scheduler module-job.

    Unlike ``poll``, this one caps its burst by default. It runs unattended
    every five minutes, and the two paths that make every feed due at once —
    "Refresh now" in the web UI and an OPML import — both hand it the whole
    subscription list. The due list is ordered oldest-first, so a backlog
    larger than the cap drains over consecutive ticks rather than being
    dropped. ``--limit`` still overrides, in both directions.
    """
    from istota.feeds.models import DEFAULT_SCHEDULED_POLL_LIMIT

    _poll_due(ctx, DEFAULT_SCHEDULED_POLL_LIMIT if limit is None else limit)


@cli.command("prune")
@click.option("--dry-run", is_flag=True, help="Report what would go; delete nothing")
@pass_ctx
def cmd_prune(ctx: FeedsContext, dry_run) -> None:
    """Apply the retention policy: age window, per-feed maximum, image cascade."""
    from dataclasses import asdict

    from istota.feeds import retention

    try:
        result = retention.prune_feeds(ctx, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — the envelope is the contract
        _output(_err(str(exc)))
        return
    _output(_ok(**asdict(result)))


# ---------------------------------------------------------------------------
# OPML
# ---------------------------------------------------------------------------


@cli.command("import-opml")
@click.argument("opml_path", type=click.Path(exists=True, dir_okay=False))
@pass_ctx
def cmd_import_opml(ctx: FeedsContext, opml_path) -> None:
    """Import subscriptions from an OPML file."""
    from istota.feeds.opml import import_opml

    text = Path(opml_path).read_text()
    result = import_opml(ctx, text)
    _output(_ok(
        feeds_added=result.feeds_added,
        feeds_updated=result.feeds_updated,
        feeds_skipped=result.feeds_skipped,
        categories_added=result.categories_added,
        rewritten_bridger_urls=result.rewritten_bridger_urls,
    ))


@cli.command("export-opml")
@click.option("--output", "-o", type=click.Path(dir_okay=False), help="Write to file (default: stdout)")
@pass_ctx
def cmd_export_opml(ctx: FeedsContext, output) -> None:
    """Export subscriptions as OPML 2.0."""
    from istota.feeds.opml import export_opml

    text = export_opml(ctx)
    if output:
        Path(output).write_text(text)
        _output(_ok(path=str(output), bytes=len(text)))
    else:
        # Raw OPML on stdout — caller asked for it; bypass JSON wrapping.
        click.echo(text)


if __name__ == "__main__":
    cli()
