---
name: feeds
triggers: [feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml]
description: Native RSS/Atom/Tumblr/Are.na feed manager (in-tree)
cli: true
companion_skills: [untrusted_input]
env: [{"var":"FEEDS_USER","from":"user_id"},{"var":"TUMBLR_API_KEY","from":"secret","service":"feeds","key":"tumblr_api_key","sensitive":true,"fallback_var":"TUMBLR_API_KEY"}]
---
# Feeds (native)

Manage RSS/Atom/Tumblr/Are.na feed subscriptions through the in-tree feeds module. Subscriptions, categories, entries and read state live in a per-user SQLite database the CLI opens for you; exports and other files stay under `{workspace}/{BOT_DIR}/feeds/`. The database itself is on local disk outside your sandbox — go through this CLI, there is nothing to read directly.

## CLI

Run `istota-skill feeds --help` for the live list. Output is JSON.

```bash
istota-skill feeds list                                  # List subscriptions
istota-skill feeds categories                            # List categories
istota-skill feeds entries [--status unread|read|removed] [--feed-id N] [--category SLUG] [--limit N] [--offset N] [--before UNIX_TS]
istota-skill feeds add --url URL [--title T] [--category SLUG] [--poll-interval-minutes N]
istota-skill feeds remove --url URL                      # or --id N
istota-skill feeds refresh [--id N]                      # Clear next_poll_at to mark feeds due now
istota-skill feeds poll [--limit N]                      # Poll every feed whose next_poll_at is past
istota-skill feeds run-scheduled [--limit N]             # Scheduler module-job; caps its burst at 50 by default
istota-skill feeds prune [--dry-run]                     # Apply the entry retention policy
istota-skill feeds import-opml PATH                      # Import OPML; rewrites bridger URLs
istota-skill feeds export-opml [--output PATH]           # Export as OPML 2.0 (stdout without --output)
```

## URL schemes

- `https?://...` — RSS/Atom feed (parsed via `feedparser`).
- `tumblr:USERNAME` — Tumblr blog via the API v2 provider.
- `arena:CHANNEL_SLUG` — Are.na channel via the Are.na API provider.

Both OPML paths are host paths and must be **inside your own workspace** — `$NEXTCLOUD_MOUNT_PATH/Users/$ISTOTA_USER_ID/...`. Anywhere else is refused and nothing is read or written.

OPML imports automatically rewrite bridger URLs (`http://127.0.0.1:8900/{provider}/{id}/feed.xml`) to the bare `{provider}:{id}` form so old exports import cleanly on fresh machines.

## Environment variables

| Variable | Description |
|---|---|
| `FEEDS_USER` | Istota user id (set by the executor) |
| `TUMBLR_API_KEY` | Tumblr API v2 key (optional). Stored per user in the encrypted secrets store under service `feeds`, key `tumblr_api_key` — set it with `istota secret ensure -u USER --service feeds --key tumblr_api_key --value ...` or from the feeds settings page. The manifest resolves it (`from: secret`) and the skill proxy injects it here, so this variable is what the skill reads; a `TUMBLR_API_KEY` in the daemon's own environment is the declared fallback when nothing is stored. |

## Notes

- The per-user SQLite is the only source of truth. `add` / `remove` mutate it directly. Don't read or write `feeds.toml` — any pre-existing file gets imported once on first touch then stops being read.
- `run-scheduled` runs every five minutes via the `_module.feeds.run_scheduled` job that the scheduler auto-seeds when the user has the feeds module enabled. It polls at most `DEFAULT_SCHEDULED_POLL_LIMIT` (50) feeds per run unless `--limit` says otherwise; the due list is oldest-first, so a backlog drains over consecutive runs.
- Requests are paced per host (2 s minimum gap), so a set of Are.na or Tumblr feeds does not go out as one burst. An HTTP 429 is reported as a throttle, not a feed error: the run's JSON carries `throttled` beside `errors`, each feed carries `rate_limited` and `retry_after_seconds`, and a run that was rate-limited on every feed exits non-zero. Don't read `status: ok, errors: 0` as "everything fetched" without looking at `throttled`.
- `prune` runs once a day via the `_module.feeds.prune` job, seeded the same way, so you rarely need to call it by hand. It makes two passes, and they protect different things — don't describe them to a user as one rule:
  - **Age.** Deletes read and removed entries whose age is past the retention window. Starred entries, unread entries, and anything the feed's most recent response still returned are never deleted here, and a feed is never taken below a floor of 50 entries (or below the configured maximum, when that is lower). Age counts from when the entry was added to this reader, not from when it was published.
  - **Maximum.** Trims each feed to its configured maximum stored entries, keeping the most recently added ones. It does not read status at all, so an **unread entry can be deleted here** once the feed is over its maximum, ahead of a newer read one. Starred entries are the only exemption, and a feed's stars can never take its budget below 50 unstarred entries (or below the configured maximum, when that is lower) — a feed whose stars fill its maximum goes on storing new entries and stands above the maximum — by its stars, and by the unstarred entries that floor keeps. This pass honours no floor on total rows.
- On a database upgraded to schema v8 both passes are deferred for 90 days; the envelope reports that as `entry_pruning_deferred_until` and every count is zero. `--dry-run` runs both passes and rolls them back, so it reports the counts a real run would delete and changes nothing.
