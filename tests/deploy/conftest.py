"""Converge the Ansible role on a real systemd host, once per session.

The bare-metal path is the only canonical deployment shape — `AGENTS.md` says
so, because it is the only one where the sandbox actually works — and until
ISSUE-439 nothing in the repository ever ran `ansible-playbook`. The fourteen
`tests/test_ansible_*.py` files parse the role's YAML and assert on the parse,
which cannot see a unit that fails to start, a template that renders a config
the loader rejects, or a task ordering that only breaks when the tasks run.

This fixture boots `docker/test/Dockerfile.deploy` with systemd as PID 1, hands
it the real `deploy/install.sh`, and lets it converge. `istota doctor` is then
the oracle, exactly as it is for the `image` and `smoke` tiers — the point of
reusing it is that hand-written assertions about a deployment drift from the
code that builds one, and doctor does not.

## What the tier drives, and what it deliberately does not

It drives the **entry point an operator uses**: `deploy/install.sh --headless`,
which apt-installs python3/pipx/ansible-core, runs `settings_to_vars.py` over a
`settings.toml`, and applies `deploy/local-playbook.yml` to localhost. Nothing
here reimplements any of that — a driver that called `ansible-playbook` itself
would stop covering the installer, which is most of what a bare-metal install
*is*.

Four things are turned off, and each is a concession with a reason rather than
a convenience:

* **rclone** (`configure_rclone = false`). The mount is a real FUSE mount
  against a real Nextcloud; there is neither here.
* **zram** (`zram_enabled = false`). There is no `/dev/zram0` in a container
  for `systemd-zram-setup@zram0` to bring up. Reaching this switch is why
  ISSUE-439 also had to map it in `settings_to_vars.py` — it was documented in
  `defaults/main.yml` and unreachable from the only input `install.sh` takes.
* **Talk** (`talk.enabled = false`) and the Nextcloud URL (empty), so the
  deployment renders `storage_backend = "local"` and needs no server.
* **the web UI** (`web.enabled = false`), which would add `npm ci` and
  `npm run build` — minutes, and the SvelteKit build is already the subject of
  `tests/test_ansible_web_build.py` and of the `image` tier.

Everything else converges for real: the apt work, the user and group, the
directory tree, the git clone, `uv sync --extra all`, the rendered
`config.toml`, the tmpfiles snippets, logrotate, journald, the sandbox sysctl,
and the systemd units.

**Stated up front, so the tier is not read as covering the deploy end to end**:
reboot ordering and the `Require`/`After` relationship between the app units
and the rclone mount unit; a real FUSE mount; and anything about the host's own
kernel, which a container shares rather than owns. Those stay production-only.

## The source the role clones from

The role clones `istota_repo_url`, and the tier must not have it clone GitHub —
that would converge `main` and report nothing about the checkout under test. So
the session makes a bare clone of the working tree into a temp directory,
copies it into the container, and points `repo_url` at it.

Two details there are load-bearing and both were found the hard way:

* the clone is **copied in, not bind-mounted**, and `deploy/` with it. A bound
  worktree carries a `.git` *file* naming a gitdir on the host that does not
  exist in the container, and git's repository discovery walks up into it from
  whatever cwd the Ansible git module inherits — which fails with
  `not a git repository: /Users/...`, a message about the host filesystem
  appearing in the middle of a container converge.
* the copy is **chowned to root**, because `docker cp` preserves the host uid
  and git then refuses the repository as `dubious ownership` and reports it as
  `Could not read from remote repository`, which reads like a network problem.

**Two revisions are in play at once, and the asymmetry is deliberate.**
`deploy/` is copied from the *working tree*, uncommitted edits included, because
`deploy/` is the thing under test and a tier that made you commit before it
would tell you about your last commit rather than about your change. The product
source arrives by `git clone --bare`, so `src/` is *committed HEAD*. The
consequence to know: an uncommitted change under `src/` is not in this tier's
converge, and a role change that depends on one will fail here and pass on a
host. Commit the pair before reading a red run as a role defect.

A detached HEAD is refused outright rather than accommodated — see `_bare_clone`
for why the alternative is a tier that silently converges the default branch.

## Session scope, and why nothing resets

One converge per session. There is no `reset` here and no per-test isolation,
because unlike the smoke tier this one asserts about a *converge* rather than
about tasks a daemon ran: the assertions are all reads of state the converge
left behind.

The one mutation any test performs is the idempotence check, and it calls
`reapply_role()` rather than `converge()`. The difference is which half of the
bare-metal path gets re-run: `converge()` is the whole installer, bootstrap
included, and that bootstrap re-downloads three Ansible collections from
galaxy.ansible.com on every invocation, which made the assertion fail on a
truncated fetch — a network flake reported as a role defect. `reapply_role()`
re-applies the playbook against the vars file the first converge already
produced. `converge()` stays as the escape hatch for a test that genuinely
wants the installer twice; nothing in the tier uses it today.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from ..conftest import REPO, _require_no_xdist, require_docker, resolve_platform
from ..image.conftest import _tag_for

DEPLOY_DOCKERFILE = REPO / "docker" / "test" / "Dockerfile.deploy"

#: Where the tier puts things inside the container. `/opt/istota-src` is
#: deliberately outside `{istota_home}` (`/srv/app/istota`), so nothing the role
#: writes and nothing the daemon reads can be confused with the tier's own
#: scaffolding.
CONTAINER_DEPLOY_DIR = "/opt/istota-src/deploy"
CONTAINER_CLONE = "/opt/istota-src/istota.git"
CONTAINER_SETTINGS = "/etc/istota/settings.toml"

#: The installer apt-installs ansible-core through pipx, which lands in
#: `/root/.local/bin` and is not on a non-login shell's PATH.
INSTALLER_PATH = "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

BUILD_TIMEOUT = 1800
BOOT_TIMEOUT = 60
#: A cold converge does an apt update, a pipx install of ansible-core, a git
#: clone and a `uv sync --extra all` (which resolves torch). Thirty minutes is
#: slack over the ~2 minutes the converge itself took on a warm cache, not a
#: target: a cold uv cache pays for torch, and the whole point of a generous
#: ceiling here is that a slow first run must not surface as a broken role.
CONVERGE_TIMEOUT = 1800
EXEC_TIMEOUT = 300

#: The damage `scripts/test-deploy-negative-control.sh` asks for, and the only
#: values honoured. Every assertion in this tier is a claim about a container,
#: and nothing inside the tier can tell a working assertion from one that
#: matches nothing — the project's rule for a tier asserting against an
#: artifact (`.claude/rules/testing.md`) is that the control has to be run and
#: what it turned red written down.
#:
#: The damage is applied to the *deployed artifact inside the container* rather
#: than to a build input, which is why it is a variable here instead of a
#: second Dockerfile the way the image tier does it: what is being broken is a
#: template the role renders and a flag the runner passes, neither of which is
#: in an image at all.
#:
#: An unrecognised value is refused rather than ignored. A control that
#: silently changed nothing would report the tier as unable to fail while
#: proving it can, which is the inversion this whole mechanism exists to
#: prevent.
BREAKAGES = {
    # ExecStart at a module that does not exist. Deliberately the *module* and
    # not the interpreter: `Type=simple` means `systemctl start` returns as soon
    # as the process is forked, so a bad module leaves the converge succeeding
    # and the unit crash-looping under `Restart=always` — which is the state
    # these assertions have to be able to see. A bad interpreter path fails the
    # role's own start task instead, and the tier would then report fixture
    # *errors* rather than the named failures a control has to name.
    "unit": (
        "sed -i 's|-m {{ istota_package }}.scheduler|-m istota.no_such_module|' "
        f"{CONTAINER_DEPLOY_DIR}/ansible/templates/istota-scheduler.service.j2"
    ),
    # A key with no value: renders, and is not TOML.
    "config": (
        "sed -i '4i broken_key_with_no_value =' "
        f"{CONTAINER_DEPLOY_DIR}/ansible/templates/config.toml.j2"
    ),
    # Handled in `systemd_host` rather than here: it is a `docker run` flag,
    # not a file. Named in this map so the script and the fixture cannot
    # disagree about which breakages exist.
    "sandbox": None,
}

_XDIST_MESSAGE = (
    "the deploy tier must run with -n0. The converge is a session-scoped "
    "fixture, so N workers would each boot a systemd container and race to "
    "converge the role inside it."
)


def _run(argv: list[str], timeout: int = EXEC_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


#: Every container this tier makes starts with it, which is what lets the sweep
#: below find leftovers without touching anything else a developer is running.
CONTAINER_PREFIX = "istota-deploy-"


def _sweep_leftover_containers() -> None:
    """Reclaim containers an earlier run was killed before tearing down.

    A unique name per session stops one run from adopting another's container,
    and it also means nothing ever reclaims them — `tests/conftest.py`'s
    `_sweep_leftover_stacks` exists for exactly this reason on the compose
    tiers. It matters more here: what leaks is a booted Debian container
    holding `CAP_SYS_ADMIN`, `--cgroupns=host` and a read-write bind of the
    host's own `/sys/fs/cgroup`, and it stays running for ever. `qtest` reports
    `KILLED-SIGKILL` often enough for this to be a real state, and
    `ISTOTA_DEPLOY_TIER_KEEP` is a second, deliberate producer of the same
    litter.

    Scoped by name prefix and anchored, so it can never reach a container a
    developer named something else. Best-effort: a sweep that cannot run is not
    a reason to fail a tier that has not started.
    """
    listed = _run([
        "docker", "ps", "-aq", "--filter", f"name=^{CONTAINER_PREFIX}",
    ], timeout=60)
    if listed.returncode != 0:
        return
    leftovers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if leftovers:
        print(
            f"\nreclaiming {len(leftovers)} leftover deploy container(s) "
            "from an earlier run"
        )
        _run(["docker", "rm", "-f", *leftovers], timeout=180)


def requested_breakage() -> str:
    """Which deliberate damage the negative control asked for, if any."""
    requested = os.environ.get("ISTOTA_DEPLOY_TIER_BREAK", "").strip()
    if not requested:
        return ""
    if requested not in BREAKAGES:
        raise pytest.UsageError(
            f"ISTOTA_DEPLOY_TIER_BREAK={requested!r} is not a breakage. "
            f"Known: {sorted(BREAKAGES)}"
        )
    return requested


@dataclass
class ConvergedHost:
    """A running systemd container the role has been applied to."""

    container: str
    settings: dict
    converge_output: str
    #: Memoized `doctor --json`. Five tests ask for it and each call is a
    #: `docker exec` that imports the package and probes half a dozen binaries.
    #: Invalidated by `converge`, since a second converge can change the answer.
    _doctor: list[dict] | None = None
    #: Memoized `home`, which is two `docker exec`s away and never changes for
    #: the life of a container.
    _home: str | None = None

    # -- reading the host ---------------------------------------------------

    def exec(self, command: str, user: str | None = None, timeout: int = EXEC_TIMEOUT):
        """Run a shell command inside the container.

        `PATH` is set explicitly rather than inherited: `docker exec` gets the
        image's environment, not a login shell's, so pipx's `/root/.local/bin`
        is absent and `ansible-playbook` is not found.
        """
        argv = ["docker", "exec"]
        if user:
            argv += ["-u", user]
        argv += [self.container, "bash", "-c", f"export PATH={INSTALLER_PATH}; {command}"]
        return _run(argv, timeout=timeout)

    def unit_property(self, unit: str, prop: str) -> str:
        result = self.exec(f"systemctl show {unit} -p {prop} --value")
        return result.stdout.strip()

    def failed_units(self) -> list[str]:
        result = self.exec("systemctl list-units --state=failed --no-legend --plain")
        return [
            line.split()[0]
            for line in result.stdout.splitlines()
            if line.strip()
        ]

    def journal(self, unit: str, lines: int = 80) -> str:
        return self.exec(
            f"journalctl -u {unit} -n {lines} --no-pager"
        ).stdout

    def read_file(self, path: str) -> str:
        return self.exec(f"cat {path}").stdout

    def path_exists(self, path: str) -> bool:
        return self.exec(f"test -e {path}").returncode == 0

    # -- the oracle ---------------------------------------------------------

    def doctor(self) -> list[dict]:
        """`istota doctor --json`, run **with the daemon's own environment**.

        This is the part that is easy to get wrong and quietly meaningless when
        you do. Doctor answers questions about the process it is running in —
        `security.skill_proxy` asks whether `istota-skill` is on *this* PATH,
        `security.secret_key` reads *this* environment — so running it from a
        bare `docker exec` reports on the shell rather than on the deployment,
        and the first version of this tier collected two FAILs that were both
        artifacts of that (`istota-skill` is in the venv's `bin` and the unit's
        `PATH` names it; `ISTOTA_SECRET_KEY` comes from the unit's
        `EnvironmentFile`). `doctor.py`'s own `_non_daemon_env_markers` exists
        for the same reason from the other side.

        So the environment is reconstructed from the unit itself — its
        `Environment=` assignments and its `EnvironmentFile=` — rather than
        approximated here, and every value comes from `systemctl show` so a
        change to the unit template moves this with it.
        """
        if self._doctor is not None:
            return self._doctor

        # `EnvironmentFiles`, plural. `EnvironmentFile` is the spelling in the
        # unit *file* and `systemctl show` answers it with an empty string
        # rather than an error — which is the failure this method's whole
        # docstring is about, arriving through the fix for it: the environment
        # silently lost `secrets.env`, and doctor reported the deployment as
        # holding no model credential when it holds one. The assertion below is
        # what stops that being a quiet wrong answer a third time.
        #
        # Rendered as `path (ignore_errors=no)` per entry, several entries
        # space-separated, so the paths are the tokens that are not a
        # parenthesised flag.
        raw_files = self.unit_property("istota-scheduler", "EnvironmentFiles")
        env_paths = [
            token for token in raw_files.split() if not token.startswith("(")
        ]
        assignments = self.unit_property("istota-scheduler", "Environment")

        assert env_paths and assignments, (
            "could not reconstruct the daemon's environment from "
            "istota-scheduler: doctor would then be reporting on this shell "
            f"instead of on the deployment.\n  EnvironmentFiles={raw_files!r}\n"
            f"  Environment={assignments!r}"
        )

        # Quoted rather than spliced raw. Every value here comes from the
        # role's own templates today, so this is a latent shape rather than a
        # live defect — but the string is about to become a `bash -c` argument,
        # and a value carrying `;` or `$(` would be executed rather than
        # assigned. An env-file path containing whitespace would split into two
        # tokens and `. /part1` would fail quietly inside `set -a`, so it is
        # refused instead: an unreadable environment must not become a doctor
        # run that reports on the shell.
        for path in env_paths:
            assert path.startswith("/") and not any(c.isspace() for c in path), (
                f"unusable EnvironmentFile path from systemctl: {path!r}"
            )
        source = "".join(f"set -a; . {shlex.quote(path)}; set +a; " for path in env_paths)
        exports = " ".join(shlex.quote(part) for part in assignments.split())
        config = f"{self.home}/istota/config/config.toml"
        command = (
            f"{source}{exports} "
            f"{self.home}/.venv/bin/python -m istota.cli -c {config} doctor --json"
        )
        result = self.exec(command, user=self.settings.get("_user", "istota"))
        # doctor exits 1 when anything failed, which is a verdict rather than a
        # crash — the payload is on stdout either way. A missing payload is the
        # crash, and it must not be swallowed into an empty check list.
        if not result.stdout.strip():
            raise AssertionError(
                "istota doctor produced no JSON.\n"
                f"exit={result.returncode}\nstderr:\n{result.stderr[-4000:]}"
            )
        self._doctor = json.loads(result.stdout)
        return self._doctor

    @property
    def home(self) -> str:
        """Read back from the unit, not hardcoded.

        `/srv/app/istota` is the role's `istota_home` default and the settings
        file does not set one, so writing the literal here would make a change
        to that default surface as half this tier failing on missing files
        rather than on the default. `WorkingDirectory` is `{{ istota_repo_dir }}`
        — the checkout *inside* the home — so the home is its parent.
        """
        if self._home is None:
            workdir = self.unit_property("istota-scheduler", "WorkingDirectory")
            assert workdir.startswith("/"), (
                f"istota-scheduler reports no usable WorkingDirectory: {workdir!r}"
            )
            self._home = str(PurePosixPath(workdir).parent)
        return self._home

    # -- driving another converge -------------------------------------------

    def converge(self) -> subprocess.CompletedProcess:
        """The whole installer again, bootstrap included.

        Takes no overrides deliberately. It used to accept an `extra_settings`
        dict that was merged into `self.settings` and then dropped on the floor,
        because `_settings_toml` reads a fixed set of keys and ignores the rest
        — so a caller passing `{"web_enabled": True}` would converge an
        unchanged deployment and pass green. A parameter that silently does
        nothing is worse than no parameter; add a key to `_settings_toml` when
        a test needs one.
        """
        self._doctor = None
        return _converge(self.container, self.settings)

    def reapply_role(self) -> subprocess.CompletedProcess:
        """Re-apply the playbook alone, against the vars `install.sh` wrote.

        **Idempotence is a property of the role, and this is the half that has
        it.** Re-running the whole installer also re-runs its bootstrap, and
        `ensure_collections` there is `ansible-galaxy collection install
        --force-with-deps`, which re-downloads three collections from
        galaxy.ansible.com on every invocation. That made the idempotence
        assertion fail on a truncated download — a network flake reported as a
        role defect, which is the worst kind of red because the next run is
        green and nothing was learned.

        The installer is still covered end to end: `converged_host` drives it
        in full, once, and that is the run every other assertion in the tier
        reads. What is deliberately *not* covered is `install.sh --update`'s own
        re-run behaviour, which needs the network by construction.

        `/etc/istota/vars.yml` is the file the first converge produced through
        the real `settings_to_vars.py`, so this re-applies exactly what the
        installer would, minus the bootstrap.
        """
        self._doctor = None
        return self.exec(
            f"cd /opt/istota-src && ansible-playbook {CONTAINER_DEPLOY_DIR}/local-playbook.yml "
            "--connection local --inventory localhost, "
            "--extra-vars @/etc/istota/vars.yml",
            timeout=CONVERGE_TIMEOUT,
        )


def _toml_string(value: str) -> str:
    """A TOML basic string, escaped.

    Every value here is fixture-controlled except `repo_branch`, which is
    whatever `git` reports — and while git refuses `"` and `\\` in a ref name,
    that is a fact about git rather than about this function, and the next
    value interpolated in may not come from git at all. Escaping is two lines;
    reasoning about which callers are safe is not.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _settings_toml(settings: dict) -> str:
    """The tier's `settings.toml`, written the way an operator would.

    Deliberately a real settings file rather than a vars file: `install.sh`
    reads TOML and runs `settings_to_vars.py` over it, so writing vars directly
    would skip the converter — which is a shipped part of the bare-metal path
    and the thing ISSUE-439's own fix landed in.
    """
    lines = [
        'namespace = "istota"',
        'bot_name = "Istota"',
        f"repo_url = {_toml_string(settings['repo_url'])}",
        f"repo_branch = {_toml_string(settings['repo_branch'])}",
        'repo_tag = ""',
        "configure_rclone = false",
        "use_nextcloud_mount = false",
        'nextcloud_url = ""',
        f"secret_key = {_toml_string(settings['secret_key'])}",
        # A placeholder, and the reason it is set at all is
        # `security.skill_model_credential.value`: that check asks only whether
        # the daemon *holds* one of three names, because `code_review` spawns a
        # model of its own and cannot authenticate without one. A harness that
        # sets no credential anywhere makes the check fail on every run and the
        # tier then has to exempt it — which is how the image tier came to have
        # a permanently red assertion nobody had looked at (#434). `AGENTS.md`
        # is explicit that a check is only an oracle for a test if the test
        # names the environment that makes it run, so the tier deploys a
        # complete host instead. Nothing here ever calls a model.
        f"claude_oauth_token = {_toml_string(settings['claude_oauth_token'])}",
        'admin_users = ["istota"]',
        # The two ISSUE-439 mapped. Without them the converge dies on
        # `dev-zram0.swap` with no way to say otherwise through install.sh.
        "zram_enabled = false",
        "swapfile_enabled = false",
        "",
        "[talk]",
        "enabled = false",
        "",
        "[web]",
        "enabled = false",
    ]
    return "\n".join(lines) + "\n"


def _converge(container: str, settings: dict) -> subprocess.CompletedProcess:
    """Write the settings file and run the real installer."""
    with tempfile.TemporaryDirectory() as staging:
        settings_path = Path(staging) / "settings.toml"
        settings_path.write_text(_settings_toml(settings))
        _run(["docker", "exec", container, "mkdir", "-p", "/etc/istota"])
        copied = _run(
            ["docker", "cp", str(settings_path), f"{container}:{CONTAINER_SETTINGS}"]
        )
        assert copied.returncode == 0, copied.stderr

    return _run(
        [
            "docker", "exec",
            "-e", f"PATH={INSTALLER_PATH}",
            container,
            "bash", f"{CONTAINER_DEPLOY_DIR}/install.sh", "--headless",
        ],
        timeout=CONVERGE_TIMEOUT,
    )


def _bare_clone(destination: Path) -> str:
    """A bare clone of the working tree, and the branch to check out.

    `--no-local` rather than the default hardlink clone: the result is copied
    into a container, and a hardlink clone's objects are links into the source
    repository, which do not survive the copy.
    """
    branch = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # `--abbrev-ref HEAD` returns the literal string "HEAD" on a detached
    # checkout, and handing that to the role as `repo_branch` is the worst
    # available outcome: `git clone --branch HEAD` resolves the *remote's* HEAD,
    # so the tier would converge the default branch, pass, and report nothing
    # about the commit under test. Refused rather than worked around — a tier
    # that silently tests something else is the failure this whole file exists
    # to end.
    if branch in ("", "HEAD"):
        raise AssertionError(
            "the deploy tier cannot run from a detached HEAD: `repo_branch` "
            "would be the literal 'HEAD', which the role's clone resolves to "
            "the remote's default branch rather than to the commit under test. "
            "Check out a branch."
        )
    subprocess.run(
        ["git", "clone", "--bare", "--no-local", str(REPO), str(destination)],
        capture_output=True, text=True, check=True, timeout=600,
    )
    return branch


@pytest.fixture(scope="session")
def deploy_image(request) -> str:
    _require_no_xdist(request.config)
    require_docker()

    platform = resolve_platform(request.config)
    # `_tag_for` rather than a constant, and reused rather than restated. Its
    # docstring names the case that bites in this repo: work runs in parallel
    # git worktrees, and two runs sharing a tag mean the second
    # `docker build -t <same tag>` moves the tag out from under the first run's
    # containers, mid-session. A constant also lets `--platform amd64` overwrite
    # the native image under the same name, so a later native run silently boots
    # an emulated one. The four axes it keys on — HEAD, the Dockerfile's bytes,
    # the checkout path, the architecture — are exactly the four that differ
    # here too.
    tag = _tag_for(DEPLOY_DOCKERFILE, platform, "deploy")
    argv = ["docker", "build", "-f", str(DEPLOY_DOCKERFILE), "-t", tag]
    if platform:
        argv += ["--platform", platform]
    argv += [str(REPO)]

    result = _run(argv, timeout=BUILD_TIMEOUT)
    assert result.returncode == 0, (
        f"building {DEPLOY_DOCKERFILE.name} failed:\n{result.stderr[-4000:]}"
    )
    return tag


@pytest.fixture(scope="session")
def systemd_host(deploy_image):
    """A booted systemd container with the tier's scaffolding inside it.

    Seven settings: the four `scripts/test-linux.sh` explains (SYS_ADMIN,
    NET_ADMIN, seccomp and apparmor unconfined), the two systemd needs
    (`--cgroupns=host` and a writable cgroup tree), and `systempaths` — which
    that script does not need and this one does, because bwrap here has to
    mount a procfs inside a nested namespace. `scripts/test-deploy.sh` carries
    the reason for each. They exist for this local runner and must never
    appear in a deployment compose file.
    """
    breakage = requested_breakage()

    # The `sandbox` control. Dropping `systempaths=unconfined` leaves the
    # seccomp grant in place, so bwrap still *creates* a user namespace and
    # then cannot mount a procfs inside it — which is precisely the state the
    # shipped Docker stack is in, and the state the tier's sandbox assertions
    # must be able to see. Removing both grants instead would be a weaker
    # control: it fails earlier and for a coarser reason.
    sandbox_grants = ["--security-opt", "systempaths=unconfined"]
    if breakage == "sandbox":
        sandbox_grants = []

    _sweep_leftover_containers()

    name = f"istota-deploy-{uuid.uuid4().hex[:8]}"
    started = _run([
        "docker", "run", "-d",
        "--name", name,
        "--hostname", "istota-deploy",
        # systemd needs a cgroup tree it can write and its own view of it.
        "--cgroupns=host",
        "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "--tmpfs", "/run",
        "--tmpfs", "/run/lock",
        # `exec` because uv and the sandbox probes run binaries out of /tmp.
        "--tmpfs", "/tmp:exec",
        # bubblewrap. The four are a set, not alternatives — see the driver.
        "--cap-add=SYS_ADMIN",
        "--cap-add=NET_ADMIN",
        "--security-opt", "seccomp=unconfined",
        "--security-opt", "apparmor=unconfined",
        *sandbox_grants,
        deploy_image,
    ])

    # Inside the `try`, not ahead of it. `docker run -d` can create a container
    # and then fail to start it, and the id is on stdout either way — asserting
    # first would leave that container behind with the rest of the teardown
    # skipped.
    try:
        assert started.returncode == 0, started.stderr

        deadline = time.monotonic() + BOOT_TIMEOUT
        state = ""
        while time.monotonic() < deadline:
            probe = _run([
                "docker", "exec", name, "systemctl", "is-system-running"
            ], timeout=30)
            state = (probe.stdout or probe.stderr).strip()
            if state in ("running", "degraded"):
                break
            time.sleep(1)
        else:
            logs = _run(["docker", "logs", "--tail", "60", name]).stdout
            raise AssertionError(
                f"systemd did not come up inside {BOOT_TIMEOUT}s "
                f"(last state {state!r}).\n{logs}"
            )

        with tempfile.TemporaryDirectory() as staging:
            clone = Path(staging) / "istota.git"
            branch = _bare_clone(clone)

            _run(["docker", "exec", name, "mkdir", "-p", "/opt/istota-src"])
            for source, target in (
                (clone, CONTAINER_CLONE),
                (REPO / "deploy", CONTAINER_DEPLOY_DIR),
            ):
                copied = _run(
                    ["docker", "cp", str(source), f"{name}:{target}"], timeout=600
                )
                assert copied.returncode == 0, copied.stderr
            # `docker cp` preserves the host uid, and git refuses a repository
            # it does not own with a message about the *remote* being
            # unreadable rather than about ownership.
            _run(["docker", "exec", name, "chown", "-R", "root:root", "/opt/istota-src"])

        command = BREAKAGES.get(breakage)
        if command:
            damaged = _run(["docker", "exec", name, "bash", "-c", command])
            assert damaged.returncode == 0, (
                f"could not apply the {breakage!r} breakage: {damaged.stderr}"
            )
            print(f"\nISTOTA_DEPLOY_TIER_BREAK={breakage}: the tier is running "
                  "against a deliberately broken deployment")

        yield name, branch
    finally:
        keep = os.environ.get("ISTOTA_DEPLOY_TIER_KEEP", "").strip().lower()
        if keep in ("1", "true", "yes", "on"):
            print(f"\nISTOTA_DEPLOY_TIER_KEEP: leaving container {name} running")
        else:
            _run(["docker", "rm", "-f", name], timeout=120)


@pytest.fixture(scope="session")
def converged_host(systemd_host) -> ConvergedHost:
    """The role, applied for real, once per session."""
    container, branch = systemd_host
    settings = {
        "repo_url": CONTAINER_CLONE,
        "repo_branch": branch,
        # A real-shaped key so the secrets store is on. Fixed rather than
        # random: nothing here decrypts anything a later session wrote, and a
        # fixed value keeps a kept container reproducible.
        "secret_key": "0" * 64,
        # Deliberately shaped as a placeholder rather than as a token. This
        # repo is public and `.githooks/pre-commit` scans staged content with
        # gitleaks; a string shaped like a real OAuth token is exactly what
        # that scan exists to stop, and a documentation-placeholder spelling is
        # exempt by design.
        "claude_oauth_token": "CHANGEME-deploy-tier-placeholder",
        "_user": "istota",
    }

    result = _converge(container, settings)
    if result.returncode != 0:
        raise AssertionError(
            "the role did not converge.\n"
            f"exit={result.returncode}\n"
            f"--- stdout (tail) ---\n{result.stdout[-8000:]}\n"
            f"--- stderr (tail) ---\n{result.stderr[-4000:]}"
        )

    return ConvergedHost(
        container=container, settings=settings, converge_output=result.stdout
    )


def pytest_collection_modifyitems(config, items):
    """Mark everything in this package `deploy`.

    A `pytestmark` in each module would work and would be one more thing to
    forget; the marker is a property of the directory.
    """
    for item in items:
        if Path(str(item.fspath)).parent == Path(__file__).parent:
            item.add_marker(pytest.mark.deploy)
