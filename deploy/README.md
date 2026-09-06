# Deployment

A single deployment path using Ansible as the canonical provisioning tool. The `install.sh` script is a thin bootstrap that ensures Ansible is installed, then runs the bundled Ansible role locally.

## Quick start

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

## How it works

`install.sh` is a ~250-line bootstrap that:

1. Ensures Python 3.11+, pipx, and ansible-core are installed
2. Installs required Ansible collections (`community.general`, `ansible.posix`)
3. Gets the Ansible role (from local repo or cloned copy)
4. Runs the setup wizard by default (writes `/etc/istota/settings.toml`); skipped under `--headless` or `--update`
5. Converts `settings.toml` to Ansible vars via `settings_to_vars.py`
6. Runs `ansible-playbook` in local connection mode

The Ansible role (`deploy/ansible/`) is the single source of truth for provisioning.

## Settings file

The interactive wizard writes a settings file to `/etc/istota/settings.toml`. This file drives all subsequent `--update` runs. Settings mirror Ansible variable names without the `istota_` prefix.

Minimal example:

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

The rclone obscured password is auto-generated from the app password during installation. You don't need to set `rclone_password_obscured` manually.

## Using Ansible directly

For infrastructure-as-code workflows (multiple servers, CI/CD), use the Ansible role directly without `install.sh`:

```yaml
# In your playbook:
- hosts: your-server
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
```

Point your Ansible `roles_path` at the `deploy/ansible/` directory, or symlink it into your roles directory.

## File reference

| File | Purpose |
|---|---|
| `install.sh` | Bootstrap: ensures Ansible, runs wizard, delegates to role |
| `wizard.sh` | Interactive setup wizard (writes settings.toml) |
| `settings_to_vars.py` | Converts settings.toml to Ansible vars YAML |
| `local-playbook.yml` | Playbook for local-mode deployment |
| `ansible/` | Ansible role (tasks, templates, defaults, handlers) |

## Prerequisites

- Debian 13+ or Ubuntu server
- Nextcloud instance with an app password for the bot user
- Claude Code CLI subscription (the wizard collects a long-lived OAuth token up front — generate one with `claude setup-token`)

## Post-install

1. Claude auth: the wizard's "Claude Authentication" prompt collects an OAuth token and provisions the credentials file, so no separate login is needed. Only if you skipped it (and aren't using `ANTHROPIC_API_KEY`), run `sudo -u istota HOME=/srv/app/istota claude login`.
2. Invite the bot user to Nextcloud Talk conversations
3. Test: `sudo -u istota HOME=/srv/app/istota /srv/app/istota/.venv/bin/istota task "Hello" -u USER -x`

## Service management

```bash
systemctl status istota-scheduler
systemctl restart istota-scheduler
journalctl -u istota-scheduler -f
```

## Optional features

The core install covers Talk integration, email, scheduling, and Claude Code execution. The wizard prompts for these features and configures them automatically:

- Memory search (semantic search over conversations)
- Sleep cycle (nightly memory extraction)
- Whisper (audio transcription)
- GPS location tracking
- Automated backups
- Browser container (web browsing via Docker)

Features not covered by the wizard (requires manual Ansible vars):

- Nginx site hosting
- Web interface (OIDC)
- Developer skill (Git/GitLab/GitHub)
- Auto-update

All settings go in `/etc/istota/settings.toml`, then re-run `install.sh --update` to apply changes.

## Migration from old install.sh

If you have an existing installation deployed with the previous monolithic `install.sh`, the new bootstrap works with your existing `/etc/istota/settings.toml` unchanged. Run `sudo bash install.sh --update` and it will install Ansible, convert your settings, and re-deploy via the Ansible role.
