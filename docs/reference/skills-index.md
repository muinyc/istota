# Skills index

All built-in skills shipped with Istota. Skills marked "always" are loaded for every task. Skills marked "doc-only" provide reference documentation without a CLI module.

## Always included

| Skill | Description |
|---|---|
| `files` | File operations in your workspace |
| `sensitive_actions` | Confirmation rules for destructive operations |
| `memory` | Persistent memory writes — USER.md and a room's CHANNEL.md (behavioral) plus the knowledge graph (facts). CLI: append, add-heading, remove, replace, remove-heading, remove-subheading, show, headings. Per-skill overlays are **not** written here (ISSUE-343): they are user-authored files, read on the `skills` CLI |
| `scripts` | User's reusable Python scripts |
| `memory_search` | Memory search CLI (search, index, reindex, stats, facts, timeline, add-fact, invalidate, delete-fact, fact-history) |
| `kv` | Key-value store for persistent runtime state |
| `skills` | On-demand skill loader (`istota-skill skills show <name>` / `list`) — always eager so the model can pull menu skills. Also the read side of [per-skill overlays](../configuration/per-user.md#per-skill-overlays): `overlay <name>` prints one whole, `overlays` is the inventory (skill, bytes, lines, first line, whether it binds, and why not) |

## Communication

| Skill | Keywords | CLI |
|---|---|---|
| `email` | email, mail, send, inbox, reply, message | yes -- list, read, search, thread, attachments, from-senders, newsletters, send, reply, reply-all, mark, delete, output |
| `nextcloud` | share, sharing, download link, nextcloud, permission, access, capabilities, quota | yes -- capabilities, user, group, share (incl. `share link`), files, talk, notify, activity. Gated on `requires_capability: [nextcloud]` |
| `ntfy` | ntfy, push notification, notify me, notify my phone, mobile alert | yes -- send (one-way push to the user's ntfy device) |

## Productivity

| Skill | Keywords | CLI |
|---|---|---|
| `calendar` | calendar, event, meeting, schedule, appointment | yes -- list (alias `agenda`), create, update, delete |
| `todos` | todo, task, checklist, reminder, done, complete | doc-only |
| `reminders` | remind, reminder, alert me, notify me | doc-only |
| `schedules` | schedule, recurring, cron, daily, weekly | doc-only |
| `tasks` | subtask, queue, background, later | yes -- status, recent, transcript (admin-only; read your own task state and finished transcripts, queue subtasks) |
| `bookmarks` | bookmark, bookmarks, karakeep, saved, reading list, favourited, favorite | yes -- search, list, add, tags, etc. |

## Information

| Skill | Keywords | CLI |
|---|---|---|
| `briefing` | (auto-selected for briefing source type) | doc-only -- output formatting for a generated briefing |
| `briefings` | briefing, block, digest, briefing source | yes -- list (every briefing name this user has, with block counts and the latest archived generation), blocks list/add/set/reorder/remove, sources list/add/remove, archive list/show |
| `briefings_config` | briefing config, briefing schedule | doc-only -- user-editable briefing schedule in `{bot_dir}/config/` |
| `markets` | market, stock, ticker, index, futures | yes -- quote, summary, finviz |
| `feeds` | feed, rss, subscribe, unsubscribe, opml | yes -- list, categories, entries, add, remove, refresh, poll, prune, run-scheduled, import-opml, export-opml |
| `browse` | browse, website, scrape, screenshot, url | yes -- get, render, screenshot, extract, interact, links, close |

## Media

| Skill | Keywords | CLI |
|---|---|---|
| `transcribe` | transcribe, ocr, screenshot, scan, image | yes -- OCR via Tesseract |
| `whisper` | transcribe, whisper, audio, voice, speech | yes -- transcribe, models, download (faster-whisper, CPU int8) |
| `notes` | note, save, write, markdown | doc-only (companion to transcribe) |

## Development

| Skill | Keywords | CLI |
|---|---|---|
| `developer` | git, gitlab, github, repo, commit, branch, MR, PR, worktree, clone | doc-only (env setup via hook) |
| `commit` | commit, commit message, changelog, git commit, staging | doc-only |
| `code_review` | review, code review, review the diff, review before merge | yes -- `run --worktree PATH [--base REF] [--range RANGE] [--intent TEXT] [--agents both\|conformance\|bughunt]`. Admin-only |

`developer` is the entry point and declares `commit` and `code_review` as `companion_skills`, so all three load together on any task that reaches for git. The split is about what each one owns: `developer` covers repository work and the merge-request and pull-request lifecycle, `commit` covers message format, what gets staged and what must never be committed, and `code_review` covers running a review and acting on its findings.

### Forge commands

Git work uses the real `gh` and `glab` binaries, not hand-written REST wrappers. Both run behind `forge_cli.py`, which decides which forge it is from `argv[0]`, checks the argv against a code-owned deny policy, fetches the token from whichever credential socket is present, and execs the real binary with the token in its own environment. `[developer] forge_cli_extra_denied` extends the policy and `forge_cli_permit` punctures it — each entry there removes an accident guard.

The CLIs need wider token scopes than the old wrappers did: GitLab `api` plus `write_repository`, GitHub `repo`. A token scoped for the previous path fails on every forge command.

The two binaries are not interchangeable in their flags. `gh` filters output with `--jq`; the `glab` a standard server install ships does not have it, and reads a field with `-F json` instead. The skill's recipes are written per forge for that reason, and a read whose value feeds a later command aborts rather than passing an empty string on.

### Review before merge

A change large enough to be more than a one-or-two-file edit is reviewed before the merge request opens. A reviewer reads the branch diff against a one-line statement of what the change was meant to do, and the bot fixes what comes back before pushing, reporting anything it disagreed with as a decision rather than dropping it silently.

Two reviewers run on a diff at or above `both_agents_threshold_lines` (150), and on any diff touching a boundary path — credentials, money, migrations, the sandbox, deploy. Smaller diffs get the conformance reviewer alone. A reviewer may ask once for files it was not given, up to `max_need_files`. Where review cannot run at all — the budget is spent, the model is unreachable — the merge request still opens and says it is unreviewed. Caps and models are under [`[developer.review]`](../configuration/reference.md#developerreview).

## Accounting

| Skill | Keywords | CLI | Notes |
|---|---|---|---|
| `money` | accounting, ledger, beancount, invoice, expense, money | yes -- in-process accounting (ledger, invoicing, transactions, work log, investment portfolio) | Default-on module — no resource needed; opt out via the user's `disabled_modules`. Operations are also operator-reachable as `istota money <op>` |

## Google Workspace

| Skill | Keywords | CLI |
|---|---|---|
| `google_workspace` | google drive, google docs, google sheets, google calendar, google chat, spreadsheet, gws | yes -- a thin `istota-skill google_workspace …` entry point that execs the `gws` binary |

Requires OAuth connection via the [web dashboard](../features/google-workspace.md). Token injected via `setup_env()` hook.

## Location

| Skill | Keywords | CLI |
|---|---|---|
| `location` | location, gps, where, place, tracking | yes -- current (alias `last`), history, places, learn, update, delete, attendance, reverse-geocode, day-summary, discover, dismiss-cluster, list-dismissed, restore-dismissed, place-stats, import-garmin-tracks |

## Health

| Skill | Keywords | CLI |
|---|---|---|
| `health` | health, weight, bloodwork, labs, biomarker, panel, blood pressure | yes -- log, stats, latest, panels, panel, add-panel, add-biomarker, trend, upload, import-csv, export-csv, summary, settings, set, encounters, encounter, add-encounter, update-encounter, delete-encounter, diagnoses, diagnosis, add-diagnosis, update-diagnosis, resolve-diagnosis, delete-diagnosis, link-encounter, unlink-encounter, history-summary, immunizations, immunization, add-immunization, update-immunization, delete-immunization, vaccine-refs, coverage, import-immunizations, explain-immunization, garmin-status, garmin-sync, garmin-disconnect, documents, document, attach-document, detach-document |

Requires the `health` module to be enabled (on by default).

## Infrastructure

| Skill | Keywords | CLI |
|---|---|---|
| `devbox` | devbox, install package, pip install, compile, dig, nslookup, traceroute, network diagnostic | yes -- exec, exec-file, cp-in, cp-out, status, reset |

## Specs

| Skill | Keywords | CLI |
|---|---|---|
| `spec` | spec, draft spec, design doc, implementation plan | doc-only |

Codifies a spec-driven development workflow. Specs live in `{notes_folder}/Specs/{Drafts,Active,Done}/` by default, or in a named project's folder. Supports drafting, starting, marking done, listing, showing, and editing specs. See the skill body for the full lifecycle and conventions.

## Monitoring

| Skill | Keywords | CLI |
|---|---|---|
| `heartbeat` | heartbeat, monitoring, health check, alert | doc-only |

## Safety

| Skill | Keywords | CLI |
|---|---|---|
| `untrusted_input` | (none — never selected directly) | doc-only |

`untrusted_input` is a doc-only companion skill with no triggers. It loads via `companion_skills` declarations on the ten ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`, `briefings`, `nextcloud`, `tasks`), so its inbound-content security rules ride along whenever a task processes content from outside the trust boundary. It pairs with `sensitive_actions` (outbound rules there, inbound-reading rules here).

## Selection

Skill loading is single-axis: a skill is either **eager** (full body in the prompt) or in the **menu** (a one-line "load on demand" entry the model pulls in full via `istota-skill skills show <name>`). A single deterministic pass produces the eager set from these selectors: `always_include`, `source_types` match, `file_types` match, sticky skills (carried from recent conversation turns), and `companion_skills` of the selected set. Everything else eligible goes in the menu, which is the full eligible catalogue minus the eager set.

Keyword (`triggers`) matching and the former `resource_types` matching are **no longer selectors**. `triggers` survives only as `!skills` documentation; the `resource_types` menu-membership gate was removed in the Resources sunset. The former "progressive disclosure" two-axis model and the LLM Pass-2 pre-router are both gone.

See [skills](../features/skills.md) for details on the selection system.

## Checking availability

Use `!skills` (in Talk or web chat) to see which skills are available, unavailable (missing dependencies), or disabled for your user. Use `!skills <name>` for details on a specific skill.
