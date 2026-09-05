# Local single-user install

Istota's default shape is a multi-user server deployment backed by Nextcloud (files, Talk chat, CalDAV, OAuth login), isolated per-user with bubblewrap — see the [bare metal](quickstart-bare-metal.md) and [Docker](quickstart-docker.md) quickstarts. This page covers the other shape: a slimmed-down **local, single-user install** you run on your own mac or Linux box, like a locally-installed agent harness. No Nextcloud, no server, no sandbox, no login.

The workspace is a plain local folder (default `~/.istota`). The web UI runs on loopback with authentication bypassed. It is always single-user and always trusted.

## Trust model — read this first

!!! warning "A local install runs unsandboxed"
    There is no bubblewrap isolation, no skill proxy, and no network proxy. The agent's subprocesses run with **your user account's full privileges** — full filesystem access and open network. A prompt injection carried in ingested content (an email, a browsed page, a feed item) therefore has real reach.

Only give a local instance content and instructions you trust. The content-trust guardrails (`untrusted_input` companion on the ingest skills, `sensitive_actions`) stay in place, but they are about content provenance, not process isolation.

If you need isolation between untrusted content and your host, use the server deployment (Linux + bubblewrap), not the local install. See [Security](../deployment/security.md) for how the sandboxed shape confines the agent.

## Requirements

- macOS or Linux (Windows is not supported).
- Python 3.11+ with [`uv`](https://docs.astral.sh/uv/).
- For the default model backend: the [`claude` CLI](https://docs.anthropic.com/en/docs/build-with-claude/claude-code), installed and logged in (reuses your existing Claude Code subscription). Alternatively, an API key for any OpenAI-compatible endpoint — see the [native brain runbook](../configuration/native-brain.md).

## Install

```bash
uv tool install 'istota[local]'
```

The `local` extra is the lean footprint: the core agent, the web UI, and the light pure-Python modules (feeds, calendar, email, markets). The guided `install.sh --standalone` installs `local` plus `money` and `location` — both are light on disk, so there's nothing to gate at install time. Which modules are actually *enabled* is a choice made in `istota setup`, not a packaging one. If you install by hand, add the same extras:

```bash
uv tool install 'istota[local,money,location]'
```

Heavier, genuinely optional extras (`memory-search`, `whisper`, `transcribe`, `garmin`) stay off unless you name them:

```bash
uv tool install 'istota[local,money,location,memory-search,whisper,transcribe]'
```

A module whose extra isn't installed hides itself — the app skips it and its web UI tab doesn't appear rather than showing a broken tab.

!!! note "weasyprint (invoice PDFs)"
    The `money` extra pulls weasyprint, whose native libs (pango/cairo) are only touched when you *render an invoice PDF*. Everything else in the money module — the ledger, queries, balances, the Money tab — works without them. On macOS that one path needs `brew install pango`; until then invoice-PDF generation is the only thing that errors.

## Set up

```bash
istota setup
```

The interactive wizard:

1. **Workspace** — where your data lives (default `~/.istota`).
2. **Model backend** — if the `claude` CLI is detected it offers to use it (no extra keys). Otherwise it asks for an OpenAI-compatible base URL, model, and API key.
3. **Identity** — a user id (default your OS username), display name, timezone.
4. **Web port** — default `8766`.
5. **Modules & surfaces** — everything ships installed, so this only chooses what's *enabled*. The rule is simple: a module that works with no further setup is on, and one that needs something external is off.
    - GPS/location tracking — off, it needs an Overland ingest token to receive pings.
    - Money, health, feeds and briefings — each on, each an opt-out. A "no" is recorded in your profile's `disabled_modules`, which also hides the tab.
    - Email (IMAP/SMTP) — off. Answering yes asks for the IMAP host, the user, the SMTP host (defaulting to the IMAP one, since a submission service on another name is ordinary), and the password, which is read without echo.
    - An external CalDAV server — off. This is the calendar for a machine with no Nextcloud; see below.

It writes `~/.config/istota/config.toml` and a sibling `~/.config/istota/istota.env` (secrets — the model API key, the secrets-store master key, the calendar and mail passwords; `chmod 600`), plus `~/.config/istota/admins` naming you. Then it creates the directories the config names — the workspace, and `db-backups` and `tmp` under it at `0700` — initializes the database, and seeds your workspace.

Finally it runs `istota doctor` against what it just wrote and prints anything that failed. A red check never fails the install: the files are written and the database is initialized either way, so it is information rather than a reason to unwind a working install. `istota doctor` gives the full report at any time.

`setup` is idempotent. Re-running prompts before touching an existing config; `--force` overwrites. Two things survive a rewrite: the secrets-store master key, always, because replacing it would make every stored credential permanently unreadable; and a CalDAV server you already configured, which you are asked about rather than silently losing — its password lives only in `istota.env` and a rewrite that dropped the block would destroy it. For scripted installs, `--yes` takes defaults plus flags:

```bash
istota setup --yes --workspace ~/.istota --user me --port 8766 --brain claude_code
# or, with an API-key backend:
istota setup --yes --brain native --native-model claude-sonnet-4-6 \
  --native-base-url https://api.anthropic.com/v1 --native-api-key sk-...
# opting out of modules, which are otherwise all on:
istota setup --yes --no-money --no-health --no-feeds --no-briefings
```

`--yes` leaves `[caldav]` off, and there is deliberately no flag for it — a URL without a password is worse than no block at all, so it is an interactive question or nothing. A `--yes --force` re-run over an install that already has one carries it forward rather than dropping it.

## Run

```bash
istota serve
```

This runs the task worker and the web server in one process. Open the printed URL (`http://127.0.0.1:8766/istota`). There is no login — you are the single configured user, and you are admin. `Ctrl-C` stops both cleanly.

`serve` sources `~/.config/istota/istota.env` itself, so you don't need to export anything. Override the bind with `--host`/`--port`, or a different env file with `--env-file`. A non-standard config goes through the global `-c`, which comes before the subcommand: `istota -c /path/to/config.toml serve`.

The **REPL** works too, in a separate terminal, whether or not `serve` is running:

```bash
istota repl
```

See the [CLI reference](../reference/cli.md) for the full `setup` / `serve` / `repl` flag lists.

## Updating

```bash
istota update
```

Pulls the latest code from the checkout `install.sh` recorded in `~/.config/istota/install.json` (by default a clone under `~/.local/share/istota/src`), reinstalls, and runs any database migrations. When it finishes, restart `istota serve` to pick up the new code — a running process holds the old code in memory until then. Pass `--force` to update even if that checkout has uncommitted changes (it discards them with `git reset --hard`).

By default `update` follows the **stable** channel — the latest tagged release. To ride the development branch instead (newer, less tested), run `istota update --channel main`; switch back with `istota update --channel stable`. The choice is remembered, so you set it once. (An install made before this option existed keeps tracking `main` until you pick a channel.)

!!! note "Standalone only"
    `update` applies to this standalone shape and needs the install record `install.sh` writes; a hand-run `uv tool install` won't have it, so re-run `install.sh --standalone` once. A server (Nextcloud/auth) deployment is updated separately and `update` declines to run there.

## What works, what's off

- **Web chat** — the primary surface. Fully local (SQLite + local files).
- **REPL** — secondary, fully local, inline execution.
- **TASKS.md** — the `~/.istota/Users/<user>/<bot>/config/TASKS.md` file, polled while `serve` runs.
- **Scheduled jobs, briefings, heartbeat, cron** — run in the same process.
- **Money, health, feeds, briefings** — on unless you said no in `setup`. Each has a tab in the web UI; turn one off later with `disabled_modules` on your profile.
- **`istota doctor`** — about thirty checks over the paths, the database, the credentials and the model backend. `setup` runs it for you at the end; run it yourself after any config edit. For a config anywhere but the default path, `-c` is a global option and goes *before* the subcommand: `istota -c /path/to/config.toml doctor`.
- **Session transcripts** — a native-brain task writes its whole run to `<workspace>/logs/<user>/`, one JSONL file per attempt. The `claude` CLI backends write their own instead, so this is empty unless you chose `native`. Read one back with `istota session list` / `show`. They hold the assembled prompt and raw tool output, so `[brain.native.session_log] enabled = false` is the way out.
- **Nextcloud Talk** — off. Chat is the web UI and REPL.
- **Email / ntfy** — off by default; enable in `setup` or config.
- **GPS location webhooks** — off by default.
- **Calendar** — `setup` offers an external CalDAV server (Radicale, Fastmail, Google). Off if you declined; see below for adding it later.
- **Per-room brain pinning** — `!brain` and the room settings can pin a room to a specific backend, but only to one the operator listed in `[brain] room_selectable`, which is empty by default and which `setup` does not write. Add the kinds you want pinnable to `config.toml` first.

The Admin pane (`/istota/admin`) shows a "Running in standalone mode" notice listing exactly what's off in your install, so a feature that intentionally doesn't work reads as expected, not broken.

## Enabling optional pieces

**Calendar (external CalDAV).** A local install has no Nextcloud, so there is nothing for calendar to derive from. `setup` asks about this, and re-running it is the easiest way to add one later — it keeps a server you already configured and lets you replace it.

By hand, it is two files. The URL and the username go in `config.toml`:

```toml
[caldav]
url = "https://dav.example.com"
username = "you@example.com"
```

and the password goes in `istota.env` beside every other credential:

```
ISTOTA_CALDAV_PASSWORD=app-specific-password
```

Not in `config.toml`. That file's generated header says secrets live in the sibling env file and never there, and the environment override is what makes that true without an exception — which is why `config.toml` is left at the usual permissions and only `istota.env` is `0600`.

Set both or neither. The URL, the username and the password each fall back to `[nextcloud]` on their own rather than as a group, so half a block is a mixture rather than an override that is simply off. On this shape there is no Nextcloud to fall back to, so a URL with no password just leaves calendar unable to authenticate — `setup` refuses that pairing, which is why it can only arise from a hand edit. On a server deployment the same half-block is worse: it names a foreign host and then hands that host the Nextcloud app password.

**Email.** Set `[email] enabled = true` with your IMAP host, user and SMTP host in `config.toml`, and the passwords in `istota.env` (`ISTOTA_EMAIL_IMAP_PASSWORD`, `ISTOTA_EMAIL_SMTP_PASSWORD`). `setup --email` collects these interactively; it asks for the SMTP host separately, defaulting to the IMAP one, and reads the password without echoing it.

**Heavy modules.** Install the matching extra (above), then the module is on by default (opt out per user via `disabled_modules`).

See the [configuration reference](../configuration/reference.md) for every option and [per-user configuration](../configuration/per-user.md) for the workspace files (`USER.md`, `PERSONA.md`, `CRON.md`, and the rest) each user owns.

## Notes

- **Loopback only.** No-auth mode refuses to start on a non-loopback bind — you cannot accidentally expose an unauthenticated instance on the network. Use the server deployment if you need remote access.
- **One instance.** `serve` holds a lock; a second `serve` reports "already running" and exits.
- **Backups.** `setup` writes an explicit `[scheduler] db_backup_dir` under the workspace, and creates it at `0700`, so local snapshots run even though the workspace isn't a mountpoint.
- **Everything in one folder.** The database, module databases, and workspace all live under the workspace directory — back it up or move it as a unit.
- **Storage vocabulary follows the backend.** The bot describes storage based on whether a Nextcloud server backs it, keyed on `[nextcloud] url` presence (not the standalone flag). With no URL — the local shape — the prompt and skill docs talk about "your workspace" (a local folder) instead of a Nextcloud mount, and tell the model it also has ordinary access to the rest of the machine's filesystem. Set a `[nextcloud] url` and the vocabulary switches back to Nextcloud/rclone.
