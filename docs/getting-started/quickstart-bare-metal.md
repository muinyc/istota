# Bare metal quickstart

Bare metal is the canonical deployment. It runs Istota natively on a Debian/Ubuntu VM and connects to an existing Nextcloud instance. If you don't have a Nextcloud, use the [Docker quickstart](quickstart-docker.md) instead — it bundles its own.

Requirements: a Nextcloud instance, a Debian/Ubuntu VM, and a model backend (a Claude Code subscription/OAuth token, or any OpenAI-compatible endpoint — see the [native brain runbook](../configuration/native-brain.md)).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash
```

That's the whole install. The one-liner clones the repo, installs prerequisites, and runs an interactive wizard that walks you through connecting to Nextcloud, setting up users, and choosing optional features.

Prefer to read before you pipe? Download and inspect it first:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

When you're done, see [post-install](post-install.md) to authenticate Claude and send your first message.

## What install.sh does

The dispatcher clones the repo (when curl-piped) and hands off to `deploy/install.sh`, a bootstrap that:

1. Ensures Python 3.11+, pipx, and ansible-core are installed
2. Installs required Ansible collections (`community.general`, `ansible.posix`)
3. Gets the Ansible role (from local repo or cloned copy)
4. Runs the setup wizard by default (writes `/etc/istota/settings.toml`); skipped under `--headless` or `--update`
5. Converts `settings.toml` to Ansible vars via `settings_to_vars.py`
6. Runs `ansible-playbook` in local connection mode

## Common commands

```bash
# Default: runs the interactive setup wizard
sudo ./install.sh

# Skip the wizard — requires existing settings (or --settings PATH)
sudo ./install.sh --headless

# Update existing installation
sudo ./install.sh --update

# Preview changes without applying
sudo ./install.sh --dry-run

# Use a custom settings file
sudo ./install.sh --settings /path/to/settings.toml
```

## Settings file

The wizard writes `/etc/istota/settings.toml`. This file drives all subsequent `--update` runs. Minimal example:

```toml
home = "/srv/app/istota"
namespace = "istota"
nextcloud_url = "https://nextcloud.example.com"
nextcloud_username = "istota"
nextcloud_app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
use_nextcloud_mount = true
nextcloud_mount_path = "/srv/mount/nextcloud/content"
use_environment_file = true

[users.alice]
display_name = "Alice"
timezone = "America/New_York"
email_addresses = ["alice@example.com"]
```

See `deploy/ansible/defaults/main.yml` for the full list of available settings.

## Using Ansible directly

For infrastructure-as-code workflows, use the Ansible role without `install.sh`:

```yaml
- hosts: your-server
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
```

Point your `roles_path` at `deploy/ansible/`, or symlink it into your roles directory. See [Ansible deployment](../deployment/ansible.md) for details.

## Prerequisites

- Debian 13+ or Ubuntu server
- Nextcloud instance with an app password for the bot user
- A model backend: a Claude Code subscription/OAuth token (default), or any OpenAI-compatible endpoint via the [native brain](../configuration/native-brain.md)

## Optional features

The wizard prompts for these and configures them automatically:

- Email (IMAP/SMTP)
- Memory search (semantic search over conversations)
- Sleep cycle (nightly memory extraction), and channel memory extraction with it
- Whisper (audio transcription)
- GPS location tracking
- Talk over a signaling server, instead of polling Nextcloud
- Automated backups
- Browser container (web browsing via Docker)
- The developer skill — the repos directory and a GitLab or GitHub token together, since the role refuses a deploy that enables the skill without one
- Public hostname, and the web UI with its Nextcloud OAuth2 client
- The map basemap provider, with a CARTO key if you pick that one
- The model backend, and with it which brain kinds a room or a scheduled job may pin, and what a task falls back to when the primary brain cannot run it

Two of those are worth reading before you answer.

**The signaling server** has to be registered with Talk, and it has to be reachable from a browser *and* from Nextcloud's own PHP. The wizard asks for the URL and offers no default for that reason. If the daemon is told to use it and Talk is still in internal mode, it refuses to start.

**The basemap** defaults to a provider that needs no key. Choosing `carto` without one is the case to avoid: every tile comes back watermarked with a `200` status, so nothing detects it for you and the map merely looks wrong.

Still set by hand in the settings file: auto-update, and the devbox container.

All settings go in `/etc/istota/settings.toml`, then re-run `install.sh --update` to apply.

## Next steps

See [post-install](post-install.md) for authenticating Claude and testing.
