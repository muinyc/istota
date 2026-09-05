"""One-shot importer: legacy ``feeds.toml`` → per-user SQLite.

Runs idempotently on first touch from the web routes, the CLI, and the
skill facade (via :func:`ensure_initialised`). After one successful run
all subsequent calls are no-ops gated on a ``schema_meta`` sentinel.

The DB has been the runtime source of truth since the start; the TOML
was just the human-editable seed. This module exists to migrate users
who already have a populated TOML on disk before the cut. The source
file is left in place so an operator can confirm the import landed
(via the ``feeds_legacy_toml_imported`` log line) and delete it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tomllib
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    FeedsContext,
    default_poll_interval_for,
    detect_source_type,
    parse_image_urls,
)
from istota.feeds.sanitize import (
    dedupe_image_variants,
    html_to_text,
    remove_images,
)
from istota.timestamps import iso_now as _iso_now


logger = logging.getLogger(__name__)


_SENTINEL_KEY = "feeds_legacy_toml_imported_at"
_DEFAULTS_SENTINEL_KEY = "feeds_default_opml_seeded_at"
_BACKFILL_SENTINEL_KEY = "feeds_image_dedup_backfilled_at"
# Re-exported for tests; canonical home is db.py.
_DEFAULT_INTERVAL_SETTING_KEY = feeds_db._DEFAULT_INTERVAL_KEY

_BUNDLED_LABEL = "<bundled:istota.feeds:data/feeds-defaults.opml>"


def _read_bundled_defaults_opml() -> str | None:
    """Read the package-shipped ``feeds-defaults.opml`` as text.

    Uses ``importlib.resources`` so this works from a wheel install, an
    editable checkout, or a zipapp/PyInstaller bundle (where ``__file__``
    can point inside an archive). Returns ``None`` if the package was
    built without the data file.
    """
    try:
        resource = files("istota.feeds").joinpath("data/feeds-defaults.opml")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as concrete:
            if not concrete.is_file():
                return None
            return concrete.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def _legacy_toml_candidates(ctx: FeedsContext) -> list[Path]:
    """Ordered list of paths to probe for a legacy ``feeds.toml``.

    Mirrors the search the (now-removed) workspace loader used to do,
    plus an explicit ``ctx.config_path`` if the field is still present
    on the context (commit-1 transition window).
    """
    seen: set[Path] = set()
    out: list[Path] = []

    explicit = getattr(ctx, "config_path", None)
    if explicit is not None:
        out.append(Path(explicit))
        seen.add(Path(explicit))

    # Default workspace layout: data_dir = {workspace}/feeds.
    data_dir_candidate = Path(ctx.data_dir) / "config" / "feeds.toml"
    if data_dir_candidate not in seen:
        out.append(data_dir_candidate)
        seen.add(data_dir_candidate)

    # Workspace-root config dir (a user can colocate feeds.toml with USER.md
    # / CRON.md). data_dir.parent reconstructs the workspace root for the
    # default layout.
    workspace_candidate = Path(ctx.data_dir).parent / "config" / "feeds.toml"
    if workspace_candidate not in seen:
        out.append(workspace_candidate)

    return out


def _find_legacy_toml(ctx: FeedsContext) -> Path | None:
    for candidate in _legacy_toml_candidates(ctx):
        if candidate.is_file():
            return candidate
    return None


def _parse_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        parsed = tomllib.load(fh)
    parsed.setdefault("settings", {})
    parsed.setdefault("categories", [])
    parsed.setdefault("feeds", [])
    return parsed


def migrate_legacy_toml(ctx: FeedsContext) -> dict | None:
    """Import a legacy ``feeds.toml`` into the per-user feeds DB.

    Returns ``None`` when nothing was done — no toml found, migration
    already ran, or the DB already has feeds from another source. Returns
    a summary dict on a successful import.

    Idempotent and race-safe across processes:

    * A ``schema_meta`` sentinel row (``feeds_legacy_toml_imported_at``)
      records that the migration ran. Subsequent runs see the sentinel
      and bail.
    * The sentinel is inserted via plain ``INSERT`` (PK conflict aborts),
      so only one of N concurrent processes gets to run the upserts.
    * The DB-populated check is a defensive secondary gate so we don't
      stomp on rows added through the web UI / OPML import.

    On success the source ``feeds.toml`` is renamed to ``feeds.toml.imported``
    so an operator can confirm the import landed and `rm` it at leisure.
    """
    toml_path = _find_legacy_toml(ctx)

    feeds_db.init_db(ctx.db_path)

    # Pre-flight: has the migration already run, or does the DB have feeds
    # from another source? Both conditions short-circuit before we parse the
    # file.
    with feeds_db.connect(ctx.db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?",
            (_SENTINEL_KEY,),
        ).fetchone()
        if already:
            if toml_path is not None:
                logger.warning(
                    "feeds_legacy_toml_present_but_already_imported path=%s "
                    "— file is no longer read; delete it",
                    toml_path,
                )
            return None
        if toml_path is None:
            return None
        has_feeds = conn.execute(
            "SELECT 1 FROM feeds LIMIT 1",
        ).fetchone()
        if has_feeds:
            logger.warning(
                "feeds_legacy_toml_present_but_db_populated path=%s "
                "— DB already has subscriptions; delete or merge manually",
                toml_path,
            )
            return None

    try:
        parsed = _parse_toml(toml_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "feeds_legacy_toml_unparseable path=%s error=%s", toml_path, e,
        )
        return None

    cats_added = 0
    feeds_added = 0
    with feeds_db.connect(ctx.db_path) as conn:
        # Atomic claim — only one concurrent process gets past this insert.
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_SENTINEL_KEY, _iso_now()),
            )
        except sqlite3.IntegrityError:
            return None

        slug_to_id: dict[str, int] = {}
        for c in parsed.get("categories") or []:
            slug = str(c.get("slug") or "").strip()
            if not slug:
                continue
            raw_title = str(c.get("title") or "").strip()
            existing_cat = feeds_db.get_category_by_slug(conn, slug)
            if raw_title:
                # Caller provided a real title — upsert it.
                cat_id = feeds_db.upsert_category(conn, slug, raw_title)
            else:
                # No title in TOML; don't stomp an existing one.
                cat_id = feeds_db.ensure_category(conn, slug)
            slug_to_id[slug] = cat_id
            if existing_cat is None:
                cats_added += 1

        explicit_default_raw = parsed.get("settings", {}).get(
            "default_poll_interval_minutes"
        )
        explicit_default: int | None
        try:
            explicit_default = (
                int(explicit_default_raw) if explicit_default_raw else None
            )
        except (TypeError, ValueError):
            explicit_default = None

        for f in parsed.get("feeds") or []:
            url = str(f.get("url") or "").strip()
            if not url:
                continue
            cat_slug = f.get("category")
            cat_id = slug_to_id.get(cat_slug) if cat_slug else None
            if cat_slug and cat_id is None:
                existing_cat = feeds_db.get_category_by_slug(conn, cat_slug)
                cat_id = feeds_db.ensure_category(conn, cat_slug)
                slug_to_id[cat_slug] = cat_id
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
            existing_feed = feeds_db.get_feed_by_url(conn, url)
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=f.get("title"),
                site_url=f.get("site_url"),
                source_type=source_type,
                category_id=cat_id,
                poll_interval_minutes=interval,
            )
            if existing_feed is None:
                feeds_added += 1

        if explicit_default is not None:
            feeds_db.set_default_poll_interval(conn, explicit_default)

        conn.commit()

    logger.info(
        "feeds_legacy_toml_imported path=%s feeds=%d categories=%d "
        "— file no longer read; safe to delete",
        toml_path, feeds_added, cats_added,
    )
    return {
        "path": str(toml_path),
        "categories_added": cats_added,
        "feeds_added": feeds_added,
    }


def _default_opml_fs_candidates(ctx: FeedsContext) -> list[Path]:
    """Filesystem paths to probe for a ``feeds-defaults.opml`` override.

    Per-user override wins over workspace-level override. The bundled
    default (resolved via :func:`_read_bundled_defaults_opml`) is the
    final fallback and intentionally not included here.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    primary = Path(ctx.data_dir) / "config" / "feeds-defaults.opml"
    out.append(primary)
    seen.add(primary)

    # Workspace-level override. Prefer the explicit ``workspace_root`` on the
    # context; fall back to ``data_dir.parent`` only for the default layout
    # (``data_dir = {workspace}/feeds``) when ``workspace_root`` is unset.
    if ctx.workspace_root is not None:
        workspace = Path(ctx.workspace_root) / "config" / "feeds-defaults.opml"
    else:
        workspace = Path(ctx.data_dir).parent / "config" / "feeds-defaults.opml"
    if workspace not in seen:
        out.append(workspace)

    return out


def _resolve_default_opml(ctx: FeedsContext) -> tuple[str, str] | None:
    """Return ``(opml_text, source_label)`` for the highest-priority
    available defaults file, or ``None`` if none are available.

    File-system override paths are tried first (see
    :func:`_default_opml_fs_candidates`); on miss we fall back to the
    package-shipped bundled OPML. The label is the absolute path for FS
    sources and a stable sentinel string for the bundled resource.
    """
    for candidate in _default_opml_fs_candidates(ctx):
        if not candidate.is_file():
            continue
        try:
            return candidate.read_text(encoding="utf-8"), str(candidate)
        except OSError as e:
            logger.warning(
                "feeds_default_opml_unreadable path=%s error=%s", candidate, e,
            )
            return None
    bundled = _read_bundled_defaults_opml()
    if bundled is not None:
        return bundled, _BUNDLED_LABEL
    return None


def _try_write_defaults_sentinel(ctx: FeedsContext) -> bool:
    """Insert the ``feeds_default_opml_seeded_at`` row.

    Returns True on success, False on PK collision (another process or
    a previous run already claimed the slot). Independent connection so
    callers can decide when to commit.
    """
    with feeds_db.connect(ctx.db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_DEFAULTS_SENTINEL_KEY, _iso_now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
    return True


def seed_default_opml(ctx: FeedsContext) -> dict | None:
    """Seed the per-user feeds DB with example subscriptions.

    Runs at most once per user. Successful runs write a
    ``feeds_default_opml_seeded_at`` row in ``schema_meta``; the next
    call sees the row and bails. Skip / abort cases:

    * ``ISTOTA_FEEDS_SKIP_DEFAULT_SEED`` is set (ops opt-out / test
      suite). No sentinel is written — clearing the env var lets
      seeding run on the next call.
    * Sentinel already set — no-op.
    * DB already has feeds (legacy TOML, ``feeds add``, OPML import) —
      we record the sentinel so we don't probe the FS on every boot.
    * No defaults file found anywhere — return without writing the
      sentinel, so a later release shipping the OPML or an operator
      drop-in still triggers seeding on a subsequent boot.
    * Defaults file unreadable / malformed — return without writing the
      sentinel, so fixing the file unblocks seeding.

    Resolution order: ``{data_dir}/config/feeds-defaults.opml`` →
    ``{workspace_root}/config/feeds-defaults.opml`` → bundled
    ``istota.feeds:data/feeds-defaults.opml`` (resolved via
    ``importlib.resources``).

    Reset incantation for an operator who wants to re-seed:
    ``DELETE FROM schema_meta WHERE key='feeds_default_opml_seeded_at'``
    against the user's per-user feeds DB.

    Race-safety: two processes racing into an empty DB may both pass
    the ``has_feeds=False`` gate and both call :func:`import_opml`.
    That's fine — ``feeds_db.upsert_feed`` is keyed on URL so
    re-imports are no-ops, and only one of the two ``INSERT INTO
    schema_meta`` calls below wins; the other returns having done
    redundant-but-safe work.
    """
    if os.environ.get("ISTOTA_FEEDS_SKIP_DEFAULT_SEED"):
        return None

    feeds_db.init_db(ctx.db_path)

    with feeds_db.connect(ctx.db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?",
            (_DEFAULTS_SENTINEL_KEY,),
        ).fetchone()
        if already:
            return None
        has_feeds = conn.execute(
            "SELECT 1 FROM feeds LIMIT 1",
        ).fetchone()

    if has_feeds:
        # User got rows from another path (TOML migration, manual `feeds
        # add`, OPML import). Burn the sentinel so we don't re-probe the
        # FS forever; seeding is no longer applicable for this user.
        _try_write_defaults_sentinel(ctx)
        return None

    resolved = _resolve_default_opml(ctx)
    if resolved is None:
        # No FS override and no bundled file — nothing to do, but
        # intentionally don't write the sentinel: a later release that
        # ships the bundled OPML, or an operator drop-in, should still
        # be picked up on a subsequent call.
        logger.debug("feeds_default_opml_no_source ctx_user=%s", ctx.user_id)
        return None

    opml_text, source_label = resolved

    from istota.feeds.opml import import_opml  # noqa: PLC0415

    try:
        result = import_opml(ctx, opml_text)
    except Exception as e:  # noqa: BLE001
        # Don't write the sentinel — fixing the override file should
        # unblock seeding without operator surgery on schema_meta.
        logger.warning(
            "feeds_default_opml_import_failed source=%s error=%s",
            source_label, e,
        )
        return None

    # Import committed; claim the sentinel slot. Loser of a multi-
    # process race silently bails (their import was already a no-op
    # via upsert).
    _try_write_defaults_sentinel(ctx)

    logger.info(
        "feeds_default_opml_seeded source=%s feeds=%d categories=%d",
        source_label, result.feeds_added, result.categories_added,
    )
    return {
        "path": source_label,
        "feeds_added": result.feeds_added,
        "categories_added": result.categories_added,
    }


def backfill_image_dedup(ctx: FeedsContext) -> dict | None:
    """Strip already-promoted hero images out of stored ``content_html``.

    The RSS poller collapses resolution variants and drops the hero copy
    from an entry's body at ingest time (see :func:`poller._rss_entry_to_item`).
    But it writes rows with ``INSERT OR IGNORE`` keyed on ``(feed_id, guid)``,
    so an entry stored *before* that dedup landed is never rewritten: its
    ``content_html`` still embeds the comic/photo that's also in
    ``image_urls``, and the reader paints it twice (hero + body). xkcd is
    the clearest case — the whole entry body is that one ``<img>``.

    This one-shot backfill re-runs the body-side dedup over every stored
    entry: for each row it re-collapses ``image_urls`` variants and removes
    those images from ``content_html`` (by :func:`sanitize.image_identity`,
    so a differently-sized body copy still matches). Genuine mid-article
    inline images — anything not promoted to the hero set — are left alone.
    ``content_text`` is recomputed from the rewritten HTML so search stays
    consistent.

    Idempotent and race-safe, mirroring :func:`migrate_legacy_toml`: a
    ``schema_meta`` sentinel (``feeds_image_dedup_backfilled_at``) is claimed
    via a plain ``INSERT`` (PK conflict → one winner), then the rewrites run
    in the same transaction. Returns ``None`` when the sentinel already
    exists; otherwise a summary dict with the updated-row count (which may be
    zero — the sentinel is still burned so we don't re-scan every boot).

    Reset incantation for an operator who wants to re-run:
    ``DELETE FROM schema_meta WHERE key='feeds_image_dedup_backfilled_at'``.
    """
    feeds_db.init_db(ctx.db_path)

    with feeds_db.connect(ctx.db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?",
            (_BACKFILL_SENTINEL_KEY,),
        ).fetchone()
        if already:
            return None

        # Atomic claim — only one concurrent process gets past this insert.
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_BACKFILL_SENTINEL_KEY, _iso_now()),
            )
        except sqlite3.IntegrityError:
            return None

        updated = 0
        rows = conn.execute(
            "SELECT id, content_html, image_urls FROM feed_entries "
            "WHERE content_html IS NOT NULL AND content_html != ''"
        ).fetchall()
        for row in rows:
            images = parse_image_urls(row["image_urls"])
            if not images:
                continue
            # Re-collapse resolution variants first so a body copy stored at a
            # different width than the hero still matches by identity.
            images = dedupe_image_variants(images)
            new_html = remove_images(row["content_html"], images)
            if new_html == row["content_html"]:
                continue
            new_text = html_to_text(new_html)
            conn.execute(
                "UPDATE feed_entries SET content_html = ?, content_text = ? "
                "WHERE id = ?",
                (new_html, new_text, row["id"]),
            )
            updated += 1

        conn.commit()

    logger.info("feeds_image_dedup_backfilled entries_updated=%d", updated)
    return {"entries_updated": updated}


def ensure_initialised(ctx: FeedsContext) -> None:
    """Wire up a feeds workspace for use.

    Creates the dirs, runs schema migrations on the SQLite, imports any
    legacy ``feeds.toml`` (once), seeds default subscriptions from OPML
    (once), then backfills the image dedup over any pre-existing entries
    (once). Safe to call from every entry point — web routes, CLI
    subcommands, skill facade.
    """
    ctx.ensure_dirs()
    feeds_db.init_db(ctx.db_path)
    migrate_legacy_toml(ctx)
    seed_default_opml(ctx)
    backfill_image_dedup(ctx)
