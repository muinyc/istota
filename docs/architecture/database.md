# Database

Istota uses SQLite with WAL mode for concurrent access. All operations live in `db.py`. The schema is defined in `schema.sql`.

## Tables

### Core

| Table | Purpose |
|---|---|
| `tasks` | Task queue with full lifecycle: id, status, source_type, user_id, prompt, conversation_token, talk_delivery_token, priority, attempts, `last_heartbeat` (worker-liveness ping for stuck-task reclaim, ISSUE-112), execution trace, model/effort overrides, plus `skill` / `skill_args` for skill-task dispatch |
| `user_resources` | Per-user folder mounts (`folder`) + internal `shared_file` organizer state |
| `user_profiles` | Per-user profile fields (display_name, timezone, channels, worker overrides, disabled_skills, disabled_modules, email_addresses, trusted_email_senders) |
| `briefing_configs` | Briefing schedule + delivery (cron, conversation_token, `output`, enabled flag). Content lives in the per-user briefings module DB, not here |
| `secrets` | Per-user encrypted credentials (Fernet over scrypt-derived `ISTOTA_SECRET_KEY`) |
| `google_oauth_tokens` | Google OAuth access/refresh token pairs (Fernet-encrypted at rest) |
| `web_user_tokens` | Retained user-scoped Nextcloud OAuth pairs for post-as-user Talk mirroring; encrypted with the web-only `ISTOTA_WEB_TOKEN_KEY` (distinct salt + table from `secrets`, so "who can decrypt" stays greppable) |
| `task_logs` | Structured task-level observability |
| `task_steers` | Mid-run steering notes for a running task (`!steer`): per-task monotonic `seq`, `pending` / `consumed` / `dropped`, plus who steered and from which surface |
| `istota_kv` | Per-user key-value store for script runtime state |
| `shared_kv` | Cross-user namespaced JSON store. Reads are open; writes are admin-only and fail closed. Substrate for curated shared briefing content |
| `_migration_state` | Markers for one-time data migrations, so each runs exactly once |

### Messaging

| Table | Purpose |
|---|---|
| `talk_poll_state` | Last message ID per Talk conversation |
| `talk_messages` | Poller-fed message cache for conversation context |
| `processed_emails` | Email dedup with RFC 5322 thread tracking |
| `email_poll_state` | Inbound poll cursor, one row per polled folder: `folder, uidvalidity, last_uid, updated_at`. Each tick takes the oldest `email_poll_batch_size` UIDs *above* `last_uid`, so the batch is a boundary rather than a window and a backlog drains instead of truncating. A changed `uidvalidity` means the mailbox was recreated and UIDs restarted, so the cursor is meaningless and resets (ISSUE-250) |
| `sent_emails` | Outbound email tracking for emissary thread matching |
| `trusted_email_senders` | Per-user fnmatch allowlist for the email trust gate, read in both directions (inbound confirmation and outbound approval) |
| `outbound_drafts` | Outbound mail held by the approval gate: recipients, subject, body, headers and origin surface snapshotted at hold time, plus `status` (`pending` \| `sending` \| `sent` \| `discarded`), the room it belongs to and the task that composed it. Released rows carry `sent_message_id`. Never expired — see [answering a held draft](../features/email.md#answering-a-held-draft) |
| `task_events` | Task-event-streaming log: `id, task_id, seq, kind, payload (JSON), created_at`, `UNIQUE(task_id, seq)`. One persisted, typed event stream per task feeding Talk / SSE / log / push consumers. `seq` is monotonic per task (writer-assigned, resumed across retries via `get_max_task_event_seq`); rows are deleted only by `cleanup_old_tasks` (retention) |

### Web chat (per-user rooms)

| Table | Purpose |
|---|---|
| `web_chat_rooms` | One row per web chat room: `id, user_id, token (channel id), name, archived, created_at, updated_at`. One room = one `conversation_token`, each with its own `CHANNEL.md` |
| `web_chat_messages` | Bot-delivered (unsolicited) room messages — alerts / logs / notifications routed to the `web` surface via `WebTransport.deliver`: `id, user_id, token, role, title, text, created_at`. Distinct from task-backed turns; merged into room history by time |

### Rooms (unified Talk/web)

The unified Talk/web room-sync model (defined in `schema.sql`) supersedes the de-facto tasks-as-history store with a surface-neutral room + message model.

| Table | Purpose |
|---|---|
| `rooms` | Canonical room registry keyed on `conversation_token`; `origin` (talk\|web), display name, `archived` flag, plus the standing per-room `model` / `effort` default applied by `record_inbound` |
| `room_bindings` | One row per (room, surface) exposing a room; maps canonical token to each surface's ref |
| `messages` | Canonical transcript (role user\|assistant\|system, `task_id`, `origin_surface`, `external_ids` mirror ledger) |
| `room_members` | Per-user membership of a shared room; web visibility resolves through this, not the single-owner `rooms.user_id` |
| `room_dismissals` | Per-user "hide this room" tombstone, cleared by the user's own next inbound |
| `room_read_state` | Per-surface, per-user read cursors driving unread badges |
| `message_stars` | Per-user starred messages (Talk has no per-message star API, so this is web-only) |
| `message_deletions` | Hard-delete ledger with its own stream cursor, so a reconnecting client learns what vanished while it was away. Pruned at 30 days |

### Notifications

| Table | Purpose |
|---|---|
| `notifications` | The durable open set of what is waiting on a user — the inbox behind the app-bar bell. One row per thing needing attention: `source` (the registered producer), `dedup_key` (its stable identity), an optional `object_type` / `object_id` the resolver validates, `severity`, `actionable`, `title` / `body` / `params`, an in-app `link` checked against an allowlist on every read, `occurrences`, `seen_at`, and `state` (`open` \| `resolved` \| `dismissed` \| `stale`). `UNIQUE (user_id, source, dedup_key)` is what makes a producer idempotent |

Two things about this table are load-bearing and easy to undo by accident.

`last_delivered_at` records only a send that actually reached a destination. `send_notification` returns False when none is configured, and suppressing a re-delivery on the strength of a send that reached nobody is the exact failure the inbox exists to fix.

Ordering is `updated_at DESC`, not `created_at DESC` — hence `idx_notifications_user_state`. A reopen preserves `created_at` and refreshes `updated_at`; under `created_at` ordering an old row reopened today would deliver a push and bump the badge while sorting below fifty newer rows, so it would be unreachable in the panel. Never seen means never closed for an auto-resolving source.

`idx_notifications_object` leads with `user_id`, and so does `resolve_by_object`. Panel ids come from the per-user health module DB, where every user has a panel `12`, so a close path keyed on `(source, object_type, object_id)` alone would resolve every other user's row when one user confirms theirs.

See [notifications and the inbox](../features/notifications.md).

### Scheduling

| Table | Purpose |
|---|---|
| `scheduled_jobs` | Cron job definitions (synced from CRON.md) |
| `briefing_configs` | Briefing schedule + delivery per user |
| `briefing_state` | Last-run timestamps per briefing per user |
| `shared_block_configs` | Admin-managed definitions of module-owned shared briefing blocks (cron, render mode, trust flag, sources JSON). Seeded once from config, DB-authoritative thereafter |
| `briefing_shared_block_state` | Last-run timestamps for shared-block generation (global, not per user) |
| `istota_file_tasks` | Tasks sourced from TASKS.md files (content-hash identity) |

### Memory

| Table | Purpose |
|---|---|
| `sleep_cycle_state` | Per-user nightly memory extraction state |
| `channel_sleep_cycle_state` | Per-channel memory extraction state |
| `memory_chunks` | Text chunks for hybrid search; carries `valid_from` / `valid_until` episode-window columns (ISSUE-109) so a chunk whose episode has closed self-suppresses from recall |
| `memory_chunks_fts` | FTS5 virtual table (trigger-synced from memory_chunks) |
| `knowledge_facts` | Temporal subject/predicate/object triples (freeform predicates, fuzzy dedup); `valid_from` / `valid_until` bound a fact's currency |
| `knowledge_facts_audit` | Append-only audit trail of KG fact add/invalidate/delete ops |
| `user_skills_fingerprint` | Skills version tracking for "what's new" |

### Monitoring

| Table | Purpose |
|---|---|
| `heartbeat_state` | Per-check monitoring state (timestamps, consecutive errors) |
| `reminder_state` | Shuffle queue for briefing reminders |

### Usage

| Table | Purpose |
|---|---|
| `task_usage` | One row per brain attempt — tokens, cost, timing, and the identity to attribute them |
| `task_usage_models` | Per-model split of one `task_usage` row, where the brain reports one |
| `code_review_calls` | Rounds charged per task by the `code_review` skill's own model calls (`task_id, calls, updated_at`), counting rounds whether or not the response parsed. Cascades with the task |

`task_usage` is deliberately **not** foreign-keyed to `tasks`. `cleanup_old_tasks` deletes tasks at `task_retention_days` (7) and a usage row must outlive that, at `usage_retention_days` (180), so `task_id` dangles afterwards. It is NULL outright for the daemon's own model calls, which have no task — the sleep cycle, shared briefing blocks, health OCR, code review — and `origin` names which. The denormalized identity columns (`user_id`, `source_type`, `brain_kind`, `model`) keep every row self-sufficient, so no aggregate ever joins `tasks`. `tasks.id` is `AUTOINCREMENT`, so a dangling `task_id` can never be reassigned.

Two traps in this table. Every date comparison against it uses the ISO-Z format `created_at` stores, **not** the `datetime('now')` idiom `cleanup_old_tasks` uses — `' '` (0x20) sorts below `'T'` (0x54), so mixing the two silently drops boundary-day rows instead of raising. And every token aggregate must filter on `has_totals`: a run killed before its result frame has meaningless zeroes in the token columns.

The `task_usage_models` foreign key is documentation only. Istota never sets `PRAGMA foreign_keys=ON`, so `prune_old_usage` deletes children explicitly and parents second, on one connection.

See [token usage and cost](../features/usage.md) for what is recorded and how it is read back.

### Tracking

| Table | Purpose |
|---|---|
| `monarch_synced_transactions` | Unread. See below |
| `csv_imported_transactions` | Unread. See below |

Invoice timing tables (`invoice_schedule_state`, `invoice_overdue_notified`) live in the per-user money DB (`money/db.py`), not the framework `istota.db`.

Watch the names: `money/db.py` creates its *own* `monarch_synced_transactions`, `csv_imported_transactions`, and `kv_store` in the per-user money DB. **Those are the live ones.** The two framework tables above predate the money module and have had no writer since it arrived with its own copy; the framework code that read and wrote them is gone (ISSUE-427). The tables stay declared because two empty tables cost nothing and dropping them is a migration on deployments that may hold rows. Write nothing new against them.

### Feeds (per-user feeds.db)

| Table | Purpose |
|---|---|
| `feed_categories` | User-defined feed categories |
| `feeds` | Subscribed RSS/Atom/Tumblr/Are.na sources + per-feed poll state |
| `feed_entries` | Aggregated feed content + read/star state, plus `embed_url` (an inline video player) and `file_url` (an attachment such as a PDF) |
| `entry_images` | Repeat-image index (`entry_id`, `image_key`, `seen_ts`) backing the reader's reblog-image suppression |
| `schema_meta` | Schema version, the global default poll interval, and `feeds_settings.image_dedupe_window_days` |

A poll **updates** an entry it has already seen rather than discarding it, so a provider fix or a richer render reaches entries already on file instead of applying only to new ones. User state (`status`, `starred`, `starred_at`) and `fetched_at` are never overwritten — `fetched_at` is the first sighting, which keeps "recently added" ordering and the image-dedupe look-back stable — and a field is only overwritten by a non-empty value, so a sparser re-fetch cannot blank a title. The "N new entries" count still means newly inserted; refreshes are counted separately.

### Location (per-user location.db)

Location tables live in a per-user `location.db`, not in the framework DB. The module package at `src/istota/location/` provides `resolve_for_user(user_id, config)`.

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data from Overland webhook |
| `places` | Named geofences |
| `visits` | Detected place visits |
| `location_state` | Per-user location tracking state |
| `dismissed_clusters` | Clusters the user chose not to save as places |
| `schema_meta` | Schema version |

The two Nominatim caches (`geocode_cache`, `reverse_geocode_cache`) remain in the framework `istota.db` for cross-user dedup.

### Health (per-user health.db)

| Table | Purpose |
|---|---|
| `stats` | Body stat time series (metric, value, unit, date, source) |
| `panels` | Bloodwork panels |
| `biomarkers` | Individual results linked to a panel |
| `biomarker_explainers` | Cached explainer text per (name, direction) |
| `biomarker_refs` | Bundled canonical reference ranges and aliases |
| `encounters` | Visits, procedures, screenings, hospitalizations |
| `diagnoses` | Conditions with status (active, resolved, chronic) |
| `diagnosis_encounters` | Which appointments a condition was seen at (many-to-many; real FKs, cascading) |
| `immunizations` | Vaccine administration records |
| `immunization_refs` | Bundled canonical vaccine list and schedules |
| `documents` | Stored paperwork, one row per blob, deduped by content hash |
| `document_links` | Polymorphic join: which records a document evidences |
| `health_settings` | Profile (DOB, height, sex) and unit display preferences |
| `schema_meta` | Schema version |

Only the `.db` is local; document and panel bytes stay in the workspace on the mount.

### Briefings (per-user briefings.db)

Blocks, their sources, and the archive of rendered results live in a per-user `briefings.db`. Schedule and delivery stay framework-owned in `briefing_configs`.

| Table | Purpose |
|---|---|
| `briefing_blocks` | The blocks a user's briefing is assembled from, in order |
| `briefing_block_sources` | Per-block source rows (kind + config) |
| `briefing_items` | Items gathered for a block |
| `briefing_item_state` | Per-item seen/dismissed state for next-run dedup |
| `briefing_archive` | Rendered briefing results |
| `schema_meta` | Schema version |
 Archived results are pruned by `[briefings] archive_retention_days` on insert, and individually deletable from the web reader.

### Module DB storage

The framework `istota.db` and all five per-user module DBs (feeds, health, location, money, briefings) run **WAL on local disk**, at `Config.module_db_path(user_id, module)` — by default `{db_path.parent}/modules/{user}/{module}.db`. Only the `.db` files are local; user-facing workspace files (health uploads, money ledgers, feeds exports) stay on the Nextcloud mount. Module DBs were moved off the mount because WAL's mmap'd `-shm` file SIGBUSes on the rclone FUSE mount, which had forced them onto `journal_mode=DELETE` and left them with no reader/writer concurrency. `python -m istota.db_relocate` is the one-time idempotent migrator; `db_backup` snapshots the now-local DBs back to dated directories on the mount for off-host durability, and `db_restore` copies them back.

## Key operations

### Task lifecycle

```python
create_task(conn, prompt, user_id, source_type="cli", ...)  # -> task_id
claim_task(conn, worker_id, user_id=None)                    # -> Task | None
update_task_status(conn, task_id, status, result=None, ...)  # completed/failed
set_task_pending_retry(conn, task_id, error, delay_minutes)  # exponential backoff
set_task_confirmation(conn, task_id, confirmation_prompt)     # -> pending_confirmation
cancel_task(conn, task_id)                                    # sets cancel_requested
```

### Conversation history

```python
get_conversation_history(conn, token, exclude_task_id=None, limit=10)
# Returns: list[ConversationMessage(id, prompt, result, created_at, actions_taken)]
```

### Cleanup

```python
expire_stale_confirmations(conn, timeout_minutes)  # -> list of expired tasks
fail_ancient_pending_tasks(conn, fail_hours)        # -> list of failed tasks
cleanup_old_tasks(conn, retention_days)             # -> count deleted
prune_old_usage(conn, retention_days)               # -> count deleted (children first)
```

## Single source of truth for `Task` columns

Every `Task`-returning helper (`claim_task`, `get_task`, `get_pending_confirmation*`, `get_reply_parent_task`, `get_stale_pending_tasks`, `get_completed_*_since`) routes its `SELECT` / `RETURNING` clause through a single `_TASK_COLUMNS` constant. Adding a column means editing one place; missing columns now raise `IndexError` rather than silently returning `None`.

## WAL mode

SQLite WAL mode allows concurrent reads from multiple threads (talk poller, workers, CLI) while the scheduler thread writes. Each worker creates fresh DB connections per call.

## Schema initialization

The schema is applied via `schema.sql`. The CLI command `istota init` creates the database and applies the schema.
