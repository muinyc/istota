"""Tests for the runtime self-check registry (`istota doctor`).

Two things are under test here and they pull in opposite directions.

The *checks* are assertions about a host, so each one is driven against
fabricated binaries in ``tmp_path`` rather than against whatever the machine
running the suite happens to have installed. A check that passed because the
developer had ``gh`` on their PATH would be asserting nothing.

The *registry* is the part every layer above doctor consumes as an oracle, so
its invariants get their own class: names unique, a result's name predictable
from its registry entry, ``only=`` selecting before invoking, ``probe=False``
spawning nothing, and a raising check reported rather than propagated.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from istota import doctor, secrets_store, subscription_usage
from istota import executor as doctor_executor
from istota.doctor import (
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    IMAGE,
    LIVE_CHECKS,
    OK,
    SKIP,
    WARN,
    CheckResult,
    exit_code,
    render_json,
    render_text,
    run_checks,
)


def _fake_bin(path, output="", exit_code=0):
    """Write an executable shell script printing `output` and exiting `exit_code`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


def _by_name(results):
    return {r.name: r for r in results}


#: What `runtime.model_execution` asks the model to echo. Restated rather than
#: imported, so a rename of the private constant cannot silently disarm the
#: sentinel below by making it match nothing.
_MODEL_MARKER = "healthcheck-ok"


def _dev_config(make_config, tmp_path, **developer_overrides):
    """A Config with the developer skill fully wired — the shape that makes the
    `developer.*` checks actually run rather than SKIP."""
    from istota.config import DeveloperConfig

    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    fields = {
        "enabled": True,
        "repos_dir": str(repos),
        "gitlab_token": "t" * 20,
        "gh_bin_path": str(tmp_path / "bin" / "gh"),
        "glab_bin_path": str(tmp_path / "bin" / "glab"),
    }
    fields.update(developer_overrides)
    return make_config(developer=DeveloperConfig(**fields))


class TestRegistry:
    """Invariants the layers above doctor depend on."""

    def test_names_are_unique(self):
        names = [name for name, _ in CHECKS]
        assert len(names) == len(set(names))

    def test_names_are_dotted_and_stable(self):
        for name, _ in CHECKS:
            assert "." in name, f"{name} is not a dotted id"
            assert name == name.strip()
            assert name.islower()

    def test_deep_checks_are_registered(self):
        names = {name for name, _ in CHECKS}
        assert DEEP_CHECKS <= names

    def test_live_checks_are_registered(self):
        names = {name for name, _ in CHECKS}
        assert LIVE_CHECKS <= names

    def test_every_result_name_matches_its_registry_entry(self, make_config, tmp_path):
        """`only=` filters on the registry name, so a result named something
        else is invisible to the caller that asked for it."""
        config = _dev_config(make_config, tmp_path)
        for name, _ in CHECKS:
            # `live=` because a registry entry filtered out of the sweep is a
            # registry entry this test does not check. It is the one place in
            # the default suite that selects the model probe at all, so the
            # sentinel goes on the iteration that takes the risk rather than in
            # a neighbouring test: `_dev_config` configures no users and the
            # check therefore SKIPs, but nothing here enforces that, and a
            # guard that only reports afterwards reports after the spend.
            live = name in LIVE_CHECKS
            with pytest.MonkeyPatch.context() as mp:
                if live:
                    mp.setattr(subprocess, "run", _NoModel())
                results = run_checks(config, only=(name,), deep=True, live=live)
            assert results, f"{name} produced no result"
            for r in results:
                assert r.name == name or r.name.startswith(name + "."), (
                    f"{name} returned a result named {r.name!r}"
                )

    def test_the_registry_sweep_cannot_reach_a_model(self, make_config, tmp_path, monkeypatch):
        """The sweep above is the only `live=True` run in the default suite.

        It does not bill the account because `_dev_config` configures no users
        and `runtime.model_execution` therefore SKIPs — which is luck until
        something asserts it. This is that assertion, with `subprocess.run`
        replaced by a sentinel so a future change that makes the sweep reach a
        model fails here rather than on an invoice.
        """
        sentinel = _NoModel()
        monkeypatch.setattr(subprocess, "run", sentinel)
        config = _dev_config(make_config, tmp_path)
        results = run_checks(config, only=("runtime.model_execution",), deep=True, live=True)
        assert [r.status for r in results] == [SKIP]
        assert sentinel.calls == []

    def test_every_result_has_a_detail(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            assert r.detail.strip(), f"{r.name} returned an empty detail"

    def test_warn_and_fail_carry_a_remedy(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            if r.status in (WARN, FAIL):
                assert r.remedy.strip(), f"{r.name} is {r.status} with no remedy"

    def test_every_result_has_a_known_status_and_scope(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            assert r.status in (OK, WARN, FAIL, SKIP)
            assert r.scope in (IMAGE, DEPLOYMENT)

    def test_only_selects_before_invoking(self, make_config, tmp_path, monkeypatch):
        """Filtering after the fact would run every check to discard most."""
        called = []

        def _explodes(config, probe):
            called.append(1)
            raise AssertionError("this check must never be invoked")

        monkeypatch.setattr(
            doctor, "CHECKS", (("runtime.platform", doctor.check_platform), ("boom.check", _explodes))
        )
        results = run_checks(make_config(), only=("runtime.",))
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]

    def test_only_accepts_several_prefixes(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        results = run_checks(config, only=("developer.", "security.skill_proxy"))
        assert results
        for r in results:
            assert r.name.startswith("developer.") or r.name.startswith("security.skill_proxy")

    def test_empty_only_runs_everything_except_deep(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        names = {r.name.split(".")[0] + "." + r.name.split(".")[1] for r in run_checks(config)}
        assert not (names & DEEP_CHECKS)
        assert "runtime.platform" in names

    def test_deep_checks_run_only_when_asked(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        shallow = {r.name for r in run_checks(config, only=("sandbox.masks",), deep=False)}
        deep = {r.name for r in run_checks(config, only=("sandbox.masks",), deep=True)}
        assert shallow == set()
        assert deep == {"sandbox.masks"}

    def test_scope_filters(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, scope=IMAGE):
            assert r.scope == IMAGE
        for r in run_checks(config, scope=DEPLOYMENT):
            assert r.scope == DEPLOYMENT

    def test_every_registry_entry_declares_a_scope(self):
        assert {name for name, _ in CHECKS} == set(doctor.CHECK_SCOPES)
        assert set(doctor.CHECK_SCOPES.values()) <= {IMAGE, DEPLOYMENT}

    def test_a_checks_results_carry_its_registry_scope(self, make_config, tmp_path):
        """`scope=` selects on the registry entry, so a result whose own scope
        disagreed would be selected by one value and reported with another."""
        config = _dev_config(make_config, tmp_path)
        for name, _ in CHECKS:
            for r in run_checks(config, only=(name,), deep=True):
                assert r.scope == doctor.CHECK_SCOPES[name], f"{r.name} disagrees with {name}"

    def test_scope_selects_before_invoking(self, make_config, tmp_path, monkeypatch):
        """`--scope image` runs in a volume-less `docker run`, where the
        deployment-scoped checks would fail on a perfectly good image. Filtering
        afterwards would pay for them in order to discard them."""
        called = []

        def _deployment_check(config, probe):
            called.append(1)
            return CheckResult("dep.check", OK, "ran", scope=DEPLOYMENT)

        def _image_check(config, probe):
            return CheckResult("img.check", OK, "ran", scope=IMAGE)

        monkeypatch.setattr(
            doctor, "CHECKS", (("dep.check", _deployment_check), ("img.check", _image_check))
        )
        monkeypatch.setattr(
            doctor, "CHECK_SCOPES", {"dep.check": DEPLOYMENT, "img.check": IMAGE}
        )
        results = run_checks(make_config(), scope=IMAGE)
        assert called == []
        assert [r.name for r in results] == ["img.check"]

    def test_skip_excludes_before_invoking(self, make_config, monkeypatch):
        called = []

        def _expensive(config, probe):
            called.append(1)
            return CheckResult("runtime.framework_db", OK, "ran")

        monkeypatch.setattr(
            doctor,
            "CHECKS",
            (("runtime.framework_db", _expensive), ("runtime.platform", doctor.check_platform)),
        )
        results = run_checks(make_config(), skip=("runtime.framework_db",))
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]

    def test_skip_wins_over_only(self, make_config, monkeypatch):
        results = run_checks(
            make_config(), only=("runtime.",), skip=("runtime.framework_db",)
        )
        assert "runtime.framework_db" not in {r.name for r in results}
        assert "runtime.platform" in {r.name for r in results}

    def test_a_raising_check_becomes_fail(self, make_config, monkeypatch):
        """Doctor runs on the daemon start-up path; an exception there would
        turn a diagnostic into an outage."""

        def _raises(config, probe):
            raise RuntimeError("kaboom in the check")

        monkeypatch.setattr(doctor, "CHECKS", (("boom.check", _raises),))
        results = run_checks(make_config())
        assert len(results) == 1
        assert results[0].name == "boom.check"
        assert results[0].status == FAIL
        assert "kaboom in the check" in results[0].detail
        assert results[0].remedy

    def test_probe_false_spawns_no_subprocess(self, make_config, tmp_path, monkeypatch):
        """`_validate_forge_clis` calls this from `load_config`, which runs in
        every host-side skill-CLI subprocess. Five `--version` spawns per call
        is not a refactor, it is a regression.

        Counted with a spy rather than asserted by raising: `run_checks` catches
        every exception per check and turns it into a FAIL result, so a raising
        stub is swallowed and the test passes no matter what the checks do.
        """
        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(subprocess, "run", _spy)
        monkeypatch.setattr(subprocess, "Popen", _spy)
        monkeypatch.setattr(subprocess, "check_output", _spy)
        config = _dev_config(make_config, tmp_path)
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        results = run_checks(config, probe=False, deep=True)
        assert spawns == [], f"probe=False spawned: {spawns}"
        # And nothing was quietly converted into a FAIL along the way, which is
        # how the raising version of this test hid its own failure.
        raised = [r for r in results if "the check itself raised" in r.detail]
        assert raised == [], [(r.name, r.detail) for r in raised]

    def test_the_spy_would_catch_a_spawn(self, make_config, tmp_path, monkeypatch):
        """Positive control for the test above.

        A guard that cannot fail is not a guard, and the previous version of
        this pair could not: it asserted by raising into a `try/except` that
        exists precisely to swallow. Register a check that spawns, and confirm
        the technique sees it.
        """
        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(subprocess, "run", _spy)

        def _spawning_check(config, probe):
            subprocess.run(["/bin/true"], capture_output=True)
            return CheckResult("boom.spawns", OK, "should not get here")

        monkeypatch.setattr(doctor, "CHECKS", (("boom.spawns", _spawning_check),))
        run_checks(make_config(), probe=False)
        assert spawns != []

    def test_probe_false_is_named_in_the_detail(self, make_config, tmp_path):
        """An operator reading a probe=False result must be able to tell it was
        answered from the filesystem rather than by running anything."""
        config = _dev_config(make_config, tmp_path)
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        results = _by_name(run_checks(config, only=("developer.forge_binaries",), probe=False))
        assert "not executed" in results["developer.forge_binaries.gh"].detail


class TestConfigLoadPathStaysCheap:
    """The config-load path is the hot one, and two heavy imports crept onto it.

    `_validate_forge_clis` runs inside every `load_config`: the daemon, the web
    app, the webhook receiver, every CLI invocation, and every host-side skill
    CLI subprocess the skill proxy spawns *per call*. Reaching the forge
    resolution rule through `istota.skills.developer` pulled in the whole skill
    package (~190ms, because `istota.skills.__init__` star-imports every skill),
    and `web.static` reaching its path through `web_app` pulled in FastAPI,
    authlib and a second full `load_config()` (+56MB RSS).

    Asserted as import graphs rather than as timings: a wall-clock threshold on
    a shared laptop is a flaky test, and the thing that actually regressed is
    which module gets imported.
    """

    def _import_graph(self, module: str) -> set[str]:
        """Modules pulled in by importing `module` in a fresh interpreter."""
        code = (
            "import json, sys\n"
            f"import {module}\n"
            "print(json.dumps(sorted(sys.modules)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        return set(json.loads(out.stdout))

    def test_forge_bin_is_a_stdlib_only_leaf(self):
        loaded = self._import_graph("istota.forge_bin")
        assert "istota.skills" not in loaded
        assert "istota.config" not in loaded

    def test_static_dir_is_a_stdlib_only_leaf(self):
        loaded = self._import_graph("istota.static_dir")
        assert "fastapi" not in loaded
        assert "istota.web_app" not in loaded
        assert "istota.config" not in loaded

    def _run_in_fresh_interpreter(
        self, tmp_path, body: str
    ) -> tuple[set[str], list[tuple[str, str]]]:
        """Run `body` against a wired-up dev Config in a fresh interpreter.

        Returns the modules loaded afterwards and the `(name, status)` pairs of
        whatever `run_checks` the body called put in `results`. Statuses and not
        just names, because a check that returned early is still a result under
        the same name — see the caller.

        `tmp_path` is handed to the subprocess rather than letting it call
        `mkdtemp`, so pytest owns the cleanup like it does for every other test
        in this file.

        A subprocess rather than `monkeypatch.delitem` on `sys.modules`, for the
        reason the two sibling tests above already spawn one: deleting
        `istota.skills` while its importer stays cached makes the deletion
        inert, so the import chain resolves from cache, nothing re-adds the
        module, and the assertion passes while the property is untested. That is
        ordering-dependent, so under `-n auto` it is a flake rather than only a
        run-it-alone curiosity (ISSUE-335). No arrangement of in-process cache
        surgery fixes this; a clean interpreter is the only honest substrate.
        """
        code = (
            "import json, pathlib, sys\n"
            "from istota.config import CONFIG_LOAD_CHECKS, Config, DeveloperConfig\n"
            "from istota.doctor import run_checks\n"
            "tmp = pathlib.Path(sys.argv[1])\n"
            "skills = tmp / 'skills'; skills.mkdir(exist_ok=True)\n"
            "(skills / '_index.toml').write_text('')\n"
            "mount = tmp / 'mount'; mount.mkdir(exist_ok=True)\n"
            "repos = tmp / 'repos'; repos.mkdir(exist_ok=True)\n"
            "config = Config(\n"
            "    db_path=tmp / 'test.db', temp_dir=tmp / 'temp',\n"
            "    skills_dir=skills, nextcloud_mount_path=mount,\n"
            "    developer=DeveloperConfig(\n"
            "        enabled=True, repos_dir=str(repos), gitlab_token='t' * 20,\n"
            "        gh_bin_path=str(tmp / 'bin' / 'gh'),\n"
            "        glab_bin_path=str(tmp / 'bin' / 'glab'),\n"
            "    ),\n"
            ")\n"
            "results = []\n"
            f"{body}\n"
            "print(json.dumps({\n"
            "    'modules': sorted(sys.modules),\n"
            "    'results': [[r.name, r.status] for r in results],\n"
            "}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
        )
        # Not `check=True`: `CalledProcessError` does not render `stderr`, so a
        # traceback from the constructed `Config` — a renamed field, say —
        # would surface only as a non-zero exit status with the cause discarded.
        assert out.returncode == 0, out.stderr
        # The last line, not the whole of stdout: anything the body prints ahead
        # of the payload should fail an assertion rather than a JSON parse.
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        return set(payload["modules"]), [tuple(r) for r in payload["results"]]

    def test_the_config_load_checks_do_not_import_the_skill_package(self, tmp_path):
        """The checks `load_config` runs stay off the skill package's import graph.

        Scoped to `config.CONFIG_LOAD_CHECKS` — the exact tuple
        `_validate_forge_clis` passes — rather than to a `developer.` prefix.
        The prefix was wrong at both ends: it overshot onto `repos_layout` and
        `container`, which reach `executor` deliberately and are on no hot path,
        and it undershot by omitting `security.skill_proxy`, which really does
        run inside every `load_config`.

        `istota.executor` is asserted alongside `istota.skills` because it is
        the importer that pulls the package in (`executor` imports
        `.skills.calendar` at module scope, and `istota.skills.__init__`
        star-imports every skill). Naming both means the guard still bites if
        the star-import is ever removed but the chain onto `executor` is not.
        """
        loaded, ran = self._run_in_fresh_interpreter(
            tmp_path,
            "results = run_checks(config, only=CONFIG_LOAD_CHECKS, probe=False)",
        )
        from istota.config import CONFIG_LOAD_CHECKS

        # Every requested check produced a result. `only=` filters on registry
        # names, so a rename would otherwise leave the module assertions below
        # passing over an empty run.
        for name in CONFIG_LOAD_CHECKS:
            assert any(
                n == name or n.startswith(f"{name}.") for n, _ in ran
            ), f"{name} produced no result; the guard would be vacuous"

        # And the two that reach for something did the reaching. Returning a
        # result is not evidence of work: both of these emit one under their own
        # name from an early `SKIP` — `check_forge_binaries` before it calls
        # `_resolved_forge_bin`, `check_forge_policy` before it imports
        # `forge_cli` — so a tightened gate would leave nothing heavy running and
        # every assertion below satisfied. `security.skill_proxy` is deliberately
        # not held to this: it SKIPs legitimately when `istota-skill` is off the
        # PATH, which is a property of the machine, not of the checks.
        did_work = {"developer.forge_binaries", "developer.forge_policy"}
        for name, status in ran:
            if name in did_work or name.rsplit(".", 1)[0] in did_work:
                assert status != SKIP, f"{name} skipped; nothing heavy was reached"

        assert "istota.skills" not in loaded
        assert "istota.executor" not in loaded

    def test_the_import_probe_can_see_an_import(self, tmp_path):
        """The control for the test above, which otherwise cannot be seen to fail.

        A fresh-interpreter probe that reported an empty or truncated module set
        would satisfy every `not in` assertion above while testing nothing. This
        asserts the other direction on the same helper, against a synthetic body
        rather than against a real check, so no product change can make it churn.

        Only the module named by the body is asserted, and deliberately not
        `istota.skills` alongside it: that one is present because
        `executor` imports `.skills.calendar`, which is a product fact the test
        above says may legitimately change. Asserting it here would make the
        control red for a reason having nothing to do with whether the probe can
        see an import.
        """
        loaded, _ = self._run_in_fresh_interpreter(tmp_path, "import istota.executor")
        assert "istota.executor" in loaded

    def test_web_static_does_not_import_web_app(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        monkeypatch.delitem(sys.modules, "istota.web_app", raising=False)
        config = make_config(web=WebConfig(enabled=True))
        assert run_checks(config, only=("web.static",))[0].status == OK
        assert "istota.web_app" not in sys.modules

    def test_web_app_and_doctor_resolve_the_same_static_dir(self):
        """One implementation, two callers — the point of the leaf."""
        from istota import static_dir, web_app

        assert web_app._resolve_static_dir() == static_dir.resolve_static_dir()

    def test_developer_skill_and_doctor_resolve_the_same_binary(self):
        from istota import forge_bin
        from istota.skills import developer

        assert developer._resolve_real_bin is forge_bin.resolve_real_bin


class TestPlatform:
    def test_linux_is_ok(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
        r = run_checks(make_config(), only=("runtime.platform",))[0]
        assert r.status == OK
        assert "x86_64" in r.detail

    def test_non_linux_with_sandbox_enabled_fails(self, make_config, monkeypatch):
        from istota.config import SecurityConfig

        monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
        config = make_config(security=SecurityConfig(sandbox_enabled=True))
        r = run_checks(config, only=("runtime.platform",))[0]
        assert r.status == FAIL
        assert "Darwin" in r.detail

    def test_non_linux_without_sandbox_warns(self, make_config, monkeypatch):
        from istota.config import SecurityConfig

        monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
        config = make_config(security=SecurityConfig(sandbox_enabled=False))
        r = run_checks(config, only=("runtime.platform",))[0]
        assert r.status == WARN

    def test_scope_is_image(self, make_config):
        r = run_checks(make_config(), only=("runtime.platform",))[0]
        assert r.scope == IMAGE


class TestBwrap:
    def test_skips_when_sandbox_disabled(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(sandbox_enabled=False))
        r = run_checks(config, only=("runtime.bwrap",))[0]
        assert r.status == SKIP

    def test_missing_bwrap_fails(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_present_and_runnable_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "bubblewrap 0.8.0")
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == OK

    def test_unrunnable_bwrap_fails(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "nope", exit_code=1)
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == FAIL

    def test_probe_false_answers_from_the_filesystem(self, make_config, tmp_path, monkeypatch):
        """A binary that exists and is executable is OK without running it —
        even one that would exit non-zero."""
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "nope", exit_code=1)
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",), probe=False)[0]
        assert r.status == OK


class TestModelCli:
    def test_skips_under_the_native_brain(self, make_config):
        from istota.config import BrainConfig

        config = make_config(brain=BrainConfig(kind="native"))
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == SKIP
        assert "native" in r.detail

    def test_missing_claude_fails_under_claude_code(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        r = run_checks(make_config(), only=("runtime.model_cli",))[0]
        assert r.status == FAIL

    def test_present_claude_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "claude", "2.1.168 (Claude Code)")
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "claude" else None)
        r = run_checks(make_config(), only=("runtime.model_cli",))[0]
        assert r.status == OK
        assert "2.1.168" in r.detail


class TestTmux:
    def test_skips_unless_tmux_brain(self, make_config):
        r = run_checks(make_config(), only=("runtime.tmux",))[0]
        assert r.status == SKIP

    def test_missing_tmux_fails_under_the_tmux_brain(self, make_config, monkeypatch):
        from istota.config import BrainConfig

        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = make_config(brain=BrainConfig(kind="tmux_claude"))
        r = run_checks(config, only=("runtime.tmux",))[0]
        assert r.status == FAIL


class TestTheBinaryChecksFollowTheReachableSet:
    """`runtime.model_cli` and `runtime.tmux` ask which kinds a task could run
    under, not which one is the base kind.

    Both halves are here on purpose. A test that only asserts the widening
    passes just as happily against a check that widened unconditionally — which
    would report a missing `claude` on every native-only deployment in the
    estate — so the negative is what says the answer tracks the allowlist.
    """

    def _native(self, make_config, **brain_fields):
        from istota.config import BrainConfig

        return make_config(brain=BrainConfig(kind="native", **brain_fields))

    def test_model_cli_runs_when_a_room_may_pin_claude_code(
        self, make_config, monkeypatch,
    ):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=["claude_code"])
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == FAIL
        assert "claude_code" in r.detail

    def test_model_cli_still_skips_with_an_empty_allowlist(
        self, make_config, monkeypatch,
    ):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=[])
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == SKIP

    def test_model_cli_ignores_an_unbuildable_allowlist_entry(
        self, make_config, monkeypatch,
    ):
        """The allowlist is not a free-text widener: a name no brain answers to
        must not turn a check on."""
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=["claude_kode"])
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == SKIP

    def test_tmux_runs_when_a_room_may_pin_tmux_claude(
        self, make_config, monkeypatch,
    ):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=["tmux_claude"])
        r = run_checks(config, only=("runtime.tmux",))[0]
        assert r.status == FAIL
        assert "tmux_claude" in r.detail

    def test_tmux_still_skips_with_an_empty_allowlist(
        self, make_config, monkeypatch,
    ):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=[])
        r = run_checks(config, only=("runtime.tmux",))[0]
        assert r.status == SKIP

    def test_tmux_is_unmoved_by_an_allowlist_naming_another_kind(
        self, make_config, monkeypatch,
    ):
        """Allowlisting `claude_code` widens `runtime.model_cli` and nothing
        else; each check reads its own kinds out of the same set."""
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, room_selectable=["claude_code"])
        r = run_checks(config, only=("runtime.tmux",))[0]
        assert r.status == SKIP

    def test_a_source_type_route_also_widens_the_cli_check(
        self, make_config, monkeypatch,
    ):
        """Not a room-allowlist property: routing a lane to a CLI brain has
        always put tasks on the binary, and the check SKIPped through it."""
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(
            make_config, source_type_overrides={"scheduled": "claude_code"},
        )
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == FAIL

    def test_a_configured_fallback_also_widens_the_cli_check(
        self, make_config, monkeypatch,
    ):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = self._native(make_config, fallback="claude_code")
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == FAIL


class TestNativeBrainCredential:
    """`runtime.native_brain` — buildable is not runnable.

    `make_brain("native")` constructs a defaulted dataclass and asserts nothing
    about a key, so before this check an operator who allowlisted `native` got
    a room where every turn failed at the provider with nothing in the registry
    naming it.
    """

    def _native(self, make_config, **brain_fields):
        from istota.config import BrainConfig, NativeBrainConfig

        native = NativeBrainConfig(
            api_key=brain_fields.pop("api_key", ""),
            base_url=brain_fields.pop("base_url", "https://api.anthropic.com/v1"),
            extra_headers=brain_fields.pop("extra_headers", {}),
        )
        return make_config(
            brain=BrainConfig(kind="native", native=native, **brain_fields)
        )

    def test_skips_where_native_is_unreachable(self, make_config, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        r = run_checks(make_config(), only=("runtime.native_brain",))[0]
        assert r.status == SKIP

    def test_fails_with_no_key_anywhere(self, make_config, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = self._native(make_config)
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_the_instance_key_satisfies_it(self, make_config, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = self._native(make_config, api_key="sk-test")
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == OK

    def test_a_whitespace_only_key_does_not(self, make_config, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = self._native(make_config, api_key="   ")
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_the_env_var_satisfies_it(self, make_config, monkeypatch):
        """Asked separately from the field: `load_config` folds the variable
        into `api_key`, but a Config assembled any other way holds one and not
        the other."""
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "sk-env")
        config = self._native(make_config)
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == OK

    def test_a_per_user_secret_satisfies_it(self, make_config, monkeypatch, tmp_path):
        """The executor overlays this per task, so a deployment where every
        user brings their own key needs no instance-wide one."""
        from istota import db, secrets_store
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "secrets.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "alice", "native_brain", "api_key", "sk-u")

        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == OK
        assert "1 user" in r.detail

    def test_another_users_secret_is_not_confused_for_one(
        self, make_config, monkeypatch, tmp_path,
    ):
        """A row for a service that is not the native brain must not count."""
        from istota import db, secrets_store
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "secrets.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "alice", "ntfy", "token", "t")

        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_a_missing_database_is_no_key_rather_than_a_traceback(
        self, make_config, monkeypatch, tmp_path,
    ):
        """And it must not *create* one on the way to finding out.

        `secrets_store.secret_exists` opens read-write and commits, so against
        an absent path it leaves a zero-byte database that a later read reports
        as `no such table` — which reads as corruption rather than absence — and
        against a live WAL database it materializes the sidecars. `sudo istota
        doctor` beside a stopped daemon would leave both owned by root.
        `check_framework_db` states the rule; this check now follows it.
        """
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        root = tmp_path / "empty"
        root.mkdir()
        config = self._native(make_config)
        config.db_path = root / "absent.db"
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL
        assert list(root.iterdir()) == []

    def test_it_opens_the_database_read_only(
        self, make_config, monkeypatch, tmp_path,
    ):
        """The mechanism, not a side effect of it.

        Asserted on the `sqlite3.connect` arguments because the observable
        outcomes are both weak: a WAL database already has its sidecars from
        `init_db`, so a before/after listing cannot see a read-write open, and
        the missing-file case is covered by an `exists()` guard that a partial
        revert would leave in place. Reverting to `secrets_store.secret_exists`
        — which connects read-write and commits — turns this red, because that
        helper's own connect carries no `mode=ro`.
        """
        import sqlite3

        from istota import db, secrets_store
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "alice", "native_brain", "api_key", "sk-u")

        opens: list[tuple[tuple, dict]] = []
        real_connect = sqlite3.connect

        def _recording(*args, **kwargs):
            opens.append((args, kwargs))
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _recording)

        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]

        assert r.status == OK
        assert opens, "the check reached no database at all"
        for args, kwargs in opens:
            assert kwargs.get("uri") is True, (args, kwargs)
            assert "mode=ro" in str(args[0]), (args, kwargs)

    def test_a_secret_for_a_user_the_config_does_not_list_does_not_count(
        self, make_config, monkeypatch, tmp_path,
    ):
        """A row left behind by a removed user is not a credential any current
        user has."""
        from istota import db, secrets_store
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "secrets.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "gone", "native_brain", "api_key", "sk-u")

        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_a_stored_key_needs_the_store_key_to_be_readable(
        self, make_config, monkeypatch, tmp_path,
    ):
        """Without `ISTOTA_SECRET_KEY` no stored row can be decrypted, so
        counting one would report a credential the daemon cannot use."""
        from istota import db, secrets_store
        from istota.config import UserConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "secrets.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "alice", "native_brain", "api_key", "sk-u")
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)

        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_a_local_endpoint_warns_rather_than_fails(self, make_config, monkeypatch):
        """An Ollama / vLLM / llama.cpp endpoint takes no key, and a FAIL is not
        inert — it reaches the start-up report, the scheduler sweep and the
        self-check heartbeat, each of which alerts on FAIL and none on WARN."""
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        for url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:11434/v1",
            "http://192.168.1.5:11434/v1",
            "http://ollama:11434/v1",
            "http://box.lan:11434/v1",
        ):
            config = self._native(make_config, base_url=url)
            r = run_checks(config, only=("runtime.native_brain",))[0]
            assert r.status == WARN, url

    def test_a_public_endpoint_still_fails(self, make_config, monkeypatch):
        """The converse: without it the WARN above would be every deployment."""
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        for url in ("https://api.anthropic.com/v1", "https://openrouter.ai/api/v1"):
            config = self._native(make_config, base_url=url)
            r = run_checks(config, only=("runtime.native_brain",))[0]
            assert r.status == FAIL, url

    def test_extra_headers_warns_rather_than_fails(self, make_config, monkeypatch):
        """The provider merges `extra_headers` over the Authorization header it
        builds, so an operator can authenticate entirely through them. This
        check cannot confirm one of them is a credential, so it says so."""
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = self._native(make_config, extra_headers={"x-api-key": "v"})
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == WARN
        assert "extra_headers" in r.detail

    def test_an_empty_extra_headers_table_does_not_soften_it(
        self, make_config, monkeypatch,
    ):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = self._native(make_config, extra_headers={})
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_a_room_allowlisting_native_is_enough_to_run_it(
        self, make_config, monkeypatch,
    ):
        """The D11 case: the base kind is a CLI brain and native is reachable
        only because a room may pin it."""
        from istota.config import BrainConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = make_config(
            brain=BrainConfig(kind="claude_code", room_selectable=["native"])
        )
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == FAIL

    def test_and_stays_silent_without_the_allowlist(self, make_config, monkeypatch):
        from istota.config import BrainConfig

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        config = make_config(brain=BrainConfig(kind="claude_code"))
        r = run_checks(config, only=("runtime.native_brain",))[0]
        assert r.status == SKIP


class TestFrameworkDb:
    def test_missing_db_warns(self, make_config, tmp_path):
        config = make_config(db_path=tmp_path / "absent.db")
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == WARN
        assert r.remedy

    def test_clean_db_is_ok(self, make_config, tmp_path):
        import sqlite3

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        conn.close()
        config = make_config(db_path=db_path)
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == OK

    def test_a_zero_length_db_warns_rather_than_reading_clean(self, make_config, tmp_path):
        """SQLite treats a zero-length file as a valid empty database, so
        `quick_check` returns no issues and the check used to report
        `quick_check clean` about a file with nothing in it (ISSUE-412)."""
        db_path = tmp_path / "istota.db"
        db_path.touch()
        assert db_path.stat().st_size == 0
        r = run_checks(make_config(db_path=db_path), only=("runtime.framework_db",))[0]
        assert r.status == WARN
        assert "0 bytes" in r.detail
        assert "no schema" in r.detail
        assert "quick_check clean" not in r.detail
        assert "istota init" in r.remedy

    def test_a_header_only_db_warns_too(self, make_config, tmp_path):
        """The property is "has this file a schema", and size is only a proxy
        for it. A header-only file is what an interrupted `istota init` or a
        bare `PRAGMA journal_mode=WAL` leaves: non-zero, passes `quick_check`,
        and has no more schema than an empty one. A size test misses it."""
        import sqlite3

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        assert db_path.stat().st_size > 0, "this test needs a non-empty file"
        r = run_checks(make_config(db_path=db_path), only=("runtime.framework_db",))[0]
        assert r.status == WARN
        assert "no schema" in r.detail
        assert "istota init" in r.remedy

    def test_unopenable_db_fails(self, make_config, tmp_path):
        db_path = tmp_path / "istota.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        config = make_config(db_path=db_path)
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == FAIL

    def test_is_deployment_scoped(self, make_config, tmp_path):
        config = make_config(db_path=tmp_path / "absent.db")
        assert run_checks(config, only=("runtime.framework_db",))[0].scope == DEPLOYMENT

    def test_does_not_repair(self, make_config, tmp_path, monkeypatch):
        """Doctor is a diagnostic. `check_db_health` owns the REINDEX."""
        import sqlite3

        from istota import db_health

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        conn.close()

        def _fail(*args, **kwargs):
            raise AssertionError("doctor must not repair the database")

        monkeypatch.setattr(db_health, "reindex", _fail)
        monkeypatch.setattr(db_health, "check_and_repair", _fail)
        run_checks(make_config(db_path=db_path), only=("runtime.framework_db",))


class TestTaskFailureRate:
    """The recent-failure-rate query, lifted verbatim off the two hand-rolled
    probes in `heartbeat.py` and `commands.py`.

    Driven through the real `db` helpers against a `tmp_path` database rather
    than by patching the query: the predicate and the NULL coalescing are the
    whole subject, and a patched query would be asserting about the patch.
    """

    def _db(self, tmp_path, statuses):
        from istota import db

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for status in statuses:
                task_id = db.create_task(conn, prompt="p", user_id="alice")
                db.update_task_status(conn, task_id, status)
        return db_path

    def _run(self, make_config, db_path):
        return run_checks(make_config(db_path=db_path), only=("runtime.task_failure_rate",))[0]

    def test_warns_when_failures_match_or_beat_completions(self, make_config, tmp_path):
        db_path = self._db(tmp_path, ["failed", "failed", "completed"])
        r = self._run(make_config, db_path)
        assert r.status == WARN
        assert r.remedy.strip()
        assert "2" in r.detail and "1" in r.detail

    def test_warns_at_the_boundary(self, make_config, tmp_path):
        """`failed >= completed`, not `>`. One and one is the existing predicate."""
        r = self._run(make_config, self._db(tmp_path, ["failed", "completed"]))
        assert r.status == WARN

    def test_ok_when_failures_are_a_minority(self, make_config, tmp_path):
        r = self._run(make_config, self._db(tmp_path, ["failed", "completed", "completed"]))
        assert r.status == OK

    def test_ok_when_nothing_failed(self, make_config, tmp_path):
        r = self._run(make_config, self._db(tmp_path, ["completed", "completed"]))
        assert r.status == OK

    def test_ok_on_an_empty_window(self, make_config, tmp_path):
        """`SUM` over no rows is NULL, not 0. Both copies coalesce before
        comparing; a naive port raises here, on the commonest deployment state."""
        r = self._run(make_config, self._db(tmp_path, []))
        assert r.status == OK

    def test_old_rows_are_outside_the_window(self, make_config, tmp_path):
        from istota import db

        db_path = self._db(tmp_path, ["failed", "failed"])
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET created_at = datetime('now', '-2 hours')")
        r = self._run(make_config, db_path)
        assert r.status == OK

    def test_never_fails_on_a_high_rate(self, make_config, tmp_path):
        """A symptom, not a broken deployment: it must not fail the daemon's
        start-up report or `istota doctor`'s exit code."""
        r = self._run(make_config, self._db(tmp_path, ["failed", "failed"]))
        assert r.status != FAIL
        assert exit_code([r]) == 0

    def test_skips_with_no_database(self, make_config, tmp_path):
        r = self._run(make_config, tmp_path / "absent.db")
        assert r.status == SKIP

    def test_fails_with_no_tasks_table(self, make_config, tmp_path):
        """`check_framework_db` runs `quick_check` and never touches `tasks`, so
        a schema-less database passes it. This is where that surfaces."""
        import sqlite3

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE unrelated (a INTEGER)")
        conn.commit()
        conn.close()
        r = self._run(make_config, db_path)
        assert r.status == FAIL
        assert "init" in r.remedy

    def test_a_corrupt_file_gets_the_restore_remedy_not_init(self, make_config, tmp_path):
        """The URI form opens lazily, so a file that is not a database connects
        and raises on the first execute — in the same branch a missing table
        lands in. `istota init` is the wrong advice for corruption."""
        db_path = tmp_path / "istota.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        r = self._run(make_config, db_path)
        assert r.status == FAIL
        assert "db_restore" in r.remedy
        assert "init" not in r.remedy

    def test_opens_the_database_read_only(self, make_config, tmp_path, monkeypatch):
        """`sudo istota doctor` against a stopped daemon must not materialize
        root-owned WAL sidecars — `check_framework_db`'s reasoning, same file."""
        import sqlite3

        seen = []
        real_connect = sqlite3.connect

        def _spy(target, *args, **kwargs):
            seen.append(target)
            return real_connect(target, *args, **kwargs)

        db_path = self._db(tmp_path, ["completed"])
        monkeypatch.setattr(sqlite3, "connect", _spy)
        assert self._run(make_config, db_path).status == OK
        assert seen, "the check opened no connection"
        assert all(str(t).startswith("file:") and "mode=ro" in str(t) for t in seen), seen

    def test_is_deployment_scoped(self, make_config, tmp_path):
        assert self._run(make_config, tmp_path / "absent.db").scope == DEPLOYMENT

    def test_is_not_live_or_deep(self):
        """It opens a file and spawns nothing, so it runs for every caller."""
        assert "runtime.task_failure_rate" not in doctor.LIVE_CHECKS
        assert "runtime.task_failure_rate" not in DEEP_CHECKS


class _ModelReached(BaseException):
    """Deliberately not an ``Exception``.

    Both `check_model_execution` and `run_checks` catch `Exception` and turn it
    into a FAIL result, so a sentinel raising `AssertionError` is swallowed
    twice over: the test that spent the money goes green unless it happens to
    assert on the sentinel's own call list. A `BaseException` passes through
    both and ends the test where the call happened.
    """


#: Captured before any test can patch it, so the stand-in below can delegate.
_REAL_SUBPROCESS_RUN = subprocess.run


class _NoModel:
    """A `subprocess.run` stand-in that fails the test if the *model probe* runs.

    It discriminates on the probe's own marker rather than refusing everything,
    because a blanket refusal cannot be installed across a whole-registry run:
    `runtime.model_cli`, `runtime.tmux` and the forge checks all spawn a real
    `--version`, and turning those into a "reached a model" failure would
    report the hazard where there is none while saying nothing about the one
    that matters. Everything else is delegated to the real function, which is
    what those checks would have called anyway.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        if any(_MODEL_MARKER in str(part) for part in (cmd or ())):
            self.calls.append(cmd)
            raise _ModelReached("a test reached a real model invocation")
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)


class TestModelExecution:
    """The live `claude -p` probe.

    Every case patches `subprocess.run` and asserts the patched callable was
    the one invoked, so no case can bill the account by accident.
    """

    def _config(self, make_config, make_user_config, tmp_path, **overrides):
        fields = {"users": {"alice": make_user_config()}, "admin_users": {"alice"}}
        fields.update(overrides)
        return make_config(**fields)

    def _stub(self, monkeypatch, *, stdout="healthcheck-ok\n", stderr="", raises=None):
        calls = []

        def _run_stub(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, 0, stdout, stderr)

        monkeypatch.setattr(subprocess, "run", _run_stub)
        return calls

    def _unsandboxed(self, monkeypatch):
        """Pin the wrap off, so a Linux host and a macOS host answer alike."""
        from istota import executor

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: False)

    def _run(self, config, **kwargs):
        kwargs.setdefault("live", True)
        return run_checks(config, only=("runtime.model_execution",), **kwargs)[0]

    def test_skips_under_probe_false_even_when_live(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """The no-spawn constraint at the top of doctor.py is unconditional,
        and this is the most expensive possible way to violate it."""
        monkeypatch.setattr(subprocess, "run", _NoModel())
        config = self._config(make_config, make_user_config, tmp_path)
        r = self._run(config, probe=False)
        assert r.status == SKIP

    def test_skips_on_a_native_brain(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        from istota.config import BrainConfig

        monkeypatch.setattr(subprocess, "run", _NoModel())
        config = self._config(
            make_config, make_user_config, tmp_path, brain=BrainConfig(kind="native")
        )
        r = self._run(config)
        assert r.status == SKIP
        assert "native" in r.detail

    def test_skips_with_no_user_to_run_as(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _NoModel())
        r = self._run(make_config())
        assert r.status == SKIP
        assert "user" in r.detail

    def test_ok_when_the_echo_comes_back(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        calls = self._stub(monkeypatch)
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == OK
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[0] == "claude" and "healthcheck-ok" in " ".join(cmd)
        assert kwargs["timeout"] == doctor.MODEL_PROBE_TIMEOUT

    def test_fails_when_the_output_is_wrong(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch, stdout="something else", stderr="boom")
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == FAIL
        assert r.remedy.strip()
        # The excerpt is what makes the finding actionable; without asserting it
        # the whole `observed` chain could return "" and this would still pass.
        assert "boom" in r.detail

    def test_the_detail_falls_back_to_stdout_and_names_the_exit_status(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        calls = []

        def _run_stub(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 7, "only on stdout", "")

        monkeypatch.setattr(subprocess, "run", _run_stub)
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == FAIL
        assert "only on stdout" in r.detail
        assert "7" in r.detail
        assert calls

    def test_the_detail_bounds_a_long_stream(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """A subprocess stream is unbounded; a rendered check line is not."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch, stdout="nope", stderr="x " * 5000)
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert len(r.detail) < 400, len(r.detail)

    def test_the_detail_says_so_when_there_was_no_output_at_all(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch, stdout="", stderr="")
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == FAIL
        assert "no output" in r.detail

    def test_fails_on_a_timeout(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        self._stub(
            monkeypatch, raises=subprocess.TimeoutExpired(cmd=["claude"], timeout=30)
        )
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == FAIL
        assert "timed out" in r.detail

    def test_fails_when_the_binary_is_missing(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch, raises=FileNotFoundError("no claude"))
        r = self._run(self._config(make_config, make_user_config, tmp_path))
        assert r.status == FAIL

    def test_resolves_the_single_admin_and_says_so(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """The probe answers a question about the deployment, not about a
        caller, so which user it ran as belongs in the detail."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch)
        config = self._config(
            make_config,
            make_user_config,
            tmp_path,
            users={"alice": make_user_config(), "bob": make_user_config()},
            admin_users={"bob"},
        )
        assert "bob" in self._run(config).detail

    def test_falls_through_to_a_configured_user_when_everyone_is_admin(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """An empty `admin_users` means everyone is admin (`Config.is_admin`),
        so it names nobody in particular and the user list decides."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch)
        config = self._config(
            make_config,
            make_user_config,
            tmp_path,
            users={"alice": make_user_config()},
            admin_users=set(),
        )
        assert "alice" in self._run(config).detail

    def test_prefers_an_admin_who_is_also_a_configured_user(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """With several admins, the probe must still run in an admin's sandbox
        shape — that is the deployment the operator asked about. Picking the
        first configured user would answer as `alice`, a non-admin, and
        `build_bwrap_cmd` would build the scoped namespace instead."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch)
        config = self._config(
            make_config,
            make_user_config,
            tmp_path,
            users={
                "alice": make_user_config(),
                "bob": make_user_config(),
                "carol": make_user_config(),
            },
            admin_users={"bob", "carol"},
        )
        assert "bob" in self._run(config).detail

    def test_an_admin_with_no_user_config_does_not_displace_a_real_user(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """`admin_users` comes from /etc/istota/admins and has no relationship
        to `config.users`, so an admin id with nothing behind it would get a
        namespace around a workspace that does not exist — a failure about the
        user list wearing a model-failure label."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch)
        config = self._config(
            make_config,
            make_user_config,
            tmp_path,
            users={"alice": make_user_config()},
            admin_users={"ghost"},
        )
        assert "alice" in self._run(config).detail

    def test_refuses_rather_than_creating_the_per_user_temp_dir(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """Doctor is a diagnostic and one of its entry points is an operator
        shell, so creating this directory means `sudo istota doctor` leaves a
        root-owned one every later task binds read-write and cannot write to."""
        from istota import executor

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: True)
        monkeypatch.setattr(subprocess, "run", _NoModel())
        config = self._config(make_config, make_user_config, tmp_path)
        user_temp = Path(config.temp_dir) / "alice"
        assert not user_temp.exists()

        r = self._run(config)

        assert r.status == SKIP
        assert not user_temp.exists(), "the check created the directory it was asked about"

    def test_reads_user_resources_read_only(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """`db.get_db` connects read-write and commits; on a WAL database that
        materializes sidecars a `sudo` run would leave owned by root."""
        import sqlite3

        from istota import db, executor

        seen = []
        real_connect = sqlite3.connect

        def _spy(target, *args, **kwargs):
            seen.append(str(target))
            return real_connect(target, *args, **kwargs)

        config = self._config(make_config, make_user_config, tmp_path)
        db.init_db(Path(config.db_path))
        (Path(config.temp_dir) / "alice").mkdir(parents=True)

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: True)
        monkeypatch.setattr(executor, "build_bwrap_cmd", lambda cmd, *a, **kw: list(cmd))
        self._stub(monkeypatch)
        monkeypatch.setattr(sqlite3, "connect", _spy)

        assert self._run(config).status == OK
        assert seen, "the resource lookup opened no connection"
        assert all("mode=ro" in target for target in seen), seen

    def test_wraps_under_effective_sandboxing_not_the_flag(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """On the shipped Docker stack `sandbox_enabled` is true and the
        namespace cannot be created, so the copies' spelling builds a wrap that
        dies for the wrong reason."""
        from istota import executor

        def _must_not_wrap(*args, **kwargs):
            raise AssertionError("wrapped a probe on a deployment with no sandbox")

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: False)
        monkeypatch.setattr(executor, "build_bwrap_cmd", _must_not_wrap)
        calls = self._stub(monkeypatch)
        config = self._config(make_config, make_user_config, tmp_path)
        assert self._run(config).status == OK
        assert calls[0][0][0] == "claude"

    def test_wraps_when_the_sandbox_is_effective(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        from istota import executor
        from istota.executor import SandboxProfile

        seen = {}

        def _wrap(cmd, config, task, is_admin, user_resources, user_temp_dir, *a, **kw):
            seen["profile"] = kw.get("profile")
            seen["task"] = task
            seen["temp"] = user_temp_dir
            return ["bwrap", "--", *cmd]

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: True)
        monkeypatch.setattr(executor, "build_bwrap_cmd", _wrap)
        calls = self._stub(monkeypatch)
        config = self._config(make_config, make_user_config, tmp_path)
        # The check refuses to create this itself; the daemon owns that.
        (Path(config.temp_dir) / "alice").mkdir(parents=True)
        assert self._run(config).status == OK
        assert calls[0][0][0] == "bwrap"
        assert seen["profile"] == SandboxProfile.CLAUDE
        assert seen["task"].user_id == "alice"

    def test_env_comes_from_build_model_cli_env(
        self, make_config, make_user_config, tmp_path, monkeypatch
    ):
        """A daemon-side model call with no task behind it: its env is that
        function's business, not `os.environ`'s."""
        from istota import executor

        self._unsandboxed(monkeypatch)
        monkeypatch.setattr(executor, "build_model_cli_env", lambda config: {"MARKER": "1"})
        calls = self._stub(monkeypatch)
        self._run(self._config(make_config, make_user_config, tmp_path))
        assert calls[0][1]["env"] == {"MARKER": "1"}

    def test_is_deployment_scoped(self, make_config, tmp_path, monkeypatch):
        sentinel = _NoModel()
        monkeypatch.setattr(subprocess, "run", sentinel)
        assert self._run(make_config()).scope == DEPLOYMENT
        # A scope assertion is true of the FAIL a swallowed sentinel would
        # produce as well, so it cannot stand alone on this check.
        assert sentinel.calls == []


class TestLiveChecks:
    """The second opt-in axis. `DEEP_CHECKS` means "spawns a namespace";
    `LIVE_CHECKS` means "costs money", and no caller wants one flag for both."""

    def test_live_checks_are_registered(self):
        assert doctor.LIVE_CHECKS <= {name for name, _ in CHECKS}

    def test_the_two_axes_are_disjoint(self):
        assert not (doctor.LIVE_CHECKS & DEEP_CHECKS)

    def test_the_sentinel_matches_the_probes_own_marker(self):
        """`_NoModel` discriminates on this string. A rename in doctor.py that
        did not reach here would disarm every guard in this file at once, and
        every one of them would keep passing."""
        assert doctor._MODEL_PROBE_MARKER == _MODEL_MARKER

    def test_deep_checks_is_exactly_the_mask_probe(self):
        """A budget guard for `web_app._doctor_deep_timeout`, which is
        `DEEP_TIMEOUT` plus headroom for exactly one deep check. It lives here
        because the file that pays for a violation cannot see it."""
        assert DEEP_CHECKS == frozenset({"sandbox.masks"})

    def test_deep_does_not_select_a_live_check(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _NoModel())
        names = {r.name for r in run_checks(make_config(), deep=True)}
        assert "runtime.model_execution" not in names

    def test_live_selects_it(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _NoModel())
        names = {r.name for r in run_checks(make_config(), live=True)}
        assert "runtime.model_execution" in names

    def test_live_filters_before_invoking(self, make_config, tmp_path, monkeypatch):
        """A live check discarded after the fact is a live check that already
        billed. Same shape as `test_only_selects_before_invoking`."""
        called = []

        def _explodes(config, probe):
            called.append(1)
            raise AssertionError("a live check must not run without live=True")

        monkeypatch.setattr(
            doctor,
            "CHECKS",
            (("runtime.platform", doctor.check_platform), ("live.check", _explodes)),
        )
        monkeypatch.setattr(doctor, "LIVE_CHECKS", frozenset({"live.check"}))
        monkeypatch.setitem(doctor.CHECK_SCOPES, "live.check", DEPLOYMENT)
        results = run_checks(make_config(), deep=True)
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]

    def test_live_does_not_imply_deep(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _NoModel())
        names = {r.name for r in run_checks(make_config(), live=True)}
        assert "sandbox.masks" not in names


class TestWritableDirs:
    def test_writable_dirs_are_ok(self, make_config, tmp_path):
        config = make_config()
        results = run_checks(config, only=("runtime.writable_dirs",))
        assert results
        assert all(r.status == OK for r in results), [
            (r.name, r.status, r.detail) for r in results if r.status != OK
        ]

    @pytest.mark.requires_dac
    def test_unwritable_dir_fails(self, make_config, tmp_path):
        if sys.platform == "win32":  # pragma: no cover
            pytest.skip("posix permissions")
        temp = tmp_path / "locked"
        temp.mkdir()
        temp.chmod(0o500)
        try:
            config = make_config(temp_dir=temp)
            results = _by_name(run_checks(config, only=("runtime.writable_dirs",)))
            assert results["runtime.writable_dirs.temp_dir"].status == FAIL
        finally:
            temp.chmod(0o700)

    def test_one_result_per_directory(self, make_config):
        names = {r.name for r in run_checks(make_config(), only=("runtime.writable_dirs",))}
        assert "runtime.writable_dirs.temp_dir" in names
        assert "runtime.writable_dirs.module_db_root" in names


class TestMountLiveness:
    @staticmethod
    def _nextcloud_backed(make_config, **overrides):
        from istota.config import NextcloudConfig

        return make_config(
            nextcloud=NextcloudConfig(url="https://cloud.example"), **overrides
        )

    def test_skips_when_no_mount_configured(self, make_config):
        config = make_config(nextcloud_mount_path=None)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == SKIP

    def test_skips_for_a_local_workspace_folder(self, make_config):
        """The local single-user install points this at a plain directory under
        `~` that nothing ever mounts. Asserting ismount there reports a healthy
        install as broken."""
        r = run_checks(make_config(), only=("runtime.mount_liveness",))[0]
        assert r.status == SKIP
        assert "local workspace folder" in r.detail

    def test_configured_but_not_mounted_fails(self, make_config, tmp_path):
        # `make_config` points nextcloud_mount_path at a plain tmp_path dir,
        # which is on the same filesystem as its parent and so is not a mount.
        config = self._nextcloud_backed(make_config)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_a_real_mount_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.os.path, "ismount", lambda p: True)
        config = self._nextcloud_backed(make_config)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == OK


# The root conftest neutralizes `subscription_usage.get_snapshot` for the whole
# suite, so the doctor sweep on a developer's macOS laptop cannot read the real
# keychain or issue a live request. This file drives the real function on
# purpose, so it captures it at import time — the same technique the money
# fixture's docstring names.
_REAL_GET_SNAPSHOT = subscription_usage.get_snapshot

# The credential a leak test looks for. Long enough that doctor's own `redact()`
# would catch it if it were a configured secret — it is not one, which is the
# point: the check must keep it out of the report on its own.
_TOKEN_SENTINEL = "sk-ant-oat01-" + "z" * 40


class _UsageTransport:
    """A stub `subscription_usage` transport that records every call.

    Recording rather than raising: `run_checks` turns an exception from a check
    into a FAIL result, so a transport that asserted by raising would be
    swallowed and the test would pass whatever the check did.
    """

    def __init__(self, status=200, body=b"{}", response_headers=None):
        self.status = status
        self.body = body
        self.response_headers = dict(response_headers or {})
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.status, self.body, dict(self.response_headers)


# A reset far enough from a minute boundary that the rendered countdown cannot
# tick over between building the payload and reading the result: 1h 04m 30s, so
# the assertion holds for any delay under 30 seconds.
_RESETS_IN = 3870


def _usage_body(*percents, resets_in=_RESETS_IN):
    """A `limits[]` payload with one window per percentage, resetting soon.

    `resets_at` is built from the wall clock rather than a frozen constant
    because the check passes its own `time.time()` all the way through — to the
    fetch, to the countdown, and to the staleness age — and a fixed timestamp
    would drift into the past as the year goes on.
    """
    kinds = ["session", "weekly_all"]
    resets_at = datetime.fromtimestamp(time.time() + resets_in, tz=timezone.utc).isoformat()
    return json.dumps(
        {
            "limits": [
                {
                    "kind": kinds[i] if i < len(kinds) else f"other_{i}",
                    "group": "weekly",
                    "percent": percent,
                    "severity": "normal",
                    "resets_at": resets_at,
                    "scope": None,
                    "is_active": True,
                }
                for i, percent in enumerate(percents)
            ]
        }
    ).encode()


def _usage_config(make_config, **fields):
    from istota.config import BrainConfig, ClaudeCodeBrainConfig

    return make_config(brain=BrainConfig(claude_code=ClaudeCodeBrainConfig(**fields)))


# Never the running developer's real home. `get_snapshot(home=None)` means "use
# `Path.home()`", not "there is no home", so a helper defaulting to None would
# read `~/.claude/.credentials.json` on the machine running the suite.
_NO_HOME = Path("/nonexistent/istota-test-home")


def _drive_usage(
    monkeypatch, *, transport=None, env=None, home=_NO_HOME, darwin_blob=None
):
    """Reinstate the real `get_snapshot` with only the host substituted.

    The check calls `get_snapshot(config, now_ts=...)` and has nowhere to pass a
    transport, an environment or a home — right for production, useless for a
    test, because the resolver would then read the developer's own keychain and
    the fetch would be a live request. Supplying those three behind a wrapper
    runs the whole real policy (resolution, TTL, cache, fetch, stale fallback)
    against a stub host.

    `now_ts` is passed through rather than frozen: the check computes the
    staleness age against the same clock it hands the module, and overriding one
    half of that pair would make every reading look hours old.
    """
    from istota import subscription_usage as su

    if darwin_blob is not None:
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            su.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, darwin_blob, ""),
        )
    else:
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")

    calls = []

    def _wrapper(config, *, now_ts, **kwargs):
        calls.append(now_ts)
        return _REAL_GET_SNAPSHOT(
            config,
            now_ts=now_ts,
            transport=transport,
            env={} if env is None else env,
            home=home,
        )

    monkeypatch.setattr(su, "get_snapshot", _wrapper)
    return calls


def _credential_file(tmp_path, token):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token}})
    )
    return home


class TestSubscriptionUsage:
    """`runtime.subscription_usage` — the plan's own budget.

    On a subscription deployment the dashboard's cost column is deliberately
    blank, so the rate-limit windows are the only budget there is. Every test
    here asserts the check did not SKIP before asserting anything else: the
    module docstring's warning applies with full force to a check whose natural
    resting state on an unconfigured host is exactly SKIP.
    """

    def _result(self, config, **kwargs):
        results = run_checks(config, only=("runtime.subscription_usage",), **kwargs)
        assert len(results) == 1
        return results[0]

    def test_it_is_registered_as_a_deployment_check_and_is_not_deep(self):
        """It reads a network endpoint, not the image, and spawns no namespace."""
        assert doctor.CHECK_SCOPES["runtime.subscription_usage"] == DEPLOYMENT
        assert "runtime.subscription_usage" not in DEEP_CHECKS

    def test_doctor_does_not_import_the_module_at_module_scope(self):
        """The import is lazy, so `doctor` stays cheap for the config-load path.

        `_validate_forge_clis` imports `doctor` inside every `load_config`, which
        runs in every CLI invocation and every host-side skill CLI the proxy
        spawns per call. Same technique as `TestConfigLoadPathStaysCheap`.
        """
        code = "import json, sys\nimport istota.doctor\nprint(json.dumps(sorted(sys.modules)))"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert "istota.subscription_usage" not in set(json.loads(out.stdout))

    def test_disabled_by_config_skips(self, make_config, monkeypatch):
        transport = _UsageTransport(body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport)
        r = self._result(_usage_config(make_config, subscription_usage=False))
        assert r.status == SKIP
        assert "disabled" in r.detail
        assert transport.calls == []

    def test_probe_false_skips_without_a_network_call(self, make_config, monkeypatch):
        """`TestRegistry` already proves no check spawns a process under
        probe=False. This one must clear the network bar too, and a check that
        merely skipped *rendering* while still fetching would pass that test."""
        transport = _UsageTransport(body=_usage_body(40))
        snapshots = _drive_usage(
            monkeypatch,
            transport=transport,
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config), probe=False)
        assert r.status == SKIP
        assert "probe disabled" in r.detail
        assert transport.calls == []
        assert snapshots == [], "probe=False must not even ask for a snapshot"

    def test_no_credential_skips(self, make_config, tmp_path, monkeypatch):
        transport = _UsageTransport(body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport, env={}, home=tmp_path / "nowhere")
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "no Claude Code OAuth credential found" in r.detail
        assert transport.calls == [], "nothing to authenticate with, so nothing to send"

    def test_a_healthy_plan_is_ok_and_names_every_window(self, make_config, monkeypatch):
        """Worst first, and *all* of them.

        "5-hour at 12%, weekly at 94%" and "5-hour at 94%, weekly at 12%" call
        for different operator responses, and this one line is the whole of what
        a terminal reader sees.
        """
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(12, 40)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status == OK
        assert "5-hour at 12%" in r.detail
        assert "Weekly (all models) at 40%" in r.detail
        assert r.detail.index("Weekly (all models)") < r.detail.index("5-hour")
        assert "resets in 1h 04m" in r.detail

    @pytest.mark.parametrize(
        "percent,expect_warn",
        [(0, False), (79.9, False), (80, True), (94.9, True), (95, True), (100, True)],
    )
    def test_the_thresholds(self, make_config, monkeypatch, percent, expect_warn):
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(percent)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status == (WARN if expect_warn else OK)
        if expect_warn:
            assert r.remedy, "a WARN an operator cannot act on is a log line"

    @pytest.mark.parametrize("percent,expect_warn", [(45, False), (55, True)])
    def test_the_configured_thresholds_are_the_ones_that_are_read(
        self, make_config, monkeypatch, percent, expect_warn
    ):
        """Every other threshold case sets the value the dataclass already has.

        A check that ignored `[brain.claude_code]` entirely and used its own
        hardcoded 80/95 would pass all of them. This is the case that fails on
        such a check: 55% is a WARN only if the configured 50 was read, and 45%
        is an OK only if the hardcoded 80 was not.
        """
        config = _usage_config(
            make_config,
            subscription_usage_warn_percent=50.0,
            subscription_usage_high_percent=60.0,
        )
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(percent)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(config)
        assert r.status == (WARN if expect_warn else OK)

    def test_an_inverted_threshold_pair_still_warns_in_the_gap(
        self, make_config, monkeypatch
    ):
        """`warn` above `high` would otherwise make the band unreachable.

        The loader corrects the pair, so this is the second line: a config that
        reached the dataclass some other way must not silently stop warning.

        75% is chosen to sit below the *default* warn of 80 as well, so a check
        that ignored the configured pair entirely would answer OK here.
        """
        config = _usage_config(
            make_config,
            subscription_usage_warn_percent=90.0,
            subscription_usage_high_percent=70.0,
        )
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(75)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        assert self._result(config).status == WARN

    def test_the_busy_remedy_names_the_configured_fallback(
        self, make_config, monkeypatch
    ):
        """ISSUE-362: the remedy used to promise a failover unconditionally.

        `claude_code` has never had an implicit fallback and since ISSUE-362 no
        kind has, so on a deployment with none the old fixed literal told the
        operator their tasks would reroute when they will simply fail.
        """
        from istota.config import BrainConfig, ClaudeCodeBrainConfig

        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(97)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        config = make_config(
            brain=BrainConfig(
                kind="claude_code",
                fallback="native",
                claude_code=ClaudeCodeBrainConfig(),
            )
        )
        r = self._result(config)
        assert r.status == WARN
        assert "native" in r.remedy
        assert "No [brain] fallback" not in r.remedy

    def test_the_busy_remedy_says_so_when_there_is_no_fallback(
        self, make_config, monkeypatch
    ):
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(97)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status == WARN
        assert "No [brain] fallback is configured" in r.remedy
        assert "fail over" not in r.remedy

    @pytest.mark.parametrize("percent", [0, 79.9, 80, 94.9, 95, 100, 150])
    def test_no_utilization_ever_fails(self, make_config, monkeypatch, percent):
        """A plan at 97% is a fact about the plan, not a defect in the host.

        `doctor.exit_code` returns 1 on any FAIL and `scheduler._alert_doctor_failures`
        messages every admin on the transition into failure. Neither is a
        reasonable response to a busy week.
        """
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=_usage_body(percent)),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status != FAIL
        assert r.status != SKIP, "a SKIP here would pass this test on a broken check"

    @pytest.mark.parametrize("source", ["env", "file", "keychain"])
    def test_a_rejected_credential_skips_naming_which_one(
        self, make_config, tmp_path, monkeypatch, source
    ):
        """Three credential sources resolve, and only the source name is fit to print.

        Which one the endpoint refused is the whole diagnostic: a setup token in
        the environment and an interactive login in the keychain fail for
        completely different reasons and have different repairs.

        SKIP rather than WARN, and no remedy. The endpoint does not serve the
        long-lived setup-token credential both server shapes deploy, so on those
        hosts this row was a permanent warning naming no action anyone could
        take. The reason is still carried; only the severity changed.
        """
        transport = _UsageTransport(status=403, body=b'{"error":"forbidden"}')
        _drive_usage(monkeypatch, transport=transport, **self._sources(tmp_path, source))
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "403" in r.detail
        assert source in r.detail
        assert not r.remedy, "a SKIP names no repair"
        assert transport.calls, "a resolvable credential should have been tried"

    @pytest.mark.parametrize("source", ["env", "file", "keychain"])
    @pytest.mark.parametrize("status", [200, 403])
    def test_the_token_value_is_never_in_the_report(
        self, make_config, tmp_path, monkeypatch, source, status
    ):
        """Doctor's `redact()` is a backstop, not the plan.

        It scans against `config_secrets`, and this credential is not in the
        config at all — it comes from the environment, a file in `~/.claude`, or
        the keychain. Nothing downstream would catch a leak here.
        """
        transport = _UsageTransport(status=status, body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport, **self._sources(tmp_path, source))
        r = self._result(_usage_config(make_config))
        # Not `status != SKIP` any more: a refused credential is a legitimate
        # SKIP now. The guard that check-level tests must not pass on a check
        # that never ran still holds, so it is spelled against evidence the
        # check reached the endpoint and reported what came back.
        assert transport.calls, "the check never issued a request"
        assert ("403" in r.detail) if status == 403 else ("40" in r.detail)
        assert _TOKEN_SENTINEL not in r.detail + r.remedy
        assert "sk-ant" not in r.detail + r.remedy
        sent = transport.calls[0][1]["Authorization"]
        assert _TOKEN_SENTINEL in sent, "the token belongs in the header and nowhere else"

    @staticmethod
    def _sources(tmp_path, source):
        """Resolver inputs that make exactly `source` the winning branch."""
        blob = json.dumps({"claudeAiOauth": {"accessToken": _TOKEN_SENTINEL}})
        if source == "env":
            return {"env": {"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL}, "home": tmp_path / "no"}
        if source == "file":
            return {"env": {}, "home": _credential_file(tmp_path, _TOKEN_SENTINEL)}
        return {"env": {"USER": "someone"}, "home": tmp_path / "no", "darwin_blob": blob}

    def test_an_unreachable_endpoint_with_no_cache_skips(self, make_config, monkeypatch):
        transport = _UsageTransport(status=500, body=b"")
        _drive_usage(
            monkeypatch,
            transport=transport,
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "500" in r.detail
        assert not r.remedy

    def test_an_endpoint_with_no_recognizable_windows_skips(self, make_config, monkeypatch):
        """A shipped shape change reads as "nothing to check", not as 0%.

        It reported WARN with a "the parser needs updating" remedy until the
        whole no-data family became SKIP. The distinction that remedy drew — the
        request succeeded, so do not go hunting for an egress fault — is worth
        keeping, and it survives in the detail, which still says the endpoint
        named no window this reader understands.
        """
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(body=b'{"limits": [], "quince": null}'),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "no recognizable rate-limit windows" in r.detail
        assert not r.remedy

    def test_a_windowless_success_skips_rather_than_raising(
        self, make_config, monkeypatch
    ):
        """The guard behind the never-FAIL promise, driven directly.

        `get_snapshot` cannot return this today — an error-free snapshot always
        carries windows — so the only way to exercise the guard is to hand the
        check one. It is worth exercising because the failure mode is an
        IndexError, and `run_checks` converts a raising check into the one status
        this check must never produce.
        """
        from istota import subscription_usage as su

        monkeypatch.setattr(
            su,
            "get_snapshot",
            lambda config, **kwargs: su.UsageSnapshot(fetched_at=time.time()),
        )
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert r.detail, "a SKIP still has to say why"

    def _seed_cache(self, config, age_seconds, percent=40):
        """Write a good cache entry `age_seconds` old, as a fetch would have."""
        from istota import subscription_usage as su

        now = time.time()
        windows, spend = su.parse_usage(json.loads(_usage_body(percent)), now_ts=now)
        assert windows, "the fixture must really parse, or the test proves nothing"
        su.write_cache(
            su.cache_path(config.db_path.parent),
            su.UsageSnapshot(fetched_at=now - age_seconds, windows=windows, spend=spend),
        )

    def test_a_stale_reading_within_the_window_still_reports_its_numbers(
        self, make_config, monkeypatch
    ):
        """An old-but-real reading is worth more than nothing — but say it is old.

        The status stays OK below `stale_after`; that threshold is the whole
        point of the setting. What must not happen is an hour-long outage reading
        as a plain OK, because the countdown beside the percentage is recomputed
        against the current clock while the percentage is not, and that pair is
        the most misleading line this check could print.
        """
        # TTL pinned below the seeded age for the same reason as the test below:
        # at the shipping 1800s default a 900s reading is fresh, no fetch fires,
        # and the stale branch this test exists for is never reached.
        config = _usage_config(
            make_config,
            subscription_usage_stale_after_seconds=3600,
            subscription_usage_cache_ttl_seconds=300,
        )
        self._seed_cache(config, age_seconds=900)
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(status=500, body=b""),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(config)
        assert r.status == OK
        assert "5-hour at 40%" in r.detail
        assert "last successful reading is 15m old" in r.detail
        assert "500" in r.detail

    def test_the_configured_stale_window_is_the_one_that_is_read(
        self, make_config, monkeypatch
    ):
        """The same 900s reading, against a 60s window instead of the default."""
        # The cache TTL is pinned below the seeded age: at the shipping default
        # of 1800 a 900s reading is still *fresh*, so nothing would fetch and
        # there would be no stale branch under test at all.
        config = _usage_config(
            make_config,
            subscription_usage_stale_after_seconds=60,
            subscription_usage_cache_ttl_seconds=300,
        )
        self._seed_cache(config, age_seconds=900)
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(status=500, body=b""),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(config)
        assert r.status == SKIP
        assert "5-hour at 40%" not in r.detail, "past the window it is not a reading"

    def test_a_reading_older_than_stale_after_skips_with_its_age(
        self, make_config, monkeypatch
    ):
        config = _usage_config(make_config, subscription_usage_stale_after_seconds=3600)
        self._seed_cache(config, age_seconds=7300)
        _drive_usage(
            monkeypatch,
            transport=_UsageTransport(status=500, body=b""),
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(config)
        assert r.status == SKIP
        assert "last successful reading is 2h 01m old" in r.detail
        assert "500" in r.detail
        assert not r.remedy

    def test_a_fresh_cache_is_served_without_a_request(self, make_config, monkeypatch):
        """The TTL is deployment-wide: doctor, the dashboard and `!usage` share it."""
        config = _usage_config(make_config, subscription_usage_cache_ttl_seconds=300)
        self._seed_cache(config, age_seconds=40, percent=90)
        transport = _UsageTransport(body=_usage_body(1))
        _drive_usage(
            monkeypatch,
            transport=transport,
            env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        r = self._result(config)
        assert r.status == WARN
        assert "5-hour at 90%" in r.detail
        assert transport.calls == []


class TestSkillProxy:
    def test_resolvable_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "istota-skill")
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(fake) if name == "istota-skill" else None
        )
        results = _by_name(run_checks(make_config(), only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == OK

    def test_unresolvable_fails(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        results = _by_name(run_checks(make_config(), only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == FAIL

    def test_skips_when_the_proxy_is_disabled(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == SKIP

    def test_forge_posture_warns_when_tokens_configured_and_proxy_off(
        self, make_config, tmp_path
    ):
        """Wording preserved from `_validate_forge_clis`: the tokens still work,
        but they sit in the environment the model's own shell inherits."""
        from istota.config import SecurityConfig

        config = _dev_config(make_config, tmp_path)
        config.security = SecurityConfig(skill_proxy_enabled=False)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        posture = results["security.skill_proxy.forge_posture"]
        assert posture.status == WARN
        assert "readable by anything else the task runs" in posture.detail

    def test_forge_posture_skips_without_tokens(self, make_config, tmp_path):
        from istota.config import SecurityConfig

        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        config.security = SecurityConfig(skill_proxy_enabled=False)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy.forge_posture"].status == SKIP

    def test_forge_posture_skips_when_the_proxy_is_on(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy.forge_posture"].status == SKIP


class TestSkillModelCredential:
    """`security.skill_model_credential` — ISSUE-409.

    The failure this covers was invisible to the deployment: `code_review`'s
    reviewers had no credential while the daemon's own brain worked, so the
    only way to find out was to run a review and read the envelope. Both halves
    are driven here with the daemon environment controlled, since a check that
    passed because the developer had a token exported is asserting nothing.
    """

    def _run(self, config):
        return _by_name(
            run_checks(config, only=("security.skill_model_credential",))
        )

    def test_wiring_ok_and_value_ok(self, make_config, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        results = self._run(make_config())
        wiring = results["security.skill_model_credential.wiring"]
        assert wiring.status == OK
        assert "code_review" in wiring.detail
        value = results["security.skill_model_credential.value"]
        assert value.status == OK
        # The name, never the value: a CheckResult is rendered into the boot
        # log and the admin dashboard.
        assert "CLAUDE_CODE_OAUTH_TOKEN" in value.detail
        assert "sk-ant-oat-test" not in value.detail

    def test_a_renamed_skill_fails_the_wiring(self, make_config, monkeypatch):
        """The drift guard, and the only one of these that catches a *re*-
        regression. `SKILL_MODEL_CALLERS` matches skill names against the index
        at task-build time, so a rename silently stops the injection — no
        import breaks and nothing on a deployment goes red."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        monkeypatch.setattr(
            doctor_executor, "SKILL_MODEL_CALLERS", frozenset({"code_reviewe"}),
        )
        results = self._run(make_config())
        wiring = results["security.skill_model_credential.wiring"]
        assert wiring.status == FAIL
        assert "code_reviewe" in wiring.detail
        # Nothing to say about a credential for a skill that does not resolve.
        assert "security.skill_model_credential.value" not in results

    def test_no_credential_fails(self, make_config, monkeypatch):
        """The positive control for the whole `.value` half.

        The markers are deleted explicitly rather than left to the suite's
        environment scrub: this is the one case that must reach FAIL, and every
        skip below is a way for it to stop doing so quietly.
        """
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN", "ISTOTA_TASK_ID",
                     "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"):
            monkeypatch.delenv(name, raising=False)
        results = self._run(make_config())
        assert results["security.skill_model_credential.wiring"].status == OK
        assert results["security.skill_model_credential.value"].status == FAIL

    def test_an_api_key_counts(self, make_config, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
        results = self._run(make_config())
        value = results["security.skill_model_credential.value"]
        assert value.status == OK
        assert "ANTHROPIC_API_KEY" in value.detail

    def test_the_value_half_skips_with_the_proxy_off(self, make_config, monkeypatch):
        """No injection and no strip: the CLI is re-exec'd with the daemon's
        own environment, so whatever authenticates the daemon authenticates it.
        Reporting a FAIL there would call a shape broken for a boundary it does
        not have."""
        from istota.config import SecurityConfig

        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        results = self._run(config)
        assert results["security.skill_model_credential.value"].status == SKIP

    @pytest.mark.parametrize(
        "marker",
        ["ISTOTA_TASK_ID", "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"],
    )
    def test_the_value_half_cannot_answer_from_a_task_env(
        self, make_config, monkeypatch, marker
    ):
        """The `.value` half reads `os.environ`, and doctor runs in six places.

        Four are the daemon and one is the web unit, but `istota doctor` is
        also a command a task can run, and a task's environment is the one
        ISSUE-390 deliberately strips the Claude credential out of, so absence
        there is by design and says nothing about the daemon. Reading it as the
        daemon's answer reported FAIL, and exited 1, about a deployment whose
        reviews worked.
        """
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN", "ISTOTA_TASK_ID",
                     "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(marker, "1")
        results = self._run(make_config())
        value = results["security.skill_model_credential.value"]
        assert value.status == SKIP
        # The detail says what was observed, which is the marker rather than a
        # verdict about the daemon.
        assert marker in value.detail

    def test_the_wiring_half_still_answers_from_a_task_env(
        self, make_config, monkeypatch
    ):
        """The drift guard reads the skill index and the config, never the
        environment, so it is sound wherever it runs, and it is the half that
        catches a renamed skill directory."""
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_TASK_ID", "4213")
        results = self._run(make_config())
        assert results["security.skill_model_credential.wiring"].status == OK

        monkeypatch.setattr(
            doctor_executor, "SKILL_MODEL_CALLERS", frozenset({"code_reviewe"}),
        )
        results = self._run(make_config())
        assert results["security.skill_model_credential.wiring"].status == FAIL

    def test_a_credential_present_in_a_task_env_still_counts(
        self, make_config, monkeypatch
    ):
        """Presence is positive evidence wherever it is read.

        `build_clean_env` copies the token out of the daemon's own environment
        into every task's, so seeing it inside a task says the daemon has it.
        Only *absence* is the unanswerable direction, so the skip must not
        outrank a credential that is right there.
        """
        monkeypatch.setenv("ISTOTA_TASK_ID", "4213")
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        results = self._run(make_config())
        value = results["security.skill_model_credential.value"]
        assert value.status == OK
        assert "sk-ant-oat-test" not in value.detail

    def test_an_unrelated_istota_variable_is_not_a_marker(
        self, make_config, monkeypatch
    ):
        """The negative control for the marker set: the daemon's own
        environment carries `ISTOTA_*` variables too, so a namespace test
        rather than named markers would swallow the FAIL everywhere."""
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN", "ISTOTA_TASK_ID",
                     "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/etc/istota/config.toml")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", "/etc/istota/admins")
        results = self._run(make_config())
        assert results["security.skill_model_credential.value"].status == FAIL

    def test_a_disabled_skill_skips(self, make_config, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = make_config()
        config.disabled_skills = ["code_review"]
        results = self._run(config)
        assert results["security.skill_model_credential.wiring"].status == SKIP
        assert "security.skill_model_credential.value" not in results


class TestSecretKey:
    """`security.secret_key` — the master Fernet key for the secrets store.

    The gap this closes was silent in the worst way: with no
    ``ISTOTA_SECRET_KEY`` every stored credential is unreachable, and
    `_native_key_holders` reports *0 holders* rather than an error, so the
    absence read as "nobody has configured a credential". The standalone
    wizard generated none at all for seven weeks and nothing anywhere said so.
    """

    NAME = "security.secret_key"

    def _run(self, config):
        return _by_name(run_checks(config, only=(self.NAME,)))[self.NAME]

    def _clear(self, monkeypatch):
        for name in ("ISTOTA_SECRET_KEY", "ISTOTA_TASK_ID", "ISTOTA_SANDBOXED",
                     "PRECOMMIT_SCANS_REQUIRED"):
            monkeypatch.delenv(name, raising=False)

    def test_absent_fails(self, make_config, monkeypatch):
        """The positive control for the whole check. The markers are deleted
        explicitly rather than left to the suite's scrub: this is the one case
        that must reach FAIL, and every skip below is a way for it to stop
        doing so quietly."""
        self._clear(monkeypatch)
        result = self._run(make_config())
        assert result.status == FAIL
        assert result.remedy
        # The consequence, not just the absence: it is the thing that made the
        # gap invisible.
        assert "credential" in result.detail.lower()

    def test_too_short_fails_naming_the_floor_and_the_length(
        self, make_config, monkeypatch
    ):
        self._clear(monkeypatch)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "changeme")
        result = self._run(make_config())
        assert result.status == FAIL
        assert str(secrets_store._MIN_KEY_LEN) in result.detail
        # The length is not the value.
        assert "8" in result.detail
        assert "changeme" not in result.detail
        assert "changeme" not in result.remedy

    def test_present_is_ok_and_never_reports_the_value(
        self, make_config, monkeypatch
    ):
        self._clear(monkeypatch)
        key = "0" * 31 + "sentinelkeymaterial" + "1" * 20
        monkeypatch.setenv("ISTOTA_SECRET_KEY", key)
        result = self._run(make_config())
        assert result.status == OK
        # Never the value, and never a prefix of it: a CheckResult is rendered
        # into the boot log and the admin dashboard.
        assert "sentinelkeymaterial" not in result.detail
        assert "sentinelkeymaterial" not in result.remedy
        assert key[:8] not in result.detail

    @pytest.mark.parametrize(
        "marker",
        ["ISTOTA_TASK_ID", "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"],
    )
    def test_absence_in_a_non_daemon_env_skips(
        self, make_config, monkeypatch, marker
    ):
        """`build_clean_env` strips this name from every task env by design and
        `_PROXY_LOOKUP_BLOCKED` blocks it from the proxy, so absence inside a
        task is guaranteed and says nothing about the daemon."""
        self._clear(monkeypatch)
        monkeypatch.setenv(marker, "1")
        result = self._run(make_config())
        assert result.status == SKIP
        assert marker in result.detail

    def test_presence_still_answers_from_a_non_daemon_env(
        self, make_config, monkeypatch
    ):
        """Ordering control: only absence is the unanswerable direction, so a
        key that is right there must not be swallowed by the skip."""
        self._clear(monkeypatch)
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
        assert self._run(make_config()).status == OK

    def test_an_unrelated_istota_variable_is_not_a_marker(
        self, make_config, monkeypatch
    ):
        """Negative control for the marker set: the daemon's own environment
        carries `ISTOTA_*` variables, so a namespace test would skip
        everywhere and the check could never fail."""
        self._clear(monkeypatch)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/etc/istota/config.toml")
        assert self._run(make_config()).status == FAIL

    def test_the_standalone_remedy_names_the_env_file(
        self, make_config, monkeypatch
    ):
        from istota.config import WebConfig

        self._clear(monkeypatch)
        config = make_config(web=WebConfig(auth="none"))
        assert config.is_standalone is True
        result = self._run(config)
        assert result.status == FAIL
        assert "istota.env" in result.remedy

    def _standalone(self, make_config, tmp_path):
        from istota.config import WebConfig

        config_path = tmp_path / "cfg" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("")
        return make_config(web=WebConfig(auth="none"), config_path=config_path)

    def test_a_key_only_in_the_env_file_is_ok(
        self, make_config, monkeypatch, tmp_path
    ):
        """`cmd_serve` is the only thing in the tree that sources istota.env,
        so `istota doctor` in an operator's shell is a process that carries
        none of the markers *and* has never read the file. Taking its own
        environment as the answer failed a correctly configured install and
        told the operator to add a line already in the file."""
        self._clear(monkeypatch)
        config = self._standalone(make_config, tmp_path)
        env_file = Path(config.config_path).parent / "istota.env"
        env_file.write_text("ISTOTA_SECRET_KEY=" + "h" * 64 + "\n")
        result = self._run(config)
        assert result.status == OK
        assert "istota.env" in result.detail
        assert "h" * 8 not in result.detail

    def test_a_short_key_in_the_env_file_fails(
        self, make_config, monkeypatch, tmp_path
    ):
        self._clear(monkeypatch)
        config = self._standalone(make_config, tmp_path)
        env_file = Path(config.config_path).parent / "istota.env"
        env_file.write_text("ISTOTA_SECRET_KEY=tooshort\n")
        result = self._run(config)
        assert result.status == FAIL
        assert str(secrets_store._MIN_KEY_LEN) in result.detail
        assert "tooshort" not in result.detail

    def test_an_env_file_without_the_key_still_fails(
        self, make_config, monkeypatch, tmp_path
    ):
        """The positive control for the arm above: the fallback must not turn
        a genuinely missing key into a pass just because a file is there."""
        self._clear(monkeypatch)
        config = self._standalone(make_config, tmp_path)
        env_file = Path(config.config_path).parent / "istota.env"
        env_file.write_text("ISTOTA_WEB_INSECURE_COOKIES=1\n")
        assert self._run(config).status == FAIL

    def test_the_remedy_names_the_configs_own_env_file(
        self, make_config, monkeypatch, tmp_path
    ):
        """`istota setup -c` puts the env file beside whatever config it was
        given, so a hardcoded default sends the operator to a path that does
        not exist on their install."""
        self._clear(monkeypatch)
        config = self._standalone(make_config, tmp_path)
        result = self._run(config)
        assert result.status == FAIL
        assert str(Path(config.config_path).parent / "istota.env") in result.remedy

    def test_the_floor_is_read_from_the_secrets_store(
        self, make_config, monkeypatch
    ):
        """The drift guard. A second copy of the floor is exactly what this
        check exists to catch, so a raised floor must move the verdict here
        without any edit to doctor."""
        self._clear(monkeypatch)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 40)
        assert self._run(make_config()).status == OK
        monkeypatch.setattr(secrets_store, "_MIN_KEY_LEN", 64)
        assert self._run(make_config()).status == FAIL

    def test_scope_is_deployment(self):
        """A key is a property of an install; a bare `docker run` has none and
        would report a missing key about nothing."""
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT

    # --- What an absent key actually costs, which is not one answer -------
    #
    # `[defaults] istota_secret_key` documents an empty value as *disabling*
    # the secrets store, so a bare-metal deployment can legitimately run
    # without one — and a check that pages that operator on every scheduler
    # sweep is a warning wearing a failure's label. But the same absence on a
    # deployment that has stored something is the original defect: those rows
    # cannot be decrypted and every connected service built on them reports as
    # unconfigured rather than as broken.
    #
    # So the discriminator is the `secrets` table itself, and the split is
    # deliberately asymmetric: only an *observed* empty table softens the
    # verdict. A table that could not be read has not established that nothing
    # is stored, so it keeps the FAIL — the same "never pass on a question you
    # could not settle" rule the sandbox and session-log checks follow.

    def _db_with_secrets(self, config, rows: int):
        """Create the framework DB and put `rows` rows in `secrets`."""
        import sqlite3

        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id TEXT NOT NULL, service TEXT NOT NULL,"
                " key TEXT NOT NULL, encrypted_value BLOB NOT NULL,"
                " UNIQUE(user_id, service, key))"
            )
            for n in range(rows):
                conn.execute(
                    "INSERT INTO secrets (user_id, service, key, encrypted_value)"
                    " VALUES (?, ?, ?, ?)",
                    ("someone", "garmin", f"key{n}", b"ciphertext"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_absence_with_stored_credentials_fails_naming_the_count(
        self, make_config, monkeypatch
    ):
        """The original defect, stated as a number. Two rows exist and neither
        can be decrypted; `_native_key_holders` would report nought holders and
        the deployment would read as one nobody had configured."""
        self._clear(monkeypatch)
        config = make_config()
        self._db_with_secrets(config, 2)
        result = self._run(config)
        assert result.status == FAIL
        assert "2" in result.detail
        assert result.remedy

    def test_absence_with_an_observed_empty_store_warns(
        self, make_config, monkeypatch
    ):
        """The documented opt-out. Nothing is stored, so nothing is currently
        unreachable — but every future `istota secret ensure` will raise, so
        this is reported rather than passed."""
        self._clear(monkeypatch)
        config = make_config()
        self._db_with_secrets(config, 0)
        result = self._run(config)
        assert result.status == WARN
        assert result.remedy

    def test_an_empty_store_does_not_page(self, make_config, monkeypatch):
        """The point of the WARN. `verdict` is False on any FAIL and unmoved by
        a WARN, so this is what keeps a deliberate posture out of the daemon's
        boot alert and off every scheduler sweep."""
        self._clear(monkeypatch)
        config = make_config()
        self._db_with_secrets(config, 0)
        healthy, _ = doctor.verdict([self._run(config)])
        assert healthy is True

    def test_a_populated_store_does_page(self, make_config, monkeypatch):
        """The control for the test above: the softening must not reach the
        case the check was written for."""
        self._clear(monkeypatch)
        config = make_config()
        self._db_with_secrets(config, 1)
        healthy, _ = doctor.verdict([self._run(config)])
        assert healthy is False

    def test_an_unreadable_database_keeps_the_failure(
        self, make_config, monkeypatch
    ):
        """`make_config` names a database that does not exist. Absence of the
        table is not evidence of an empty store, so the verdict stays FAIL —
        softening on an unanswered question is how a check stops working."""
        self._clear(monkeypatch)
        config = make_config()
        assert not Path(config.db_path).exists()
        assert self._run(config).status == FAIL

    def test_the_row_count_query_leaves_no_sidecars_and_no_database(
        self, make_config, monkeypatch
    ):
        """Read-only, like every other database-touching check here. A
        read-write open materializes `-wal`/`-shm` and, against a missing
        file, creates a zero-byte database that later reads as corruption
        rather than as absence — and a diagnostic run as root beside a stopped
        daemon would leave all of it owned by the wrong user."""
        self._clear(monkeypatch)
        config = make_config()
        self._run(config)
        db_path = Path(config.db_path)
        assert not db_path.exists()
        assert not db_path.with_suffix(db_path.suffix + "-wal").exists()
        assert not db_path.with_suffix(db_path.suffix + "-shm").exists()

    def test_a_present_key_never_reads_the_database(
        self, make_config, monkeypatch
    ):
        """Ordering: the store's rows only matter once the key is absent. A
        deployment with a usable key is OK whatever is in the table, and the
        check must not pay for a query to say so."""
        self._clear(monkeypatch)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
        config = make_config()
        self._db_with_secrets(config, 3)
        assert self._run(config).status == OK


class TestSandboxCredentials:
    """`security.sandbox_credentials` — ISSUE-396.

    `load_config` warns on the sandbox-on/proxy-off pairing (ISSUE-393), but the
    boot log is read once at install. The Health pane and `istota doctor` are
    the surfaces an operator returns to, and before this check they reported the
    pairing as a bare `SKIP` on `security.skill_proxy` carrying only a
    restatement of the setting.

    The class holds two things apart: that the finding fires at all, and that it
    keeps firing on the shape where the sandbox turns out not to be in force —
    which is the shape a check written around `effective_sandboxing` would most
    naturally go quiet on, and the one where the operator has neither half of
    what they asked for.
    """

    NAME = "security.sandbox_credentials"

    def _config(self, make_config, *, sandbox, proxy):
        from istota.config import SecurityConfig

        return make_config(
            security=SecurityConfig(
                sandbox_enabled=sandbox, skill_proxy_enabled=proxy
            )
        )

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    @pytest.fixture
    def bwrap_works(self, monkeypatch):
        """Answer the availability axis rather than letting the host answer it.

        `effective_sandboxing` is False on any non-Linux host, so without this
        every "sandbox in force" case would take the bwrap-unavailable branch on
        a developer machine. Both the function's probe and the memo
        `effective_sandboxing_if_known` reads, since the check reaches them by
        two routes and the memo is a process global the suite's other tests
        also set — see `TestSessionLogDir._bwrap_works_here`.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_checked", True)

    # -- the regression assertion ------------------------------------------

    def test_the_pairing_warns_and_names_the_credentials(
        self, make_config, bwrap_works
    ):
        """The finding ISSUE-396 was filed for.

        Before the check existed this name produced no result at all, so the
        `only=` run came back empty and the indexing below raised — which is
        the negative control for the whole class.
        """
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "readable by the model" in r.detail

    def test_the_warning_carries_a_remedy(self, make_config, bwrap_works):
        """`TestRegistry.test_warn_and_fail_carry_a_remedy` cannot see this one.

        That guard runs over `_dev_config`, which leaves the proxy on, so this
        WARN never fires under it. A finding an operator cannot act on is a
        line of noise, and the image tier asserts the same property over
        `--scope image`, which this check is deliberately outside of.
        """
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert r.remedy.strip()
        assert "skill_proxy_enabled" in r.remedy

    # -- and the shapes it must stay quiet on ------------------------------

    def test_both_switches_off_is_silent(self, make_config):
        """The single-user install's deliberate trust decision.

        `setup_wizard` writes the pair. The task then runs unconfined as the
        daemon user and can read `config.toml`, the secrets database or
        `/proc/<daemon>/environ`, so removing a variable from its environment
        is decorative rather than a boundary. ISSUE-393 put this shape out of
        scope for the warning and it is out of scope here for the same reason.
        """
        r = self._run(self._config(make_config, sandbox=False, proxy=False))

        assert r.status == SKIP
        assert "unconfined by design" in r.detail

    def test_the_proxy_being_on_is_ok_not_a_warning(self, make_config):
        # No `bwrap_works`: this branch returns before anything reaches
        # `executor`, and taking the fixture would imply an availability
        # dependency the code does not have.
        r = self._run(self._config(make_config, sandbox=True, proxy=True))

        assert r.status == OK
        assert "injected per call" in r.detail

    def test_the_sandbox_being_off_skips_even_with_the_proxy_on(self, make_config):
        r = self._run(self._config(make_config, sandbox=False, proxy=True))

        assert r.status == SKIP

    # -- the in-force clause, which is a clause and not a condition --------

    def test_it_says_the_sandbox_is_in_force(self, make_config, bwrap_works):
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert "the sandbox is in force" in r.detail

    def test_it_still_warns_where_bubblewrap_does_not_work(
        self, make_config, monkeypatch
    ):
        """The shape a check gated on `effective_sandboxing` would go quiet on.

        The shipped Docker stack grants neither `seccomp:unconfined` nor
        `systempaths=unconfined`, so the probe fails and every task runs
        unconfined while `sandbox_enabled` still reads true. The credentials are
        in the task environment on the strength of `skill_proxy_enabled` alone
        — `_split_credential_env` runs only inside the proxy branch of
        `execute_task` — so the finding is established whatever bubblewrap does,
        and the operator there has neither half of what they configured.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "runtime.bwrap" in r.detail

    def test_an_unprobed_run_says_the_sandbox_state_is_unestablished(
        self, make_config, monkeypatch
    ):
        """`probe=False` with a cold memo must not assert either answer.

        `runtime.session_log_dir` set the precedent: a check whose subject is a
        boundary may not report a protection it did not look for. Here only the
        clause is in doubt — the WARN itself still stands.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_checked", None)
        r = self._run(
            self._config(make_config, sandbox=True, proxy=False), probe=False
        )

        assert r.status == WARN
        assert "unestablished" in r.detail

    def test_a_broken_availability_lookup_still_warns(self, make_config, monkeypatch):
        """A diagnostic must not raise, and must not lose its finding either."""
        from istota import executor

        def _boom(config):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(executor, "effective_sandboxing", _boom)
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "unestablished" in r.detail

    # -- what registration must not change ---------------------------------

    def test_it_does_not_collide_with_the_load_config_warning(self, make_config):
        """`tests/test_config.py` filters `caplog` on the warning's opening phrase.

        The two messages say nearly the same thing, so the doctor detail naming
        the settings in the same order would be a substring of that filter. It
        breaks nothing while this check stays off the config-load path, but it
        would turn those filters from an assertion into an `any(...)` over two
        records the day it moved onto it.
        """
        r = self._run(self._config(make_config, sandbox=True, proxy=False))

        assert "sandbox_enabled with skill_proxy_enabled = false" not in r.detail
        # Both settings still named, so an operator greps either one and finds it.
        assert "skill_proxy_enabled" in r.detail
        assert "sandbox_enabled" in r.detail

    def test_it_is_deployment_scoped(self):
        """`--scope image` must not select it.

        The image tier asserts no check fails and every WARN carries a remedy
        over `doctor --scope image`. This reports a posture an operator chose in
        a rendered config, which is not a property of the image.
        """
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT

    def test_it_does_not_run_inside_load_config(self):
        """Two reasons, both consequences of `CONFIG_LOAD_CHECKS` being hot.

        It reaches `istota.executor` for the bwrap probe, which
        `TestConfigLoadPathStaysCheap` forbids on that path; and
        `_validate_forge_clis` logs every WARN those checks return, which would
        print this finding beside the ISSUE-393 warning already emitted a few
        lines above it, on every `load_config`.
        """
        from istota.config import CONFIG_LOAD_CHECKS

        assert self.NAME not in CONFIG_LOAD_CHECKS

    def test_the_skill_proxy_check_is_unchanged(self, make_config):
        """ISSUE-396 considered widening the existing SKIP and rejected it.

        A `SKIP` reads as "not applicable here", which is the wrong status for a
        live exposure. The finding moved to a result of its own instead, and
        this pins that the old one kept its status rather than being quietly
        repurposed.
        """
        config = self._config(make_config, sandbox=True, proxy=False)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))

        assert results["security.skill_proxy"].status == SKIP
        assert self.NAME not in results


class TestSandboxEffective:
    """`security.sandbox_effective` — the flag versus what the deployment got.

    `runtime.bwrap` answers "is bubblewrap installed and runnable", which is a
    property of the *image* and is why it stays `IMAGE`-scoped. Whether a
    namespace can actually be created here is a property of the *deployment*:
    the shipped `docker/docker-compose.yml` grants neither `seccomp:unconfined`
    nor `systempaths=unconfined`, so the probe fails and every task runs
    unsandboxed while `sandbox_enabled` still reads true (ISSUE-381). Both
    hand-rolled health probes report `Sandbox (bwrap): PASS` on exactly that
    deployment.

    Two properties of the class are load-bearing rather than decorative. The
    scope exclusion is asserted directly, because folding this answer onto
    `runtime.bwrap` would turn `tests/image/test_istota_image.py` red for a
    reason that has nothing to do with the image under test. And the
    `probe=False` case is asserted to be neither a pass nor a FAIL: a boundary
    check must not report a protection it did not look for, nor assert an
    exposure it did not observe. `runtime.session_log_dir` set that precedent.
    """

    NAME = "security.sandbox_effective"

    def _config(self, make_config, *, sandbox=True):
        from istota.config import SecurityConfig

        return make_config(security=SecurityConfig(sandbox_enabled=sandbox))

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    # -- the three answers under a probing run -----------------------------

    def test_a_namespace_that_cannot_be_created_fails(self, make_config, monkeypatch):
        """The ISSUE-381 shape, and the finding this check exists for.

        Before the check existed this name produced no result at all, so the
        `only=` run came back empty and `_run`'s indexing raised — the negative
        control for the whole class.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config))

        assert r.status == FAIL
        assert "unsandboxed" in r.detail
        assert r.remedy.strip()

    def test_the_remedy_names_both_halves(self, make_config, monkeypatch):
        """An operator meets this on one of two hosts and the fix differs.

        A container wants the two `security_opt` settings; a bare-metal host
        wants unprivileged user namespaces enabled. A remedy naming only one
        sends half the readers to the wrong file.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config))

        assert "seccomp:unconfined" in r.remedy
        assert "systempaths=unconfined" in r.remedy
        assert "unprivileged_userns_clone" in r.remedy

    def test_a_working_namespace_is_ok(self, make_config, monkeypatch):
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_checked", True)
        r = self._run(self._config(make_config))

        assert r.status == OK
        assert not r.remedy

    def test_the_sandbox_being_off_skips(self, make_config):
        """The operator turned it off and knows. `check_bwrap` SKIPs here too.

        No availability patch: this branch must return before anything reaches
        `executor`, and taking one would imply a dependency the code does not
        have.
        """
        r = self._run(self._config(make_config, sandbox=False))

        assert r.status == SKIP
        assert "sandbox_enabled" in r.detail

    # -- the third state, which is the reason it is not a bool -------------

    def test_an_unprobed_run_with_a_cold_memo_settles_nothing(
        self, make_config, monkeypatch
    ):
        """`probe=False` may not spawn, so a cold memo has no answer to give.

        The requirement is symmetric and both halves are asserted: not a FAIL,
        because nothing observed an exposure; and not a silent OK, because
        nothing observed the boundary either. The detail has to say so.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_checked", None)
        r = self._run(self._config(make_config), probe=False)

        assert r.status != FAIL
        assert r.status != OK
        assert "not probed on this run" in r.detail
        assert "unknown" in r.detail
        assert r.remedy.strip()

    def test_an_unprobed_run_with_a_warm_memo_answers(self, make_config, monkeypatch):
        """The daemon probes at start-up, so `probe=False` there is not blind.

        Without this, "unestablished" would be the answer on the one process
        whose start-up report and hourly sweep are the check's main consumers.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config), probe=False)

        assert r.status == FAIL
        assert "unsandboxed" in r.detail

    def test_probe_false_spawns_nothing(self, make_config, monkeypatch):
        """The memo is read directly rather than through `_bwrap_available`.

        `TestRegistry.test_probe_false_spawns_no_subprocess` sweeps the whole
        registry for this, but only with whatever memo state the worker happens
        to carry. A cold memo is the state that would spawn.
        """
        from istota import executor

        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(executor, "_bwrap_checked", None)
        monkeypatch.setattr(subprocess, "run", _spy)
        self._run(self._config(make_config), probe=False)

        assert spawns == []

    def test_a_broken_availability_lookup_does_not_raise(self, make_config, monkeypatch):
        """A diagnostic must not raise, and must not pass on a question it
        could not ask either."""
        from istota import executor

        def _boom(config):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(executor, "effective_sandboxing", _boom)
        r = self._run(self._config(make_config))

        assert r.status != OK
        assert "the check itself raised" not in r.detail
        assert "could not be determined" in r.detail
        assert "unknown" in r.detail

    # -- run from inside a task's own sandbox ------------------------------

    def test_a_probe_from_inside_a_sandbox_settles_nothing(
        self, make_config, monkeypatch
    ):
        """The reported defect, and the reason `_deployment_sandboxing` exists.

        A task's own namespace is built with `--disable-userns`, so bwrap in
        there fails on the nesting depth whatever the deployment can do. This
        check read that as the deployment's answer: `istota doctor` run by a
        task on the production host reported every task unsandboxed, and exited
        1, from inside a task whose database masks were demonstrably in place.

        The availability patch is what makes it a reproduction rather than a
        tautology — it is the answer a real nested probe gives, and against the
        pre-change code this asserted a FAIL.
        """
        from istota import executor

        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config))

        assert r.status == SKIP
        assert "ISTOTA_SANDBOXED" in r.detail
        assert "unsandboxed" not in r.detail

    def test_it_asks_nothing_from_inside_a_sandbox(self, make_config, monkeypatch):
        """The marker is read before either route to the availability answer.

        Not a performance point: a nested probe spawns a `bwrap` that is going
        to fail, and a check that ran it and then discarded the answer would
        keep the failure one refactor away from being read again.
        """
        asked = self._record_availability(monkeypatch)
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")

        r = self._run(self._config(make_config))

        assert r.status == SKIP
        assert asked == []

    def test_a_task_on_an_unconfined_deployment_still_fails(
        self, make_config, monkeypatch
    ):
        """The boundary control on the skip, and the reason it is not
        `_non_daemon_env_markers`.

        `ISTOTA_SANDBOXED` is set only where the sandbox was really in force, so
        a task on the ISSUE-381 shape carries `ISTOTA_TASK_ID` without it and its
        probe is a valid one. Skipping on the whole marker set would hide the one
        deployment this check exists to report from the surface an operator
        actually reaches — a task run of `istota doctor`.
        """
        from istota import executor

        monkeypatch.setenv("ISTOTA_TASK_ID", "1234")
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        r = self._run(self._config(make_config))

        assert r.status == FAIL
        assert "unsandboxed" in r.detail

    # -- the scope guarantee, which is the whole reason for a separate check -

    def test_scope_image_never_selects_it(self, make_config, monkeypatch):
        """`tests/image/test_istota_image.py::test_no_check_fails` runs
        `istota doctor --json --scope image` in a bare `docker run` with no
        `security_opt`, and `cmd_doctor` passes `probe=True`. A capability arm
        on `runtime.bwrap` would turn that red across all three shapes.

        Asserted before invocation, not after: the availability lookup is the
        first thing the check reaches past the `sandbox_enabled` gate, so a
        recorder there catches a run that happened and was filtered out of the
        results afterwards.
        """
        asked = self._record_availability(monkeypatch)
        config = self._config(make_config)

        results = run_checks(config, scope=IMAGE)

        assert self.NAME not in {r.name for r in results}
        assert asked == [], "the check ran and was discarded rather than filtered"

    def test_the_scope_exclusion_control(self, make_config, monkeypatch):
        """Positive control for the test above.

        Both of its assertions pass against a check that was deleted, so the
        unscoped run has to show the same recorder firing and the same name
        present.
        """
        asked = self._record_availability(monkeypatch)
        config = self._config(make_config)

        results = run_checks(config, only=(self.NAME,))

        assert self.NAME in {r.name for r in results}
        assert asked == ["probed"]

    @staticmethod
    def _record_availability(monkeypatch):
        """Record every route the check has to the availability answer.

        Both of them, which is the point: the check calls `effective_sandboxing`
        under `probe=True` and `effective_sandboxing_if_known` under
        `probe=False`, so a recorder on one leaves the other blind and a
        filtering test built on it would pass while the check ran.
        """
        from istota import executor

        asked = []

        def _probed(config):
            asked.append("probed")
            return False

        def _memo(config):
            asked.append("memo")
            return False

        monkeypatch.setattr(executor, "effective_sandboxing", _probed)
        monkeypatch.setattr(executor, "effective_sandboxing_if_known", _memo)
        return asked

    def test_the_recorder_sees_the_unprobed_route_too(self, make_config, monkeypatch):
        """Positive control for `_record_availability`'s second half.

        Without this, the `probe=False` recorder is an untested claim and the
        filtering tests above rest on it.
        """
        asked = self._record_availability(monkeypatch)

        self._run(self._config(make_config), probe=False)

        assert asked == ["memo"]

    def test_it_is_registered_deployment_scoped(self):
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT
        assert self.NAME in {name for name, _ in CHECKS}

    def test_it_is_neither_deep_nor_live(self):
        """`effective_sandboxing` memoizes its probe and the daemon has already
        paid for it at start-up, so this needs no opt-in axis. `DEEP_CHECKS`
        must also keep exactly one member — `web_app._doctor_deep_timeout`
        budgets for its contents and cannot see a change here."""
        assert self.NAME not in DEEP_CHECKS
        assert self.NAME not in LIVE_CHECKS
        assert DEEP_CHECKS == frozenset({"sandbox.masks"})

    def test_the_bwrap_check_is_unchanged(self, make_config, tmp_path, monkeypatch):
        """The capability answer moved to a check of its own rather than onto
        this one.

        An installed, runnable bwrap on a host where the namespace is refused —
        the ISSUE-381 shape exactly — must still leave `runtime.bwrap` `OK` and
        `IMAGE`-scoped. That is what the image tier depends on, and softening
        it here is the failure `doctor.py`'s own scope comment warns about, in
        reverse.
        """
        from istota import executor

        fake = _fake_bin(tmp_path / "bin" / "bwrap", "bubblewrap 0.11.0")
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None
        )
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(executor, "_bwrap_checked", False)
        results = _by_name(
            run_checks(self._config(make_config), only=("runtime.bwrap", self.NAME))
        )

        assert results["runtime.bwrap"].status == OK
        assert results["runtime.bwrap"].scope == IMAGE
        assert doctor.CHECK_SCOPES["runtime.bwrap"] == IMAGE
        # And the deployment-scoped one is where the finding landed instead.
        assert results[self.NAME].status == FAIL


class TestForgeGating:
    """Every `developer.*` check inherits today's gating. Without this, a
    tokenless developer-skill deployment goes from silent to alerting."""

    def test_skips_when_the_skill_is_off(self, make_config):
        results = run_checks(make_config(), only=("developer.",))
        assert results
        assert all(r.status == SKIP for r in results)

    def test_skips_without_repos_dir(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, repos_dir="")
        results = run_checks(config, only=("developer.",))
        assert all(r.status == SKIP for r in results)

    def test_binary_checks_skip_without_a_token(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        results = _by_name(run_checks(config, only=("developer.",)))
        assert results["developer.forge_binaries.gh"].status == SKIP
        assert results["developer.forge_config_drift.gh"].status == SKIP

    def test_policy_check_runs_without_a_token(self, make_config, tmp_path):
        """`forge_cli_permit` validation is about the config file, not about
        whether a credential happens to be wired yet."""
        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        results = _by_name(run_checks(config, only=("developer.forge_policy",)))
        assert results["developer.forge_policy"].status != SKIP


class TestForgeBinaries:
    def test_present_and_executable_is_ok(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == OK
        assert results["developer.forge_binaries.glab"].status == OK

    def test_missing_binary_fails(self, make_config, tmp_path):
        """The ISSUE-263 shape: `os.execve` onto a path that does not exist."""
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == FAIL
        assert str(tmp_path / "bin" / "gh") in results["developer.forge_binaries.gh"].detail
        assert results["developer.forge_binaries.gh"].remedy

    def test_a_binary_that_exits_nonzero_fails(self, make_config, tmp_path):
        """The half of this check that actually runs the thing. `check_forge_versions`
        used to do this too and was deleted as redundant, which is only true while
        `_binary_status` keeps executing `--version` under probe — so pin it here
        rather than leaving the property resting on a docstring."""
        _fake_bin(tmp_path / "bin" / "gh", "boom", exit_code=1)
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == FAIL
        assert "exited 1" in results["developer.forge_binaries.gh"].detail
        assert results["developer.forge_binaries.glab"].status == OK

    def test_a_binary_is_not_executed_when_probe_is_off(self, make_config, tmp_path):
        """Nothing may shell out on the probe-disabled path; an operator reading
        the result has to be able to tell that nothing ran."""
        _fake_bin(tmp_path / "bin" / "gh", "boom", exit_code=1)
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(
            run_checks(config, only=("developer.forge_binaries",), probe=False)
        )
        assert results["developer.forge_binaries.gh"].status == OK
        assert "not executed" in results["developer.forge_binaries.gh"].detail

    def test_present_but_not_executable_fails(self, make_config, tmp_path):
        path = tmp_path / "bin" / "gh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o644)
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == FAIL
        assert "not executable" in results["developer.forge_binaries.gh"].detail

    def test_one_result_per_binary(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        names = {r.name for r in run_checks(config, only=("developer.forge_binaries",))}
        assert names == {"developer.forge_binaries.gh", "developer.forge_binaries.glab"}

    def test_is_image_scoped(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, only=("developer.forge_binaries",)):
            assert r.scope == IMAGE


class TestForgeConfigDrift:
    """`_resolve_real_bin`'s fallback is correct and load-bearing, and it hides
    the stale-config condition. This check restores that signal."""

    def test_configured_path_that_exists_and_resolves_to_itself_is_ok(
        self, make_config, tmp_path
    ):
        _fake_bin(tmp_path / "bin" / "gh")
        _fake_bin(tmp_path / "bin" / "glab")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        assert results["developer.forge_config_drift.gh"].status == OK

    def test_stale_configured_path_warns_naming_both(self, make_config, tmp_path, monkeypatch):
        """The retained-volume upgrade: `config.toml` predates the binaries, so
        resolution falls through to the image location."""
        from istota.skills import developer as developer_skill

        shipped = _fake_bin(tmp_path / "image" / "gh")
        stale = "/usr/local/bin/gh"
        original_exists = doctor.Path.exists
        monkeypatch.setattr(
            doctor.Path,
            "exists",
            lambda path: False if str(path) == stale else original_exists(path),
        )
        monkeypatch.setattr(
            developer_skill.os.path,
            "exists",
            lambda path: False if str(path) == stale else original_exists(doctor.Path(path)),
        )
        monkeypatch.setitem(developer_skill._IMAGE_BIN, "gh", str(shipped))
        config = _dev_config(make_config, tmp_path, gh_bin_path=str(stale))
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        drift = results["developer.forge_config_drift.gh"]
        assert drift.status == WARN
        assert stale in drift.detail
        assert str(shipped) in drift.detail
        assert drift.remedy

    def test_an_explicit_missing_path_does_not_contradict_itself(self, make_config, tmp_path):
        """`_resolve_real_bin` returns an explicitly chosen path as given, so
        configured == resolved while nothing exists there. One combined message
        would read "x but the wrapper will exec x"."""
        config = _dev_config(make_config, tmp_path, gh_bin_path=str(tmp_path / "nowhere" / "gh"))
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        drift = results["developer.forge_config_drift.gh"]
        assert drift.status == WARN
        assert "nothing exists there" in drift.detail
        assert "but the wrapper will exec" not in drift.detail

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, only=("developer.forge_config_drift",)):
            assert r.status != FAIL


class TestWrapperShadowing:
    """The question is "is something *unexpected* reachable by name", not "is a
    real forge binary on PATH" — the latter is true by design on the Ansible
    shape, which is what production runs."""

    def test_nothing_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        assert results["developer.forge_wrapper_shadowing.gh"].status == OK

    def test_an_unexpected_real_binary_on_path_fails(self, make_config, tmp_path, monkeypatch):
        """Someone apt-installed gh onto the image shape: the model's shell finds
        it before the per-task wrapper and skips the policy and the injection."""
        real = tmp_path / "path" / "gh"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
        real.chmod(0o755)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(real) if name == "gh" else None
        )
        # The deployment resolved something else entirely.
        _fake_bin(tmp_path / "bin" / "gh")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == FAIL
        assert str(real) in gh.detail
        assert gh.remedy

    def test_the_ansible_shape_is_ok(self, make_config, tmp_path, monkeypatch):
        """The role installs the real binaries into /usr/bin and renders those
        paths into config.toml, so `which` finding them is correct. A FAIL here
        would alert the admin allowlist on every boot of a healthy host."""
        installed = _fake_bin(tmp_path / "usr-bin" / "gh")
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(installed) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path, gh_bin_path=str(installed))
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == OK
        assert "Ansible shape" in gh.detail

    def test_the_real_wrapper_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        """Copied from `forge_cli.py` itself, not hand-written to match.

        The wrapper the daemon writes per task *is* a verbatim copy of that
        file, so a hand-written stand-in could satisfy the identity test while
        the real article failed it — which is how the previous docstring-prose
        matching would have broken on a reworded comment.
        """
        import shutil as _shutil

        from istota import forge_cli

        wrapper = tmp_path / "path" / "gh"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy(forge_cli.__file__, wrapper)
        wrapper.chmod(0o755)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(wrapper) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        assert results["developer.forge_wrapper_shadowing.gh"].status == OK

    def test_the_sentinel_is_near_the_top_of_the_wrapper(self):
        """`_looks_like_the_wrapper` reads only the file's head."""
        from istota import forge_cli

        head = Path(forge_cli.__file__).read_bytes()[:8192]
        assert doctor._WRAPPER_SENTINEL in head

    def test_the_devbox_copy_carries_the_sentinel_too(self):
        """The devbox image ships a byte-identical copy under another name; it
        is the one shape where the wrapper really is on PATH."""
        copy = Path(__file__).resolve().parents[1] / "docker/devbox/lib/istota_forge_cli.py"
        assert doctor._WRAPPER_SENTINEL in copy.read_bytes()[:8192]

    @pytest.mark.requires_dac
    def test_an_unreadable_binary_is_unknown_not_a_failure(
        self, make_config, tmp_path, monkeypatch
    ):
        """A permission bit is not evidence of a shadowing real binary."""
        opaque = tmp_path / "path" / "gh"
        opaque.parent.mkdir(parents=True, exist_ok=True)
        opaque.write_text("whatever")
        opaque.chmod(0o311)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(opaque) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path)
        try:
            results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        finally:
            opaque.chmod(0o644)
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == WARN
        assert gh.remedy


class TestForgePolicy:
    def test_clean_permits_are_ok(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, forge_cli_permit=[])
        r = run_checks(config, only=("developer.forge_policy",))[0]
        assert r.status == OK

    def test_unmatched_permit_warns_naming_the_entry(self, make_config, tmp_path):
        config = _dev_config(
            make_config, tmp_path, forge_cli_permit=["gh not-a-real-verb-at-all"]
        )
        r = run_checks(config, only=("developer.forge_policy",))[0]
        assert r.status == WARN
        assert "not-a-real-verb-at-all" in r.detail

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, forge_cli_permit=["gh nonsense"])
        assert run_checks(config, only=("developer.forge_policy",))[0].status != FAIL


class TestGitlabReviewer:
    """ISSUE-289. The setting was silent in both directions: a numeric value
    produced `failed to find user by name` inside the task, and an unset one
    produced nothing at all. Neither reached the operator, so every MR for
    weeks opened with no reviewer on it. A boot-time line is the only thing
    that closes that loop."""

    def test_a_username_is_ok(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="reviewer-user")
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == OK

    def test_an_all_digits_username_warns_naming_the_value(self, make_config, tmp_path):
        """`glab mr create --reviewer` resolves by username. A GitLab username
        cannot be all digits, so this value can only be the numeric user id."""
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="1234567")
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN
        assert "1234567" in r.detail
        assert r.remedy

    def test_the_old_id_key_alone_warns(self, make_config, tmp_path):
        """The upgrade shape. A host that set only `gitlab_reviewer_id` used to
        get a reviewer flag built from it; it now gets none, and the operator
        has no other way to find out."""
        config = _dev_config(
            make_config, tmp_path, gitlab_reviewer="", gitlab_reviewer_id="1234567"
        )
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN
        assert "gitlab_reviewer" in r.detail
        assert "username" in r.remedy

    def test_a_username_left_in_the_old_key_is_named_as_one(self, make_config, tmp_path):
        """The narrow population the remedy would otherwise mislead.

        `gitlab_reviewer_id` was documented as a username for one day before
        ISSUE-289 was filed, so a host that followed those docs has a working
        username sitting in the retired key. Telling that operator the value is
        "the id" and to go find the username sends them looking for something
        they already have.
        """
        config = _dev_config(
            make_config, tmp_path, gitlab_reviewer="", gitlab_reviewer_id="reviewer-user"
        )
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN
        assert "reviewer-user" in r.remedy
        assert "copy it verbatim" in r.remedy

    def test_a_non_string_value_does_not_crash_the_check(self, make_config, tmp_path):
        """TOML types its scalars, so an unquoted `gitlab_reviewer = 1234567`
        arrives as an int. `run_checks` reports a raising check as FAIL — the
        one status that alerts — so a crash here would page the operator in
        precisely the misconfiguration the check exists to describe."""
        config = _dev_config(make_config, tmp_path, gitlab_reviewer=1234567)
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN
        assert "user id" in r.detail

    def test_a_non_string_value_in_the_old_key_does_not_crash_either(
        self, make_config, tmp_path
    ):
        config = _dev_config(
            make_config, tmp_path, gitlab_reviewer="", gitlab_reviewer_id=1234567
        )
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN

    def test_a_value_with_whitespace_warns(self, make_config, tmp_path):
        """The recipe expands `--reviewer $GITLAB_REVIEWER` unquoted, so a
        display name hands `glab` a stray positional argument."""
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="First Last")
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status == WARN
        assert "whitespace" in r.detail

    def test_non_ascii_digits_are_not_called_a_user_id(self, make_config, tmp_path):
        """`str.isdigit` is Unicode-wide. Arabic-Indic digits are not a GitLab
        user id, so the WARN must not claim they are — it may still warn, but
        not with that wording."""
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="\u0661\u0662\u0663")
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert "user id" not in r.detail

    def test_neither_key_set_is_ok(self, make_config, tmp_path):
        """Not configuring a reviewer is a choice, not a misconfiguration."""
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="")
        assert run_checks(config, only=("developer.gitlab_reviewer",))[0].status == OK

    def test_an_id_recorded_beside_a_username_is_ok(self, make_config, tmp_path):
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_reviewer="reviewer-user",
            gitlab_reviewer_id="1234567",
        )
        assert run_checks(config, only=("developer.gitlab_reviewer",))[0].status == OK

    def test_skips_when_the_developer_skill_is_off(self, make_config):
        from istota.config import DeveloperConfig

        config = make_config(developer=DeveloperConfig(enabled=False))
        assert run_checks(config, only=("developer.gitlab_reviewer",))[0].status == SKIP

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_reviewer="1234567")
        assert run_checks(config, only=("developer.gitlab_reviewer",))[0].status != FAIL


class TestForgeTransport:
    """A forge token sent over plain HTTP.

    This became reachable when the developer skill started seeding glab's
    `api_protocol` for an `http://` forge URL — before that a plain-HTTP forge
    simply failed at the TLS handshake, so no token ever left. It works now,
    and a working plaintext credential transport is worth one line in the
    report rather than silence.
    """

    def test_https_is_ok(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_url="https://gitlab.com")
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert r.status == OK

    def test_plain_http_with_a_token_warns_naming_the_url(self, make_config, tmp_path):
        config = _dev_config(
            make_config, tmp_path, gitlab_url="http://gitlab.internal:8080"
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert r.status == WARN
        assert "http://gitlab.internal:8080" in r.detail
        assert r.remedy

    def test_the_token_value_is_never_in_the_report(self, make_config, tmp_path):
        """The detail names the URL, and a URL can carry userinfo."""
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="http://gitlab.internal:8080",
            gitlab_token="glpat-" + "s" * 20,
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert "glpat-" not in (r.detail + (r.remedy or ""))

    def test_loopback_still_warns(self, make_config, tmp_path):
        """No carve-out for localhost.

        A loopback forge URL in a real deployment is a proxy or a tunnel, and
        what is on the far side of it is not knowable from here. The check is
        cheap and a WARN costs nothing; guessing wrong is a silently plaintext
        credential.
        """
        config = _dev_config(make_config, tmp_path, gitlab_url="http://127.0.0.1:18080")
        assert run_checks(config, only=("developer.forge_transport",))[0].status == WARN

    def test_skips_without_a_token(self, make_config, tmp_path):
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="http://gitlab.internal:8080",
            gitlab_token="",
            github_token="",
        )
        assert run_checks(config, only=("developer.forge_transport",))[0].status == SKIP

    def test_a_plain_http_github_url_warns_too(self, make_config, tmp_path):
        """Both forges, and for gh the scheme is the whole problem.

        gh refuses a scheme inside `GH_HOST`, so a plain-HTTP `github_url`
        cannot connect however it is spelled — the token never leaves, but the
        operator still wrote `http://` and a check that stayed quiet about it
        would be reporting on the deployment it wished it had. The port is a
        separate matter and is no longer broken (`forge_cli._gh_host`,
        ISSUE-279).
        """
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="https://gitlab.com",
            github_url="http://ghe.internal",
            github_token="g" * 20,
        )
        assert run_checks(config, only=("developer.forge_transport",))[0].status == WARN

    def test_a_url_carrying_a_credential_is_warned_about(self, make_config, tmp_path):
        """A forge URL is not where a credential belongs.

        The token goes in `gitlab_token`, and `git_remote_scrub` exists to
        strip exactly this out of URLs. It matters more since the plain-HTTP
        entry landed: `_plain_http_host_entry` refuses to write an entry for
        such a URL — writing one would mean putting the password in a file the
        sandbox can read — so the call fails, and without this check nothing
        says why.
        """
        config = _dev_config(
            make_config, tmp_path, gitlab_url="https://bot:sekritvalue@gitlab.internal"
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]

        assert r.status == WARN
        assert "gitlab.internal" in r.detail
        assert "sekritvalue" not in (r.detail + (r.remedy or "")), r.detail

    def test_the_redacted_url_still_shows_a_credential_was_there(
        self, make_config, tmp_path
    ):
        """Removing the userinfo silently is its own failure.

        An operator reading `https://gitlab.internal` cannot tell the
        configured value carried a credential at all, which is the single most
        useful thing this check could tell them.
        """
        config = _dev_config(
            make_config, tmp_path, gitlab_url="https://bot:sekritvalue@gitlab.internal"
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]

        assert "@gitlab.internal" in r.detail, r.detail

    def test_a_malformed_url_does_not_turn_a_warning_into_a_failure(
        self, make_config, tmp_path
    ):
        """`urlsplit` raises on some inputs — `http://[::1` is `Invalid IPv6 URL`.

        Unguarded, `run_checks` catches it and reports FAIL with the remedy
        "this is a defect in the check": a WARN-only check emitting a FAIL, and
        blaming itself for the operator's typo.
        """
        config = _dev_config(make_config, tmp_path, gitlab_url="http://[::1")
        r = run_checks(config, only=("developer.forge_transport",))[0]

        assert r.status != FAIL, r.detail

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_url="http://gitlab.internal")
        assert run_checks(config, only=("developer.forge_transport",))[0].status != FAIL


class TestWebStatic:
    def test_skips_when_no_web_surface(self, make_config):
        r = run_checks(make_config(), only=("web.static",))[0]
        assert r.status == SKIP

    def test_missing_build_fails(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(tmp_path / "nope"))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_empty_index_fails(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == FAIL

    def test_present_build_is_ok(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html><html></html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == OK


class TestWebBuildCurrent:
    """ISSUE-428: whether the served bundle is current for this checkout's `web/`.

    `web.static` asks only whether `index.html` exists and is non-empty, and
    both stay true across a stale bundle — which is the condition the issue
    reported: a frontend-only commit landed, every unit restarted, and the
    browser kept running old code with nothing on the host saying so.

    The predicate is **"has `web/` changed since the build"**, not "is the
    stamp HEAD". The cron rebuilds only when a commit touches `web/`, so on a
    branch taking mostly Python commits the stamp trails HEAD nearly always
    while the bundle is exactly the one this checkout would produce. Equality
    would warn on every ordinary deploy and make the stale case indetectable
    among the noise; `test_head_moving_without_web_is_not_stale` is that arm.

    These drive a real git repository rather than hand-written `.git` files,
    because the check now shells out to `git diff` and a fixture that only
    looks like a repository would answer nothing.
    """

    @staticmethod
    def _repo(root: Path):
        """A checkout with one `web/` file and one Python file."""

        def git(*args: str) -> str:
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
                ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
            )
            return out.stdout.strip()

        root.mkdir(parents=True, exist_ok=True)
        git("init", "--initial-branch=main", ".")
        (root / "web").mkdir()
        (root / "web" / "app.svelte").write_text("<p>a</p>\n")
        (root / "app.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-m", "a")
        return git

    @staticmethod
    def _bundle(tmp_path, monkeypatch, version: str | None):
        build = tmp_path / "build"
        (build / "_app").mkdir(parents=True)
        (build / "index.html").write_text("<!doctype html>")
        if version is not None:
            (build / "_app" / "version.json").write_text('{"version":"%s"}' % version)
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        return build

    def _config(self, make_config):
        from istota.config import WebConfig

        return make_config(web=WebConfig(enabled=True))

    def _run(self, make_config, **kwargs):
        return run_checks(self._config(make_config), only=("web.build_current",), **kwargs)[0]

    # -- the arms that need no repository ---------------------------------

    def test_skips_when_no_web_surface(self, make_config):
        r = run_checks(make_config(), only=("web.build_current",))[0]
        assert r.status == SKIP

    def test_skips_when_the_bundle_carries_no_version(self, make_config, tmp_path, monkeypatch):
        self._bundle(tmp_path, monkeypatch, None)
        assert self._run(make_config).status == SKIP

    def test_skips_when_the_bundle_was_not_stamped_with_a_commit(
        self, make_config, tmp_path, monkeypatch
    ):
        """SvelteKit's default version is a build timestamp.

        A container image and a developer's own build both produce one, and
        neither is evidence of anything about a deployment.
        """
        self._bundle(tmp_path, monkeypatch, "1788110322364")
        r = self._run(make_config)
        assert r.status == SKIP
        assert "not stamped" in r.detail

    def test_a_malformed_version_file_skips(self, make_config, tmp_path, monkeypatch):
        """Never raises: one caller is the daemon's boot sequence."""
        build = self._bundle(tmp_path, monkeypatch, "a" * 40)
        (build / "_app" / "version.json").write_text("{not json")
        assert self._run(make_config).status == SKIP

    def test_skips_when_there_is_no_checkout(self, make_config, tmp_path, monkeypatch):
        """A wheel install has the bundle and no repository to compare it to."""
        self._bundle(tmp_path, monkeypatch, "a" * 40)
        monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_path / "nowhere")
        assert self._run(make_config).status == SKIP

    def test_skips_under_probe_false_rather_than_guessing(
        self, make_config, tmp_path, monkeypatch
    ):
        """The comparison needs git, and `probe=False` forbids spawning.

        It must not fall back to comparing the two shas for equality, which is
        the wrong question — see `test_head_moving_without_web_is_not_stale`.

        The repository is **real** here on purpose: against a path that does
        not exist the no-checkout arm returns SKIP before reaching the spawn,
        so removing the probe gate left this passing. `run_checks` turns a
        raising check into a FAIL rather than propagating, which is what makes
        the spy assertion visible in the result at all.
        """
        repo = tmp_path / "repo"
        git = self._repo(repo)
        self._bundle(tmp_path, monkeypatch, git("rev-parse", "HEAD"))
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)

        def _fail(*args, **kwargs):
            raise AssertionError("spawned under probe=False")

        monkeypatch.setattr(subprocess, "run", _fail)
        r = self._run(make_config, probe=False)
        assert r.status == SKIP, r.detail
        assert "not executed" in r.detail

    def test_skips_when_the_stamped_commit_is_unknown(self, make_config, tmp_path, monkeypatch):
        """A bundle can outlive a re-clone. Unanswerable, not stale."""
        self._bundle(tmp_path, monkeypatch, "a" * 40)
        repo = tmp_path / "repo"
        self._repo(repo)
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        r = self._run(make_config)
        assert r.status == SKIP
        assert "could not compare" in r.detail

    # -- the two that are the point ---------------------------------------

    def test_head_moving_without_web_is_not_stale(self, make_config, tmp_path, monkeypatch):
        """The false positive this check must not have.

        The cron rebuilds only on a `web/` change, so after any Python-only
        commit the stamp trails HEAD while the bundle is byte-correct. On a
        host whose cron fires every two minutes that is nearly always, and a
        permanently amber check hides the one it exists to report.
        """
        repo = tmp_path / "repo"
        git = self._repo(repo)
        built_at = git("rev-parse", "HEAD")
        (repo / "app.py").write_text("x = 2\n")
        git("add", "-A")
        git("commit", "-m", "python only")
        assert git("rev-parse", "HEAD") != built_at

        self._bundle(tmp_path, monkeypatch, built_at)
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        r = self._run(make_config)
        assert r.status == OK, r.detail

    def test_a_web_change_since_the_build_warns(self, make_config, tmp_path, monkeypatch):
        """The reported condition, and the whole reason this check exists."""
        repo = tmp_path / "repo"
        git = self._repo(repo)
        built_at = git("rev-parse", "HEAD")
        (repo / "web" / "app.svelte").write_text("<p>b</p>\n")
        git("add", "-A")
        git("commit", "-m", "frontend")

        self._bundle(tmp_path, monkeypatch, built_at)
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        r = self._run(make_config)
        assert r.status == WARN, r.detail
        assert r.remedy
        # A WARN must not page anyone: the next auto-update tick clears it.
        assert doctor.verdict([r])[0] is True

    def test_a_bundle_built_from_head_is_ok(self, make_config, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        git = self._repo(repo)
        self._bundle(tmp_path, monkeypatch, git("rev-parse", "HEAD"))
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        assert self._run(make_config).status == OK

    def test_the_git_call_carries_the_hardening_overrides(
        self, make_config, tmp_path, monkeypatch
    ):
        """Asserted on the argv, because behaviourally it cannot be observed.

        A repository's own config can name a program for `git` to run, and
        under a deployment that config is written by whoever can write the
        checkout. It happens that `git diff --quiet` generates no diff and so
        runs no `diff.external` — measured, by setting one on a real
        repository and watching the canary never appear, with and without
        these overrides. So this guards the argv against a future change (a
        dropped `--quiet`, a different subcommand) rather than a live hole,
        and it is written against the argv precisely because a behavioural
        version of it passes whatever the code does.
        """
        from istota.git_hardening import GIT_HARDENING

        repo = tmp_path / "repo"
        git = self._repo(repo)
        self._bundle(tmp_path, monkeypatch, git("rev-parse", "HEAD"))
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)

        seen = []
        real = doctor._run
        monkeypatch.setattr(doctor, "_run", lambda argv, **kw: seen.append(argv) or real(argv, **kw))
        self._run(make_config)

        assert seen, "the check never ran git"
        argv = seen[0]
        assert argv[0] == "git"
        for flag in GIT_HARDENING:
            assert flag in argv, f"{flag} missing from {argv}"


class TestSandboxMasks:
    def test_skips_when_bwrap_is_unavailable(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: False)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True)[0]
        assert r.status == SKIP

    def test_skips_under_probe_false(self, make_config, monkeypatch):
        """The probe contract is unconditional. Checked before `_bwrap_usable`,
        which spawns a probe of its own."""

        def _fail(*args, **kwargs):
            raise AssertionError("spawned under probe=False")

        monkeypatch.setattr(doctor.subprocess, "run", _fail)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True, probe=False)[0]
        assert r.status == SKIP
        assert "probe disabled" in r.detail

    def test_timeout_is_reported_as_fail_not_a_hang(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: True)

        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="bwrap", timeout=30)

        monkeypatch.setattr(doctor.subprocess, "run", _timeout)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True)[0]
        assert r.status == FAIL
        assert "timed out" in r.detail.lower()


class TestRendering:
    def test_exit_code_is_one_on_any_fail(self):
        results = [
            CheckResult("a.b", OK, "fine"),
            CheckResult("c.d", FAIL, "broken", remedy="fix it"),
        ]
        assert exit_code(results) == 1

    def test_exit_code_is_zero_without_a_fail(self):
        results = [
            CheckResult("a.b", OK, "fine"),
            CheckResult("c.d", WARN, "iffy", remedy="look at it"),
            CheckResult("e.f", SKIP, "n/a"),
        ]
        assert exit_code(results) == 0

    def test_exit_code_of_nothing_is_zero(self):
        assert exit_code([]) == 0

    def test_render_json_round_trips(self):
        results = [
            CheckResult("a.b", OK, "fine", scope=IMAGE),
            CheckResult("c.d", FAIL, "broken", remedy="fix it"),
        ]
        parsed = json.loads(render_json(results, secrets=()))
        assert isinstance(parsed, list)
        assert parsed[0] == {
            "name": "a.b",
            "status": OK,
            "detail": "fine",
            "remedy": "",
            "scope": IMAGE,
        }
        assert parsed[1]["remedy"] == "fix it"

    def test_render_json_is_valid_even_when_checks_failed(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        json.loads(render_json(run_checks(config), secrets=doctor.config_secrets(config)))

    def test_secrets_is_required_so_the_boundary_cannot_be_fail_open(self):
        """`render_json` crosses an HTTP boundary to the admin dashboard. A
        caller that simply forgot the argument must not get unredacted output —
        omitting it has to be a decision, spelled `secrets=()`."""
        results = [CheckResult("a.b", OK, "fine")]
        with pytest.raises(TypeError):
            render_json(results)
        with pytest.raises(TypeError):
            render_text(results)

    def test_render_json_redacts_a_credential_in_a_detail(self):
        """Check authors are forbidden from putting credentials in `detail`;
        the renderer does not take their word for it."""
        secret = "NOT-A-REAL-TOKEN-aaaaaaaa"
        results = [CheckResult("x.y", FAIL, f"token {secret} rejected", remedy=f"rotate {secret}")]
        rendered = render_json(results, secrets=[secret])
        assert secret not in rendered
        assert "[redacted]" in rendered

    def test_render_json_redaction_ignores_empty_secrets(self):
        results = [CheckResult("x.y", OK, "all good")]
        rendered = render_json(results, secrets=["", None])
        assert "all good" in rendered

    def test_render_text_groups_by_prefix_and_indents_remedies(self):
        results = [
            CheckResult("runtime.platform", OK, "Linux x86_64"),
            CheckResult("developer.forge_binaries.gh", FAIL, "missing", remedy="install gh"),
        ]
        text = render_text(results, secrets=())
        assert "runtime" in text
        assert "developer" in text
        assert "install gh" in text
        remedy_line = [ln for ln in text.splitlines() if "install gh" in ln][0]
        assert remedy_line.startswith(" ")

    def test_render_text_redacts_too(self):
        secret = "NOT-A-REAL-TOKEN-bbbbbbbb"
        results = [CheckResult("x.y", FAIL, f"saw {secret}", remedy="rotate")]
        assert secret not in render_text(results, secrets=[secret])


class TestConfigSecrets:
    def test_collects_configured_credentials(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="NOT-A-REAL-TOKEN-zzzzzzzz")
        secrets = doctor.config_secrets(config)
        assert "NOT-A-REAL-TOKEN-zzzzzzzz" in secrets

    def test_ignores_short_and_empty_values(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="")
        assert "" not in doctor.config_secrets(config)

    def test_descends_into_dicts(self, make_config):
        """`config.users` is a dict of dataclasses and holds every per-user
        credential; a walk that only followed attributes missed all of them."""
        from istota.config import UserConfig

        config = make_config()
        user = UserConfig(display_name="Alice")
        if hasattr(user, "email_password"):
            user.email_password = "hunter2-hunter2-hunter2"
            config.users = {"alice": user}
            assert "hunter2-hunter2-hunter2" in doctor.config_secrets(config)

    def test_collects_the_always_secret_header_dict(self, make_config):
        """`brain.native.extra_headers` is a dict `admin_config_view` marks
        always-secret, because it is where a non-Anthropic deployment puts its
        provider key."""
        config = make_config()
        config.brain.native.extra_headers = {"Authorization": "NOT-A-REAL-HEADER-VALUE-1"}
        assert "NOT-A-REAL-HEADER-VALUE-1" in doctor.config_secrets(config)

    def test_collects_a_header_whose_name_is_not_obviously_a_credential(self, make_config):
        """The whole field is an auth channel by construction, so a header spelling
        nobody anticipated must not be the thing that escapes redaction."""
        config = make_config()
        config.brain.native.extra_headers = {"x-goog-api-client": "zzzzzzzzzzzzzzzzzz"}
        assert "zzzzzzzzzzzzzzzzzz" in doctor.config_secrets(config)

    def test_terminates_on_a_self_referential_config(self, make_config):
        """A cycle must not hang the boot path."""
        config = make_config()
        config.users = {"loop": config}
        doctor.config_secrets(config)


# ---------------------------------------------------------------------------
# The development container


def _container_config(make_config, tmp_path, *, devbox=True, users=("alice",), **overrides):
    """A Config with `[developer.container]` wired and one devbox user.

    `devbox` is the switch the backend is derived from; there is no `backend`
    key to set any more.
    """
    from istota.config import (
        ContainerConfig,
        DeveloperConfig,
        DevboxConfig,
        SecurityConfig,
        UserConfig,
    )

    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    exec_root = tmp_path / "run" / "exec"
    exec_root.mkdir(parents=True, exist_ok=True)
    # The per-user socket directory is what says "this user has a devbox": the
    # role creates it only for `istota_devbox_users`, and the check treats its
    # absence as "not configured for one" rather than as a broken container.
    if not overrides.pop("no_socket_dirs", False):
        for u in users:
            (exec_root / u).mkdir(exist_ok=True)
    container_fields = {
        "exec_socket_dir": str(exec_root),
        "connect_timeout_seconds": 0.5,
    }
    container_fields.update(overrides.pop("container", {}))
    # Overridable so a caller can express "no repos_dir at all", which is the
    # shape the derived package cache does not exist in.
    repos_dir = overrides.pop("repos_dir", str(repos))
    return make_config(
        developer=DeveloperConfig(
            enabled=True, repos_dir=repos_dir, container=ContainerConfig(**container_fields)
        ),
        devbox=DevboxConfig(enabled=devbox),
        security=SecurityConfig(**overrides.pop("security", {})),
        users={u: UserConfig(display_name=u) for u in users},
        **overrides,
    )


def _ping_reply(socket_path, payload, timeout):
    return [{"pong": True, "protocol": 1}, {"exit_code": 0}], ""


def _agreeing_container(repos_root, *, uid_offset=0, cache_exit=0, reaper=True):
    """A fake server that answers ping, stat and `test -d` the way a healthy one does.

    `reaper=None` is a server too old to know the field, which is a third answer
    and not a quieter spelling of `False`.
    """
    import os as _os

    def _reply(socket_path, payload, timeout):
        body = payload.decode()
        if '"ping"' in body:
            return [{"pong": True, "protocol": 1}, {"exit_code": 0}], ""
        if '"stat"' in body:
            stat = {"uid": _os.getuid() + uid_offset, "repos_root": repos_root}
            if reaper is not None:
                stat["reaper"] = reaper
            return [stat, {"exit_code": 0}], ""
        return [{"exit_code": cache_exit}], ""

    return _reply


class TestTheReposLayoutCheck:
    """The loud path for an upgrade that has not moved its clones.

    `repos_dir` became a per-user root on *every* backend, and the bind is
    skipped when its source does not exist — so a host whose clones still sit
    flat has an unusable developer skill and no error anywhere naming a path.
    This is what says so.
    """

    def _config(self, make_config, tmp_path, users=("alice",)):
        from istota.config import DeveloperConfig, UserConfig

        repos = tmp_path / "repos"
        repos.mkdir(exist_ok=True)
        return make_config(
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
            users={u: UserConfig(display_name=u) for u in users},
        )

    @staticmethod
    def _bare(path):
        path.mkdir(parents=True, exist_ok=True)
        for marker in ("HEAD", "config"):
            (path / marker).write_text("")
        (path / "objects").mkdir(exist_ok=True)

    def test_the_flat_layout_fails_and_names_what_it_found(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        self._bare(tmp_path / "repos" / "namespace" / "project.git")

        result = doctor.check_repos_layout(config, probe=False)

        assert result.status == FAIL
        assert "namespace" in result.detail
        assert result.remedy

    def test_the_per_user_layout_is_ok(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        self._bare(tmp_path / "repos" / "alice" / "namespace" / "project.git")

        result = doctor.check_repos_layout(config, probe=False)

        assert result.status == OK

    def test_a_half_migrated_host_still_fails(self, make_config, tmp_path):
        """One user moved and another not is the shape a partial play leaves."""
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        self._bare(tmp_path / "repos" / "alice" / "ns" / "project.git")
        self._bare(tmp_path / "repos" / "leftover" / "project.git")

        result = doctor.check_repos_layout(config, probe=False)

        assert result.status == FAIL
        assert "leftover" in result.detail

    def test_an_empty_root_is_ok(self, make_config, tmp_path):
        result = doctor.check_repos_layout(self._config(make_config, tmp_path), probe=False)

        assert result.status == OK

    def test_a_directory_holding_no_repository_is_not_a_finding(
        self, make_config, tmp_path
    ):
        """`repos_dir` is a directory an operator may put other things in."""
        config = self._config(make_config, tmp_path)
        (tmp_path / "repos" / "notes").mkdir()
        (tmp_path / "repos" / "notes" / "README").write_text("hi")

        assert doctor.check_repos_layout(config, probe=False).status == OK

    def test_it_skips_when_the_skill_is_off(self, make_config, tmp_path):
        from istota.config import DeveloperConfig

        config = make_config(developer=DeveloperConfig(enabled=False))

        assert doctor.check_repos_layout(config, probe=False).status == SKIP

    def test_it_spawns_nothing_under_probe_false(self, make_config, tmp_path, monkeypatch):
        """It is on the config-load path, where `probe=False` forbids spawning."""
        monkeypatch.setattr(
            doctor, "_run",
            lambda *a, **k: pytest.fail("the repos layout check spawned a process"),
        )
        config = self._config(make_config, tmp_path)
        self._bare(tmp_path / "repos" / "namespace" / "project.git")

        doctor.check_repos_layout(config, probe=False)


class TestTheDeveloperContainerChecks:
    """Five properties, each of which fails silently on its own.

    Registered as one entry so a single connection per user answers four of
    them; the fifth reads the rendered config file and opens nothing.
    """

    GROUP = "developer.container"
    NAMES = {
        "developer.container.backend",
        "developer.container.transport",
        "developer.container.identity",
        "developer.container.uv_cache",
        "developer.container.command_reaper",
    }

    def test_all_five_are_produced_whatever_happens(self, make_config, tmp_path):
        """A caller asserts on a name, never on a count — a check that vanishes
        under some configuration is a check nothing can require."""
        for devbox in (False, True):
            config = _container_config(make_config, tmp_path, devbox=devbox)
            results = doctor.check_developer_container(config, probe=False)
            assert {r.name for r in results} == self.NAMES

    def test_the_group_is_in_the_registry(self):
        assert self.GROUP in {name for name, _ in CHECKS}

    def test_the_backend_being_off_skips_the_three_that_need_a_container(
        self, make_config, tmp_path
    ):
        config = _container_config(make_config, tmp_path, devbox=False)

        by_name = _by_name(doctor.check_developer_container(config, probe=True))

        for name in self.NAMES - {"developer.container.backend"}:
            assert by_name[name].status == SKIP

    def test_the_skip_names_whichever_input_is_off(self, make_config, tmp_path):
        """This used to warn about a pair — the devbox skill offered while
        `backend = none` meant every verb but `reset` refused. That state is
        no longer configurable, so the detail's job is now to say which of the
        three derivation inputs is the one holding the transport off.
        """
        config = _container_config(make_config, tmp_path, devbox=False)

        transport = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.transport"
        ]

        assert transport.status == SKIP
        assert "[devbox] enabled is false" in transport.detail

    def test_the_skip_names_the_developer_skill_when_that_is_what_is_off(
        self, make_config, tmp_path
    ):
        """Control for the test above: a different input off has to produce a
        different sentence, or the detail is decoration rather than a
        diagnosis."""
        config = _container_config(make_config, tmp_path, devbox=True)
        config.developer.enabled = False

        transport = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.transport"
        ]

        assert transport.status == SKIP
        assert "the developer skill is off" in transport.detail
        assert "[devbox] enabled is false" not in transport.detail
        assert "devbox skill is offered" not in transport.detail

    def test_probe_false_opens_no_socket(self, make_config, tmp_path, monkeypatch):
        """Doctor runs on the daemon's start-up path; `probe=False` must connect
        to nothing."""
        called = []
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            lambda *a, **k: (called.append(a), ([], "unreachable"))[1],
        )
        config = _container_config(make_config, tmp_path)

        by_name = _by_name(doctor.check_developer_container(config, probe=False))

        assert not called
        assert by_name["developer.container.transport"].status == SKIP

    def test_a_user_with_no_devbox_is_not_a_failure(self, make_config, tmp_path):
        """Which users have a devbox is not in the daemon's config — the list
        lives in Ansible. Counting every configured user as unreachable would
        FAIL this check permanently on the reference shape (one admin with a
        container, several other users without) and alert every admin hourly."""
        config = _container_config(
            make_config, tmp_path, users=("alice", "bob"), no_socket_dirs=True
        )

        by_name = _by_name(doctor.check_developer_container(config, probe=True))

        assert by_name["developer.container.transport"].status == SKIP
        assert "no devbox socket directory" in by_name["developer.container.transport"].detail

    def test_a_dead_container_is_a_fail_naming_the_socket(
        self, make_config, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            lambda socket_path, payload, timeout: ([], f"could not connect to {socket_path}"),
        )
        config = _container_config(make_config, tmp_path)

        transport = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.transport"
        ]

        assert transport.status == FAIL
        assert "alice" in transport.detail
        assert transport.remedy

    def test_a_live_container_that_agrees_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            _agreeing_container(str(tmp_path / "repos" / "alice")),
        )
        config = _container_config(
            make_config, tmp_path,
            security={"sandbox_cache_dir": str(tmp_path / "cache")},
        )

        by_name = _by_name(doctor.check_developer_container(config, probe=True))

        assert by_name["developer.container.transport"].status == OK
        assert by_name["developer.container.identity"].status == OK
        assert by_name["developer.container.uv_cache"].status == OK
        assert by_name["developer.container.command_reaper"].status == OK

    def test_a_uid_mismatch_fails_and_says_what_it_costs(
        self, make_config, tmp_path, monkeypatch
    ):
        """Untreated, uid drift ends in worktrees that can never be reaped, and
        there is no error message anywhere that says so."""
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            _agreeing_container(str(tmp_path / "repos" / "alice"), uid_offset=1),
        )
        config = _container_config(make_config, tmp_path)

        identity = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.identity"
        ]

        assert identity.status == FAIL
        assert "uid" in identity.detail
        assert "reap" in identity.remedy

    def test_a_repos_root_mismatch_fails(self, make_config, tmp_path, monkeypatch):
        """The two sides must spell the tree identically: the shim sends
        `os.getcwd()` and the server checks it with `realpath` against its own
        root, so a disagreement refuses every real working directory."""
        monkeypatch.setattr(
            doctor, "_exec_transport_request", _agreeing_container("/somewhere/else"),
        )
        config = _container_config(make_config, tmp_path)

        identity = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.identity"
        ]

        assert identity.status == FAIL
        assert "/somewhere/else" in identity.detail

    def test_an_unset_repos_dir_skips_rather_than_warning(
        self, make_config, tmp_path, monkeypatch
    ):
        """The inverse of what this test used to assert, and the inversion is
        the point.

        It used to WARN when `security.sandbox_cache_dir` was unset. That key
        stopped being the cache root: the cache is derived at
        `{repos_dir}/{user_id}/.package-caches`, the key is read only where
        `repos_dir` is unset, and its Ansible default is blank — so the old
        assertion would fire on every correctly configured deployment, which is
        worse than no check at all. With no `repos_dir` there is no subtree and
        no derived cache, so there is nothing to look for and SKIP is honest.
        """
        monkeypatch.setattr(doctor, "_exec_transport_request", _ping_reply)
        config = _container_config(make_config, tmp_path, repos_dir="")

        cache = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.uv_cache"
        ]

        assert cache.status == SKIP

    def test_a_missing_cache_mount_warns(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            _agreeing_container(str(tmp_path / "repos" / "alice"), cache_exit=1),
        )
        config = _container_config(
            make_config, tmp_path,
            security={"sandbox_cache_dir": str(tmp_path / "cache")},
        )

        cache = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.uv_cache"
        ]

        assert cache.status == WARN
        assert "alice" in cache.detail

    def test_a_server_with_no_reaper_warns_rather_than_failing(
        self, make_config, tmp_path, monkeypatch
    ):
        """The transport works and every command is still killed on its own exit
        path. What is gone is the backstop for the death that skips those paths,
        so the cost is a leak rather than an outage — and the whole reason this
        check exists is that a ping and a stat both answered happily while a
        24-hour-old build ran against the repos mount."""
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            _agreeing_container(str(tmp_path / "repos" / "alice"), reaper=False),
        )
        config = _container_config(make_config, tmp_path)

        result = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.command_reaper"
        ]

        assert result.status == WARN
        assert "alice" in result.detail
        assert "docker logs" in result.remedy

    def test_a_server_too_old_to_answer_is_not_reported_as_broken(
        self, make_config, tmp_path, monkeypatch
    ):
        """A missing field and a `false` are different facts. Reading the first
        as the second warns on every container that has not been rebuilt yet,
        which is a check an operator learns to scroll past."""
        monkeypatch.setattr(
            doctor, "_exec_transport_request",
            _agreeing_container(str(tmp_path / "repos" / "alice"), reaper=None),
        )
        config = _container_config(make_config, tmp_path)

        result = _by_name(doctor.check_developer_container(config, probe=True))[
            "developer.container.command_reaper"
        ]

        assert result.status == SKIP


class TestTheBackendAgreementCheck:
    """Design 1 asks for this and an earlier draft put it in a unit test, which
    is the wrong place for a property an operator needs to see on the host that
    has the problem."""

    def _write(self, tmp_path, *, devbox, retired=None):
        """A rendered config, described by its inputs rather than by a key.

        The check has to re-derive from the file for the same reason the daemon
        does; a version that read `[developer.container] backend` would report
        OK on every deployment forever, since nothing writes that key any more.
        """
        path = tmp_path / "config.toml"
        body = (
            "[developer]\nenabled = true\n"
            f'repos_dir = "{tmp_path / "repos"}"\n\n'
            f"[devbox]\nenabled = {str(bool(devbox)).lower()}\n"
        )
        if retired is not None:
            body += f'\n[developer.container]\nbackend = "{retired}"\n'
        path.write_text(body)
        return path

    def _backend_result(self, config):
        return _by_name(doctor.check_developer_container(config, probe=False))[
            "developer.container.backend"
        ]

    def test_agreement_is_ok(self, make_config, tmp_path):
        config = _container_config(make_config, tmp_path, devbox=True)
        config.config_path = self._write(tmp_path, devbox=True)

        assert self._backend_result(config).status == OK

    def test_a_daemon_running_the_old_value_fails(self, make_config, tmp_path):
        """The file says one thing and the running process another, which is
        what an operator sees after editing config.toml and not restarting: a
        feature that was switched on and did not switch on."""
        config = _container_config(make_config, tmp_path, devbox=False)
        config.config_path = self._write(tmp_path, devbox=True)

        result = self._backend_result(config)

        assert result.status == FAIL
        assert "devbox" in result.detail and "none" in result.detail
        assert result.remedy

    def test_the_drift_check_reads_every_input_not_just_the_devbox_switch(
        self, make_config, tmp_path
    ):
        """The derivation is a conjunction, so the re-derivation has to be one
        too. Reading `[devbox] enabled` alone would call a file with the devbox
        on and no `repos_dir` a devbox deployment, and then report drift against
        a daemon that correctly decided otherwise."""
        config = _container_config(make_config, tmp_path, devbox=False)
        path = tmp_path / "config.toml"
        path.write_text(
            '[developer]\nenabled = true\nrepos_dir = ""\n\n'
            "[devbox]\nenabled = true\n"
        )
        config.config_path = path

        assert self._backend_result(config).status == OK

    def test_a_file_still_carrying_the_retired_key_is_reported(
        self, make_config, tmp_path
    ):
        """The one case where intent and behaviour differ with no drift present.

        An operator who wrote `backend = "none"` had builds on the host until
        this release. The derivation now ignores the key, so a deployment can be
        doing exactly the opposite of what its config file appears to say while
        the file and the daemon agree perfectly.
        """
        config = _container_config(make_config, tmp_path, devbox=True)
        config.config_path = self._write(tmp_path, devbox=True, retired="none")

        result = self._backend_result(config)

        assert result.status == WARN
        assert "retired" in result.detail
        assert result.remedy

    def test_a_file_without_the_retired_key_does_not_warn(
        self, make_config, tmp_path
    ):
        """Control: the WARN above must key on the stale key rather than on
        anything the ordinary rendering also produces."""
        config = _container_config(make_config, tmp_path, devbox=True)
        config.config_path = self._write(tmp_path, devbox=True)

        assert self._backend_result(config).status == OK

    def test_the_retired_key_does_not_suppress_a_real_drift(
        self, make_config, tmp_path
    ):
        """The ordering, and the reason it is not the obvious one.

        Reporting the stale key and returning makes this check dead on exactly
        the hosts most likely to have one: the Ansible template stopped
        emitting it, so a managed host loses it on the next deploy, while a
        hand-maintained `/etc/istota/config.toml` keeps it for ever. A WARN
        about a key would then stand in, permanently, for a FAIL about a daemon
        running the wrong thing.
        """
        config = _container_config(make_config, tmp_path, devbox=False)
        config.config_path = self._write(tmp_path, devbox=True, retired="devbox")

        result = self._backend_result(config)

        assert result.status == FAIL
        assert "restart" in result.remedy.lower()
        # Named, but explicitly not blamed — deleting it would not clear this.
        assert "retired" in result.detail
        assert "not the cause" in result.detail

    def test_a_whitespace_repos_dir_is_not_drift(self, make_config, tmp_path):
        """Both derivations strip, so neither calls a blank path a root.

        A mismatch here is the worst shape a check can take: a permanent FAIL
        whose remedy is to restart a daemon that is already running the right
        answer.
        """
        config = _container_config(make_config, tmp_path, devbox=True)
        config.developer.repos_dir = "   "
        path = tmp_path / "config.toml"
        path.write_text(
            '[developer]\nenabled = true\nrepos_dir = "   "\n\n'
            "[devbox]\nenabled = true\n"
        )
        config.config_path = path

        assert self._backend_result(config).status == OK

    def test_a_config_built_in_memory_skips(self, make_config, tmp_path):
        config = _container_config(make_config, tmp_path)
        config.config_path = None

        assert self._backend_result(config).status == SKIP

    def test_an_unreadable_file_warns_rather_than_claiming_agreement(
        self, make_config, tmp_path
    ):
        config = _container_config(make_config, tmp_path)
        config.config_path = tmp_path / "gone.toml"

        result = self._backend_result(config)

        assert result.status == WARN
        assert result.remedy



class TestSkillOverlays:
    """`config.skill_overlays` is the only thing that ever says a per-skill
    overlay will not be read. Every case here asserts the check did **not**
    SKIP — a suite asserting "no FAIL" is green on exactly the broken tree.
    """

    NAME = "config.skill_overlays"

    @staticmethod
    def _config(make_config, tmp_path, **overrides):
        bundled = tmp_path / "bundled"
        for skill in ("developer", "notes", "browse", "sensitive_actions"):
            d = bundled / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "skill.md").write_text(
                f"---\nname: {skill}\ndescription: the {skill} skill\n---\n\n# {skill}\n"
            )
        return make_config(bundled_skills_dir=bundled, **overrides)

    @staticmethod
    def _overlays(config, user_id="alice"):
        d = (
            Path(config.nextcloud_mount_path)
            / "Users" / user_id / config.bot_dir_name / "config" / "skills"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, config):
        results = run_checks(config, only=(self.NAME,))
        assert len(results) == 1
        return results[0]

    # ------------------------------------------------------------ the gates

    def test_it_skips_without_a_mount(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, nextcloud_mount_path=None)
        r = self._run(config)
        assert r.status == SKIP
        assert "mount" in r.detail

    def test_it_skips_when_there_are_no_user_trees_yet(self, make_config, tmp_path):
        r = self._run(self._config(make_config, tmp_path))
        assert r.status == SKIP

    def test_no_overlays_anywhere_is_ok_and_not_a_skip(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (Path(config.nextcloud_mount_path) / "Users" / "alice").mkdir(parents=True)
        r = self._run(config)
        assert r.status == OK
        assert r.status != SKIP

    # ---------------------------------------------------------- the findings

    def test_a_good_overlay_is_ok(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "developer.md").write_text("- one rule\n")
        r = self._run(config)
        assert r.status == OK
        assert r.status != SKIP

    def test_a_misspelled_skill_name_fails(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "develper.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "develper.md" in r.detail
        assert "unknown_skill" in r.detail
        assert r.remedy

    def test_a_denylisted_skill_fails(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "sensitive_actions.md").write_text("- planted\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "denylisted" in r.detail

    def test_an_over_cap_overlay_fails(self, make_config, tmp_path):
        from istota.skills._loader import OVERLAY_MAX_BYTES

        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "developer.md").write_text(
            "- x\n" * (OVERLAY_MAX_BYTES // 4 + 4)
        )
        r = self._run(config)
        assert r.status == FAIL
        assert "over_cap" in r.detail

    def test_an_oversized_overlay_warns(self, make_config, tmp_path):
        from istota.skills._loader import OVERLAY_WARN_BYTES

        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "developer.md").write_text(
            "- x\n" * (OVERLAY_WARN_BYTES // 4 + 4)
        )
        r = self._run(config)
        assert r.status == WARN
        assert "over_warn_bytes" in r.detail
        assert r.remedy

    def test_a_shallow_heading_warns(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "notes.md").write_text("## My rules\n\n- a rule\n")
        r = self._run(config)
        assert r.status == WARN
        assert "shallow_heading" in r.detail

    def test_a_fail_outranks_a_warning(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        (d / "develper.md").write_text("- a rule\n")
        (d / "notes.md").write_text("## heading\n- a rule\n")
        r = self._run(config)
        assert r.status == FAIL

    def test_a_disabled_skill_is_deliberately_not_reported(self, make_config, tmp_path):
        """Its overlay binds again the moment the skill is switched back on, so
        it is a fact about the configuration and not a defect in the file.
        `skills overlays` still says so for the user asking about their own."""
        config = self._config(make_config, tmp_path, disabled_skills=["browse"])
        (self._overlays(config) / "browse.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == OK

    def test_it_walks_every_user_tree_not_just_the_configured_ones(
        self, make_config, tmp_path
    ):
        """A user whose config block was removed still has files on disk, and
        one left there is exactly what nothing else would report."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config, "alice") / "developer.md").write_text("- ok\n")
        (self._overlays(config, "bob") / "develper.md").write_text("- broken\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "bob/develper.md" in r.detail
        assert "alice" not in r.detail

    def test_the_detail_names_at_most_a_handful(self, make_config, tmp_path):
        # `developer` with one character dropped, nine ways. Each is a typo, so
        # the list being truncated is the FAIL list. Deliberately not
        # `developer{i}` — a trailing digit reads as a numbered copy and WARNs.
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        name = "developer"
        typos = [name[:i] + name[i + 1:] for i in range(len(name))]
        assert len(set(typos)) == 9
        for typo in typos:
            (d / f"{typo}.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "9 of 9" in r.detail
        assert "and 4 more" in r.detail

    def test_an_empty_file_warns_rather_than_failing(self, make_config, tmp_path):
        """It loads as nothing and belongs in the report, but FAIL is reserved
        for the misfiling a person fixes by renaming or shrinking."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "developer.md").write_text("")
        r = self._run(config)
        assert r.status == WARN
        assert "empty" in r.detail

    def test_a_control_character_in_a_filename_cannot_forge_a_second_line(
        self, make_config, tmp_path
    ):
        """A filename here is text the model wrote, and a name may hold anything
        but `/` and NUL. The detail is one line, printed to a terminal and
        rendered into the admin dashboard."""
        config = self._config(make_config, tmp_path)
        overlays = self._overlays(config)
        (overlays / "bad\nname\x1b[31m.md").write_text("- a rule\n")
        r = self._run(config)
        # WARN rather than FAIL: nothing this shape is within a typo's distance
        # of a real skill name. Both statuses render the name through the same
        # `_overlay_label`, which is what is under test.
        assert r.status == WARN
        assert "\n" not in r.detail
        assert "\x1b" not in r.detail

    def test_a_very_long_filename_is_truncated(self, make_config, tmp_path):
        # WARN for the same reason as the control-character case above: 200
        # characters is not a typo of any skill name.
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / ("z" * 200 + ".md")).write_text("- a rule\n")
        r = self._run(config)
        assert r.status == WARN
        assert len(r.detail) < 200
        assert "..." in r.detail

    # --------------------------------------------------- the plantable tree

    def test_a_symlinked_user_entry_is_not_descended_into(
        self, make_config, tmp_path
    ):
        """Every component under `{mount}/Users/{user_id}` is model-writable, so
        a link planted at another name must not make the walk descend elsewhere
        and report a file against the wrong user."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config, "alice") / "develper.md").write_text("- broken\n")
        users = Path(config.nextcloud_mount_path) / "Users"
        (users / "mallory").symlink_to(users / "alice", target_is_directory=True)

        r = self._run(config)
        assert r.status == FAIL
        assert "alice/develper.md" in r.detail
        assert "mallory" not in r.detail

    def test_an_overlay_dir_redirected_out_of_the_user_tree_is_named_not_followed(
        self, make_config, tmp_path
    ):
        """`config/` and `skills/` are ordinary entries a task can replace with
        a link. Following one would report — and open — files anywhere the
        daemon can read.

        That property is unchanged; the status is not. This used to be skipped
        outright and so reported by nothing, which left the most clear-cut
        plant of the set as the one case an operator never heard about
        (ISSUE-344). It is now named at WARN — WARN rather than FAIL because a
        sandboxed task can create the link at will, and a deployment-scope red
        an attacker can raise on demand is the aimable alert ISSUE-340 split
        this check to avoid.
        """
        config = self._config(make_config, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "develper.md").write_text("- planted\n")
        user_config = (
            Path(config.nextcloud_mount_path)
            / "Users" / "alice" / config.bot_dir_name / "config"
        )
        user_config.mkdir(parents=True)
        (user_config / "skills").symlink_to(elsewhere, target_is_directory=True)

        r = self._run(config)
        assert r.status == WARN
        assert "dir_outside_user_tree" in r.detail
        # The half that matters and has not moved: nothing behind the link was
        # opened, so the planted filename appears nowhere in the report.
        assert "develper" not in r.detail

    def test_a_symlinked_overlay_file_is_reported_and_never_read(
        self, make_config, tmp_path
    ):
        config = self._config(make_config, tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        (self._overlays(config) / "developer.md").symlink_to(secret)

        r = self._run(config)
        # WARN, not FAIL: the file loads as nothing and belongs in the report,
        # but it is not the misfiling a person fixes by renaming or shrinking.
        assert r.status == WARN
        assert "overlay_is_a_symlink" in r.detail
        assert "TOP SECRET" not in r.detail + r.remedy

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
    def test_a_fifo_does_not_hang_the_check(self, make_config, tmp_path):
        """`doctor` runs on the daemon's start-up path, where a blocking
        `open(2)` has no timeout over it at all."""
        config = self._config(make_config, tmp_path)
        os.mkfifo(self._overlays(config) / "developer.md")

        r = self._run(config)
        assert r.status == WARN
        assert "overlay_not_a_regular_file" in r.detail

    def test_no_overlay_content_ever_leaves_the_users_directory(
        self, make_config, tmp_path
    ):
        """The same result is rendered into the admin dashboard, which every
        admin reads. A filename is the most that may cross out of one user's
        tree."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "develper.md").write_text(
            "- alice's private rule about her doctor\n"
        )
        r = self._run(config)
        assert "private rule" not in r.detail + r.remedy

    # ------------------------------------------------- typo versus scratch file

    def test_a_stray_file_warns_rather_than_failing(self, make_config, tmp_path):
        """The overlay directory is inside the tree `build_bwrap_cmd` binds
        read-write into the user's sandbox, so any task can create a file here
        with one `touch`. A deployment-scope FAIL an ordinary task can pin red
        is an alert an operator learns to skip past."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "zzz.md").write_text("- scratch\n")
        r = self._run(config)
        assert r.status == WARN
        assert "zzz.md" in r.detail
        assert "unknown_skill" in r.detail
        assert "unknown_skill" in r.remedy

    def test_a_near_miss_names_the_skill_it_probably_meant(
        self, make_config, tmp_path
    ):
        """A typo is the case the check exists for, so it keeps FAIL — and the
        suggestion is what makes the report actionable without opening a shell
        on the deployment."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "develper.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "did you mean developer" in r.detail

    def test_a_typo_of_a_denylisted_name_still_fails(self, make_config, tmp_path):
        """`sensitive_actions` takes no overlay, but a misspelling of it is
        still a file its author believed was live. The note must not suggest
        renaming to it — that name is refused by the write path and FAILs here
        as `denylisted`."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "sensitive_action.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "sensitive_actions" in r.detail
        assert "takes no overlay" in r.detail

    def test_a_stray_file_does_not_hide_a_typo_beside_it(
        self, make_config, tmp_path
    ):
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        (d / "zzz.md").write_text("- scratch\n")
        (d / "develper.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "develper.md" in r.detail

    def test_a_stray_file_does_not_mask_a_denylisted_one(
        self, make_config, tmp_path
    ):
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        (d / "zzz.md").write_text("- scratch\n")
        (d / "sensitive_actions.md").write_text("- planted\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "denylisted" in r.detail

    def test_a_case_difference_alone_is_a_typo(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "Developer.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "did you mean developer" in r.detail

    def test_a_backup_of_a_real_overlay_warns_rather_than_failing(
        self, make_config, tmp_path
    ):
        """`<skill>2`, `<skill>~` and `<skill>.bak` are what an editor and a
        task leave behind, and every one of them is one edit from the name it
        was copied from — so the distance test alone reads the whole class as
        misspellings and this is the largest hole in the fix."""
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        for name in ("developer2.md", "developer~.md", "notes.bak.md"):
            (d / name).write_text("- a copy\n")
        r = self._run(config)
        assert r.status == WARN
        assert "developer2.md" in r.detail

    def test_a_fail_never_hides_the_files_that_only_warn(
        self, make_config, tmp_path
    ):
        """One planted typo must not suppress the rest of the report. Before
        the severity split every unknown name was fatal, so every one was
        named; afterwards a FAIL branch that returned without rendering
        `warned` reported "1 of 21" and hid twenty files that also reach no
        prompt — a count that reads in the reassuring direction, and a
        suppression an attacker can aim with a single `touch`."""
        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        (d / "develper.md").write_text("- a rule\n")
        for i in range(20):
            (d / f"projectnotes{i}.md").write_text("- scratch\n")

        r = self._run(config)
        assert r.status == FAIL
        assert "1 of 21" in r.detail
        assert "20 more" in r.detail
        assert "projectnotes0.md" in r.detail
        # The glossary for the warned half travels with the FAIL remedy.
        assert "unknown_skill on its own" in r.remedy

    def test_a_fail_does_not_hide_an_overlay_near_the_loading_cap(
        self, make_config, tmp_path
    ):
        """The pointed version of the case above: the file being hidden is a
        real, loading overlay a few KB from the cliff past which it silently
        stops reaching any prompt."""
        from istota.skills._loader import OVERLAY_WARN_BYTES

        config = self._config(make_config, tmp_path)
        d = self._overlays(config)
        (d / "developer.md").write_text("- x\n" * (OVERLAY_WARN_BYTES // 4 + 4))
        (d / "notse.md").write_text("- planted\n")

        r = self._run(config)
        assert r.status == FAIL
        assert "over_warn_bytes" in r.detail
        assert "developer.md" in r.detail

    def test_a_short_stray_name_is_not_read_as_a_typo(self, make_config, tmp_path):
        """`nte` is two edits from `notes`, which the long budget would accept
        and the short one does not — the shorter the name, the more of it two
        edits are, and the more a loose budget turns a scratch file into an
        alert. The predicate itself is pinned in `TestOverlayNearMiss`."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config) / "nte.md").write_text("- scratch\n")
        r = self._run(config)
        assert r.status == WARN


class TestOverlayNearMiss:
    """The predicate separating a misspelled overlay from a scratch file."""

    KNOWN = ("developer", "notes", "browse", "sensitive_actions")

    def _near(self, stem):
        from istota.doctor import _overlay_near_miss

        return _overlay_near_miss(stem, self.KNOWN)

    def test_an_exact_name_is_not_a_near_miss(self):
        # The caller only asks about names the index already rejected, but the
        # predicate must not claim a name is a typo of itself.
        assert self._near("developer") is None

    def test_one_edit_on_a_long_name_is_a_typo(self):
        assert self._near("develper") == "developer"
        assert self._near("developerr") == "developer"
        assert self._near("dveloper") == "developer"

    def test_two_edits_on_a_long_name_are_a_typo(self):
        assert self._near("sensitiveactons") == "sensitive_actions"

    def test_three_edits_are_not(self):
        assert self._near("develo") is None

    def test_a_short_name_gets_a_tighter_budget(self):
        assert self._near("note") == "notes"
        assert self._near("nots") == "notes"
        # Two edits, which the long budget accepts and the short one does not.
        assert self._near("nte") is None

    def test_the_budget_switches_at_the_stated_length(self):
        from istota.doctor import _OVERLAY_TYPO_SHORT_NAME_CHARS, _overlay_near_miss

        assert _OVERLAY_TYPO_SHORT_NAME_CHARS == 5
        # Four characters, two edits from `browse`: short budget, so no.
        assert _overlay_near_miss("brse", ("browse",)) is None
        # Five characters, two edits from `browser`: long budget, so yes.
        assert _overlay_near_miss("brwse", ("browse",)) == "browse"

    def test_case_is_ignored(self):
        assert self._near("NOTES") == "notes"

    def test_an_unrelated_name_is_not_a_typo(self):
        assert self._near("zzz") is None
        assert self._near("scratch") is None
        assert self._near("") is None

    # ------------------------------------------------ copies, not misspellings

    def test_the_closest_candidate_wins_over_a_further_one(self):
        from istota.doctor import _overlay_near_miss

        # `notez` is one edit from `notes` and two from `notest`. Returning the
        # first sorted match rather than the closest would answer `notest`.
        assert _overlay_near_miss("notez", ("notest", "notes")) == "notes"
        assert _overlay_near_miss("notez", ("notes", "notest")) == "notes"

    def test_a_tie_is_broken_by_name_not_by_iteration_order(self):
        from istota.doctor import _overlay_near_miss

        # Both are exactly one edit away, so only the sort makes the answer
        # the same for two callers holding the same names in a different order.
        assert _overlay_near_miss("noteX", ("noteb", "notea")) == "notea"
        assert _overlay_near_miss("noteX", ("notea", "noteb")) == "notea"

    def test_a_singular_of_a_real_skill_is_still_a_typo(self):
        """The plural slip is the most common misspelling there is, and a file
        named `note.md` for the `notes` skill reaches no prompt at all."""
        assert self._near("note") == "notes"


class TestClassifyUnknownOverlay:
    """Severity and wording for a filename the skill index rejected.

    One helper decides both, because every earlier version of this had a label
    stating a reason the branch above it had not used.
    """

    KNOWN = ("developer", "notes", "browse", "sensitive_actions")

    def _classify(self, stem):
        from istota.doctor import _classify_unknown_overlay

        return _classify_unknown_overlay(stem, self.KNOWN)

    @pytest.mark.parametrize(
        "stem",
        [
            "notes2", "notes-1", "notes~", "notes.bak", "notes.tmp",
            "notes-old", "notes_new", "notes copy", "notes.orig",
            "notes backup", "notes.save", "notes v2", "notes.bak2",
        ],
    )
    def test_a_copy_of_a_real_overlay_warns_and_says_what_it_copies(self, stem):
        """Each is one or two edits from the name it was made from, so distance
        alone reads the whole class as misspellings. Whoever made it misspelled
        nothing — and the label has to say that, or the WARN remedy tells the
        operator the name is 'not close enough to a skill to be a typo', which
        for `notes2` is arithmetically false."""
        fails, note = self._classify(stem)
        assert fails is False
        assert note == "unknown_skill, a copy of notes.md"

    def test_a_copy_marker_on_a_name_that_is_not_a_skill_is_still_a_typo(self):
        # Strips to `develper`, which is not a skill, so it falls through.
        fails, note = self._classify("develper2")
        assert fails is True
        assert "did you mean developer" in note

    def test_a_bare_copy_marker_is_not_a_skill_name_plus_a_suffix(self):
        for stem in ("2", "~", "bak"):
            assert self._classify(stem) == (False, "unknown_skill")

    def test_a_typo_is_fatal_and_names_the_skill(self):
        assert self._classify("develper") == (
            True, "unknown_skill, did you mean developer?"
        )

    def test_a_typo_of_a_denylisted_name_does_not_suggest_a_rename(self):
        """`sensitive_actions` takes no overlay and the write path refuses it,
        so `did you mean sensitive_actions?` would walk the operator from this
        FAIL straight into the next one."""
        fails, note = self._classify("sensitive_action")
        assert fails is True
        assert "takes no overlay" in note
        assert "did you mean" not in note

    @pytest.mark.parametrize(
        "stem",
        ["developer.local", "01-developer", "developer-overlay", "developer_overlay",
         "my-developer-rules"],
    )
    def test_a_name_built_around_a_real_skill_is_fatal(self, stem):
        """Distance is blind in exactly this direction: the more deliberately a
        name is decorated the further it gets from the skill, while its author's
        belief that the file was live only gets more obvious."""
        fails, note = self._classify(stem)
        assert fails is True
        assert "names the developer skill but is not developer.md" in note

    def test_a_two_word_skill_needs_both_words(self):
        assert self._classify("sensitive-actions-old")[0] is True
        assert self._classify("actions-only")[0] is False

    def test_a_scratch_name_is_neither(self):
        assert self._classify("zzz") == (False, "unknown_skill")
        assert self._classify("scratch") == (False, "unknown_skill")

    def test_a_copy_beats_a_containment_match(self):
        # `developer2` names `developer` and is also a copy of it. The copy
        # reading is the quieter and the correct one.
        assert self._classify("developer2") == (
            False, "unknown_skill, a copy of developer.md"
        )



def _now_iso() -> str:
    """A timestamp the staleness bound reads as fresh."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TestAvatarImport:
    """`web.avatar_import` — configuration and recorded state, and no socket.

    Stage 4 of the profile-icons spec. The check runs on the daemon's start-up
    path, on a scheduler interval, from `istota doctor` and from the admin
    dashboard's Health pane, so a live Nextcloud call here would hang the admin
    page behind a remote timeout. That is the same reasoning that keeps
    `web.basemap` from making a request, and it gets an assertion rather than a
    docstring.
    """

    @staticmethod
    def _config(make_config, db_path, **overrides):
        from istota.config import NextcloudConfig, SchedulerConfig, WebConfig

        settings = {
            "db_path": db_path,
            "nextcloud": NextcloudConfig(url="https://cloud.example"),
            "web": WebConfig(enabled=True, avatar_import_from_nextcloud=True),
            "scheduler": SchedulerConfig(avatar_import_interval=21600),
        }
        settings.update(overrides)
        return make_config(**settings)

    @staticmethod
    def _run(config):
        return run_checks(config, only=("web.avatar_import",))[0]

    def test_skips_on_a_local_storage_backend(self, make_config, db_path):
        from istota.config import NextcloudConfig

        r = self._run(self._config(make_config, db_path,
                                   nextcloud=NextcloudConfig(url="")))
        assert r.status == SKIP
        assert "nextcloud" in r.detail.lower()

    def test_skips_when_the_import_is_switched_off(self, make_config, db_path):
        from istota.config import WebConfig

        r = self._run(self._config(
            make_config, db_path,
            web=WebConfig(enabled=True, avatar_import_from_nextcloud=False),
        ))
        assert r.status == SKIP

    def test_skips_when_the_interval_is_zero(self, make_config, db_path):
        from istota.config import SchedulerConfig

        r = self._run(self._config(
            make_config, db_path, scheduler=SchedulerConfig(avatar_import_interval=0),
        ))
        assert r.status == SKIP

    def test_reports_that_no_tick_has_run_yet_without_paging_anyone(
        self, make_config, db_path
    ):
        """The daemon runs the first tick seconds after boot, and doctor's own
        boot run comes before it. A WARN here would fire on every restart."""
        r = self._run(self._config(make_config, db_path))
        assert r.status == OK
        assert "no import tick" in r.detail.lower()

    def test_a_tick_every_user_failed_is_not_an_ok(self, make_config, db_path):
        """`failed` used to be rendered and gate nothing, so a deployment whose
        every fetch raised — wrong username, expired app password, uids that
        match no Nextcloud account — printed its failure count inside a green
        line. Reading the row and ignoring the one column that says it is not
        working gives up the only thing this socket-free check has."""
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": _now_iso(), "users": 5, "imported": 0, "no_custom": 0,
                 "unchanged": 0, "failed": 5,
                 "header": avatars.HEADER_UNOBSERVED},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == WARN
        assert "every user" in r.detail
        assert r.remedy

    def test_a_tick_with_failures_but_progress_is_still_ok(
        self, make_config, db_path
    ):
        """The control: one unreachable account among many must not warn, or
        the check cries wolf on every deployment with a stale user in [users]."""
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": _now_iso(), "users": 5, "imported": 2, "no_custom": 2,
                 "unchanged": 0, "failed": 1, "header": avatars.HEADER_SEEN},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == OK

    def test_a_tick_that_has_not_run_in_days_is_not_an_ok(
        self, make_config, db_path
    ):
        """Two documented paths stop this job silently and leave the last good
        row standing: a wedged fetch means `_spawn_background_check` never
        starts another run, and an unreadable probe state returns without
        recording anything."""
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": "2019-01-01T00:00:00Z", "users": 2, "imported": 1,
                 "no_custom": 1, "unchanged": 0, "failed": 0,
                 "header": avatars.HEADER_SEEN},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == WARN
        assert "may have stopped" in r.detail

    def test_an_unreadable_timestamp_is_not_reported_as_stale(
        self, make_config, db_path
    ):
        """A check never raises, and it does not invent a fault either. `at` is
        a JSON value out of a KV table; a shape change must not turn a healthy
        import into a warning."""
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": "not-a-timestamp", "users": 1, "imported": 1,
                 "no_custom": 0, "unchanged": 0, "failed": 0,
                 "header": avatars.HEADER_SEEN},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == OK

    def test_reports_the_recorded_state(self, make_config, db_path):
        from istota import avatars
        from istota import db as db_module

        recorded_at = _now_iso()
        with db_module.get_db(db_path) as conn:
            avatars.put_user_avatar(
                conn, "alice", source=avatars.SOURCE_NEXTCLOUD,
                image=b"not-really-an-image", content_hash="deadbeef",
                remote_etag='"e1"',
            )
            avatars.touch_import_probe(conn, "bob", remote_etag='"g"')
            avatars.write_import_state(
                conn,
                {"at": recorded_at, "users": 5, "imported": 1,
                 "no_custom": 1, "unchanged": 3, "failed": 0,
                 "header": avatars.HEADER_SEEN},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == OK
        assert recorded_at in r.detail
        # Every counter the tick records is rendered. `unchanged` is the steady
        # state, so omitting it made a healthy deployment report numbers that
        # did not add up to the user count printed beside them.
        assert "5 users" in r.detail
        assert "1 imported" in r.detail
        assert "1 with no custom avatar" in r.detail
        assert "3 unchanged" in r.detail
        assert "0 failed" in r.detail
        assert "1 imported" in r.detail
        assert "1 with no custom" in r.detail

    def test_a_missing_custom_avatar_header_warns_with_a_remedy(
        self, make_config, db_path
    ):
        """The one finding here that is worth an operator's attention: the
        header is how a user-set picture is told from the coloured letter
        Nextcloud generates, so without it nothing will ever be imported."""
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": "2026-08-30T09:00:00Z", "users": 2, "imported": 0,
                 "no_custom": 2, "failed": 0, "header": avatars.HEADER_ABSENT},
            )

        r = self._run(self._config(make_config, db_path))

        assert r.status == WARN
        assert r.remedy
        assert "avatar_import_from_nextcloud" in r.remedy

    def test_a_tick_that_observed_nothing_is_not_reported_as_a_missing_header(
        self, make_config, db_path
    ):
        from istota import avatars
        from istota import db as db_module

        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(
                conn,
                {"at": _now_iso(), "users": 1, "imported": 0,
                 "no_custom": 0, "failed": 0, "header": avatars.HEADER_UNOBSERVED},
            )

        r = self._run(self._config(make_config, db_path))
        assert r.status == OK

    def test_an_unreadable_database_is_reported_rather_than_raised(
        self, make_config, tmp_path
    ):
        missing = tmp_path / "nothing" / "istota.db"
        r = self._run(self._config(make_config, missing))
        # WARN specifically, not "WARN or SKIP": with this fixture's config
        # every SKIP branch in the check is unreachable, so accepting SKIP
        # would pass a future regression in the gates.
        assert r.status == WARN

    def test_it_opens_no_socket(self, make_config, db_path, monkeypatch):
        """`doctor` runs on the daemon's boot path and behind the admin Health
        pane. A remote call here hangs both."""
        import socket

        attempts: list[str] = []

        def _refuse(target):
            def _fn(*args, **kwargs):
                attempts.append(target)
                raise OSError(f"network blocked: {target}")

            return _fn

        monkeypatch.setattr(socket.socket, "connect", _refuse("connect"))
        monkeypatch.setattr(socket.socket, "connect_ex", _refuse("connect_ex"))
        monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
        monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))

        r = self._run(self._config(make_config, db_path))

        assert not attempts, f"web.avatar_import reached the network: {attempts}"
        assert r.status in (OK, SKIP, WARN)


class TestSessionLogDir:
    """`runtime.session_log_dir` — where the native brain's transcripts land, and
    whether the sandbox's database mask actually covers them.

    The check must ask `_mask_dir`'s own question rather than a copy of it. "Is
    the resolved directory under `db_path.parent`" answers True on the
    standalone install, where the mask is refused — so a checker with its own
    copy of the rule would report the property holding while the directory sat
    outside every mask. That is the `map_basemap` two-consumers failure, and the
    WARN/OK pair below is what proves the predicate is the real one.
    """

    NAME = "runtime.session_log_dir"

    def _config(self, make_config, tmp_path, **session_log_kwargs):
        from istota.config import BrainConfig, NativeBrainConfig, SessionLogConfig

        home = tmp_path / "srv"
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "data" / "istota.db").touch()
        temp = tmp_path / "tmp" / "istota"
        temp.mkdir(parents=True, exist_ok=True)
        return make_config(
            db_path=home / "data" / "istota.db",
            temp_dir=temp,
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(
                    session_log=SessionLogConfig(**session_log_kwargs),
                ),
            ),
        )

    def _run(self, config):
        return run_checks(config, only=(self.NAME,))[0]

    @pytest.fixture(autouse=True)
    def _bwrap_works_here(self, monkeypatch):
        """Every test in this class runs as if bubblewrap works.

        The check consults `executor.effective_sandboxing`, which is False on
        any non-Linux host and on a Linux host without a usable bwrap. Without
        this the whole class would answer on the availability axis on a
        developer machine and never reach the mask reasoning it exists to pin —
        and the OK cases would pass or fail depending on who ran them. The
        tests that are *about* that axis patch it back themselves.

        **Both the function and the memo**, because the check reads each by a
        different route: `effective_sandboxing` calls the function,
        `effective_sandboxing_if_known` reads `_bwrap_checked` directly. That
        global is set once per process and never invalidated, so leaving it
        alone would make this class's answer depend on whatever ran earlier in
        the same xdist worker — and the suite runs `-n auto`.
        """
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_checked", True)

    # -- when it does not apply -------------------------------------------

    def test_skips_when_no_routing_reaches_the_native_brain(self, make_config, tmp_path):
        config = self._config(
            make_config, tmp_path, retention_days=0, max_total_gb=0,
        )
        config.brain.kind = "claude_code"
        r = self._run(config)
        assert r.status == SKIP
        assert "native" in r.detail

    def test_checks_retention_when_no_routing_reaches_native(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        config.brain.kind = "claude_code"
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, still_over=True)
        assert self._run(config).status == WARN

    def test_a_native_fallback_is_not_a_skip(self, make_config, tmp_path):
        # `brain/native.py` builds the writer from `session_log` alone and
        # consults `brain.kind` nowhere, so a `claude_code` primary with
        # `fallback = "native"` writes a transcript on every availability
        # failover. Gating on `kind` SKIPs on exactly the mixed-brain deployment
        # nobody would think to look at.
        config = self._config(make_config, tmp_path)
        config.brain.kind = "claude_code"
        config.brain.fallback = "native"
        assert self._run(config).status != SKIP

    def test_a_source_type_override_onto_native_is_not_a_skip(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        config.brain.kind = "claude_code"
        config.brain.source_type_overrides = {"scheduled": "native"}
        assert self._run(config).status != SKIP

    def test_skips_when_the_feature_is_off(self, make_config, tmp_path):
        config = self._config(
            make_config, tmp_path, enabled=False, retention_days=0, max_total_gb=0,
        )
        r = self._run(config)
        assert r.status == SKIP

    def test_checks_retention_when_the_feature_is_off(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, enabled=False)
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, still_over=True)
        assert self._run(config).status == WARN

    # -- the healthy shape -------------------------------------------------

    def test_ok_on_the_default_directory(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        log_dir = config.db_path.parent / "logs"
        log_dir.mkdir(parents=True)
        r = self._run(config)
        assert r.status == OK
        assert str(log_dir) in r.detail

    def test_ok_before_the_directory_exists(self, make_config, tmp_path):
        # Nothing creates it until the first native task, so an install that has
        # not run one yet is healthy rather than broken.
        r = self._run(self._config(make_config, tmp_path))
        assert r.status == OK

    def test_the_ok_line_reports_the_size_against_the_ceiling(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, max_total_gb=5.0)
        log_dir = config.db_path.parent / "logs" / "alice"
        log_dir.mkdir(parents=True)
        (log_dir / "a.jsonl").write_bytes(b"x" * 4096)
        r = self._run(config)
        assert r.status == OK
        assert "5.0" in r.detail
        assert "1 file" in r.detail

    # -- the exposures -----------------------------------------------------

    def test_warns_when_the_directory_is_outside_the_masked_one(self, make_config, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        config = self._config(make_config, tmp_path, dir=str(elsewhere))
        r = self._run(config)
        assert r.status == WARN
        assert str(elsewhere) in r.detail
        assert r.remedy

    def _workspace_shape(self, make_config, tmp_path, *, sandbox_enabled):
        """`setup_wizard`'s layout: db_path.parent *is* the workspace and the
        temp dir is inside it, so `_mask_dir` refuses.

        `sandbox_enabled` is passed explicitly in both directions because the
        two halves are different findings and the review found the test asserting
        one while naming the other: `setup_wizard` ships `sandbox_enabled = false`,
        so the shape it writes never reaches the mask reasoning at all.
        """
        from istota.config import (
            BrainConfig,
            NativeBrainConfig,
            SecurityConfig,
            SessionLogConfig,
        )

        workspace = tmp_path / "istota-home"
        (workspace / "tmp").mkdir(parents=True)
        (workspace / "istota.db").touch()
        return make_config(
            db_path=workspace / "istota.db",
            temp_dir=workspace / "tmp",
            nextcloud_mount_path=workspace,
            security=SecurityConfig(sandbox_enabled=sandbox_enabled),
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(session_log=SessionLogConfig()),
            ),
        )

    def test_warns_on_the_standalone_shape_where_the_mask_is_refused(
        self, make_config, tmp_path,
    ):
        # "Under db_path.parent" answers True here, which is why the check cannot
        # be written that way.
        config = self._workspace_shape(make_config, tmp_path, sandbox_enabled=True)
        r = self._run(config)
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "workspace" in r.detail.lower()
        assert r.remedy

    def test_warns_when_the_sandbox_is_switched_off_entirely(
        self, make_config, tmp_path,
    ):
        # On a layout whose mask would otherwise be *emitted*, so the only thing
        # that can produce a finding is the sandbox being off. Writing this
        # against the workspace shape — which `setup_wizard` really ships, and
        # which is the tempting way to phrase it — passes on the mask-refusal
        # reason instead, and stays green with this arm deleted. Measured.
        from istota.config import SecurityConfig

        config = self._config(make_config, tmp_path)
        config.security = SecurityConfig(sandbox_enabled=False)
        r = self._run(config)
        assert r.status == WARN
        assert "switched off on this deployment" in r.detail
        assert r.remedy

    def test_the_standalone_install_as_shipped_warns_on_both_counts(
        self, make_config, tmp_path,
    ):
        # `setup_wizard` writes the workspace layout *and* `sandbox_enabled =
        # false`, so both conditions hold at once. Whichever reason is reported,
        # the finding must be there — gating the mask arm on `sandbox_enabled`
        # made this shape report a plain OK.
        config = self._workspace_shape(make_config, tmp_path, sandbox_enabled=False)
        r = self._run(config)
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert r.remedy
        # Both, by name. "Whichever reason is reported" was as much as the
        # early-return version could promise; the one shipped shape that fails
        # both axes is now pinned at both rather than at "some finding".
        assert "switched off on this deployment" in r.detail
        assert "workspace" in r.detail.lower()

    def test_the_two_shapes_disagree_which_is_the_point_of_the_check(
        self, make_config, tmp_path,
    ):
        # The pair, side by side: the Ansible shape is OK and the standalone one
        # WARNs, on a predicate that would answer the same for both if it were
        # the "under db_path.parent" copy. Both have the sandbox on, so the only
        # thing separating them is the mask refusal.
        ansible = self._config(make_config, tmp_path / "a")
        standalone = self._workspace_shape(
            make_config, tmp_path / "b", sandbox_enabled=True,
        )
        assert self._run(ansible).status == OK
        assert self._run(standalone).status == WARN

    # -- the availability axis ---------------------------------------------

    def test_warns_when_bubblewrap_does_not_work_on_this_deployment(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The shipped Docker stack: `docker-compose.yml` grants neither
        # `seccomp:unconfined` nor `systempaths=unconfined`, so the probe fails,
        # `build_bwrap_cmd` never runs and no mask is emitted — while
        # `sandbox_enabled` still reads true. On the layout whose mask *would*
        # cover the directory, so the only thing that can produce a finding here
        # is the sandbox not actually being in place.
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = self._run(config)
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "bubblewrap" in r.detail.lower()
        assert r.remedy

    def test_a_nested_probe_is_unknown_rather_than_an_exposure(
        self, make_config, tmp_path, monkeypatch,
    ):
        """Run inside a task's own sandbox the availability probe answers
        nothing, and this check must not read that as the logs being unbound.

        Same defect as `security.sandbox_effective`, one prefix along: the
        finding here is phrased as an observation about the deployment, and a
        nested probe observed only its own namespace. Both halves are asserted,
        because the reason text alone would satisfy either prefix.
        """
        from istota import executor

        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = self._run(config)

        assert "could not be established" in r.detail
        assert "unbound rather than masked" not in r.detail
        assert "ISTOTA_SANDBOXED" in r.detail

    def test_an_unavailable_sandbox_and_a_refused_mask_are_both_reported(
        self, make_config, tmp_path, monkeypatch,
    ):
        # Precedent: `test_the_standalone_install_as_shipped_warns_on_both_counts`
        # below. Neither reason pre-empts the other, because an operator who
        # reads one and fixes it would otherwise be told nothing about the
        # second and would still have unmasked transcripts.
        from istota import executor

        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        config = self._workspace_shape(make_config, tmp_path, sandbox_enabled=True)
        r = self._run(config)
        assert r.status == WARN
        assert "bubblewrap" in r.detail.lower()
        assert "workspace" in r.detail.lower()
        # Availability leads, on this arm as on the switched-off one. Asserted
        # here rather than only there because the ordering test below drives the
        # *pre-existing* branch, so it stays green with this arm deleted.
        assert r.detail.index("bubblewrap") < r.detail.index("workspace")

    def test_the_availability_reason_is_reported_before_the_mask_shape(
        self, make_config, tmp_path,
    ):
        # Order pinned rather than left to whichever arm happens to run first:
        # whether a mask exists at all outranks where it would land if it did.
        config = self._workspace_shape(make_config, tmp_path, sandbox_enabled=False)
        r = self._run(config)
        assert r.detail.index("switched off") < r.detail.index("workspace")

    def _recording_probe(self, monkeypatch, *, cached, answer=True):
        """Stand in for the bwrap probe, recording whether it was invoked.

        The spawn question cannot be asked of `subprocess` here. Two things
        answer it before a process is created — `_bwrap_available` returns False
        at its `sys.platform` check on this developer machine, and it memoizes
        in `_bwrap_checked` after the first call anywhere in the process — so a
        `subprocess` spy stays empty whether or not the gate exists. Measured:
        with the `probe` gate deleted, the spy is still empty and this recorder
        fires.
        """
        from istota import executor

        calls: list[str] = []

        def _probe():
            calls.append("bwrap")
            return answer

        monkeypatch.setattr(executor, "_bwrap_available", _probe)
        monkeypatch.setattr(executor, "_bwrap_checked", cached)
        return calls

    def test_probe_false_does_not_claim_a_mask_it_could_not_verify(
        self, make_config, tmp_path, monkeypatch,
    ):
        # `probe=False` forbids spawning and the bwrap probe is a spawn, so with
        # a cold memo the availability axis cannot be answered at all. The cheap
        # half alone cannot tell the Ansible shape from the Docker one, and
        # reporting OK there is the defect this check had.
        calls = self._recording_probe(monkeypatch, cached=None)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = run_checks(config, only=(self.NAME,), probe=False)[0]
        assert calls == [], "probe=False invoked the bwrap probe"
        assert r.status == WARN
        assert "not probed" in r.detail

    def test_an_unestablished_answer_does_not_assert_that_the_logs_are_unbound(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The finding prefix used to be fixed, so an unanswerable availability
        # question rendered as "the logs are unbound rather than masked — [it]
        # was not probed": a sentence asserting the exposure in its first clause
        # and disclaiming it in the second, on a deployment whose mask is fine.
        self._recording_probe(monkeypatch, cached=None)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = run_checks(config, only=(self.NAME,), probe=False)[0]
        assert "could not be established" in r.detail
        assert "unbound" not in r.detail.lower()

    def test_probe_false_answers_from_a_warm_memo_rather_than_declining_to_look(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The daemon probes at start-up (`_log_startup_status`), so inside that
        # process the answer is free. Saying "not probed" while `_bwrap_checked`
        # holds it is a statement about the world that is wrong — and it would
        # be the one that mattered, since a warm memo of False is the Docker
        # shape this issue is about.
        calls = self._recording_probe(monkeypatch, cached=False)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = run_checks(config, only=(self.NAME,), probe=False)[0]
        assert calls == []
        assert r.status == WARN
        assert "bubblewrap does not work" in r.detail
        assert "not probed" not in r.detail

    def test_an_availability_question_that_raises_is_a_finding_not_a_pass(
        self, make_config, tmp_path, monkeypatch,
    ):
        # Swallowing to `True` reinstated ISSUE-381 in miniature: an answer
        # nobody could get, reported as a protection in place, with only a debug
        # line behind it. `effective_sandboxing` catches nothing itself and
        # `_bwrap_available` catches only OSError and TimeoutExpired.
        from istota import executor

        def _boom(_config):
            raise RuntimeError("no answer available")

        monkeypatch.setattr(executor, "effective_sandboxing", _boom)
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = self._run(config)
        assert r.status == WARN
        assert "could not be determined" in r.detail

    @pytest.mark.requires_dac
    def test_fails_on_an_unwritable_directory(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        log_dir = config.db_path.parent / "logs"
        log_dir.mkdir(parents=True)
        os.chmod(log_dir, 0o500)
        try:
            r = self._run(config)
        finally:
            os.chmod(log_dir, 0o700)
        assert r.status == FAIL
        assert r.remedy

    # -- the ceiling is what actually binds --------------------------------

    def _record_sweep(self, config, **fields):
        from istota import db as _db
        from istota.session.session_log import (
            SWEEP_STATE_KEY,
            SWEEP_STATE_NAMESPACE,
            SweepResult,
            encode_sweep_state,
        )

        _db.init_db(config.db_path)
        with _db.get_db(config.db_path) as conn:
            _db.shared_kv_set(
                conn,
                SWEEP_STATE_NAMESPACE,
                SWEEP_STATE_KEY,
                encode_sweep_state(SweepResult(**fields), now=time.time()),
                "test",
            )

    def test_warns_when_the_last_sweep_evicted_by_size(self, make_config, tmp_path):
        # `deleted_size > 0` means the effective retention is a function of load
        # rather than `retention_days`. An operator who wanted 14 days and is
        # getting 3 should be told, not left to infer it from a listing.
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, deleted_size=7)
        r = self._run(config)
        assert r.status == WARN
        assert "retention" in r.detail.lower()
        assert r.remedy

    def test_a_sweep_that_evicted_only_by_age_is_ok(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, deleted_age=12)
        assert self._run(config).status == OK

    def test_still_over_outranks_an_eviction_by_size(self, make_config, tmp_path):
        # The worse condition: the tree is over its ceiling and everything left
        # is inside the live window, so nothing is reclaiming it at all. Recorded
        # by the sweep since Stage 1 and read by nobody until now.
        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, deleted_size=3, still_over=True)
        r = self._run(config)
        assert r.status == WARN
        assert "nothing it could evict" in r.detail
        assert r.remedy

    def test_the_exposure_and_the_retention_findings_are_composed_not_raced(
        self, make_config, tmp_path,
    ):
        # Returning at the first made the retention arm unreachable on exactly
        # the deployments that need it: an operator-set `dir` and the standalone
        # shape are both *permanent* exposure conditions, so the check could
        # never go on to say the ceiling was what actually bound.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        config = self._config(make_config, tmp_path, dir=str(elsewhere))
        self._record_sweep(config, deleted_size=4)
        r = self._run(config)
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "retention" in r.detail.lower()

    def test_a_stale_row_stops_warning_once_both_sweep_rules_are_off(
        self, make_config, tmp_path,
    ):
        # With both rules off the scheduler's gate is false and nothing rewrites
        # the row, so a `deleted_size` from before they were switched off would
        # warn for ever about a rule that no longer runs.
        config = self._config(make_config, tmp_path, retention_days=0, max_total_gb=0)
        (config.db_path.parent / "logs").mkdir(parents=True)
        self._record_sweep(config, deleted_size=9)
        assert self._run(config).status == OK

    def test_an_infinite_ceiling_reads_as_no_ceiling(self, make_config, tmp_path):
        # TOML spells `inf` and the sweep reads it as no ceiling. This is the one
        # place the two consumers of the setting could disagree about what an
        # operator is being told.
        config = self._config(make_config, tmp_path, max_total_gb=float("inf"))
        (config.db_path.parent / "logs").mkdir(parents=True)
        r = self._run(config)
        assert "of inf GB" not in r.detail
        assert "no ceiling configured" in r.detail

    def test_a_stray_file_at_the_root_is_not_counted(self, make_config, tmp_path):
        # The sweep measures per-user directories, so a file sitting in the root
        # is in no user's tree and its bytes reach neither `bytes_after` nor the
        # ceiling. Counting it here would inflate the figure reported *against*
        # that ceiling.
        config = self._config(make_config, tmp_path)
        log_dir = config.db_path.parent / "logs"
        (log_dir / "alice").mkdir(parents=True)
        (log_dir / "alice" / "a.jsonl").write_bytes(b"x" * 4096)
        (log_dir / "stray.jsonl").write_bytes(b"y" * 4096)
        r = self._run(config)
        assert "1 file" in r.detail

    def test_a_nested_file_inside_a_user_directory_is_counted(self, make_config, tmp_path):
        # The other half: the sweep walks a user's tree recursively, so the
        # measurement has to as well or the two disagree the other way.
        config = self._config(make_config, tmp_path)
        nested = config.db_path.parent / "logs" / "alice" / "deep"
        nested.mkdir(parents=True)
        (nested / "a.jsonl").write_bytes(b"x" * 4096)
        assert "1 file" in self._run(config).detail

    def test_an_unreadable_state_row_is_not_a_finding(self, make_config, tmp_path):
        from istota import db as _db
        from istota.session.session_log import SWEEP_STATE_KEY, SWEEP_STATE_NAMESPACE

        config = self._config(make_config, tmp_path)
        (config.db_path.parent / "logs").mkdir(parents=True)
        _db.init_db(config.db_path)
        with _db.get_db(config.db_path) as conn:
            _db.shared_kv_set(
                conn, SWEEP_STATE_NAMESPACE, SWEEP_STATE_KEY, "not json", "test",
            )
        assert self._run(config).status == OK

    def test_the_check_never_raises_on_a_broken_config(self, make_config, tmp_path):
        # It runs on the daemon's start-up path. A `dir` that names a file, not
        # a directory, must come back as a finding rather than an exception.
        config = self._config(make_config, tmp_path)
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        config.brain.native.session_log.dir = str(blocker)
        r = self._run(config)
        assert r.status in (WARN, FAIL)


class TestTaskControlDir:
    """`runtime.task_control_dir` — the daemon-owned tree the framework writes
    each task's prompt halves, briefing metadata and prepared image attachments
    into.

    Two independent questions, both answered on every run, in the shape
    `runtime.session_log_dir` set: is the tree itself a directory the daemon
    owns at 0700, and is anything model-writable at or above it. The second is
    what the whole change rests on — the tree is a *sibling* of the per-user
    workspaces rather than a child of one — and it is a property of the
    rendered config, which is exactly the class of fact the suite cannot see
    and doctor exists to name.

    The mask axis runs the other way from `runtime.session_log_dir`'s. There a
    mask over the directory is defence in depth and its absence is the finding;
    here a mask *over* the control tree takes away a file the task must open,
    so the finding is the mask existing.
    """

    NAME = "runtime.task_control_dir"

    def _config(self, make_config, tmp_path, *, users=("alice",), **overrides):
        from istota.config import UserConfig

        temp = tmp_path / "srv" / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        data = tmp_path / "srv" / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "istota.db").touch()
        # A mapping is taken as written, so a test that needs resources on a
        # user can pass one. Binding `users` as a named parameter and then
        # rebuilding it from the keys is how the first draft of the two
        # resource tests silently ran against a `UserConfig()` with no rows and
        # passed on an OK that meant nothing.
        if not isinstance(users, dict):
            users = {u: UserConfig() for u in users}
        kwargs = {
            "db_path": data / "istota.db",
            "temp_dir": temp,
            "users": users,
        }
        kwargs.update(overrides)
        return make_config(**kwargs)

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    def _root(self, config):
        from istota.executor import CONTROL_DIR_NAME

        return Path(config.temp_dir).resolve() / CONTROL_DIR_NAME

    # -- when it does not apply --------------------------------------------

    def test_skips_when_no_users_are_configured(self, make_config, tmp_path):
        # Nothing names a control directory until a task runs for somebody, and
        # a check with no subject must not report a property holding.
        r = self._run(self._config(make_config, tmp_path, users=()))
        assert r.status == SKIP
        assert "user" in r.detail

    # -- the healthy shape -------------------------------------------------

    def test_ok_before_the_tree_exists(self, make_config, tmp_path):
        # `ensure_task_control_dir` creates it on the first task, so an install
        # that has not run one is healthy rather than broken.
        config = self._config(make_config, tmp_path)
        r = self._run(config)
        assert r.status == OK
        assert str(self._root(config)) in r.detail

    def test_ok_on_a_well_formed_tree(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o700)
        (root / "alice").mkdir()
        os.chmod(root / "alice", 0o700)
        r = self._run(config)
        assert r.status == OK
        assert not r.remedy

    def test_the_ok_line_names_the_root_and_the_users_it_resolved_for(
        self, make_config, tmp_path,
    ):
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        r = self._run(config)
        assert r.status == OK
        assert str(self._root(config)) in r.detail
        assert "2" in r.detail

    # -- question one: is the tree itself ours ------------------------------

    def test_reports_a_widened_root(self, make_config, tmp_path):
        # `ensure_task_control_dir` re-asserts 0700 on the next task, so this
        # heals itself — but only while the daemon still owns the directory,
        # and until then every other local account can walk into it.
        #
        # `chmod` after the `mkdir` rather than `mode=`, here and below: the
        # umask subtracts from a `mkdir` mode, so the mode under test would be
        # whatever the ambient umask left of it.
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o755)
        r = self._run(config)
        assert r.status == WARN
        assert "0755" in r.detail
        assert r.remedy

    def test_fails_when_a_regular_file_sits_at_the_root(self, make_config, tmp_path):
        # `_ensure_control_level` refuses this with ENOTDIR, so every task of
        # every user fails at start-up until it is moved aside.
        config = self._config(make_config, tmp_path)
        self._root(config).write_text("not a directory")
        r = self._run(config)
        assert r.status == FAIL
        assert "not a directory" in r.detail
        assert r.remedy

    def test_fails_when_the_root_is_a_symlink(self, make_config, tmp_path):
        # A symlink is what `get_task_control_dir`'s containment equality is
        # resolving through when it refuses; the daemon owns the parent, so
        # this is a corrupt-state report rather than a boundary.
        config = self._config(make_config, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        self._root(config).symlink_to(elsewhere)
        r = self._run(config)
        assert r.status == FAIL
        assert "symlink" in r.detail
        assert r.remedy

    def test_reports_a_root_owned_by_another_account(
        self, make_config, tmp_path, monkeypatch,
    ):
        # `_ensure_control_level` fails closed on a uid mismatch, so this
        # predicts every task under the level failing. It is still a WARN: the
        # only uid available is this process's, `istota doctor` is commonly run
        # from an operator's own account, and a FAIL sets `exit_code` and mails
        # every admin from the scheduler's sweep — the same reason
        # `check_subscription_usage` never fails on a busy plan.
        #
        # The daemon's own uid is what moves, since a test cannot chown to
        # another account, and the real one is read *before* the patch, because
        # `doctor.os` is the `os` module itself and a lambda calling through it
        # recurses.
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o700)
        mine = os.geteuid()
        monkeypatch.setattr(doctor.os, "geteuid", lambda: mine + 1)
        r = self._run(config)
        assert r.status == WARN
        # Both uids, so a reader can tell which of the two cases they are in.
        assert f"owned by uid {mine}" in r.detail
        assert f"runs as uid {mine + 1}" in r.detail
        assert r.remedy

    def test_a_correctly_owned_tree_is_not_reported(self, make_config, tmp_path):
        # The other half of the pair. Without it the arm above would pass on a
        # check that reported a uid line unconditionally.
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o700)
        r = self._run(config)
        assert r.status == OK
        assert "uid" not in r.detail

    def test_reports_a_per_user_level_the_daemon_does_not_own(
        self, make_config, tmp_path, monkeypatch,
    ):
        # `_ensure_control_level` asserts type, ownership and mode at all three
        # levels and fails the task from any of them, so inspecting only the
        # root reports a healthy deployment while every task of one user fails.
        # `get_task_control_dir`'s containment equality does not cover it: it
        # compares paths and says nothing about who owns one.
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        (root / "alice").mkdir(parents=True)
        os.chmod(root, 0o700)
        os.chmod(root / "alice", 0o755)
        r = self._run(config)
        assert r.status == WARN
        assert "control directory of user 'alice'" in r.detail
        assert "0755" in r.detail
        assert r.remedy

    def test_fails_when_a_per_user_level_is_a_regular_file(
        self, make_config, tmp_path,
    ):
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o700)
        (root / "alice").write_text("not a directory")
        r = self._run(config)
        assert r.status == FAIL
        assert "control directory of user 'alice'" in r.detail

    # -- question two: is anything model-writable above it ------------------

    def test_reports_a_user_workspace_at_or_above_the_control_tree(
        self, make_config, tmp_path,
    ):
        # `{temp_dir}/{user}` is bound read-write into that user's own sandbox
        # and is the model's working directory. A link making it resolve to the
        # shared temp root puts every user's control tree inside that bind,
        # which is the one thing the sibling layout exists to prevent.
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        temp = Path(config.temp_dir)
        (temp / "bob").symlink_to(temp)
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail
        assert "bob" in r.detail
        assert r.remedy

    def test_reports_the_user_id_that_collides_with_the_control_directory(
        self, make_config, tmp_path,
    ):
        # `get_user_temp_dir` is a plain join, so a user of this name would put
        # their model-writable scratch directory exactly where the control root
        # goes. `get_task_control_dir` refuses the name; this is what says so
        # out loud, because the refusal alone is a user whose every task fails
        # with nothing in the config pointing at why.
        config = self._config(make_config, tmp_path, users=("alice", ".control"))
        r = self._run(config)
        assert r.status == FAIL
        # Both arms by name, because the refusal alone satisfies a loose
        # `".control" in detail` and the test would then stay green with the
        # overlap comparison deleted — the branch its comment is about.
        assert "no control directory can be named for '.control'" in r.detail
        assert "the workspace of user '.control'" in r.detail
        assert "overlaps the control tree" in r.detail
        assert r.remedy

    def test_reports_a_resource_row_that_would_bind_the_control_tree(
        self, make_config, tmp_path,
    ):
        # The gap Stage 3's review recorded and `native_fs_roots`' docstring
        # names: a `user_resources` row resolves to `mount / resource_path`,
        # bounded by the Nextcloud mount and nothing else, so on a layout where
        # `temp_dir` sits under the mount a row is a second route into the
        # whole tree that neither guard covers. No shipped shape produces the
        # layout; this is what would say so if one did.
        from istota.config import ResourceConfig, SecurityConfig, UserConfig

        mount = tmp_path / "mount"
        temp = mount / "tmp"
        temp.mkdir(parents=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            nextcloud_mount_path=mount,
            security=SecurityConfig(sandbox_enabled=True),
            users={
                "alice": UserConfig(
                    resources=[
                        ResourceConfig(
                            type="folder", path="tmp/.control",
                            permissions="readwrite",
                        ),
                    ],
                ),
            },
        )
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail
        assert "readwrite" in r.detail
        assert r.remedy

    def test_a_read_only_resource_row_over_the_tree_is_reported_too(
        self, make_config, tmp_path,
    ):
        # Read-only is the read exposure rather than the write vector, which is
        # the same thing `sandbox_ro_paths` warns about at load: one task
        # reading every other task of that user's assembled prompt.
        from istota.config import ResourceConfig, SecurityConfig, UserConfig

        mount = tmp_path / "mount"
        temp = mount / "tmp"
        temp.mkdir(parents=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            nextcloud_mount_path=mount,
            security=SecurityConfig(sandbox_enabled=True),
            users={
                "alice": UserConfig(
                    resources=[
                        ResourceConfig(type="folder", path="tmp", permissions="read"),
                    ],
                ),
            },
        )
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail

    def test_a_resource_row_is_not_reported_with_the_sandbox_switched_off(
        self, make_config, tmp_path,
    ):
        # Nothing is bound at all with the sandbox off, so there is no bind for
        # a row to widen. Same gate `config._warn_ro_paths_over_control_tree`
        # takes, and for the same reason: the *requested* flag, because the
        # effective one spawns.
        from istota.config import ResourceConfig, SecurityConfig, UserConfig

        mount = tmp_path / "mount"
        temp = mount / "tmp"
        temp.mkdir(parents=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            nextcloud_mount_path=mount,
            security=SecurityConfig(sandbox_enabled=False),
            users={
                "alice": UserConfig(
                    resources=[
                        ResourceConfig(
                            type="folder", path="tmp/.control",
                            permissions="readwrite",
                        ),
                    ],
                ),
            },
        )
        assert self._run(config).status == OK

    def test_a_resource_row_elsewhere_under_the_mount_is_not_a_finding(
        self, make_config, tmp_path,
    ):
        from istota.config import ResourceConfig, SecurityConfig, UserConfig

        mount = tmp_path / "mount"
        temp = mount / "tmp"
        temp.mkdir(parents=True)
        (mount / "Docs").mkdir(parents=True, exist_ok=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            nextcloud_mount_path=mount,
            security=SecurityConfig(sandbox_enabled=True),
            users={
                "alice": UserConfig(
                    resources=[
                        ResourceConfig(
                            type="folder", path="Docs", permissions="readwrite",
                        ),
                    ],
                ),
            },
        )
        assert self._run(config).status == OK

    def test_reports_a_repos_subtree_that_holds_the_control_tree(
        self, make_config, tmp_path,
    ):
        # Measured during review: with `temp_dir` under `{repos_dir}/{user}`
        # the whole control tree sits inside a read-write bind and the check
        # reported `ok`. `build_bwrap_cmd` binds that subtree for an admin
        # developer task, so it belongs on this axis with the other binds.
        from istota.config import DeveloperConfig, SecurityConfig

        repos = tmp_path / "repos"
        temp = repos / "alice" / "tmp"
        temp.mkdir(parents=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            security=SecurityConfig(sandbox_enabled=True),
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
        )
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail
        assert "repos subtree" in r.detail

    def test_reports_a_sandbox_ro_paths_entry_over_the_control_tree(
        self, make_config, tmp_path,
    ):
        # `config._warn_ro_paths_over_control_tree` already says this at load,
        # once per process, into a log. Doctor is the surface an operator
        # reads, and the axis already reports a read-only resource row, so
        # leaving the one entry that is bound verbatim off it was inconsistent
        # with its own rule.
        from istota.config import SecurityConfig

        config = self._config(make_config, tmp_path)
        config.security = SecurityConfig(
            sandbox_enabled=True, sandbox_ro_paths=[str(config.temp_dir)],
        )
        r = self._run(config)
        assert r.status == WARN
        assert "sandbox_ro_paths entry" in r.detail
        assert "overlaps the control tree" in r.detail

    # -- question two, second half: the database mask -----------------------

    def _bwrap(self, monkeypatch, *, available=True, cached=True):
        """Stand in for the bwrap capability probe, recording each invocation.

        `subprocess` cannot answer the spawn question here: `_bwrap_available`
        returns False at its `sys.platform` check on a macOS host and memoizes
        in `_bwrap_checked` after the first call anywhere in the process, so a
        `subprocess` spy stays empty whether or not the gate exists.
        """
        from istota import executor

        calls: list[str] = []

        def _probe():
            calls.append("bwrap")
            return available

        monkeypatch.setattr(executor, "_bwrap_available", _probe)
        monkeypatch.setattr(executor, "_bwrap_checked", cached)
        return calls

    def _masked_shape(self, make_config, tmp_path):
        """`db_path.parent` *is* the control root, so the mask lands on it.

        The one layout where the tree is swallowed: a mask anywhere above
        `temp_dir` shadows the workspace and `_mask_dir` refuses it, so the
        only candidate that reaches the tree is one at or inside it.
        """
        config = self._config(make_config, tmp_path)
        root = self._root(config)
        root.mkdir(mode=0o700, parents=True)
        config.db_path = root / "istota.db"
        config.db_path.touch()
        return config

    def test_reports_a_mask_that_swallows_the_control_tree(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The database mask is the last mount operation and cannot be worked
        # around, so a control directory under it could never be opened inside
        # the namespace — the composed system prompt would be named by the
        # request and unreadable, and every Claude Code task would fail at
        # start-up. `## Design` claims this holds on all three shipped shapes;
        # this is what checks the claim against a rendered config.
        self._bwrap(monkeypatch, available=True)
        r = self._run(self._masked_shape(make_config, tmp_path))
        assert r.status == WARN
        # The established prefix by name. "masked out of every sandbox" alone
        # is a substring of the "would be" prefix too, so it cannot separate
        # the two states the constants exist to keep apart.
        assert doctor._CONTROL_MASKED in r.detail
        assert r.remedy

    def test_reports_a_mask_that_lands_inside_the_control_tree(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The other direction. A mask above the tree takes every user's away; a
        # mask inside it takes one user's, and it is emitted rather than
        # refused, since `{temp_dir}/.control/{user}` shadows nothing in
        # `mask_protected_paths`. A one-directional test reported nothing here.
        self._bwrap(monkeypatch, available=True)
        config = self._config(make_config, tmp_path)
        inside = self._root(config) / "alice"
        inside.mkdir(parents=True)
        config.db_path = inside / "istota.db"
        config.db_path.touch()
        r = self._run(config)
        assert r.status == WARN
        assert doctor._CONTROL_MASKED in r.detail
        assert str(inside) in r.detail

    def test_a_mask_that_is_refused_does_not_produce_a_finding(
        self, make_config, tmp_path, monkeypatch,
    ):
        # `mask_shadowed_by` is the sandbox builder's own predicate rather than
        # a copy: "is the tree under db_path.parent" answers True on the
        # standalone install, where db_path.parent is the workspace and the
        # mask is refused, so a copy would report a mask that is never emitted.
        self._bwrap(monkeypatch, available=True)
        workspace = tmp_path / "istota-home"
        (workspace / "tmp").mkdir(parents=True)
        (workspace / "istota.db").touch()
        from istota.config import UserConfig

        config = make_config(
            db_path=workspace / "istota.db",
            temp_dir=workspace / "tmp",
            nextcloud_mount_path=workspace,
            users={"alice": UserConfig()},
        )
        r = self._run(config)
        assert "masked" not in r.detail

    def test_the_healthy_layout_asks_no_availability_question_at_all(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The availability answer cannot change the verdict where no mask would
        # reach the tree, and asking it spawns. `probe` exists to stop exactly
        # that, so the shape question is asked first and the probe is reached
        # only where its answer matters.
        calls = self._bwrap(monkeypatch, available=True, cached=None)
        assert self._run(self._config(make_config, tmp_path)).status == OK
        assert calls == []

    def test_probe_false_does_not_spawn_for_the_mask_question(
        self, make_config, tmp_path, monkeypatch,
    ):
        calls = self._bwrap(monkeypatch, cached=None)
        r = self._run(self._masked_shape(make_config, tmp_path), probe=False)
        assert calls == [], "probe=False invoked the bwrap probe"
        assert r.status == WARN
        assert "could not be established" in r.detail

    def test_an_unestablished_answer_does_not_assert_the_tree_is_masked(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The session-log check's lesson: a fixed prefix rendered a sentence
        # asserting the exposure in its first clause and disclaiming it in the
        # second. A boundary check must neither pass on a question it could not
        # settle nor assert a condition it did not observe.
        self._bwrap(monkeypatch, cached=None)
        r = self._run(self._masked_shape(make_config, tmp_path), probe=False)
        assert "could not be established" in r.detail
        assert "is masked out of every sandbox" not in r.detail

    def test_a_warm_memo_answers_without_probing(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The daemon probes at start-up, so inside that process the answer is
        # free. Saying "could not be established" while `_bwrap_checked` holds
        # it is a statement about the world that is wrong.
        calls = self._bwrap(monkeypatch, available=False, cached=False)
        r = self._run(self._masked_shape(make_config, tmp_path), probe=False)
        assert calls == []
        assert "would be masked" in r.detail
        assert "could not be established" not in r.detail

    # -- both answers, not the first ---------------------------------------

    def test_both_questions_are_reported_rather_than_the_first(
        self, make_config, tmp_path,
    ):
        # The property the stage is named for. An operator who reads one reason
        # and fixes it would otherwise be told nothing about the second and
        # would still have the tree reachable.
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        root = self._root(config)
        root.mkdir(parents=True)
        os.chmod(root, 0o755)
        temp = Path(config.temp_dir)
        (temp / "bob").symlink_to(temp)
        r = self._run(config)
        assert r.status == WARN
        # `"0755"` alone appears in the unconditional `observed` prefix, so it
        # is satisfied with the mode finding deleted — which is the half this
        # test is named for. Assert on wording only the finding produces.
        assert "rather than 0700" in r.detail
        assert "overlaps the control tree" in r.detail

    # -- it never raises ----------------------------------------------------

    def test_never_raises_on_a_relative_temp_dir(self, make_config, tmp_path):
        # Called directly rather than through `run_checks`, which catches every
        # exception and would report a raise as a plain FAIL.
        config = self._config(make_config, tmp_path)
        config.temp_dir = Path("relative/tmp")
        r = doctor.check_task_control_dir(config, True)
        assert r.status in (OK, WARN, FAIL, SKIP)

    def test_never_raises_on_a_config_whose_paths_are_broken(
        self, make_config, tmp_path,
    ):
        config = self._config(make_config, tmp_path)
        config.db_path = Path("")
        config.nextcloud_mount_path = None
        r = doctor.check_task_control_dir(config, True)
        assert r.status in (OK, WARN, FAIL, SKIP)


class TestSandboxMasksUsesTheNativeProfile:
    """The `sandbox.masks` probe execs `/bin/sh`, not the `claude` CLI.

    It moved to `SandboxProfile.NATIVE` with the profile split, so a diagnostic
    no longer builds a namespace holding the subscription credential. The claim
    that came with that move is that its *verdict* is unchanged, and this is
    where that is checked rather than assumed: everything the probe reads — the
    two database masks, and the system binds that make `/bin/sh` resolve — is
    part of the generic plan and identical under both profiles.

    The verdict itself is asserted end to end on the Linux tier
    (`tests/linux/test_sandbox_profiles_real.py`), which is the only place a
    namespace is actually entered.
    """

    @pytest.fixture
    def claude_home(self, tmp_path, monkeypatch):
        """A HOME with a sentinel at every Claude runtime path, so "the probe
        carries none of them" is an assertion about the gate rather than about
        an empty home directory."""
        home = tmp_path / "home"
        for d in (
            ".local/bin", ".local/share/claude", ".local/state/claude",
            ".claude/projects", ".claude/debug", ".claude/todos",
        ):
            (home / d).mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text('{"token": "sentinel"}')
        monkeypatch.setenv("HOME", str(home))
        return home

    def _run_and_capture(self, make_config, monkeypatch, tmp_path):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: True)
        monkeypatch.setattr("istota.executor._bwrap_available", lambda: True)
        seen = {}

        class _Result:
            stdout = ""
            stderr = ""
            returncode = 0

        def _capture(cmd, **kwargs):
            seen["cmd"] = cmd
            return _Result()

        monkeypatch.setattr(doctor.subprocess, "run", _capture)
        # The database in a directory of its own: `make_config` puts it beside
        # the Nextcloud mount, and a mask there is refused (it would shadow a
        # path the sandbox needs), which would make every assertion below about
        # the refusal rather than about the profile.
        config = make_config(db_path=tmp_path / "data" / "istota.db")
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(config.db_path).touch()
        verdict = run_checks(config, only=("sandbox.masks",), deep=True)[0]
        return verdict, seen["cmd"], config

    def test_the_probe_is_built_under_the_native_profile(
        self, make_config, monkeypatch, claude_home, tmp_path,
    ):
        verdict, cmd, _ = self._run_and_capture(make_config, monkeypatch, tmp_path)

        assert verdict.status == OK
        assert cmd[0] == "bwrap"
        for token in cmd:
            assert str(claude_home / ".claude") not in token
            assert str(claude_home / ".local") not in token

    def test_everything_the_probe_reads_is_identical_under_both_profiles(
        self, make_config, monkeypatch, claude_home, tmp_path,
    ):
        """The masks it asserts on, and the `/bin/sh` it runs them with.

        Rebuilds the same argv under CLAUDE and compares the two things the
        verdict depends on. If a future change made a mask or a system bind
        profile-dependent, this is what would say so before the Linux tier did.
        """
        import tempfile

        from istota import db
        from istota.executor import SandboxProfile, build_bwrap_cmd

        _, native_cmd, config = self._run_and_capture(
            make_config, monkeypatch, tmp_path,
        )
        task = db.Task(
            id=0, status="running", source_type="doctor", user_id="doctor", prompt="",
        )
        script = native_cmd[native_cmd.index("--") + 3]
        with tempfile.TemporaryDirectory(prefix="istota-doctor-probe-") as user_temp:
            claude_cmd = build_bwrap_cmd(
                ["/bin/sh", "-c", script], config, task, is_admin=False,
                user_resources=[], user_temp_dir=Path(user_temp),
                profile=SandboxProfile.CLAUDE,
            )

        def _masks(argv):
            """The database masks: every tmpfs/remount-ro after the last bind.

            Not every `--tmpfs` in the argv — `/tmp` is mounted early beside
            `--proc`, and under CLAUDE so is the `~/.claude` base, which is part
            of the block this profile deliberately drops.
            """
            last_bind = max(
                (i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")),
                default=-1,
            )
            return [
                (argv[i], argv[i + 1]) for i in range(last_bind + 1, len(argv) - 1)
                if argv[i] in ("--tmpfs", "--remount-ro")
            ]

        def _system_binds(argv):
            return [
                (argv[i], argv[i + 1], argv[i + 2])
                for i, a in enumerate(argv)
                if a in ("--ro-bind", "--symlink")
                and argv[i + 1].startswith(("/usr", "/bin", "/lib", "/sbin", "/etc"))
            ]

        # The user temp dir differs (a fresh TemporaryDirectory each call), so
        # compare the two families the verdict actually reads.
        assert _masks(native_cmd) == _masks(claude_cmd)
        assert _system_binds(native_cmd) == _system_binds(claude_cmd)
        assert native_cmd[native_cmd.index("--"):][:3] == ["--", "/bin/sh", "-c"]

    def test_the_probe_still_masks_both_database_directories(
        self, make_config, monkeypatch, claude_home, tmp_path,
    ):
        """The control for the two above: a probe built under NATIVE that had
        lost the masks would satisfy "no Claude paths" perfectly."""
        _, cmd, config = self._run_and_capture(make_config, monkeypatch, tmp_path)

        masked = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tmpfs"]
        db_dir = Path(config.db_path).parent.resolve()
        assert str(db_dir) in masked
        # The module root is under it here, and `_mask_dir` skips a candidate an
        # earlier mask already covers — so the assertion is that it is covered,
        # not that it has a mask of its own.
        assert config.module_db_root().resolve().is_relative_to(db_dir)


class TestVerdict:
    """`verdict` — the adapter for a caller that needs a boolean and a sentence.

    `!check`'s non-admin arm and `heartbeat._check_self` are the two consumers.
    Both used to compute their own pass/fail from their own hand-rolled probe,
    which is what this whole spec removes; the shape they need back from the
    registry is one bool and one line.
    """

    def _r(self, name, status):
        return CheckResult(name, status, "detail")

    def test_a_failure_makes_the_verdict_unhealthy(self):
        healthy, summary = doctor.verdict(
            [self._r("a.one", OK), self._r("a.two", FAIL)]
        )
        assert healthy is False
        assert "1 fail" in summary

    def test_a_warning_does_not(self):
        """A warning that pages someone is a failure wearing the wrong label.

        This is a deliberate behaviour change from `heartbeat._check_self`,
        which appended its high-failure-rate finding to `failures` and so
        returned `healthy=False` for it.
        """
        healthy, summary = doctor.verdict(
            [self._r("a.one", OK), self._r("a.two", WARN)]
        )
        assert healthy is True
        assert "1 warn" in summary, (
            f"the warning is not named in the summary: {summary!r}"
        )

    def test_all_skip_is_healthy_and_says_how_many(self):
        """A run that checked nothing must not read as a run that passed
        everything, so the count carries the caveat."""
        healthy, summary = doctor.verdict(
            [self._r("a.one", SKIP), self._r("a.two", SKIP)]
        )
        assert healthy is True
        assert "2 skip" in summary
        assert "0 ok" in summary

    def test_an_empty_list_says_so_in_words(self):
        assert doctor.verdict([]) == (True, "no checks ran")

    def test_every_status_appears_in_the_summary(self):
        _, summary = doctor.verdict(
            [
                self._r("a.one", OK),
                self._r("a.two", WARN),
                self._r("a.three", FAIL),
                self._r("a.four", SKIP),
            ]
        )
        assert summary == "1 ok, 1 warn, 1 fail, 1 skip"

    def test_it_agrees_with_exit_code(self):
        """`verdict`'s bool and `exit_code`'s int answer the same question, and
        a caller reading one and a caller reading the other must not disagree
        about whether the deployment is healthy."""
        for results in (
            [self._r("a.one", OK)],
            [self._r("a.one", WARN)],
            [self._r("a.one", FAIL)],
            [self._r("a.one", SKIP)],
            [],
        ):
            healthy, _ = doctor.verdict(results)
            assert healthy is (exit_code(results) == 0)

    def test_verdict_and_summarize_are_different_functions(self):
        """The collision the name was chosen to avoid.

        `summarize` was already taken and returns counts by status; `verdict`
        returns a bool and a sentence. They look close enough that a later
        tidy-up could merge them, and the two callers of `verdict` would then
        get a dict where they expected a tuple.
        """
        assert doctor.verdict is not doctor.summarize
        results = [self._r("a.one", OK)]
        assert isinstance(doctor.summarize(results), dict)
        assert isinstance(doctor.verdict(results), tuple)


#: The two files that used to carry a hand-rolled copy of the health probe.
_ONCE_HAND_ROLLED = ("heartbeat.py", "commands.py")

#: Strings that only a hand-rolled copy of the probe can contain. The marker is
#: what the copies asked the model to echo; `build_bwrap_cmd` is how they
#: wrapped it. Both now live in `doctor.check_model_execution` and nowhere else.
_PROBE_FINGERPRINTS = (_MODEL_MARKER, "build_bwrap_cmd")


@pytest.mark.parametrize("filename", _ONCE_HAND_ROLLED)
def test_no_hand_rolled_health_probe(filename):
    """Neither caller carries its own health probe any more.

    Cheap, and it is what stops the copies growing back — the same shape as
    `tests/test_lint_scope.py`. Both files ran the same five checks as doctor's
    registry, in the same order, drifting from it and from each other; the whole
    point of pointing them at `run_checks` is that there is one copy.
    """
    source = Path(doctor.__file__).parent / filename
    text = source.read_text()
    for fingerprint in _PROBE_FINGERPRINTS:
        assert fingerprint not in text, (
            f"{filename} contains {fingerprint!r} — a hand-rolled health probe "
            "has grown back. The one copy lives in "
            "`doctor.check_model_execution`; call `doctor.run_checks` instead."
        )


# ---------------------------------------------------------------------------
# talk.signaling_*
# ---------------------------------------------------------------------------


def _signaling_config(make_config, *, talk=True, enabled=True, url="", **fields):
    from istota.config import NextcloudConfig, TalkConfig, TalkSignalingConfig

    config = make_config(nextcloud=NextcloudConfig(url="https://nc.example.com"))
    config.talk = TalkConfig(
        enabled=talk,
        signaling=TalkSignalingConfig(enabled=enabled, url=url, **fields),
    )
    return config


def _settings_payload(mode="external", server="https://hpb.example.com/signaling"):
    from istota.transport.talk import signaling as sig

    return sig.parse_settings(
        {"signalingMode": mode, "server": server, "helloAuthParams": {}},
        nextcloud_url="https://nc.example.com",
    )


def _caps(mode="external", key="LS0tLS1CRUdJTiBQVUJMSUMgS0VZ"):
    config = {"signaling": {"mode": mode}}
    if key is not None:
        config["signaling"]["hello-v2-token-key"] = key
    return {"capabilities": {"spreed": {"config": config}}}


@pytest.fixture
def signaling_seams(monkeypatch):
    """Stand in for the three things the signaling checks reach out to.

    Every one of them is a network call, so a check driven against the real
    thing would be asserting about the developer's own host — and two of the
    three would be asserting about a Nextcloud that is not there.
    """
    doctor.reset_signaling_probe_memo()

    state = {
        "settings": _settings_payload(),
        "settings_error": "",
        "welcome": {"type": "welcome", "welcome": {
            "version": "2.1.1",
            "features": ["hello-v2", "chat-relay", "welcome"],
        }},
        "welcome_error": "",
        "capabilities": _caps(),
        "capabilities_error": "",
        "urls": [],
    }

    def fake_settings(config, timeout):
        if state["settings_error"]:
            return None, state["settings_error"]
        return state["settings"], ""

    def fake_welcome(ws_url, timeout):
        state["urls"].append(ws_url)
        if state["welcome_error"]:
            return {}, state["welcome_error"]
        return state["welcome"], ""

    def fake_caps(config, timeout):
        if state["capabilities_error"]:
            return None, state["capabilities_error"]
        return state["capabilities"], ""

    monkeypatch.setattr(doctor, "_signaling_settings", fake_settings)
    monkeypatch.setattr(doctor, "_signaling_welcome_frame", fake_welcome)
    monkeypatch.setattr(doctor, "_signaling_capabilities", fake_caps)
    yield state
    doctor.reset_signaling_probe_memo()


class TestTheSignalingChecksAreRegistered:
    def test_all_four_are_in_the_registry(self):
        names = {name for name, _ in CHECKS}
        assert {
            "talk.signaling_reachable",
            "talk.signaling_chat_relay",
            "talk.signaling_auth",
            "talk.signaling_watchers",
        } <= names

    def test_they_are_deployment_scoped(self):
        # Every one of them asks about a rendered config, a running signaling
        # server or a live supervisor. A bare `docker run` has none of those,
        # and the image tier asserts no check fails there.
        for name in (
            "talk.signaling_reachable", "talk.signaling_chat_relay",
            "talk.signaling_auth", "talk.signaling_watchers",
        ):
            assert doctor.CHECK_SCOPES[name] == DEPLOYMENT

    def test_none_of_them_is_deep_or_live(self):
        for name in (
            "talk.signaling_reachable", "talk.signaling_chat_relay",
            "talk.signaling_auth", "talk.signaling_watchers",
        ):
            assert name not in DEEP_CHECKS
            assert name not in LIVE_CHECKS


class TestSignalingReachable:
    """The HPB answers a ``welcome``, read before any hello.

    Unauthenticated by construction: nothing here sends a hello, so no
    signaling session exists and no ``participants/active`` POST is made.
    ``doctor`` runs on a scheduler interval and from the admin Health pane, so
    a check that joined a room would put a phantom participant in it every time
    somebody opened a dashboard.
    """

    def test_disabled_signaling_skips(self, make_config, signaling_seams):
        config = _signaling_config(make_config, enabled=False)
        result = doctor.check_signaling_reachable(config, True)
        assert result.status == SKIP

    def test_disabled_talk_skips(self, make_config, signaling_seams):
        config = _signaling_config(make_config, talk=False)
        result = doctor.check_signaling_reachable(config, True)
        assert result.status == SKIP

    def test_a_deployment_with_nothing_configured_skips(self, make_config):
        """The spec's own acceptance line: `--only talk.signaling_reachable`
        against nothing answers `skip`, not `fail`."""
        config = make_config()
        results = run_checks(config, only=("talk.signaling_reachable",))
        assert [r.status for r in results] == [SKIP]

    def test_probe_disabled_opens_no_socket(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_reachable(config, False)
        assert result.status == SKIP
        assert signaling_seams["urls"] == [], "a socket was opened under probe=False"

    def test_a_welcome_frame_is_ok(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_reachable(config, True)
        assert result.status == OK, result.detail
        assert "2.1.1" in result.detail

    def test_the_discovered_server_is_turned_into_a_websocket_url(
        self, make_config, signaling_seams
    ):
        config = _signaling_config(make_config)
        doctor.check_signaling_reachable(config, True)
        assert signaling_seams["urls"] == [
            "wss://hpb.example.com/signaling/spreed"
        ]

    def test_a_configured_url_wins_and_costs_nextcloud_nothing(
        self, make_config, signaling_seams, monkeypatch
    ):
        def refuse(config, timeout):
            raise AssertionError(
                "the settings endpoint was called for a configured URL"
            )

        monkeypatch.setattr(doctor, "_signaling_settings", refuse)
        config = _signaling_config(make_config, url="https://other.example.com/sig/")

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == OK, result.detail
        assert signaling_seams["urls"] == ["wss://other.example.com/sig/spreed"]

    def test_internal_mode_skips_and_points_at_the_check_that_owns_it(
        self, make_config, signaling_seams
    ):
        """One cause, one FAIL.

        A deployment with no backend registered has nothing for a reachability
        check to reach, and `talk.signaling_auth` already FAILs it with the
        remedy that fixes it. A second FAIL here would report a configuration
        fault under a check named for reachability and page an operator twice
        for one cause.
        """
        signaling_seams["settings"] = _settings_payload(mode="internal")
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == SKIP
        assert "internal" in result.detail
        assert "talk.signaling_auth" in result.detail

    def test_only_one_of_the_four_fails_on_an_internal_mode_deployment(
        self, make_config, signaling_seams
    ):
        signaling_seams["settings"] = _settings_payload(mode="internal")
        signaling_seams["capabilities"] = _caps(mode="internal")
        config = _signaling_config(make_config)

        results = run_checks(config, only=("talk.signaling_",))

        failed = [r.name for r in results if r.status == FAIL]
        assert failed == ["talk.signaling_auth"], [
            (r.name, r.status) for r in results
        ]

    def test_an_unreachable_server_fails(self, make_config, signaling_seams):
        signaling_seams["welcome_error"] = "connection refused"
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == FAIL
        assert "connection refused" in result.detail
        assert result.remedy

    def test_a_settings_call_that_failed_is_reported_with_its_cause(
        self, make_config, signaling_seams
    ):
        signaling_seams["settings_error"] = "401 Unauthorized"
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == SKIP
        assert "401" in result.detail

    def test_the_remedy_does_not_claim_the_daemon_will_refuse_to_boot(
        self, make_config, signaling_seams
    ):
        """An unreachable-but-registered server is not one of the two refusals.

        The spec's Behaviour table says watchers retry on a backoff and the
        reconciliation pass carries inbound meanwhile. A remedy sending an
        operator to look for a boot failure that will not happen is a remedy
        they cannot act on.
        """
        signaling_seams["welcome_error"] = "connection refused"
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == FAIL
        assert "refuses to start" not in result.remedy
        assert "room_sync_interval" in result.remedy

    def test_a_missing_library_fails_naming_the_extra(
        self, make_config, signaling_seams, monkeypatch
    ):
        from istota.transport.talk import signaling as sig

        def refuse():
            raise sig.SignalingUnavailable(
                "the websockets library is not installed; install istota[signaling]"
            )

        monkeypatch.setattr(sig, "require_websockets", refuse)
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)

        # A fault, not an unanswerable question: `enabled = true` with no
        # library is one of the two startup refusals, unlike a backend that is
        # merely not registered.
        assert result.status == FAIL
        assert "signaling" in result.detail
        assert "refuses to start" in result.remedy
        assert signaling_seams["urls"] == []

    def test_the_probe_is_shared_with_the_chat_relay_check(
        self, make_config, signaling_seams
    ):
        """One socket per doctor run, not one per check.

        Both checks read the same frame. Probing twice would double the OCS
        settings call and the connect on the hourly sweep and on every admin
        page load, for one answer.
        """
        config = _signaling_config(make_config)
        results = run_checks(config, only=("talk.signaling_",))
        assert len(signaling_seams["urls"]) == 1, signaling_seams["urls"]
        assert {r.status for r in results} <= {OK, SKIP}


class TestSignalingChatRelay:
    def test_present_is_ok(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_chat_relay(config, True)
        assert result.status == OK

    def test_absent_warns_naming_the_consequence(self, make_config, signaling_seams):
        signaling_seams["welcome"] = {
            "type": "welcome",
            "welcome": {"version": "2.0.1", "features": ["hello-v2", "welcome"]},
        }
        config = _signaling_config(make_config)

        result = doctor.check_signaling_chat_relay(config, True)

        assert result.status == WARN
        assert "chat-relay" in result.detail
        assert result.remedy

    def test_an_unreachable_server_skips_rather_than_claiming_absence(
        self, make_config, signaling_seams
    ):
        """A server we could not reach advertises nothing we can read.

        Reporting that as "no chat-relay" would name the wrong fault and send
        an operator to upgrade a server that is simply down.
        """
        signaling_seams["welcome_error"] = "connection refused"
        config = _signaling_config(make_config)

        result = doctor.check_signaling_chat_relay(config, True)

        assert result.status == SKIP
        assert "talk.signaling_reachable" in result.detail

    def test_probe_disabled_skips(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_chat_relay(config, False)
        assert result.status == SKIP
        assert signaling_seams["urls"] == []


class TestSignalingAuth:
    """Talk's own half: external mode, and a hello-v2 token key.

    Reads ``/cloud/capabilities`` and mints nothing. The mode question is
    answered by ``signaling.signaling_mode_reason``, the same predicate the
    startup refusal uses, so the two cannot disagree about whether a deployment
    is configured.
    """

    def test_external_with_a_key_is_ok(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_auth(config, True)
        assert result.status == OK

    def test_internal_mode_fails(self, make_config, signaling_seams):
        signaling_seams["capabilities"] = _caps(mode="internal")
        config = _signaling_config(make_config)

        result = doctor.check_signaling_auth(config, True)

        assert result.status == FAIL
        assert "internal" in result.detail
        assert "talk:signaling" in result.remedy

    def test_no_hello_v2_key_warns_naming_the_non_expiring_ticket(
        self, make_config, signaling_seams
    ):
        signaling_seams["capabilities"] = _caps(key=None)
        config = _signaling_config(make_config)

        result = doctor.check_signaling_auth(config, True)

        assert result.status == WARN
        assert "v1" in result.detail
        assert "expire" in result.detail or "rotate" in result.detail

    def test_the_key_itself_is_never_echoed(self, make_config, signaling_seams):
        """It is a *public* key, so this is hygiene rather than a boundary —
        but a check's detail is one line saying what was observed, and pasting
        a base64 blob into the daemon log and the admin Health pane is not it.
        """
        marker = "PUBLIC-KEY-MATERIAL-abcdef"
        signaling_seams["capabilities"] = _caps(key=marker)
        config = _signaling_config(make_config)

        result = doctor.check_signaling_auth(config, True)

        assert marker not in result.detail
        assert marker not in result.remedy

    def test_unreadable_capabilities_warn_rather_than_pass(
        self, make_config, signaling_seams
    ):
        signaling_seams["capabilities_error"] = "connection timed out"
        config = _signaling_config(make_config)

        result = doctor.check_signaling_auth(config, True)

        assert result.status == WARN
        assert "timed out" in result.detail

    def test_probe_disabled_skips(self, make_config, signaling_seams):
        config = _signaling_config(make_config)
        result = doctor.check_signaling_auth(config, False)
        assert result.status == SKIP

    def test_disabled_signaling_skips(self, make_config, signaling_seams):
        config = _signaling_config(make_config, enabled=False)
        assert doctor.check_signaling_auth(config, True).status == SKIP


class TestSignalingWatchers:
    """The number that separates "the stream is working" from "the safety net
    is carrying it".

    A watcher count alone cannot say that: a sweep-style safety net backfills
    silently, so every room stays current while the event path delivers
    nothing. ``rooms_behind`` is what makes the failure visible.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        from istota.transport.talk import signaling as sig

        sig.clear_stats_source()
        yield
        sig.clear_stats_source()

    def _stats(self, **fields):
        from istota.transport.talk import signaling as sig

        base = {"watchers": 3, "connected": 3, "disconnected": [], "rooms_behind": 0}
        base.update(fields)
        sig.set_stats_source(lambda: base)

    def test_no_supervisor_in_this_process_skips(self, make_config):
        """doctor also runs in the web process, in the CLI and from `!check`.

        Reporting every watcher down there would page an operator about a
        process that was never supposed to have any.
        """
        config = _signaling_config(make_config)
        result = doctor.check_signaling_watchers(config, True)
        assert result.status == SKIP

    def test_all_connected_and_nothing_behind_is_ok(self, make_config):
        self._stats()
        config = _signaling_config(make_config)
        result = doctor.check_signaling_watchers(config, True)
        assert result.status == OK, result.detail
        assert "3" in result.detail

    def test_a_disconnected_watcher_warns_and_is_named(self, make_config):
        self._stats(connected=2, disconnected=["abc123"])
        config = _signaling_config(make_config)

        result = doctor.check_signaling_watchers(config, True)

        assert result.status == WARN
        assert "abc123" in result.detail
        assert result.remedy

    def test_rooms_behind_warns_even_with_every_watcher_connected(self, make_config):
        """The case the check exists for.

        A socket that is up and delivering nothing looks healthy from every
        other angle: the watcher count is right, no reconnect is logged, and
        the reconciliation comparison quietly fetches the rooms the stream
        missed. The count is the only thing that says so.
        """
        self._stats(rooms_behind=4)
        config = _signaling_config(make_config)

        result = doctor.check_signaling_watchers(config, True)

        assert result.status == WARN
        assert "4" in result.detail

    def test_a_supervisor_that_raises_skips_rather_than_failing(self, make_config):
        from istota.transport.talk import signaling as sig

        def boom():
            raise RuntimeError("mid-restart")

        sig.set_stats_source(boom)
        config = _signaling_config(make_config)

        assert doctor.check_signaling_watchers(config, True).status == SKIP

    def test_it_needs_no_probe(self, make_config):
        """It reads in-process counters, so `probe=False` is not a reason to
        skip: the answer costs nothing and is the same either way."""
        self._stats()
        config = _signaling_config(make_config)
        assert doctor.check_signaling_watchers(config, False).status == OK

    def test_disabled_signaling_skips(self, make_config):
        self._stats()
        config = _signaling_config(make_config, enabled=False)
        assert doctor.check_signaling_watchers(config, True).status == SKIP


class TestTheProbeNeverAuthenticates:
    """The spec's load-bearing constraint, asserted against the one function
    that could break it.

    Every other test in this file monkeypatches `_signaling_welcome_frame`
    wholesale, so all of them are assertions about the fixture — the shape
    `.claude/rules/testbed.md` has now recorded eight times. Opening a
    signaling session means POSTing `participants/active` for a room, so a
    check that sent a hello would put a phantom istota participant into a live
    room's member list every time an admin opened the Health pane. Adding a
    `send` to that function must turn something red, and nothing above would.
    """

    class _FakeSocket:
        def __init__(self, frame, log):
            self._frame = frame
            self.log = log

        async def recv(self):
            self.log.append("recv")
            return self._frame

        async def send(self, _payload):
            self.log.append("send")

        async def close(self):
            self.log.append("close")

    class _FakeConnect:
        def __init__(self, socket, log):
            self._socket = socket
            self._log = log

        def __call__(self, url, **kwargs):
            self._log.append(("connect", url, sorted(kwargs)))
            return self

        async def __aenter__(self):
            return self._socket

        async def __aexit__(self, *exc):
            self._log.append("exit")
            return False

    @pytest.fixture
    def fake_websockets(self, monkeypatch):
        import json
        import types

        from istota.transport.talk import signaling as sig

        log = []
        socket = self._FakeSocket(
            json.dumps({
                "type": "welcome",
                "welcome": {"version": "2.1.1", "features": ["chat-relay"]},
            }),
            log,
        )
        module = types.SimpleNamespace(
            connect=self._FakeConnect(socket, log)
        )
        monkeypatch.setattr(sig, "require_websockets", lambda: module)
        return log

    def test_it_reads_one_frame_and_sends_none(self, fake_websockets):
        frame, error = doctor._signaling_welcome_frame(
            "wss://hpb.example.com/spreed", 5.0,
        )

        assert error == ""
        assert frame["type"] == "welcome"
        assert "send" not in fake_websockets, (
            "the reachability probe sent a frame; a hello here creates a "
            "signaling session and, with it, a Talk participant row"
        )
        assert fake_websockets.count("recv") == 1

    def test_the_control_can_fail(self, fake_websockets):
        """Without this the assertion above is a claim rather than a guard.

        Drive the same fake socket through a variant that does send a hello;
        if the log stays empty the instrument is not observing sends and the
        test above proves nothing.
        """
        import asyncio
        import json

        from istota.transport.talk import signaling as sig

        async def _sending_read():
            websockets = sig.require_websockets()
            async with websockets.connect("wss://h/spreed") as socket:
                await socket.send(json.dumps({"type": "hello"}))
                return json.loads(await asyncio.wait_for(socket.recv(), 1))

        doctor._run_off_loop(_sending_read, 5.0)

        assert "send" in fake_websockets, (
            "the instrument does not observe sends, so the assertion above "
            "proves nothing"
        )

    def test_it_bounds_itself_well_inside_the_callers_budget(self, monkeypatch):
        """The handshake and the first frame share the budget, not each take it.

        Giving each leg the caller's whole number let the pair run to twice it
        before `_run_off_loop`'s outer bound noticed — which returned a timeout
        naming the wrong number and left the thread and its socket live.
        """
        seen = {}

        import types

        from istota.transport.talk import signaling as sig

        class _Connect:
            def __call__(self, url, **kwargs):
                seen.update(kwargs)
                raise OSError("refused")

        monkeypatch.setattr(
            sig, "require_websockets",
            lambda: types.SimpleNamespace(connect=_Connect()),
        )

        doctor._signaling_welcome_frame("wss://h/spreed", 10.0)

        assert seen["open_timeout"] <= 10.0 / 2.0


class TestTheWatcherCensusDoesNotContradictItself:
    """A supervisor may report counters and no token list.

    Reading only the token list returns OK with a detail saying "1 of 5
    watchers connected", which is a check contradicting itself in the one
    place it is meant to be authoritative — and a watcher mid-reconnect is
    plausibly counted as not connected while belonging on no list.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        from istota.transport.talk import signaling as sig

        sig.clear_stats_source()
        yield
        sig.clear_stats_source()

    def _stats(self, **fields):
        from istota.transport.talk import signaling as sig

        base = {"watchers": 5, "connected": 5, "disconnected": [], "rooms_behind": 0}
        base.update(fields)
        sig.set_stats_source(lambda: dict(base))

    def test_a_counts_only_shortfall_warns(self, make_config):
        self._stats(connected=1, disconnected=[])
        config = _signaling_config(make_config)

        result = doctor.check_signaling_watchers(config, True)

        assert result.status == WARN, result.detail
        assert "1 of 5" in result.detail

    def test_a_string_disconnected_field_does_not_become_six_rooms(
        self, make_config
    ):
        """`or []` on a bare string iterates it character by character.

        Each character passes an `isinstance(token, str) and token` filter, so
        the WARN would name six rooms that do not exist.
        """
        self._stats(connected=4, disconnected="abc123")
        config = _signaling_config(make_config)

        result = doctor.check_signaling_watchers(config, True)

        assert "a, b, c" not in result.detail
        assert "4 of 5" in result.detail

    def test_a_missing_key_reads_as_zero_rather_than_raising(self, make_config):
        from istota.transport.talk import signaling as sig

        sig.set_stats_source(lambda: {})
        config = _signaling_config(make_config)

        result = doctor.check_signaling_watchers(config, True)

        assert result.status == OK
        assert "0 of 0" in result.detail

    def test_the_reader_is_handed_a_copy_not_the_supervisors_own_mapping(self):
        """The supervisor is on the loop thread and the reader is not."""
        from istota.transport.talk import signaling as sig

        live = {"watchers": 1, "connected": 1}
        sig.set_stats_source(lambda: live)

        read = sig.read_stats()
        read["watchers"] = 99

        assert live["watchers"] == 1

    def test_registering_a_mapping_instead_of_a_callable_is_reported(self, caplog):
        """The obvious mistake, and it is silent without this.

        A non-callable used to clear the source, after which
        `talk.signaling_watchers` reports "no supervisor in this process" for
        the life of the daemon with nothing anywhere saying why.
        """
        import logging

        from istota.transport.talk import signaling as sig

        with caplog.at_level(logging.WARNING, logger=sig.logger.name):
            sig.set_stats_source({"watchers": 1})

        assert sig.read_stats() is None
        assert any("callable" in r.getMessage() for r in caplog.records)


class TestSignalingAuthOnATalkThatPublishesNoMode:
    """An absent capability key must not fail a working deployment.

    `capabilities.spreed.config.signaling.mode` was read off the deployment
    this design was verified against, which is why the check asks it here
    rather than paying for the settings call and its token. A different Talk
    version may not publish it, and letting that fall through
    `signaling_mode_reason`'s unreadable-mode arm would FAIL a deployment whose
    high-performance backend is fine.
    """

    def test_an_absent_mode_key_warns_rather_than_failing(
        self, make_config, signaling_seams
    ):
        signaling_seams["capabilities"] = {
            "capabilities": {"spreed": {"config": {"signaling": {
                "hello-v2-token-key": "LS0tLS1CRUdJTg==",
            }}}}
        }
        config = _signaling_config(make_config)

        result = doctor.check_signaling_auth(config, True)

        assert result.status == WARN, result.detail
        assert "mode" in result.detail
        assert "talk.signaling_reachable" in result.remedy

    def test_a_capabilities_payload_with_no_spreed_block_also_warns(
        self, make_config, signaling_seams
    ):
        signaling_seams["capabilities"] = {"capabilities": {}}
        config = _signaling_config(make_config)

        assert doctor.check_signaling_auth(config, True).status == WARN
class TestConfigVisibility:
    """The gate in front of the whole registry (ISSUE-412).

    `load_config` returns a bare `Config()` when no candidate resolves, so
    every path-and-policy check then answers about defaults — a relative
    `data/istota.db`, the default temp dir, a whole `[security]` block the
    operator never wrote — while reading exactly like a run about the real
    deployment. Inside a task that is *unconditional*: `build_clean_env` exports
    `ISTOTA_CONFIG_PATH` naming a file under `config/`, and `config/` is bound
    into no sandbox by design.

    The verdict splits on the same principle `_deployment_sandboxing` did and
    not on the same predicate: a boundary doing its job is not a fault, so a
    *task* gets a `SKIP`, and the question "is this the daemon" is answered by
    `ISTOTA_TASK_ID` rather than by `ISTOTA_SANDBOXED`, which `task_env` sets
    only under `skill_proxy_enabled and effective_sandboxing`.
    """

    def _loaded(self, make_config, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("")
        return make_config(config_path=path)

    def test_a_loaded_config_opens_the_gate(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        assert doctor.config_visibility(self._loaded(make_config, tmp_path)) is None

    def test_inside_a_task_it_skips_rather_than_failing(
        self, make_config, tmp_path, monkeypatch
    ):
        """`config/` is bound into no sandbox on purpose. The boundary working
        is not a fault — reporting it as one is the ISSUE-381 shape again."""
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        r = doctor.config_visibility(make_config(config_path=None))
        assert r is not None
        assert r.status == SKIP

    def test_a_task_without_the_sandbox_marker_still_skips(
        self, make_config, tmp_path, monkeypatch
    ):
        """The defect this predicate exists for. `task_env` sets
        `ISTOTA_SANDBOXED` only under `skill_proxy_enabled and
        effective_sandboxing`, while `executor` exports `ISTOTA_CONFIG_PATH` on
        `config_path is not None` alone. So a `sandbox_enabled` deployment with
        the proxy off — warned about since ISSUE-393, and shipped — puts a task
        in a namespace with no marker and an unreachable config directory, and
        keying on the marker would FAIL it with a remedy it cannot follow."""
        monkeypatch.delenv("ISTOTA_SANDBOXED", raising=False)
        monkeypatch.setenv("ISTOTA_TASK_ID", "41")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == SKIP

    def test_a_cron_command_job_is_not_a_task_arm(
        self, make_config, tmp_path, monkeypatch
    ):
        """`PRECOMMIT_SCANS_REQUIRED` is the third marker in
        `_non_daemon_env_markers` and is deliberately not in this predicate: a
        cron `command` job runs unsandboxed as the daemon user with the config
        directory in front of it, so "run it on the host as the daemon user" is
        telling it to do what it is already doing."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("PRECOMMIT_SCANS_REQUIRED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == FAIL

    def test_the_task_detail_says_the_checks_would_be_about_defaults(
        self, make_config, tmp_path, monkeypatch
    ):
        """A SKIP's remedy is not rendered by `render_text`, so the one line an
        operator or a model actually sees has to carry the whole point."""
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
        r = doctor.config_visibility(make_config(config_path=None))
        assert "default" in r.detail.lower()

    def test_an_exported_path_that_does_not_resolve_fails_outside_a_task(
        self, make_config, tmp_path, monkeypatch
    ):
        """The daemon named the file and the subprocess could not read it.
        A broken install, wrong permissions, or a stale exported value."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == FAIL
        assert "ISTOTA_CONFIG_PATH" in r.detail
        assert str(tmp_path / "absent.toml") in r.detail
        assert r.remedy

    def test_the_two_arms_are_reported_differently(
        self, make_config, tmp_path, monkeypatch
    ):
        """The negative control for the whole change: without it both runs
        report a clean bill of health about defaults."""
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        inside = doctor.config_visibility(make_config(config_path=None))
        monkeypatch.delenv("ISTOTA_SANDBOXED")
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        outside = doctor.config_visibility(make_config(config_path=None))
        assert (inside.status, outside.status) == (SKIP, FAIL)

    def test_an_explicit_c_that_does_not_resolve_fails_and_names_itself(
        self, make_config, tmp_path, monkeypatch
    ):
        """`-c` is consulted instead of the environment, so the finding has to
        name the argument rather than a variable the operator never set."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID", "ISTOTA_CONFIG_PATH"):
            monkeypatch.delenv(name, raising=False)
        asked = tmp_path / "typo.toml"
        r = doctor.config_visibility(make_config(config_path=None), requested=asked)
        assert r.status == FAIL
        assert str(asked) in r.detail
        assert "-c" in r.detail

    def test_a_whitespace_path_is_not_reported_as_nothing_named(
        self, make_config, monkeypatch
    ):
        """The arm is chosen from `source`, never from the rendered `named`: a
        whitespace path collapses to empty, and reading that as "nothing was
        named" gives the wrong cause and the wrong remedy."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "   ")
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == FAIL
        assert "ISTOTA_CONFIG_PATH" in r.detail
        assert "standard locations" not in r.detail

    def test_nothing_named_and_nothing_found_still_fails(
        self, make_config, monkeypatch
    ):
        """A fresh checkout, or a run from the wrong directory. Nobody pointed
        at anything, so the reason and the remedy differ — but the exit code
        has to stay 1, because that invocation used to run 31 checks against
        defaults and exit 1 on the several that fail. `verdict` draws the same
        line for an empty list: a run that checked nothing must not read as a
        run that passed everything."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID", "ISTOTA_CONFIG_PATH"):
            monkeypatch.delenv(name, raising=False)
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == FAIL
        assert exit_code([r]) == 1
        assert r.remedy

    def test_only_the_task_arm_exits_zero(self, make_config, monkeypatch):
        """A non-zero code every time a task runs doctor is noise about a
        boundary behaving correctly."""
        monkeypatch.setenv("ISTOTA_TASK_ID", "7")
        monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
        assert exit_code([doctor.config_visibility(make_config(config_path=None))]) == 0

    def test_image_scope_is_exempt(self, make_config, monkeypatch):
        """`IMAGE` is defined as what a bare `docker run` with no volumes can
        answer, which is exactly a host with no config on any search path.
        Swallowing that scope would make a loaded config a precondition for the
        one scope declared not to need one."""
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/nonexistent/config.toml")
        assert doctor.config_visibility(make_config(config_path=None), scope=IMAGE) is None

    def test_deployment_scope_is_not_exempt(self, make_config, monkeypatch):
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/nonexistent/config.toml")
        r = doctor.config_visibility(make_config(config_path=None), scope=DEPLOYMENT)
        assert r is not None and r.status == FAIL

    def test_the_named_path_cannot_forge_a_line_of_output(
        self, make_config, monkeypatch
    ):
        """The value is quoted straight into a `detail` that lands on a terminal
        line, and both sources are writable by anyone who can set an environment
        on the machine."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(
            "ISTOTA_CONFIG_PATH", "/a.toml\n  OK   security.sandbox_effective  fine"
        )
        r = doctor.config_visibility(make_config(config_path=None))
        assert r.status == FAIL
        assert "\n" not in r.detail
        assert len(r.detail.splitlines()) == 1

    def test_a_very_long_named_path_is_capped_and_says_so(
        self, make_config, monkeypatch
    ):
        """An operator told that a path did not resolve must not be shown a
        different path from the one that was tried."""
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/" + "x" * 5000 + ".toml")
        r = doctor.config_visibility(make_config(config_path=None))
        assert len(r.detail) < 500
        assert "…" in r.detail

    def test_the_gate_is_deliberately_outside_the_registry(self):
        """It answers whether the run is about this host at all, so it has no
        `only=` prefix to be selected by and nothing to filter. Stated as an
        assertion rather than left as an absence, because the registry
        invariants above would otherwise silently not cover it."""
        assert doctor.CONFIG_GATE not in {name for name, _ in CHECKS}
        assert doctor.CONFIG_GATE not in doctor.CHECK_SCOPES

    def test_every_arm_satisfies_the_registry_invariants(
        self, make_config, monkeypatch
    ):
        """It renders through the same two renderers and is read by the same
        consumers, so `scope` is set explicitly rather than left to a default."""
        arms = (
            {"ISTOTA_TASK_ID": "1", "ISTOTA_CONFIG_PATH": "/nonexistent/c.toml"},
            {"ISTOTA_CONFIG_PATH": "/nonexistent/c.toml"},
            {},
        )
        seen = []
        for env in arms:
            with pytest.MonkeyPatch.context() as mp:
                for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID", "ISTOTA_CONFIG_PATH"):
                    mp.delenv(name, raising=False)
                for name, value in env.items():
                    mp.setenv(name, value)
                r = doctor.config_visibility(make_config(config_path=None))
            seen.append(r.status)
            assert r.detail.strip()
            assert r.status in (OK, WARN, FAIL, SKIP)
            assert r.scope == DEPLOYMENT
            if r.status in (WARN, FAIL):
                assert r.remedy.strip()
        assert seen == [SKIP, FAIL, FAIL]
