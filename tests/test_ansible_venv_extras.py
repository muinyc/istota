"""ISSUE-446: the deployment installs every extra, and nothing selects a subset.

`istota_install_all_extras` switched between `uv sync --extra all` and a list
assembled from the runtime feature flags. The `false` branch was never a
working deployment: `all` is twelve extras and the branch could name at most
four, so email, calendar, feeds, money, markets, garmin, signaling and
transcribe went missing — the dependencies behind shipped subsystems, not
runtime options. The two call sites also disagreed with each other. The play's
branch named `--extra web` and the auto-update cron's did not, and `uv sync`
prunes the venv to what the extras name, so a `false` deployment installed the
web extra and had it removed again on the next two-minute tick, for ever, with
nothing reporting it.

The flag is gone rather than fixed. Whether a feature's wheels are in the venv
is not the question the runtime feature flags answer, and with the switch
retired that separation is the only one left.

These render the two invocations with a **stale** `istota_install_all_extras =
false` still set, because that is what an operator carrying the line in
`settings.toml` produces. The value has to reach the render for the test to
discriminate: against the pre-fix role it selects the broken branch at both
sites and the two disagree, which is the failure being removed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
UPDATE_TEMPLATE = ANSIBLE / "templates" / "istota-update.sh.j2"
SETTINGS_TO_VARS = REPO / "deploy" / "settings_to_vars.py"

RETIRED_FLAG = "install_all_extras"

# Read from the manifest rather than restated, so an extra added there and left
# out of `all` fails here instead of quietly falling out of every deployment.
PYPROJECT = REPO / "pyproject.toml"

# An operator who never edited `settings.toml` and one who set the retired key
# to `false` must now get the same venv, so every case renders with the flag
# false. `web_enabled` is on and the other three off — an ordinary deployment,
# and the pairing that exhibits the disagreement: under the pre-fix role the
# play rendered `--extra web` here and the cron rendered no extra at all.
STALE_VARS = {
    "istota_install_all_extras": False,
    "istota_memory_search_enabled": False,
    "istota_whisper_enabled": False,
    "istota_location_enabled": False,
    "istota_web_enabled": True,
    "istota_namespace": "istota",
    "istota_update_lock_wait": 900,
}


def find_task(name: str) -> dict:
    for task in yaml.safe_load(TASKS_FILE.read_text()):
        if isinstance(task, dict) and task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def render(source: str, **overrides) -> str:
    # `ChainableUndefined` rather than the default: these render with partial
    # vars, and the update template already carries
    # `{% if istota_devbox_enabled and ... (istota_devbox_users | length > 0) %}`,
    # which survives only because the first conjunct short-circuits before the
    # filter. Reordering it would turn every assertion here into a collection
    # error instead of a failure, so the leniency is asked for rather than
    # inherited.
    env = Environment(undefined=ChainableUndefined)
    return env.from_string(source).render(**{**STALE_VARS, **overrides})


def extras_in(text: str) -> set[str]:
    return set(re.findall(r"--extra\s+([\w-]+)", text))


def play_sync(**overrides) -> str:
    return render(find_task("Install Python dependencies with uv")["command"], **overrides)


def cron_sync(**overrides) -> str:
    """The rendered update script's `uv` invocation, and any line feeding it.

    Scoped rather than taken from the whole script, so an unrelated `--extra`
    mention elsewhere in the template cannot be read as part of this install.
    The `UV_ARGS=` lines are still collected because the pre-fix template put
    the extras on those lines rather than on the `uv` line, and a helper that
    read only the `uv` line would misreport what that branch installed — the
    assertions below still fail against it either way, but for the wrong
    reason, which is the thing that rots.
    """
    rendered = render(UPDATE_TEMPLATE.read_text(), **overrides)
    return "\n".join(
        line
        for line in rendered.splitlines()
        if line.startswith("UV_ARGS=") or re.search(r"\buv\s", line)
    )


class TestBothInvocationsInstallEveryExtra:
    def test_the_play_installs_all(self):
        assert extras_in(play_sync()) == {"all"}

    def test_the_auto_update_cron_installs_all(self):
        assert extras_in(cron_sync()) == {"all"}

    def test_the_two_agree(self):
        """The cron must not prune what the play just installed.

        `uv sync` prunes the venv to the extras it is given, so the play and
        the cron naming different sets is not a cosmetic drift — the losing
        extra is uninstalled on the next tick and reinstalled by the next play.
        """
        assert extras_in(play_sync()) == extras_in(cron_sync())

    def test_the_feature_flags_do_not_reach_the_extras(self):
        """Turning a feature on or off must not change what is installed.

        The runtime flags decide whether a subsystem runs, which is a different
        question from whether its wheels are on disk. That separation is the
        whole of what survives the retirement, so it is asserted rather than
        assumed.
        """
        on = dict.fromkeys(
            (
                "istota_memory_search_enabled",
                "istota_whisper_enabled",
                "istota_location_enabled",
                "istota_web_enabled",
            ),
            True,
        )
        off = dict.fromkeys(on, False)
        assert extras_in(play_sync(**on)) == extras_in(play_sync(**off)) == {"all"}
        assert extras_in(cron_sync(**on)) == extras_in(cron_sync(**off)) == {"all"}


class TestTheFlagIsRetired:
    def test_it_is_named_nowhere_under_deploy(self):
        """Including `settings_to_vars.py`, the only route an operator had.

        An unknown key in `settings.toml` is discarded, so a stale line costs
        the operator nothing once the mapping is gone — but a mapping left
        behind would keep feeding a variable no template reads, which is worse
        than either state.
        """
        offenders = []
        scanned = 0
        for path in sorted((REPO / "deploy").rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            if RETIRED_FLAG in text:
                offenders.append(str(path.relative_to(REPO)))
        # A walk that reached nothing reads exactly like a clean one. `rglob`
        # does not descend a symlinked directory before 3.13, and
        # `.claude/rules/deployment.md` describes an operator checkout where
        # `deploy/ansible` is a symlink — so the floor is what stops this
        # guard passing on a tree it never opened.
        assert scanned >= 20, f"walk reached only {scanned} files under deploy/"
        assert offenders == [], f"retired flag still named in: {offenders}"

    def test_the_default_is_gone(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        assert "istota_install_all_extras" not in defaults

    def test_settings_to_vars_does_not_map_it(self):
        assert RETIRED_FLAG not in SETTINGS_TO_VARS.read_text()


class TestAllCoversEveryExtra:
    def test_all_names_every_optional_dependency_group(self):
        """`--extra all` is the whole install, so `all` has to be whole.

        Retiring the switch makes `all` the only shape a deployment can take.
        An extra declared in `pyproject.toml` and left out of `all` is then
        uninstallable on a deployment by any route at all, which is the failure
        mode the per-feature branch had and the reason it is being removed.
        """
        # Parsed rather than pattern-matched. A regex over the table fails
        # *open*: an extra written with different spacing simply does not
        # appear in `declared`, so one genuinely missing from `all` passes in
        # silence — which is the failure this test exists to catch.
        extras = tomllib.loads(PYPROJECT.read_text())["project"][
            "optional-dependencies"
        ]
        declared = set(extras)
        assert len(declared) >= 12, f"suspiciously few extras parsed: {declared}"

        covered = set()
        for entry in extras["all"]:
            covered.update(re.findall(r"istota\[([\w-]+)\]", entry))

        # Five groups are deliberately outside `all`: `all` itself, the `dev`
        # and `test` aggregates, `docs` (builds the site rather than running
        # the daemon) and `local` (the other deployment shape). Note this says
        # nothing about what a deployment installs — the `dev` *extra* here is
        # a different thing from the `[dependency-groups] dev` table, which uv
        # installs by default and which neither `uv sync` in the role declines.
        assert declared - covered - {"all", "dev", "test", "docs", "local"} == set()
