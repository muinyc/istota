---
paths:
  - "src/istota/config.py"
---

# Config Module (`src/istota/config.py`)

## Dataclass Definitions

### `LoggingConfig`
```
level: str = "INFO"          output: str = "console"     file: str = ""
rotate: bool = True          max_size_mb: int = 10       backup_count: int = 5
```

### `NextcloudConfig`
```
url: str = ""                username: str = ""          app_password: str = ""
share_default_expire_days: int = 14
dav_prefix: str = ""         auto_share_bot_dir: bool = True
```

`dav_prefix` is where the daemon's storage root sits inside the *bot account's*
Nextcloud file tree. Blank on bare metal, because they are the same directory:
the rclone remote points at `remote.php/dav/files/<bot>/` and is mounted at
`nextcloud_mount_path`, so `/Users/alice` on disk is `/Users/alice` over DAV. On
the Docker shape `/mnt/shared` is an ordinary volume Nextcloud serves through a
`files_external` mount, so the same directory is `/Shared Files/Users/alice` to
the bot; compose owns that mount name (`x-shared-mount-name`) and hands it to
`provision-nc.sh` and to the daemon as `ISTOTA_NEXTCLOUD_DAV_PREFIX`.

**Where it is applied, and the two places it must not be.** The mapping is
`nextcloud._http.to_remote_path`, called from the DAV URL builder, the SEARCH
scope href and the OCS share `path`, with `dav.href_to_path` as the inverse —
so a path coming back is logical again and `list_dir`'s own filter still
matches. Not on `storage.BOT_USER_BASE`: `_get_mount_path` builds on-disk paths
from the same helper and would write to `/mnt/shared/Shared Files/Users/…`. Not
inside `resolve_scoped_path`: that is the confinement boundary keeping the skill
in the caller's workspace, and it keeps speaking logical `/Users/{uid}`. The
skill CLI is a subprocess with a manifest-built environment, so it carries the
value as `NC_DAV_PREFIX` (`skills/nextcloud/skill.md`) rather than inheriting a
Config.

`auto_share_bot_dir` gates the boot-time OCS share of the bot workspace back to
the user in `ensure_user_directories_v2`. True on bare metal, where that share
is how the user gets the directory. False on Docker, set as a compose *literal*
rather than an interpolation: `provision-nc.sh` already mounts the same
directory into the user's tree at provisioning, so the share would hand them a
second copy of it under the received-share name.

### `TalkConfig`
```
enabled: bool = True         bot_username: str = "istota"
```

### `EmailConfig`
```
enabled: bool = False        imap_host/port/user/password    poll_folder: str = "INBOX"
smtp_host/port/user/password                                 bot_email: str = ""
imap_timeout_seconds: int = 30                               confirm_sender_match: str = "off"
dmarc_canary: bool = True                                    dmarc_canary_warn_on_missing: bool = False
authserv_id: str = ""
```
Properties: `effective_smtp_user` (L53), `effective_smtp_password` (L57) — fall back to imap creds

`confirm_sender_match` is a three-state policy (`off` | `verify` | `gate`, ISSUE-249 Gap 3) deciding what the own-address branch of `is_trusted_email_sender` is worth. `off` (default) takes the `From:` as proof; `gate` never does, so every self-claim is held for an out-of-band yes/no; `verify` takes it as proof exactly when `_authentication_verdict` returns `pass` — a stamp carrying our `authserv_id` whose `header.from` aligns with the routed address. One expression, `_own_address_claim_counts`, feeds `include_own_addresses`. **Fails closed**: anything short of that `pass`, including a `None` result where no verdict was computed (the thread route), is held. **`verify` raises at config load without `authserv_id`** — unscoped, the verdict comes off whichever header arrived on top, which the sender writes, so it would gate on an attacker-chosen value. The legacy booleans still load (`false`→`off`, `true`→`gate`), as do the strings `"true"`/`"false"` that Ansible produces when a YAML boolean reaches a quoted slot; anything else raises, matching `outbound_approval_floor`. Ansible asserts the value *and* the `verify`-needs-an-id rule before templating. Read it as a **declaration about the inbound mail path**, not a safety switch: `off` asserts something upstream already authenticated the header — normally DMARC enforcement at the receiving MTA, which rejects a forgery before it reaches `poll_folder`; `gate` asserts nothing does; `verify` asserts the MTA checks and stamps, and we read the stamp. Upstream is still the better place to solve it (silent, no per-message cost, cannot be talked past by a human approving a prompt), and `off` is the behaviour every deployment ran before `verify` existed. `verify` is what makes the gate usable at all: `gate` is noisy by construction because nothing in a plain SMTP message separates the user from someone claiming to be them, so it has to ask about every self-sent message, and the MTA's verdict is the signal that finally tells them apart. `_sender_match_policy` is the reader — it normalises case and the legacy booleans for anything that builds an `EmailConfig` directly, since a stray `False` meaning `off` and silently becoming `gate` would hold every message and expire it into cancellation. Ansible `istota_email_confirm_sender_match` (now a *string*; the assert admits the YAML-boolean forms). See `.claude/rules/transport.md` "Email confirmation gate".

`dmarc_canary` (default **on**) is the monitoring for what that default assumes, not a second gate — it warns and alerts when a self-claim arrives carrying a DMARC verdict other than `pass`, and never changes what happens to the message, which is why it is safe on by default. `dmarc_canary_warn_on_missing` (default **off**) extends it to mail carrying no DMARC verdict at all; off because a path that stamps nothing would warn on every message, and the only reason to turn it on is knowing your MTA does stamp. Note `dmarc=none` is a *failure* here (the domain publishes no policy — the drift case), not the missing class. Ansible `istota_email_dmarc_canary` / `istota_email_dmarc_canary_warn_on_missing`. See `.claude/rules/transport.md` "The DMARC canary".

`authserv_id` (default **blank**, ISSUE-249) scopes which `Authentication-Results` headers the canary reads to the ones carrying the receiving MTA's own RFC 8601 authserv-id. Blank keeps the topmost-header-only read, which is a proxy for "ours" that inverts exactly when the MTA stops stamping — the drift the canary exists to catch. Setting it is also the operator's assertion that their MTA stamps, so "no header of ours" (`unstamped`) warns without `dmarc_canary_warn_on_missing`; that flag stays scoped to "our stamp is there and carries no DMARC verdict" (`unevaluated`). Ansible `istota_email_authserv_id`, Docker `ISTOTA_EMAIL_AUTHSERV_ID`.

### ntfy push notifications

ntfy is a per-user connected service — there is no global `[ntfy]` block or
`NtfyConfig` dataclass. Each user supplies their own server URL, topic, and
(optional) auth via the encrypted `secrets` table. See "ntfy" under
`secret_schema.CONNECTED_SERVICE_SCHEMA`. Priority is hardcoded to 3 (the
ntfy default); per-call overrides flow through `send_notification(...)`.

### `BrowserConfig`
```
enabled: bool = False        api_url: str = "http://localhost:9223"    vnc_url: str = ""
```

### `DevboxConfig`
```
enabled: bool = False                container_prefix: str = "devbox-"
docker_cli: str = "/usr/bin/docker"  # `reset` only; nothing else shells docker
max_output_bytes: int = 102_400      # per stream, in the JSON envelope
```
Per-user persistent Docker container. When `enabled`, the executor exports `ISTOTA_DEVBOX_CONTAINER`, `ISTOTA_DEVBOX_DOCKER_CLI` and `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` (container name = `f"{container_prefix}{task.user_id}"`). The skill CLI reaches the container over the **exec transport** — a Unix socket into a server running inside it. Two verbs also speak Docker, about the container rather than into it: `status` adds a `docker inspect` for the container's own facts, and `reset` wipes `/home/dev` and `docker restart`s it, entirely in Docker. Both run host-side in the CLI's own process, with the daemon's environment and no `DOCKER_HOST`.

**Six keys are retired, and the sweep in `tests/test_ansible_config_template.py` is what keeps them from coming back**: `docker_socket`, `exec_timeout_seconds`, `api_proxy_enabled`, `api_proxy_socket_dir`, `api_proxy_exec_ttl_seconds` and `api_proxy_audit_log`. `load_config` reads none of them, so a value left in a TOML file is inert — and since the loader became a dataclass walk it is also *named*, in the one unrecognised-key warning at startup, rather than being discarded in silence. That reverses an earlier decision not to warn about these, which was made when the alternative was hand-writing the check per key; a retired setting an operator can still see in their file is worth one line. The Docker-API allowlist proxy they configured is deleted whole — module, both Ansible templates, the per-user units — because its only consumer in the tree was an unconditional bind of its socket into every sandbox, and nothing in a build needs Docker any more. `exec_timeout_seconds` went with the transport's own answer to timeouts: there is no default, the task's budget governs, and a caller wanting a kill passes `--timeout`.

**There is deliberately no `exec_socket_dir` here.** The skill CLI resolves the socket through `config.exec_socket_path`, the same helper the executor's bwrap bind and the `doctor` transport check use, so `/run/istota-exec` has one spelling in the tree. A mirror in this block could only be dead code — `ContainerConfig.exec_socket_dir` carries a non-empty default, so its value always wins — or a second knob for a value the design says has one. `tests/test_skills_devbox.py::TestTheSocketPathComesFromConfig::test_the_devbox_block_carries_no_second_spelling` holds the absence, so a later reader finds the decision rather than the gap.

Image is built from `docker/devbox/Dockerfile`, and the Ansible role is the only deployment that runs a container from it — `docker/docker-compose.yml` ships no devbox service, because nothing in that shape can reach one (`docs/deployment/docker.md`).

### `ContainerConfig` (`[developer.container]`, on `DeveloperConfig`)
```
exec_socket_dir: str = "/run/istota-exec"   # the parent; socket is {dir}/{user_id}/exec.sock
connect_timeout_seconds: float = 5.0        idle_timeout_seconds: int = 3600
shim_commands: list[str] = DEFAULT_SHIM_COMMANDS   # fifteen, listed below
```
How the exec transport is configured — **not whether it is used**. That is `container_backend(config)`, derived from `[devbox] enabled` together with `developer.enabled` and a non-empty `repos_dir`. A **deploy-time** choice, not a runtime one: within a deployment there is exactly one place a build happens, which is what keeps the property a per-command fallback would cost — nothing on the host ever consumes an environment the container built, so no parity rule has to hold. With a devbox, one shim per entry in `shim_commands` goes into the task's shim directory and routes those commands into the container; without one, no shim is written, no socket is bound and no container is reached.

**The `backend` key is retired** (`none` | `devbox`). It could disagree with `[devbox] enabled` in both directions and each pairing was a deployment nobody wanted: the devbox on with the backend off offered the model a devbox skill whose every verb but `reset` refused, and the reverse asked the developer skill to reach a container the role had never built. `_parse_container_block` warns on a file still carrying it rather than ignoring it silently — an operator who wrote `backend = "none"` to keep builds on the host had that honoured until the change, and on the next deploy their devbox starts taking the work. `doctor`'s `developer.container.backend` repeats the warning where someone will look, and it re-derives from the file's three inputs rather than reading a key, since a check reading a key nobody sets would report OK on every deployment forever.

**Derived from configuration, never from availability.** Asking whether the container is up would make a stopped devbox silently reroute builds onto the host — same commands, different containment posture, no error anywhere. A configured-but-unreachable transport fails loudly instead: the shims exit 120 and say why.

`DEFAULT_SHIM_COMMANDS` is `npm npx pnpm yarn node uv uvx pip pip3 cargo rustc rustup go bundle gem`. `_UNSHIMMABLE_COMMANDS` / `_UNSHIMMABLE_RE` refuse an operator's additions of the interpreter (`python`, `python3`, `python3.12`, …), the shells, `env`, `git`, `gh`, `glab` and `istota-skill`, whatever is written in config: the sandbox launches its own network bridge as `python3 {bridge_path}`, several recipes in `developer/skill.md` parse forge output with `python3 -c`, and the exec client is itself a Python script, so shimming the interpreter routes all three into a container that has none of them. `make` is merely *absent* from the default and stays configurable — shimming a driver inverts routing for everything beneath it, since the shim directory exists only in the sandbox's `PATH`, so a Makefile calling `git`, `gh` or `python3` would get the container's copies.

**`developer.repos_dir` is a per-user root whether or not a devbox is in play.** The daemon derives `{repos_dir}/{user_id}` through `config.repos_root(config, user_id)`, and every consumer that scopes a task takes that: the bwrap bind, the native file-tool write root, `DEVELOPER_REPOS_DIR` (via the `config_per_user` `EnvSpec` source), `git_remote_scrub` and the devbox mount. Two consumers deliberately keep the **global** root, because neither has a user to scope to: `worktree_reaper`'s sweep, which is deployment-wide and would silently keep every worktree of a user missing from any list it was handed, and `_protected_cache_parents`, where equal-or-ancestor makes the global entry the stricter test. Helpers: `repos_root`, `container_backend`, `devbox_container_backend`, `exec_socket_dir`, `exec_socket_path`.

### `ConversationConfig`
```
enabled: bool = True                lookback_count: int = 25
selection_model: str = "fast"       selection_timeout: float = 30.0
skip_selection_threshold: int = 3   use_selection: bool = True
always_include_recent: int = 5      context_truncation: int = 0
context_recency_hours: float = 0    context_min_messages: int = 10
previous_tasks_count: int = 3       talk_context_limit: int = 100
```

### `SchedulerConfig`
See `.claude/rules/scheduler.md` for full table of fields and defaults.

### `SleepCycleConfig`
```
enabled: bool = True               cron: str = "0 2 * * *"
memory_retention_days: int = 0     lookback_hours: int = 24
auto_load_dated_days: int = 3      curate_user_memory: bool = False
curation_log_summary: bool = True
extraction_model: str = "general"  curation_model: str = "general"
knowledge_graph_audit_retention_days: int = 365  # KG audit pruning; independent of memory_retention_days
```

### `ChannelSleepCycleConfig`
```
enabled: bool = True         cron: str = "0 3 * * *"
lookback_hours: int = 24     memory_retention_days: int = 0
```

### `LocationReceiverConfig`
```
enabled: bool = False                    webhooks_port: int = 8765
accuracy_threshold_m: float = 100.0      # pings worse than this skip place match + state machine
visit_exit_minutes: float = 5.0          # continuous away time before closing an open visit
reconcile_enabled: bool = True           # periodic re-derivation of closed visits from pings
reconcile_lookback_hours: float = 6.0    # reconcile window
reconcile_buffer_minutes: float = 10.0   # don't touch pings newer than this (keeps open visit safe)
reconcile_grace_minutes: float = 10.0    # time away before an unassigned ping closes a visit
reconcile_min_pings: int = 3             # walk-by filter
reconcile_min_dwell_sec: int = 60
```

### `WebConfig` (`[web]`) — auth mode + token retention
```
auth: str = "nextcloud"            # "nextcloud" | "none"; env ISTOTA_WEB_AUTH; unknown → warning + "nextcloud"
port: int = 8766
token_storage: str = "ephemeral"   # "ephemeral" | "encrypted"; anything else → warning + ephemeral
max_avatar_kb: int = 4096          # profile-picture upload cap; header + running total; 0 = uploads off
avatar_import_from_nextcloud: bool = True   # import users' Nextcloud pictures; custom avatars only
```
`max_avatar_kb` bounds one endpoint, and it is deliberately not the same number
as nginx's `client_max_body_size` (rendered from
`istota_web_chat_max_attachment_mb`, 100 MB). nginx bounds what reaches the
process at all; this bounds what the avatar route accepts, and it is checked
twice — on the declared `Content-Length` before the body is read, and again on
the running total as the stream arrives, because the declared length is a
claim. Neither substitutes for the other, and a cap checked on
`len(await file.read())` would materialize whatever nginx let through before
refusing. Ansible: `istota_web_max_avatar_kb`.
`avatar_import_from_nextcloud` switches the scheduler's import job on, and the
cadence is `[scheduler] avatar_import_interval` (6h). Only a **custom** avatar
is imported: Nextcloud answers its avatar endpoint with a picture whether or
not the user set one, generating a coloured letter when they have not, and
importing that would swap the app's own initial chip for Nextcloud's version of
the same idea with nothing downstream able to tell them apart. The distinction
is one response header, so a Nextcloud that does not send it imports nothing and
`doctor`'s `web.avatar_import` says so — the job records what the last tick saw
in `shared_kv`, because that check opens no socket. Inert on a local storage
backend. Ansible: `istota_web_avatar_import_from_nextcloud`.
`auth = "none"` is the local single-user no-auth mode: `web_app._require_api_auth`
early-returns the fixed local user (`Config.local_user_id`), `_user_is_web_admin`
is True for that user, `_verify_origin` no-ops, and `_resolve_session_secret`
generates a random per-process key instead of crashing import. `serve` refuses to
bind no-auth to a non-loopback host (`web_app.assert_no_auth_bind_safe`). Default
`"nextcloud"` = unchanged server behaviour. See AGENTS.md "Local single-user
install".
`"encrypted"` retains the login's user-scoped Nextcloud OAuth pair in the
`web_user_tokens` framework table, encrypted with the **web-only**
`ISTOTA_WEB_TOKEN_KEY` env var (≥32 chars; distinct scrypt salt from the
`ISTOTA_SECRET_KEY` store — see `src/istota/web_tokens.py`). The key is a
runtime env var like `ISTOTA_SECRET_KEY`, *not* a config field, and is
delivered only to the web unit (Ansible `web-secrets.env` /
Docker `/data/.web_token_key`). `"encrypted"` without the key logs one ERROR
at web startup and behaves as ephemeral. Docker-path override:
`ISTOTA_WEB_TOKEN_STORAGE` env var (validated the same way).

### `WebMapConfig` (`[web.map]`) — where map tiles come from
```
provider: str = "openfreemap"   # openfreemap | carto | osm | custom; unknown → warning + openfreemap
api_key: str = ""               # carto only; public by construction
dark_style: str = ""            # custom only: a MapLibre style URL
light_style: str = ""           # custom only
attribution: str = ""           # custom only
```
The seam that replaced two hardcoded CARTO URLs in `LocationMap.svelte`
(ISSUE-334). Resolution is `istota/map_basemap.py`, a stdlib-only leaf read by
both consumers — `GET /istota/api/map/basemap` and the `web.basemap` doctor
check — so the checker cannot pass while the map is blank. Never raises and
never returns an unusable spec: an unknown provider, a `custom` with no URL or
a non-http(s) one, **and a keyed provider with no key** all fall back to
`openfreemap` and set `fell_back`.

That last one is the case worth being explicit about. Returning the keyless
CARTO templates with a `needs_key` flag set was the original bug wearing a
label — nothing in a browser can act on a flag, so the user still got the
watermark. The flag survives on the fallback spec as the *reason*, which is
what lets doctor say "carto, with no key" instead of the much vaguer "did not
resolve as written".

`api_key` **is not a secret**. MapLibre puts it in the tile URL, so it ships to
every browser that loads a map and appears in every request they make; CARTO
issues these free for exactly that. It is redacted in the admin config view
because it matches `is_secret_name`, which is harmless but not a guarantee
about the value.

A user can also store their own CARTO key from the location settings page
(`MODULE_SERVICE_SCHEMA["location"]["carto"]`). **A stored key selects CARTO
for that user**, overriding `provider` — `map_basemap.select_provider` — because
otherwise pasting a key would do nothing visible and the reason would live in a
file the user cannot reach. The endpoint returns that key only already embedded
in the tile URL, never as a field, so the secrets store stays write-only to the
browser everywhere it means something.

**`web.basemap` opens no socket, deliberately.** Two measured facts remove the
value a probe would have. The watermark is invisible to a fetch: CARTO returns
200, `content-type: image/png`, and a byte-identical body and ETag for a
keyless request and one with a bogus key (measured 2026-08-28), so a probe
reports a working basemap for a defaced one. And the daemon is the wrong host —
tiles are fetched by the browser, over a different route, so a proxied
deployment would fail a probe for a basemap every browser renders. Running it
anyway also put a third-party request on the boot path and the hourly sweep,
where a CDN blip becomes a FAIL and pages the operator. A keyed CARTO is
therefore reported as *configured*, never as verified.
`tests/test_doctor_basemap.py::TestItOpensNoSocket` holds this.

Docker: `ISTOTA_WEB_MAP_PROVIDER`, `_API_KEY`, `_DARK_STYLE`, `_LIGHT_STYLE`,
`_ATTRIBUTION`, read by `render-config.sh` and passed through
`docker-compose.yml`. Ansible: `istota_web_map_*` in `defaults/main.yml`.

### `WebChatConfig` (`[web.chat]`) — read-sync knob
```
talk_read_sync_interval: int = 60   # Talk→web read pull cadence (s); 0 disables the pull
```

### `CaldavConfig` (`[caldav]`)
```
url: str = ""    username: str = ""    password: str = ""
```
Explicit CalDAV override. When any field is set it overrides the value the
`Config.caldav_url` / `caldav_username` / `caldav_password` properties otherwise
derive from `[nextcloud]` — so a local install can point calendar at an external
CalDAV server (Radicale, Fastmail, Google) with no Nextcloud. All-blank (default)
= NC derivation, so server deployments are unchanged. Related: `Config.is_standalone`
(blank `nextcloud.url` + `web.auth == "none"`) and `Config.local_user_id` (the
sole configured user, for no-auth mode).

### `SiteConfig`
```
hostname: str = ""
```
The deployment's public DNS name (web OAuth2 redirect + origin/CSRF checks +
location webhook URL). That is the whole dataclass — static hosting is not an
istota concern.

Per-user `/~user/` static sites were removed in ISSUE-171; the instance-wide
one (`enabled` + `base_path`) followed in **ISSUE-194**. It was bound RW into
the sandbox and served unauthenticated by nginx, which made `cp <anything>
$WEBSITE_PATH/` a public egress the confirmation model classified as a benign
local write — the gate framework is channel-aware (email / share / ntfy /
browser POST) and a filesystem write to a publicly-reachable path was an
unnamed channel. Removing the primitive was chosen over teaching the model to
reason about "is this path public", since that judgement is exactly what an
injected instruction talks its way past. Gone with it: the bwrap RW bind and
`native_fs_roots` write root, the `WEBSITE_PATH`/`WEBSITE_URL` env vars, the
"Web Root" prompt resource section, and the `WEBSITE_PATH`-shaped Ansible
plumbing. A stale `enabled`/`base_path` in TOML logs a warning and is ignored.

Ansible does serve a static root at `/` again (`istota_web_root_enabled`,
default on, `istota_web_root_path` = `/srv/www/html`), but it is not the same
primitive: the directory is root-owned, lives outside `istota_home`, is bound
into no sandbox, and the istota user cannot write it — so there is no path
from the agent to a published file, which is the property ISSUE-194 was
about. It is operator content, managed only by the playbook (the templated
`index.html` is written `force: no`, so it seeds an empty root once and never
overwrites). Disabling it restores the explicit `location / { return 404; }`,
which the vhost still needs in that mode — with no `root` the server block
would inherit the http-level default and serve the distro page.

### `NetworkConfig`
```
enabled: bool = True         allow_pypi: bool = True      extra_hosts: list[str] = []
```

### `SecurityConfig`
```
sandbox_enabled: bool = True         skill_proxy_enabled: bool = True
skill_proxy_timeout: int = 300
passthrough_env_vars: list[str] = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
sandbox_ro_paths: list[str] = []     # extra RO binds; keep narrow
sandbox_cache_dir: str = ""          # FALLBACK cache root; unread while developer.repos_dir is set
sandbox_cache_sweep_enabled: bool = True   # bound what the per-user caches grow to (ISSUE-317)
sandbox_cache_max_gb: float = 10.0         # per user; clamped to a 1 GiB floor
network: NetworkConfig = NetworkConfig()
```
`skill_proxy_enabled` is **required wherever `sandbox_enabled` is true**, for two
reasons, and the load-time warning names both (ISSUE-393). The quiet one is
credentials: `_split_credential_env` removes the manifest-declared sensitive vars
only inside the proxy branch of `execute_task`, so with the proxy off they stay
in the environment handed to the model — and with a sandbox on that is a real
boundary they now sit inside, which is the opposite of what switching a sandbox
on is for. The loud one is databases: the DB directories are masked out of the
sandbox, so a skill CLI that can't reach the proxy has nothing to open and
`skill_client._run_direct` refuses (on `ISTOTA_SANDBOXED`) rather than failing as
a missing table. Both switches **off** together is a different shape and is not
warned about: `setup_wizard` writes that pair for the single-user install, the
task then runs unconfined as the daemon user, and there is no boundary for an env
var to cross.

`sandbox_ro_paths` defaults to `[]` and is now **parsed from TOML** — it never
was, so every deployment silently ran the hardcoded old default (`["/srv/app"]`)
and no operator could narrow it. That entry existed for a co-located moneyman
install that no longer exists, and since `istota_home` lives under it, it
exposed the framework DB and every user's module DB to every task.
`build_bwrap_cmd` masks `db_path.parent` + `module_db_root()` *after* applying
this list, so a broad entry can't undo that.

One thing did depend on the old entry: with `custom_system_prompt = true` the
CLI opens `config/system-prompt.md` from *inside* the sandbox, and `/srv/app`
was what put it there. Narrowing the default took it away and every task on
such an install exited "System prompt file not found". `build_bwrap_cmd` now
binds that single file itself (`custom_system_prompt_path`), so no operator
needs an entry here for it — and `config.toml`, its neighbour, still isn't in
the sandbox.

`sandbox_cache_dir` is the **fallback** root for the package managers' caches (uv, npm, and anything else honouring `XDG_CACHE_HOME`), read only where the developer skill is not configured. Empty is the default and keeps the pre-ISSUE-305 behaviour, which is that `$HOME/.cache` exists inside the namespace only as a directory bwrap created on its own root tmpfs: a `uv sync` unpacks into RAM that `host_pressure.read_tmpfs_usage` cannot attribute — the mount is in the task's namespace and in no table the host reads, so it lands in `shmem_unaccounted` — and the whole cache is discarded at task exit.

**Where a cache goes is decided by `resolve_sandbox_cache_dir`, and it has two branches.** With `developer.enabled` and `developer.repos_dir` set the path is *derived*: `{repos_dir}/{user_id}/.package-caches`, inside the subtree `build_bwrap_cmd` binds for that task, and this key is not consulted at all. Without them it is `{sandbox_cache_dir}/{user_id}`, which serves a deployment running the sandbox without the developer skill — nothing binds an ancestor of it, so it is its own mount and a venv in the task workspace pays the copy, the same cost it always paid rather than a regression.

**Per user in both shapes.** A single shared directory would be the first RW surface a non-admin task and an admin task hold in common, and it persists across tasks by construction; uv's unpacked-wheel cache is trusted on read and never re-verified against a hash, so a planted archive is executed by the next `uv sync` that hardlinks out of it. Per-user costs nothing the placement argument was about — hardlink sharing is between one user's worktrees, which stay inside one subtree.

**The derivation exists for the mount.** uv populates a venv by hardlinking out of its cache and `link(2)` compares **mounts**, not devices, so a cache root anywhere else returns EXDEV even on one filesystem and every worktree pays for a full copy. Measured four ways on the reference deployment (ISSUE-319): host with no namespace works, two separate binds gives EXDEV, one bind covering both works, and re-binding the cache on top of the repos bind gives EXDEV again. The cache bind is emitted first and the repos bind — its ancestor — after it, so the second covers the first. That covering is the entire mechanism, and carving the cache back out costs exactly what putting it elsewhere costs. For the same reason `resolve_sandbox_cache_dir` returns the path **as written** rather than resolved: `_bind` uses the string it is handed as the sandbox destination and the repos bind passes its path unresolved, so resolving here would put a symlinked `repos_dir` and a cache under it at two names, hence two mounts, and hardlinking between them fails silently.

**What that covering exposes is now the calling user's own subtree, which is why there are no masks.** Under the shared root it exposed every other user's cache read-write to every admin developer task, and closing that took about 200 lines in `build_bwrap_cmd`. All of it is gone — `_sandbox_cache_covering_targets`, `_sandbox_cache_is_covered`, `sandbox_cache_sibling_dirs`, `MAX_SANDBOX_CACHE_SIBLINGS`, `_BWRAP_BIND_VERBS`, the argv covering scan, the sibling-mask loop with its root-mask fallback and both error branches, and the matching write denials in `native_fs_roots`. In their place is one assertion: the derived directory must resolve to exactly the path the layout names. That is the invariant the layout rests on, and it is a line plus a test rather than an argv walk. `_mask_dir` keeps the `bool` return the mask loop added — it is a genuine improvement over a silent `continue` on a delete-adjacent path, and the database mask callers can start reading it.

**The assertion is not decorative**, because the cache's parent is bound read-write into the task's own sandbox. A symlink planted at `.package-caches` would otherwise be created, `chmod 0700`-ed and bound by the daemon, which is ISSUE-319 back through a name. The mode goes on through an `O_NOFOLLOW` fd rather than a path, since `mkdir(exist_ok=True)` and `os.chmod` both follow symlinks and re-traverse by name.

**The `--disable-userns` precondition on the cache bind went with the masks**, and the argument it left behind is recorded rather than settled. It was there because a tmpfs mask can be unmounted from a nested user namespace, and there is no mask to defend now. It was also pinning the cache directory as a mountpoint, and `rename` on a mountpoint returns `EBUSY` — so removing it reopens a window between the containment check and `_bind`'s own resolution at `execve` in which a symlink could be swapped in, walkable in principle by a second concurrent task for the same user, since `user_max_foreground_workers` defaults to 2. Nothing in the default suite can answer that: it needs a real bwrap where the flag probes false, two concurrent admin tasks for one user, and a loop racing the swap. Restoring the precondition is not free either — on a bwrap without the flag it refuses the cache outright, which is the EXDEV full copy ISSUE-305 exists to avoid. Kept deleted, raised as ISSUE-320, with a comment at the bind site pointing there. The flag itself is unchanged for the database masks, where a refusal costs defence in depth rather than the boundary.

**Every refusal lives in `resolve_sandbox_cache_dir`, not in `build_bwrap_cmd`,** and that placement is the correctness argument rather than a tidiness one: the bind, the cache environment in `execute_task` and `native_fs_roots` are three consumers of one predicate, and they drop together or not at all. It **never raises** — both callers are on the task path, and for NativeBrain `build_bwrap_cmd` runs per Bash call — and the branch selection sits *inside* the `try`, since `Path.resolve()` raises `ValueError` on an embedded null byte and the join raises `TypeError` on a non-str user id. Every rejection falls open to the pre-ISSUE-305 behaviour: a relative path, a root that is not an existing writable directory, anything under a database directory (checked here, since `_validate_workspace_dir` skips a relative `db_path`), the rest of that function's blocklist, and **anything at or above a path the sandbox already mounts**. That last one is `_sandbox_bind_targets`: bwrap applies argv in order and the cache bind is emitted late, so a destination above an earlier mount covers it — `= $HOME/.cache` overmounts the read-only huggingface bind, `= config.temp_dir` hands every user's workspace to every task and makes the `.developer` credential helpers writable again, `= $HOME/.local` gives the model write access to the `claude` binary. It answers that one direction only, and inferring from "a cache inside a bind target covers nothing" that it is therefore safe is what kept ISSUE-319 invisible for a release. Each distinct refusal warns once per process, not twice per task.

**The protection checks run against the cache's parent**, on both branches, which can only ever refuse more. One consequence has no escape hatch any more and is worth stating plainly: `_validate_workspace_dir` overlaps in *both* directions, so a `developer.repos_dir` overlapping the source tree, the Nextcloud mount, a database directory or a `$HOME` dotfile directory loses its disk cache on every task — and `sandbox_cache_dir` cannot be used to put it somewhere else, because that branch is not taken. The refusal is a fact about `repos_dir`, and the fix is to move `repos_dir`.

**The residual ISSUE-319 declined to close is closed by the layout rather than by a guard.** The old shared root stayed writable inside the sandbox, so a task could create a cache directory for a user who had not run one yet, populate it, and have the daemon adopt it on that user's first task. There is no shared root in the namespace now: a task sees `{repos_dir}/{user_id}` and nothing above it, so there is no name left to plant.

One consequence of the placement, since it is not obvious: `git_remote_scrub.find_git_dirs` walks under `repos_dir` on every task and on every reaper sweep, and uv's `archive-v0` is one directory per unpacked wheel. `_MAX_DEPTH` already stops it descending into them, but it still lists and lstats every one — 25 ms over 4,500 directories, and a per-sweep log line claiming thousands went unswept for credentials. Both callers pass the derived cache as `skip`: the developer skill's `setup_env` passes the one cache under the subtree it sweeps, and `scheduler.check_worktree_reap` enumerates `{repos_dir}/*/.package-caches` from disk, since a cache appears on a user's first task. For the reaper that prune is not only performance — a git directory inside a cache would otherwise be evaluated as a reap candidate, which fetches against a model-written `remote.origin.url` from the unsandboxed scheduler, outside the CONNECT allowlist, every sweep. The cost of the prune, stated rather than implied: a repository parked under a cache directory is not swept.

The environment variables (`UV_CACHE_DIR`, `XDG_CACHE_HOME`, `npm_config_cache`, and `HF_HOME` pinned back to `~/.cache/huggingface` so moving XDG does not orphan the read-only model-cache bind) are set in `execute_task`, in the model-only block **after** `proxy_base_env` is snapshotted — deliberately not in `build_clean_env`. That function feeds the proxy's base env, which SkillProxy hands every host-side skill CLI: a process running unsandboxed as the daemon user has no business resolving a cache out of a directory the model can write, which is the same confused-deputy shape the `ISTOTA_PATH_PREPEND` handling guards against.

**The sweep follows the cache, not the key.** `sandbox_cache_sweep_enabled` / `sandbox_cache_max_gb` / `[scheduler] sandbox_cache_sweep_interval` bound what these directories grow to (ISSUE-317, `src/istota/sandbox_cache_sweeper.py`). Moving the caches onto disk is what makes them **persist**, and nothing pruned them, so the fix for a bounded RAM burn was an unbounded disk leak on the volume the worktree reaper is already fighting for. `scheduler.sandbox_cache_sweep_root` reproduces `resolve_sandbox_cache_dir`'s branch selection, so the blank Ansible default for `sandbox_cache_dir` switches nothing off on a developer deployment — the sweep walks `{repos_dir}/{user_id}/.package-caches` for each user instead. It reproduces the *branch selection* and deliberately not the refusals, because a root the resolver never writes into is a sweep that finds nothing while the real caches grow. On the derived layout the user ids come from `config.users` and the sweeper derives one path each, reading no name back out of the tree: entries under `repos_dir` are model-writable, and a user id is the one axis that must not come from there. An empty user list is reported rather than passing as a silent no-op. The price is visible rather than hidden — `report_orphan_caches` names a cache belonging to nobody the caller listed, which the one-level shape used to catch by enumerating, and acts on nothing.

**A size ceiling, not an age rule.** One `uv sync --all-extras` writes about 1.8 GB in a single command, so a window phrased in days either keeps everything or throws away a cache minutes old and about to be reused. Every visited cache gets the package managers' own cheap reclaim first (`uv cache prune`, `npm cache verify`), which keeps the warm entries; only one still over its ceiling afterwards is wiped with their `clean` verbs. **The sweeper deletes no file itself** — not the root, not a per-user directory, not a cache entry. A tool that is missing, that fails or that times out is reported and the cache is left alone, because the difference between "uv's cache" and "everything the model put in this directory" is what uv knows and the sweeper does not.

**Three guards stand between a running task and a wipe**, since unlinking a cache entry under a `uv sync` turns its next `link(2)` into `ENOENT`. `scheduler.check_sandbox_cache_sweep` reads the users with a `locked` or `running` task and passes them in; a user in that set is skipped entirely, including the cheap reclaim, since `prune` unlinks too, and an *unreadable* task table cancels the whole sweep rather than arriving as an empty set that reads as "nobody is working". Behind that, an idle window on the cache tree's newest mtime catches a writer the task table never knew about. Behind that, `--force` is never passed to uv, so uv's own in-use check still stands — which is the only one of the three that sees a sync against a fully warm cache, since that writes nothing and merely hardlinks out. npm's `--force` on `cache clean` is a different flag with no in-use check behind it.

**A fourth thing guards the sweep's own aim, and the derived layout is what forced it.** The module used to claim that resolving a path before handing it to a subprocess left nothing to swap. That held under one level for a structural reason nobody had recorded — the cache root's parent was never bound into a sandbox — and it is false under two, where `.package-caches` is an ordinary entry in a directory the task writes. The directory's `(st_dev, st_ino)` is pinned through an `O_NOFOLLOW` open and re-asserted before each round and before each tool; one that changed identity mid-sweep reports `swapped` and nothing further is run against it. Reverted, the control reports the victim's cache wiped at zero bytes.

`sandbox_admin_db_write` was **removed**: the framework DB is no longer bound
into the sandbox for anyone, so there is no bind left to widen. A stale key
logs a WARNING at load and is ignored.

### `SkillsConfig` — removed

`SkillsConfig` and the `[skills]` config section are **gone** (no
`progressive_disclosure`, `auto_lazy_threshold_chars`, or `always_eager` knobs).
The two-axis eager/lazy "progressive disclosure" model collapsed into one axis:
a skill is either **eager** (full body in the prompt, because `select_skills`
picked it deterministically) or in the **menu** (a one-line "load on demand"
entry the model pulls in full via `istota-skill skills show <name>`). The menu —
the full eligible catalogue (`eligible_skill_names`) minus the eager set and its
`exclude_skills` — is intrinsic, with no master gate and no per-skill
body-deferral flag, so there are no routing knobs left to configure. A stale
`[skills]` block in `config.toml` logs a warning at load time but doesn't fail.
See `.claude/rules/skills.md` for the single-axis model.

### `PlaybooksConfig`
```
enabled: bool = False        # Part B master gate (learned playbooks / procedural memory)
recall_limit: int = 3        # top-K playbooks injected per task
min_tool_calls: int = 4      # a task must use >= this many tools to qualify (LLM-judged in the extraction prompt)
retention_days: int = 90     # 0 = keep forever; >0 = age-prune by last-use mtime (recall stamps it)
max_chars: int = 0           # 0 = share the global max_memory_chars budget
```
Parsed from `[playbooks]`. A playbook is a per-user markdown procedure distilled
by the sleep cycle from a successful multi-step task, stored under the user's
bot `playbooks/` dir, indexed into `memory_chunks` with `source_type="playbook"`,
and recalled by relevance (`executor._recall_playbooks`). Off by default.
`extraction_model` is reused from `[sleep_cycle]` (no new model knob).

### `BrainConfig`
```
kind: str = "claude_code"                       # "claude_code" | "native" | "tmux_claude"
native: NativeBrainConfig                       # [brain.native] block (native harness)
tmux: TmuxBrainConfig                           # [brain.tmux] block (tmux-driven interactive TUI)
claude_code: ClaudeCodeBrainConfig              # [brain.claude_code] block (subscription usage poll)
source_type_overrides: dict[str, str] = {}      # [brain.source_type_overrides] — per-source-type routing
room_selectable: list[str] = []                 # [brain] room_selectable — kinds a room, or a CRON.md job, may pin; empty = none
fallback: str = ""                              # brain kind to fall back to when primary unavailable
fallback_on_transient: bool = True              # also reroute a persistent transient_api_error (ISSUE-212)
fallback_cooldown_seconds: int = 900            # skip an unavailable primary this long; 0 disables stickiness
```
`fallback` / `fallback_on_transient` / `fallback_cooldown_seconds` drive
availability failover (brain-fallback spec). When the primary brain is
unavailable (usage limit / missing binary / tmux launch failure) the executor
reruns the attempt through the fallback brain with that brain's own settings.
`""` = no fallback, for every brain kind — `brain._fallback.effective_fallback_kind`
is the configured value or None, with no implicit target (ISSUE-362; a
`tmux_claude` primary used to resolve to `claude_code` there with nothing
configured). `_validate_brain_fallback` (config load) neutralizes an unknown kind
with one WARNING, and a self-fallback — read as "the only kind this deployment
runs", so a `source_type_overrides` entry routing elsewhere keeps a value equal
to `kind`, which is the only spelling of "route scheduled work to tmux and fail
it over to the CLI" — with another. It also logs one INFO line per process where
`tmux_claude` runs with no fallback, since that pairing was unconfigurable before
ISSUE-362 and an upgrade would otherwise drop failover silently. See
`.claude/rules/brain.md` "Brain fallback" + `.claude/rules/executor.md`.

`room_selectable` names the brain kinds a room may pin for itself through
`!brain` or the web room settings, **and** the kinds a scheduled job may pin
with `brain` in CRON.md (ISSUE-419). The key's name is narrower than the
setting: it bounds every pin written outside this file, since
`resolve_brain_kind` applies it to any `override` it is handed without knowing
the provenance. A second `job_selectable` was rejected for that reason, and the
rename left as a follow-up rather than done here. The job pin carries one gate
this key does not, at sync time: CRON.md is model-writable, so
`cron_loader.fj_brain_or_none` drops the field for a non-admin — which on a
deployment with an empty admins file is nobody. Empty is the default, so the
feature ships inert and an operator opts in by naming kinds — a gate rather
than a preference,
because a brain kind decides which process holds the agent loop, what tool set
it registers and which sandbox profile is built, and a change to an enforcement
posture should not arrive switched on by an upgrade (`.claude/rules/brain.md` is
exact about which posture, since the obvious answer went out of date with
ISSUE-389). The mapper hook
stringifies and strips each entry and drops the empties, and `_KEEP`s a
non-list; `_validate_room_selectable` warns once at load about a name
`make_brain` cannot build, since nothing else would — `resolve_brain_kind` warns
only when a room actually pins one, which for an unbuildable name is never. Two
consequences beyond the room itself: an admitted pin clears `fallback` for that
task, and `brain.reachable_brain_kinds` folds this list in, so allowlisting a
kind widens the `doctor` checks for it whether or not a room has selected it.
**Both deployment shapes can set it, and neither renders the key at its own
default.** Ansible uses `istota_brain_room_selectable`
(`deploy/ansible/defaults/main.yml` + `templates/config.toml.j2`); Docker uses
`ISTOTA_BRAIN_ROOM_SELECTABLE`, comma-separated, in both
`docker/istota/render-config.sh` and `docker/docker-compose.yml`, per the
testbed two-file rule. An empty list and an absent key load identically, and
both files rewrite `config.toml` from scratch — the play on every run, the
Docker generator on every boot since ISSUE-368 — so a key printed at its own
default invites an edit that cannot survive. Two halves on each shape, and
neither works alone: on Ansible a default with no template line is inert and a
template line with no default fails the render under `StrictUndefined`; on
Docker a variable compose does not pass through never reaches the generator,
which `test_render_config.py::TestTheEntrypointStillOwnsWhatItKept::test_every_var_the_render_reads_is_passed_by_compose`
catches as a blanket scan. The Docker side renders each name through
`toml_escape` rather than judging it: a `"` would otherwise leave a
`config.toml` that does not parse, which on that shape is a container that will
not boot, while whether a name is a real brain kind is already
`_validate_room_selectable`'s answer and a second opinion in a shell script
would go stale at the fourth kind. `TestTheRoomSelectableAllowlist` in both
`tests/test_render_config.py` and `tests/test_ansible_config_template.py`
asserts each shape from both ends. See `.claude/rules/brain.md` "Per-room brain
selection".

`TmuxBrainConfig` (`[brain.tmux]`): `model` / `effort` (this brain's own
defaults, ISSUE-418 — same values and same `anthropic` namespace as
`[brain.claude_code]`, which is why the retired top-level keys migrate onto
both), `fallback_trip_threshold` (5),
`fallback_cooldown_seconds` (300), `ready_timeout_seconds` (30),
`tmux_command_timeout` (10), `cli_version_pin` ("2.1.168"), plus the readiness /
dialog / error / usage-limit marker lists (`ready_markers`, `trust_markers`,
`theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`,
`usage_limit_markers` — pane substrings → `stop_reason=usage_limit` → fallback,
checked before `error_markers`). All defaulted to the prototype's hardcoded
values; see `.claude/rules/brain.md` "TmuxClaudeBrain".

`BrainConfig` selects which `Brain` implementation handles model invocation. `source_type_overrides`
maps a task `source_type` to a brain kind, overriding `kind` for matching tasks
(gradual rollout: cron/heartbeat on native, interactive on claude_code). The
executor routes per task via `brain.resolve_brain_kind(task.source_type, config.brain)`;
unknown target kinds are logged and ignored. See `.claude/rules/brain.md` for the
protocol, ClaudeCodeBrain, NativeBrain, and `NativeBrainConfig` fields.

`ClaudeCodeBrainConfig` (`[brain.claude_code]`) — this brain's own `model` and
`effort` (ISSUE-418), plus the subscription usage poll. The two model fields
replaced the top-level keys, which were this brain's defaults sitting where they
read as deployment-wide and were therefore applied to every brain, shadowing each
one's own; `[brain.tmux]` gained the same pair, and the retired keys migrate onto
both. Its subprocess behaviour is not configurable. It is read whatever `kind` is set to, because a `native` primary
with a `claude_code` fallback (or a `source_type_overrides` entry) burns the
same plan. Every field is defaulted, so an absent block is the shipping
behaviour. Read by `istota.subscription_usage.get_snapshot`, which the doctor
check `runtime.subscription_usage`, the `/admin` stats payload and `!usage` all
share — one fetch per TTL for the whole deployment, from a disk cache at
`{db_path.parent}/subscription_usage.json`. The credential is read, never
written and never refreshed.
- `subscription_usage: bool = True` — poll `GET https://api.anthropic.com/api/oauth/usage` for plan utilization at all. `false` = the doctor check `SKIP`s and the admin card is absent, because the section returns `None` for a snapshot with no windows and a disabled reading has none. The card used to render `Plan limits unavailable: disabled by config` in place of its tiles instead, on the reading that an operator who expects the reading should learn why it is missing. That was right while a missing reading meant something was wrong; the endpoint does not serve the long-lived setup-token credential either server shape deploys, so the note became permanent and named nothing anyone could act on. The reason is carried by `runtime.subscription_usage` as a SKIP, which is where a diagnostic belongs.
- `subscription_usage_cache_ttl_seconds: int = 1800` — **two jobs, one number.** It is the freshness window for a successful reading (one deployment-wide fetch per window; the dashboard's 60s poll therefore costs nothing) and it is also the retry interval after a *failed* one. A failed reading is never cached as a reading, so without the second job nothing would bound the retry: with no prior success every caller would re-fetch, and an open dashboard on a rejected credential is roughly 1,440 live 403s a day against `api.anthropic.com`. So an operator raising this to slow the polling also lengthens how long a transient network blip is suppressed for — up to one TTL, exactly as long as a rejected credential would be. That is deliberate rather than an oversight: this is a diagnostic reading, not a control path, and a separate backoff knob was rejected as a sixth number in a block that already carries five. A success clears the timer immediately, so recovery is never delayed, and a stale-cache reading is still served during the backoff — an old real number outranks a fresh failure. Floored at 1 by the loader; a zero TTL would fetch on every dashboard poll.

  The default is 30 minutes rather than the 5 it shipped with, because the endpoint rate-limits a deployment that polls it harder — and the shortest window it reports is five hours, so a faster poll buys no accuracy for the extra requests. A production host running the 5-minute default never obtained a single successful reading: it was answered with `HTTP 429` and a `Retry-After` of 2327 seconds, retried 7 times inside that window, and stayed limited. Which is the other half of the fix — a stated `Retry-After` now overrides this floor, so this number is the *minimum* backoff after a failure and no longer the whole of it. See `MAX_RETRY_AFTER_SECONDS` in `subscription_usage.py` for the ceiling on what a server can ask for.
- `subscription_usage_timeout_seconds: float = 10.0` — matches `doctor.PROBE_TIMEOUT`. Floored at 1.
- `subscription_usage_warn_percent: float = 80.0` / `subscription_usage_high_percent: float = 95.0` — our own thresholds, applied identically by doctor and the admin tile (the server's own `severity` is carried on the wire but does not drive either). Doctor WARNs and the tile turns amber at or above `warn`, red at or above `high`. **Never a `FAIL` at any utilization** — a busy plan is a fact about the plan, not a defect in the host, and a `FAIL` would exit `istota doctor` non-zero and alert every admin.
- `subscription_usage_stale_after_seconds: int = 3600` — a stale-cache reading older than this is reported `SKIP` rather than as a current one. Not `WARN`: a reading this old means the fetches are failing, which on a server shape is the steady state rather than a fault, so there is nothing left to check — the same reasoning as the no-data branch beside it. The numbers are still shown on the admin card, with their age and the error, because there the alternative is a blank card rather than a misleading verdict.

**Only a failure the endpoint produced is shared; "no credential here" is not.** The retry timer above is a file in the shared data dir, and a 403, a 500 or an unreachable host are facts about the deployment that every process reading that dir is entitled to reuse. A `None` from `resolve_token` is not: it reports the environment and home directory of *the calling process*. `istota-scheduler` and `istota-web` take `CLAUDE_CODE_OAUTH_TOKEN` from a systemd `EnvironmentFile`; an operator's `istota doctor` in a shell usually does not, and on macOS a background agent and a login session do not see the same keychain. Writing that answer into the shared file would have one read-only diagnostic run tell the dashboard for a full TTL that there is no credential while the daemon was happily using one. So the no-credential branch is rate-limited by a **process-local** record instead, keyed by data dir — which is also where its actual cost lives, the macOS `security` subprocess. A token added five minutes later is still picked up within one TTL by the process that could not find it, and instantly by any other.

**A bad value in this block never stops the daemon.** `load_config` runs in the
scheduler, the web app, the webhook receiver and every host-side skill CLI the
proxy spawns per call, so a typo on a knob that only draws a dashboard tile must
not stop any of them from starting. No raw TOML value is ever handed to a bare
`int()` / `float()` — `int(float("inf"))` raises `OverflowError`,
`int(float("nan"))` raises `ValueError`, and TOML spells both. A non-finite
number, a bool in a numeric slot, or a value that is not a number at all logs
one WARNING and takes the dataclass default. This block used to state those
rules in its own hand-written helpers; they are now what `config_mapper`'s
`coerce_int` / `coerce_float` do for every numeric field in the tree, which is
the same guarantee reached from the other direction. One difference from the
old helpers: a *quoted* number (`"1800"`) is now read as the number rather than
refused, matching the quoted-boolean tolerance below — a rendered config is a
place where a value arrives quoted for reasons that have nothing to do with
intent. `subscription_usage` is stricter still: it accepts a real boolean or a
quoted one (`"false"`, `"no"`, `"off"`, `"0"`, case-insensitive) and warns on
anything else, because `bool("false")` is `True` and this is the field that decides
whether the deployment makes an unsolicited outbound request — "operator wrote
false, poll stayed on" is the one failure it must not have.

`_validate_claude_code_brain` (config load, beside `_validate_brain_fallback`)
then corrects rather than refuses, one WARNING per correction: both percentages
clamp to `[0, 100]`; `warn > high` after clamping is lowered to `high` (an
inverted pair leaves no amber band and is more likely a typo than an intent);
the TTL and the timeout floor at 1. A **non-finite** value takes the default
instead of a bound — clamping a NaN percentage would land it at 0.0, i.e. amber
at every utilization for ever on a check whose whole point is that it does not
cry wolf, and an `inf` timeout is both an unbounded socket read and a value the
admin config pane cannot serialize (starlette renders JSON with
`allow_nan=False`, so one would 500 `GET /api/admin/config` instance-wide).
`stale_after_seconds` is deliberately not floored — zero there coherently means
"treat any stale reading as too old". No I/O: the poll is reached only from a
diagnostic path, never from `load_config`.

`subscription_usage.py` is a stdlib-only leaf and carries its own copy of the
three defaults it reads, pinned against this dataclass by
`tests/test_config_claude_code_brain.py::TestOneSourceOfTruthForTheDefaults`.
Its `_positive` guard refuses the same values the loader does but substitutes
the *default* where the loader *floors*: an operator asking for a small TTL gets
1, while a value that reached the dataclass past the loader has no intent worth
preserving. `deploy/ansible/files/validate_config.py` allowlists `claude_code`
under `[brain]`; the role's template renders no block (every field is
defaulted), but an unlisted sub-table would fail the play.

`NativeBrainConfig` (`[brain.native]`) — model-agnosticism knobs (see `.claude/rules/brain.md` "NativeBrain"):
- `model_overrides: dict = {}` (`[brain.native.model_overrides."<model-id>"]`) — per-model partial `ModelInfo` (any of `context_window`, `max_output_tokens`, `supports_thinking`, `supports_vision`, prices). Applied globally at config load via `llm.catalog.set_model_overrides`, merged over the live-fetched entry (or the conservative default) in `get_model_info`. Lets a non-Anthropic reasoning/vision or small-window model declare real capabilities instead of being degraded to no-thinking / no-vision / 200k, and corrects a single wrong field on a fetched model (NB-4). Unknown keys are dropped.
- `model_catalog_fetch: bool = True` / `model_catalog_cache_ttl_hours: float = 24.0` (`[brain.native]`) — live model-catalog enrichment from OpenRouter (ISSUE-182). When `base_url` contains `openrouter.ai`, `NativeBrain._ensure_fetched_catalog` fetches OpenRouter's public `GET /models` list once per process (disk-cached at `{db_path.parent}/openrouter_models.json` with this TTL) and installs real window/capabilities/prices into `llm.catalog` (below `model_overrides`, above the 200k default). The bundled `model_catalog.json` is gone — resolution is override > fetched(OpenRouter) > default. No effect for a non-OpenRouter endpoint (set `context_window` there); fetch failure is never fatal (fresh cache → live fetch → stale cache → default). Off via `model_catalog_fetch = false`. See `.claude/rules/brain.md` "Model catalog".
- `compaction_reserve_tokens: int = 0` / `compaction_keep_recent_tokens: int = 0` — compaction sizing; `0` = derive from the model window (`session.compaction.derive_reserve_tokens` / `derive_keep_recent_tokens`, capped at the legacy 16k/20k so a 200k model is unchanged), so a small-window model compacts sensibly instead of using Anthropic-sized constants (NB-14).
- `web_fetch: WebFetchConfig` (`[brain.native.web_fetch]`) — the daemon-side, SSRF-hardened `WebFetch` tool for the native harness (native-only; runs in the daemon netns, not gated by the bwrap CONNECT allowlist). All fields defaulted to safe values, so an absent block enables the tool. Every field but `admin_only` maps 1:1 onto `session.tools.WebFetchPolicy` in `NativeBrain._build_tools`. Fields: `enabled` (True), `timeout_seconds` (20.0), `max_bytes` (5_000_000), `max_content_chars` (100_000), `max_redirects` (5), `allow_http` (False — HTTPS-only), `allowed_ports` ([80, 443]), `user_agent` ("IstotaBot/1.0"), `allow_hosts` ([] = default-open suffix allowlist), `block_hosts` ([]), `extra_blocked_cidrs` ([] — operator additions to the private/reserved IP blocklist), `require_url_provenance` (False — only fetch URLs seen in the task *prompt*, never in a prior tool result, so it also blocks a WebSearch-then-read chain; for sensitive deployments), `admin_only` (False — withholds the tool from non-admins, the pre-ISSUE-449 rule; read by `executor.build_allowed_tools` and by nothing under `session/`, which is why it is the one field with no `WebFetchPolicy` counterpart). See `.claude/rules/brain.md` "Native WebFetch tool".
- `session_log: SessionLogConfig` (`[brain.native.session_log]`) — the per-attempt JSONL transcript of a native run (native-only; the two CLI brains already get one from the `claude` CLI, and `build_bwrap_cmd` already binds it out of the sandbox). All fields defaulted, so an absent block ships the feature on. Fields: `enabled` (True — false means no writer, no file, no directory), `dir` (`""`), `retention_days` (14 — the age rule; 0 keeps everything by age and the ceiling still runs), `max_total_gb` (2.0 — the disk ceiling across **every** user summed, clamped to a 0.5 floor by the sweep; 0 drops the ceiling and the age rule still runs, which is why the scheduler's gate is `or` and not `and`), `max_content_chars` (32768, per text/thinking block), `max_args_chars` (8192, per tool-call arguments object), `include_thinking` (True). On the two char caps `0` means **no cap** rather than "off", which is the opposite of what it means on the two limits beside them. `dir` is the one field not used as written: `session_log.resolve_session_log_dir(db_path, dir)` — a free function rather than a `Config` method, so the writer, the sweep, `doctor` and the skill proxy share one answer and `session_log.py` stays a stdlib-only leaf that imports no config — maps `""` to `{db_path.parent}/logs` (local disk on every shipped shape, `/data/db/logs` on Docker, behind the sandbox's database mask on the Ansible shape and merely *unbound* on the other two — the standalone one because `_mask_dir` refuses where `db_path.parent` is the workspace, and the shipped Docker stack because it grants neither `seccomp:unconfined` nor `systempaths=unconfined`, so the bwrap probe fails and no mask is emitted at all). A set value is taken literally with no `~` expansion and no resolving, so a relative one follows each process's own cwd and an absolute path is what to write; a value naming no directory of its own (`/`, `.`, `..`) or carrying a null byte is refused back to the default, because the resolved directory is what the retention sweep deletes under. The numbers are restated from `session/session_log.py`'s `DEFAULT_*` constants rather than imported — `config.py` sits below the session layer and is loaded on every CLI invocation and every host-side skill CLI spawn — and `tests/test_config_native_session_log.py` holds the two copies equal. See `.claude/rules/brain.md`.
- `bash_spill_full_output: bool = True` (`[brain.native]`) — when the native Bash tool's output exceeds the per-tool cap, spill the full captured output to a task-scoped temp file (under `ISTOTA_DEFERRED_DIR` → `ToolEnv.deferred_dir`, fallback system temp) and name it in the result so the model can `Read` it, instead of silently dropping the tail. Best-effort (degrades to cap-only on I/O error); skipped when the call sets `exclude_from_context`. See `.claude/rules/brain.md` "Native-brain coding enhancements".
- `turn_budget_nudge: bool = True` / `turn_budget_nudge_early_percent: int = 50` / `turn_budget_nudge_remaining: list[int] = [15, 5]` (`[brain.native]`) — turn-budget awareness nudge (ISSUE-187 defect 3). On a **tool-bearing** run with a `max_turns` cap, the loop injects an environment notice as the run approaches the cap so the model paces itself and delivers a partial answer instead of getting capped mid-plan. Fires once at `early_percent` of the cap (a "~halfway, keep it in mind" reminder), then once each as absolute steps-remaining crosses each level in `turn_budget_nudge_remaining` (escalating urgency). Counted from assistant turns (monotonic across compaction); each threshold fires at most once. Also gates a non-numeric upfront pacing line in the coding system prompt (mechanism A). Text-only runs (empty `allowed_tools`, e.g. the sleep cycle) never see it. Off for models that mishandle meta-instructions. See `.claude/rules/brain.md` "Turn-budget awareness nudge". Since ISSUE-373 the ladder also runs against the wall clock: the loop estimates how many turns the remaining time has room for (rolling mean of recent turn latency, three samples minimum) and takes whichever budget is scarcer, so the notices stay reachable on a brain slow enough that the clock beats the cap.
- `soft_deadline_percent: int = 90` (`[brain.native]`) — where the loop stops itself relative to `scheduler.task_timeout_minutes` (ISSUE-373). A wall-clock timeout discards the model's work; `max_turns` delivers it under a marker. On a slow brain the clock arrives first, so the run that would have been killed by the discarding stop is ended a little early by a preserving one (`stop_reason = "soft_timeout"`, `success = True`, marker appended). The remaining slack is what the hard deadline still covers — a turn that hangs has no boundary to stop at. `0` or `>= 100` turns it off. See `.claude/rules/brain.md` "The soft deadline".

Built-in role aliases (`fast`/`general`/`smart`) resolve to `native.model` on the native brain unless remapped via `[models.aliases]` (NB-3) — so stock config's `extraction_model`/`curation_model = "general"` never reaches the wire as a literal alias string.

### `ModelsConfig`
```
aliases: dict[str, str | dict] = {}   # raw [models.aliases] structure — flat string OR per-namespace table
```
The operator-visible model alias registry (centralized-model-alias-registry spec):
one table holding **both** the portable tiers (`fast`/`general`/`smart`) and the
provider shortcuts (`opus`/`sonnet`/`haiku`), **per-namespace**. `aliases` holds
the **raw** parsed `[models.aliases]` structure: each value is either a bare
string (legacy flat, resolved by whichever brain runs the task) or a per-namespace
table (`{anthropic = "...", openai_compat = "..."|{model, effort}}`, plus an
optional reserved `portable = true` sibling) so one definition covers every brain
family. The shipped default set (`fast`→Haiku, `general`→Sonnet, `smart`→Opus,
`opus`/`sonnet`/`haiku` shortcuts) lives on the active brain
(`brain.claude_code.DEFAULT_ALIASES`) as the overridable code floor. Effort is an
orthogonal `:effort` modifier on any reference (`opus:high`), never baked into a
name. Normalization into `RoleTarget(model, effort)` objects happens once in
`brain._roles.set_alias_overrides(config.models.aliases)` (run late in config
load), keyed `name -> namespace -> RoleTarget` with the reserved `"*"` namespace
for a flat value; each brain reads its own namespace via
`get_alias_override_target(name, self.model_namespace)`, so an anthropic value
never leaks onto the native wire. Config-load validation is namespace-aware
(anthropic entries → `claude_code` via `validate_alias_override`; flat `"*"` →
active brain; `openai_compat` → native, no alias table so no warnings; the
`portable` key is skipped); warnings only, never fails load. Custom alias names
(`deep`, `cheap`) are accepted; a custom alias is a non-portable pin unless
flagged `portable = true`. Every wired field that takes a model name
(`selection_model`, `extraction_model`, `curation_model`, the top-level `model`,
per-task `model`, `[[jobs]] model`) accepts canonical IDs, shortcuts, tiers, or
any of them + a `:effort` modifier. **Hard rename:** `[models.roles]` is no longer
read — a stale one logs a one-time migration WARNING and does not populate
`aliases`; a no-`[models.aliases]` deployment resolves via the code floor.

### `ExperimentalConfig`
```
features: list[str] = []     # operator opt-in for rough features ([experimental] features in TOML)
```
Operator-scoped feature flags. Flat list of feature names; off by default.
`is_enabled(feature) -> bool` is the check used by `Config.is_module_enabled`
(via `EXPERIMENTAL_MODULES` in `modules.py`), by the `@requires_feature`
Click decorator (`src/istota/experimental.py`), and by `select_skills` /
`eligible_skill_names` (gated on `skill_<name>` flags).
`load_config()` logs a warning when a configured name isn't in the
`KNOWN_FEATURES` registry but keeps the entry — operators can graduate
features in code without breaking deployments that still list them.
Naming convention: `module_<x>` for module gates, `skill_<x>` for skill
gates, free-form for CLI subcommand gates (`money_tax`, `money_wash_sales`).
See `docs/EXPERIMENTAL.md` for the registry and graduation policy.

### `BriefingsModuleConfig` (`[briefings]`)

Beyond the archive/lookback/char caps documented in AGENTS.md:
`newsletter_max_links_per_source: int = 20` caps the inline `[anchor](url)` links
`briefings/sources/_html.html_to_markdown` preserves per newsletter body
(`0` = unlimited). A filtered-out or over-cap anchor keeps its text and loses only
the destination, so the cap bounds prompt size without dropping content. Ansible
`istota_briefing_newsletter_max_links`.

### `MoneyModuleConfig` (`[money]`)

```
autoclass_lookup: bool = True
```
Gates the portfolio module's ticker-metadata lookup — the primary tier of
`portfolio_autoclass`, which sends every newly imported symbol to a
third-party quote API. Held symbols are private financial data and the call
runs in the unsandboxed daemon/web process, outside the CONNECT allowlist, so
`[security.network]` cannot restrain it (contrast `[brain.native.web_fetch]`,
which ships its own knobs for exactly this class of automatic egress). Default
on — the classification is the feature. Off keeps the offline description
heuristics, which need no network at all, and surfaces as
`lookups_available: false` in the import/backfill response. Threaded to both
consumers through `money.cli.UserContext.autoclass_lookup`, set by
`_loader.resolve_for_user`, so the web routes and the CLI honour one switch.
Ansible `istota_money_autoclass_lookup`.

### `BriefingConfig`
```
name: str                    cron: str                   conversation_token: str = ""
title: str = ""              # blank derives from the name; date appended by the renderer
output: str = "talk"         components: dict = {}   # migration-read carrier only (never TOML-authored)
blocks: list[dict] = []      # config-authored rich blocks; in-memory only
```
`blocks` (config-authored-rich-briefing-blocks spec) is the full block/source
authoring shape (`[[users.X.briefings.blocks]]` + `[[...blocks.sources]]`): a raw
dict passthrough parsed by `_parse_briefing_specs`, threaded through
`_apply_user_briefings` (re-attached to the DB-shadowed entry by name) and
`get_briefings_for_user` (verbatim — the legacy component expansion is retired),
and read **once** by the module-DB seeder (`briefings/_migrate.normalize_block_specs`
→ `_seed_blocks`) as an editable baseline. `compare=False`/`repr=False`; it is
**never persisted to `briefing_configs`** (content is module-DB territory, so the
framework row stays byte-unchanged). Blocks are the **sole content model**
(retire-legacy-briefing-components spec): `components` is retained only as a
migration-read carrier populated from the DB row (`__output__` no longer packs
into it — `output` is a real `briefing_configs` column now); TOML `components =`
authoring is dropped (a stray key is ignored with a warning in
`_parse_briefing_specs`).

### `default_briefings` (top-level) + `UserConfig.default_briefings`
```
Config.default_briefings: list[BriefingConfig] = []   # parsed from [[default_briefings]]
UserConfig.default_briefings: bool = True             # per-user opt-in (user_profiles column, DEFAULT 1)
```
A canonical shared briefing set (retire-legacy-briefing-components spec): the
top-level `[[default_briefings]]` section (same name/cron/output/blocks shape,
parsed via `_parse_briefing_specs`) is seeded by name into each opted-in user's
`briefings` in `_apply_user_briefings` (an explicit user briefing of the same
name wins), before the DB overlay. `import_from_user_configs` (never overwrites
an existing `briefing_configs` row) + the one-time block sentinel give
seed-once + edit-preservation for free. The per-user flag is a `user_profiles`
scalar bool plumbed through `_apply_user_profiles` + `istota user ensure
--default-briefings/--no-default-briefings`.

### `ResourceConfig`
```
type: str                    path: str = ""              name: str = ""
permissions: str = "read"
extra: dict = {}            # unrecognized TOML keys (incl. base_url/api_key for the obsolete migration)
```
After the Resources sunset only `folder` (an out-of-workspace sandbox mount)
and `shared_file` (internal organizer state) are live types. Obsolete
credential types (karakeep, monarch, overland, …) survive only in the
load-time migration window (`_allow_obsolete=True`); calendar/email_folder/
notes_folder are auto-cleaned; todo_file/reminders_file are **not**
auto-cleaned, but no longer because anything reads them — the fetcher that
read `reminders_file` was deleted with the last of the legacy briefing
generator, and `todo_file` never had a reader. They stay out of the
auto-clean set because deleting a user's rows is a data migration, not
dead-code removal.
`base_url`/`api_key` are no longer flat fields — they live in `extra` and are
absorbed into the secrets table by `secrets_store.import_from_user_configs`.

### `UserConfig`
```
display_name: str = ""                    email_addresses: list[str] = []
timezone: str = "UTC"                     briefings: list[BriefingConfig] = []
resources: list[ResourceConfig] = []
log_channel: str = ""                     # Talk room for verbose execution logs
alerts_channel: str = ""                  # Talk room for confirmations/alerts
max_foreground_workers: int = 0           max_background_workers: int = 0  # 0 = use global default
disabled_skills: list[str] = []           # per-user skills to exclude
trusted_email_senders: list[str] = []     # patterns for trusted senders (email gate)
disabled_modules: list[str] = []          # modules to opt out of (default-on otherwise)
email_reply_routing: str = "origin+thread" # email-reply mirror policy: origin+thread | origin | thread
briefing_email_html: bool = True          # briefing email as multipart/alternative (HTML + plain)
```

`email_reply_routing` is a `user_profiles` column read via `Config.email_reply_routing_for(user_id)` (invalid value → default + warning). It controls where a reply to a bot-sent email is delivered — the origin surface (`web:`/`talk:` descriptor stored on `sent_emails.origin_target`), the email thread, or both. Set via `istota user ensure --email-reply-routing`. See `.claude/rules/transport.md` "Email-reply origin routing".

`outbound_approval` is a `user_profiles` column (`'' | off | untrusted | all`) holding the user's own outbound email approval policy. `''` means **unset**, not "off" — it resolves to the operator's `[email] outbound_approval_floor`, which is what makes raising the floor reach every user who never touched the setting. Resolution is `istota.outbound_policy.effective_policy(config, user_id)` = `max(floor, user)` on the ordering `off < untrusted < all`: the operator sets a minimum and a user may tighten but never loosen. Same shape as the Google scope ceiling, in the opposite direction. An invalid stored value (a hand-edited row) logs a warning naming the user and is treated as unset, so it tightens toward the floor rather than disabling the gate. See `.claude/rules/transport.md` "Outbound email approval gate".

`external_turn_display` is a `user_profiles` column (`full | collapsed | hidden`, default `collapsed`) controlling how an external-origin turn's **body** renders in web chat. It never removes the turn: a transcript showing a bot answer with no question above it is the defect the inbound mirror was built to fix (ISSUE-136), so `hidden` still renders the sender/subject header row.

### `[email] outbound_approval_floor`

```
outbound_approval_floor: str = "untrusted"   # off | untrusted | all
```

The weakest outbound approval policy any user on this instance may run. `off` = no holds (the pre-feature behaviour); `untrusted` = hold unless **every** recipient (To, Cc, Bcc) is trusted per `Config.is_trusted_email_sender`; `all` = hold unless every recipient is one of the user's own addresses. Any single untrusted recipient holds the whole message — there are no partial sends.

Unlike the other enum-ish keys, an invalid value **raises at config load** rather than warning and falling back (`_validate_outbound_approval_floor`). There is no safe fallback to pick: `off` would disable a gate the operator asked for, and `untrusted` would override an operator who deliberately wrote `off`. A typo in a security floor stops the process.

Ansible `istota_email_outbound_approval_floor`, Docker `ISTOTA_EMAIL_OUTBOUND_APPROVAL_FLOOR`. Both deploy paths need one because this is a gate that switches **on** at upgrade — the dataclass default is `untrusted`, so a deployment that never configures anything gets holds it did not have before, and without a knob there is no supported way back (Ansible overwrites hand edits to `config.toml`; the Docker entrypoint regenerates it). The raising validator makes the Ansible side sharper than it looks: `off` is a YAML boolean, so an unquoted `istota_email_outbound_approval_floor: off` renders `"False"` and produces a config the daemon refuses to load, which is why the role `assert`s the value (and each per-user `outbound_approval`) *before* the template task rather than relying on `validate_config.py` afterwards. Per-user policy is set by `istota user ensure --outbound-approval`; the `[users.X] outbound_approval` TOML key is seed-only, like every other profile field.

The allowlist is explicit authorization only and must never be derived from observed correspondence — not `sent_emails.to_addr`, not `processed_emails.sender_email`, and not a "we already replied to them once" shortcut. An earlier attempt did exactly that and inverted the gate, since one inbound message from a stranger then permanently authorized mailing them. `tests/test_outbound_gate.py::TestLayerARegressionGuard` is the standing guard.

`briefing_email_html` is a `user_profiles` bool read via `Config.briefing_email_html_for(user_id)` (unknown user → True, matching `is_module_enabled`'s docker auto-seed rule). On (the default) a briefing email is sent `multipart/alternative` — `skills/briefing.render_briefing_html` output plus the `strip_markdown` plain fallback — so article links are clickable in a mail client; off is byte-identical to the pre-feature single-part plain send. Set via `istota user ensure --briefing-email-html/--no-briefing-email-html`, the `[users.X] briefing_email_html` TOML key (Ansible `briefing_email_html:`), or the **Email delivery** card on `/briefings/settings` (it governs briefing delivery, so it lives with the briefings module rather than in the general profile card). See `.claude/rules/transport.md` "Briefing email bodies".

`google_scopes` is a `user_profiles` JSON-dict column (`{service: off|readonly|full}`) and deliberately **not** a `UserConfig` field — nothing at config-load time reads it, only the web connect/status path does. It is the user's own selection within the `[google_workspace] scopes` ceiling; `{}` means unset and resolves to the whole ceiling. Written by `PUT /api/google/scopes`, resolved by `istota.google_scopes.resolve_selection`, and there is no CLI or TOML surface for it — a scope grant is the user's consent, so the operator sets the maximum and nothing else. See `.claude/rules/skills.md` under `google_workspace/`.

### `MemorySearchConfig`
```
enabled: bool = True         auto_index_conversations: bool = True
auto_index_memory_files: bool = True
auto_recall: bool = False    auto_recall_limit: int = 5
```

### `DeveloperConfig`
```
enabled: bool = False        repos_dir: str = ""
gitlab_url: str = "https://gitlab.com"
gitlab_token: str = ""       gitlab_username: str = ""
gitlab_default_namespace: str = ""  # Default namespace for short repo names
gitlab_reviewer: str = ""           # GitLab username to assign as MR reviewer
gitlab_reviewer_id: str = ""        # That user's numeric id, recorded not consumed
github_url: str = "https://github.com"
github_token: str = ""       github_username: str = ""
github_default_owner: str = ""  # Default org/user for short repo names
github_reviewer: str = ""
author_credit: str = ""
forge_cli_extra_denied: list[str] = []   # Extra verbs the gh/glab wrapper refuses
forge_cli_permit: list[str] = []         # Baseline deny entries to turn off
gh_bin_path: str = "/usr/local/bin/gh"   # Ansible renders the installed path
glab_bin_path: str = "/usr/local/bin/glab"
devbox_proxy_enabled: bool = True
devbox_proxy_socket_dir: str = "/var/run/istota"
devbox_proxy_audit_log: str = ""
worktree_reap_enabled: bool = True    # Reap landed worktrees, from the scheduler
worktree_retention_hours: float = 24.0  # Idle time before one is a candidate; clamped to a 1h floor
container: ContainerConfig   # [developer.container] — see its own entry above
review: ReviewConfig
```

`repos_dir` is a **root of per-user subtrees**, not one shared tree: `{repos_dir}/{user_id}/{namespace}/{project}.git`, with a worktree as a sibling of its bare clone and the derived package cache at `{repos_dir}/{user_id}/.package-caches`. `build_bwrap_cmd` and `native_fs_roots` give an admin developer task only `{repos_dir}/{user_id}`, so one admin cannot read or write another's clones, worktrees, model-written git configs or cache. That is structural rather than enforced, which is what let the ISSUE-319 mask machinery be deleted instead of extended.

The namespace level is kept rather than flattened to `{user_id}/{project}.git`, which reads better and collides when one user clones two projects with the same basename from different namespaces. Depth 3 stays inside `git_remote_scrub._MAX_DEPTH` and inside what the worktree reaper walks, so neither needed a change. Two admins working the same repository now keep two clones; the disk cost is bounded and small next to worktrees and caches, and the alternative is the shared mutable tree the split exists to remove.

`DEVELOPER_REPOS_DIR` is that subtree, derived per task by the developer skill's `setup_env` hook. Both manifests that declare it (`developer`, `code_review`) are `from: setup_env`, so the hook is the sole producer — a hook value never outranks a manifest-resolved `from: config` entry (`executor` merges `build_skill_env` first, both under `if k not in env`), so the conflict is removed rather than won. One consequence: emission is gated on `is_admin` and config rather than on skill authorization, which is the same gate the bind already carries. An operator override manifest at `config/skills/developer/skill.md` still carrying the old `from: config` entry would outrank the hook and hand the model the shared root again, silently; the guard test reads bundled manifests only.

An existing deployment is moved by `repos_relocate.py`, which the Ansible role runs after the code is in place and before the units restart. Ownership is the whole problem — nothing on disk says which user owns a clone, since a forge namespace is not a user id — so it assigns every namespace to the single configured admin and refuses where there are none or several. `{repos_dir}/.istota-layout` holding `2` is the idempotency marker. `{repos_dir}/.package-caches`, the shared cache root the previous layout used, is reported and left in place: it is orphaned by the derivation and safe for an operator to remove by hand.

Two consumers deliberately keep the **global** root and are not bugs: `worktree_reaper`'s sweep, which runs deployment-wide from the scheduler with no user to scope to, and `_protected_cache_parents`, which takes no user and for which the global entry is the stricter equal-or-ancestor test. The `ContainerConfig` entry above covers what the devbox transport binds on top of this layout.

`gitlab_reviewer` is the value the developer skill exports as `GITLAB_REVIEWER` and hands to `glab mr create --reviewer`, which resolves by username. `gitlab_reviewer_id` holds the same person's numeric id and is read by nothing — it kept its name and lost its consumer in ISSUE-289, where the name was the bug: operators put the id `users/<id>` reports into the field the skill consumed, `glab` answered `failed to find user by name`, and because the recipe builds the flag rather than failing, every agent-authored MR opened with nobody assigned. The `developer.gitlab_reviewer` doctor check WARNs on an all-digits username, and on an `_id` set with no username beside it — the shape a host that has not re-run Ansible is in.

`worktree_reap_enabled` and `worktree_retention_hours` drive `worktree_reaper.py` (ISSUE-288). Nothing removed a task's worktree before it, so `repos_dir` accumulated gigabyte checkouts with no owner and no stated retention rule. The sweep runs from the **scheduler**, on `scheduler.worktree_reap_interval`, not from the developer skill's `setup_env`: `dispatch_setup_env_hooks` calls every skill's hook whatever the task selected, so a sweep there fired before every Talk reply, every cron job and every heartbeat tick — and the heartbeat builds a task with `id=0`, so it also ran with no notion of whose worktree was whose. A delete path belongs on a cadence somebody chose. The retention window is what protects a task running *right now*, since the periodic sweep knows nothing about the worker pool and recent activity is the only evidence available that a checkout is in use. It is clamped to a one-hour floor: a worktree seconds old is clean, unlocked and carries nothing that is not upstream, which is exactly the reapable state, so a shorter window does not mean "reap sooner" but "reap the checkout a task is still setting up".

`gitlab_api_allowlist` / `github_api_allowlist` and `api_timeout_seconds` are **gone** (unified-forge-cli-wrapper spec). An endpoint allowlist cannot describe what a real `gh` invocation does — `gh pr create` is several calls, `gh pr checks` paginates — so the deny list moved into `forge_cli.py`'s argv policy, and `api_timeout_seconds` lost its last consumer when the devbox proxy's httpx client went. The loader ignores unknown keys by design, so a `config.toml` still carrying any of the three loads clean and inert; `config.toml.j2` no longer renders them, but a host keeps its last-rendered file until Ansible runs again.

`forge_cli_permit` is documented as turning an accident guard *off*, because that is what it does. An entry matching no baseline rule and no `forge_cli_extra_denied` entry is warned about at startup (`_validate_forge_clis`): a hatch that silently stopped matching after a baseline rewording reads exactly like one that is still open. The same function warns when a configured `gh_bin_path` / `glab_bin_path` does not exist, and when tokens are set while `security.skill_proxy_enabled = false`. That second one is about posture, not breakage: `setup_env` writes `direct_token` into the policy file for that shape and the wrapper reads the ambient `GH_TOKEN` / `GITLAB_TOKEN`, so forge commands work — but the token sits in the environment the model's own shell inherits rather than being injected per call.

**One instance per forge.** `gitlab_url` and `github_url` are single strings, and three separate things derive from each: the CONNECT allowlist entry (`_build_network_allowlist`, `executor.py:1030-1039`), the git credential helper (installed under a per-host `credential.{host}.helper` key), and the `GITLAB_HOST` / `GH_HOST` the wrapper writes into the real CLI. A repository on a *second* GitLab or GitHub instance therefore gets none of them. It fails safe — the CONNECT proxy refuses before anything authenticates — but opaquely, and adding the second host to `security.network.extra_hosts` buys reachability without buying credentials, so the clone still fails. There is no supported second-instance configuration.

`git` itself is unwrapped and forge-agnostic: public reads work against any allowlisted host, authenticated pushes only against the two configured ones, and no policy gates `git` at all. Force-push protection is forge-side branch rules, for the same reason `pr merge` is left un-denied.

### `BriefingDefaultsConfig` — removed

`BriefingDefaultsConfig` and the `[briefing_defaults]` load block are **gone**
(retire-legacy-briefing-components spec). Boolean-component defaults expansion
(`_expand_boolean_components`) and the legacy component generator
(`build_briefing_prompt`) were deleted with it; blocks are the sole content
model. A stale `[briefing_defaults]` section in TOML is ignored (no field to
populate).

### `Config`
```
db_path: Path = Path("data/istota.db")
bot_name: str = "Istota"            emissaries_enabled: bool = True
model: str = ""                     # DEPRECATED (ISSUE-418): was claude_code's own default at the root, applied to every brain. Migrated onto [brain.claude_code] + [brain.tmux] with a warning; never onto [brain.native]
effort: str = ""                    # DEPRECATED (ISSUE-418), migrated the same way
advisor_model: str = ""             # Advisor model (anthropic-namespace brains only); resolves through the alias table like `model`, no effort. Empty = no advisor. Dropped for a task carrying a model pin (executor._resolve_advisor)
custom_system_prompt: bool = False  # Use config/system-prompt.md instead of CC default
nextcloud: NextcloudConfig          talk: TalkConfig
email: EmailConfig                  conversation: ConversationConfig
scheduler: SchedulerConfig          browser: BrowserConfig
devbox: DevboxConfig
logging: LoggingConfig
default_briefings: list[BriefingConfig] = []  # canonical shared set, seeded into opted-in users
brain: BrainConfig                          # selects model-invocation backend
security: SecurityConfig
memory_search: MemorySearchConfig   playbooks: PlaybooksConfig
sleep_cycle: SleepCycleConfig
channel_sleep_cycle: ChannelSleepCycleConfig
developer: DeveloperConfig          site: SiteConfig
health: HealthModuleConfig          money: MoneyModuleConfig
location: LocationReceiverConfig
models: ModelsConfig                experimental: ExperimentalConfig
users: dict[str, UserConfig] = {}
admin_users: set[str] = set()      # from /etc/istota/admins (empty = all admin)
rclone_remote: str = "nextcloud"
nextcloud_mount_path: Path | None = None
skills_dir: Path = Path("config/skills")
temp_dir: Path = Path("/tmp/istota")
module_data_dir: Path | None = None  # local-disk root for per-user module DBs (feeds/health/location/money); None derives {db_path.parent}/modules. MUST be local (WAL -shm SIGBUSes on the FUSE mount); an explicit value under nextcloud_mount_path is refused
max_memory_chars: int = 0  # cap total memory in prompts (0 = unlimited)
max_knowledge_facts: int = 50  # cap knowledge graph facts per prompt (0 = unlimited)
disabled_skills: list[str] = []    # instance-wide skills to exclude
bundled_skills_dir: Path | None = None  # override for testing
```
Properties / methods:
- `use_mount`: `bool` — True if `nextcloud_mount_path` set
- `module_db_root() -> Path`: the local-disk root holding every user's module DBs (`module_data_dir`, or `{db_path.parent}/modules`). Explicit `module_data_dir` under the mount raises `ValueError` (WAL SIGBUS guard); the derived default is trusted-local, unguarded. Split out of `module_db_path` because the sandbox needs the root on its own — `build_bwrap_cmd` masks it and `_validate_workspace_dir` refuses a REPL workspace overlapping it. Deriving that root in three places is how it went unmasked in the first place
- `module_db_path(user_id, module) -> Path`: `module_db_root() / user / f"{module}.db"`. The seam each module loader passes as its `db_path=` override; workspace/`data_dir` stays on the mount. Single enumerator for `db_health.check_db_health` + `db_backup` + `db_relocate`
- `bot_dir_name`: `str` — sanitized `bot_name` for filesystem use (ASCII lowercase, spaces→underscores)
- `caldav_url`: derived from `nextcloud.url + /remote.php/dav`
- `caldav_username`: `nextcloud.username`
- `caldav_password`: `nextcloud.app_password`
- `storage_is_nextcloud`: `bool` — whether a Nextcloud server backs the file workspace. Keyed on `bool(nextcloud.url)`, deliberately **not** `is_standalone` (which folds in web auth, an axis orthogonal to file storage): a URL means the files are Nextcloud whether reached via mount or rclone; no URL means a plain local folder. The single source of truth for storage vocabulary in prompts/skills.
- `storage_backend`: `str` — `"nextcloud"` | `"local"`, derived from `storage_is_nextcloud`.
- `storage_label`: `str` — short noun for prose: `"Nextcloud"` when Nextcloud-backed, else `"your workspace"` (a mid-sentence noun phrase).
- `workspace_root(user_id=None) -> Path | None`: on-disk root of the workspace (mount mode only; `None` under rclone). Scoped to `{mount}/Users/{user_id}` when `user_id` is given, else the bare mount root. De-dups the `mount / "Users" / uid` idiom inlined across the codebase — not a storage abstraction (no I/O, no backend switch).
Methods:
- `get_user(nc_username) -> UserConfig | None`
- `is_admin(user_id) -> bool` — True if `admin_users` empty or user in set
- `available_capabilities() -> set[str]` — backing-service capabilities currently deployed; the single map from a capability name to its config flag (`browser`→`config.browser.enabled`, `devbox`→`config.devbox.enabled`). Drives the skill capability gate: a skill declaring `requires_capability: [name]` whose capability isn't in this set is folded into the effective `disabled_skills` (dropped from selection, the on-demand menu, and shown disabled in `!skills`) via `skills._loader.effective_disabled_skills`. Both flags default off, so `browse`/`devbox` disappear automatically in the standalone install (no headless browser / no devbox container). Adding a service-backed skill = declare the capability here + in the skill frontmatter. See `.claude/rules/skills.md` "Capability gate".
- `is_module_enabled(user_id, module) -> bool` — True unless ``module`` appears in the user's `disabled_modules`. Unknown users default to True (docker auto-seed path). Module names are validated against `istota.modules.MODULE_NAMES` (`feeds`, `money`, `location`, `health`); unknown names always return False. Reads from the `user_profiles` DB row when `db_path` is set (so web edits to `disabled_modules` take effect across web/scheduler/webhook processes without SIGHUP), falls back to the in-memory `UserConfig.disabled_modules` for init/test paths or unseeded rows. **Experimental gate**: if `module` appears in `modules.EXPERIMENTAL_MODULES` (currently empty), the method also requires the matching flag to be enabled in `config.experimental.features`; this check runs before the user-profile DB read so a disabled experimental module short-circuits without a DB hit. **Dependency-availability gate**: if `module` has an install extra declared in `modules.MODULE_DEPENDENCIES` (`money → beancount`) and `modules.module_available(module)` finds the import missing, the method returns False — also before the DB read — so a lean install (e.g. `istota[local]` without beancount) hides the module everywhere instead of half-shipping it and crashing on first use. Surfaces that need to enumerate visible modules (the `/settings/modules` web endpoint, `disabled_modules` profile-write validation in `_coerce_profile_value`) filter against the same gate.
- `find_user_by_email(email_address) -> str | None`
- `is_trusted_email_sender(user_id, sender_email, conn=None, *, include_own_addresses=True) -> bool` — checks user's own emails + `trusted_email_senders` patterns via fnmatch + the runtime `trusted_email_senders` DB table (only when `conn` is passed). `include_own_addresses=False` drops the first branch, for a caller asking about the own-address claim itself: the sender-match confirmation gate (ISSUE-227), whose route *is* that match, so the default would answer circularly and the gate could never fire. See `.claude/rules/transport.md` "Email confirmation gate"

## Config Loading

### `load_config()`
Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`

1. Parse TOML file
2. **Walk the dataclass tree** (`config_mapper.apply_section`) and set every field the TOML names, coercing by declared type. This is the mechanical half and it covers the great majority of the schema — a field's TOML key is its own name, and its default is the one on the dataclass. Unknown keys are collected and reported in one warning (`report_unknown`); they are still never fatal, because a config written for a newer version has to load on an older one for a rollback to work.
3. Two tables steer it, both in `config.py`, and `tests/test_config_mapper.py` holds each to naming real fields. `_CONFIG_HOOKS` maps a dotted key to a parse that is more than a coercion (`web.auth` and the other closed vocabularies, `security.sandbox_ro_paths`, `scheduler.email_task_queue`, `developer.container`, `health.max_document_bytes`). `_PARSED_BY_HAND` names what the walk must skip because something below builds it; `_RETIRED` names keys that are no longer fields at all, held out so each gets its own migration warning instead of a generic "unrecognised".
4. Hand-parsed sections: `[users.*]` → `_parse_user_data()`, `[[default_briefings]]` / `[[briefing_shared_blocks]]` → their spec parsers, `[models]` (alias values kept verbatim in either shape), `[experimental]` (checked against `KNOWN_FEATURES`), and the `[email]` cross-field trio — `confirm_sender_match = "verify"` is only meaningful with an `authserv_id` to scope the verdict to, so the validator has to see both and runs after the walk has settled it.
5. Call `load_admin_users()` → `config.admin_users`
6. Apply env var overrides for secrets (`ISTOTA_NEXTCLOUD_APP_PASSWORD` → `nextcloud.app_password`, etc.)
7. **Phase 6**: `_apply_user_profiles(config)` overlays the `user_profiles` DB table onto `config.users`. Profile-shaped scalar fields (display_name, timezone, log_channel, alerts_channel, max_foreground_workers, max_background_workers) are unconditionally replaced from the DB row when one exists; list fields (email_addresses, disabled_skills, trusted_email_senders) replace TOML only when non-empty (so an auto-seeded blank row doesn't wipe ansible-templated lists). Best-effort: missing/unreadable DB doesn't fail config loading.
8. **Phase 7a**: `_apply_user_resources(config)` overlays the `user_resources` DB table onto `config.users[*].resources`. Each row becomes a `ResourceConfig` entry with extras decoded from JSON. Dedup is keyed on `(type, path)` — DB wins. Distinct paths coexist.
9. **Modules refactor + Resources sunset (between 7a and 7b)**: `_migrate_obsolete_resources(config)` first calls `secrets_store.import_from_user_configs` (idempotent — extends `_IMPORT_MAP` to absorb karakeep `base_url`/`api_key` [from `extra`], overland `ingest_token`, monarch creds), then `db.cleanup_obsolete_resources(db_path)` deletes `user_resources` rows whose type is in the retired set (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`, `calendar`, `email_folder`, `notes_folder`). `todo_file`/`reminders_file` are deliberately **not** in that set — not because anything reads them (nothing does: the `reminders_file` fetcher was deleted and `todo_file` never had a reader), but because deleting a user's rows is a data migration rather than dead-code removal; an operator removes them by hand. The cleanup is idempotent (no marker needed). Finally, the retired set is filtered out of `uc.resources` in memory so the rest of the load cycle sees post-cleanup state.
10. **Phase 7b**: `_apply_user_briefings(config)` overlays the `briefing_configs` DB table onto `config.users[*].briefings`. Each row becomes a `BriefingConfig` entry. Dedup is keyed on `name` — DB wins. Disabled DB rows (`enabled=0`) drop the matching TOML name without scheduling, so the web UI can mute a TOML-templated briefing without re-templating. **Config-authored `blocks` re-attach**: before dropping TOML briefings claimed by a DB name, it captures `{name: blocks}` from the TOML entries and re-attaches `blocks` onto the appended DB-sourced entry when the name matches (DB rows never carry `blocks`; the field lives only in TOML/`Config`). Without this the module-DB seeder would never see config-authored blocks on a briefing that already has a `briefing_configs` row (every imported TOML briefing gets one after first startup).
11. Return `Config`

**Modules vs resources vs connected services.** Three distinct concepts that used to be conflated under `[[resources]]`:
- **Resources** — after the Resources sunset, only `folder` (an out-of-workspace sandbox mount) is declarable. `[[users.X.resources]]` + `user_resources` DB table. The path-shaped types (`calendar`, `todo_file`, `notes_folder`, `email_folder`, `reminders_file`) were retired: calendars are CalDAV-discovered; todo/reminders/notes read the briefing source's own explicit `path`, with no convention-default filename, and the `notes/` folder is prompt guidance only; email folders, `todo_file` and `reminders_file` have no consumer at all. `shared_file` survives as internal organizer state.
- **Modules** — on-by-default features with their own UI tab + cog (`feeds`, `money`, `location`). Per-user opt-out via `disabled_modules`. Module names live in `istota.modules.MODULE_NAMES`. Gated everywhere by `Config.is_module_enabled(user_id, module)`.
- **Connected services** — per-user external API credentials (karakeep, google_workspace) consumed by skills. Stored encrypted in the `secrets` table.

**user_profiles.disabled_modules.** New JSON-array column added in Phase 1 of the modules refactor. Migration runs in `_run_migrations` via `ALTER TABLE … ADD COLUMN … DEFAULT '[]'`. Mirrors `disabled_skills` in handling: list-field rule in `merge_into_user_config` (DB row owns the list once it exists; auto-seed carries TOML lists in). Surfaced in the web UI as a multiselect on `/settings → Preferences`; values are validated against `MODULE_NAMES` server-side via `_coerce_profile_value("disabled_modules", …)`.

**Settings split (modules refactor, Phase 2).** `web_app._SERVICE_SCHEMA` is gone. In its place:
- `_CONNECTED_SERVICE_SCHEMA` — services that aren't owned by any single module (`karakeep`, `google_workspace`). Each entry carries `used_by` (skill names) and optional `oauth: True` / `custom_ui: True` flags. Surfaced via `GET /settings/services`. `google_workspace` carries both: the OAuth redirect is still how it authenticates, but the card is bespoke (`GoogleWorkspaceCard`, fed by `GET /api/google/status`) because it renders granted scopes and a per-service picker. `ServiceCard`'s generic OAuth branch went with that change — it served exactly one service, so a future `oauth: True` service without `custom_ui` needs its own card rather than a resurrected branch.
- `_MODULE_SERVICE_SCHEMA` — per-module schema map (`feeds → {feeds.tumblr_api_key}`, `money → {monarch.*}`, `location → {overland.ingest_token}`). Surfaced via `GET /settings/module-services/{module}` which also returns `module_enabled` so the page can render its banner instead of the config UI when the module is disabled.
- `_all_known_services()` is the union the secret PUT/DELETE handlers validate against — module pages write their secrets through the same `/settings/secrets/{service}/{key}` route.
- `GET /settings/modules` returns `{modules, disabled, enabled_for_user}` for the Preferences card.
- `_service_status` no longer takes `user_resource_types`; status is purely a function of which keys are configured. The old "unavailable when no resource declaration" path is gone — module gating is the new "unavailable" signal and lives behind `is_module_enabled`.
- `/location/settings-info` returns the webhook-URL placeholder plus read-only place-detection knobs for `/location/settings`. Its own token is never echoed back — but `POST /settings/secrets/overland/ingest_token/generate` **does** return one, once, and is the only endpoint in the app that returns a secret in a response body; the QR code on that page is rendered from it before it becomes write-only. Both build their URL through the same `_location_webhook_url` helper, which yields a *relative* path when `[site] hostname` is blank — which is why minting 409s in that case rather than handing the phone a code its decoder will refuse.

**user_profiles table (Phase 6).** Per-user profile fields live in `user_profiles` (one row per user). The scheduler imports any profile-shaped fields from TOML on startup via `user_profiles.import_from_user_configs(db_path, config.users)` (idempotent — only writes rows that don't yet exist). DB row wins at config-load time. The web UI reads/writes via `/istota/api/settings/profile` (GET, PUT). Ansible deploys provision via the `istota user ensure --name <user> ...` CLI (idempotent partial update). See `src/istota/user_profiles.py`.

**user_resources table (Phase 7a).** Per-user resources live in `user_resources` (id PK, `UNIQUE(user_id, resource_type, resource_path)`). The `extras` column is a JSON dict for resource-type-specific config. After the Resources sunset only `folder` and `shared_file` are live types; `calendar`/`email_folder`/`notes_folder` are auto-cleaned at startup by `cleanup_obsolete_resources`, while `todo_file`/`reminders_file` are left in place though nothing reads them (never auto-cleaned — removing a user's rows is a data migration). At config-load time, `_apply_user_resources` decodes extras and merges DB rows into `config.users[uid].resources`. The `/settings/resources` web endpoints were removed; folder mounts are operator-only via `istota resource ensure --user … --type folder --path …` (idempotent upsert). Ansible's `resource ensure` task filters to `folder` only.

**briefing_configs table (Phase 7b).** Per-user briefings live in `briefing_configs` (id PK, `UNIQUE(user_id, name)`). The `cron_expression` column stores the cron string, `title` the operator-set label (blank derives it from the name), `output` the delivery target (`talk` / `email` / `both`), and `enabled` lets the web UI mute a briefing without deleting it. `output` is a real column — it used to be packed into `components.__output__` because the legacy schema had none, and reads hoisted it back out; that indirection is gone. `components` survives only as a migration-read carrier. The scheduler imports `[[briefings]]` blocks from TOML on startup via `user_briefings.import_from_user_configs(db_path, config.users)` (idempotent — only writes rows whose `(user_id, name)` pair doesn't already exist). At config-load time, `_apply_user_briefings` merges DB rows into `config.users[uid].briefings` so `check_briefings` and `get_briefings_for_user` (in `skills/briefing`) read DB and TOML rows uniformly. Web UI reads/writes via `GET/POST /istota/api/settings/briefings` and `DELETE /istota/api/settings/briefings/{id}`; payload accepts `{name, cron, title?, conversation_token?, output?, components?, enabled?}` (`title` ≤ 200 chars, no control characters, blank = derive). The GET response also returns a `rooms` list (auto-provisioned `log_channel` + `alerts_channel` tokens) so the UI can offer them as conversation_token picks. Ansible deploys provision via `istota briefing ensure --user … --name … --cron … [--title …] [--conversation-token …] [--output …] [--disabled]` (idempotent upsert with `STATE: created|updated|noop` output). See `src/istota/user_briefings.py`.

**Secret env var overrides** (applied after TOML, enables `EnvironmentFile=`). Naming convention is `ISTOTA_<SECTION>_<FIELD>` matching the config dataclass path — same convention as docker-compose env vars, so a single env-var name works across both deploy paths. The literal `ISTOTA_SECRET_KEY` and `ISTOTA_WEB_TOKEN_KEY` (Fernet key sources, not config fields) and runtime injection vars (`ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_TASK_ID`, etc.) are intentionally outside this convention — they aren't config overrides. `ISTOTA_WEB_TOKEN_STORAGE` *is* an override (→ `web.token_storage`, value-validated), added for the docker path.

| Env Var | Config Field |
|---|---|
| `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_EMAIL_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_EMAIL_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | `google_workspace.client_secret` |
| `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | `web.oauth2_client_secret` |
| `ISTOTA_WEB_SESSION_SECRET_KEY` | `web.session_secret_key` |

### `load_admin_users(path=None) -> set[str]`
Loads admin user IDs from plain text file (one per line, `#` comments, blank lines ignored).
- Check `ISTOTA_ADMINS_FILE` env var, then default `/etc/istota/admins`
- Returns empty set if file missing (all users = admin for backward compat)

### `_parse_user_data()`
Parses user dict → `UserConfig`:
- Parses `[[briefings]]` → `BriefingConfig` list
- Parses `[sleep_cycle]` → `SleepCycleConfig`
- Parses `[[resources]]` → `ResourceConfig` list (only `folder` is declarable after the sunset; unknown keys including `base_url`/`api_key` land in `extra`)
- Backward compat: migrates `reminders_file` string to `ResourceConfig(type="reminders_file")` (inert — no reader; stored so an existing row survives a load, not auto-cleaned)

## UserResource (DB Model, in db.py)
```python
@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str      # "folder", "shared_file" (live)
                            # "todo_file", "reminders_file" (inert, no reader)
                            # "calendar", "email_folder", "notes_folder",
                            # "feeds", "money", "monarch", "moneyman",
                            # "karakeep", "overland" (auto-cleaned)
    resource_path: str
    display_name: str | None
    permissions: str        # "read" or "readwrite"
```

## How to Add a New Config Field

### To an existing sub-config (e.g., SchedulerConfig):
1. Add field with default to dataclass in `config.py`
2. It auto-loads from the TOML `[scheduler]` section, matching field name and coercing to the declared type — **no loader change**. This line was in this document for a long time before it was true: the loader used to need a hand-written `.get()` per key, and eleven declared, documented, generator-written settings had no line and were silently ignored. If the field needs validating beyond its type, add a hook to `_CONFIG_HOOKS` keyed on its dotted path.
   The scalar, optional, list, set and dict shapes are resolved structurally, so `str | None` and `list[float]` work as readily as `int`. An annotation the resolver cannot answer for is a **test failure**, not a silent skip — `tests/test_config_mapper.py::test_every_declared_field_resolves_to_a_coercion` walks the real tree, because a field ignored with only a log line is precisely the defect the walk exists to prevent, and it shipped once for `dict`.
3. Update `config.example.toml` with documentation
4. Update Ansible: `defaults/main.yml` + `templates/config.toml.j2`

### To add a new sub-config section:
1. Create new `@dataclass` in `config.py`
2. Add field to `Config` dataclass — the walk recurses into a nested dataclass and reads the sub-table of the same name, so there is nothing to add to `load_config`
3. Only if the section is not a plain field tree (a list of dataclasses, a verbatim mapping, a cross-field rule) does it need a hook or an entry in `_PARSED_BY_HAND`
4. Update `config.example.toml`, Ansible role

### To add a new per-user field:
1. Add field with default to `UserConfig` dataclass
2. Parse it in `_parse_user_data()` if non-trivial
3. It loads from `[users.NAME.field]` in main config (docker entrypoint path)
4. If profile-shaped, plumb it through `user_profiles` (DB row, web UI, `istota user ensure`)

## How to Add a New Folder Mount

After the Resources sunset, `folder` is the only declarable resource type —
used for mounting an out-of-workspace path into the sandbox.

1. Users add via: `uv run istota resource ensure -u USER -t folder -p /shared/path`
2. The executor's `build_sandbox_command` / `native_fs_roots` already bind-mount
   any `folder` resource path that isn't already inside `Users/{user_id}/` (RW
   or RO per `permissions`).
3. The prompt no longer enumerates resource paths (Stage 3a replaced them with a
   static workspace-layout line); the model discovers files by convention +
   Glob/Read over the bound workspace.

To read a workspace file by convention (notes, todos, reminders), use the
`files` skill's mount-aware `read_text(config, path)` against
`{bot_dir}/NOTES.md`, `TODO.md`, `reminders.md` — no resource declaration needed.

## Modules vs resources vs connected services

- **Resources** — after the Resources sunset, only `folder` (an out-of-workspace sandbox mount) is declarable. The path-shaped types (`calendar`, `todo_file`, `notes_folder`, `email_folder`, `reminders_file`) were retired: calendars are CalDAV-discovered; todo/reminders/notes read the briefing source's own explicit `path` — no convention-default filename, and the `notes/` folder is prompt guidance for the model only; email folders, `todo_file` and `reminders_file` have no consumer at all (their rows are inert, and left uncleaned only because deleting them is a data migration). `shared_file` survives as internal organizer state. Live in `[[users.X.resources]]` + the `user_resources` DB table; `folder` is operator-only (CLI/Ansible, no web UI).
- **Modules** — on-by-default features with their own UI tab and a settings page reachable via a cog icon (`feeds`, `money`, `location`, `health`). Names live in `istota.modules.MODULE_NAMES`. Per-user opt-out via `disabled_modules`. Single source of truth: `Config.is_module_enabled(user_id, module)`. Names that also appear in `EXPERIMENTAL_MODULES` are AND-gated on the matching `[experimental] features` flag — disabled-by-default until the operator opts in, after which they behave like any other module. (`EXPERIMENTAL_MODULES` is currently empty; the mechanism is kept for future modules.) A module whose optional install extra is missing is treated as **unavailable** rather than half-present: `modules.MODULE_DEPENDENCIES` maps a module to the import names its extra provides (`money → beancount`), `modules.module_available()` probes them with `importlib.util.find_spec` (cached), and `is_module_enabled` returns False for an unavailable module _before_ the per-user DB read. This is what lets the lean local install omit `money` entirely (no beancount) while a `uv tool install 'istota[local,money]'` lights it up — hidden everywhere at once (scheduler `_sync_module_jobs`, `/api/me`, web nav) through the single gate.
- **Connected services** — per-user external API credentials consumed by skills / modules (`karakeep`, `google_workspace`, `ntfy`, `garmin`). Stored encrypted in the `secrets` table (Fernet over scrypt-derived key from `ISTOTA_SECRET_KEY`); the bookmarks skill resolves both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` from there. Provisioned via `istota secret ensure|list|remove` (Ansible) or `/istota/settings` (web). Schema for both surfaces lives in `secret_schema.py`. `garmin` is cross-module (Health daily summaries + Location GPS-track import share one token blob): its auth surface is the module-agnostic `garmin_routes.py` router (not health-gated), its card is `custom_ui` (an interactive email/password→MFA flow, not writable fields), and `health.garmin.acquire_client` is the single sanctioned way to get an authenticated client — it re-persists rotated tokens under a per-user lock so the two consumers can't invalidate each other's blob.

## Experimental features

Operator-scoped feature flags for in-tree-but-off-by-default work. Configured via `[experimental] features = [...]` in `config.toml` (or `istota_experimental_features` in Ansible). Off by default; never exposed in the web UI; not toggleable per user. The flag list flows to every subprocess builder as `ISTOTA_EXPERIMENTAL_FEATURES` (CSV), so subprocess-paths see the same gate as the LLM path. Four surfaces honor the gate:

- **CLI subcommands** — `@requires_feature("name")` Click decorator (`src/istota/experimental.py`). Gated-off calls emit the standard `{"status":"error","error":"…"}` JSON envelope; in `_execute_command_task` / `_execute_skill_task` the envelope detector reclassifies stdout-OK exits as task failures with the human-readable message intact. Currently used by `money lots` (`money_tax`) and `money wash-sales` (`money_wash_sales`).
- **Skills** — `experimental: true` in `skill.md` frontmatter requires `skill_<name>` in the enabled set. The gate fires in the selection main loop, the sticky path, the companion pull-in, and the menu-catalogue filter (`eligible_skill_names`) so a gated-off skill reaches neither selection nor the on-demand menu.
- **Modules** — `EXPERIMENTAL_MODULES` mapping in `modules.py` (currently empty). When populated, `Config.is_module_enabled` AND-s the flag in before the per-user DB read; the `/settings/modules` web endpoint and `_coerce_profile_value("disabled_modules", …)` validation consult the same gate so a disabled experimental module never appears in the user-facing surface at all.
- **Web routes** — module-shaped surfaces only register when the gate is on; `/api/me` filters its `features` payload through the same check.

`istota experimental list` prints the `KNOWN_FEATURES` registry with on/off status from the loaded config. Unknown names in TOML log a warning but don't fail startup, so graduating a feature in code stays a code-only change. Naming convention: `module_<x>` for module gates, `skill_<x>` for skill gates, free-form for CLI subcommand gates. See `docs/EXPERIMENTAL.md` for the registry and graduation policy.

## ntfy push notifications

ntfy is a per-user connected service — there is no global `[ntfy]` block. Each user supplies their own server URL, topic, and (optional) auth via the encrypted `secrets` table (web settings or `istota secret ensure -s ntfy ...`). `notifications._send_ntfy` reads everything from the user's secret rows; if the user has no `topic` set, ntfy is a no-op for them. Default priority is hardcoded to `3`; per-call overrides flow through `send_notification(...)`.

Header values are RFC 2047-encoded by `ntfy_headers.encode_header_value` before they reach httpx, which serializes headers as **ASCII** — an emoji/arrow/em-dash title used to raise `UnicodeEncodeError` and lose the whole notification, body included (ISSUE-213); every briefing pushed to a phone was hit, since a briefing title carries an em-dash before the date. Bodies are opt-in **markdown**: `DeliveryOptions.markdown` on the transport, `--markdown` on the skill CLI, both emitting `Markdown: yes`. Opt-in because a plain-text body routinely carries `*`/`_`/`#`, and because ntfy renders markdown in its **web app only** — a phone popup shows the source, which is why `skills/ntfy/skill.md` tells the model to use it for genuinely structured messages rather than to bold a word.

What it IS: a one-way push channel (bot → device) used by heartbeat alerts, scheduled-job output (when `output_target=ntfy`), and `surface="ntfy"` notifications. What it ISN'T: two-way (you can't reply over ntfy), a Talk replacement, operator-shared infrastructure, or required (most users won't configure it).

## Module DB storage (local disk + WAL)

The framework `istota.db` and the four per-user module DBs (feeds/health/location/money) all run **WAL on local disk**. Rationale + the stall they fixed:

- **Framework `get_db`**: WAL is set once in `init_db`, never re-issued per open (re-issuing `PRAGMA journal_mode=WAL` takes a write lock that races sibling readers — the dispatch-loop stall root cause). `get_db` sets `synchronous=NORMAL` per open and accepts `busy_timeout_ms=` for the main loop's read-only scans (a lock past the budget → skip the tick instead of blocking 30s and tripping the watchdog; knob `scheduler.main_loop_read_timeout_ms`, default 2000).
- **Module DBs**: previously forced onto `journal_mode=DELETE` because WAL's mmap'd `-shm` SIGBUSes on the rclone FUSE mount (ISSUE-157) — DELETE gave zero reader/writer concurrency, so a per-minute reader could serialize the whole dispatch loop (ISSUE-156). They now live on local disk at `Config.module_db_path(user, module)` (default `{db_path.parent}/modules/{user}/{module}.db`; explicit `module_data_dir` is refused under `nextcloud_mount_path`) and run WAL. **Only the `.db` moved** — user-facing workspace files (health uploads, money ledgers, feeds exports) stay on the mount via each loader's unchanged `data_dir`.
- **Migration**: `python -m istota.db_relocate` (idempotent; copies each on-mount DB → local, `init_db` flips DELETE→WAL, `quick_check`, archives old as `*.migrated-<ts>`). Ansible runs it once with services stopped, gated on a `find` for legacy on-mount `*.db`.
- **Durability**: local DBs left the Nextcloud-synced workspaces, so `db_backup.backup_databases` snapshots them (SQLite online-backup API, consistent live copy) to `{mount}/istota-db-backups/<YYYY-MM-DD>/…` on a timer (`scheduler.db_backup_*`, default daily). Each run writes its own **dated** dir (not a single overwritten slot), retention keeps the `db_backup_retention` newest dirs (default 7) but never prunes a dir holding the newest _good_ copy of a DB, and a **collapse guard** (reusing `db_relocate._data_row_count`) quarantines a snapshot as `*.suspect` when a DB that previously held data comes back empty/unreadable — the same empty-shadow signal that protects relocation (exact-zero only: framework `tasks` legitimately shrinks under retention cleanup, so no fractional guard). The scheduler fires a `purpose="alert"` operator notification on any errored/suspect DB and on backup **staleness** (persisted last-run older than 2× the interval → backups silently stopped); both sends are bounded (run on a short-lived thread with a join timeout) so a wedged Talk can't stall the dispatch loop. Backup tree is `0700`/files `0600`. **Mount-liveness guard**: a mount-derived destination is written only when `os.path.ismount` is true — if the rclone FUSE mount is down, the run is skipped (never writes to local disk under a stale mountpoint) and the last-run clock is left stale so the staleness alert fires; an explicit `db_backup_dir` is trusted without the ismount check. The persisted clock advances **only when ≥1 DB snapshotted OK**, so an all-error run can't suppress the staleness net. Force an immediate backup with `python -m istota.db_backup` (ignores the interval — used right after a deploy to close the first-run gap). Restore via `db_restore` (`python -m istota.db_restore --all`), which copies the newest good cold copy back, clears stale `-wal`/`-shm` sidecars, and refuses to run while the scheduler daemon holds its flock (a copy over a live WAL DB would corrupt it); `init_db` re-flips WAL on next start. `db_health`'s `quick_check`+`REINDEX` sweep is kept as a backstop but the FUSE-corruption class it existed for is largely gone. **Both the sweep and the snapshot run off the dispatch thread** (`scheduler._spawn_background_check`, ISSUE-144 Tier 1): each goes on a short-lived daemon thread, skipped while a prior run is still in flight, so a slow sweep or a degraded mount can't starve `pool.dispatch()` — and neither needs the `LoopWatchdog.suspended()` muzzle any more. Tier 2 put the nightly sleep cycles on the same mechanism (`scheduler._run_sleep_cycles`), so no known-long check runs on the loop thread and no `suspended()` call site remains — the watchdog covers the whole loop.

## Local single-user install (standalone shape)

A second, first-class deployment shape alongside the server/Ansible/Docker one: a slimmed-down local install a single person runs on their own machine (spec in `Specs/Done/local-single-user-install.md`, docs in `docs/getting-started/local-install.md`). No Nextcloud, no server, no bwrap, no auth. Mostly config — the `use_mount=True` branch is already plain POSIX I/O, so pointing `nextcloud_mount_path` at a local dir (default `~/.istota`) lights up the whole workspace layout on local disk; `talk.enabled=false` drops Talk; sandbox/proxies are independently guarded and no-op cleanly. The genuinely new code:

- **No-auth web mode** — `[web] auth = "nextcloud" | "none"` (default `"nextcloud"`; env `ISTOTA_WEB_AUTH`). In `"none"` mode `web_app._require_api_auth` early-returns a fixed local-user dict (the single configured user, `Config.local_user_id`), `_user_is_web_admin` is True for that user, and `_verify_origin` is a no-op — all gated on `_no_auth_mode()`, an in-function flag check (not `dependency_overrides`) so it survives SIGHUP reloads. `_resolve_session_secret` generates a random per-process key in no-auth mode instead of crashing import. **Loopback guard**: `assert_no_auth_bind_safe(auth, host)` refuses to serve no-auth on a non-loopback bind (structural, not just documented). The frontend never redirects to `/login` because `/api/me` always 200s.
- **`istota serve`** (`serve.py` + `cli.cmd_serve`) — combined launcher: runs `scheduler.run_daemon(config, install_signal_handlers=False, ready_event=…)` on a worker thread (signal handlers are main-thread-only) and uvicorn on the main thread in one process, so web-chat `source_type="web"` tasks flow through the normal worker pool. `scheduler.request_shutdown()` sets the shared `_shutdown_requested` flag; `run_daemon` now clears it at start, sets `ready_event` before the loop, and raises `_DaemonAlreadyRunning` (instead of `return`) on flock contention so `serve` reports "already running" (the standalone `main()` catches it → clean exit). `bootstrap_checks` fails with a "run `istota setup` first" error when the DB/user is missing. `serve` sources `~/.config/istota/istota.env` (non-clobbering) before config load and sets `ISTOTA_CONFIG_PATH` so the web app's own `load_config()` (in its lifespan) sees a `-c` path. The daemon lock path is the module constant `scheduler.DAEMON_LOCK_PATH` (overridable in tests).
- **`istota setup`** (`setup_wizard.py` + `cli.cmd_setup`) — interactive first-run wizard: workspace, brain detection (`shutil.which("claude")` → offer the subscription backend, else collect an OpenAI-compatible base_url/model/key), user identity, port, and a grouped module-enablement block (location → money → email). Money is on by default (server parity); opting out writes `disabled_modules` to both the TOML block and the profile row (the row is what `is_module_enabled` reads first). Writes `~/.config/istota/config.toml` + a `0600` sibling `istota.env`, inits the DB, upserts the user profile, seeds the workspace via the shared `storage.ensure_workspace_for_user`. Idempotent; `--force` overwrites, `--yes` non-interactive (`--no-money` opts out of money in a `--yes` run). The wizard's renderers are pure functions and I/O is injected (`input_fn`/`which_fn`) for testing.
- **Standalone admin notice** — `Config.is_standalone` (blank `nextcloud.url` + `web.auth == "none"`) drives a `runtime: {mode, caveats}` block on `GET /api/admin/stats` (`web_app._admin_runtime_section`); caveats are **derived from what's actually disabled** (security caveat always present in standalone), and the SvelteKit admin page renders a collapsible banner when `runtime.mode == "standalone"`.
- **CalDAV decoupling** — new optional `[caldav] url/username/password` override the NC-derived `caldav_*` properties, so a local user can point calendar at any external CalDAV server; off unless configured.
- **Packaging** — `local` extras = web+feeds+calendar+email+markets (`install.sh --standalone` installs `istota[local,money,location]`, adding those two modules' deps on top); the release build (`scripts/build-web-static.sh`) copies `web/build` → `src/istota/web_static` (gitignored, `artifacts`-forced into the wheel), and `web_app._pick_static_dir` falls back to the packaged dir when the repo-relative `web/build` is absent. `schema.sql` lives at the repo root (outside the package dir) but `db.init_db` needs it at runtime, so it is `force-include`d into the wheel as `istota/schema.sql`; `db._resolve_schema_path()` prefers that packaged copy and falls back to the source-tree `<repo>/schema.sql`, so a non-editable `uv tool install` works with no checkout.
- **Bare-port redirect** — the whole UI lives under `/istota` (the base is baked into the SvelteKit build and, on the server, nginx routes `/istota/` → web). A standalone/direct-uvicorn run has no nginx, so `web_app._root_redirect` (`@app.get("/")`) 307-redirects `/` → `/istota/` and `serve` prints the bare-port URL. On the server nginx owns `/` (→ Nextcloud), so this app-level handler is only reached on direct access.
- **`install.sh` mode prompt** — run with no flag, `install.sh` asks Server vs Standalone (`--bare`/`--docker`/`--standalone` skip the prompt; a non-interactive run keeps the historical `--bare` default). `run_standalone` refuses root, ensures `uv`, installs from the local checkout (cloning first when curl-piped — to a **durable** `${XDG_DATA_HOME:-$HOME/.local/share}/istota/src`, not `/tmp`, so `istota update` can re-fetch it), best-effort builds the web assets when `npm` is present, runs `istota setup`, then writes install provenance (`write_install_record`).
- **`istota update`** (`updater.py` + `cli.cmd_update`) — self-update for the standalone shape only (`Config.is_standalone`; refuses on a server deploy so it can't contend with the Ansible auto-update cron). Provenance lives in `~/.config/istota/install.json` (`{method, source, extras, ref, channel}`), written by `install.sh`. **Update channel** (`--channel stable|main`, persisted back to install.json so it sticks): `stable` (the default for fresh installs — install.sh writes it) tracks the newest `v*` release tag; `main` tracks the recorded branch `ref` tip. A record predating the field falls back to `main` so an existing main-tip install is never silently `git reset --hard`-ed _backwards_ onto an older release tag (existing users opt into stable with one `--channel stable`); a checkout on a non-`main` feature branch installs as `main` (track that branch). Checkout method: dirty-gate (`git status --porcelain --untracked-files=no`; refuse unless `--force`) → resolve target per channel (stable: `git fetch origin --tags` → newest `v*` by `--sort=-version:refname`; main: `git fetch origin <ref>` → **`FETCH_HEAD`**, robust on a shallow single-branch clone where the remote-tracking ref isn't reliably updated) → compare HEAD vs target → no-op if equal → `git reset --hard <tag|FETCH_HEAD>` → rebuild web assets → `uv tool install --force --reinstall "<source>[<extras>]"` → migrations. Migrations shell out to the **freshly-installed** `istota init` (not the stale in-process `db.init_db` — nothing on the daemon/serve/web startup path runs framework migrations). On an install/migrate failure the checkout is rolled back to the pre-update commit so a retry re-detects the update instead of falsely reporting "already up to date". Every external effect (git/uv/web-build/migrate/daemon-flock probe) is injected for testing. `method="pypi"` is reserved (points at `uv tool upgrade`) but not yet implemented.
- **Security logging** — the "sandbox disabled" startup WARNING is softened to an INFO with intended-single-user-posture wording when `config.is_standalone`, still visible; the dev-only WARNING stays for a non-standalone sandbox-off config.
- **Storage-agnostic vocabulary** — a local session used to describe files as a "Nextcloud mount" even when the workspace is a plain local folder. The storage I/O was already backend-agnostic (the `use_mount` branch is plain POSIX), so this is purely prompt/skill _vocabulary_: `config.storage_backend` / `storage_label` (keyed on `bool(nextcloud.url)`, **not** `is_standalone`) drive a three-mode file-access block in the executor and storage-neutral skill bodies that reference paths via `{workspace}` / `{storage}` placeholders. Local mode adds a bullet clarifying the workspace is the _managed_ area, not the limit of what an unsandboxed local bot can read. Server/Nextcloud prompts are byte-unchanged. Spec in `Specs/Done/storage-agnostic-vocabulary.md`.

Nothing here changes the server/Ansible/Docker path: every field defaults to the server behaviour (`auth="nextcloud"`, `[caldav]` blank, `is_standalone` False).
