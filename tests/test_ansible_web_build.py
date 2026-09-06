"""ISSUE-428: the auto-update cron ships `web/` source without building it.

`deploy/ansible/templates/istota-update.sh.j2` resets the checkout, syncs
Python dependencies, migrates and restarts every unit. It had no npm step, and
`web/build` is gitignored — so a frontend-only commit landed on disk, every
service was restarted, and the browser kept running the old bundle until
somebody ran the play. `web_app` serves the bundle through `StaticFiles`, which
reads from disk per request, so a build in place needs no restart ordered
around it; what was missing was the build.

The second half is the early exit. `git reset --hard` lands before anything
that can fail, so under `set -euo pipefail` a later failure left HEAD at the
target and the next run exiting 0 on a half-applied deploy — indefinitely, with
the reason only in the update log. A multi-minute npm build makes that much
likelier to be reached, so the script now records the sha a run got all the way
through and compares against that rather than against HEAD movement.

These execute the rendered script against a real git repository with stubs for
everything that would touch the host (`chown`, `systemctl`, `uv`, `sqlite3`,
`npm`). That is the seam `test_ansible_clone_credential.py::TestHelperScript`
established — render through a bare Jinja environment, rewrite the absolute
host paths to a tmpdir, run it — and it is what separates a build that ran from
one that was skipped. Asserting on the template text alone cannot: every
version of this script contains the string `npm run build` once it is added,
including one whose condition is never true.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
UPDATE_TEMPLATE = ANSIBLE / "templates" / "istota-update.sh.j2"

NAMESPACE = "istota"

VARS = {
    "istota_namespace": NAMESPACE,
    "istota_repo_dir": "",  # filled per-run
    "istota_repo_branch": "main",
    "istota_repo_tag": "",
    "istota_package": "istota",
    "istota_user": "istota",
    "istota_group": "istota",
    "istota_home": "",  # filled per-run
    "istota_claude_versions_keep": 3,
    "istota_install_all_extras": True,
    "istota_memory_search_enabled": False,
    "istota_whisper_enabled": False,
    "istota_location_enabled": False,
    "istota_web_enabled": True,
    "istota_devbox_enabled": False,
    "istota_web_only": False,
    "istota_devbox_users": [],
    "istota_devbox_proxy_enabled": False,
}

STUBS = ("chown", "systemctl", "sqlite3", "uv", "npm")

# The real binaries the script needs, symlinked into a directory of their own
# so `run()` can hand it a PATH holding nothing else. A rig that appends the
# ambient PATH is not hermetic in either direction: the script puts
# `/root/.local/bin` ahead of it for `uv`, so on the shape the role installs
# the real `uv` beats the stub — and removing a stub to assert on a *missing*
# tool finds the host's own instead, which is how the missing-npm case first
# passed for the wrong reason against a real `npm ci`.
REAL_TOOLS = (
    "git",
    "bash",
    "python3",
    "sh",
    "env",
    "date",
    "basename",
    "dirname",
    "readlink",
    "cat",
    "ls",
    "tail",
    "head",
    "rm",
    "mv",
    "mkdir",
    "uname",
    "sed",
    "grep",
)


def tasks() -> list:
    return yaml.safe_load(TASKS_FILE.read_text())


def find_task(name: str) -> dict:
    for task in tasks():
        if isinstance(task, dict) and task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def task_index(name: str) -> int:
    for i, task in enumerate(tasks()):
        if isinstance(task, dict) and task.get("name") == name:
            return i
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def when_clauses(task: dict) -> list[str]:
    """``when:`` as a list of strings, however the task spelled it."""
    when = task.get("when")
    if when is None:
        return []
    if isinstance(when, str):
        return [when]
    return [str(clause) for clause in when]


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


class Rig:
    """A rendered update script over a throwaway checkout."""

    def __init__(self, root: Path, *, tag_mode: bool = False, web_enabled: bool = True):
        self.root = root
        self.origin = root / "origin.git"
        self.src = root / "src"
        self.repo = root / "repo"
        self.home = root / "home"
        self.state = root / "state"
        self.log = root / "update.log"
        self.stub_log = root / "stubs.log"
        self.bin = root / "bin"
        self.tools = root / "tools"
        self.tag_mode = tag_mode
        self.web_enabled = web_enabled
        self._build()

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        self.origin.mkdir(parents=True)
        _git(self.origin, "init", "--bare", "--initial-branch=main", ".")

        self.src.mkdir()
        _git(self.src, "init", "--initial-branch=main", ".")
        (self.src / "src").mkdir()
        (self.src / "src" / "app.py").write_text("x = 1\n")
        (self.src / "web").mkdir()
        (self.src / "web" / "package-lock.json").write_text("{}\n")
        (self.src / "web" / "app.svelte").write_text("<p>a</p>\n")
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-m", "a")
        _git(self.src, "remote", "add", "origin", str(self.origin))
        _git(self.src, "push", "-u", "origin", "main")
        if self.tag_mode:
            _git(self.src, "tag", "v1.0.0")
            _git(self.src, "push", "origin", "v1.0.0")

        _git(self.root, "clone", str(self.origin), str(self.repo))

        (self.home / ".venv" / "bin").mkdir(parents=True)
        self.state.mkdir()
        self.bin.mkdir()
        self.tools.mkdir()
        for name in REAL_TOOLS:
            found = shutil.which(name)
            if found:
                (self.tools / name).symlink_to(found)
        for name in STUBS:
            self._stub(self.bin / name)
        self._stub(self.home / ".venv" / "bin" / "python")
        # `flock` is a util-linux binary and macOS has none, so without a
        # stand-in the real script exits 0 at the lock and every assertion
        # below passes vacuously. A **working** one rather than `exit 0`: the
        # serialization is a property worth testing, and a no-op here means
        # deleting the two lock lines from the template leaves the suite green.
        #
        # `flock -n <fd>` locks the file *description* the shell opened with
        # `exec 200>`, which a child inherits — so a lock this process takes on
        # that fd outlives it, held by the shell. That is the whole reason the
        # idiom works, and it is what makes this a faithful stand-in rather
        # than an approximation.
        lock = self.bin / "flock"
        lock.write_text(
            "#!/usr/bin/env python3\n"
            "import fcntl, sys\n"
            "args = sys.argv[1:]\n"
            "nb = '-n' in args\n"
            "fds = [a for a in args if a.isdigit()]\n"
            "if not fds:\n"
            "    sys.exit(0)\n"
            "flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nb else 0)\n"
            "try:\n"
            "    fcntl.flock(int(fds[0]), flags)\n"
            "except OSError:\n"
            "    sys.exit(1)\n"
        )
        lock.chmod(0o755)

    def _stub(self, path: Path) -> None:
        path.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "$(basename "$0")" "$*" >> "{self.stub_log}"\n'
            "exit 0\n"
        )
        path.chmod(0o755)

    # -- advancing the remote --------------------------------------------

    def commit(self, files: dict[str, str], *, tag: str | None = None) -> str:
        for rel, text in files.items():
            target = self.src / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-m", "next")
        _git(self.src, "push", "origin", "main")
        if tag:
            _git(self.src, "tag", tag)
            _git(self.src, "push", "origin", tag)
        return _git(self.src, "rev-parse", "HEAD")

    # -- running ---------------------------------------------------------

    def script(self) -> str:
        variables = dict(VARS)
        variables["istota_repo_dir"] = str(self.repo)
        variables["istota_home"] = str(self.home)
        variables["istota_web_enabled"] = self.web_enabled
        if self.tag_mode:
            variables["istota_repo_tag"] = "latest"
        rendered = Environment().from_string(UPDATE_TEMPLATE.read_text()).render(**variables)
        # The three absolute host paths. Rewritten rather than parameterised so
        # the template keeps the literals a real deployment uses.
        rendered = rendered.replace(f"/var/log/{NAMESPACE}/{NAMESPACE}-update.log", str(self.log))
        rendered = rendered.replace(f"/tmp/{NAMESPACE}-update.lock", str(self.root / "lock"))
        rendered = rendered.replace(f"/var/lib/{NAMESPACE}", str(self.state))
        # The script puts `/root/.local/bin` *ahead* of the inherited PATH for
        # its `uv` call, so on any host that has one — which is the shape the
        # role installs, and the Linux tier's root container — the real `uv`
        # would win over the stub and the rig would run a genuine
        # `uv sync --extra all` against a throwaway checkout. That also
        # silently disarms `test_a_half_applied_deploy_is_retried`, which
        # makes its point by replacing that stub with a failing one.
        rendered = rendered.replace("/root/.local/bin", str(self.bin))
        return rendered

    def run(self) -> subprocess.CompletedProcess:
        path = self.root / "update.sh"
        path.write_text(self.script())
        path.chmod(0o755)
        env = dict(os.environ)
        # Nothing of the host's PATH: see REAL_TOOLS.
        env["PATH"] = f"{self.bin}:{self.tools}"
        return subprocess.run(
            ["bash", str(path)], capture_output=True, text=True, env=env, timeout=120
        )

    # -- reading back ----------------------------------------------------

    def calls(self) -> list[str]:
        if not self.stub_log.exists():
            return []
        return [line.strip() for line in self.stub_log.read_text().splitlines() if line.strip()]

    def clear_calls(self) -> None:
        self.stub_log.write_text("")

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def marker(self) -> str:
        path = self.state / "last-deployed-sha"
        return path.read_text().strip() if path.exists() else ""

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def ran(calls: list[str], prefix: str) -> bool:
    return any(line == prefix or line.startswith(prefix + " ") for line in calls)


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


class TestTheFrontendIsBuilt:
    """Each of these fails against the pre-fix script."""

    def test_a_web_change_is_built(self, rig):
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "npm run build"), rig.calls()

    def test_a_python_only_change_is_not_built(self, rig):
        rig.commit({"src/app.py": "x = 2\n"})
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert not ran(rig.calls(), "npm run build"), rig.calls()
        # The control: this run did do a deploy, so "no build" is not "no run".
        assert ran(rig.calls(), "systemctl restart istota-scheduler"), rig.calls()

    def test_npm_ci_runs_only_when_the_lockfile_changed(self, rig):
        (rig.repo / "web" / "node_modules").mkdir()
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.run()
        assert ran(rig.calls(), "npm run build"), rig.calls()
        assert not ran(rig.calls(), "npm ci"), rig.calls()

        rig.clear_calls()
        rig.commit({"web/package-lock.json": '{"n":1}\n'})
        rig.run()
        assert ran(rig.calls(), "npm ci"), rig.calls()
        assert ran(rig.calls(), "npm run build"), rig.calls()

    def test_npm_ci_runs_when_node_modules_is_absent(self, rig):
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.run()
        assert ran(rig.calls(), "npm ci"), rig.calls()

    def test_the_build_precedes_the_restarts(self, rig):
        """A build that fails must not leave new services against an old bundle.

        Placed before the migrations too, so a failure leaves the previous
        deployment entirely intact rather than half-applied.
        """
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.run()
        calls = rig.calls()
        build = next(i for i, line in enumerate(calls) if line.startswith("npm run build"))
        restart = next(i for i, line in enumerate(calls) if line.startswith("systemctl restart"))
        assert build < restart, calls

    def test_the_build_output_is_chowned_to_the_service_user(self, rig):
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.run()
        calls = rig.calls()
        build = next(i for i, line in enumerate(calls) if line.startswith("npm run build"))
        chowns = [
            i
            for i, line in enumerate(calls)
            if line.startswith("chown") and line.rstrip().endswith("/web")
        ]
        assert chowns, calls
        assert max(chowns) > build, calls

    def test_the_build_is_absent_when_the_web_surface_is_off(self, tmp_path):
        rig = Rig(tmp_path, web_enabled=False)
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.run()
        assert not ran(rig.calls(), "npm run build"), rig.calls()

    def test_tag_mode_builds_too(self, tmp_path):
        rig = Rig(tmp_path, tag_mode=True)
        rig.commit({"web/app.svelte": "<p>b</p>\n"}, tag="v1.1.0")
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "npm run build"), rig.calls()


class TestOnlyOneBuildAtATime:
    """Two builds must never run against the same checkout at once.

    Both halves matter and they are separate mechanisms. The cron serializes
    against *itself* with `flock -n` on a lock it holds for the whole run, so
    a burst of commits does not start a second build — the later ticks exit
    silently and the next one after the build deploys whatever the tip is by
    then. The play serializes against the cron by taking the same lock around
    each of its own mutating commands.

    The script's own locking predates ISSUE-428 and these are characterization
    tests rather than regression ones — they were green when written. That is
    the reason to have them: the rig used to stub `flock` to `exit 0`, so
    deleting both lock lines from the template left the whole suite green.
    Confirmed able to fail by removing them (see the negative controls).
    """

    @staticmethod
    def _hold(lock_path: Path):
        """Take the lock the way another run of the script would."""
        handle = open(lock_path, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def test_a_run_does_nothing_while_another_holds_the_lock(self, rig):
        rig.run()  # seed the marker
        deployed = rig.marker()
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        rig.clear_calls()

        handle = self._hold(rig.root / "lock")
        try:
            blocked = rig.run()
        finally:
            handle.close()

        assert blocked.returncode == 0, "a contended run must exit quietly, not fail"
        assert rig.calls() == [], rig.calls()
        assert rig.marker() == deployed, "a run that did nothing recorded a deploy"

        # The control, in the same test: with the lock free the identical run
        # builds. Without it, a rig that silently could not run at all would
        # satisfy every assertion above.
        assert rig.run().returncode == 0, rig.log_text()
        assert ran(rig.calls(), "npm run build"), rig.calls()

    def test_the_lock_is_taken_before_anything_is_mutated(self, rig):
        """A lock taken after the fetch or the reset would serialize nothing.

        Asserted on the rendered script rather than by racing two runs: the
        window is real but timing-dependent, and the ordering is what makes it
        closed.
        """
        # Comments stripped first: this file's own header explains the reset
        # ordering in prose, and a plain `index` finds that sentence rather
        # than the command — which read as a failure the first time and would
        # read as a pass just as easily under a different wording.
        code = "\n".join(
            line for line in rig.script().splitlines() if not line.lstrip().startswith("#")
        )
        lock_at = code.index("flock -n 200")
        for marker in ('cd "$REPO_DIR"', "git fetch", "git reset --hard", "npm run build"):
            assert lock_at < code.index(marker), f"{marker} is not covered by the lock"


class TestTheDeployedMarker:
    """The early exit tests what the last run finished, not where HEAD points."""

    def test_the_first_run_seeds_the_marker_without_redeploying(self, rig):
        """Upgrading to this script must not restart every unit on every host.

        A missing marker is the state of a host the play just deployed, so it
        is recorded rather than acted on.
        """
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert rig.marker() == rig.head()
        assert not ran(rig.calls(), "systemctl restart istota-scheduler"), rig.calls()

    def test_a_half_applied_deploy_is_retried(self, rig):
        """The regression: HEAD at the target, the deploy never finished."""
        rig.run()  # seed the marker
        before = rig.marker()
        rig.commit({"src/app.py": "x = 2\n"})
        # A run that resets and then dies before recording completion.
        (rig.bin / "uv").write_text("#!/bin/sh\nexit 1\n")
        (rig.bin / "uv").chmod(0o755)
        first = rig.run()
        assert first.returncode != 0
        assert rig.head() != before, "the reset did not land; the rig is wrong"
        assert rig.marker() == before, "the marker moved on a run that failed"

        rig._stub(rig.bin / "uv")
        rig.clear_calls()
        second = rig.run()
        assert second.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "systemctl restart istota-scheduler"), (
            "the second run exited on HEAD movement and left a half-applied deploy"
        )
        assert rig.marker() == rig.head()

    def test_a_retry_still_rebuilds_the_frontend(self, rig):
        """The interaction between this file's two halves, and the one that bites.

        `git reset --hard` lands on the run that then fails, so on the retry
        HEAD is already the target and a `$CURRENT..$TARGET` diff is empty. A
        build gated on that diff would be skipped on every retry and the run
        would then record itself complete — latching ISSUE-428's own symptom
        permanently, instead of until the next play. The range has to start at
        the marker, which is what is actually deployed.
        """
        rig.run()  # seed the marker
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        (rig.bin / "npm").write_text("#!/bin/sh\nexit 1\n")
        (rig.bin / "npm").chmod(0o755)
        assert rig.run().returncode != 0
        assert rig.head() != rig.marker()

        rig._stub(rig.bin / "npm")
        rig.clear_calls()
        second = rig.run()
        assert second.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "npm run build"), rig.calls()

    def test_an_unknown_deployed_revision_rebuilds(self, rig):
        """The marker outlives a re-clone, so its sha may resolve to nothing.

        `git diff` against a missing object exits non-zero, which under
        `set -e` would end every run before the migrations and the restarts.
        """
        rig.run()
        (rig.state / "last-deployed-sha").write_text("f" * 40 + "\n")
        rig.commit({"src/app.py": "x = 2\n"})
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "npm run build"), rig.calls()

    def test_a_missing_npm_does_not_stop_the_python_half(self, rig):
        """The build sits ahead of the migrations, so it must fail open.

        A host that can never build would otherwise wedge the whole pipeline
        on every tick, with the reason only in the update log.
        """
        rig.run()
        (rig.bin / "npm").unlink()
        rig.commit({"web/app.svelte": "<p>b</p>\n"})
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert ran(rig.calls(), "systemctl restart istota-scheduler"), rig.calls()
        assert "npm is not on PATH" in rig.log_text()

    def test_an_empty_marker_reads_as_missing(self, rig):
        """A redirection creates the file before the writer runs.

        A `git rev-parse` that failed would leave a zero-byte marker that no
        sha can ever equal, so every tick would redeploy in full — the outcome
        the seeding exists to avoid.
        """
        (rig.state / "last-deployed-sha").write_text("")
        result = rig.run()
        assert result.returncode == 0, rig.log_text()
        assert rig.marker() == rig.head()
        assert not ran(rig.calls(), "systemctl restart istota-scheduler"), rig.calls()

    def test_a_finished_deploy_exits_without_work(self, rig):
        rig.run()
        rig.commit({"src/app.py": "x = 2\n"})
        rig.run()
        rig.clear_calls()
        result = rig.run()
        assert result.returncode == 0
        assert rig.calls() == []


# ---------------------------------------------------------------------------
# The role, and the build stamp
# ---------------------------------------------------------------------------


class TestTheRole:
    def test_the_role_records_the_deployed_sha(self):
        """Otherwise every play is followed by a redundant cron redeploy.

        The marker would still hold the pre-play sha, so the next tick would
        re-run dependencies, the build and every restart against a checkout the
        play had already deployed.
        """
        find_task("Record the deployed revision")
        assert task_index("Record the deployed revision") > task_index(
            "Read back the deployed revision"
        )

    def test_the_play_serializes_against_the_cron(self):
        """The play took no lock at all, so it could build beside a cron run.

        The cron holds its lock for its whole run, which is now minutes rather
        than seconds. A play landing inside that window ran `npm ci` — which
        wipes `node_modules` — underneath the cron's `npm run build`, and both
        then wrote the deployed-revision marker unordered.

        `-w` rather than `-n`: an operator running the play wants the deploy to
        happen, just not concurrently, so it waits for the cron rather than
        failing. The cron keeps `-n` and yields instead, since another tick is
        two minutes away.
        """
        lock = f"/tmp/{NAMESPACE}-update.lock"
        # The same lock the script takes, or the two serialize nothing. Read
        # out of the rendered template rather than restated here, since a
        # second literal drifting from the first serializes nothing and says
        # nothing while it happens.
        rendered = (
            Environment()
            .from_string(UPDATE_TEMPLATE.read_text())
            .render(istota_namespace=NAMESPACE)
        )
        assert f'LOCK="{lock}"' in rendered

        env = Environment()
        for name in (
            "Install Python dependencies with uv",
            "Install web UI dependencies",
            "Build web UI",
        ):
            command = env.from_string(find_task(name)["command"]).render(
                istota_namespace=NAMESPACE,
                istota_update_lock_wait=900,
                istota_install_all_extras=True,
            )
            argv = command.split()
            assert argv[0] == "flock", f"{name} does not take the update lock: {argv}"
            assert "-w" in argv, f"{name} must wait for the lock, not fail: {argv}"
            assert lock in argv, f"{name} takes a different lock: {argv}"

    def test_the_marker_is_never_written_by_a_web_only_play(self):
        """A web-only play advances HEAD and deploys none of the Python half.

        `Clone or update istota repository (branch checkout)` carries no
        `when:`, so HEAD reaches the branch tip; `uv sync`, the schema
        migration and the scheduler start are all `when: not istota_web_only`.
        Recording that as a completed deploy would make the cron's early exit
        fire for ever, leaving the scheduler on pre-update code against an
        unmigrated database with nothing in the update log to say so.
        """
        for name in ("Create the deployment state directory", "Record the deployed revision"):
            clauses = when_clauses(find_task(name))
            assert any("not istota_web_only" in c for c in clauses), (name, clauses)

    def test_the_role_stamps_the_build_sha(self):
        task = find_task("Build web UI")
        assert "ISTOTA_BUILD_SHA" in yaml.safe_dump(task.get("environment") or {})

    def test_the_update_script_stamps_the_build_sha(self):
        assert "ISTOTA_BUILD_SHA" in UPDATE_TEMPLATE.read_text()

    def test_the_svelte_config_reads_the_stamp(self):
        """`_app/version.json` is only an oracle if it names a commit.

        SvelteKit's default is a build timestamp, which says nothing about
        which checkout produced the bundle.
        """
        config = (REPO / "web" / "svelte.config.js").read_text()
        assert "ISTOTA_BUILD_SHA" in config
