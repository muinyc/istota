"""SQLite layer for the native feeds module.

One DB per user, lives at ``{ctx.db_path}``. Schema lives inline; ``init_db``
is idempotent and walks ``_MIGRATIONS`` to bring an existing DB up to
``SCHEMA_VERSION`` one step at a time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from istota.feeds.image_dedupe import entry_seen_ts
from istota.feeds.models import (
    DEFAULT_ENTRY_RETENTION_DAYS,
    DEFAULT_MAX_ENTRIES_PER_FEED,
    MIN_ENTRIES_PER_FEED,
    POLL_CLAIM_SECONDS,
    UPGRADE_GRACE_DAYS,
    CategoryRecord,
    EntryRecord,
    FeedRecord,
    is_http_url,
    media_type_for_url,
    normalize_feed_url,
    parse_image_urls,
)
from istota.feeds.sanitize import image_identity
from istota.timestamps import iso_now


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 8


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feed_categories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    site_url TEXT,
    category_id INTEGER REFERENCES feed_categories(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_throttled_at TEXT,
    -- Poll time of the most recent response that returned at least one item
    -- (ISSUE-388). An entry stamped with exactly this value was in that
    -- response and is never age-deleted; an older stamp means it was not.
    -- NULL means no response has ever returned an item here, so nothing about
    -- the feed's entries is deletable. Deliberately says nothing about whether
    -- the response was complete, well-formed or a full page.
    last_items_seen_at TEXT,
    -- Lease held by the process currently fetching this feed. Bounded, so a
    -- process that dies mid-fetch delays the feed by the lease rather than
    -- stranding it.
    poll_claimed_until TEXT,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
    next_poll_at TEXT
);

CREATE TABLE IF NOT EXISTS feed_entries (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT,
    url TEXT,
    author TEXT,
    content_html TEXT,
    content_text TEXT,
    image_urls TEXT,
    -- Canonical page for playable media (a YouTube / Vimeo watch URL). The
    -- reader builds its own player from this; we never store a provider's
    -- <iframe>, which would force an iframe allowance in the sanitizer that
    -- every RSS feed also passes through.
    embed_url TEXT,
    -- A downloadable document the entry is about (an Are.na Attachment,
    -- nearly always a PDF). Distinct from embed_url: this one is opened
    -- rather than played, and its presence is what stops the reader
    -- treating a PDF's cover page as an ordinary gallery image.
    file_url TEXT,
    -- A media file played inline with a native <video>/<audio> — a Mastodon
    -- attachment, a podcast enclosure. Neither of the two above: embed_url
    -- is a provider page we rebuild a player for, file_url is opened
    -- elsewhere. Before ISSUE-356 a direct media URL had nowhere to go and
    -- was stored in image_urls, which the reader painted as a broken <img>.
    media_url TEXT,
    media_type TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread',
    starred INTEGER NOT NULL DEFAULT 0,
    starred_at TEXT,
    -- The most recent poll time at which this entry was itself observed in a
    -- response (ISSUE-388). Distinct from `fetched_at`, which is the first
    -- sighting, never moves and is the retention clock: this column answers
    -- the churn question instead — was the entry in the feed's most recent
    -- response, in which case it is never age-deleted.
    last_seen_at TEXT,
    UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_entries_feed_status
    ON feed_entries(feed_id, status);
CREATE INDEX IF NOT EXISTS idx_entries_published
    ON feed_entries(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_starred
    ON feed_entries(starred) WHERE starred = 1;
-- Partial on `starred = 0` because every retention pass excludes stars before
-- it does anything else, so the index holds only rows a prune can reach.
CREATE INDEX IF NOT EXISTS idx_entries_feed_last_seen_unstarred
    ON feed_entries(feed_id, last_seen_at)
    WHERE starred = 0;
-- `fetched_at` is the retention clock, and both passes rank on it. Partial
-- like its neighbour, so the age pass — which ranks a feed's whole contents,
-- stars included, because the floor counts every stored row — cannot use it
-- at all. The count pass, which looks at unstarred rows only, scans it as a
-- covering index and takes the partition from it, but still sorts: the index
-- is `(feed_id, fetched_at)` ascending and the window wants `fetched_at DESC,
-- id ASC`, so `EXPLAIN QUERY PLAN` reports a temp b-tree for the last two
-- terms. A `(feed_id, fetched_at DESC, id)` index would remove that sort and
-- is deliberately not added here: schema v8's index set is what the migration
-- shipped, and a third one is its own change.
CREATE INDEX IF NOT EXISTS idx_entries_feed_fetched_unstarred
    ON feed_entries(feed_id, fetched_at)
    WHERE starred = 0;

-- Normalised image keys per entry, for the reader's cross-entry image
-- suppression (ISSUE-162). Derived data: rebuildable from feed_entries at
-- any time, and deliberately a side table so entry rows stay untouched.
-- ``seen_ts`` is epoch seconds (published date, else fetch time) so the
-- look-back window is an exact integer range scan rather than string
-- comparison over mixed date formats.
CREATE TABLE IF NOT EXISTS entry_images (
    entry_id INTEGER NOT NULL REFERENCES feed_entries(id) ON DELETE CASCADE,
    image_key TEXT NOT NULL,
    seen_ts INTEGER NOT NULL,
    PRIMARY KEY (entry_id, image_key)
);

CREATE INDEX IF NOT EXISTS idx_entry_images_key
    ON entry_images(image_key, seen_ts DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``starred`` / ``starred_at`` columns + partial index.

    Guarded by ``PRAGMA table_info`` so re-running on a fresh DB (where the
    columns are already present from ``SCHEMA_SQL``) is a no-op.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "starred" not in cols:
        conn.execute(
            "ALTER TABLE feed_entries ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"
        )
    if "starred_at" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN starred_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_starred "
        "ON feed_entries(starred) WHERE starred = 1"
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add ``entry_images`` and backfill it from existing entries.

    One pass over every entry that stored images. Cheap enough to run inline
    (a heavy image reader holds tens of thousands of image instances), and the
    table is pure derived data, so a partial backfill would only weaken
    suppression, never corrupt anything.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_images (
            entry_id INTEGER NOT NULL REFERENCES feed_entries(id) ON DELETE CASCADE,
            image_key TEXT NOT NULL,
            seen_ts INTEGER NOT NULL,
            PRIMARY KEY (entry_id, image_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entry_images_key "
        "ON entry_images(image_key, seen_ts DESC)"
    )
    rows = conn.execute(
        "SELECT id, image_urls, published_at, fetched_at FROM feed_entries "
        "WHERE image_urls IS NOT NULL AND image_urls != ''"
    ).fetchall()
    indexed = 0
    for row in rows:
        indexed += _index_entry_images(
            conn,
            entry_id=row["id"],
            image_urls=parse_image_urls(row["image_urls"]),
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
        )
    if indexed:
        logger.info("feeds_db_image_index_backfilled keys=%s", indexed)


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add ``feed_entries.embed_url``.

    Purely additive — existing entries keep NULL, which reads as "no playable
    media" and renders exactly as before. Guarded by ``PRAGMA table_info`` so
    re-running on a DB already carrying the column (created from
    ``SCHEMA_SQL``) is a no-op.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "embed_url" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN embed_url TEXT")


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add ``feed_entries.file_url``.

    Additive, same shape as the v4 migration: existing entries keep NULL,
    which reads as "no attached document" and renders exactly as before.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "file_url" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN file_url TEXT")


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add ``feeds.last_throttled_at`` (ISSUE-347).

    Additive, same shape as the two above. A 429 stopped being recorded as a
    feed error — a throttled channel is healthy — but that left it recorded
    nowhere at all, so a run that was turned away for every feed reported a
    clean poll that happened to find nothing. This column is where a throttle
    is visible now that ``last_error`` no longer carries it. Existing rows keep
    NULL, which reads as "never throttled".
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
    if "last_throttled_at" not in cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN last_throttled_at TEXT")


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add the ``feed_entries`` media columns and lift stored videos into
    them (ISSUE-356).

    Additive like the three above, but with a data pass, because additive
    alone would fix nothing an existing reader can see. The poller filed
    every ``media:content`` URL as an image, so a Mastodon video attachment
    is sitting in ``image_urls`` right now and the reader paints it into an
    ``<img src>`` that never decodes. Re-polling does not clear it:
    ``insert_entries`` matches on ``(feed_id, guid)`` and its refresh path
    holds ``image_urls`` with ``COALESCE(?, image_urls)``, so a now-empty
    image list leaves the broken URL exactly where it is.

    The stored MIME type is gone by now — it was never written anywhere — so
    the extension is the only evidence, which is precisely what
    ``media_type_for_url`` is for. A URL is only promoted if it also passes
    ``is_http_url``: these rows were written before anything checked a scheme,
    and ``media_type_for_url`` reads the path, so ``javascript:x.mp4`` parses
    as a video. The poller refuses that on the way in and nothing downstream
    re-checks it, so the migration has to apply the same bar rather than
    inherit a value from a laxer era. One that fails stays an image, where it
    was already being rendered harmlessly by an ``<img>`` that never loaded.

    An entry whose images are untouched is left alone entirely, and
    ``entry_images`` is rebuilt for the ones that changed: it is derived from
    ``image_urls``, and a lifted video that stayed in the index would go on
    suppressing later posts through the image dedupe. The other side of that
    is deliberate and worth stating — a clip is now outside the repeat-image
    suppression entirely, so a boost wave of one video renders a player per
    entry where it used to render a broken ``<img>`` per entry minus the
    suppressed ones. Suppression exists to stop the same *picture* filling a
    screen; a player the reader has to press is not that.

    Only the first playable URL becomes the attachment, matching the poller.
    Any beyond it leave ``image_urls`` and are stored nowhere, so they are
    counted and logged rather than dropped in silence.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "media_url" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN media_url TEXT")
    if "media_type" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN media_type TEXT")

    rows = conn.execute(
        "SELECT id, image_urls, media_url, published_at, fetched_at "
        "FROM feed_entries WHERE image_urls IS NOT NULL AND image_urls != ''"
    ).fetchall()
    lifted = 0
    discarded = 0
    for row in rows:
        urls = parse_image_urls(row["image_urls"])
        kept: list[str] = []
        media: list[tuple[str, str]] = []
        for url in urls:
            mime = media_type_for_url(url) if is_http_url(url) else None
            if mime:
                media.append((url, mime))
            else:
                kept.append(url)
        if not media:
            continue
        # An entry that already carries media keeps it; only the image list
        # is corrected. Nothing here should overwrite a real stored value.
        media_url, media_type = media[0]
        if row["media_url"]:
            media_url, media_type = row["media_url"], None
        discarded += len(media) - 1
        conn.execute(
            "UPDATE feed_entries SET image_urls = ?, media_url = ?, "
            "media_type = COALESCE(?, media_type) WHERE id = ?",
            (json.dumps(kept) if kept else None, media_url, media_type, row["id"]),
        )
        conn.execute("DELETE FROM entry_images WHERE entry_id = ?", (row["id"],))
        _index_entry_images(
            conn,
            entry_id=row["id"],
            image_urls=kept,
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
        )
        lifted += 1

    if lifted:
        logger.info(
            "feeds_db_media_lifted_from_images entries=%d extra_media_discarded=%d",
            lifted, discarded,
        )


# "Say nothing about this column." Distinct from `None`, which for both the
# observation marker and the poll claim is a meaningful value the poller
# writes on purpose.
UNCHANGED: Any = object()


def _utc_iso(when: datetime) -> str:
    """An aware datetime as a UTC ISO string.

    Every timestamp this module stores is compared lexically against another
    ISO string, so an offset other than ``+00:00`` sorts by its own local
    reading rather than the instant it names. Converting is the one place that
    is prevented; a naive value is left to the caller's own guard, since only
    the caller knows whether guessing UTC for it is safe.
    """
    if when.tzinfo is None:
        return when.isoformat()
    return when.astimezone(timezone.utc).isoformat()

# Keys in `schema_meta`. The two settings are user-facing and reach the API;
# the third is internal and never does — it is the upgrade grace deadline, and
# a user who could edit it could turn a safety period into an immediate delete.
ENTRY_RETENTION_DAYS_KEY = "feeds_settings.entry_retention_days"
MAX_ENTRIES_PER_FEED_KEY = "feeds_settings.max_entries_per_feed"
ENTRY_PRUNE_NOT_BEFORE_KEY = "feeds_internal.entry_prune_not_before"


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add the retention observation columns and open the upgrade grace period
    (ISSUE-388).

    Three nullable columns, the partial indexes, and then a data pass, because
    the columns alone would leave every existing row unclassifiable — and
    worse, with the count pass having no age predicate of its own, deletable
    the day the feature ships.

    The pass does four things, and each is one half of a pair:

    * **Stamp every entry** with one shared observation time. It is not true
      that the source returned each of them at that instant; what it records is
      that this deployment has no earlier evidence, and the marker below is
      what stops that being read as evidence of anything. `fetched_at` is
      deliberately not touched: it is the retention clock, and rewriting it
      would reset every entry's age to the upgrade date.
    * **Leave `last_items_seen_at` null.** Age pruning requires a non-null
      marker and `last_seen_at < last_items_seen_at`, so until a feed completes
      one post-upgrade fetch that returns an item, none of its rows are
      deletable. A feed that never polls again keeps everything.
    * **Clear the conditional validators** and make every feed due. A stored
      ETag would answer the first post-upgrade poll with a 304, which carries
      no entry list and so can never advance the marker. The next poll has to
      fetch a full body.
    * **Write the grace deadline.** An observation timestamp alone cannot stop
      the count pass, which has no age predicate. This row does, for both
      passes, for ninety days.

    The stamp is guarded on `last_seen_at IS NULL` so a re-run cannot overwrite
    real observations, and the settings rows go in with `INSERT OR IGNORE` so a
    user override is never replaced by a default.

    This is the first migration to write to `schema_meta`, which is why it
    creates the table rather than assuming it: `_read_schema_version` returns 1
    for a database predating that table, and `init_db` runs the whole migration
    chain *before* `SCHEMA_SQL`. Without this, the oldest databases stopped
    opening at all rather than being migrated.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN last_seen_at TEXT")

    feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
    if "last_items_seen_at" not in feed_cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN last_items_seen_at TEXT")
    if "poll_claimed_until" not in feed_cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN poll_claimed_until TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_feed_last_seen_unstarred "
        "ON feed_entries(feed_id, last_seen_at) WHERE starred = 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_feed_fetched_unstarred "
        "ON feed_entries(feed_id, fetched_at) WHERE starred = 0"
    )

    migration_now = datetime.now(timezone.utc)
    stamp = migration_now.isoformat()
    stamped = conn.execute(
        "UPDATE feed_entries SET last_seen_at = ? WHERE last_seen_at IS NULL",
        (stamp,),
    ).rowcount
    conn.execute(
        "UPDATE feeds SET etag = NULL, last_modified = NULL, next_poll_at = NULL"
    )
    for key, value in (
        (ENTRY_RETENTION_DAYS_KEY, str(DEFAULT_ENTRY_RETENTION_DAYS)),
        (MAX_ENTRIES_PER_FEED_KEY, str(DEFAULT_MAX_ENTRIES_PER_FEED)),
        (
            ENTRY_PRUNE_NOT_BEFORE_KEY,
            (migration_now + timedelta(days=UPGRADE_GRACE_DAYS)).isoformat(),
        ),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            (key, value),
        )
    logger.info(
        "feeds_db_retention_grace_opened entries_stamped=%s days=%s",
        stamped, UPGRADE_GRACE_DAYS,
    )


_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, _migrate_v1_to_v2),
    (3, _migrate_v2_to_v3),
    (4, _migrate_v3_to_v4),
    (5, _migrate_v4_to_v5),
    (6, _migrate_v5_to_v6),
    (7, _migrate_v6_to_v7),
    (8, _migrate_v7_to_v8),
]


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema for the feeds DB.

    Idempotent. Safe to call on every startup. Migrations run *before*
    ``SCHEMA_SQL`` because ``SCHEMA_SQL`` includes ``CREATE INDEX … WHERE
    starred = 1`` — that reference would fail on a v1 DB unless the column
    has been added first.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        # WAL: this DB now lives on LOCAL disk (Config.module_db_path, off the
        # rclone FUSE mount), so WAL's mmap'd -shm is safe again — the SIGBUS
        # that forced DELETE mode (ISSUE-157) was a FUSE-mount artifact. WAL
        # gives reader/writer concurrency, which DELETE (whole-file lock) did
        # not — the dispatch-loop contention fix. journal_mode is persistent in
        # the file header, so issuing this unconditionally also converts a
        # relocated DELETE-mode DB to WAL on first touch; no-op once WAL.
        conn.execute("PRAGMA journal_mode = WAL")
        current = _read_schema_version(conn)
        for target_version, migrate in _MIGRATIONS:
            if current < target_version:
                migrate(conn)
                logger.info(
                    "feeds_db_migrated from=v%s to=v%s", current, target_version,
                )
                current = target_version
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the persisted schema version.

    On a brand-new file the ``schema_meta`` table doesn't exist yet — return
    ``SCHEMA_VERSION`` so we skip migrations and let ``SCHEMA_SQL`` create
    everything from scratch. On an existing DB without a recorded version
    (extremely old, pre-``schema_meta``), fall back to v1.
    """
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not has_meta:
        has_entries = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='feed_entries'"
        ).fetchone()
        return 1 if has_entries else SCHEMA_VERSION
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        return 1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 1


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the conventions this module expects.

    - ``foreign_keys = ON`` so the FK from feeds.category_id and from
      feed_entries.feed_id behave.
    - ``Row`` factory for column-name access in callers.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # journal_mode (WAL) is set once by init_db and persists in the file
    # header — NOT re-issued here (re-issuing takes a write lock per open).
    # 30s busy handler absorbs any residual contention between the web reader
    # and the */5min feeds poll instead of raising SQLITE_BUSY.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# -- categories ---------------------------------------------------------------


def upsert_category(conn: sqlite3.Connection, slug: str, title: str) -> int:
    """Insert or update a category by slug. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO feed_categories(slug, title) VALUES (?, ?)
        ON CONFLICT(slug) DO UPDATE SET title = excluded.title
        RETURNING id
        """,
        (slug, title),
    )
    row = cur.fetchone()
    return int(row["id"])


def list_categories(conn: sqlite3.Connection) -> list[CategoryRecord]:
    rows = conn.execute(
        "SELECT id, slug, title FROM feed_categories ORDER BY title COLLATE NOCASE"
    ).fetchall()
    return [CategoryRecord(id=r["id"], slug=r["slug"], title=r["title"]) for r in rows]


def get_category_by_slug(conn: sqlite3.Connection, slug: str) -> CategoryRecord | None:
    row = conn.execute(
        "SELECT id, slug, title FROM feed_categories WHERE slug = ?", (slug,)
    ).fetchone()
    if not row:
        return None
    return CategoryRecord(id=row["id"], slug=row["slug"], title=row["title"])


def delete_category(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM feed_categories WHERE slug = ?", (slug,))


def ensure_category(conn: sqlite3.Connection, slug: str) -> int:
    """Return the id of the category with this slug, creating one with the
    slug doubling as title if it doesn't exist yet.

    Distinct from :func:`upsert_category` — this does NOT overwrite an
    existing title when called with slug-as-title. Use this from callers
    that only know the slug (CLI ``--category``, OPML import that lacks a
    title, etc.) to avoid stomping on a title set elsewhere.
    """
    existing = get_category_by_slug(conn, slug)
    if existing is not None:
        return existing.id
    return upsert_category(conn, slug, slug)


# -- feeds --------------------------------------------------------------------


def upsert_feed(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str | None,
    site_url: str | None,
    source_type: str,
    category_id: int | None,
    poll_interval_minutes: int,
) -> int:
    """Insert or update a feed by URL. Returns the row id.

    Doesn't touch fetch state (etag, last_modified, error_count) — those are
    owned by the poller.
    """
    cur = conn.execute(
        """
        INSERT INTO feeds(
            url, title, site_url, source_type, category_id,
            poll_interval_minutes
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = COALESCE(excluded.title, feeds.title),
            site_url = COALESCE(excluded.site_url, feeds.site_url),
            source_type = excluded.source_type,
            category_id = excluded.category_id,
            poll_interval_minutes = excluded.poll_interval_minutes
        RETURNING id
        """,
        (url, title, site_url, source_type, category_id, poll_interval_minutes),
    )
    return int(cur.fetchone()["id"])


def get_feed_by_url(conn: sqlite3.Connection, url: str) -> FeedRecord | None:
    row = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    return _row_to_feed(row)


def stored_url_variants(conn: sqlite3.Connection) -> dict[str, str]:
    """Canonical feed URL -> the spelling a row is actually stored under.

    Only rows whose stored URL is *not* its own canonical form appear, so a
    lookup that misses means there is no such row. A subscription added before
    ISSUE-432 holds whatever was typed (`arena:/slug`, a mixed-case scheme),
    and every seam that stores a feed now writes the canonical form — so
    without this a seam would either insert a second row polling the same
    channel, or, in the settings save, insert one *and* delete the original
    along with its entries, since that handler removes any stored feed the
    payload does not name. Callers write to the spelling this hands back, which
    is what keeps an existing row's id, entries, stars and read state.

    A canonical form some row already holds is excluded: mapping onto it would
    point two payload entries at one row and leave the other to be swept.

    Deliberately not a repair — nothing here rewrites a stored URL. A row keeps
    its spelling and goes on fetching, because `provider_identifier` normalizes
    again on the way to the request.
    """
    feeds = list_feeds(conn)
    stored = {feed.url for feed in feeds}
    variants: dict[str, str] = {}
    for feed in feeds:
        canonical = normalize_feed_url(feed.url)
        if canonical and canonical != feed.url and canonical not in stored:
            variants.setdefault(canonical, feed.url)
    return variants


def list_feeds(conn: sqlite3.Connection) -> list[FeedRecord]:
    rows = conn.execute(
        "SELECT * FROM feeds ORDER BY title COLLATE NOCASE, url"
    ).fetchall()
    return [_row_to_feed(r) for r in rows]


def feeds_due_for_poll(
    conn: sqlite3.Connection, now: datetime | None = None,
) -> list[FeedRecord]:
    """Return feeds whose ``next_poll_at`` is in the past (or null).

    A feed under a live poll claim is excluded: another process is fetching it
    right now, and a second fetch would race that one's membership write
    (ISSUE-388). An expired claim is no claim — a process that died mid-fetch
    must not take its feed off the air.
    """
    now = now or datetime.now(timezone.utc)
    # Both comparisons below are lexical on ISO strings, so an offset other
    # than +00:00 compares wrong by that offset — eastward a live claim reads
    # as expired hours early, westward an expired one holds. Every writer here
    # stores UTC, so a caller's clock is converted rather than trusted.
    iso = _utc_iso(now)
    rows = conn.execute(
        """
        SELECT * FROM feeds
        WHERE (next_poll_at IS NULL OR next_poll_at <= ?)
          AND (poll_claimed_until IS NULL OR poll_claimed_until <= ?)
        ORDER BY (next_poll_at IS NULL) DESC, next_poll_at ASC, id ASC
        """,
        (iso, iso),
    ).fetchall()
    return [_row_to_feed(r) for r in rows]


def claim_feed_for_poll(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    now: datetime,
    lease_seconds: int = POLL_CLAIM_SECONDS,
) -> bool:
    """Take a short exclusive lease on one feed. ``True`` when we got it.

    One conditional update, committed before the caller's network call, so a
    competing process sees the claim rather than fetching the same feed. It
    succeeds only when the feed is due and its claim is null or expired — the
    same two predicates :func:`feeds_due_for_poll` filters on, restated here
    because the interval between that SELECT and the fetch is precisely the
    race this closes.

    Deliberately *not* a database lock: the fetch takes up to 30 seconds and
    nothing may hold a SQLite write transaction across network I/O. The cost of
    that choice is the lease — a process that exits unexpectedly leaves one,
    and the feed waits it out.
    """
    if now.tzinfo is None:
        raise ValueError("claim_feed_for_poll requires a timezone-aware `now`")
    # Aware is not enough: the lease is read back by another process as a
    # lexical ISO comparison, so a `now` carrying any offset but +00:00 writes
    # a lease that reader misjudges by exactly that offset — which for an
    # eastward one means two processes fetching the same feed, the race this
    # function exists to close.
    iso = _utc_iso(now)
    until = _utc_iso(now + timedelta(seconds=lease_seconds))
    cur = conn.execute(
        """
        UPDATE feeds
        SET poll_claimed_until = ?
        WHERE id = ?
          AND (next_poll_at IS NULL OR next_poll_at <= ?)
          AND (poll_claimed_until IS NULL OR poll_claimed_until <= ?)
        """,
        (until, feed_id, iso, iso),
    )
    conn.commit()
    return bool(cur.rowcount)


def update_feed_fetch_state(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    etag: str | None,
    last_modified: str | None,
    # `str | None` because the rate-limited path writes the feed's existing
    # value straight back, which is NULL on a feed that has never been fetched
    # — nothing was fetched, so the column must not start asserting otherwise.
    last_fetched_at: str | None,
    last_error: str | None,
    error_count: int,
    next_poll_at: str,
    discovered_title: str | None = None,
    discovered_site_url: str | None = None,
    last_throttled_at: str | None = None,
    # Sentinel-backed rather than `None`-defaulted, because `None` is a real
    # value for both: clearing the claim is what every handled outcome does,
    # and only a response that returned an item may advance the marker. A
    # caller that says nothing must leave both columns exactly as they are —
    # an error path defaulting the marker to NULL would discard every entry's
    # protection on the first 500 (ISSUE-388).
    last_items_seen_at: Any = UNCHANGED,
    poll_claimed_until: Any = UNCHANGED,
) -> None:
    """Persist the outcome of a single poll attempt."""
    sets = [
        "etag = ?",
        "last_modified = ?",
        "last_fetched_at = ?",
        "last_error = ?",
        "error_count = ?",
        "next_poll_at = ?",
        "title = COALESCE(?, title)",
        "site_url = COALESCE(?, site_url)",
        "last_throttled_at = ?",
    ]
    params: list[Any] = [
        etag, last_modified, last_fetched_at, last_error,
        error_count, next_poll_at, discovered_title,
        discovered_site_url, last_throttled_at,
    ]
    if last_items_seen_at is not UNCHANGED:
        sets.append("last_items_seen_at = ?")
        params.append(last_items_seen_at)
    if poll_claimed_until is not UNCHANGED:
        sets.append("poll_claimed_until = ?")
        params.append(poll_claimed_until)
    params.append(feed_id)
    conn.execute(
        f"UPDATE feeds SET {', '.join(sets)} WHERE id = ?",
        tuple(params),
    )


def delete_feed(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM feeds WHERE url = ?", (url,))


# -- settings (singleton scalars stored in schema_meta) -----------------------


_DEFAULT_INTERVAL_KEY = "feeds_settings.default_poll_interval_minutes"
_IMAGE_DEDUPE_WINDOW_KEY = "feeds_settings.image_dedupe_window_days"


def get_default_poll_interval(conn: sqlite3.Connection) -> int | None:
    """Read the user-set global poll-interval default. ``None`` if unset."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        (_DEFAULT_INTERVAL_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_default_poll_interval(
    conn: sqlite3.Connection, minutes: int | None,
) -> None:
    """Set or clear the global poll-interval default."""
    if minutes is None:
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            (_DEFAULT_INTERVAL_KEY,),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_DEFAULT_INTERVAL_KEY, str(minutes)),
        )


def get_image_dedupe_window_days(conn: sqlite3.Connection) -> int | None:
    """Read the user-set image-suppression look-back window. ``None`` if unset.

    ``0`` is a real value meaning "off", distinct from unset (which falls back
    to :data:`istota.feeds.image_dedupe.DEFAULT_WINDOW_DAYS`).
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        (_IMAGE_DEDUPE_WINDOW_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_image_dedupe_window_days(
    conn: sqlite3.Connection, days: int | None,
) -> None:
    """Set or clear the image-suppression look-back window."""
    if days is None:
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            (_IMAGE_DEDUPE_WINDOW_KEY,),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_IMAGE_DEDUPE_WINDOW_KEY, str(int(days))),
        )


def _get_int_setting(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _set_int_setting(conn: sqlite3.Connection, key: str, value: int | None) -> None:
    if value is None:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (key, str(int(value))),
        )


def get_entry_retention_days(conn: sqlite3.Connection) -> int | None:
    """Read the user-set age window in days. ``None`` if unset or malformed.

    ``0`` is a real value meaning "no age pruning", distinct from unset (which
    falls back to :data:`istota.feeds.models.DEFAULT_ENTRY_RETENTION_DAYS`).
    """
    return _get_int_setting(conn, ENTRY_RETENTION_DAYS_KEY)


def set_entry_retention_days(conn: sqlite3.Connection, days: int | None) -> None:
    """Set or clear the age window. ``None`` deletes the stored row."""
    _set_int_setting(conn, ENTRY_RETENTION_DAYS_KEY, days)


def get_max_entries_per_feed(conn: sqlite3.Connection) -> int | None:
    """Read the user-set per-feed maximum. ``None`` if unset or malformed."""
    return _get_int_setting(conn, MAX_ENTRIES_PER_FEED_KEY)


def set_max_entries_per_feed(conn: sqlite3.Connection, count: int | None) -> None:
    """Set or clear the per-feed maximum. ``None`` deletes the stored row."""
    _set_int_setting(conn, MAX_ENTRIES_PER_FEED_KEY, count)


def get_entry_prune_not_before(conn: sqlite3.Connection) -> str | None:
    """The upgrade grace deadline, or ``None`` on a database that has none.

    Internal: written once by the v7-to-v8 migration and never exposed through
    the API, because a user who could edit it could turn a safety period into
    an immediate delete.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        (ENTRY_PRUNE_NOT_BEFORE_KEY,),
    ).fetchone()
    return None if row is None else row["value"]


def clear_entry_prune_not_before(conn: sqlite3.Connection) -> None:
    """Drop the grace row. Called only after both passes have succeeded, in
    their own transaction, so a rolled-back prune keeps its grace."""
    conn.execute(
        "DELETE FROM schema_meta WHERE key = ?", (ENTRY_PRUNE_NOT_BEFORE_KEY,),
    )


def _effective_floor(min_entries_per_feed: int, max_entries_per_feed: int) -> int:
    """The floor a feed is actually held at.

    The ceiling wins where a user set one below the floor: an explicit
    instruction to store at most twenty entries must not be overridden by a
    default that says fifty. With no ceiling there is nothing to clamp
    against, so the constant stands.
    """
    floor = max(min_entries_per_feed, 0)
    if max_entries_per_feed > 0:
        return min(floor, max_entries_per_feed)
    return floor


def budget_floor(max_entries_per_feed: int) -> int:
    """The floor under a feed's unstarred budget, and the only place
    :data:`MIN_ENTRIES_PER_FEED` is read for the maximum.

    One place because the count pass and admission must delete and refuse the
    same rows or they take turns: the pass needs it as a bound SQL value and
    :func:`unstarred_budget` needs it in Python, and a second reading of the
    constant is how the two start disagreeing.

    ``0`` is *no maximum*, and it disables this clamp with it — a floor of
    fifty under a ceiling that does not exist would be a bound nobody asked
    for. That is where this parts company with :func:`_effective_floor`, whose
    caller is the age pass: there the constant stands with no ceiling, because
    a quiet feed still has to keep something.
    """
    if max_entries_per_feed <= 0:
        return 0
    return _effective_floor(MIN_ENTRIES_PER_FEED, max_entries_per_feed)


def unstarred_budget(max_entries_per_feed: int, starred_count: int) -> int:
    """How many unstarred rows one feed may hold under its maximum.

    Stars sit outside the ceiling by design, so they come off the total first
    — and the remainder is floored at ``min(MIN_ENTRIES_PER_FEED,
    max_entries_per_feed)``, which is load-bearing rather than tidiness. Without
    it a feed whose stars reach the maximum gets a budget of zero
    *permanently*: the count pass deletes every unstarred row, unread and
    in-response alike, ``plan_admission`` then admits nothing ever again, and
    the feed goes silently inert with only ``protected_excess_entries`` hinting
    at it. Reserving room beneath the stars breaks no promise the setting
    makes: such a feed is over its maximum either way, and the difference is
    only whether it still works.

    One consequence, stated because it surprises: at or below the floor the
    effective floor *is* the maximum, so stars take nothing off the budget
    there and a feed with twenty stars and a maximum of twenty stores twenty
    unstarred rows as well.

    A maximum of ``0`` disables the limit and both clamps with it; both callers
    short-circuit before here, and the ``0`` returned on that path is what a
    budget under a ceiling that does not exist is worth.
    """
    if max_entries_per_feed <= 0:
        return 0
    return max(
        max_entries_per_feed - max(starred_count, 0),
        budget_floor(max_entries_per_feed),
    )


def _changes(conn: sqlite3.Connection) -> int:
    """Rows the last statement changed.

    ``cursor.rowcount`` is ``-1`` for a statement prefixed by ``WITH`` under
    this driver, so a caller adding those up silently reports a negative
    deletion count. ``SELECT changes()`` is read immediately after the delete
    instead, and is never negative.
    """
    return int(conn.execute("SELECT changes()").fetchone()[0])


def prune_entries_by_age(
    conn: sqlite3.Connection,
    *,
    before_iso: str,
    min_entries_per_feed: int,
    max_entries_per_feed: int,
) -> tuple[int, int]:
    """Delete read and removed entries past the age cutoff. ``(deleted, held)``.

    A row goes only when every one of these holds:

    * it is not starred;
    * it is read or removed — an unread row is never aged out;
    * its ``fetched_at`` is older than ``before_iso``. That is the clock: when
      the entry entered *this reader*, not when the source published it;
    * its feed has a non-null ``last_items_seen_at`` — no response has ever
      returned an item there otherwise, so nothing is known and it fails
      closed;
    * its own ``last_seen_at`` is non-null and older than that marker, so it
      was **not** in the most recent response. This is the churn guard: we
      never delete something the feed has just handed us, so the feed cannot
      hand it back;
    * and deleting it would not take its feed below the effective floor.

    Rows are ranked newest ``fetched_at`` first across *every* stored row for
    the feed, not only the deletable ones, so a feed holding fifty unread rows
    has plenty of history and does lose its old read ones. ``held`` counts the
    candidates the floor spared — reported rather than inferred, because
    without it a quiet feed and a feed with nothing to prune produce identical
    output. Does not commit.

    The tie-break is ``id ASC`` and it is load-bearing rather than arbitrary:
    every entry of one poll is stored with that poll's single ``fetched_at``,
    so a whole batch ties and the second key decides all of it.
    ``insert_entries`` writes in source order, so the lowest rowid is the item
    the response listed first — the newest content on any ordinary feed.
    ``id DESC`` therefore protected the *tail* of each response and deleted its
    head, which is both backwards and out of step with ``plan_admission``,
    which keeps the head.
    """
    floor = _effective_floor(min_entries_per_feed, max_entries_per_feed)
    ranked = """
        WITH ranked AS (
            SELECT
                e.id AS id,
                ROW_NUMBER() OVER (
                    PARTITION BY e.feed_id
                    ORDER BY e.fetched_at DESC, e.id ASC
                ) AS rn,
                (
                    e.starred = 0
                    AND e.status IN ('read', 'removed')
                    AND e.fetched_at < :before
                    AND f.last_items_seen_at IS NOT NULL
                    AND e.last_seen_at IS NOT NULL
                    AND e.last_seen_at < f.last_items_seen_at
                ) AS candidate
            FROM feed_entries e
            JOIN feeds f ON f.id = e.feed_id
        )
    """
    params = {"before": before_iso, "floor": floor}
    # Counted before the delete, and from the same expression, so the two
    # numbers describe one population rather than two.
    held = int(conn.execute(
        ranked + "SELECT COUNT(*) FROM ranked WHERE candidate AND rn <= :floor",
        params,
    ).fetchone()[0])
    conn.execute(
        ranked
        + "DELETE FROM feed_entries WHERE id IN ("
        "SELECT id FROM ranked WHERE candidate AND rn > :floor)",
        params,
    )
    return _changes(conn), held


def prune_entries_to_feed_cap(
    conn: sqlite3.Connection,
    *,
    max_entries_per_feed: int,
) -> tuple[int, int, int]:
    """Trim each feed to its maximum. ``(deleted, feeds_over, protected)``.

    The maximum applies to *total* stored rows for one feed, with stars as the
    only exemption, so a feed's unstarred budget is what the maximum leaves
    after its stars — floored, see :func:`unstarred_budget`.

    **One ordering, no tiers, and read state is not part of it.** An earlier
    draft kept unread rows ahead of read ones here. Because admission ranks by
    source order and this pass then re-ranked by read state, the two disagreed:
    a feed near its maximum could have *in-response read* rows trimmed while
    older out-of-response unread rows were kept, and the next poll re-admitted
    the trimmed ones as unread — churn every poll, for good. Ranking by
    recency alone makes this pass delete exactly what admission refuses, which
    is what lets it carry no most-recent-response clause of its own. It cannot
    carry one: a maximum lowered below a feed's own window would be
    unenforceable if every row in the window were undeletable.

    That agreement is with the **last observed** response order, and the
    residual is worth naming rather than leaving inside the word "exactly".
    This pass ranks stored rows by ``fetched_at`` and rowid, which record the
    order of the poll that stored them; admission ranks the response in front
    of it. A source that reorders its window between polls therefore moves rows
    across the boundary, and one that moves back in can be deleted here and
    handed back as unread — the shape the age pass closes with its in-response
    clause, which a ceiling cannot carry for the reason above. What a
    reordering source costs is deleting *more* than admission refuses; nothing
    is stored and immediately trimmed, since both ends compute one budget.

    Unread rows lose nothing they should keep. The *age* pass exempts them
    absolutely, which is where "don't throw away what I haven't read" belongs;
    this is the hard ceiling and nothing else. The cost, stated plainly: on a
    feed at its ceiling an old unread entry can be dropped ahead of a newer
    read one.

    The tie-break is ``id ASC``, for the reason ``prune_entries_by_age`` states
    at length, and the tie is the common case rather than a corner: one poll's
    entries all share that poll's ``fetched_at``, so a whole batch ties and the
    tie-break alone decides it. Insertion follows source order, so the lowest
    rowid is the item the response listed first — which is what admission
    keeps. Under ``id DESC`` this pass kept the tail instead, so it deleted
    exactly the entries the next response would hand back.

    The floor on *rows* is deliberately not honoured here. It guards against an
    *age* rule emptying a quiet feed; a feed over its configured maximum is by
    definition not empty, and honouring it would make a maximum below fifty
    unenforceable. The floor on the *budget* is a different quantity and does
    apply.

    A feed can finish above the maximum two ways, and the reported overage is
    the plain difference rather than either cause: its stars, which are never
    deleted, and the floor under its budget, which holds unstarred rows a
    star-consumed budget would have taken. Reported rather than guessing that
    a starred row is safe to delete. ``0`` disables the pass entirely. Does not
    commit.
    """
    if max_entries_per_feed <= 0:
        return 0, 0, 0
    conn.execute(
        """
        WITH stars AS (
            SELECT feed_id, COUNT(*) AS n FROM feed_entries
            WHERE starred = 1 GROUP BY feed_id
        ),
        ranked AS (
            SELECT
                e.id AS id,
                e.feed_id AS feed_id,
                ROW_NUMBER() OVER (
                    PARTITION BY e.feed_id
                    ORDER BY e.fetched_at DESC, e.id ASC
                ) AS rn
            FROM feed_entries e
            WHERE e.starred = 0
        )
        DELETE FROM feed_entries WHERE id IN (
            SELECT r.id FROM ranked r
            LEFT JOIN stars s ON s.feed_id = r.feed_id
            WHERE r.rn > MAX(:cap - COALESCE(s.n, 0), :floor)
        )
        """,
        {
            "cap": max_entries_per_feed,
            # The same expression `unstarred_budget` applies in Python, in the
            # one form SQL can take it: the per-feed star count is only known
            # inside the statement, so the floor crosses as a bound value.
            "floor": budget_floor(max_entries_per_feed),
        },
    )
    deleted = _changes(conn)
    over = conn.execute(
        """
        SELECT COUNT(*) AS feeds, COALESCE(SUM(n - :cap), 0) AS excess FROM (
            SELECT COUNT(*) AS n FROM feed_entries
            GROUP BY feed_id HAVING n > :cap
        )
        """,
        {"cap": max_entries_per_feed},
    ).fetchone()
    return deleted, int(over["feeds"]), int(over["excess"])


# -- entries ------------------------------------------------------------------


# SQLite's own host-parameter limit is far higher, but one response can be
# arbitrarily large and a single over-long IN list would fail the whole poll.
_PARAM_CHUNK = 400


def mark_entries_seen(
    conn: sqlite3.Connection,
    feed_id: int,
    guids: Iterable[str],
    *,
    seen_at: str,
) -> int:
    """Record that this feed's response returned these guids. Never inserts.

    The counterpart to the observation stamp ``insert_entries`` writes, for the
    entries a response returned and admission did **not** store — everything
    past the per-feed budget. Being returned is what protects a row from the
    age pass, and admission is a decision about the maximum rather than about
    what was observed, so an entry the source is still handing over must not be
    left looking like history because the feed is full (ISSUE-388).

    Without it the two rules fight: the age pass deletes those rows, that frees
    budget, admission then re-admits the same guids from the next response as
    fresh unread rows, and a read entry resurfaces every retention period.
    """
    wanted = [g for g in guids if g]
    if not wanted or not seen_at:
        return 0
    updated = 0
    for start in range(0, len(wanted), _PARAM_CHUNK):
        chunk = wanted[start:start + _PARAM_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE feed_entries SET last_seen_at = ? "
            f"WHERE feed_id = ? AND guid IN ({placeholders})",
            (seen_at, feed_id, *chunk),
        )
        updated += max(cur.rowcount, 0)
    return updated


def insert_entries(
    conn: sqlite3.Connection,
    feed_id: int,
    items: Iterable[EntryRecord],
) -> int:
    """Insert new entries and refresh the content of ones we already hold.

    Returns the count of *newly-inserted* rows — a refresh is not "new", so
    the poller's "N new entries" log and the unread badge stay honest.

    Matching is by ``(feed_id, guid)``. A guid we've seen before used to be
    discarded outright, which meant a provider fix could only ever reach
    blocks connected *after* it shipped: the Are.na v3 upgrade taught the
    poller to emit real HTML bodies, ``embed_url`` and ``file_url``, and
    every already-stored block went on re-fetching, conflicting and being
    thrown away, so its video / PDF / text cards stayed blank forever.

    Content therefore follows the feed, and two things deliberately don't:

    * **User state.** ``status`` / ``starred`` / ``starred_at`` are never
      touched, so a repair pass can't resurrect a read entry as unread or
      drop a star.
    * **``fetched_at``.** The first sighting is when the entry entered *your*
      reader; keeping it stops the "recently added" ordering and the
      image-dedup look-back window from lurching on a refresh.

    A field the feed stopped sending never erases one we hold — a thinner
    later fetch can only degrade the card, so the richer value wins.

    ``last_seen_at`` moves on every insert and every refresh, taken from the
    incoming record's ``fetched_at`` — the poll clock, not the stored
    first-sighting. It is the entry-level half of the most-recent-response
    clause: an entry whose stamp equals its feed's ``last_items_seen_at`` was
    in the latest response and is never age-deleted (ISSUE-388).
    """
    inserted = 0
    refreshed = 0
    for item in items:
        image_json = json.dumps(item.image_urls) if item.image_urls else None
        # '' would compare as a timestamp older than every real one, so an
        # unstamped hand-built record must read as "never observed" instead.
        seen_at = item.fetched_at or None
        existing = conn.execute(
            "SELECT id, image_urls, published_at, fetched_at FROM feed_entries "
            "WHERE feed_id = ? AND guid = ?",
            (feed_id, item.guid),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO feed_entries(
                    feed_id, guid, title, url, author, content_html,
                    content_text, image_urls, embed_url, file_url,
                    media_url, media_type, published_at, fetched_at, status,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feed_id,
                    item.guid,
                    item.title,
                    item.url,
                    item.author,
                    item.content_html,
                    item.content_text,
                    image_json,
                    item.embed_url,
                    item.file_url,
                    item.media_url,
                    item.media_type,
                    item.published_at,
                    item.fetched_at,
                    item.status,
                    seen_at,
                ),
            )
            if cur.rowcount:
                inserted += 1
                _index_entry_images(
                    conn,
                    entry_id=cur.lastrowid,
                    image_urls=item.image_urls or [],
                    published_at=item.published_at,
                    fetched_at=item.fetched_at,
                )
                continue
            # OR IGNORE still applies: a concurrent poll may have inserted the
            # same guid between the SELECT and here. Reload and take the
            # refresh path rather than skipping — skipping leaves the row with
            # whatever observation state the winner gave it, and this response
            # is evidence the source still returns the entry. The poll claim
            # makes this unreachable on the normal path; a direct caller has
            # no claim, so the function has to stay correct without one.
            existing = conn.execute(
                "SELECT id, image_urls, published_at, fetched_at FROM feed_entries "
                "WHERE feed_id = ? AND guid = ?",
                (feed_id, item.guid),
            ).fetchone()
            if existing is None:
                # The winner rolled back, or something deleted the row again.
                # Nothing to refresh and nothing was inserted.
                continue

        # COALESCE(NULLIF(?, ''), col) is the "never overwrite with nothing"
        # rule: an absent field arrives as NULL and an empty one as '', and
        # both leave the stored value standing.
        conn.execute(
            """
            UPDATE feed_entries SET
                title        = COALESCE(NULLIF(?, ''), title),
                url          = COALESCE(NULLIF(?, ''), url),
                author       = COALESCE(NULLIF(?, ''), author),
                content_html = COALESCE(NULLIF(?, ''), content_html),
                content_text = COALESCE(NULLIF(?, ''), content_text),
                image_urls   = COALESCE(?, image_urls),
                embed_url    = COALESCE(NULLIF(?, ''), embed_url),
                file_url     = COALESCE(NULLIF(?, ''), file_url),
                media_url    = COALESCE(NULLIF(?, ''), media_url),
                media_type   = COALESCE(NULLIF(?, ''), media_type),
                published_at = COALESCE(NULLIF(?, ''), published_at),
                -- The observation moves on any sighting. A record carrying no
                -- stamp at all (a hand-built one) leaves the stored value
                -- standing rather than blanking it, which would read as
                -- "never observed" and make the row undeletable for good.
                last_seen_at = COALESCE(?, last_seen_at)
            WHERE id = ?
            """,
            (
                item.title,
                item.url,
                item.author,
                item.content_html,
                item.content_text,
                image_json,
                item.embed_url,
                item.file_url,
                item.media_url,
                item.media_type,
                item.published_at,
                seen_at,
                existing["id"],
            ),
        )
        refreshed += 1

        if image_json is None or image_json == existing["image_urls"]:
            continue
        # entry_images is pure derived data, so rebuild rather than merge:
        # leaving keys for images the entry no longer carries would suppress
        # a later post that legitimately shows one of them.
        conn.execute(
            "DELETE FROM entry_images WHERE entry_id = ?", (existing["id"],)
        )
        _index_entry_images(
            conn,
            entry_id=existing["id"],
            image_urls=item.image_urls or [],
            # The row keeps its original fetch time, so index against that
            # rather than this poll's clock.
            published_at=item.published_at or existing["published_at"],
            fetched_at=existing["fetched_at"],
        )

    if refreshed:
        logger.debug(
            "feeds_db_entries_refreshed feed_id=%s count=%s", feed_id, refreshed,
        )
    return inserted


def _index_entry_images(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    image_urls: list[str],
    published_at: str | None,
    fetched_at: str | None,
) -> int:
    """Record this entry's normalised image keys. Returns rows written.

    Skipped entirely for an entry with no parseable timestamp — the look-back
    window can't be evaluated against it, so indexing it would only produce
    rows nothing can ever use.
    """
    if not image_urls:
        return 0
    seen_ts = entry_seen_ts(published_at, fetched_at)
    if seen_ts is None:
        return 0
    keys = {image_identity(url) for url in image_urls if url}
    if not keys:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO entry_images(entry_id, image_key, seen_ts) "
        "VALUES (?, ?, ?)",
        [(entry_id, key, seen_ts) for key in sorted(keys)],
    )
    return len(keys)


# SQLite's default host-parameter ceiling is 999 on older builds; chunk well
# under it so a wide page of images can't blow up the owners lookup.
_KEY_CHUNK = 400


def image_key_owners(
    conn: sqlite3.Connection,
    keys: list[str],
    *,
    min_ts: int,
    max_ts: int,
    feed_id: int | None = None,
    category_id: int | None = None,
    starred: bool | None = None,
) -> list[tuple[str, int, int]]:
    """Entries carrying any of ``keys`` with ``seen_ts`` in ``[min_ts, max_ts]``.

    Returns ``(image_key, entry_id, seen_ts)`` rows for
    :func:`istota.feeds.image_dedupe.plan_suppression`.

    ``feed_id`` / ``category_id`` / ``starred`` scope the lookup to the same
    slice of the reader the caller is rendering, so a tile is only ever hidden
    because something the user can actually see already showed it — browsing
    one blog doesn't blank an image because a different blog reblogged it, and
    a starred post keeps the picture you starred it for. Read state is
    deliberately *not* a scope: marking entries read as you scroll would
    otherwise make suppressed images pop back into view mid-scroll.
    """
    if not keys:
        return []

    clauses = ["ei.seen_ts BETWEEN ? AND ?"]
    scope_params: list = [min_ts, max_ts]
    if feed_id:
        clauses.append("e.feed_id = ?")
        scope_params.append(feed_id)
    if category_id:
        clauses.append("f.category_id = ?")
        scope_params.append(category_id)
    if starred is not None:
        clauses.append("e.starred = ?")
        scope_params.append(1 if starred else 0)
    where = " AND ".join(clauses)

    out: list[tuple[str, int, int]] = []
    unique_keys = list(dict.fromkeys(keys))
    for start in range(0, len(unique_keys), _KEY_CHUNK):
        chunk = unique_keys[start:start + _KEY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT ei.image_key, ei.entry_id, ei.seen_ts
            FROM entry_images ei
            JOIN feed_entries e ON e.id = ei.entry_id
            LEFT JOIN feeds f ON f.id = e.feed_id
            WHERE ei.image_key IN ({placeholders}) AND {where}
            """,
            (*chunk, *scope_params),
        ).fetchall()
        out.extend(
            (r["image_key"], int(r["entry_id"]), int(r["seen_ts"])) for r in rows
        )
    return out


def list_entries(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    offset: int = 0,
    status: str | None = None,
    feed_id: int | None = None,
    category_id: int | None = None,
    starred: bool | None = None,
    before_published_ts: int | None = None,
    order: str = "published_at",
    direction: str = "desc",
) -> list[EntryRecord]:
    """Page through entries."""
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if feed_id:
        clauses.append("e.feed_id = ?")
        params.append(feed_id)
    if category_id:
        clauses.append("f.category_id = ?")
        params.append(category_id)
    if starred is not None:
        clauses.append("e.starred = ?")
        params.append(1 if starred else 0)
    order_col = {
        "published_at": "e.published_at",
        "created_at": "e.fetched_at",
        "id": "e.id",
        "starred_at": "e.starred_at",
    }.get(order, "e.published_at")
    if before_published_ts is not None:
        # Strictly less than: cursor operates on the same column as `order`
        # so pagination stays stable across sort modes.
        cutoff = datetime.fromtimestamp(before_published_ts, tz=timezone.utc).isoformat()
        clauses.append(f"{order_col} < ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    direction_sql = "ASC" if direction.lower() == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT e.* FROM feed_entries e
        LEFT JOIN feeds f ON f.id = e.feed_id
        {where}
        ORDER BY {order_col} {direction_sql}, e.id {direction_sql}
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def count_entries(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    feed_id: int | None = None,
    category_id: int | None = None,
    starred: bool | None = None,
) -> int:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if feed_id:
        clauses.append("e.feed_id = ?")
        params.append(feed_id)
    if category_id:
        clauses.append("f.category_id = ?")
        params.append(category_id)
    if starred is not None:
        clauses.append("e.starred = ?")
        params.append(1 if starred else 0)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM feed_entries e
        LEFT JOIN feeds f ON f.id = e.feed_id
        {where}
        """,
        params,
    ).fetchone()
    return int(row["c"])


def update_entry_status(
    conn: sqlite3.Connection, entry_ids: list[int], status: str,
) -> int:
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    cur = conn.execute(
        f"UPDATE feed_entries SET status = ? WHERE id IN ({placeholders})",
        (status, *entry_ids),
    )
    return cur.rowcount


def update_entry_starred(
    conn: sqlite3.Connection, entry_ids: list[int], starred: bool,
) -> int:
    """Toggle the star flag on a batch of entries.

    Sets ``starred_at`` to the current UTC ISO timestamp on true, clears it
    on false. ``starred_at`` is what powers the "starred (recent)" sort.
    """
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    if starred:
        now_iso = iso_now()
        cur = conn.execute(
            f"""
            UPDATE feed_entries
            SET starred = 1, starred_at = ?
            WHERE id IN ({placeholders})
            """,
            (now_iso, *entry_ids),
        )
    else:
        cur = conn.execute(
            f"""
            UPDATE feed_entries
            SET starred = 0, starred_at = NULL
            WHERE id IN ({placeholders})
            """,
            tuple(entry_ids),
        )
    return cur.rowcount


def mark_as_read(
    conn: sqlite3.Connection,
    *,
    scope: str,
    scope_id: int | None = None,
    before_id: int | None = None,
) -> int:
    """Bulk mark unread entries as read.

    ``scope`` controls which entries get touched:

    - ``"all"`` — every unread entry in the DB (no ``scope_id``).
    - ``"feed"`` — every unread entry on the given ``feed_id``.
    - ``"category"`` — every unread entry whose feed sits in the category id.

    ``before_id`` (optional) caps the operation to entries with ``id <=
    before_id``. The reader uses it to make "mark visible as read" stable
    while infinite scroll keeps loading newer entries.

    Only ``status = 'unread'`` rows are touched — already-read or removed
    rows are left alone, and starring is independent of status so this is
    safe for starred entries too.
    """
    if scope not in ("all", "feed", "category"):
        raise ValueError(f"unknown scope: {scope}")
    if scope in ("feed", "category") and scope_id is None:
        raise ValueError(f"scope={scope!r} requires scope_id")

    clauses = ["status = 'unread'"]
    params: list = []
    if scope == "feed":
        clauses.append("feed_id = ?")
        params.append(scope_id)
    elif scope == "category":
        clauses.append(
            "feed_id IN (SELECT id FROM feeds WHERE category_id = ?)"
        )
        params.append(scope_id)
    if before_id is not None:
        clauses.append("id <= ?")
        params.append(before_id)
    where = " AND ".join(clauses)
    cur = conn.execute(
        f"UPDATE feed_entries SET status = 'read' WHERE {where}",
        tuple(params),
    )
    return cur.rowcount or 0


# -- helpers ------------------------------------------------------------------


def _row_to_feed(row: sqlite3.Row) -> FeedRecord:
    return FeedRecord(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        site_url=row["site_url"],
        category_id=row["category_id"],
        source_type=row["source_type"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_fetched_at=row["last_fetched_at"],
        last_error=row["last_error"],
        error_count=row["error_count"],
        poll_interval_minutes=row["poll_interval_minutes"],
        next_poll_at=row["next_poll_at"],
        # `.keys()` rather than a bare subscript: this mapper also runs against
        # a row read before the v6 migration in a mixed-version test.
        last_throttled_at=(
            row["last_throttled_at"] if "last_throttled_at" in row.keys() else None
        ),
        # Same `.keys()` guard, same reason: a row read before the v8 migration
        # in a mixed-version test carries neither column.
        last_items_seen_at=(
            row["last_items_seen_at"] if "last_items_seen_at" in row.keys() else None
        ),
        poll_claimed_until=(
            row["poll_claimed_until"] if "poll_claimed_until" in row.keys() else None
        ),
    )


def _row_to_entry(row: sqlite3.Row) -> EntryRecord:
    return EntryRecord(
        id=row["id"],
        feed_id=row["feed_id"],
        guid=row["guid"],
        title=row["title"],
        url=row["url"],
        author=row["author"],
        content_html=row["content_html"],
        content_text=row["content_text"],
        image_urls=parse_image_urls(row["image_urls"]),
        embed_url=row["embed_url"] if "embed_url" in row.keys() else None,
        file_url=row["file_url"] if "file_url" in row.keys() else None,
        media_url=row["media_url"] if "media_url" in row.keys() else None,
        media_type=row["media_type"] if "media_type" in row.keys() else None,
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        status=row["status"],
        starred=bool(row["starred"]) if "starred" in row.keys() else False,
        starred_at=row["starred_at"] if "starred_at" in row.keys() else None,
    )
