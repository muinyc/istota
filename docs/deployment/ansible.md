# Ansible deployment

The Ansible role at `deploy/ansible/` is the canonical provisioning tool. It handles everything: system packages, Python environment, config files, systemd services, nginx, rclone mount, backups, and optional features.

## Prerequisites

- Debian 13+ or Ubuntu target host (the role's own support target; the standalone `deploy/install.sh` path warns below the same version rather than refusing)
- Nextcloud instance with app password
- Ansible 2.14+ with `community.general` and `ansible.posix` collections

## Example playbook

```yaml
- hosts: your-server
  become: yes
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
        istota_rclone_password_obscured: "{{ vault_rclone_password }}"
        istota_admin_users:
          - alice
        istota_users:
          alice:
            display_name: "Alice"
            email_addresses: ["alice@example.com"]
            timezone: "America/New_York"
```

## Using the role

Point `roles_path` at `deploy/ansible/`:

```ini
# ansible.cfg
[defaults]
roles_path = /path/to/istota/deploy/ansible
```

Or symlink into your roles directory:

```bash
ln -s /path/to/istota/deploy/ansible /path/to/roles/istota
```

## Feature flags

| Feature | Variable | Default |
|---|---|---|
| Email | `istota_email_enabled` | `false` |
| Browser container | `istota_browser_enabled` | `false` |
| Memory search | `istota_memory_search_enabled` | `true` |
| Sleep cycle | `istota_sleep_cycle_enabled` | `true` |
| Channel sleep cycle | `istota_channel_sleep_cycle_enabled` | `true` |
| Whisper transcription | `istota_whisper_enabled` | `false` |
| Static web root at `/` (nginx) | `istota_web_root_enabled` | `true` |
| Node.js | `istota_nodejs_enabled` | `false` |
| Developer/GitLab | `istota_developer_enabled` | `false` |
| Database backups | `istota_backup_enabled` | `true` |
| Journal size cap | `istota_journald_manage` | `true` |
| auditd log rotation | `istota_auditd_manage` | `true` |
| zram swap | `istota_zram_enabled` | `true` |
| Disk swapfile (second tier) | `istota_swapfile_enabled` | `false` |
| Default Talk rooms | `istota_provision_talk_rooms` | `true` |
| Bubblewrap sandbox | `istota_security_sandbox_enabled` | `true` |
| Web interface | `istota_web_enabled` | `true` |
| GPS location | `istota_location_enabled` | `false` |

## Variables

All variables with defaults are in `deploy/ansible/defaults/main.yml`. Key groups:

- **Core**: `istota_namespace`, `istota_home`, `istota_repo_url`
- **Nextcloud**: `istota_nextcloud_url`, `istota_nextcloud_username`, `istota_nextcloud_app_password`
- **Security**: `istota_security_sandbox_enabled`, `istota_use_environment_file`
- **Users**: `istota_users` (dict), `istota_admin_users` (list)
- **Scheduler**: `istota_scheduler_*` (poll intervals, worker limits, timeouts)
- **Web**: `istota_web_enabled`, `istota_web_port`, `istota_web_chat_max_attachment_mb`, `istota_web_graceful_shutdown_seconds`, `istota_web_stop_timeout_seconds`
- **Email**: `istota_email_enabled`, `istota_email_outbound_approval_floor`, plus per-user `outbound_approval` and `external_turn_display` keys inside `istota_users`

`istota_email_outbound_approval_floor` (default **`"untrusted"`**) is the [outbound approval gate](../features/email.md#the-outbound-approval-gate)'s floor, and the role is the only supported place to change it — a hand edit to `config.toml` is overwritten on the next run. **Quote the value.** `off` unquoted is a YAML boolean: it renders `outbound_approval_floor = "False"`, which the daemon refuses to load. The play asserts the floor and each per-user `outbound_approval` before templating, so a bad value fails naming the variable rather than leaving an unloadable config on disk for the next restart to find.

Per-user `outbound_approval` / `external_turn_display` under `istota_users` are passed to `istota user ensure`, not templated into `[users.X]` — the TOML keys seed only a user with no profile row yet, while the CLI flags update an existing one.

Two web variables are worth knowing about before changing them:

`istota_web_chat_max_attachment_mb` (default **100**, against an application default of 25) feeds **two** consumers — the `[web.chat] max_attachment_mb` setting in `config.toml` and nginx's `client_max_body_size`. Do not split them: if nginx's ceiling is the lower of the two it rejects the upload with its own HTML error page, which the browser client cannot parse into a message.

`istota_web_graceful_shutdown_seconds` bounds uvicorn's wait for open connections. It matters because the web chat room stream is a session-lived SSE connection whose generator exits only on client disconnect — which a server shutdown does not trigger — so without the flag a restart with any browser tab open sat out the full `TimeoutStopSec` and was eventually SIGKILLed. Note the unit template is skipped in web-only mode, so a changed value lands on the next full or `istota_update_only` run.

## The clone credential

A private `istota_repo_url` needs a token to clone and to fetch updates. That token is **not** interpolated into the URL. It used to be, which persisted it as `remote.origin.url` and expanded it into the argument vector of `git-remote-https` on every fetch — one every two minutes from the auto-update cron, readable by anyone with root on the box.

It now lives in a 0600 root-only file read by a six-line credential helper, registered per-host at system scope, with the clone using the bare URL. A deploy rewrites the old value out of hosts already set up the other way.

**Rotate the token if your host predates this.** It sat in `.git/config` and in process arguments for the life of the old shape, so the fix removes the exposure going forward but cannot undo it.

The helper answers `get` and ignores every other verb. Git's own `store --file=` was the obvious choice and is deliberately not used: it implements `erase`, and git calls `erase` on any 401, so one revoked or freshly rotated token would truncate the file and leave the cron fetch failing silently — worst in the deploy right after a rotation. `store` also rewrites its file on a successful auth, which would let an unrelated root git operation against the same host swap the deploy credential.

`GIT_TERMINAL_PROMPT=0` is set on the clone, the tag fetch and the update script. With the token out of the URL, a missing or rejected credential makes git prompt rather than fail, and a prompt inside the update script would hang the run while it holds its flock — after which every later run exits silently at the lock and updates stop with nothing reported.

## Host memory headroom

The role gives the host swap and puts a soft ceiling on the istota units. Both came out of an August 2026 outage: a host running with `Total swap = 0` had nowhere to put cold memory, threw away the page cache every program was running from, and spent 41 minutes reading itself back off disk.

| Variable | Default | Purpose |
|---|---|---|
| `istota_zram_enabled` | `true` | Install `systemd-zram-generator` and configure a zram swap device |
| `istota_zram_size` | `"ram / 2"` | The device's **uncompressed** capacity, not its RAM cost |
| `istota_zram_algorithm` | `"zstd"` | Compression algorithm |
| `istota_zram_priority` | `100` | Swap priority; Linux prefers the higher number |
| `istota_swapfile_enabled` | `false` | Optional second-tier disk swapfile |
| `istota_swapfile_size_mb` | `2048` | Its size |
| `istota_swapfile_path` | `/swapfile` | Where it lives |
| `istota_swapfile_priority` | `10` | Below the zram priority, so zram fills first |
| `istota_scheduler_memory_high` | `"5G"` | `MemoryHigh=` on the scheduler unit (`""` omits it) |
| `istota_web_memory_high` | `"1G"` | `MemoryHigh=` on the web and webhooks units |
| `istota_scheduler_cpu_weight` | `50` | `CPUWeight=` on the scheduler unit |

zram rather than a disk swapfile because the disk was already the saturated resource, at roughly 1.7 GB/s of forced re-reads. Compressed in-RAM swap adds no disk traffic. Sizing is worth reading twice: `zram-size` sets the device's uncompressed capacity, so `ram / 2` on an 8 GB box is a ~4 GB swap device costing about 1.3 GB of RAM at typical compression ratios.

`MemoryHigh`, not `MemoryMax`. Past the limit the kernel puts the cgroup under heavy reclaim pressure and slows it; a hard cap would kill it, and killing the daemon takes everything down. The scheduler's figure covers the daemon plus every `claude` subprocess and its children. `CPUWeight` sits below the systemd default of 100 so every other unit wins under contention; no `CPUQuota` is set, because PSI showed the cores idle-waiting on memory rather than oversubscribed.

Setting `istota_zram_enabled: false` makes every zram task a no-op, so a host where the operator arranged swap another way is left as it is. Note the asymmetry: false means "this role does not manage swap", not "tear down the swap this role previously set up". Flipping true to false on a host that already has it leaves the device in place — disable `systemd-zram-setup@zram0` and delete `/etc/systemd/zram-generator.conf` by hand.

The scheduler's own [host memory breadcrumb](../architecture/scheduler.md#host-memory-breadcrumb) is the matching instrument, and the [admission gate](../architecture/scheduler.md#admission-gate) is what acts on it:

| Variable | Default | Purpose |
|---|---|---|
| `istota_scheduler_host_pressure_enabled` | `true` | Master switch for host-pressure sampling |
| `istota_scheduler_host_pressure_breadcrumb_interval` | `300` | Breadcrumb cadence in seconds (0 = disabled) |
| `istota_scheduler_host_pressure_sample_interval` | `30` | Cadence of the sample the gate and the snapshot trigger read (0 = disabled) |
| `istota_scheduler_min_available_memory_mb` | `768` | Admission floor. Below it, dispatch spawns no new worker and pending tasks wait; nothing running is stopped |
| `istota_scheduler_host_pressure_psi_threshold` | `40.0` | `memory some avg10` above this also counts as pressure |
| `istota_scheduler_host_pressure_alert_cooldown` | `900` | Minimum seconds between snapshots and admin notifications |
| `istota_scheduler_host_pressure_shmem_alert_mb` | `1024` | Third snapshot trigger — shmem no filesystem accounts for. Not wired into the gate (0 disables) |
| `istota_scheduler_host_pressure_docker_socket` | `/var/run/docker.sock` | Read-only handle for resolving a container's pid during a snapshot; empty disables container lookup |

## Per-task cgroups

`MemoryHigh=` bounds the daemon as a whole. It does nothing about one task's process tree — a build, a package install, a test suite — walking the machine into a global OOM and taking an unrelated victim with it, which is what the August 2026 outage was. The role puts each task in its own cgroup v2 group under the scheduler unit instead.

| Variable | Default | Purpose |
|---|---|---|
| `istota_scheduler_task_cgroup_enabled` | `true` | Place each task's process tree in its own cgroup |
| `istota_scheduler_task_memory_max_mb` | `2048` | `memory.max` per task (0 = unbounded) |
| `istota_scheduler_task_pids_max` | `512` | `pids.max` per task — bounds a fork storm |
| `istota_scheduler_task_cpu_max_percent` | `200` | `cpu.max` as a percentage of one core (200 = two cores; 0 = unset) |

This needs both `Delegate=memory pids cpu` and `DelegateSubgroup=supervisor` on the scheduler unit, which the role's template carries. `Delegate=` alone is not enough and the gap is silent: cgroup v2 forbids a non-root cgroup from both holding processes and enabling controllers for its children, so a task group made inside the daemon's own cgroup would be created and then hold no `memory.max` at all. `DelegateSubgroup=` moves the daemon into a `supervisor/` leaf and leaves the unit cgroup free to enable controllers for its siblings.

**A host that has not re-run this role keeps working.** Containment never engages, nothing raises, and the daemon logs the reason once at startup rather than looking protected — so check the startup log after an Ansible run, not just the config.

## The dev container

`istota_devbox_enabled` brings up one container per entry in `istota_devbox_users`, from `deploy/ansible/templates/docker-compose.devbox.yml.j2`. It is the only devbox definition in the tree; the Docker compose stack ships none.

| Variable | Default | Purpose |
|---|---|---|
| `istota_devbox_enabled` | `false` | Bring up the containers and enable the `devbox` skill. With the developer variables below set, it is also what sends project builds into them |
| `istota_devbox_users` | `[]` | User ids to create a container for. Container name is `devbox-<user_id>` |
| `istota_devbox_mem_limit` | `"4g"` | Per container, shared by all of that user's concurrent tasks |
| `istota_devbox_cpus` / `istota_devbox_pids_limit` | `2` / `512` | Per container |
| `istota_devbox_log_max_size` / `istota_devbox_log_max_file` | `"10m"` / `3` | The container's own log. The default json-file driver is unbounded, and the supervisor writes a line per server exit |
| `istota_devbox_force_recreate` | `false` | Rebuild the image and recreate the containers on the next run even when nothing tracked changed. A command-line override, not an inventory setting |

The container runs a supervisor as PID 1's child rather than `sleep infinity`, with `init: true` to reap zombies. `restart: unless-stopped` restarts the container and says nothing about a process inside one, so without the supervisor a server picked off by a dockerd restart or a memory limit stays gone until somebody notices.

Restarting the server is all the supervisor does. Commands the dead server was running are handled by the server itself, which keeps a reaper child holding one end of a pipe: the kernel closes the other end however the server dies, and the reaper kills whatever is still running. `istota-skill devbox status` reports how many commands are running and whether that child is there, and `istota doctor` warns when a container has none.

The raw-socket diagnostics — `traceroute`, `mtr`, `tcpdump` — do not work inside it. They need `CAP_NET_RAW`, which the definition no longer grants: a build needs none of them, and a container holding it can pick its own source address and walk past every address-scoped drop rule the role installs. `ping` is probably unaffected, since it tries an unprivileged ICMP datagram socket first and Docker's default sysctls permit that. Egress is otherwise permissive, bounded by those rules — link-local, Azure's host agent, RFC1918 and carrier-grade NAT.

## Where project code builds and runs

Three variables decide this, deployment-wide, and all three have to be on: `istota_developer_enabled`, a non-empty `istota_developer_repos_dir`, and `istota_devbox_enabled`. With any of them off, which is the default, `npm`, `uv`, `cargo` and the rest run on the host inside the task's sandbox, where they cannot reach a registry that is not on the CONNECT allowlist and cannot install a system package. With all three on they run in that user's container over a Unix-socket exec transport, with their real exit codes, their whole output and no timeout of their own.

There is no separate backend variable. `istota_developer_container_backend` is deleted, because two switches for one decision could disagree and both pairings were bad: a container with builds kept on the host gave the model a devbox skill whose every verb but `reset` refused, and the reverse asked the developer skill to reach a container the role had never built. Every `when:` that used to test the backend already tested `istota_devbox_enabled` alongside it, so no task runs on more hosts than it did. The compose template is where the change is visible: its two conditional blocks now key on `istota_developer_enabled` and a non-empty `istota_developer_repos_dir`, so a host that had the container on and the backend unset gains the repos mount, the socket mount and the transport's environment. That is the point of the change rather than a side effect, but it is a real change to what such a host renders. A `config.toml` still carrying `[developer.container] backend` gets a warning at config load and a `WARN` from `istota doctor`; the value is ignored. An operator who set it to `none` to keep builds on the host wants `istota_devbox_enabled: false` instead.

| Variable | Default | Purpose |
|---|---|---|
| `istota_developer_container_exec_socket_dir` | `/run/{{ istota_namespace }}-exec` | Parent of the per-user socket directories |
| `istota_developer_container_connect_timeout_seconds` | `5.0` | The client's connect budget |
| `istota_developer_container_idle_timeout_seconds` | `3600` | Server-side backstop reap for an idle connection |
| `istota_developer_container_shim_commands` | fifteen | The commands routed into the container |
| `istota_developer_repos_migrate_to` | `""` | The user id an existing flat `repos_dir` belongs to. For one run, then remove it |

`istota_devbox_uid` and `istota_devbox_gid` are **derived**, not configured: the role reads them with `getent passwd {{ istota_user }}` and passes them to the image build, so the daemon and the container write into the shared worktree as one identity. Get that wrong and there is no error message anywhere that says so — the container cannot write into a worktree the daemon made, and once that is worked around the daemon cannot unlink a tree the container made, which leaves every worktree that ever ran a build permanently unreapable. The play asserts the lookup succeeded rather than failing later on an undefined variable.

Leave `istota_security_sandbox_cache_dir` blank. With `istota_developer_enabled` and `istota_developer_repos_dir` set, each user's package cache is derived at `{repos_dir}/{user_id}/.package-caches`, which is inside the repos mount the container already gets — so cache and venv are on one mount and uv hardlinks rather than copying (`link(2)` compares mounts rather than devices). Setting the key on a deployment with the developer skill configured names a path nothing reads.

### Two upgrade notes

**The repos directory gains a level whether or not the devbox is on.** `istota_developer_repos_dir` is now a per-user root: the daemon derives `{repos_dir}/{user_id}` and hands that to the sandbox bind, `DEVELOPER_REPOS_DIR`, the credential scrub and the container mount. That is what closes cross-user reach into another person's worktrees, and it applies with `istota_devbox_enabled` off as much as on. The role moves an existing flat layout down a level on the first run, refusing any repository holding uncommitted work or written to in the last quarter of an hour. **With more than one configured user it cannot derive the owner** — the old layout recorded none — so it reports what it would move, changes nothing, and fails the play until `istota_developer_repos_migrate_to` names the user. That failure is deliberate: a green play there is followed by a daemon restart into a state where the repos bind names an empty directory and the developer skill is silently unusable.

**Every dev container is rebuilt and recreated.** The image gains the exec server, the supervisor and the uid build args, and the container's PID 1 changes, so the first run after this lands interrupts whatever is in the box at the time. The `/home/dev` volume repairs its own ownership: the supervisor chowns it once when the owner does not match the new uid, guarded by a stat of the directory, so it happens on the first start and never again. Watch `docker logs devbox-<user>` for `supervising …` and no respawn loop.

Then check it before a task does: `istota doctor --only developer.container` gives five results — does the rendered config derive what the running daemon is running (re-derived from the three variables above, since there is no key left to read), does the transport answer, do the two sides agree on uid and repos root, is the uv cache mounted, and is the command reaper there. `istota doctor --only developer.repos_layout` reports repositories still sitting outside a user's directory. A `skip` in either means the check did not run, not that the property holds.

## Disk growth

Three things on the host grew without a bound until this was fixed, and between them they filled a root disk.

| Variable | Default | Purpose |
|---|---|---|
| `istota_journald_manage` | `true` | Cap the system journal, which otherwise defaults to a tenth of the disk |
| `istota_journald_max_use` | `"500M"` | Total journal size across all files |
| `istota_journald_max_file_size` | `"50M"` | Per-file cap, so rotation stays granular |
| `istota_auditd_manage` | `true` | Rotate and delete audit logs, which were set to rotate forever |
| `istota_auditd_num_logs` | `5` | File count; the cap is this times `istota_auditd_max_log_file` |
| `istota_auditd_max_log_file` | `6` | MB per file |
| `istota_claude_versions_keep` | `2` | Old `claude` CLI builds to keep, at ~320 MB each |

Audit files stranded above the new limit are removed on the next deploy. Setting either `_manage` variable to false hands that log back to you. `istota_claude_versions_keep` keeps at least one build to roll back to after a bad release, and the build currently in use is never removed even when it is the older one.

## Default Talk rooms

`istota_provision_talk_rooms` (default `true`) creates `general`, `logs` and `alerts` for each user on deploy and fills in the channel tokens, which a bare-metal install previously had no way to get — the setting that turns the execution log on asked for a room token the operator could not know yet. It calls `istota nextcloud provision-rooms` and is idempotent.

The rooms are private group rooms, not public ones: a public room is joinable by anyone holding its link, which is wrong for rooms carrying an execution log and security alerts. Rooms that already exist are reused untouched, so this only affects new installs.

**Renaming a room is safe.** Each room's token is remembered on first provision, and later runs look it up by token, so a room you renamed on either surface is still recognised as yours. The name is only used the first time. Before that record existed, renaming `general` made the next deploy create a second room under the old name — and then another on the deploy after that (ISSUE-342).

The record starts empty on the first deploy after an upgrade, so that run still matches by name. If you are already carrying a duplicate, point the record at the room you kept before deploying:

```bash
istota nextcloud provision-rooms --user alice --adopt general=<token>
```

`--adopt` writes the record and exits without contacting Talk; the token is the last path segment of the room's link. Then delete the other room. Deleting first does not work — the room you kept no longer answers to `general`, so the next deploy would make a third.

A channel you already set is left alone, and so is one you deliberately cleared — turning the execution log off in the web UI stays off across deploys. Reusing a remembered room never counts as making it usable, so that holds for a renamed room too. Also gated on `istota_talk_enabled` and a non-empty app password.

## Inlined dependencies

External role dependencies are inlined as tasks:

- **Docker**: `apt-get install docker.io docker-compose-plugin` (when browser enabled)
- **rclone**: install + config (when rclone configured)
- **rclone mount**: systemd unit for FUSE mount (when mount enabled)
- **nginx**: install + config (when location or web enabled)
- **Node.js**: NodeSource 20.x (when Node.js enabled)

## Update mode

Skip full installation for config changes or code updates:

```bash
ansible-playbook playbook.yml -e "istota_update_only=true"
```

## Post-install

Claude auth is provisioned during install from the `istota_claude_code_oauth_token` variable (generate the token with `claude setup-token`; the wizard prompts for it and the role writes the credentials file). No separate login is needed.

Only if you deployed without the token (and aren't using `ANTHROPIC_API_KEY`), authenticate manually:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

## Adding config fields

When adding new fields to the config system:

1. Add the field to the dataclass in `config.py`
2. Update `config/config.example.toml`
3. Update `deploy/ansible/defaults/main.yml`
4. Update `deploy/ansible/templates/config.toml.j2`
