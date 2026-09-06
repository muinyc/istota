# Testing

Istota uses TDD with pytest and pytest-asyncio. The Python suite has roughly 16,200 tests across ~466 files; the frontend has its own vitest suite under `web/`.

Almost all of those tests assert against Python objects on a developer's host, which for most people is macOS. That is the right default and it has one blind spot: it cannot observe what actually runs in production — a built image, a rendered `config.toml`, a `PATH`, a bubblewrap namespace. The seven discretionary tiers under "Deployment tiers" below cover that, and none of them runs unless you ask for it.

## What to install

A bare `uv sync` is never right. It installs the base dependencies only, and everything the suite needs from `[project.optional-dependencies]` is left out — `click` from `money`, `fastapi` from `location` and `web`, and six other groups. The result is several hundred `ModuleNotFoundError` collection errors, which is a big enough number to read as a catastrophic regression from whatever change is under test rather than as a missing package. A full run that comes back with hundreds of failures is an environment fault until proven otherwise: read one traceback before touching the diff.

`uv sync --all-extras` is the simple answer, and costs about 1.1 GB in the venv. The suite does not need most of that. It runs clean, every marker deselected, on the `test` extra, which is `all` minus the two heavy ML ones, at 291 MB:

```bash
uv sync --extra test
```

That is what `scripts/setup.sh` and `docker/test/Dockerfile` install. It is defined once in `pyproject.toml` because there is no way to subtract an extra from `all`, and a nine-item list repeated across a setup script, a Dockerfile and two documents is a list that drifts.

The difference is `memory-search` (torch, sentence-transformers) and `whisper` (faster-whisper, av, onnxruntime). Every heavy import in `src/` sits inside a function rather than at module scope, deliberately — `memory/search.py` imports `sentence_transformers` and `sqlite_vec` in the functions that use them, and the whisper skill goes further and imports `faster_whisper` in a subprocess — so nothing needs either extra to collect. The one test that needs one at run time carries the `ml` marker.

Prefer `--extra test` wherever the venv is per-worktree or per-container, since the difference is paid again on every checkout. Prefer `--all-extras` on a long-lived developer host, where it buys the `ml` test and the real libraries for hand-testing at a cost you pay once. `istota[test]` is not a deployment shape: `local` and `all` are the ones to install the bot with.

Two rules keep this from decaying, both enforced by `tests/test_lean_install.py`:

- **A test-only dependency goes in the `dev` group, never in an extra.** `jinja2` (the ansible-template tests) and `psutil` (`emit_scheduler_stats`, `test_talk_leak`) used to arrive as a transitive of mkdocs, torch and faster-whisper. They looked declared from inside a full install and were missing from every lean one, so a lean install reported eight collection errors and two failures with no visible connection to a missing package. The check used to be a hand-written pair per package, which is why `pyyaml` was the same bug a third time — eighteen test files imported it and nothing declared it, on `caldav`'s coattails. Both directions are now a sweep over every package the test suite imports, resolved against what `uv sync --extra test` installs rather than against any declaration anywhere, so the fourth one fails here instead.
- **No test imports a heavy package at module scope.** A marker is applied during collection; a module-scope import fails *at* collection, so the `ml` marker cannot rescue it. The sweep in `test_lean_install.py` names the offending file instead.

## Running tests

```bash
scripts/qtest uv run pytest                                  # the default suite
uv run pytest tests/test_doctor.py                           # one file, no semaphore needed
uv run pytest tests/ --cov=istota --cov-report=term-missing  # coverage
```

`addopts` in `pyproject.toml` pins `-n auto`, so the suite runs under pytest-xdist by default. New tests must be order-independent. `-v` is only readable with `-n0`, since xdist interleaves worker output. For a local edit loop use `scripts/qt`, below — **not** a bare `--testmon`, which this repo's `addopts` silently disables.

### The edit loop: `scripts/qt`

```bash
scripts/qt                     # only the tests your change affects
scripts/qt --full              # the whole suite, refreshing testmon's data
scripts/qt tests/test_db.py    # scoped, still incrementally
```

The full suite is ~17,350 tests in ~125s (2026-08-30; it was ~16,700 in ~110s when the profiling below was done, so read these as the shape rather than as thresholds), and it is **throughput-bound across every core** — roughly 740s of CPU over 110s of wall at that measurement, with no long pole (the slowest single test is 5s). Measured consequences: xdist's `worksteal`, `loadfile` and `loadscope` are all *slower* than the default `load`; gating the twenty slowest infrastructure files removes 1,179 tests and 4% of the wall time; and making individual tests cheaper backfires if it trades CPU for I/O. There is no version of "make the suite faster" that pays. The only lever is running fewer tests.

`scripts/qt` is that lever. pytest-testmon records which tests executed which source lines, so an ordinary Python change reruns a handful of tests in under a second, and a change that affects nothing exits immediately.

**Do not work out the affected set by reading the code instead.** Both intuitive methods were measured against what actually executes, and both are wrong in both directions at once. Editing `shell_argv()` in `shell_exec.py` touches 101 tests across six files:

| Method | Files picked | Of the 101 tests |
|---|---|---|
| Name-matched file | `tests/test_shell_exec.py` | 12 |
| grep `tests/` for `shell_exec` | 4 | 12 |
| `scripts/qt` | 6 | 101 |

grep adds nothing over the name match. It picks three files that never exercise the function and misses `test_scheduler.py` (35 tests), `test_heartbeat.py` (26) and `native/test_tools_bash.py` (25) — which are exactly the three consumers `AGENTS.md` documents as depending on this function's `pipefail` semantics, so the tests that would catch a regression are the ones both methods skip. The reason is structural: dependence runs through call chains, not through text, and those files call something that calls `shell_argv` without ever naming it.

It fails the other way too. The same grep picks seven files for `git_remote_scrub.py` where one holds every test that exercises the changed function. And the name match assumes a file that often does not exist — of five sampled modules, `usage_render.py` is covered by `tests/test_cli_render_cost.py` and `process_group.py` by `tests/test_process_group_kill.py`.

Where a module does have a clean 1:1 test file, the guess is right and `qt` adds nothing. The point is that you cannot tell which case you are in without asking, and `qt` asks.

Three properties of the wrapper are worth knowing, because each is a trap if you invoke testmon by hand:

**testmon and `-m` are mutually exclusive.** testmon turns its selection off entirely the moment it sees a marker expression — it says so on a line most people scroll past: `testmon: selection automatically deactivated because -m was used`. This repo's `addopts` always carries one, for the discretionary-tier deselection, so a hand-run `uv run pytest --testmon` quietly runs *everything* while looking like it worked. `qt` clears `addopts` and applies the same deselection through `ISTOTA_DESELECT_TIERS=1`, which `tests/conftest.py` reads. `tests/test_tier_deselection.py` keeps the two sets in step, and fails loudly in the direction that matters — a marker deselected by `addopts` and missing from `DISCRETIONARY_MARKERS` would have an incremental run building Docker images.

**testmon cannot see into a subprocess.** About sixty of this repo's test files drive a shell script, a git binary, a Dockerfile or a rendered template rather than the product's own Python. Measured: changing `scripts/check-private-data.sh` selects **zero** tests, while the 39 tests that exercise that script sit right there. So `qt` does not trust testmon with non-Python changes at all — any changed file that is not `.py` (prose aside) falls back to the full suite. That is the conservative direction, and it is why the fallback is automatic rather than a flag you have to remember.

**An extensionless source file used to abort the whole session.** testmon derives a traced file's extension with `filename.rsplit(".", 1)[1]`, which raises `IndexError` on a name with no dot in it. Three of this repo's Python programs carry no `.py` suffix on purpose — they are on the devbox container's PATH and invoked by name — and `tests/test_devbox_exec_server.py` imports one in-process, so coverage traced it under its real name. The `IndexError` came out of a `pytest_runtest_logreport` hook, which makes it an INTERNALERROR rather than a test failure: every test passed, pytest exited 3, and nothing in the output said which file. The fresh-worktree case above is what made it reliable, since the run that builds `.testmondata` collects the whole suite. `tests/support/testmon_compat.py` patches `SourceTree.get_file` from `tests/conftest.py` to close it. If testmon aborts a session for some other reason, `qt` moves `.testmondata` aside and reruns the suite **without** testmon — the thing that aborted cannot be the thing that answers.

`.testmondata` is gitignored and local to one checkout, so **each worktree pays for its own**: the first `scripts/qt` in a fresh one finds no data and runs a full traced suite (~132s, against 110s untraced) to build it. That is once per worktree, not once per change, and it is the same run you would have done before the first commit anyway — but it means `qt` is not free the first time you reach for it in a new tree. Delete the file to force a rebuild; every `--full` run refreshes it in passing.

Two habits keep it useful. Run `scripts/qt --full` rather than a bare `pytest` for the pre-commit full run, so the data stays current instead of going stale the moment you run the suite another way. And reach for `--par` (or `--full`) when the change is to something central: `db.py` selects ~4,500 tests, which is most of the suite, and serially that is slower than just running everything.

Wrap a full suite run in `scripts/qtest`. Both this suite and vitest size their worker pool from `cpu_count()`, so each run claims the whole machine — correct for one run and pathological for several, which is what happens with work spread across parallel git worktrees. `qtest` is a `flock` semaphore holding one machine-wide slot; it queues the run rather than letting three jobs ask for 36 workers on 12 cores. Every run ends with one verdict line on stderr — `qtest: PASS exit=0 time=3m41s cmd: uv run pytest`, or `FAIL`, or `KILLED-SIGKILL`, or `NO-SLOT` — so a run read through `| tail`, which reports the pipe's exit code rather than the suite's, still says plainly how it went; stdout is left untouched. Exit code 75 means no slot came free and the command did not run, which is not a test failure. A single test file needs no slot, and neither do `ruff`, `svelte-check` or `format:check`.

Four variables tune it: `QTEST_SLOTS` (how many runs may hold the machine at once, default 1), `QTEST_TIMEOUT` (seconds to wait for a slot, default 1800), `QTEST_LOCK_DIR` (default `~/.cache/qtest`, deliberately outside any repo, because the resource being shared is the laptop) and `QTEST_DISABLE=1` to bypass the semaphore entirely.

Nine marker sets are deselected by default (also via `addopts`), each with a different prerequisite, so they are selectable independently:

| Marker | Needs | Runner |
|---|---|---|
| `integration` | a live Nextcloud instance, Garmin credentials, or a running devbox whose exec server answers, from a host shell — see below | `uv run pytest -m integration` |
| `live` | a Claude Code credential (or an API key); costs money — see below | `uv run pytest -m live -n0` |
| `linux` | a real Linux kernel, a usable bubblewrap, and — for the cgroup tests — a delegated cgroup v2 subtree. Docker only when the kernel has to be borrowed from a container | `scripts/test-linux.sh` |
| `image` | a Docker daemon | `uv run pytest -m image -n0` |
| `smoke` | a Docker daemon | `uv run pytest -m smoke -n0` |
| `full` | a Docker daemon, and the network — see below | `uv run pytest -m full -n0` |
| `testbed` | a Docker daemon, and no istota image | `uv run pytest -m testbed -n0` |
| `deploy` | a Docker daemon, and the network | `scripts/test-deploy.sh` |
| `ml` | the `memory-search` or `whisper` extra | `uv sync --all-extras && uv run pytest -m ml` |

**The `live` marker has one test, and it is the only thing that can say Claude Code turns a `Read` into sight.** `tests/live/test_claude_code_read_image.py` builds a two-colour PNG with no text, prepares it through the shipped `prepare_image_attachments`, renders the shipped directive with `build_image_prompt`, runs the argv `ClaudeCodeBrain._build_command` produces, and reads the raw `--output-format stream-json` lines for a `Read` call on that path whose tool result carries an image block. Raw lines rather than `brain._events.parse_stream_line`, deliberately: that parser returns `None` for the user frame a tool result arrives in, so a test written against it could only fall back to asserting on the path string istota itself wrote — which a model that opened nothing satisfies. It does not grade the answer's prose; whether the model names the colours is a question about the model. The JSON scanning lives in `tests/live/stream_json.py` and is tested for free in the default suite by `tests/test_live_witness_scan.py`, negative case included, since only the model call needs a credential.

It **skips** rather than fails with no credential, following the `developer.*` checks in `doctor.py`: a witness that hard-fails on every machine without a key is worse than no witness. The probe is `subscription_usage.resolve_token` over the ambient environment, captured at module import because the credential names are on the conftest scrub list. On macOS that probe usually comes back empty even where the CLI is credentialled — the token sits in the login keychain under a per-application ACL — so `ISTOTA_LIVE_TIER=1` asserts that this host can run the tier and turns every skip into a failure, the same split `ISTOTA_LINUX_TIER` draws.

**The devbox half of the `integration` marker needs a running devbox whose exec server answers.** `tests/test_skills_devbox_integration.py` is the only tier that reaches a real container: every other devbox test drives the protocol against a server in a tmpdir on the host, which proves the skill speaks the wire and not that the wire reaches a container. The assertions that make it worth running are the ones only a container can satisfy: a hostname that is not the host's, and a path that exists in the image and nowhere else. Run it from a host shell as `uv run pytest -m integration tests/test_skills_devbox_integration.py -n0 --devbox-user=<user>`. The option selects the per-user socket and promises that the tier must run. Without it, the file skips as an unavailable part of a general integration run. With it, a missing config or an unreachable transport fails the run instead of reporting fourteen skips. A task sandbox cannot run this tier: its config directory is not bound in, so the skill CLI cannot resolve the host-side socket. Command arguments cross the devbox exec transport, but environment variables prefixed onto a shimmed `uv` command do not. The explicit option therefore survives long enough to turn that misuse into a failure. The file used to probe for a Docker-API allowlist refusal and skip on it: the socket a sandboxed task saw was `istota-docker-proxy`, which tracked the exec ids it issued and denied a raw `docker exec` at the exec-inspect step after the command had run, so an envelope reading `ok` or `1` said nothing about the container (ISSUE-313). That proxy is retired, the skill runs `docker` only for `reset`, and the status now comes from `waitpid` over the protocol.

**The cgroup tests need a subtree the driver builds, and it builds one only in container mode.** `task_cgroup.resolve_root()` reads `/proc/self/cgroup` and truncates at the `.service` / `.scope` component; under Docker's default private cgroup namespace that file reads `0::/`, so it answers `None` however writable the tree is, and the `linux`-marked cgroup tests would skip inside the one runner meant to execute them. `scripts/dev/linux-tier-cgroup.sh`, sourced by the driver, remounts `/sys/fs/cgroup` read-write, empties the container's own cgroup into a `supervisor/` leaf (the `DelegateSubgroup=` shape — cgroup v2 will not let one cgroup both hold processes and enable controllers for its children), turns on the controllers, and exports `ISTOTA_TEST_CGROUP_ROOT` only after proving a `memory.max` write succeeds. The tests treat that variable as a promise: set and unusable is a failure, unset is an honest skip. So a Docker without `SYS_ADMIN` or with a read-only cgroup2 mount loses those tests and keeps the rest of the tier.

Native mode never sources that script, and that is the one thing it deliberately does not borrow from the container. In a throwaway container, rearranging the cgroup tree is the point of the file; on a real host it rearranges the machine's own tree, and on a deployment that tree is where the daemon's per-task cgroups live. So a native run leaves `ISTOTA_TEST_CGROUP_ROOT` unset — it also `unset`s an inherited one, since set-and-unusable is the failing case — and the cgroup tests fall through to `task_cgroup.resolve_root()`, which finds a usable subtree only where the run has one of its own and skips otherwise. That fallback is what makes the supported way of getting them work: `systemd-run --user -p Delegate=yes --pty scripts/test-linux.sh`.

A tenth marker, `requires_dac`, is not deselected: it skips itself when the process can bypass permission bits, which is what happens as root inside the Linux runner.

`image`, `smoke`, `full`, `testbed` and `deploy` must run with `-n0`. Their fixtures are session-scoped and build one tagged image; N xdist workers would each race to build it, and on the two compose tiers would also bring up their own stacks under one project prefix and sweep each other's projects. `testbed` is on the list for the same reason at a smaller scale — its mail container and two mailboxes are session-scoped, and `deploy` because its whole session is one converged container. All five conftests fail the session with that reason rather than letting it happen.

**Two shapes, one seam.** `smoke` and `full` are the same fixtures over different compose files. The *lean* shape (`docker/docker-compose.test.yml`) is one container with the entrypoint bypassed and the config rendered on the host: seconds to boot, right for a subsystem whose external is an HTTP endpoint. The *full* shape (`docker/docker-compose.yml` plus `testbed/compose/testbed.yml`) is the deployment as shipped — postgres, redis, nextcloud, istota, web, nginx — booted through `entrypoint.sh` with the generator running inside the container. It is the only thing that executes `provision-nc.sh` or reaches the half of `entrypoint.sh` past the config write. Full detail on both, and on everything below, is in `.claude/rules/testbed.md`.

**The full tier needs the network, and it is worth knowing which way that fails.** `provision-nc.sh` runs `app:enable spreed`, `calendar` and `files_external`; only the last is bundled in `nextcloud:30-apache`, and the other two are fetched from the app store at first install. Every `occ` call in that script is `|| true`, so an install with no network writes its provisioning flag and reports success having enabled nothing. `tests/full/test_provisioning.py` asserts outcomes by name for exactly that reason.

## Deployment tiers

Seven discretionary tiers, none of them automatic. Six answer "does the artifact match what the code assumes?" rather than "does the code do the right thing?"; `testbed` is the exception and is described below.

```bash
scripts/test-linux.sh                        # the suite + the linux tests, on a real kernel
uv run pytest -m image -n0                   # the built image's contract
uv run pytest -m smoke -n0                   # end-to-end against the lean compose stack
uv run pytest -m full -n0                    # end-to-end against the full stack, incl. a real Nextcloud
uv run pytest -m testbed -n0                 # wire-level email against a real IMAP/SMTP server
scripts/test-deploy.sh                       # the Ansible role converged on a real systemd host
scripts/test-upgrade.sh                      # the current image over an older release's state
```

**`scripts/test-linux.sh` has two modes, and picks between them.** `ISTOTA_LINUX_TIER_MODE=auto|native|container`, default `auto`. Container mode builds `docker/test/Dockerfile`, binds the checkout read-only at `/src` and runs the suite inside, which is what every macOS developer gets and what the driver did exclusively before. The image installs the dependencies from the lockfile and the project itself as a path entry in `/venv` naming the bind, so `istota` is importable by that interpreter in any child however its environment was built — which is what a deployment gets from `uv sync`, and what neither pytest's `pythonpath` nor a `PYTHONPATH` on the `docker run` could give the tool-server spawn (ISSUE-398). Native mode runs the suite on the host, in the worktree venv, with no Docker at all. `auto` picks native when the host is Linux and bwrap can create both a user and a network namespace here, and container otherwise — so a macOS host lands on exactly the path it always did. `ISTOTA_LINUX_TIER_PRINT_MODE=1` resolves the mode, prints it, and runs nothing.

On a Linux host the container is not merely slower, it tests *less*. Inside it bwrap runs as real root and never creates a user namespace, which is why the container has to be granted `CAP_NET_ADMIN` from outside; the deployment runs bwrap unprivileged, and a host run reproduces that. `--disable-userns` is never exercised in the container either — the flag needs `/proc/sys/user/max_user_namespaces`, which is read-only there, so `_bwrap_supports_disable_userns()` probes false and the flag is dropped from argv, which is why `tests/linux/test_sandbox_real.py` carries a standing instruction that an assertion about nested user namespaces has to guard on that same probe rather than assume the flag is in force. And the `requires_dac` tests skip as uid 0 and run as an ordinary user. What native mode gives up is the pinned toolchain: the image does `uv sync --frozen` and `uv tool install ruff==0.16.4` where a host run gets the worktree venv, though `tests/test_lean_install.py` pins the dev-group ruff equal to the image's so the lint gate still matches. The host needs `bubblewrap git sqlite3 tmux procps` present, which the image installs.

**`auto` never picks native on a host running the deployment.** Unsandboxed and correct is not the same as safe: the native tier spawns real bwrap namespaces and claims every core through `-n auto`, on the machine the daemon is running on. The driver probes rather than trusting whoever typed the command, and refuses with **exit 75**, naming both ways on: `ISTOTA_LINUX_TIER_MODE=container` to build and run the image there instead, or `ISTOTA_LINUX_TIER_MODE=native` to mean it. Neither is reachable by forgetting, which is the point.

Three arms, because no one of them sees every install shape and the dangerous answer is the false negative. **The units cannot be named in advance**: the Ansible role writes `/etc/systemd/system/{{ istota_namespace }}-scheduler.service` and the same for `-web`, `-webhooks`, `-devbox-proxy@`, `-docker-proxy@` (retired, still matched on, since a host that has not taken the teardown yet is exactly the host this guard exists for) and `-devbox-iptables`, and `istota_namespace` is an inventory variable — so the probe enumerates active units under both the system and the user manager and matches on those suffixes, rather than asking `is-active istota-scheduler.service` and going blind on the production host. **The config is not at one known path either**: the role renders it to `{{ istota_repo_dir }}/config/config.toml` under a home the script cannot guess, so what it checks is the two absolute-or-HOME-relative paths from `config.py`'s search order — `/etc/istota/config.toml` and `~/.config/istota/config.toml`, the latter being what `istota setup` writes for the single-user shape that has no unit at all. The third arm is `docker ps`, for the compose shape whose config is inside a volume. `ISTOTA_LINUX_TIER_DEPLOYMENT_CONFIG` adds a fourth path and can only make the guard stricter; it never replaces the list, because a variable that switched a safety guard off would be one `export` away from the thing being guarded against.

**`scripts/test-deploy.sh` is the bare-metal tier, and it is the newest** (ISSUE-439). `AGENTS.md` calls bare metal via Ansible the only canonical deployment shape — it is the only one where the sandbox actually works — and until this tier existed nothing in the repository had ever run `ansible-playbook`. The fourteen `tests/test_ansible_*.py` files parse the role's YAML and assert on the parse. That cannot see a unit systemd refuses to start, a rendered `config.toml` the loader rejects, or a task ordering that only breaks when the tasks actually run — and the first end-to-end run of this tier found one of each.

It boots `docker/test/Dockerfile.deploy` (Debian 13, systemd as PID 1) and drives the real `deploy/install.sh --headless` inside it, so the installer's own apt steps, `settings_to_vars.py`, the git clone, `uv sync --extra all` and all 35 `systemd` touchpoints run for real. `istota doctor` is the oracle, as it is for `image` and `smoke`. About three minutes on a warm cache.

**It is deliberately not `docker/test/Dockerfile`.** That image runs under `--init`, so PID 1 is a reaper — chosen with a comment saying "on a real host systemd does this job", which makes it precisely the container the role cannot converge on. PID 1 is the whole of it: ISSUE-440 moved that image to Debian 13 too, so the bases no longer differ. The `docker run` flags carry over and gain `--cgroupns=host` with a writable `/sys/fs/cgroup`, and `systempaths=unconfined` beside `seccomp=unconfined` — the pair without which bwrap creates a user namespace and then cannot mount a procfs inside it.

**What it does not cover**, so it is not read as covering the deploy end to end: reboot ordering and the `Require`/`After` relationship with the rclone mount unit, a real FUSE mount against a real remote, and anything about the host's own kernel, which a container shares rather than owns. It also converges with rclone, zram, Talk and the web UI turned off; `tests/deploy/conftest.py` gives each concession its reason. Run `scripts/test-deploy-negative-control.sh` when you change the tier — it breaks a unit file, a rendered config key and the sandbox grant, and requires each to turn a named set of node ids red.

**`testbed` is the odd one and is worth one paragraph.** It builds no istota image and brings up no stack: it runs a small mail server in a container and calls `poll_emails` and the email skill directly against it, over a local SQLite database. So it is not an artifact tier — it is the only place in the tree that opens a socket to a real IMAP or SMTP server, which is what inbound email routing, thread matching and the untrusted-sender gate have repeatedly needed and never had. It is deselected because it needs Docker, and it needs `-n0` because the mail container and its two mailboxes are session-scoped.

**Every one of them needs a Docker daemon that will create and start containers — the Linux tier unless it runs natively — so a sandboxed agent task cannot run any of them.** A task reaches Docker not at all: no socket is bound into a sandbox at any path and no `DOCKER_HOST` is exported, so the `docker` binary a task can still resolve under the read-only `/usr` bind fails at connect. The allowlist proxy that used to permit inspect, archive, restart and exec against the task's own container — and nothing that creates or starts one — is retired along with its bind. The Linux tier's container mode additionally wants `CAP_SYS_ADMIN`, `CAP_NET_ADMIN` and unconfined seccomp, which is the exact capability the sandbox exists to deny, and the deploy tier wants those plus a writable cgroup tree. This is structural, not a misconfiguration: widening the allowlist to admit it would hand every task a host escape. Both shell drivers refuse up front with **exit 75** — the tier did not run, which is a different thing from the tier running and going red — and the deploy driver says explicitly that it has no native mode to fall back to, because what it tests is a host converge and running that on this host would install the deployment over it.

The two shell drivers refuse up front when `ISTOTA_SANDBOXED` is set, and say so rather than failing later. The refusal still earns its place now that no socket is bound at all: a `docker version` or `docker info` precheck fails at connect, which is a message about a socket rather than about the boundary, and a driver that got past one would die minutes later inside `docker build` reporting a buildx error that describes nothing at all. The refusal is what names the real reason. It predates the socket's removal, when `docker version` was on the allowlist and a task passed that precheck outright.

**For the Linux tier, Docker is not the whole reason and on a Linux deployment host it is not the one that bites first.** A task's own sandbox passes `--unshare-user --disable-userns` wherever bwrap supports the flag (`executor.py`, `_bwrap_supports_disable_userns`), and that switches off exactly the nested user namespace a second bwrap needs to start. So on the production shape both routes are shut: the container cannot be created, and bwrap cannot be run directly either. That matters because native mode makes "skip the container" a supported flag — an agent reading a refusal phrased only in terms of Docker, on a machine that is already Linux with `/usr/bin/bwrap` in view, has an obvious next move that fails with a namespace error naming nothing about the real boundary. No value of `ISTOTA_LINUX_TIER_MODE` opens either route.

The driver **probes** rather than asserting that second half, and the distinction is worth keeping: a deployment on bwrap older than 0.8 never got `--disable-userns` at all, so a nested namespace really would start there, and the refusal says so instead of claiming a boundary that is not present. It still refuses — a task runs on the machine the daemon runs on, and the sandbox masks the database directories, keeps config out of view and routes the network through an allowlist, so the suite would go red on the sandbox rather than tell you anything about it — but it refuses for the reasons that are true.

That refusal **exits 75**, the same code `scripts/qtest` uses for "the command did not run". Every real failure in those scripts exits 1, so the two are distinguishable from the status alone: 75 means the tier was out of reach and nothing was tested, and it is not a red suite.

**What an agent should do instead.** When a change touches the sandbox, the network proxy, the skill proxy, a migration or the image, say in the merge request that it does, name the tier that covers it, and ask for the run before merge. That is a complete handover, not an apology: the reviewer knows which command to run and why. Do not merge a sandbox-touching change while quietly reporting the default suite as green — that suite patches `_bwrap_available` and checks argv, so it has never executed the code path in question.

The tier that executes it is `smoke`. `tests/smoke/test_sandbox_in_stack.py::TestTheDatabaseMasks` reads `db_path.parent` from inside a live task in the shipped image and requires an empty read-only tmpfs there, which tells a sandbox that ran from one that was skipped — a Bash tool call whose output came back proves neither. It found the deployment running every task unsandboxed the first time it was run. `tests/smoke/test_sandbox_repos_isolation.py` is the second witness of that kind: it seeds another user's subtree under `developer.repos_dir` on the host and requires the path to be missing from inside the task, so a skipped sandbox — where the daemon's own view is what the task gets — fails it rather than passing quietly. `TestTheComposedSystemPromptInTheStack` is the third: it reads `system_prompt.txt` out of the task's control directory from inside a live task and requires it to hold Istota's standing instructions, to refuse an append, to refuse a file planted anywhere in that directory, and a sibling file in the *per-user temp directory* — a different directory — to stay writable, which is what separates a read-only bind scoped to the control directory from a task directory that went read-only wholesale. Two further answers require a neighbouring task's control directory to be absent and unreadable, which is what witnesses that the bind is scoped to this task rather than to the per-user level above it.

When to run each:

| Tier | Run it when | Cost |
|---|---|---|
| `scripts/test-linux.sh` | you touched the sandbox, the network proxy, the skill proxy, per-task cgroups, or anything else whose behaviour differs on Linux | minutes; the whole suite, in a container on macOS and on the host itself on Linux |
| `-m image` | you touched either Dockerfile, `render-config.sh`, or anything about where a binary lives | under a minute against a warm layer cache; builds both images natively |
| `-m image --platform amd64` | before a release, on a non-amd64 machine | about ten minutes under emulation. It checks the deployment architecture rather than reaching tests nothing else runs — since ISSUE-280 the devbox image builds natively too, so its assertions are covered by a plain `-m image` |
| `-m smoke` | you touched the developer skill's forge chain, the sandbox, the entrypoint, the compose stack, ntfy, feeds, or outbound email | about three minutes against a warm layer cache: one stack per profile rather than per test, so most of it is the six boots |
| `-m full` | you touched `entrypoint.sh`, `provision-nc.sh`, `docker-compose.yml`, Talk, Nextcloud storage or shares, or anything about first-boot provisioning; and before a release | minutes, most of it one cold boot of six containers — 50 to 84 seconds to both healthchecks on warm base images, then the scenarios |
| `-m testbed` | you touched inbound email — routing, threading, the confirmation gate, the DMARC canary — or anything in `skills/email/` | about half a minute, one small container |
| `scripts/test-deploy.sh` | you touched `deploy/ansible/`, `deploy/install.sh`, `deploy/settings_to_vars.py`, or a unit-file template | about three minutes, one systemd container |
| `scripts/test-upgrade.sh` | you touched a migration, a config key, or `config.toml` generation | seconds against a cached capture |
| `scripts/test-upgrade.sh --from-floor --shape volume` | before a release | seconds, plus one container the first time |

The costs above were measured on one arm64 developer machine with warm caches; they are the shape of each tier rather than a threshold.

Two of these carry a negative control, and the controls are not a formality — on a tier that asserts against an artifact, reading the test tells you almost nothing about whether it can fail. `scripts/test-image-negative-control.sh` covers both halves of the image tier and requires each to go red against a deliberately broken image.

The istota half is one control: the image with `/usr/local/lib/istota_forge` removed. The devbox half needs **ten**, because that file asserts several separable things and no single broken image reaches all twenty-seven of its assertions:

| Control | Turns red | Why it is separate |
|---|---|---|
| `Dockerfile.devbox-no-forge` | the six forge version assertions | removing the directory leaves `/usr/local/bin/gh` alone — it is a *copy* of the wrapper, not a symlink — so four assertions stay green |
| `Dockerfile.devbox-stale-wrapper` | the byte-identity assertion | the file is present and still runs; only its bytes differ. Deleting it makes that test raise before it compares anything, which proves the file exists rather than that the comparison works |
| `Dockerfile.devbox-real-binary-on-path` | `test_what_resolves_is_the_python_wrapper_not_a_real_binary` | the bypass reached by overwriting the wrapper on PATH |
| `Dockerfile.devbox-forge-dir-on-path` | `test_the_name_resolves_to_the_wrapper`, `test_the_real_binary_is_off_path` | the same bypass reached by the likelier route — one `PATH` entry, no file changed. The other three controls left these four assertions either untouched or failing through a guard rather than through their own comparison |
| a real build with `--build-arg DEV_UID=1234` | `test_the_dev_account_has_the_default_uid_and_gid` | the only control that is a build rather than a perturbation. `DEV_UID` exists so the deploy can pass the daemon's own uid, and the only way to know the arg works is to use it — `usermod -u` on the built image would turn the same assertion red and prove nothing about `ARG DEV_UID`. It is also the only one that has to pass `--platform` itself, since it starts from a multi-arch base rather than from the built image |
| `Dockerfile.devbox-home-owned-by-a-stranger` | `test_the_home_directory_belongs_to_the_dev_account` | that test compares two of the image's own self-reports, which is the shape of an assertion that agrees with itself on any image. The build-arg control moves the account and the directory together and leaves it green, which is correct and is why both exist |
| `Dockerfile.devbox-stale-exec-protocol` | the exec protocol's byte-identity assertion | the second file `scripts/sync-devbox-lib.sh` syncs. Same argument as `devbox-stale-wrapper`: present, importable, different bytes |
| `Dockerfile.devbox-no-exec-server` | `test_the_exec_server_is_installed_and_executable`, and both transport-startup assertions | the supervisor is intact and loops on exit 127, so nothing ever binds. It also turns the `/home/dev` repair test red, for the wrong reason — that test probes the wire — which is why it is not named there |
| `Dockerfile.devbox-no-home-repair` | `test_the_supervisor_repairs_a_home_directory_with_the_wrong_owner` | the transport comes up normally and only the repair call is removed, so the assertion reaches its own comparison. The `sed` verifies its own edit took, because a control that quietly changed nothing reports a green tier as proof |
| `Dockerfile.devbox-workspace-present` | `test_the_image_has_no_workspace_directory` | an absence assertion is the archetype of a test that can never fail: `test ! -e` against a path nothing creates passes on any image at all |

Six assertions in that file have no control, deliberately, and each fails closed rather than passing vacuously: `test -x` against a named absolute path, `python3 -c 'import …'` against a named directory, a `Cmd` compared to an exact list, `command -v uv` compared to the directory the home volume mounts over, the graceful-stop assertion (a log line only a graceful shutdown writes, plus an unlinked socket), and the unconfigured hold (a process still alive and a message naming two literal variables).

**Each control names the exact parametrized node ids it must turn red**, and the script requires those to appear in pytest's own `FAILED` summary. Checking only the exit status is not enough, and that is not hypothetical: the first cut did that, and one control passed on a `UnicodeDecodeError` raised inside `subprocess` before its assertion ever ran — red for the right image, for the wrong reason, which is indistinguishable from a working assertion and is exactly what a control exists to tell apart. The underlying harness bug is fixed too (`errors="replace"` in `tests/image/conftest.py`).

The upgrade tier's control is the forge-less istota image passed through `ISTOTA_IMAGE_TAG`:

```bash
ISTOTA_IMAGE_TAG=istota-test/no-forge:control uv run pytest -m image -n0 tests/image/test_upgrade.py
```

A clean run there is the failure.

### Which shape a subsystem belongs on

The rule is not real-versus-stubbed. It is what the subsystem needs in order to be exercised honestly, and cost breaks ties.

| Subsystem | Shape | Why |
|---|---|---|
| Forge chain | lean | The forge is HTTP and git over HTTP. A stub is the real protocol; a real GitLab is a heavy container for no added fidelity |
| ntfy | lean | A POST with headers. The assertion is about bytes on the wire, which a stub records better than a real server |
| Feeds | lean | Static documents over HTTP, plus conditional-GET behaviour a stub can drive deliberately |
| Sandbox masks, secret isolation | lean | Cheapest place to run them. Both shapes carry the same concessions, so this is a cost choice rather than a constraint — and `tests/full/test_provisioning.py` repeats the mask assertion, since the full shape's two `security_opt` lines are otherwise checked only by parsing a compose file |
| Email, wire level | neither | A mail server standalone, no istota container at all. That is the `testbed` marker |
| Email, deployed path | lean | `poll_emails` needs no Nextcloud for attachment-free mail |
| Email attachments | full | The write lands under `/mnt/shared` on both shapes; only the full one can read the bytes back out of Nextcloud |
| Talk, storage, shares, notifications | full | The client negotiates capabilities. A stub that answers that wrongly is worse than no test |
| Provisioning and first boot | full | The thing under test *is* `entrypoint.sh` and `provision-nc.sh` |

Eight profiles carry that split. A profile is a named shape plus the services it runs plus any extra config, declared per test as `@pytest.mark.profile("forge")` and defaulting to `base`; `StackPool` keys by name and boots each one once per session.

| Profile | Shape | Services |
|---|---|---|
| `base` | lean | model |
| `forge` | lean | model, gitlab |
| `no-forge` | lean | model, gitlab, on an image with the forge binaries removed |
| `notify` | lean | model, ntfy |
| `feeds` | lean | model, feeds |
| `mail` | lean | model, mail |
| `signaling` | lean | model, signaling |
| `full` | full | model, nextcloud, mail, signaling |

Fine-grained on the lean shape, exactly one on the full shape. Many profiles is an argument about a thirty-second boot — a stack with every subsystem on has the daemon polling mail, feeds and Talk during every unrelated test — and it inverts at a cold six-container one, where a second full profile would be a second cold boot to run one more scenario. A test that needs a stack nobody else has touched declares `@pytest.mark.profile("full", fresh=True)` and pays for a private one.

### What the full shape concedes

`testbed/compose/testbed.yml` is a harness concession file, not a deployment recipe. It is the complete list of ways the stack the `full` tier boots differs from the stack an operator boots, each entry carrying its reason inline. Read it before adding to it, and add there rather than assembling a fragment in Python, so a sixth concession gets reviewed:

- `extra_hosts: host.docker.internal:host-gateway`, so a host-side stub is reachable by one name on Docker Desktop and Docker Engine alike.
- `seccomp:unconfined` **and** `systempaths=unconfined` on the `istota` service.
- The three credential-shaped brain variables as fixed literals rather than interpolations, on `istota` and on `web`. The process environment outranks an `--env-file`, so a developer's exported `ANTHROPIC_API_KEY` would otherwise reach a test container that POSTs to a listener on their own machine.
- A healthcheck on the `tasks` table. The shipped `istota` service has none, and `restart: unless-stopped` means a boot that dies on the 600-second provisioning timeout comes straight back, so "is the container running" reads a wedged stack as healthy.

The generated passwords, the module switches derived from the profile's service list, and the ephemeral `NC_PORT` with its matching `ISTOTA_WEB_CALLBACK_URL` are in the env-file the pool writes, because a file in the repository cannot hold a value invented at boot.

**The two security options are a pair and neither substitutes for the other.** Seccomp lets bubblewrap create the user namespace; it does not let it mount a procfs inside one, and `build_bwrap_cmd` emits `--proc /proc` on every sandbox. Docker's masked `/proc` entries and read-only `/proc/sys` make the container's procfs not "fully visible" to the kernel, which then refuses the mount, so with only the seccomp grant every real sandbox dies at "Can't mount proc on /newroot/proc". `--cap-add=SYS_ADMIN` is not an alternative: measured, it gets past the unshare and fails at `pivot_root`. `docker/docker-compose.test.yml` carries the same pair.

**The shipped `docker/docker-compose.yml` carries neither, so a Docker deployment runs every task unsandboxed.** That is deliberate and settled, and it is why bare metal via Ansible is the only canonical deployment shape. The pair costs the container's own boundary: the container runs as root and is not userns-remapped, so a writable `/proc/sys` hands it kernel entries that are not namespaced, and `seccomp:unconfined` drops the syscall filter as well. On bare metal bwrap unshares the user namespace unasked and neither setting is needed. `docs/deployment/docker.md` has the trade written out.

### The storage backend, and why it needs no stack

`Config.storage_is_nextcloud` is `bool(self.nextcloud.url)`, and both values are shipped install shapes: `local` is what the single-user install runs, not a test convenience. So a change that decouples istota from Nextcloud and breaks the Nextcloud-free install has to go red somewhere.

It costs no stack, because `storage.py` branches on `use_mount` rather than on the backend and `render-config.sh` writes `nextcloud_mount_path` as the literal `/mnt/shared` on every profile. Briefings, memory and the tasks file take the identical path under both. Three things differ, all of them pure functions of a `Config`: the prompt's file-tool vocabulary, the skill menu (`available_capabilities()` drops `nextcloud` on an empty URL), and whether `runtime.mount_liveness` runs. The first two are prompt content and live in the goldens as the `base_nextcloud` / `base_local` pair; the third is `tests/test_doctor.py::TestMountLiveness`; and whether `NC_URL=""` renders a config that loads as `local` at all is `tests/test_render_config.py`.

Set-but-empty, not unset. The render's preflight is `[ -n "${NC_URL+x}" ]`, which tests whether the variable is set rather than whether it has a value, so an unset `NC_URL` fails the render with exit 2. `APP_PASSWORD` is required by the same preflight and takes the same treatment. Every lean profile renders this way, which is why `runtime.mount_liveness` reports `skip` there and why a `doctor` assertion in this tier names the checks it cares about instead of comparing a whole payload.

What that gives up: nothing asserts a *booted* local-backend daemon, only that it is configured correctly and assembles the right prompt. Since those three rows are the whole delta, that is a distinction without a consequence today. Naming the axis anyway is what stops someone adding a Nextcloud stub to the lean shape for an unrelated reason and silently deleting the coverage.

### Shared machinery, and how to add a tier

The pieces under `testbed/` are general, not forge-specific. The forge chain is the first thing to use them, and it should not be the last:

- `stack.py` — bringing a compose stack up and down, waiting for a service to report healthy, sweeping leftovers from an interrupted run, and `Stack`, which is what a scenario is handed: `submit`, `script`, `exec`, `doctor`, `restart`, `logs`, `diagnostics`.
- `probe.py` — reading the framework database of a stack that is currently running, or of a local file.
- `httpstub.py` and `services/` — the `Service` protocol every external the daemon talks to conforms to, and the shared `ThreadingHTTPServer` base under the ones we wrote. `services/model_endpoint.py` is a deterministic model endpoint serving canned turns over HTTP, so a task's path through the daemon is reproducible without an LLM; `services/gitlab.py` answers enough REST v4 for `glab` plus a real git over HTTP.
- `profiles.py` — what a scenario declares it needs: a shape, a set of services, and any extra config.

`tests/support/upgrade.py` — capturing an older release's `config.toml` and schema — deliberately stayed where it is. It belongs to the upgrade tier, and `scripts/test-upgrade.sh` reaches it by string path.

`testbed/` sits beside `src/` rather than inside `tests/` because it is not part of the shipped application and two repos outside this one consume it. It has its own `pyproject.toml` and imports no pytest, so a failure surfaces as a raised `StackError` rather than as a call into a test runner that is not installed. When a new subsystem needs an end-to-end tier, write a service and reuse these — don't build a second stack alongside them.

`testbed/services/model_endpoint.py`'s wire format has its own tests in the default suite (`tests/test_model_endpoint.py`), pinned against the real provider over a real socket. That matters more than it looks: nothing in a smoke test can tell a correctly framed stream from a subtly wrong one — a stream missing its completion signal arrives as a task that failed for a reason unrelated to what the test was asserting.

### Writing a new service

A **service** is anything the daemon talks to that is not the daemon, real or written by us; a **stub** is one we wrote. The protocol in `testbed/services/__init__.py` has five required members: `name` (its registry key), `container_url` (the address a process inside the container reaches it on, which the caller never has to know the shape of), `config_env()`, `reset()` and `close()`.

Four more are optional and resolved by `getattr`, so a service implements only the ones it needs:

| Member | Implemented by | What it is for |
|---|---|---|
| `compose_env()` | `mail` | Host paths and image tags a compose *overlay* binds. Held to a different rule from `config_env()` — these appear in no shipped file, so the two-file rule below does not apply to them |
| `container_state_paths` | `gitlab` | Container-side directories this service's use dirties. They are `rm -rf`'d as root inside the container, so the list is validated against a protected-path set that refuses `..`, relative paths and anything at or under the database and config directories |
| `bind_stack(stack)` | `nextcloud`, `mail` | Hands the service the stack it belongs to. It is what lets an attached service exist at all |
| `describe()` | most | What the service renders about itself into `Stack.diagnostics` |

The checklist for adding one: register the factory in `REGISTRY`, give it a branch in `services.build()` (a deliberately non-uniform adapter — `tests/test_testbed_services.py::test_it_covers_every_registered_service` fails without it), list the name in `HOST_STUBS` or `ATTACHED`, give it a profile in `testbed/profiles.py` with an entry in `profiles.ALL`, and — if it ships a new module — add it to `only-include` in `testbed/pyproject.toml`. That last one fails quietly: the in-tree import keeps working and only an installed consumer sees the gap.

Keep the dependency footprint to the standard library. `testbed/` depends on `cryptography` and nothing else on purpose, because a resolver conflict would push the two rigs outside this repo back to copying the package rather than installing it.

Call recording is deliberately not on the protocol. It is on `HttpStub`, the shared `ThreadingHTTPServer` base, because it does not generalize — a mail server speaks IMAP and Nextcloud is asserted through its own API. A `calls` list on the protocol would mean something different for two of six members.

Four rules bind anything added. The first three are enforced in the default suite rather than left as convention — `tests/test_testbed_services.py` checks every service's and every profile's variables against the two shipped files, `HttpStub.start` raises, and `Probe.rows_above` refuses. The fourth is a judgement call and is the one to argue about before writing the stub, not after:

**Wire it in through a variable the shipped generator reads and compose passes through.** Two files, `docker/istota/render-config.sh` *and* `docker/docker-compose.yml`, and they are not automatically in sync: `ISTOTA_EMAIL_AUTHSERV_ID` and `ISTOTA_EMAIL_CONFIRM_SENDER_MATCH` were read by the generator and passed by neither, so an operator who set the confirmation gate to `verify` in `docker/.env` silently got `off`. If a variable is missing, add it to both as a reviewed product change. The guard on the product side of that — `tests/test_render_config.py`'s passthrough check — is parametrized over `ISTOTA_DEVELOPER_`, `ISTOTA_EMAIL_` and `ISTOTA_NEXTCLOUD_` rather than over everything the generator reads, so a variable in a fourth family needs the prefix list widened with it. Never side-load config from the fixture: that is the property that makes the whole tier honest, and it applies to `Profile.config` exactly as it does to `config_env()`. A service with no such variable — ntfy is a per-user secret, feed URLs are DB rows — returns an empty `config_env()` and says in its docstring why, because an empty one otherwise reads as an oversight.

**A stub bound to anything but loopback must be given a credential to expect.** `HttpStub.start` raises otherwise. Both compose tiers bind all interfaces so a container can reach the stub, which on a laptop on a shared network is an open listener — and in the forge stub's case one running `git http-backend` with `GIT_HTTP_EXPORT_ALL`. It also gives `tests/smoke/test_secret_isolation.py` the name of every secret the session published, which is what it sweeps the model transcript for.

**A negative assertion takes a watermark and a discriminating column.** `Stack.reset` returns `Probe.watermark()`, `MAX(id)` per table, and the fixture stashes it as `stack.mark`; `Probe.rows_above(table, mark, **filters)` refuses to run with no filter. Both halves are needed. Under a session-scoped stack, "no reply was sent" against `sent_emails` reads the previous scenario's rows, because nothing truncates a framework table — and a watermark on its own still catches a row one of the daemon's background pollers made during the test. `source_type`, `conversation_token` and `to_addr` are the columns that discriminate in practice.

**Do not stub a service whose client negotiates with it.** `nextcloud/capabilities.py` means the client asks before it acts, so a stub answering wrongly steers the daemon down paths no test chose; that is why the full shape runs a real Nextcloud, and why the mail service is a real IMAP/SMTP server in a container rather than something we wrote. The rule is not "never stub". `gitlab` is spoken to by a real `glab` and a real `git`, and `ntfy`'s whole assertion is about header bytes, which a recording stub sees better than a real server would.

`reset()` runs before each test rather than after, so a failed test's state is still there to inspect; it must be cheap and total, because a reset that leaves one mutation behind is a cross-test dependency that gets diagnosed as flake. Anything the daemon writes *inside* the container is the service's to declare too — a host-side stub can rebuild its own state and cannot reach the checkout the model cloned into `/data/repos` on the previous test.

### Writing a scenario

A scenario declares the stack it wants with a marker and imports nothing from `testbed`:

```python
@pytest.mark.profile("forge")
@pytest.mark.script([{"text": "on it"}, {"bash": "git status"}])
def test_the_thing(stack):
    task_id = stack.submit("do the thing")
    task = stack.probe.wait_for_task(status="completed", task_id=task_id)
    assert task["status"] == "completed", stack.diagnostics(task)
```

**`@pytest.mark.script` is what the model answers**, turn by turn. Omit it and the fixture installs `DEFAULT_SCRIPT`, one plain answer — right for a scenario that only needs a task to run to completion. For a script that depends on something invented at run time, call `stack.script(...)` inside the test instead of declaring it on the marker.

**Filter `wait_for_task` on something that identifies your task** — `task_id`, or `conversation_token`, or `id_above`. Not on `user_id` alone: the scheduler queues its own work for the same user at startup, which is how the first smoke tests came back asserting against a `scheduled` row nobody wrote. `wait_for_task` also watches every terminal status alongside the one you asked for, so waiting for `completed` on a task that already failed returns the failure immediately rather than spending the whole timeout and then reporting "nothing reached completed", which says nothing about why.

**A negative assertion needs `stack.mark`**, the watermark `reset` returned, *and* a discriminating column — see the third rule above.

`@pytest.mark.profile("full", fresh=True)` buys a private stack torn down at test end, for anything asserting on start-up behaviour. Note what that costs on the full shape: a fresh six-container boot per test. Where a whole file shares one start-up stack, take a module-scoped fixture calling `stacks.get(profiles.FULL, fresh=True)` instead — `tests/full/test_provisioning.py` is the worked example.

### When a stack fails

`stack.diagnostics(task)` is the one call worth reaching for first. It assembles the task row, the daemon log and every service's own `describe()`, because a scenario that prints only the task row reports "the task failed" identically for a denied wrapper, a token that never arrived and a stub that answered 501. Pass it as the assertion message rather than calling it after the fact. `stack.logs(tail=...)` gets the daemon log alone.

Three failures are harness conditions rather than code defects, and each says so:

- A bare `pytest.fail(..., pytrace=False)` out of the `stack` fixture means the reset could not quiesce. The line worth reading is the task-id list.
- A `TimeoutError` out of `Stack.script` counts barrier refusals and turns served before the scenario submitted anything. It is the tier's most confusing failure and its most informative message.
- A `StackError` naming a conflicting process variable means your shell exports something that outranks the env-file — an exported `ISTOTA_BRAIN_KIND` would run the tier against the real API. Read it as "the scrub does not cover this one", not as a `.env` to go and clean up.

Every session prints two diagnostic lines of its own: how many `docker compose exec` calls the probe made, and how long each profile waited on which service to come up.

**There is no way to keep a lean stack alive for inspection.** `ISTOTA_TESTBED_KEEP` is full-shape only, keeps volumes rather than containers, and `tests/full/` refuses to run under it. Lean project names are random, and the session sweep reaps them.

### Prompt goldens

`tests/test_prompt_golden.py` runs in the default suite against no container and no model. `execute_task(..., dry_run=True)` returns **both halves** of the assembled prompt as the second element of its four-tuple, behind a `[DRY RUN] Would execute with prompts:` line the test strips, rendered by `executor.render_composed_prompt` under fixed `===== SYSTEM =====` and `===== USER =====` labels — emissaries, persona, channel guidelines, the storage vocabulary, eager skill bodies, the on-demand menu and the rules block in the system half, conversation context, memory and the request in the user half — and each case in `CASES` snapshots the labelled pair into `tests/golden/prompts/`. One file per case rather than twenty-six: separate files would double the matrix and make text lost from one half or duplicated into both harder to see. The point is the failure substring assertions decay away from: a layer that silently stops being included, and now also a layer that quietly changes which half it is in.

A diff is a failure. An intentional change is a reviewed golden update:

```bash
uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_prompt_golden.py -n0
```

`env` goes *inside* the `uv run` rather than in front of it as a shell assignment, and on a deployment with a devbox that is the difference between rewriting and not. `uv` is in `DEFAULT_SHIM_COMMANDS`, so there it is a shim handing the argv to the exec server in the container, and `devbox_exec_protocol` carries no `env` field — deliberately, and pinned by a test — so nothing set in the calling shell arrives. The run compares instead of rewriting, and every golden the change touched comes back red with nothing to say the switch was never seen. In the argv the assignment survives, and on a host with no devbox the two forms are the same command. `tests/support/env_isolation.py` ate this same variable in-process until it was named in the keep-list, with the same symptom.

Commit the resulting diff and review it like any other change. `-n0` is not optional: the orphan check has no ordering relationship with the writers under xdist, so a regeneration that adds or renames a case reports missing goldens from the run that was supposed to create them. The variable is parsed by an `updating()` helper that takes the same affirmative and negative words as `PRECOMMIT_SCANS_REQUIRED` and raises on anything else, so a stale `ISTOTA_UPDATE_GOLDEN=0` in a shell cannot quietly turn every golden into a rubber stamp.

**`dry_run` returns after assembly rather than instead of it**, so everything assembly calls is live. The first version of this module opened two real HTTPS connections per Nextcloud-backed case while its own header said it ran against nothing — `read_user_memory_v2` returning None led to `ensure_user_directories_v2` and an OCS share POST. A golden path that reaches a network socket is a golden that lies about running against nothing, and the fix is to turn the live path off through configuration rather than to mock it. An autouse `_no_sockets` fixture records the attempt, refuses it, and asserts at teardown; recording rather than only raising, because every caller on that path swallows exceptions for graceful degradation, so a guard that merely raised would be caught and the property would revert to a claim nobody checks.

Two product gaps are held here by named tests rather than fixed, so a fix arrives as a reviewed golden diff and turns them red instead of passing silently. `format_cli_skills` applies neither the capability gate nor the effective disabled set, so a Nextcloud-free install is told `istota-skill nextcloud` exists in the same prompt that omits the skill from the on-demand menu. And `custom_system_prompt` cannot change the assembled prompt at all, because it is read at brain-request assembly well past the `dry_run` return — not a defect, since that is the brain's system prompt rather than the task prompt, but the identity is asserted so that a change routing it into the task prompt is visible.

### The upgrade tier's two anchors

`scripts/test-upgrade.sh` boots the current image over an older release's `config.toml` and database. It exists because the auto-update cron resets to main every two minutes without running Ansible, so an Ansible deployment can run new code against a `config.toml` a month old. Every other tier renders a fresh config, and a fresh config is current by definition. The Docker shape used to be the second case — a rebuild over a retained volume kept the config the entrypoint wrote on that volume's first boot — and since ISSUE-368 it re-renders on every boot, so its `volume` shape now tests a *database* older than the code rather than a config as well. That narrows what the tier is for on that shape; it does not remove it.

- **Near anchor**, the default: the merge-base with the default branch. That is about three days at the current release cadence — close to a no-op as a regression detector on its own, but it is the span the auto-update cron actually crosses, and it is cheap.
- **Far anchor**: the tag in `scripts/upgrade-floor`, roughly a month back. That file is the statement of how far back an upgrade is supported. Bump it deliberately, and never to make a red run green without reading why it went red.

```bash
scripts/test-upgrade.sh                                 # near anchor, code shape
scripts/test-upgrade.sh --from-floor --shape volume     # far anchor, before a release
scripts/test-upgrade.sh --shape both
scripts/test-upgrade.sh --from v0.38.0 --shape volume   # reproduce a specific report
```

The assertion is not "reproduces ISSUE-263". `resolve_real_bin`'s fallback to the image's own binary directory is what makes such an upgrade clean, so that criterion is unreachable against current code — the fix working, not a gap in the test. What is asserted is that no check fails in either shape, and that `developer.forge_config_drift` reports `WARN` naming both the stale configured path and the resolved one on the retained-volume shape. That warning is the signal ISSUE-263 never had.

## The frontend suite

`web/` is checked independently of Python — a change touching only one half need
only run that half:

```bash
npm --prefix web run lint:design   # design-language lint (raw colours, tokens)
npm --prefix web run check         # svelte-check
npm --prefix web run test          # vitest run
npm --prefix web run format:check  # prettier
```

Needs `npm ci` in `web/` first. There is no wrapper script over the two halves —
`.claude/verify.sh` was tried and removed, because scoping and runner fallback
inside a wrapper hid which runner produced a failure. Run the commands directly;
the full set is listed in `AGENTS.md`.

## Test patterns

**Real SQLite via `tmp_path`**: No database mocking. Tests create real SQLite databases initialized from `schema.sql`. This catches actual SQL issues that mocks would hide.

**`unittest.mock` for external dependencies**: HTTP calls, subprocess invocations, and file system operations outside the test directory are mocked. One seam is the exception and has its own double, because a mock there accepts arguments the real service refuses — see "Room shapes, and the Talk double" below.

**Class-based tests**: Tests are organized in classes grouping related scenarios.

### Room shapes, and the Talk double

A room is one conversation bound to several surfaces, and it borrows its canonical identity from the surface it was created on. On an ordinary Talk room `rooms.token` and the `talk` binding's `surface_ref` are the same string. On a room created in web chat and later promoted to Talk they are different, and that difference is the only thing separating a delivery that resolved the binding from one that handed the canonical token to the Talk API and got a 404. That was ISSUE-400, and no test in the tree could see it, because every test built the first shape and the `MagicMock` standing in for the Talk client accepted any string.

Two builders in `tests/support/rooms.py` write what the real producers write — `record_inbound`'s room branch and `web_app._chat_promote_to_talk` — pinned by `tests/test_support_rooms.py` diffing every table a builder writes. That includes the `room_members` row, which arrives from `db.register_room` rather than from a call in the builder: a room with no member row is invisible to `db.list_member_rooms`, so a test asserting through the web room listing would fail for a reason having nothing to do with what it was testing.

```python
from .support.rooms import plain_talk_room, promoted_room

room = plain_talk_room(conn, "testuser")     # canonical == talk_ref
room = promoted_room(conn, "testuser")       # canonical is 'web-…', talk_ref is not
```

Both return a `RoomShape` carrying `canonical`, `talk_ref`, `origin`, `name` and a `diverges` property. **Which one to use is decided by what the test claims**, not by preference:

- **Parametrize over both** where the test asserts *where* something landed. That is the set that gains coverage: the promoted case fails on a misroute, and the plain case is what says an ISSUE-400-shaped failure is shape-specific rather than a test that breaks under any change. A mutation that misroutes on every shape reddens both, which is the answer you want, not a broken control.
- **A plain room** where the test is about content or ordering and makes no destination choice. It costs nothing and picks up the double's guard for free.
- **A promoted room alone** where the shape is what makes the scenario exist — a mirror leg only exists on a room bound to two surfaces, so there is no plain case to parametrize over.

A third shape neither builder makes: a Talk-origin room a user later also opens in web chat, so it carries a web binding while `canonical == talk_ref`. Add it when something needs it rather than faking it locally.

The `fake_talk` fixture puts `tests/support/talk_double.py`'s `FakeTalkClient` behind every `get_talk_client` binding: both module-level ones (`transport/talk/__init__.py` and `transport/talk/inbound.py` — patching only the first leaves the whole poller on the real factory) and the definition site in `async_runtime`, which is what reaches the one remaining function-local importer (`commands`' `!search`), since a function-local import resolves the name at call time. `web_app._delete_from_talk` was the second until ISSUE-407 and now builds both its clients itself. It is not autouse, it reads the `db_path` fixture's database, and it clears the two process-lifetime caches sitting in front of the seams. One rule on every method taking a token: accept it if it is a live `talk` `surface_ref` in `room_bindings` or if it is in `known_channels`, otherwise raise `UnknownTalkRoom` and record the attempt with `refused=True`.

**Assert on `calls`, never on the absence of a raise.** The product swallows: `TalkTransport.deliver` catches and returns None, `scheduler.edit_talk_message` catches and returns False, `inbound._post_ack` catches and logs. A refusal is swallowed exactly as a real 404 would be, so a test that only reaches the end of the function proves nothing. Pair every count with `refusals == []` as well, because a post refused for naming the canonical token reads exactly like a post correctly suppressed.

Two escape hatches, and they are not interchangeable:

- **`known_channels` is data and needs no justification.** Plenty of tokens are legitimately unbound and are ordinary product behaviour: `alerts_channel`, `log_channel`, the first briefing token, `default_destination`, an auto-detected 1:1 DM, and a room `provision_rooms.py` created that the Talk poller has not yet seen. Name the channel and move on.
- **`strict=False` needs a stated reason on the line.** It accepts everything, so it removes the guard for that test rather than widening it, and under it `refused` is False on every row and the `refusals` list says nothing at all. It is for the genuinely unmodellable case — `talk_channel_for_task` rung 3 can hand back an email thread hash that names nothing anywhere. As of the delivery conversion nothing outside the double's own test file uses it, which is the state to keep: a routine opt-out is not a guard.

**The web process needs the second fixture, `fake_talk_web`.** `web_app.py` constructs `TalkClient(...)` directly in eight places — the promote path that creates the promoted shape, the post-as-user mirror, the read push and pull, the rename propagation, the message delete's two legs and the liveness probe — and most take a per-user OAuth bearer token, so there is no factory to patch and the class itself is the seam. `fake_talk_web` depends on `fake_talk` and additionally replaces `istota.talk.TalkClient`, so one double stands behind both; a web test using `fake_talk` alone still reaches the real client. Two things the test still does itself: point `web_app._config` at a config on the same `db_path`, and store a token for the user, since `web_tokens.feature_enabled` gates every one of those paths before a client is built.

Every construction returns **the same instance**, which is what lets `calls` span an attempt and its retry. `constructions` is the history (`bearer_token`, `timeout`), `TalkCall.bearer_token` is what pins one call to one construction, and `created_tokens` holds what `create_conversation` minted — bound to nothing, deliberately, so `_chat_promote_to_talk`'s `add_participant` and seed post are refused unless the product wrote the binding first.

**A bearer client can fail two ways, and the double keeps them apart.** `_post_as_user` and `_mark_read_as_user` each force-refresh the token once on a **401** and retry, so a double whose only unhappy answer was `UnknownTalkRoom` would turn a stale credential into a misroute and delete that coverage without failing anything. `bearer_rejections` maps a credential to the status the server answers with; the answer raises `httpx.HTTPStatusError` and is recorded as `TalkCall.status`, never as `refused`, so `refusals == []` goes on meaning "nothing that reached the room check was misrouted". Read that qualifier literally: the credential is checked first, as Nextcloud does it, so a call that was both stale-credentialled and misrouted never reaches the room check and leaves `refusals` empty. A test setting `bearer_rejections` pairs `refusals == []` with `auth_failures`; the ~30 existing assertions elsewhere are unaffected, since membership is what is tested and no other test registers a key for the bot.

**The `None` key is the bot's basic auth** (ISSUE-407), not "no credential". The bot carries one and Nextcloud refuses it exactly as it refuses the user's, which is what makes two product branches expressible: `_delete_from_talk`'s "both legs refused and nothing went wrong" — distinct from "a leg errored", which is the state ISSUE-407 was reporting as the first — and `_talk_conversation_verdict`'s `bot_removed`. Its sibling `gone` is still not drivable, and the blocker is the shared-instance hazard below rather than the 404: the user client built *inside* the verdict leaves its bearer on the one instance, the outer bot client picks it up, and refusing that token then refuses the `create_conversation` the verdict was meant to authorize.

**One coroutine at a time.** Every construction returns the same instance, so `bearer_token` is a field two in-flight calls would share: `constructions` would interleave and a call would record whichever construction ran last. Nothing hits that today, because these tests drive the coroutines directly rather than through an endpoint — but `chat_read_all_rooms` fires one `_push_read_to_talk` per moved room, so an endpoint-driven test would, and it wants a per-construction facade over a shared ledger before it can read `constructions` positionally.

```python
async def test_a_401_forces_a_refresh_and_retries_once(fake_talk_web, room, ...):
    fake_talk_web.bearer_rejections["stale-at"] = 401
    ...
    assert [c.bearer_token for c in fake_talk_web.constructions] == ["stale-at", "fresh-at"]
    assert fake_talk_web.refusals == []          # a stale token is not a misroute
```

`tests/test_web_talk_seams.py` is the file that drives these. **The fixture reaches all seven sites; five are driven end to end and two are not**, which is the honest form of the claim:

- **The rename propagation** lives inside the `PATCH /chat/rooms/{id}` handler, so reaching it needs an ASGI-driven test rather than a direct call. It resolves its token through `_room_talk_binding`, which is exactly the canonical-vs-`surface_ref` question, so it is worth adding.
- **`_talk_conversation_seen_by_user`** is reachable only through `_talk_conversation_verdict`'s **bot** 404, and the double's second failure mode is keyed on the bearer token, which the bot has none of. An unknown token there raises `UnknownTalkRoom`, lands in the generic handler and reads as `unknown`, so the `gone` / `bot_removed` verdicts and the whole rebind branch cannot be driven. Expressing it wants a token-keyed rejection map beside the bearer-keyed one, and the one-coroutine constraint above has to be lifted first, because the promote path holds a bot client across the call that would build a user one.

Also still uncovered: nothing puts the double behind a `!search` through `commands` — the `async_runtime` patch *reaches* it, but `search_messages` is on no seam and not on the double, so such a call gets an `AttributeError` — and the double still accepts a dead binding (ISSUE-401), by the same rule that makes it accept a live one.

One product defect the instrument found, filed as ISSUE-403 and since fixed: `_delete_from_talk` built its user-scoped client inline as an argument and never closed it, alone among the sites of the time, so every web message delete in a Talk-bound room leaked the `httpx` pool behind it. Its neighbour was worse and became ISSUE-407: the bot leg took the runtime's singleton from a coroutine `_fire_and_forget` had scheduled on uvicorn's loop, so in the web process the DELETE went out and the answer could never be read. Both legs now build and close their own client, and the tests assert construction and close counts together on all three of `_attempt`'s exits.

## Shared fixtures (`conftest.py`)

| Fixture | Purpose |
|---|---|
| `db_path` | Initialized SQLite database from schema.sql |
| `db_conn` | Database connection |
| `make_task` | Factory for creating test tasks |
| `make_config` | Factory for creating Config objects |
| `make_user_config` | Factory for creating UserConfig objects |
| `fake_talk` | A Talk client that refuses a token Nextcloud would refuse — see "Room shapes, and the Talk double" above |
| `fake_talk_web` | The same double, additionally behind `web_app`'s own `TalkClient(...)` constructions |

Three autouse fixtures apply to every test whether you ask for them or not: `_no_network_symbol_lookups` (fails a test that tries to resolve a ticker symbol over the network), `_reset_async_runtime_singletons` (drops the persistent asyncio loop and pooled HTTP client between tests), and `_reset_expunge_warning_latch` (clears the once-per-process IMAP expunge warning).

## Testing skills

Skill loader tests require isolation from bundled skills:

```python
# Pass bundled_dir to isolate from bundled skills
index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
```

Executor tests set `bundled_skills_dir` on the Config object to an empty directory to isolate from bundled skills.

## TDD workflow

For new features:

1. Read existing codebase structure and test patterns
2. Write failing tests covering happy path, edge cases, and error handling
3. Run tests to confirm they fail
4. Implement the feature
5. Run tests and iterate until all pass
6. Run `ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts`, plus the `web/` checks above if the change touched the frontend
7. Commit
