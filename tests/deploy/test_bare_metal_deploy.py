"""What a converged bare-metal host must look like.

Every assertion here is a read of state the Ansible role left behind on a real
systemd host, and `tests/deploy/conftest.py` explains the shape it converged and
the four things it turns off. Read that first.

**On a tier asserting against an artifact, reading the test tells you almost
nothing about whether it can fail.** That rule is the project's, written down in
`.claude/rules/testing.md` after five separate cases of a probe whose success
was indistinguishable from a no-op, and it applies here more than anywhere: a
container that converged nothing at all still answers `systemctl` and still has
a `/srv/app`. `scripts/test-deploy-negative-control.sh` is what proves these can
go red, and it names the node ids each control must turn red.

Two of the assertions below carry their own in-session control, because a
container is the wrong place to rely on an out-of-band script for the property
most likely to be silently vacuous:

* `TestTheSandboxWorksHere` requires bubblewrap to create a namespace *and*
  requires the same probe to report a different answer outside it. A `bwrap`
  that was skipped runs the command anyway and returns the same bytes, which is
  the exact failure `tests/smoke/test_sandbox_in_stack.py` was rebuilt around.
* `TestDoctorIsTheOracle` requires a non-trivial number of checks to have
  *run*, so an empty or truncated payload cannot read as "nothing failed".
"""

from __future__ import annotations

import tomllib

import pytest


class TestTheRoleConverges:
    """The play itself, and what it says about its own run.

    `converged_host` already fails the session when `install.sh` exits
    non-zero, so these are about the content of a run that reported success —
    a play can exit 0 with tasks it silently skipped past.
    """

    def test_the_play_recap_reports_no_failures(self, converged_host):
        assert "failed=0" in converged_host.converge_output, (
            "the play recap does not report failed=0:\n"
            + converged_host.converge_output[-3000:]
        )

    def test_the_play_actually_changed_something(self, converged_host):
        """A converge against an already-converged host is `changed=0`, which
        is correct there and meaningless here: this is a first install, so a
        `changed=0` recap means the role took none of its own branches and the
        rest of this file is asserting about a host nothing built."""
        recap = converged_host.converge_output
        changed = [
            part
            for line in recap.splitlines()
            if "changed=" in line
            for part in line.split()
            if part.startswith("changed=")
        ]
        assert changed, f"no play recap in the output:\n{recap[-3000:]}"
        assert int(changed[-1].split("=")[1]) > 0, (
            f"the first converge reported {changed[-1]}"
        )


class TestTheUnitsAreEnabledAndRunning:
    """The 35 `systemd`/`systemctl` touchpoints in the role, observed.

    `tests/test_ansible_*.py` can see that the role *writes* a unit file. Only
    a converge can see whether systemd accepts it, whether its `ExecStart`
    resolves, and whether the process it names stays up.
    """

    def test_the_scheduler_unit_is_enabled(self, converged_host):
        result = converged_host.exec("systemctl is-enabled istota-scheduler")
        assert result.stdout.strip() == "enabled", result.stdout + result.stderr

    def test_the_scheduler_unit_is_active(self, converged_host):
        """Not `is-enabled`, and not "the unit file exists".

        The daemon crash-looping is `activating (auto-restart)` here, which is
        neither `active` nor `failed` — so this compares to the exact string
        rather than asking whether the unit is *not* failed, which a restart
        loop passes.
        """
        result = converged_host.exec("systemctl is-active istota-scheduler")
        state = result.stdout.strip()
        assert state == "active", (
            f"istota-scheduler is {state!r}, not active.\n"
            + converged_host.journal("istota-scheduler")
        )

    def test_the_scheduler_execstart_names_the_deployed_interpreter(
        self, converged_host
    ):
        """The unit template interpolates a path that only exists after
        `uv sync` has run, so this is an ordering assertion as much as a
        rendering one."""
        exec_start = converged_host.unit_property("istota-scheduler", "ExecStart")
        assert f"{converged_host.home}/.venv/bin/python" in exec_start, exec_start
        assert "istota.scheduler" in exec_start, exec_start

    def test_the_scheduler_is_not_restarting(self, converged_host):
        """`NRestarts` catches the daemon that comes up, dies, and is up again
        by the time `is-active` is asked — which is what a missing dependency
        looks like from the outside.

        Not zero: systemd counts the restarts the tier's own second converge
        causes, and the role restarts the unit on a source change by design.
        What must not happen is a *loop*, so this reads the counter twice with
        a gap and requires it to have settled.
        """
        first = converged_host.unit_property("istota-scheduler", "NRestarts")
        converged_host.exec("sleep 5")
        second = converged_host.unit_property("istota-scheduler", "NRestarts")
        assert first == second, (
            f"istota-scheduler restarted between reads ({first} -> {second}), "
            "so it is crash-looping.\n" + converged_host.journal("istota-scheduler")
        )

    def test_no_unit_on_the_host_is_failed(self, converged_host):
        """The whole failed set, not a list of units we thought of.

        An assertion naming units cannot see the one nobody predicted, which is
        the case this tier exists for. The image masks the container-meaningless
        units so this set is empty on a healthy converge — see the Dockerfile.
        """
        failed = converged_host.failed_units()
        assert failed == [], f"failed units after the converge: {failed}"


class TestTheRenderedConfigLoads:
    """`config.toml.j2` is 39.9K of Jinja, and `tests/test_ansible_config_template.py`
    renders it and reads the TOML. What that cannot do is hand the result to
    the loader the daemon actually uses, on a host where the paths it names
    exist."""

    def test_the_config_was_rendered_where_the_unit_looks_for_it(
        self, converged_host
    ):
        path = f"{converged_host.home}/istota/config/config.toml"
        assert converged_host.path_exists(path), f"{path} does not exist"
        exec_start = converged_host.unit_property("istota-scheduler", "ExecStart")
        assert path in exec_start, (
            f"the unit does not point at the rendered config:\n{exec_start}"
        )

    def test_the_rendered_config_is_valid_toml(self, converged_host):
        raw = converged_host.read_file(
            f"{converged_host.home}/istota/config/config.toml"
        )
        assert raw.strip(), "the rendered config is empty"
        tomllib.loads(raw)

    def test_the_real_loader_accepts_it(self, converged_host):
        """`load_config` on the deployed interpreter, against the deployed file.

        A config that parses as TOML and that `load_config` rejects — or that
        it accepts while dropping a section — is invisible to every existing
        Ansible test, all of which stop at `tomllib`.
        """
        config = f"{converged_host.home}/istota/config/config.toml"
        # `load_config` takes a `Path`; handing it the string exits on an
        # AttributeError inside the loader, which reads like a config defect.
        program = (
            "from pathlib import Path; "
            "from istota.config import load_config; "
            f"print(load_config(Path({config!r})).storage_backend)"
        )
        result = converged_host.exec(
            f"{converged_host.home}/.venv/bin/python -c {program!r}",
            user="istota",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The tier converges with an empty `nextcloud_url`, so this is also the
        # assertion that the settings answer survived the whole chain.
        assert result.stdout.strip() == "local", result.stdout + result.stderr


class TestTheSandboxWorksHere:
    """The reason bare metal is the canonical shape.

    `AGENTS.md`: the shipped Docker stack grants neither `seccomp:unconfined`
    nor `systempaths=unconfined`, so it runs every task unsandboxed. A tier for
    the shape where the sandbox *does* work has to prove the sandbox works, or
    it is testing the Docker posture with extra steps.
    """

    PROBE = "bwrap --dev-bind / / --proc /proc --unshare-net --unshare-pid true"

    #: **Every probe here runs as the daemon's user, and that is the whole
    #: assertion.** The first version ran them as root, which is who
    #: `docker exec` is by default, and root in a container holding
    #: CAP_SYS_ADMIN can mount a procfs that an unprivileged process cannot.
    #: So the probes passed on a container deliberately run *without*
    #: `systempaths=unconfined` while doctor — which probes as the daemon —
    #: correctly reported that no namespace could be created. Two answers to
    #: one question, and the reassuring one was about a user that never runs a
    #: task. `scripts/test-deploy-negative-control.sh` is what caught it.
    USER = "istota"

    def test_bubblewrap_can_create_a_namespace(self, converged_host):
        result = converged_host.exec(self.PROBE, user=self.USER)
        assert result.returncode == 0, (
            "bwrap could not create a namespace as the daemon's own user on "
            "the converged host:\n" + result.stderr
        )

    def test_the_probe_is_not_vacuous(self, converged_host):
        """The in-session control.

        `bwrap ... true` succeeding proves nothing on its own — so does `true`.
        This asks a question that can only be answered one way inside a
        namespace and the other way outside it: who PID 1 is. Inside
        `--unshare-pid` it is bwrap's own init; on the converged host it is
        systemd.

        Deliberately not the command's own `$$`, which was the first version
        and was wrong: under `--unshare-pid` bwrap keeps PID 1 for its init and
        the command is PID 2, so an assertion that the command sees itself as
        PID 1 fails on a namespace that was created correctly. Reading
        `/proc/1/comm` has no such off-by-one and names the two states instead
        of counting them.
        """
        inside = converged_host.exec(
            "bwrap --dev-bind / / --proc /proc --unshare-pid cat /proc/1/comm",
            user=self.USER,
        )
        outside = converged_host.exec("cat /proc/1/comm", user=self.USER)
        assert inside.returncode == 0, inside.stderr
        assert inside.stdout.strip() == "bwrap", (
            "PID 1 inside the sandbox is "
            f"{inside.stdout.strip()!r}, so no PID namespace was created"
        )
        assert outside.stdout.strip() == "systemd", (
            "PID 1 on the host is "
            f"{outside.stdout.strip()!r}, so the two readings do not "
            "distinguish a namespace from its absence"
        )

    def test_the_probe_and_doctor_answer_the_same_question(self, converged_host):
        """Two independent readings of one fact, required to agree.

        This tier now asks "can a task be sandboxed here" twice — once with a
        `bwrap` of its own and once through `security.sandbox_effective`, which
        runs the product's own `_bwrap_available`. They disagreed on the first
        negative control, in the direction that matters: the probe said yes
        about root and doctor said no about the daemon. Requiring them to agree
        is what makes either one worth reading, and it fails whichever way they
        part.
        """
        probe_ok = converged_host.exec(self.PROBE, user=self.USER).returncode == 0
        checks = {c["name"]: c for c in converged_host.doctor()}
        doctor_ok = checks["security.sandbox_effective"]["status"] == "ok"
        assert probe_ok == doctor_ok, (
            f"the tier's own bwrap probe says {probe_ok} and doctor says "
            f"{doctor_ok}: {checks['security.sandbox_effective']['detail']}"
        )

    def test_the_role_enabled_unprivileged_user_namespaces(self, converged_host):
        """The sysctl the role sets for the sandbox, read back from the host
        rather than from the YAML that asks for it."""
        result = converged_host.exec("sysctl -n kernel.unprivileged_userns_clone")
        # The knob is Debian-specific and absent on some kernels; the role's own
        # task carries `failed_when: false` for that reason, so this asserts the
        # value only where the knob exists.
        if result.returncode != 0:
            pytest.skip("kernel.unprivileged_userns_clone is not present here")
        assert result.stdout.strip() == "1", result.stdout


class TestDoctorIsTheOracle:
    """`istota doctor`, run with the daemon's environment.

    Reused rather than restated, for the reason `image` and `smoke` reuse it:
    hand-written assertions about a deployment drift from the code that builds
    one. `ConvergedHost.doctor` explains why reconstructing the unit's
    environment is load-bearing and what it cost to learn.
    """

    #: Checks allowed to fail, each with the reason it is about the tier rather
    #: than about the deployment. Keep this list short and keep it explicit: it
    #: is the one place this tier can be silently weakened.
    EXPECTED_FAILURES: dict[str, str] = {}

    def test_doctor_ran_a_real_number_of_checks(self, converged_host):
        """Guards the assertions below against an empty payload.

        `all(... for r in [])` is True, so a doctor that returned nothing would
        make every check here pass. The registry is 40-odd checks; twenty is a
        floor that a truncated payload fails and that a check being retired
        does not.
        """
        checks = converged_host.doctor()
        assert len(checks) >= 20, f"doctor returned only {len(checks)} checks"

    def test_doctor_reports_no_unexpected_failure(self, converged_host):
        failures = {
            check["name"]: check["detail"]
            for check in converged_host.doctor()
            if check["status"] == "fail"
        }
        unexpected = {
            name: detail
            for name, detail in failures.items()
            if name not in self.EXPECTED_FAILURES
        }
        assert not unexpected, "doctor reports unexpected failures:\n" + "\n".join(
            f"  {name}: {detail}" for name, detail in sorted(unexpected.items())
        )

    def test_every_exemption_is_still_needed(self, converged_host):
        """The exemption list is a claim, and a stale one weakens the tier.

        A check that stopped failing must leave the list, or the list slowly
        becomes a set of things nobody has looked at.
        """
        failed = {
            check["name"]
            for check in converged_host.doctor()
            if check["status"] == "fail"
        }
        stale = sorted(set(self.EXPECTED_FAILURES) - failed)
        assert not stale, (
            f"these are exempted but no longer fail: {stale}. Remove them."
        )

    def test_the_sandbox_check_agrees_with_the_host(self, converged_host):
        """Named specifically because it is the one doctor check this tier
        exists to see pass, and the one the Docker tiers structurally cannot.
        """
        checks = {c["name"]: c for c in converged_host.doctor()}
        assert "security.sandbox_effective" in checks, sorted(checks)
        result = checks["security.sandbox_effective"]
        assert result["status"] == "ok", (
            f"{result['status']}: {result['detail']}"
        )


class TestTheConvergeIsIdempotent:
    """A second run of the same play must not fight the first.

    This is the property an operator relies on every time they re-run
    `install.sh --update`, and no static test of the YAML can see it: a task
    with a wrong `changed_when`, a template that renders differently on the
    second pass, or a `command` with no `creates` all look fine in the source
    and report `changed` for ever here.
    """

    def test_a_second_converge_succeeds_and_leaves_the_daemon_up(
        self, converged_host
    ):
        """One test, not two, because the second assertion is about the state
        the first one creates.

        These were split, and the daemon half then asserted `is-active ==
        active` while taking no dependency on the reapply having happened. Run
        in the other order it re-states `test_the_scheduler_unit_is_active` and
        proves nothing — and nothing pins the order: the driver deliberately
        does not pass `-p no:randomly`, and pytest is free to reorder within a
        class regardless. Folding them makes the sequence the test rather than
        an assumption about the runner.

        `reapply_role`, not `converge` — see its docstring. The bootstrap's own
        re-run downloads three Ansible collections and made this fail on a
        truncated fetch, which is a network flake wearing a role defect's
        clothes.
        """
        result = converged_host.reapply_role()
        assert result.returncode == 0, (
            f"the second converge failed:\n{result.stdout[-6000:]}\n"
            f"{result.stderr[-2000:]}"
        )
        assert "failed=0" in result.stdout, result.stdout[-3000:]

        # The reapply restarts the unit by design. What must not happen is that
        # it comes back broken.
        state = converged_host.exec("systemctl is-active istota-scheduler")
        assert state.stdout.strip() == "active", (
            state.stdout + converged_host.journal("istota-scheduler")
        )
