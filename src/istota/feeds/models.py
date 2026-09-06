"""Data types for the feeds module.

The ``FeedsContext`` is the per-user runtime handle (paths + parsed config).
Built by :func:`istota.feeds.workspace.synthesize_feeds_context` or the legacy
:func:`istota.feeds._loader.resolve_for_user`. Everything else takes a context
and operates on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from istota.retry_after import parse_retry_after as _parse_retry_after
from istota.retry_after import retry_after_from_headers as _retry_after_from_headers


# URL-scheme prefixes that bypass the RSS poller and route to native API
# providers instead. These are the same identifiers rss-bridger used; on
# OPML import we rewrite ``http://127.0.0.1:8900/{provider}/{id}/feed.xml``
# to ``{provider}:{id}`` so old exports import cleanly on fresh machines.
PROVIDER_SCHEMES = ("tumblr:", "arena:")

# Default poll cadence and error-backoff cap (minutes).
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_BACKOFF_MAX_MINUTES = 24 * 60

# Per-source-type default poll cadence (minutes). Tumblr and Are.na have
# tight per-key / per-IP rate limits, so we poll them less aggressively
# unless the user overrides per-feed. RSS / Atom go through dozens of
# separate origins so the global default applies.
_SOURCE_TYPE_POLL_DEFAULTS: dict[str, int] = {
    "tumblr": 60,
    "arena": 60,
}


def default_poll_interval_for(source_type: str) -> int:
    """Return the default ``poll_interval_minutes`` for a feed source type."""
    return _SOURCE_TYPE_POLL_DEFAULTS.get(source_type, DEFAULT_POLL_INTERVAL_MINUTES)


# -- rate limiting (ISSUE-347) ------------------------------------------------
#
# The cadence above is per *feed*, and the limits it defends against are per
# key and per IP. Those are different quantities: a cadence says nothing about
# how many requests reach one host at once, which is what a per-IP limit
# counts. Every Are.na channel a user has is one request to `api.are.na`, so a
# due list of N channels was N requests as fast as the process could issue
# them. The four constants below are the parts of the answer that are policy
# rather than mechanism; the poller takes each as a parameter so a caller can
# override one without reaching in here.

# Minimum seconds between two requests to the *same host*. Keyed on the host
# rather than the source type, so it covers Are.na and Tumblr (one host each,
# every feed sharing it) with one rule, and costs RSS nothing (dozens of
# distinct origins). Two feeds that genuinely share an RSS origin are paced
# too, which is correct for the same reason.
DEFAULT_HOST_GAP_SECONDS = 2.0

# Floor for a 429 standoff, and the whole standoff where the server named no
# time. Above `DEFAULT_POLL_INTERVAL_MINUTES` on purpose: being turned away has
# to cost *more* than an ordinary cadence, or a throttled feed comes back on
# the same schedule as a healthy one and the standoff means nothing.
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = 60

# Ceiling on a server-named `Retry-After`. A header naming a week would
# otherwise take a channel off the air for a week on one unverified value.
MAX_RATE_LIMIT_BACKOFF_MINUTES = 6 * 60

# Fractional spread applied to every `next_poll_at`. A set of feeds that was
# due together, burst together and failed together otherwise reschedules to the
# same instant and bursts again one doubling later — the herd re-forms every
# round instead of dispersing.
DEFAULT_JITTER_FRACTION = 0.1

# Burst cap for the periodic job. The due list is ordered oldest-first, so a
# larger backlog drains over consecutive ticks rather than being dropped.
DEFAULT_SCHEDULED_POLL_LIMIT = 50


# -- retention (ISSUE-388) ----------------------------------------------------
#
# A feed's entries used to leave only with the feed. These are the two limits
# that bound them instead, both per user and both resolved from the stored
# setting when there is one. `0` disables a limit; a missing value takes the
# constant.

# How long a read entry is kept after it entered *this reader*. The clock is
# `fetched_at`, not `published_at`: an Are.na block created in 2019 and added
# to a channel today arrives with a 2019 date and would be purged on the day it
# appeared. Never applies to a starred or unread row, and never to an entry the
# most recent response returned.
DEFAULT_ENTRY_RETENTION_DAYS = 90

# How long an upgraded database deletes nothing at all. Deliberately its own
# constant rather than a reuse of the retention default above: they happen to
# be the same number, and coupling them would mean lowering the age window
# silently shortened the safety period an upgrade gets.
UPGRADE_GRACE_DAYS = 90

# Total stored rows for one feed, stars excepted, and the size of the window
# admitted from one response — one budget at both ends, which is what stops a
# response larger than the maximum reinserting everything the count pass just
# deleted. `feeds_db.unstarred_budget` is that budget and is where the two
# qualifications live: stars come off this total, and the remainder is floored
# at `min(MIN_ENTRIES_PER_FEED, this)`, so a feed can stand above the number
# below by its stars and by the rows that floor holds. At or below the floor
# the clamp is the maximum itself, so stars take nothing off it there.
DEFAULT_MAX_ENTRIES_PER_FEED = 5000

# The fewest entries a feed keeps whatever their age. Deliberately not a user
# setting: it is a safety floor rather than a preference — its whole job is to
# stop a low-volume feed emptying out — and the quantity a user has an opinion
# about is the ceiling, which is already exposed. Where `max_entries_per_feed`
# is set below this, the ceiling wins: an explicit instruction to store at most
# twenty entries must not be overridden by a default that says fifty.
MIN_ENTRIES_PER_FEED = 50

# How long one process's claim on a feed lasts. Longer than a single feed's
# 30-second network timeout and scoped to the individual fetch rather than the
# paced batch, so a crashed poll costs one lease rather than the feed. Nothing
# holds a database lock for this long — the claim is one committed row.
POLL_CLAIM_SECONDS = 300

# Ceiling on how long one run may spend *asleep* pacing itself, in seconds.
# Bounds a cost the feed cap alone does not: the poll is a background skill
# task, `user_max_background_workers` defaults to 1, and 50 same-host channels
# at a 2s gap would hold that one slot for ~98s of every 5-minute tick with
# every other background job for that user queued behind it. On exhaustion the
# run stops rather than un-pacing itself — the feeds it did not reach are still
# due, and the next tick takes them, which is the same draining behaviour the
# feed cap relies on.
DEFAULT_MAX_PACING_SECONDS = 60.0


# The single host each native provider talks to. Kept beside the schemes above
# rather than imported from the provider modules, which would make the poller's
# pacing depend on importing every provider it might pace.
_PROVIDER_HOSTS: dict[str, str] = {
    "tumblr": "api.tumblr.com",
    "arena": "api.are.na",
}


class FeedRateLimited(Exception):
    """Raised by a provider when the API answers HTTP 429.

    Carries the ``Retry-After`` the server named, in seconds, so the poller can
    schedule against the server's own answer instead of the generic error
    doubling. Same shape as :class:`istota.health.garmin.GarminRateLimited`,
    deliberately — that is the in-repo precedent and a second spelling of one
    idea is what makes the next one a third.

    ``retry_after`` is ``None`` when the server named no time, which is a
    different fact from naming zero and is why it is not defaulted to a number
    here.
    """

    def __init__(self, retry_after: int | None = None, *, host: str = "") -> None:
        super().__init__(f"rate-limited by {host or 'the server'} (retry_after={retry_after})")
        self.retry_after = retry_after
        self.host = host


def poll_host(url: str, source_type: str = "") -> str:
    """The pacing key for a feed: the host its poll actually reaches.

    Never raises. A feed with an unparseable URL is about to fail in the fetch,
    and failing here instead would take the whole batch down with it — so an
    unusable URL still yields a stable key of its own, derived from the URL.
    Two differently-broken feeds therefore get two keys and are not paced
    against each other, which is the right way round: pacing them together
    would be a claim about a shared host that nothing here established.
    """
    source = source_type or detect_source_type(url)
    known = _PROVIDER_HOSTS.get(source)
    if known:
        return known
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host or f"?{url.lower()}"


def parse_retry_after(raw: object, *, now: "datetime | None" = None) -> int | None:
    """``Retry-After`` in whole seconds, or ``None`` when the header is unusable.

    A thin adapter over :func:`istota.retry_after.parse_retry_after`, which is
    the one implementation and holds every judgement about malformed input.
    This exists only to speak the feeds module's own vocabulary: a ``datetime``
    base rather than a POSIX timestamp, and whole seconds rather than a float,
    which is what the scheduling arithmetic downstream wants.
    """
    base = now or datetime.now(timezone.utc)
    seconds = _parse_retry_after(raw, now_ts=base.timestamp())
    return None if seconds is None else int(seconds)


def retry_after_from_headers(headers: object, *, now: "datetime | None" = None) -> int | None:
    """The same, read off a response's headers case-insensitively.

    Every one of the three 429 sites reads the header through this rather than
    a direct ``headers.get("Retry-After")``: `httpx` and `requests` both hand
    back a case-insensitive mapping, but a plain ``dict`` standing in for one
    does not, and a lookup that quietly missed would put the feed back on the
    generic doubling this issue removed.
    """
    base = now or datetime.now(timezone.utc)
    seconds = _retry_after_from_headers(headers, now_ts=base.timestamp())
    return None if seconds is None else int(seconds)


@dataclass
class FeedsContext:
    """Per-user runtime handle for the feeds module.

    All paths are absolute. The data dir / db path are materialised by
    the workspace loader; ``tumblr_api_key`` and other credentials come
    from the user's encrypted secrets.

    ``workspace_root`` is the user's bot workspace dir (parent of
    ``feeds/``). When ``data_dir`` follows the default layout
    ``{workspace}/feeds`` it equals ``data_dir.parent``, but when an
    operator overrides ``data_dir`` to a non-workspace path, callers
    that need workspace-level config drop-ins (e.g. seed-OPML override
    discovery) must consult this field — ``data_dir.parent`` is
    unreliable in that case.
    """
    user_id: str
    data_dir: Path
    db_path: Path
    tumblr_api_key: str = ""
    workspace_root: Path | None = None

    def ensure_dirs(self) -> None:
        """Create the data dir and the SQLite parent dir."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class FeedRecord:
    """A row from the ``feeds`` table."""
    id: int
    url: str
    title: str | None
    site_url: str | None
    category_id: int | None
    source_type: str            # 'rss' | 'tumblr' | 'arena'
    etag: str | None
    last_modified: str | None
    last_fetched_at: str | None
    last_error: str | None
    error_count: int
    poll_interval_minutes: int
    next_poll_at: str | None
    # When this feed was last turned away with a 429. Distinct from
    # `last_error`, which a throttle deliberately does not write: a throttled
    # channel is healthy, but it is not silent either (ISSUE-347).
    last_throttled_at: str | None = None
    # The poll time of the most recent response that returned at least one
    # item. An entry stamped with exactly this value was in that response and
    # is never age-deleted; anything older was not, which is the only thing
    # that makes a row a deletion candidate (ISSUE-388). NULL means no response
    # has ever returned an item, so nothing about this feed is deletable.
    last_items_seen_at: str | None = None
    # A lease held by whichever process is fetching this feed now. Bounded, so
    # a process that dies mid-fetch delays the feed rather than stranding it.
    poll_claimed_until: str | None = None


@dataclass
class CategoryRecord:
    """A row from the ``feed_categories`` table."""
    id: int
    slug: str
    title: str


@dataclass
class EntryRecord:
    """A row from the ``feed_entries`` table."""
    id: int
    feed_id: int
    guid: str
    title: str | None
    url: str | None
    author: str | None
    content_html: str | None
    content_text: str | None
    image_urls: list[str] = field(default_factory=list)
    embed_url: str | None = None
    file_url: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    published_at: str | None = None
    fetched_at: str = ""
    status: str = "unread"      # 'unread' | 'read' | 'removed'
    starred: bool = False
    starred_at: str | None = None


@dataclass
class FetchedItem:
    """A polled item, pre-storage. Producers (RSS poller, Tumblr provider,
    Are.na provider) emit these; the storage layer turns them into
    :class:`EntryRecord` rows.
    """
    guid: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    image_urls: list[str] = field(default_factory=list)
    # Canonical page for playable media (a YouTube / Vimeo watch URL). The
    # reader rebuilds a player from it; we deliberately never store the
    # provider's own <iframe> (see providers/arena.py).
    embed_url: str | None = None
    # A downloadable document the post is *about* (an Are.na Attachment —
    # nearly always a PDF). Distinct from embed_url: this one is opened, not
    # played, and the reader must not treat its cover page as a gallery image.
    file_url: str | None = None
    # A media file the reader plays inline with a native <video>/<audio> — a
    # Mastodon attachment, a podcast enclosure. A third thing from the two
    # above and not expressible as either: ``embed_url`` means "a provider
    # page we can rebuild a player for" and ``file_url`` means "open this
    # somewhere else". Before ISSUE-356 a direct media URL had nowhere to go
    # and was filed as an image, which the reader rendered as a broken <img>.
    media_url: str | None = None
    media_type: str | None = None       # e.g. 'video/mp4', 'audio/mpeg'
    published_at: str | None = None     # ISO 8601 UTC


@dataclass
class FetchResult:
    """Outcome of polling one feed."""
    feed_url: str
    items: list[FetchedItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    discovered_title: str | None = None
    discovered_site_url: str | None = None
    # A 429. Deliberately not folded into ``error``: a throttled feed is
    # healthy, and recording it as an error is what made a transient throttle
    # and a dead feed read identically in diagnostics. ``retry_after_seconds``
    # is what the server named, or None when it named nothing.
    rate_limited: bool = False
    retry_after_seconds: int | None = None


def _as_text(value: object) -> str:
    """A trimmed string from a value that arrived off a row, a payload or argv.

    Every reader below takes ``object`` rather than ``str`` on the convention
    `kv_namespaces.is_reserved_namespace` sets: callers pass values straight
    off a database row, a JSON body or a TOML document, and a `.strip()` on an
    int is an unhandled 500 on the settings save. Falsy is empty, which is what
    the `str(x or "")` these replaced already did.
    """
    if isinstance(value, str):
        return value.strip()
    return str(value).strip() if value else ""


def detect_source_type(url: object) -> str:
    """Classify a feed URL by how the poller should fetch it."""
    lo = _as_text(url).lower()
    if lo.startswith("tumblr:"):
        return "tumblr"
    if lo.startswith("arena:"):
        return "arena"
    return "rss"


# Neither identifier can hold a query or a fragment, and neither marker is
# inert once it reaches the path: `httpx` replaces the query wholesale from
# `params=`, so `arena:slug?x=1` polls the channel-metadata endpoint and comes
# back 200 carrying the wrong body rather than failing. Cut rather than
# rejected, because the shape comes from a half-pasted URL and the part in
# front of the marker is what was meant.
_IDENTIFIER_STOPS = ("?", "#")


def _normalize_provider_identifier(identifier: object, provider: str) -> str:
    """The bare channel slug / blog name, whatever the user typed.

    Returns ``""`` when nothing usable is left, which is what the add seams
    reject on rather than storing.

    The two providers take different identifiers, so a pasted URL is read
    differently for each: an Are.na channel is a **path segment** and a Tumblr
    blog is a **host**. Extraction is not gated on the host being are.na or
    tumblr.com — anything carrying a scheme is already not a valid identifier,
    so there is nothing to preserve by leaving it alone.

    What this does **not** claim is that the result is safe to interpolate. It
    is a canonicalizer, and the guard against an identifier steering the
    request is `quote(..., safe="")` at the two provider call sites, where the
    string actually meets a URL.
    """
    ident = _as_text(identifier)
    if "://" in ident:
        try:
            parts = urlsplit(ident)
        except ValueError:
            return ""
        ident = (parts.hostname or "") if provider == "tumblr" else parts.path
    for stop in _IDENTIFIER_STOPS:
        ident = ident.split(stop, 1)[0]
    if provider == "arena":
        # An Are.na slug never contains a slash, so the channel is the last
        # non-empty path segment however it arrived. One rule covers the stray
        # leading slash this was filed for (ISSUE-432), a trailing one, and a
        # pasted https://www.are.na/<user>/<slug>.
        segments = [seg for seg in ident.split("/") if seg.strip()]
        ident = segments[-1] if segments else ""
    ident = ident.strip().strip("/").strip()
    if provider == "tumblr" and "/" in ident:
        # A Tumblr identifier is a blog name or a host, and neither holds a
        # slash. Something that does is a path, not a blog.
        return ""
    if ident in (".", ".."):
        # The segment rule above can produce these out of `arena:a/..`, so the
        # normalizer has to refuse what it can itself construct. Nothing on the
        # far side of a relative segment is a channel.
        return ""
    return ident


def normalize_feed_url(url: object) -> str:
    """Canonical stored form of a feed URL or ``provider:`` identifier.

    Returns ``""`` when nothing usable is left — the caller rejects rather than
    storing it, since a stored identifier that can never resolve fails exactly
    like a dead channel and there is nothing in the failure that says which it
    is (ISSUE-432).

    Only the identifier half of a provider URL is rewritten. A plain RSS URL is
    stripped of surrounding whitespace and otherwise left alone: its path
    slashes are part of the address, so the slash rule must not reach it.

    The scheme is lower-cased. `feeds.url` is `TEXT NOT NULL UNIQUE` with no
    `COLLATE NOCASE` and every lookup is a binary `=`, so `Arena:x` and
    `arena:x` are two rows polling one channel — the duplicate this function
    exists to prevent, and the reason preserving the typed case was wrong. A
    row already stored under a mixed-case scheme is found through
    `db.stored_url_variants`, the same route a pre-ISSUE-432 identifier takes,
    so re-importing an OPML export updates that row instead of adding a second.
    """
    text = _as_text(url)
    if not text:
        return ""
    for scheme in PROVIDER_SCHEMES:
        if text.lower().startswith(scheme):
            ident = _normalize_provider_identifier(text[len(scheme):], scheme[:-1])
            return f"{scheme}{ident}" if ident else ""
    return text


def provider_identifier(url: object) -> str:
    """Strip the ``provider:`` scheme to get the bare identifier.

    Normalized on the way out as well as at the add seams, and the second pass
    is not redundant: a row stored before ISSUE-432 still holds whatever was
    typed, and this is what lets it fetch without a data migration.
    """
    text = _as_text(url)
    for scheme in PROVIDER_SCHEMES:
        if text.lower().startswith(scheme):
            return _normalize_provider_identifier(text[len(scheme):], scheme[:-1])
    return text


# Media a browser can play inline from a plain URL, by file extension.
# Consulted when a feed's MIME type and ``medium`` between them say nothing we
# recognise — Mastodon ships both, but plenty of feeds ship a bare
# ``<media:content url="…mp4"/>`` and the extension is then the only evidence
# there is. Deliberately short: every entry here is a format the
# <video>/<audio> element actually plays, so a match is a promise the reader
# can keep. Anything unlisted falls through to the caller's own default, which
# for a ``media:content`` is "image" — what the poller did with everything
# before ISSUE-356.
#
# ``.ogg`` is deliberately absent though ``.oga`` and ``.ogv`` are here: Ogg is
# a container, not a format, and ``video/ogg`` is registered, so guessing audio
# from it would put a Theora video in an <audio> element and lose the picture
# silently. That is the failure `inlineMedia` refuses on the other side; better
# to offer no player than the wrong one.
#
# Mirrored in TypeScript as PLAYABLE_EXTENSIONS in web/src/lib/feeds/embed.ts.
# tests/test_feeds_media_parity.py holds the two together — do not edit one
# without the other.
PLAYABLE_MEDIA_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "ogv": "video/ogg",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "aac": "audio/aac",
}


def media_type_for_url(url: str) -> str | None:
    """MIME type for a playable media URL, by extension. ``None`` otherwise.

    The path is parsed rather than string-matched so a query string can't
    decide the answer: ``…/photo.jpg?poster=clip.mp4`` is a photo. An extension
    is the tail of the *last* path segment, so ``…/clip.mp4/thumb`` is not a
    video either. The TypeScript half of this rule is ``inlineMedia``'s
    extension fallback; they are held equal by a parity test.
    """
    if not url:
        return None
    from urllib.parse import urlsplit

    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    # `;params` are part of the path in a URL and are not part of a filename.
    segment = path.rpartition("/")[2].partition(";")[0]
    name, dot, ext = segment.rpartition(".")
    # `name` empty with a dot means a leading-dot filename (".mp4"), which is a
    # name, not an extension.
    if not dot or not name or not ext:
        return None
    return PLAYABLE_MEDIA_TYPES.get(ext.lower())


def is_http_url(url: str) -> bool:
    """Whether a URL is one we are willing to put in a browser ``src``.

    http(s) only — the bar the feed sanitizer already applies to markup, kept
    here for the URLs that arrive as feed *attributes* rather than as markup
    and so never pass through it. Both the poller (on the way in) and the v7
    migration (over URLs stored before there was a check) use this one.
    """
    lo = (url or "").strip().lower()
    return lo.startswith("http://") or lo.startswith("https://")


def parse_image_urls(raw: Any) -> list[str]:
    """Coerce the DB ``image_urls`` column (JSON or empty) into a list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        import json
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return [raw] if raw else []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return [str(parsed)]
    return []
