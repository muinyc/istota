# Istota - Claude Code Bot

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (SSH, installed at `/srv/app/istota`).

This file is loaded into every session, so it stays an index: what exists, where it lives, and the rules that decide a change before it is written. The reasoning behind each rule lives in `.claude/rules/`, loaded on demand.

Subsystems:

- `brain.md` — Brain protocol + ClaudeCodeBrain + NativeBrain (in-process agent loop), plus per-room brain selection
- `executor.md` — `execute_task()`, env mapping, prompt assembly, security
- `prompts.md` — the two halves of a task prompt, and the control directory that hands them over
- `scheduler.md` — daemon loop, worker pool, DB tables, deferred ops
- `config.md` — every dataclass field + TOML mapping
- `skills.md` — skill metadata, single-axis selection (eager vs menu), per-skill user overlays, CLI modules
- `transport.md` — Transport seam over messaging surfaces (Talk + email; Matrix / web chat designed-for), plus the room model
- `web-chat.md` — web chat surface: rooms, composer, drafts, send durability, message replies, room-event stream
- `web-ui.md` — web UI backend: route/endpoint map, admin Logs + Configuration panes, settings/module-services split
- `notifications.md` — the notifications table, the resolver seam, and the six shipped sources
- `briefings.md` — block/source briefings, shared blocks, titles, HTML email
- `health.md` — health module schema, documents store, OCR/explainer, serialisers, surfaces
- `location.md` — GPS pings, place detection, visits, Overland/Garmin ingest
- `feeds.md` — native RSS/Atom/Tumblr/Are.na poller, per-user SQLite, image dedupe
- `money.md` — quarterly tax estimator, portfolio snapshots, classifications
- `memory.md` — USER.md/CHANNEL.md, per-skill overlays, knowledge graph, playbooks, sleep cycle

Boundaries and operations:

- `sandbox.md` — the security posture in full, plus the modules that build and enforce a task's runtime: `task_env`, the tool server, cgroups, the credential splits, path scoping, git hardening
- `devbox.md` — the development container: what it is, the exec transport, the proxy
- `maintenance.md` — backups, migrators, the worktree and cache sweepers, host pressure, session transcripts
- `doctor.md` — the runtime self-check registry, and what each check may and may not do
- `install.md` — the standalone single-user install: the wizard's boundaries and the updater
- `deployment.md` — Ansible role, Docker stack, the Nextcloud rclone mount
- `testbed.md` — `testbed/`: the two compose shapes, profiles, the `Service` protocol, session-scoped reset, the prompt goldens
- `testing.md` — why each verification rule below is what it is
- `committing.md` — the two pre-commit scans and how they fail
- `leaf-modules.md` — single-purpose modules that belong nowhere else

## Project Structure

```
src/istota/
├── brain/                # Pluggable model invocation (Brain protocol)
├── memory/               # search.py, knowledge_graph.py, sleep_cycle.py, curation/
├── skills/               # 36 self-contained skills (skill.md + optional CLI)
├── cli.py                # Local CLI (task, resource, briefing, secret, user, run, serve, setup, …)
├── serve.py              # Combined local launcher (`istota serve`): scheduler thread + uvicorn in one process
├── setup_wizard.py       # Interactive first-run installer (`istota setup`) → install.md
├── updater.py            # `istota update` self-update for the standalone install → install.md
├── config.py             # TOML loader + DB-overlay (user_profiles / user_resources / briefing_configs)
├── config_mapper.py      # Maps a parsed TOML document onto the `Config` dataclass tree → leaf-modules.md
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (framework tables)
├── db_health.py          # `PRAGMA quick_check` + self-healing `REINDEX` backstop for local SQLite DBs
├── db_relocate.py        # One-time migrator: per-user module DBs from the mount → local disk, DELETE→WAL
├── db_backup.py          # Timed online-backup snapshot of local DBs to dated dirs on the mount; retention, row-count collapse guard, 0700/0600
├── db_restore.py         # Restore a cold snapshot back to local disk (newest good, or `--date`); refuses an empty snapshot without `--force`
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── executor_stream.py    # `TaskStreamAdapter`: the brain's `StreamEvent`s adapted to `TaskEvent`s → leaf-modules.md
├── task_env.py           # `build_task_runtime`: one task's env, the credential split, the proxies, the bind list → sandbox.md
├── events.py             # Task event streaming: TaskEvent, EventWriter, EventSubscriber + task_events log
├── consumers/            # Event consumers: TalkEventSubscriber, LogChannelSubscriber, PushNotificationSubscriber
├── scheduler.py          # Task processor, briefings, all polling
├── transport/            # Transport seam: IncomingMessage, registry, ingest, routing, talk/ email/ ntfy/ istota_file/ repl/ web/
├── surfaces.py           # What role each surface plays in the room model, in one table → leaf-modules.md
├── email_support.py      # Shared non-transport email plumbing (get_email_config, thread helpers, cleanup)
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── host_pressure.py      # Host memory instrumentation: PSI/meminfo/tmpfs, shmem attribution → maintenance.md
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── garmin_routes.py      # Module-agnostic Garmin auth router (/api/garmin/*), shared by Health + Location
├── web_app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
├── web_shutdown.py       # Whether the web process is stopping, where the three SSE generators can see it → leaf-modules.md
├── web_router_stubs.py   # The auth/CSRF stubs and the user-context factory every module router shares → leaf-modules.md
├── usage.py              # Normalized per-attempt token/cost telemetry → leaf-modules.md
├── usage_render.py       # The cost render rule for token-usage surfaces → leaf-modules.md
├── subscription_usage.py # The Claude Code plan's rate-limit windows: one fetch, one disk cache → leaf-modules.md
├── doctor.py             # Runtime self-check: every environmental fact istota depends on → doctor.md
├── map_basemap.py        # Where the map's background tiles come from → leaf-modules.md
├── admin_logs.py         # Read-only log sources for the admin UI: the rotating app log (+ rotation chain, paged reader, live tail) and `task_logs`
├── admin_config_view.py  # Redacted, sectioned rendering of the loaded Config for the admin UI (credentials never leave the process)
├── sqlite_util.py        # One SQLite open, each caller's pragma set as parameters; no journal_mode, deliberately → leaf-modules.md
├── du.py                 # Du-style tree measurement and the first-level directory scan → leaf-modules.md
├── rclone_client.py      # The rclone API `storage` and the files skill each had a copy of → leaf-modules.md
├── secrets_store.py      # Encrypted credential store (Fernet via scrypt-derived key)
├── secret_schema.py      # Shared service/key schema for `istota secret` CLI + web UI
├── google_scopes.py      # The Google service ↔ OAuth scope table, bounded by the operator's configured ceiling
├── modules.py            # MODULE_NAMES (feeds, money, location, health, briefings) + EXPERIMENTAL_MODULES (empty)
├── experimental.py       # Operator feature-flag gate (`@requires_feature`, env helpers)
├── user_profiles.py      # Per-user profile store
├── user_briefings.py     # Per-user briefings store
├── notifications.py      # Talk / Email / ntfy dispatcher (delivery), distinct from the inbox below
├── notification_store.py # The `notifications` table: the durable open set behind the bell → notifications.md
├── notification_sources.py  # The resolver seam: rows, views, actions, the registry → notifications.md
├── notification_resolvers/  # One module per source: id, dedup key, producer helpers, resolver → notifications.md
├── claude_runtime_env.py # What a task env carries only because the outer process is the `claude` CLI → sandbox.md
├── image_sniff.py        # Which bytes `/chat/files` will serve `inline` on the app's own origin → leaf-modules.md
├── ntfy_headers.py       # RFC 2047 encoding for ntfy header values (stdlib-only leaf, shared by transport + skill)
├── kv_namespaces.py      # Which `istota_kv` namespaces the model may not touch → sandbox.md
├── git_hardening.py      # The `-c` overrides that stop a repository's own config running a program → sandbox.md
├── git_remote_scrub.py   # Strips credentials out of the git configs under `developer.repos_dir` → sandbox.md
├── repos_relocate.py     # One-shot migrator: `developer.repos_dir` → per-user subtrees → maintenance.md
├── skill_proxy.py        # Unix-socket proxy for credential isolation
├── tool_server.py        # The native brain's tool server, one process per task attempt, inside the sandbox → sandbox.md
├── tool_server_protocol.py  # The wire format between the two, stdlib-only → sandbox.md
├── worktree_reaper.py    # Removes a developer worktree once its work has landed → maintenance.md
├── sandbox_cache_sweeper.py # Bounds the on-disk package caches the sandbox keeps per user → maintenance.md
├── session/session_log.py       # Append-only JSONL transcript of one NativeBrain task attempt, and its sweep → maintenance.md
├── session/session_log_read.py  # Reading a transcript back: one set of parsing rules, two consumers → maintenance.md
├── user_scope.py         # Scoping a user id under a root, in one place → sandbox.md
├── skill_host_paths.py   # Host-path allowlist for the skill CLIs that take one → sandbox.md
├── task_cgroup.py        # A cgroup v2 group per task: memory.max, pids.max, cpu.max → sandbox.md
├── shell_exec.py         # How a command string becomes a shell argv, with `pipefail` on → sandbox.md
├── process_group.py      # `kill_process_group(pid, sig)`: signal a subprocess and its descendants → sandbox.md
├── network_proxy.py      # CONNECT proxy for network isolation
├── forge_cli.py          # The `gh` / `glab` wrapper: deny policy + server-side token injection → sandbox.md
├── devbox_proxy.py       # Per-user host-side daemon: git credentials and the forge token injected server-side
├── devbox_proxy_protocol.py # Wire protocol for devbox_proxy (single-line JSON, 16 MiB cap)
├── devbox_exec_protocol.py  # The exec transport's wire format → devbox.md
├── devbox_exec_client.py    # The other end, copied into each task's shim directory → devbox.md
├── nextcloud_api.py      # NC user metadata
├── provision_rooms.py    # Default Talk rooms (general/logs/alerts) for a user → leaf-modules.md
├── nextcloud/            # OCS + WebDAV client: _http (ocs_request/dav_request/OcsError/path scoping), capabilities, shares, users, dav, notifications
├── nextcloud_client.py   # Back-compat shim: the None-returning variants four best-effort daemon paths depend on
├── storage.py            # Bot-managed Nextcloud storage
├── briefings/            # Block/source briefings module — DB, source resolvers, generation, reader/settings routes, migration
├── feeds/                # Native RSS/Atom/Tumblr/Are.na — poller, SQLite, routes, OPML, image_dedupe
├── health/               # Body stats, bloodwork, biomarker trends, encounters, immunizations, Garmin, OCR
├── location/             # Per-user location.db module (pings, places, visits, state, migration)
├── location_logic.py     # Place stats / cluster discovery (shared web ⇄ skill)
├── scheduler_deferred.py # Deferred-op replay (subtasks, KG, KV, health_ops, …)
├── shared_file_organizer.py
├── commands.py           # surface-agnostic !command dispatch (CommandContext + registry push/stream)
├── toml_fence.py         # Where a TOML fence starts and ends, for the four markdown-config parsers → leaf-modules.md
├── llm_json.py           # The same, for a fence in *model* output; anchored closer, linear walk → leaf-modules.md
├── date_parse.py         # Loose date parsing for text a model or a person typed, validated → leaf-modules.md
├── cron_loader.py        # CRON.md → DB sync
└── logging_setup.py
```

Alongside `src/`: `config/` (config.toml, persona.md, emissaries.md, system-prompt.md, guidelines/ — read by the daemon, never bound into the sandbox; skill bodies live in `src/istota/skills/`), `deploy/ansible/`, `docker/` (full-stack compose), `web/` (SvelteKit, adapter-static, base `/istota`), `tests/`, `testbed/` (the deployment tiers' staging environment; its own `pyproject.toml`, never imported by `src/istota/` — see `.claude/rules/testbed.md`), `schema.sql`.

## Key Concepts

### Identity

- Technical IDs (package, env vars, DB, CLI): always `istota`.
- User-facing identity: `bot_name` config (default "Istota"). `bot_dir_name` sanitizes for filesystem use.
- Templated docs use `{BOT_NAME}`, `{BOT_DIR}`, `{user_id}` placeholders.

### Prompt Layers

1. **Emissaries** (`config/emissaries.md`) — constitutional principles, global only.
2. **Persona** (`config/persona.md` or user `PERSONA.md`) — character.
3. **Custom system prompt** (`config/system-prompt.md`, opt-in) — replaces CC default.

**A task prompt is two halves, split by authority** (`executor.ComposedPrompt`). Standing instructions — identity, execution constraints, emissaries, persona, workspace layout, tool descriptions, rules, response guidelines, skills changelog, eager skill bodies — are the `system` half. Retrieved memory, knowledge facts, playbooks, conversation and confirmation history, the request and its attachments are the `user` half, which is what a compaction summary carries forward. The handoff is a file in `{temp_dir}/.control/{user_id}/task_{id}/`, a directory no task can write and only this task can read. Two rules are held by tests: no line in the system half may point at material in the user half, and every scalar interpolated into a system header goes through `_one_line()`. Full rules in `.claude/rules/prompts.md`.

### Admin / Non-Admin Isolation

Admin user IDs in `/etc/istota/admins` (empty = all admin). Non-admins: scoped mount, no DB write, no subtasks, `admin_only` skills filtered.

### Nextcloud Layout

```
/Users/{user_id}/{bot_name}/{config,exports,scripts,examples}/
/Users/{user_id}/{inbox,memories,shared}/
/Channels/{conversation_token}/{CHANNEL.md,memories/}
```

### Scheduled Jobs (CRON.md)

Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides, and a per-job `brain` — admin-only, dropped at sync for anyone else, bounded by `[brain] room_selectable`. Auto-disable after 5 consecutive failures — recorded in `scheduled_jobs.auto_disabled_at`, which the file cannot express and the sync never writes, so CRON.md saying `enabled = true` does not bring a suspended job back. Three things lift it: a successful run, `!cron enable`, and an edit in CRON.md to what the job dispatches (`cron`, `prompt`, `command`, `skill`, `skill_args`). `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Heartbeat

`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### Which brain a task runs

`tasks.brain` > `[brain.source_type_overrides][source_type]` > `[brain] kind`, resolved by `brain.resolve_brain_kind`. The column has two producers: `rooms.brain` at task creation (Talk and web only) and `scheduled_jobs.brain` from a CRON.md job. The column is what makes a room or a file edited mid-flight change nothing already running; retries and subtasks inherit it.

A room or a job may only pin a kind the operator listed in `[brain] room_selectable`, which is empty by default, and only an admin may write it — brain kind decides which process holds the agent loop, which credentials it carries and which `SandboxProfile` is built. An unknown or unlisted pin is a WARNING and a fallthrough, never a failed task. A pinned room or job has **no availability failover**: `resolve_brain_kind` clears `fallback` on the admission path, so a turn the pinned brain cannot run fails with that brain's own reason. Set from chat with `!brain`, from the web room settings, or per job with `brain` in CRON.md. Full rules in `.claude/rules/brain.md`.

### Security

Full posture, with the reasoning and the shape-by-shape caveats, in `.claude/rules/sandbox.md`. The operative rules:

- **Sandbox** (`bwrap`): per-user filesystem isolation. Linux + bubblewrap is the only supported deployment. The shipped Docker stack grants neither `seccomp:unconfined` nor `systempaths=unconfined`, so it runs every task unsandboxed.
- **The native brain's tools are in the sandbox too**, one namespace per task attempt: `NativeBrain` spawns `istota.tool_server` through `build_bwrap_cmd(..., profile=NATIVE)` over an inherited socketpair, and the six core tools run in there. `executor.native_fs_roots` is still enforced and is the only confinement on the unsandboxed shapes.
- **No databases in the sandbox**: `build_bwrap_cmd` ends by masking `db_path.parent` and `module_db_root()` with an empty read-only tmpfs, after every other mount. Nothing binds the framework DB for anyone, and `sandbox_ro_paths` defaults to `[]`. Reads and writes go through skill CLIs that run host-side and scope by `ISTOTA_USER_ID`; the masks are defence in depth behind that.
- **Session logs are unbound** and a transcript holds the assembled prompt: `{db_path.parent}/logs/{user_id}/` is bound at no path and is in no `native_fs_roots` root. That is an absence rather than a guard, so keep `sandbox_ro_paths` narrow and add no `user_resources` row naming it. A task reads its own finished transcripts through `istota-skill tasks transcript`.
- **One admin's repositories are not in another's sandbox**: `developer.repos_dir` is a root of per-user subtrees and `build_bwrap_cmd` binds `{repos_dir}/{user_id}`, never the root. The package cache is derived inside that subtree, gated on the repos bind's own condition.
- **Config dir out of the sandbox**: emissaries, persona, guidelines and skill bodies become prompt text, so `config/` is never bound. The one exception is `config/system-prompt.md` under `custom_system_prompt`, bound as a single file under the `CLAUDE` profile only.
- **Network proxy**: `--unshare-net` + CONNECT proxy on a Unix socket; allowlist of `host:port`. No MITM.
- **Skill proxy**: strips secret env vars from the model; CLI calls go through a Unix socket that injects credentials server-side. Required wherever the sandbox is — `istota-skill` refuses to run in-sandbox rather than reaching for databases that are not there.
- **Host paths in skill CLIs**: a skill CLI runs host-side with the daemon's filesystem view, so any verb taking a host path is scoped by `skill_host_paths.py` — never `NEXTCLOUD_MOUNT_PATH` whole. Symlinks rejected, callers use the returned resolved path.
- **No Docker API in the sandbox**: no socket is bound at any path and no `DOCKER_HOST` is exported. Project builds go to the user's devbox over the exec transport (`.claude/rules/devbox.md`).
- **Native WebFetch tool**: daemon-netns, credential-free, SSRF-hardened, and **admin-only** whatever `[brain.native.web_fetch]` says.
- **Deferred DB**: sandboxed tasks write JSON to the temp dir; the scheduler processes it after success. Identity (`user_id`, `conversation_token`) always from the task, never the JSON. Subtasks rate-limited and admin-only.

## Code Style

Indentation is **spaces, never tabs**, declared in `.editorconfig` at the repo root: Python is 4 spaces, everything under `web/` is 2 spaces. The frontend is formatted by prettier — run `npm run format` in `web/` before committing frontend changes (config in `web/.prettierrc.json`). Exceptions: `web/package-lock.json` (npm-generated) and `docker/devbox/etc/gitconfig` (git-idiomatic tabs).

Python is **linted but not formatted**. `ruff check` runs clean over the seven paths the command below names; the rule set is pinned in `[tool.ruff.lint]` to ruff's defaults (`E4`, `E7`, `E9`, `F`) with no formatting-adjacent rules. **Do not run `ruff format`**: it is not adopted, the hand formatting in the tree is the baseline, and a reformat would rewrite roughly 715 of 910 files and carry `git blame` with it. A deliberate unused import is marked `# noqa: F401` with the reason, not left to be pruned by the next `--fix` run.

## Verification

There is no single entry point. Run the checks directly, and run only the half the change touches — Python and `web/` are independent. Why each rule below is what it is: `.claude/rules/testing.md`, and `docs/development/testing.md` for the developer-facing version.

Python:

```bash
ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts
scripts/qt                       # the edit loop: only the tests your change affects
scripts/qtest uv run pytest      # the full run before a commit; deselects every marker below
```

Web, from the repo root (needs `npm ci` in `web/` first):

```bash
npm --prefix web run lint:design
npm --prefix web run check       # svelte-check
scripts/qtest npm --prefix web run test    # vitest run
npm --prefix web run format:check
```

- **Install with `uv sync --extra test`.** A bare `uv sync` leaves out everything the suite needs and yields hundreds of collection errors that read as a code regression. `--all-extras` also works and costs about 1.1 GB against the test extra's 291 MB; use it when you want the two heavy ML extras. Test-only dependencies belong in the `dev` group, never in an extra.
- **Use `scripts/qt` while iterating, not a hand-picked subset.** Dependence runs through call chains, not through text: a name-matched test file and a grep of `tests/` both under- and over-select, measured. `qt` wraps pytest-testmon, which records which tests executed which source lines. Do not invoke `--testmon` by hand — this repo's `addopts` carries a `-m` expression and testmon switches its selection off entirely when it sees one, so the run looks selective and is not.
- **Wrap a full suite run in `scripts/qtest`.** Both suites size their worker pool from `cpu_count()`, so concurrent runs across worktrees fail on timeouts that have nothing to do with the code. `qtest` is a machine-wide `flock` semaphore and ends every run with a verdict line on stderr (`PASS` / `FAIL` / `KILLED-SIGKILL` / `NO-SLOT`). Exit 75 means the command did not run. Serialize the expensive runs only.
- **Chain the checks in one shell invocation**, and use `-x` / `--bail=1` while iterating; drop the bail flag for the run you report. Never read a result through a pipe unless `pipefail` is on — a task under the daemon inherits it, a terminal in this repo does not.

**Nine markers are deselected by default and none runs unless you ask**: `integration`, `live`, `linux`, `image`, `smoke`, `full`, `testbed`, `deploy`, `ml`. A tenth, `requires_dac`, skips itself where the process can bypass permission bits. The seven discretionary tiers:

```bash
scripts/test-linux.sh            # the suite + the linux tests, on a real kernel
uv run pytest -m image -n0       # the built image's contract
uv run pytest -m smoke -n0       # end-to-end against the lean compose stack
uv run pytest -m full -n0        # end-to-end against the full stack, incl. a real Nextcloud
uv run pytest -m testbed -n0     # wire-level email against a real IMAP/SMTP server, no istota image
scripts/test-deploy.sh           # the Ansible role converged on a real systemd host
scripts/test-upgrade.sh          # the current image over an older release's state
```

`image`, `smoke`, `full`, `testbed` and `deploy` require `-n0`: their fixtures are session-scoped and build one tagged image. Before a release, add `-m full -n0`, `-m image -n0 --platform amd64` and `scripts/test-upgrade.sh --from-floor --shape volume`. Three tiers carry a negative control that must go red (`scripts/test-image-negative-control.sh`, `scripts/test-deploy-negative-control.sh`, and the same broken image handed to the upgrade tier via `ISTOTA_IMAGE_TAG`). **On a tier asserting against an artifact, reading the test tells you almost nothing about whether it can fail** — run the control and write down what it turned red.

**The `deploy` tier is the bare-metal half, and it is new (ISSUE-439).** It boots `docker/test/Dockerfile.deploy` with systemd as PID 1 and drives the real `deploy/install.sh --headless` inside it, so `ansible-playbook` actually runs — which nothing in the repository did before. The fourteen `tests/test_ansible_*.py` files parse the role's YAML and assert on the parse, and cannot see a unit that fails to start or a task ordering that only breaks when the tasks run. Doctor is the oracle, as it is for `image` and `smoke`. It converges with rclone, zram, Talk and the web UI off, and covers neither reboot ordering, nor the `Require`/`After` relationship with the rclone mount unit, nor a real FUSE mount — `tests/deploy/conftest.py` states each concession and why.

**All seven tiers need Docker — the Linux tier unless it runs natively — so a sandboxed task cannot run any of them; check `ISTOTA_SANDBOXED` before you plan around one.** No Docker socket is bound into a sandbox, and a task's own sandbox passes `--unshare-user --disable-userns`, which shuts the nested-namespace route too. **When a change touches the sandbox, the network proxy, the skill proxy, a migration or the image, say so in the merge request, name the tier that covers it, and ask for the run before merge.** Report the default suite as what it is: it patches `_bwrap_available` and checks argv, so it has never executed the sandbox path you changed.

Anything added to `testbed/` is bound by four rules, the first three enforced in the default suite: a service points the daemon at itself only through a variable `docker/istota/render-config.sh` reads **and** `docker/docker-compose.yml` passes through; a stub bound off loopback must be given a credential to expect; a negative assertion takes a watermark *and* a discriminating column; a service whose client negotiates capabilities is run for real rather than stubbed. The prompt goldens (`tests/test_prompt_golden.py`) are in the default suite and need no container — regenerate with `uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_prompt_golden.py -n0` and review the diff.

## Committing

This repo is public, so `.githooks/pre-commit` scans staged content twice: `gitleaks` for credentials and `scripts/check-private-data.sh` for private data (a real name, a production hostname, a home-directory path, an account number). Enabled per clone by `git config core.hooksPath .githooks`, which `scripts/setup.sh` does. **A scan that cannot run refuses the commit when the shell is an unattended one** — a model task, an admin task with a checkout, or `PRECOMMIT_SCANS_REQUIRED=1`; a human gets a warning and the commit. That refusal is a broken install to report, not a step to work around, and `--no-verify` drops both scans. Patterns come from the committed `.private-data-patterns`, the gitignored `.private-data-local` and two terms derived at runtime. Neither scan prints the matched value. Details in `.claude/rules/committing.md` and `docs/development/secret-scanning.md`.

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by `istota user|resource|briefing|secret ensure`. The `[users.X]` block in `config.toml` (docker entrypoint path) is also accepted; DB rows win at config-load time. The retired `config/users/{user}.toml` mechanism is gone. CalDAV derived from Nextcloud. Field-by-field reference in `.claude/rules/config.md`.

## Deployment

Ansible role (`deploy/ansible/`), Docker stack (`docker/`), and the Nextcloud rclone mount — see `.claude/rules/deployment.md`.

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
