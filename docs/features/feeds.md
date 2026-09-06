# Feeds

A native feed reader — RSS, Atom, Tumblr, and Are.na — with its own web tab and per-user SQLite store. There is no external service; the poller, the store, and the reader are all in-tree (`src/istota/feeds/`).

The `feeds` module is on by default. Opt out per user with `disabled_modules = ["feeds"]`.

## Reading

The **Feeds** tab is a masonry card grid with an image/text filter, sort by published or added, grid and list views, a navigable image lightbox, and a click-to-expand reader overlay showing a card's full un-clipped content with `←`/`→` navigation between posts.

The sidebar scopes the view to everything, unread only, a single feed, or a whole category. Per-entry starring is bound to `f`; bulk mark-as-read (`Shift-A`) honours whatever scope is active rather than clearing the whole account. Entries mark themselves read after 1.5 seconds in the viewport.

Video embedded in a post plays inline with ordinary controls. Nothing autoplays — a grid of cards all starting at once is not what scrolling a reader asks for — and the image/text filter hides inline video along with pictures, since a filter that left clips playing would only mean "some of the media". Embeds resolve through a host allowlist (YouTube, youtube-nocookie, Vimeo) rather than passing a provider's own iframe HTML through the sanitizer.

### Repeat images

As a reblogged photo travels through the blogs you follow, the same picture arrives several times. A duplicate inside one post is dropped, and across posts an image a newer entry already showed is hidden on the older ones — the post still appears, with a note counting what was hidden.

This is a display-time decision, never a row mutation, and it is bounded two ways: to a look-back window (`image_dedupe_window_days`, default 14, 0 = off) and to the slice you are currently viewing. An image resurfacing months later still shows, and browsing one blog never hides a tile because of something in another.

## Subscriptions

Manage subscriptions, categories, and OPML import/export from the sprocket-icon settings page, or from the skill CLI:

```bash
istota-skill feeds list                      # subscribed feeds
istota-skill feeds categories                # categories
istota-skill feeds entries                   # entries
istota-skill feeds add URL                   # subscribe
istota-skill feeds remove ID                 # unsubscribe
istota-skill feeds refresh                   # mark feeds due for the next poll
istota-skill feeds poll                      # poll everything due, now
istota-skill feeds prune [--dry-run]         # apply the entry retention policy
istota-skill feeds import-opml PATH
istota-skill feeds export-opml
```

`run-scheduled` is the scheduler's entry point, not something to run by hand. It runs every five minutes and, unlike `poll`, caps its burst at 50 feeds by default; the due list is oldest-first, so a larger backlog drains over consecutive ticks rather than being dropped. `--limit` overrides in both directions.

## Entry retention

Stored entries are bounded by two passes, both applied by `prune` and run daily by the auto-seeded `_module.feeds.prune` job. They protect different things and are not one rule:

- **Age** deletes read and removed entries older than `entry_retention_days` (default 90). Starred entries, unread entries and anything the feed's most recent response still returned are exempt, and a feed is never taken below a floor of 50 entries. Age counts from when the entry arrived in this reader, not from its published date.
- **Maximum** trims each feed to `max_entries_per_feed` (default 5000), keeping the most recently added. This pass does not read status, so an **unread entry can be deleted here** once a feed is over its maximum. Starred entries are the only exemption, and a feed's stars can never take its budget below 50 unstarred entries.

Either pass is switched off by setting its value to `0`. On a database upgraded to schema v8 both are deferred for 90 days; the envelope reports that as `entry_pruning_deferred_until` and every count is zero. `--dry-run` runs both passes and rolls them back, reporting the counts a real run would delete.

## Polling and rate limits

Requests are paced **per host**, not per feed. A feed's poll interval says nothing about how many requests reach one host at once, and every Are.na channel you have is one request to `api.are.na` (every Tumblr blog one to `api.tumblr.com`), so a due list of twenty channels used to go out as twenty back-to-back requests and the tail of it was reliably turned away. A minimum gap of 2 s now separates two requests to the same host. RSS pays nothing for it — dozens of distinct origins — and two RSS feeds that genuinely share an origin are paced together, which is correct for the same reason. A second cap bounds the sleeping itself at 60 s per run: the poll is a background task holding one worker slot, and on exhaustion the run stops rather than un-pacing itself, leaving the rest due for the next tick.

**HTTP 429 is a throttle, not a feed error.** It does not write `last_error`, does not increment `error_count` and does not carry a doubled interval forward once it clears — being turned away for asking too often says nothing about whether the feed is broken. It is recorded in its own right instead: `feeds.last_throttled_at` (added in schema v6), cleared by the next successful fetch so the column means "throttled now" rather than "throttled once". `GET /api/feeds/config` reports it as a `throttled_feeds` count beside `error_feeds`, and per feed as `last_throttled_at`; the settings page does not render either yet. `last_fetched_at` is written back unchanged, because nothing was fetched.

The standoff never schedules a throttled feed sooner than a healthy one would poll: it floors at the largest of the feed's own interval, the 30-minute default and a 60-minute rate-limit backoff, and a server-named `Retry-After` is taken only where it is longer than that floor, capped at 6 hours. Every `next_poll_at` — success and failure alike — is multiplied by a random factor within ±10%, so a group of feeds refreshed together drifts apart instead of bursting, failing and rescheduling in lockstep forever.

A poll run reports `throttled` alongside `errors`, and per feed `rate_limited` and `retry_after_seconds`; a run that was turned away for every feed exits non-zero rather than reading as a clean poll that found nothing.

The known gap is that none of this spans users: a poll is a per-user subprocess, so two users reach one host from one IP with no shared budget.

Are.na runs on the v3 API. Six block types have typed builders (`Text`, `Image`, `Link`, `Embed`, `Attachment`, `Channel`) over a generic fallback, so a type Are.na adds later still renders rather than breaking the poll.

## Storage

Per-user `feeds.db` at `Config.module_db_path(user_id, "feeds")`, with tables `feed_categories`, `feeds`, `feed_entries`, `entry_images`, and `schema_meta` (schema v8). Settings live as rows keyed under `feeds_settings.*` — `default_poll_interval_minutes`, `image_dedupe_window_days`, `entry_retention_days` and `max_entries_per_feed`.

A poll **refreshes** an entry it already holds rather than discarding it, so a provider-side fix reaches entries already on file. User state (read status, starred, starred timestamp) is never overwritten, and a field is only replaced by a non-empty value, so a sparser re-fetch cannot blank a title you already have. The "N new" count still counts only genuinely new inserts.

## Related

- [Briefings](briefings.md) — an `rss` briefing source reads from the same subscriptions.
- [Web interface](web-interface.md) — where the Feeds tab sits in the nav.
