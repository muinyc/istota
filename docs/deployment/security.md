# Security

Istota isolates Claude Code invocations through layered security: clean environment, filesystem sandbox, credential proxy, and network isolation.

## Supported deployment

Linux with [bubblewrap](https://github.com/containers/bubblewrap) is the only supported deployment configuration. The filesystem sandbox is the boundary between users and between Claude and the host — without it, env-var scoping in the prompt is the only thing keeping one user's tasks from reading another user's data, and that boundary depends on the model following instructions.

macOS and any Linux without bwrap, or where bwrap can't create user namespaces, are **development configurations only**. They will run, but they provide no isolation guarantees and are not suitable for multi-user deployments. The scheduler logs a `SECURITY UNSUPPORTED CONFIGURATION` warning at startup when it detects either condition with more than one user configured.

A container is the common case of the second, and `CAP_SYS_ADMIN` is not what it is missing: Docker's default seccomp profile blocks the `unshare` call, and granting the capability instead gets past that and then fails at `pivot_root`. What a container needs is `seccomp:unconfined` together with `systempaths=unconfined`, and that pair buys the sandbox by giving up much of the container's own boundary — the syscall filter goes, and container root gets a writable `/proc/sys` whose `kernel` entries are not namespaced. Which way that trade falls depends on how many people share the deployment; see [Docker deployment](docker.md).

If you disable the sandbox or run on an unsupported platform, you accept that:

- A prompt injection in one user's task may exfiltrate any other user's data on the same host.
- Claude has access to the full filesystem visible to the istota service account, not just the per-user subtree.
- The credential proxy and network proxy still run, but their effectiveness drops without the sandbox boundary (Claude can read arbitrary files, including ones holding the credentials the proxy exists to hide).

## Clean environment

Every Claude Code subprocess gets a minimal environment built by `build_clean_env()`: only PATH, HOME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`). Task-specific variables (Nextcloud credentials, CalDAV, email, etc.) are added per-task.

For heartbeat/cron shell commands, `build_stripped_env()` removes all credential-pattern vars (PASSWORD, TOKEN, SECRET, API_KEY, etc.) from the environment.

## Filesystem sandbox (bubblewrap)

When `sandbox_enabled = true` (default), each Claude Code invocation runs inside a `bwrap` mount namespace with PID isolation.

**Non-admin users see**:

- System libraries (read-only)
- Python venv + source (read-only)
- Their own Nextcloud subtree (read-write)
- Active channel directory (read-write)
- Their temp directory (read-write)
- Extra resource paths

**Hidden from everyone, admin included**: every SQLite database the daemon owns. The framework DB directory and the per-user module-DB root (`module_data_dir`, holding each user's `health.db` / `money.db` / `location.db` / `feeds.db`) are covered by an empty tmpfs, applied as the last mount operations so no earlier bind shows through. Local DB backups and the browser profile live under the same directory and go with them.

Each mask is then remounted read-only (`--remount-ro`, where bwrap supports it — 0.2+). A writable mask makes the dead end lie: `sqlite3 {db_dir}/istota.db "select …"` creates the file on the tmpfs and answers `no such table`, which reads as a missing schema rather than as "this file is not in the namespace", and leaves a zero-byte `istota.db` behind for the rest of the task. Read-only, the same command fails at open. Nothing a task writes under a database directory can survive to be mistaken for a database.

A mask hides rather than revokes: `kernel.unprivileged_userns_clone` is on (bwrap needs it), so a sandboxed process can enter a nested user namespace and unmount a tmpfs to reveal whatever was bound underneath. `--disable-userns` is passed where bwrap supports it (0.8+), together with the `--unshare-user` it requires — without that companion flag bwrap refuses the argv, which is why the support check answered "unsupported" on every host and the flag reached no sandbox at all until this was found. In the shipped default nothing is bound underneath, so there was nothing to reveal either way. Both of those are reasons to keep `sandbox_ro_paths` narrow rather than to rely on the mask to make a broad entry safe.

**Also hidden from non-admin**: other users' directories, `/etc/istota/`, user config files.

The config directory is not bound — it holds `config.toml`. `emissaries.md`, `persona.md`, `guidelines/*.md` and the skill bodies reach the model as content the daemon read and put in the prompt, so they never needed to be there. `system-prompt.md` is the exception, since `custom_system_prompt = true` makes the CLI open the path itself; that one file is bound read-only, which leaves `config.toml` outside. Until now the file arrived only via the `sandbox_ro_paths = ["/srv/app"]` default, which is why narrowing that default broke every task on such an install.

**Admin users additionally see**: full Nextcloud mount (read-write), developer repos.

The masks are unconditional rather than a matter of not binding the files, because not binding them was the previous design and it did not hold. `module_data_dir` defaults under the framework DB's directory, the reference deployment puts that under `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` containing it — so one RO bind that named no database exposed all of them, to every task. `sandbox_ro_paths` now defaults to `[]` and is honoured from config (it was previously never parsed), but the masks are what makes the property hold regardless.

Reads and writes reach the databases only through skill CLIs, which the credential proxy runs **outside** the sandbox and which scope their queries by `ISTOTA_USER_ID`. That scoping, not the filesystem, is the per-user boundary; the sandbox is defence in depth behind it.

Linux-only and merged-usr compatible for Debian 13+. See [Supported deployment](#supported-deployment) above for the policy on non-Linux / no-bwrap configurations.

### The `.developer` carve-out

Each task's scratch space holds a `.developer` directory, written by the `developer` skill's `setup_env` hook. It holds two kinds of thing, and both need the same protection.

The **credential plumbing**: `credential-fetch`, the git credential helper, and the `gh` / `glab` wrappers. A task that could replace one of them could intercept a forge token on its next use.

The **policy the wrappers enforce**, which is the half that is easy to overlook: `forge-policy.json` — which carries the deny rules, the real binary path and the forge URL, precisely so the wrapper reads none of them from an environment the model's own shell can set — plus the seeded per-forge config dirs and the pinned-empty data dirs. Those last two are not incidental. `gh` expands aliases from `config.yml` *before* command dispatch, so a writable config dir is a complete bypass of the deny list; and it dispatches an unknown first argument to `gh-<name>` under the data dir, which no argv rule can see. A writable `.developer` would leave the deny list decorative rather than merely leaking a token.

`build_bwrap_cmd` therefore re-binds `.developer` read-only *after* the read-write bind of its parent, so the sandboxed path has always been covered. The in-process agent loop (the native brain) runs without bwrap, and its confinement is a list of writable roots — pure containment, which cannot express a hole inside a root. Every path under the task's temp directory, `.developer` included, was writable there.

`ToolEnv` now takes `write_denied_roots`, checked before the allow loop and on the write path only. Reads still pass, matching what the read-only bind gives the other brain. `native_fs_roots()` returns the carve-outs as a third element rather than leaving them to a second call a future caller could forget, and the executor passes them through as `BrainRequest.fs_write_denied_roots`.

The deny root is appended unconditionally rather than only when the directory exists. bwrap re-checks on every Bash call and self-heals; this list is built once, so an existence gate would hand a task that started before `.developer` existed an empty deny set for its whole life — and `Write` creates parent directories, so the model could then make the directory itself. A refused write reports read-only rather than "outside the allowed workspace", which is the one thing it is not.

### The task control directory

The same mechanism as `.developer`, applied to a directory of the daemon's own. Istota's standing instructions — identity, persona, tool descriptions, rules, response guidelines, skill bodies — reach the model as a *system* prompt rather than as its first message, so a native compaction cannot summarize them away. The handoff is a file: the executor writes `system_prompt.txt` into the task's control directory and names it on the brain request, which each backend then reads or passes to the CLI.

That directory is `{temp_dir}/.control/{user_id}/task_<id>/`, and it holds every per-task file the daemon authors: both prompt halves, the briefing block metadata and the prepared image renditions. It is a *sibling* of the task's scratch directory rather than a child of it, created `0700` at all three levels and owned by the daemon, so no task can write into it and nothing model-writable is an ancestor of it. The two guards are the ones above, applied to the directory: `build_bwrap_cmd` binds it read-only after every other bind, and `ToolEnv` carries it as a second `write_denied_roots` entry — plus a read root under confinement, since the directory is inside no write root and the task still has to open its own prepared attachment. Both are needed and neither substitutes for the other: the bind covers the sandboxed `Bash` tool, and the file tools reach `ToolEnv` without entering a namespace at all. Unlike `.developer`, the deny entry is seeded whether or not filesystem confinement is active, because the shapes that skip bwrap (macOS, the standalone install, the shipped Docker stack) are exactly the ones with no bind behind it.

Only the task's own directory is bound, never the per-user level above it, so one task reaches no other task's instructions and no other task's assembled request. The flat layout this replaced allowed both: the write for as long as two of a user's tasks overlapped, the read for the length of the temp-file retention window.

Two limits are worth stating plainly. `security.sandbox_ro_paths` is bound verbatim, so an entry at or above `temp_dir` would expose the whole tree; the config loader warns on one, and `istota doctor`'s `runtime.task_control_dir` reports the same overlap for the other binds a config can produce, including a `user_resources` row — which is bounded by the Nextcloud mount root and nothing else. And the deferred-op files still sit in the writable scratch directory with `O_NOFOLLOW` as their only guard against a symlink planted at a not-yet-started task's filename. Those are model-authored by design, and the scheduler's own validation of what they contain is the boundary there.

## Credential proxy

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment and routed through a Unix socket proxy instead. See [credentials](../configuration/credentials.md) for the full inventory of which credentials are global vs per-user and how they're provisioned.

The set of stripped variables is **manifest-derived**: `derive_credential_set(skill_index)` collects every env var declared with `sensitive: true` across all loaded skill manifests. Today's set:

- `CALDAV_PASSWORD`, `NC_PASS`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`, `TUMBLR_API_KEY`

`ISTOTA_SECRET_KEY` (the master Fernet key) is **not** in the manifest-derived set. It is the proxy's hard-reject lookup var (`_PROXY_LOOKUP_BLOCKED`) and never enters any subprocess env.

Adding a sensitive credential to a skill's `env:` block is the only step needed to route it through the proxy; there is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` list to keep in sync.

Skill CLI commands run through the proxy (`skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped per skill: `derive_skill_credential_map(authorized, skill_index)` returns the per-skill credential map, so a CLI invocation only ever sees credentials its own manifest declared. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

The proxy's Unix socket path includes the host process PID — `istota-proxy-{pid}-{task_id}.sock` (and the same shape for the network proxy). This prevents collisions when multiple processes (xdist test workers, parallel `istota run` instances, the daemon plus a manual scheduler) pick the same `task.id` from independent SQLite databases.

### Authorization model

Credential authorization is **decoupled from skill selection**. A skill is authorized for credential access if any of its sensitive `EnvSpec`s actually resolves under the task's context — that is, if the user has the corresponding resource configured (Karakeep, etc.) or the relevant instance config is set (SMTP, GitLab/GitHub tokens). Skill selection controls only which skill *docs* go into the prompt, not which credentials can be requested at runtime.

This avoids the failure mode where a keyword miss locks a skill out: e.g. a user has a Karakeep resource configured, the prompt didn't say "bookmark", `bookmarks` wasn't selected — under the old model the proxy would refuse to inject `KARAKEEP_API_KEY` and the CLI invocation would fail mysteriously. Under the new model the credential is injectable as soon as Claude decides it needs the bookmarks skill, regardless of selection.

Doc-only skills (no CLI module) are eligible too: the `developer` skill consumes `GITLAB_TOKEN`/`GITHUB_TOKEN` via `credential-fetch` from the git credential helper and the `gh` / `glab` wrappers its `setup_env` hook writes into the task's `.developer` directory. Gating authorization on `cli=true` (the prior heuristic) would lock it out.

Auto-authorization uses `_resolve_env_spec(spec, ctx, fallbacks_disabled=True)` so an instance-wide `EnvironmentFile` fallback for an operator-set value cannot fan out and auto-authorize every user — preserving the per-user privacy posture.

`derive_lookup_allowlist(authorized, skill_index)` is the union the proxy will respond to over `credential-fetch`, with `_PROXY_LOOKUP_BLOCKED = {"ISTOTA_SECRET_KEY"}` subtracted as a defense-in-depth hard reject. The master Fernet key flows into specific module-skill subprocess envs (so they can decrypt per-user secrets in-process) but is never returned over the lookup channel — `bash -c '.developer/credential-fetch ISTOTA_SECRET_KEY'` from inside Claude is rejected.

Threat model: a compromised Claude can only request credentials that already exist for this user (resources are user-scoped, instance config is operator-controlled).

### Rejection observability

Every proxy rejection emits a structured WARNING log:

```
proxy_rejected task_id=42 type=skill skill=evil_skill reason=unknown_skill
proxy_rejected task_id=42 type=credential name=NC_PASS reason=not_authorized
```

Reason codes: `unknown_skill` (skill name not in the CLI whitelist), `not_authorized_credential` (credential not in this task's allowed set), `credential_not_present` (credential genuinely missing from env).

Rejection responses include the structured `reason` field and, for unknown skills, an `authorized_skills` list — surfaced to the model via the client's stderr so it can adapt rather than retry blindly.

Use these logs together with the selection logs (`pass1_selection`, `disclosure:`; see [skills](../features/skills.md#selection-observability)) to count selection misses and decide whether a skill's keywords or disclosure mode need tuning.

## Admin-gated job types

Two scheduled-job types can run arbitrary shell, so they're gated to admin users:

- **`command:` rows in CRON.md** — `cron_loader.sync_cron_jobs_to_db` drops command-type rows for non-admin authors at sync time and orphan-deletes any DB row left over from a prior admin sync. `_execute_command_task` refuses non-admin tasks at runtime as defense in depth. Auto-seeded `_module.*` rows are scheduler-inserted, not user-authored, so they're unaffected.
- **`type: shell-command` heartbeat checks** — `heartbeat.run_check` refuses these for non-admin users.

CRON.md `command:` rows of the shape `istota-skill <name> [args]` (no shell metacharacters) auto-promote to skill-tasks at sync time and dispatch through `_execute_skill_task` instead, which is not admin-gated — operators can give non-admin users access to specific skills without granting full shell.

## The development container

A deployment may run project code — `npm`, `uv`, `cargo`, a test suite — inside the user's devbox rather than on the host. That is derived rather than switched on directly: it happens where `[developer] enabled`, `[developer] repos_dir` and `[devbox] enabled` are all set. Off by default, and where it is off none of this exists.

**Nothing here creates a container.** The devbox is per user and standing, started by the Ansible-rendered service. The daemon starts none, and a task cannot ask for one: the only verb that speaks Docker at all, `istota-skill devbox reset`, wipes `/home/dev` and restarts an existing container. It runs host-side in the skill CLI's own process, outside the sandbox, against the user's own container and no other.

**The Docker API has left the sandbox, and its proxy is deleted.** Until this change `build_bwrap_cmd` bound a per-user Docker-API allowlist proxy at `/var/run/docker.sock` in **every** task's sandbox — including tasks built from an email, a feed or a fetched page. The allowlist was narrow and held: it refused create, run, build, privileged and host-mount, so a task reaching the socket directly with `curl --unix-socket` could not escalate. It still permitted exec, cp, inspect and restart on that user's container from any task at all. Nothing in a build needs it now, so the bind is gone, `src/istota/docker_proxy.py` and its systemd units are deleted, and a deploy stops and removes any that are still running. The `docker` CLI is no longer bound in either — though `/usr` is, so a `docker` binary may still resolve on a host that installs one. The guarantee is the socket: none is bound at any path and no `DOCKER_HOST` is exported, so any `docker` a task finds fails at connect.

**What replaces it is gated, and that is the difference that matters.** Commands reach the container over a Unix socket into a server running inside it. That socket's directory is bound into a task's sandbox only when `"developer" in authorized_skills` — byte for byte the predicate that already decides whether the task's CONNECT allowlist gets the package registries and the forge. An allowlist is safe to hand every task; an unauthenticated arbitrary-command channel into a container with permissive egress is not, because it would be a route around the per-task, skill-scoped network allowlist described below.

**What does not improve, stated plainly.** What the model may do *inside* the devbox is not narrowed by any of this. `dev` has passwordless sudo, so a task with access was already root in that container, and the transport widens the degree: no output cap, no timeout of its own, stdin, and a real exit status. One axis narrows — a working directory the caller names must resolve under that user's repos root, where `docker exec -w` took any path in the container.

**Every containment decision is made inside the container**, by the server, at the moment of use. A file path is resolved with `realpath` and refused unless the result is under an explicit root list: that user's repos root, `/home/dev`, and the staging directory. A caller-named working directory is narrower still, and must resolve under the repos root. The credential and transport socket directories are refused by their literal default paths as well, `/run/istota-cred` and `/run/istota-exec` — a deployment that repoints either keeps the root-list refusal and loses the named one. This replaces four daemon-side mechanisms that each encoded a guess about what the container's mount table looked like, which is what the two file-copy defects before it were. A process inside the container sees what the container sees.

**Everything is per user.** `[developer] repos_dir` is a per-user root whether builds run in the container or on the host: the daemon derives `{repos_dir}/{user_id}`, the container binds only that subdirectory, and the socket directory is per user too — only the per-user subdirectory is ever mounted, since the parent holds every user's socket and mounting it would be arbitrary command execution against another user's repositories. The daemon and the container also run as the same numeric uid, which is what makes a file either side writes readable, editable and removable by the other.

**The socket directory is one a container root can write to, and that is bounded rather than denied.** `dev` has sudo, so it can create files there, change their modes, and unlink the server's own inode to bind something else in its place. Three things bound what that buys. The daemon connects to exactly one path it composed itself and never opens, lists or executes anything else in that directory. The directory is per user, so the substituting party is already root for that user and reaches only that user's own tasks. And the client refuses to hang on whatever answers: it gives up on an acknowledgement that does not arrive within its own ceiling rather than holding a shim for the length of a task. The directory holds one inode, is mode 0770 owned by the daemon's uid and gid, and on the default path lives on a tmpfs.

**Egress from the container is permissive**, as it always was, bounded by the `DOCKER-USER` drops the role installs for link-local, cloud-metadata, RFC1918 and carrier-grade NAT ranges. The hosts a postinstall script, a `build.rs` or a git dependency wants are not nameable in advance, which is the whole point of the container; the sandbox's own CONNECT allowlist below is unchanged and still governs everything running on the host. `CAP_NET_RAW` is dropped, so `ping`, `traceroute`, `mtr` and `tcpdump` no longer work inside the container: a build needs none of them, and a container holding that capability picks its own source address and walks past every address-scoped drop rule.

**A task's cgroup no longer bounds a build.** Container processes are in the container's cgroup, not the task's, so `memory.max`, `pids.max` and `cpu.max` stop applying to anything that moved. The container's own `mem_limit`, `pids_limit` and `cpus` replace them at a coarser granularity — per user rather than per task — so one user's runaway build can take another of their own tasks down with it. Cross-user isolation is intact. An OOM-killed build comes back as exit 137 and is reported by nothing else.

## Network isolation

When `[security.network] enabled = true` (default, requires sandbox), each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket.

Default allowlist:

- `api.anthropic.com:443` -- Claude API
- `mcp-proxy.anthropic.com:443` -- Claude API
- `pypi.org:443`, `files.pythonhosted.org:443` -- package installs (when `allow_pypi = true`)

Additional hosts added automatically:

- Git remote hosts from `[developer]` config when the developer skill is selected
- `results-receiver.actions.githubusercontent.com:443` on github.com, where `gh run view --log-failed` fetches job logs. Measured through a logging CONNECT proxy rather than assumed: it is one stable hostname across independent uncached runs, so an exact entry covers it and the proxy needs no wildcard support
- `registry.npmjs.org:443`, `index.crates.io:443` and `static.crates.io:443` when the developer skill is *authorized*, so an install inside a worktree can resolve its dependencies. Authorized is wider than selected: `derive_authorized_skills` also admits a skill whose credentials resolve, and `developer` qualifies as soon as either forge token is configured — so on such a deployment these hosts are on the allowlist for every one of that user's tasks, not only the coding ones. That is the gate the git remote hosts above already ride, deliberately; the registries were added to it rather than to a new one. Measured the same way: a full `npm ci` of this repo's frontend made 15 CONNECTs, all to the one npm host, and `cargo fetch` used the sparse index plus the download host. `crates.io:443` is the publish and search API, was never contacted by a build, and is not allowlisted. There is no `allow_npm` flag — the registries arrive with the developer skill, which is already opt-in through `developer.enabled` and skill authorization
- Operator extras via `extra_hosts`

**A private registry or a corporate mirror is an `extra_hosts` job.** The entries above are the public defaults, measured against the tools' own defaults; a deployment pointing npm or cargo somewhere else needs its host added, and the proxy matches `host:port` exactly with no wildcards, so a missing name fails at the boundary and reads as a broken install rather than as a refused connection. `extra_hosts` is global rather than skill-gated: it applies to every task, not only developer ones, and there is no per-skill operator list. Set it as `istota_security_network_extra_hosts` in the Ansible variables.

**Ecosystems that are not covered**, listed so the boundary is legible rather than looking like a bug. None of these is exercised by this repository, so unlike the entries above they are reasoned rather than measured. `node-gyp` compiling a native addon wants headers from `nodejs.org`; Playwright wants its own CDN for browser binaries; anything resolving a GitHub release asset wants `objects.githubusercontent.com`, which the `github.com` entry does not cover — and that last one includes `uv python install`, which fetches interpreters from release assets rather than from PyPI. Composer, Maven, NuGet, RubyGems and the Go module proxy are absent entirely. Add what a deployment actually needs through `extra_hosts`, and prefer measuring it through a logging proxy over taking a vendor's documented hostname on trust.

`gh run download` is deliberately **not** covered. Artifacts come from `productionresultssa<N>.blob.core.windows.net` with the shard varying per repository, and the only entry that would cover that is `*.blob.core.windows.net` — all of Azure Blob Storage, a general-purpose exfiltration channel reachable from the sandbox. The CI feedback loop needs logs, not artifacts.

The forge wrapper sets `GH_TELEMETRY=0` and `DO_NOT_TRACK=1`, so no telemetry host needs allowlisting and no command spends a rejected CONNECT on one. GitHub Enterprise Server needs no extra entry: its API is a path on the same host (`<host>/api/v3`), already added as the git remote.

No MITM -- TLS is end-to-end between Claude Code and the destination.

## Deferred DB operations

With no database reachable from inside the sandbox at all, skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes them after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_sent_emails.json` -- outbound email tracking
- `task_{id}_kv_ops.json` -- KV store set/delete operations
- `task_{id}_kg_ops.json` -- knowledge-graph fact add/invalidate/delete (per-op commit)
- `task_{id}_user_alerts.json` -- notices the model raised, graded `security` / `action_needed` / `note`; the first two are pushed to the user's alerts channel, a `note` is recorded in the panel only
- `task_{id}_email_output.json` -- deferred email sends (SMTP delivery after task completion)
- `task_{id}_health_ops.json` -- health module writes (stats, bloodwork, encounters)
- `task_{id}_garmin_import.json` -- Garmin Connect sync requests

One recovery artifact is written *by* the scheduler rather than read by it: `task_{id}_health_op_failures.json`, left behind when a health op fails mid-batch so an operator can recover the lost rows. It is recognized but never purged on retry.

Handlers and the shared envelope helper (`_load_deferred_json`) live in `scheduler_deferred.py`. Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection. See [scheduler](../architecture/scheduler.md#deferred-db-operations) for retry-replay safety and the unconsumed-file warning.

## Configuration

```toml
[security]
sandbox_enabled = true
skill_proxy_enabled = true   # needed wherever sandbox_enabled is true; turning it
                             # off with the sandbox on warns at startup, leaves every
                             # configured credential in the task environment where the
                             # model can read it, and leaves skill commands with
                             # nothing to read
skill_proxy_timeout = 300
passthrough_env_vars = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
sandbox_ro_paths = []        # extra RO binds for co-located services; keep narrow

[security.network]
enabled = true
allow_pypi = true
extra_hosts = []
```

The startup warning is read once at install. `istota doctor --only security.sandbox_credentials` reports the same pairing on demand, and the admin Health pane shows it as a standing `WARN` — with the bwrap probe's answer attached, so it says whether the sandbox you asked for is actually in force. Both switches off together is the single-user install's deliberate trust decision and is reported as a `SKIP`, not a warning.
