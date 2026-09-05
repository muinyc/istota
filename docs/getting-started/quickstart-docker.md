# Docker quickstart

The Docker setup spins up a complete stack: Postgres, Redis, a fresh Nextcloud instance, and the Istota scheduler. If you already have a Nextcloud instance, use [bare metal](quickstart-bare-metal.md) instead -- Docker Compose creates its own Nextcloud.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
```

The one-liner clones the repo to `~/istota` and runs an interactive wizard that auto-generates the passwords, prompts for your Claude token and optional features, writes `docker/.env`, and brings the stack up. Requires Docker with the `docker compose` plugin.

What it asks about, roughly in order: the passwords and your Nextcloud user, the model backend (which model, per brain -- a `claude_code` install sets `ISTOTA_BRAIN_CLAUDE_CODE_MODEL` rather than a deployment-wide one), Talk over the signaling server, email, GPS location, the modules (feeds, money, health, briefings), the developer skill, and the browser container. It computes `COMPOSE_PROFILES` from your answers so the optional containers come up without naming a profile per command.

**The signaling question is asked early because it has a one-shot window.** Registering the server with Talk happens in a Nextcloud post-installation hook, which the image runs only on a genuinely fresh instance -- so `ISTOTA_TALK_SIGNALING_SERVER` and its secret have to be in `.env` before the *first* `docker compose up`. Missing it is recoverable, by running `occ talk:signaling:add` by hand inside the Nextcloud container, but nothing else will do it for you. See [Talk over the signaling server](../deployment/docker.md#talk-over-the-signaling-server), which also explains why the URL has to resolve from both a browser and Nextcloud's own PHP.

Answering no to a module records it in `USER_DISABLED_MODULES`, which seeds your profile the first time the stack boots. After that the stored profile wins, so change it in the web settings rather than in `.env`.

First start takes a few minutes: Nextcloud initializes the database, creates user accounts, installs apps (Talk, Calendar, External Storage), sets up shared folders, and creates the bot's Talk rooms -- private group rooms, not public ones.

When it's up, open `http://localhost:8080`, log in with the username and password the wizard set, go to Talk, and start chatting.

### Wizard flags

The `--docker` flag and everything after it forwards to `docker/init.sh`:

```bash
bash docker/init.sh --minimal    # passwords + Claude token + user only, skip optional sections
bash docker/init.sh --force      # overwrite an existing .env without asking
bash docker/init.sh --no-start   # write .env but don't run `docker compose up`
```

### Manual configuration (from a clone)

To skip the wizard and edit the environment by hand, copy the example and fill it in:

```bash
cd ~/istota/docker
cp .env.example .env
```

Set at minimum:

- `CLAUDE_CODE_OAUTH_TOKEN` -- generate with `claude setup-token` (or set `ANTHROPIC_API_KEY` for direct API access)
- `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `BOT_PASSWORD`, `USER_PASSWORD`
- `USER_NAME` -- your Nextcloud username

Optional but recommended:

- `USER_DISPLAY_NAME` -- your full name
- `USER_TIMEZONE` -- e.g. `America/New_York` (defaults to UTC)
- `USER_EMAIL` -- enables email features
- `USER_DISABLED_MODULES` -- comma-separated modules to leave off (`feeds,money,health,briefings`), read once when your profile row is created
- `COMPOSE_PROFILES` -- comma-separated optional services, below. A hand-copied `.env` leaves it empty, which means the core stack only

Then bring the stack up:

```bash
docker compose up -d
```

## Optional services

Three run as Docker Compose profiles: `browser` (Chrome with bot-detection countermeasures), `location` (the GPS webhook receiver) and `signaling` (Talk over a WebSocket instead of polling).

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS webhook receiver
docker compose --profile browser --profile location up -d  # Combine as needed
```

Setting `COMPOSE_PROFILES` in `.env` is the alternative to naming them per command; `init.sh` writes it from your answers. The browser container requires an x86-64 host, since Chrome has no ARM packages. `signaling` needs its registration in place before the first boot -- see above.

## Configuration after first start

`/data/config/config.toml` is rendered from `docker/.env` on **every** start. Editing the rendered file directly does not survive a restart; edit the variable instead:

```bash
$EDITOR docker/.env
docker compose restart istota
docker compose restart web webhooks   # webhooks only if you run the location profile
```

The boot logs every key that changed, so `docker compose logs istota` is where you confirm an edit landed, and the outgoing file is kept as `config.toml.prev`. This is also what makes a release that adds or renames a config key land on an existing install: restarting `istota` is the patch. Values provisioning derives once -- the OAuth2 client, the Talk room tokens, the location ingest token, the web session key -- persist and are fed back into each render.

The `.env` file exposes most of the same settings available in the Ansible role. See `.env.example` for the full list, and [Docker deployment](../deployment/docker.md) for the reasoning behind the ones that matter.

## Differences from bare metal

| Aspect | Docker | Bare metal |
|---|---|---|
| Task sandbox | Off -- see below | bubblewrap, per user |
| Network proxy | Disabled (Docker network isolation) | CONNECT proxy with domain allowlist |
| Users | Single user provisioned | Multi-user from config |
| Nextcloud | Bundled (new instance) | Connects to existing instance |
| Backups | Your responsibility (volume backups) | Ansible sets up cron-based DB backups |
| Python extras | All installed | Configurable per feature |
| Devbox | Not shipped; the skill cannot run here | Available |

**The shipped compose file runs every task unsandboxed, and that is deliberate.** Docker's default seccomp profile blocks the `unshare(CLONE_NEWUSER)` bubblewrap needs, so the startup probe fails and every command runs unwrapped -- the daemon says so at boot, in a line carrying `bubblewrap unavailable`. Turning it on takes both `seccomp:unconfined` and `systempaths=unconfined` on the `istota` service, and neither substitutes for the other: seccomp lets bwrap create the namespace but not mount a procfs inside one, which every sandbox does. `--cap-add SYS_ADMIN` is not an alternative -- it gets past the unshare and fails at `pivot_root`.

Adding the pair trades the container-to-host boundary for the task-to-daemon one, which is a real trade rather than a formality. [Running tasks sandboxed](../deployment/docker.md#running-tasks-sandboxed) has the whole argument and the exact compose fragment. The supported production shape is bare metal via Ansible, where bubblewrap unshares the user namespace unasked and neither setting is needed.

The skill credential proxy works either way.

## Next steps

See [post-install](post-install.md) for first steps after deployment.
