"""``config.toml.j2`` renders to something ``load_config`` accepts.

The cheap piece of bare-metal coverage, and the one that addresses where
``30bb7c83``'s bug actually lived. Production is the Ansible shape, not the
Docker one: the role installs the forge CLIs to ``/usr/bin`` and renders those
paths into ``config.toml``, and nothing in the suite had ever rendered that
template. A key the code renamed, or a path the role stopped creating, showed up
first on a host.

Three properties, and the third is the one with teeth:

  * the rendered file parses as TOML and ``load_config`` accepts it;
  * every key the template emits exists on the corresponding dataclass — the
    loader ignores unknown keys, so a typo reaches every host and does nothing
    at all, silently;
  * ``developer.gh_bin_path`` names a path the role's own install tasks create.

**What this cannot see.** Ansible is not in the dependency set, so the template
is rendered with plain jinja2 plus shims for the two Ansible-provided filters it
uses. ``to_json`` and ``ternary`` are shimmed to their documented behaviour, and
for the values involved here — lists of strings, booleans — that is the same
output. Variable references *inside* ``defaults/main.yml`` are resolved to a
fixed point below, because Ansible resolves them recursively and jinja2 does
not; without that, ``db_path`` renders with a literal ``{{ istota_namespace }}``
in it. What none of this reproduces is inventory, host facts, or the vault, so
this asserts the template against its own defaults and nothing more.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from istota import config as config_module
from istota.config import Config, devbox_container_backend, load_config

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TEMPLATE = ANSIBLE / "templates" / "config.toml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"


def _custom_filters() -> dict:
    """The role's own filter plugin, loaded the way test_ansible_briefing_blocks_toml does."""
    spec = importlib.util.spec_from_file_location(
        "istota_toml", ANSIBLE / "filter_plugins" / "istota_toml.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FilterModule().filters()


def _ternary(value, true_val, false_val, none_val=None):
    if value is None and none_val is not None:
        return none_val
    return true_val if value else false_val


def _environment() -> Environment:
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters.update(_custom_filters())
    # Ansible-provided, not jinja2-provided.
    env.filters["to_json"] = lambda v, **kw: json.dumps(v, **kw)
    env.filters["ternary"] = _ternary
    return env


def _resolve(variables: dict, env: Environment) -> dict:
    """Expand `{{ other_var }}` inside the defaults, the way Ansible would.

    Iterates to a fixed point rather than once: `istota_repo_dir` is
    `{{ istota_home }}/istota` and `istota_home` is itself
    `/srv/app/{{ istota_namespace }}`, so a single pass leaves a template in the
    output. Bounded, because a genuine cycle in the defaults should fail here
    loudly rather than hang the suite.
    """

    def expand(value):
        if isinstance(value, str) and "{{" in value:
            return env.from_string(value).render(**variables)
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    for _ in range(10):
        expanded = {k: expand(v) for k, v in variables.items()}
        if expanded == variables:
            return variables
        variables = expanded
    raise AssertionError("defaults/main.yml did not reach a fixed point in 10 passes")


# The one host fact the defaults read: `istota_browser_cpu_limit` is
# `{{ ansible_facts['processor_vcpus'] }}`. Supplied rather than stubbed away,
# because a *second* fact appearing in the defaults should fail this file
# loudly — StrictUndefined does that — rather than render as an empty string.
FACTS = {"processor_vcpus": 4}


def render(**overrides) -> str:
    env = _environment()
    variables = _resolve(
        {
            **yaml.safe_load(DEFAULTS_FILE.read_text()),
            "ansible_facts": FACTS,
            **overrides,
        },
        env,
    )
    return env.from_string(TEMPLATE.read_text()).render(**variables)


def _write_temp(text: str) -> str:
    """A rendered config on disk, for the one test that needs `load_config`."""
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8",
    )
    with handle:
        handle.write(text)
    return handle.name


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


@pytest.fixture(scope="module")
def parsed(rendered: str) -> dict:
    return tomllib.loads(rendered)


class TestItRendersSomethingTheLoaderAccepts:
    def test_no_jinja_survives_into_the_output(self, rendered):
        # A `{{ istota_namespace }}` left in a path is the failure mode this
        # file's own harness had before the fixed-point pass, and it is also
        # what an Ansible variable named in the template but missing from the
        # defaults would look like on a host.
        assert "{{" not in rendered
        assert "{%" not in rendered

    def test_the_output_is_valid_toml(self, rendered):
        tomllib.loads(rendered)

    def test_load_config_parses_it(self, tmp_path, rendered):
        path = tmp_path / "config.toml"
        path.write_text(rendered)

        config = load_config(path)

        assert isinstance(config, Config)
        assert config.bot_name

    def test_the_per_skill_proxy_timeouts_survive_the_round_trip(self, tmp_path):
        """A sub-table ends the scalar section it follows, so one emitted above
        `[security]`'s remaining keys silently reparents them: the loader then
        reports the orphans as unrecognised and runs the defaults, which is a
        deploy setting quietly doing nothing. That already happened once in
        `config/config.example.toml` while writing ISSUE-448.

        `test_load_config_parses_it` cannot see it — a reparented file is still
        valid TOML and still loads — so this reads the values back. The
        discriminating half is `sandbox_cache_*`: those are the last scalars the
        template emits before the new table, they are conditional on
        `sandbox_cache_dir`, and the default render leaves them out entirely. So
        the override is what puts a scalar either side of the boundary and makes
        an ordering mistake observable at all.

        The map itself is overridden because the role ships it empty — the
        `code_review` ceiling lives in `skill_proxy.DEFAULT_SKILL_TIMEOUTS`,
        where an operator's group_vars cannot replace it — so a default render
        emits no table and would exercise none of this.
        """
        path = tmp_path / "config.toml"
        path.write_text(render(
            istota_security_skill_proxy_timeouts={"browse": 90, "code_review": 480},
            istota_security_sandbox_cache_dir="/srv/cache",
            istota_security_sandbox_ro_paths=["/srv/ro"],
        ))

        security = load_config(path).security

        assert security.skill_proxy_timeouts == {"browse": 90, "code_review": 480}
        assert security.skill_proxy_timeout == 300
        assert security.sandbox_cache_dir == "/srv/cache"
        assert security.sandbox_ro_paths == ["/srv/ro"]
        assert security.network.enabled is True

    def test_the_default_render_emits_no_per_skill_table(self, parsed):
        """The role ships the map empty on purpose, so the block is conditional
        and a default deploy writes no `[security.skill_proxy_timeouts]` at all.
        Paired with the round-trip test above: that one only means something
        while this one says the override is what turns the block on."""
        assert "skill_proxy_timeouts" not in parsed["security"]


class TestEveryRenderedKeyIsARealField:
    """The loader ignores unknown keys, so a rename is silent on a host.

    Walked section by section against the dataclass tree rather than flattened:
    a key that is valid under `[scheduler]` and meaningless under `[web]` should
    fail, and a flat name set cannot tell those apart.
    """

    @pytest.mark.parametrize(
        "section", ["scheduler", "security", "web", "logging", "nextcloud", "brain"]
    )
    def test_the_walk_descends_into_the_sections_that_matter(self, parsed, section):
        """Guard on the guard, asserting the descent rather than the presence.

        The first version of this only checked ``section in parsed``, which is
        not the same claim. ``_nested_dataclass`` resolves a field's annotation
        to a dataclass by name; if that resolution broke — a field annotated
        ``SchedulerConfig | None``, a qualified name, ``config.py`` moving off
        ``from __future__ import annotations`` — the walk would silently check
        top-level keys only, and a presence check would stay green over a
        coverage claim that had collapsed to nothing.

        So: inject a key that cannot be a real field, and require the walk to
        report it at the right depth.
        """
        assert section in parsed, f"[{section}] is not in the rendered template"

        poisoned = {**parsed, section: {**parsed[section], "zzz_not_a_field": 1}}

        assert f"{section}.zzz_not_a_field" in _unknown_keys(poisoned, Config, prefix="")

    def test_no_section_names_a_field_the_dataclass_does_not_have(self, parsed):
        unknown = sorted(_unknown_keys(parsed, Config, prefix=""))

        assert not unknown, (
            "config.toml.j2 renders keys no dataclass has. The loader drops "
            f"these silently on every host: {unknown}"
        )


def _unknown_keys(section: dict, target, prefix: str) -> list[str]:
    """Every dotted key in `section` with no matching field on `target`.

    Descends only where the dataclass field is itself a dataclass. A dict-valued
    field like `users` is user data, not config schema — its keys are user ids
    and cannot be checked against a field list.
    """
    if not is_dataclass(target):
        return []

    by_name = {f.name: f for f in fields(target)}
    problems: list[str] = []

    for key, value in section.items():
        dotted = f"{prefix}{key}"
        field = by_name.get(key)
        if field is None:
            problems.append(dotted)
            continue
        if isinstance(value, dict):
            nested = _nested_dataclass(field.type)
            if nested is not None:
                problems.extend(_unknown_keys(value, nested, prefix=f"{dotted}."))

    return problems


def _nested_dataclass(annotation):
    """The dataclass a field's annotation names, or None.

    Handles both forms, because `config.py` today gives type objects and a
    future `from __future__ import annotations` there would give strings —
    either way the name is what gets resolved against the config module.

    Returns None for anything that is not a plain dataclass annotation, which
    includes unions like `SchedulerConfig | None`. That is a silent loss of
    coverage rather than an error, so
    `TestEveryRenderedKeyIsARealField::test_the_walk_descends_into_the_sections_that_matter`
    asserts the descent for each section that matters instead of trusting this.
    """
    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    candidate = getattr(config_module, name, None)
    return candidate if is_dataclass(candidate) else None


class TestTheForgePathsMatchWhatTheRoleInstalls:
    """Where 30bb7c83 lived.

    The rendered config has to name the path the role actually installs to. A
    change to either side alone leaves a config naming a binary that is not
    there, and `os.execve` exits 6 mid-task with `ENOENT`.

    Rewritten when the role stopped using apt: it now extracts both binaries
    from the vendors' release .debs into `/usr/local/bin`, so the old `/usr/bin`
    inference no longer holds. The previous premise test asserted `"apt:" in
    tasks` over the *whole* task file, which several other sections also
    satisfy — so it would have gone on passing against exactly this change.
    That is why the premise below names the install task and its method rather
    than a substring the file is never without.
    """

    def test_the_role_installs_the_forge_clis_from_the_vendor_releases(self):
        # The premise of the assertion below. `tests/test_ansible_forge_cli_
        # install.py` is where the install itself is held to its contract; this
        # only establishes that the inference about *where* it lands is sound.
        tasks = TASKS_FILE.read_text()

        assert "Install the forge CLIs from the vendors' releases" in tasks
        assert "dpkg-deb -x" in tasks

    @pytest.mark.parametrize(
        "key,binary",
        [("gh_bin_path", "gh"), ("glab_bin_path", "glab")],
    )
    def test_the_rendered_path_is_where_the_role_puts_the_binary(self, key, binary):
        config = load_config_from(render(istota_developer_enabled=True))

        assert getattr(config.developer, key) == f"/usr/local/bin/{binary}"

    def test_the_developer_container_block_names_only_real_fields(self):
        """`[developer]` renders only when the skill is on, so the default
        walk in `TestEveryRenderedKeyIsARealField` never sees it — and the
        loader ignores an unknown key, so a rename there is silent on every
        host. Walk the enabled rendering too."""
        rendered = render(istota_developer_enabled=True)
        parsed = tomllib.loads(rendered)

        assert "container" in parsed["developer"], (
            "config.toml.j2 no longer emits [developer.container], so the exec "
            "transport has no socket directory or timeouts to configure"
        )
        assert not sorted(_unknown_keys(parsed, Config, prefix=""))

    def test_the_container_walk_would_notice_a_bad_key(self):
        """The guard on the guard, same shape as the section walk above: prove
        the descent reaches `[developer.container]` rather than stopping at
        `[developer]` and reporting nothing."""
        parsed = tomllib.loads(render(istota_developer_enabled=True))
        poisoned = {
            **parsed,
            "developer": {
                **parsed["developer"],
                "container": {**parsed["developer"]["container"], "zzz_not_a_field": 1},
            },
        }

        assert "developer.container.zzz_not_a_field" in _unknown_keys(
            poisoned, Config, prefix=""
        )

    def test_the_rendered_container_block_loads_with_the_values_it_names(self):
        config = load_config_from(render(istota_developer_enabled=True))

        # A list, not a string. `shim_commands = "npm"` would iterate as
        # characters and install a shim called `n`; the loader refuses that, and
        # this is the assertion that the template does not produce it.
        assert isinstance(config.developer.container.shim_commands, list)
        assert "npm" in config.developer.container.shim_commands
        # The two deliberate absences, which are the routing decision rather
        # than a preference. See `config.DEFAULT_SHIM_COMMANDS`.
        assert "python3" not in config.developer.container.shim_commands
        assert "make" not in config.developer.container.shim_commands

    def test_the_default_rendering_leaves_the_backend_off(self):
        """A host that has not opted in must render the behaviour it already
        had. The developer skill alone does not route builds anywhere: the
        devbox has to be on too, and the role default for that is false."""
        config = load_config_from(render(istota_developer_enabled=True))

        assert devbox_container_backend(config) is False

    def test_the_template_no_longer_renders_the_retired_key(self):
        """It would load as an unknown key and earn a WARN from `doctor` on
        every host the role touches."""
        parsed = tomllib.loads(render(istota_developer_enabled=True))

        assert "backend" not in parsed["developer"]["container"]

    def test_a_devbox_host_derives_the_devbox_backend(self):
        """The other half: the role's own devbox switch is what turns it on,
        with no second key to keep in step.

        All three inputs, because `istota_developer_repos_dir` ships empty and
        is load-bearing rather than incidental — it is the exec server's
        containment root, so there is nothing to mount and nothing to contain
        without one. A render with the devbox on and no repos_dir derives
        `none`, which is the correct answer and not a bug in the template.
        """
        config = load_config_from(
            render(
                istota_developer_enabled=True,
                istota_developer_repos_dir="/srv/app/istota/repos",
                istota_devbox_enabled=True,
            )
        )

        assert devbox_container_backend(config) is True

    def test_the_developer_block_is_absent_when_the_skill_is_off(self, parsed):
        # The role default is off, and an off skill should render no paths at
        # all rather than paths to binaries the role was never asked to install.
        assert "developer" not in parsed


class TestTheDockerApiProxyLeftNothingBehindInTheRole:
    """The Design 14 deletion, checked as a *whole-role* property.

    A partial retirement is the failure mode worth naming: a `defaults` entry
    with no template, a template with no task, or a task notifying a handler
    that no longer exists. Any of those is a play that either does nothing or
    fails on a host, and neither shows up in a rendered config. So this greps
    every file in the role rather than the one template.

    The teardown block is the deliberate exception, and it is matched by name:
    the units it removes are on every host that ever enabled a devbox, and
    leaving a per-user HTTP intermediary listening with no consumer is exactly
    the state this design refused to leave the compose devbox in.
    """

    #: Where the retirement task is allowed to keep saying the name.
    _TEARDOWN_MARKER = "Retire the docker-proxy units"

    def _role_files(self):
        for path in sorted(ANSIBLE.rglob("*")):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".py"}:
                yield path

    def test_the_sweep_reads_something(self):
        """A grep that matched no files would pass against anything."""
        files = list(self._role_files())
        assert len(files) > 5, files
        assert TEMPLATE in files and DEFAULTS_FILE in files and TASKS_FILE in files

    def test_no_api_proxy_key_survives_anywhere_in_the_role(self):
        offenders = [
            f"{path.relative_to(ANSIBLE)}:{n}: {line.strip()}"
            for path in self._role_files()
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
            if "api_proxy" in line
        ]
        assert not offenders, (
            "the Docker-API allowlist proxy is retired; these still name its "
            "config keys or variables:\n" + "\n".join(offenders)
        )

    def test_the_only_remaining_docker_proxy_mentions_are_the_teardown(self):
        tasks = TASKS_FILE.read_text()
        assert self._TEARDOWN_MARKER in tasks, (
            "the teardown block is gone, so an upgraded host keeps a per-user "
            "HTTP intermediary listening with nothing to serve"
        )
        for path in self._role_files():
            if path == TASKS_FILE:
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if "docker-proxy" in line or "docker_proxy" in line:
                    assert "retire" in line.lower() or path.name == "main.yml", (
                        f"{path.relative_to(ANSIBLE)}:{n} still deploys the "
                        f"retired proxy: {line.strip()}"
                    )

    def test_the_templates_are_gone(self):
        for name in (
            "istota-docker-proxy@.service.j2",
            "istota-docker-proxy.tmpfiles.j2",
        ):
            assert not (ANSIBLE / "templates" / name).exists(), name

    def test_the_devbox_block_names_no_retired_key(self):
        rendered = render(istota_devbox_enabled=True)
        parsed = tomllib.loads(rendered)

        assert "devbox" in parsed, "the [devbox] block stopped rendering entirely"
        for retired in (
            "api_proxy_enabled",
            "api_proxy_socket_dir",
            "api_proxy_exec_ttl_seconds",
            "api_proxy_audit_log",
            "docker_socket",
            "exec_timeout_seconds",
        ):
            assert retired not in parsed["devbox"], retired


def load_config_from(rendered: str) -> Config:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(rendered)
        return load_config(path)


class TestTheBrainFallbackDefault:
    """ISSUE-362 — the whole back-compat mechanism for existing tmux deployments.

    The hardcoded ``tmux_claude`` -> ``claude_code`` failover was removed from
    ``effective_fallback_kind``; what keeps a deployed tmux host on the failover
    it already had is one Jinja expression in ``defaults/main.yml``. Nothing else
    asserts it, and a later edit to ``istota_brain_kind``'s default or to the
    ``{% if istota_brain_fallback %}`` gate would break it in silence.
    """

    def test_a_tmux_primary_renders_the_claude_code_fallback(self):
        config = load_config_from(render(istota_brain_kind="tmux_claude"))
        assert config.brain.kind == "tmux_claude"
        assert config.brain.fallback == "claude_code"

    @pytest.mark.parametrize("kind", ["claude_code", "native"])
    def test_no_other_primary_gets_an_implicit_fallback(self, kind):
        rendered = render(istota_brain_kind=kind)
        config = load_config_from(rendered)
        assert config.brain.kind == kind
        assert config.brain.fallback == ""
        assert "\nfallback = " not in rendered

    def test_an_explicit_empty_string_turns_tmux_failover_off(self):
        """The operator override has to win, or "" still means nothing on tmux."""
        rendered = render(istota_brain_kind="tmux_claude", istota_brain_fallback="")
        assert "\nfallback = " not in rendered
        assert load_config_from(rendered).brain.fallback == ""

    def test_a_routed_tmux_target_gets_the_fallback_too(self):
        """A routed config inherits `fallback` from `[brain]`.

        `resolve_brain_kind` returns `replace(brain_config, kind=target)`, so a
        `claude_code` primary routing `scheduled` to tmux loses exactly the same
        failover as a tmux primary — with nothing in `kind` to see it by. The
        template already knows this shape: it is the `_tmux_routed` condition
        that decides whether to emit `[brain.tmux]` at all.
        """
        config = load_config_from(
            render(istota_brain_source_type_overrides={"scheduled": "tmux_claude"})
        )
        assert config.brain.kind == "claude_code"
        assert config.brain.source_type_overrides == {"scheduled": "tmux_claude"}
        assert config.brain.fallback == "claude_code"

    def test_an_explicit_kind_wins_over_the_tmux_default(self):
        config = load_config_from(
            render(istota_brain_kind="tmux_claude", istota_brain_fallback="native")
        )
        assert config.brain.fallback == "native"


class TestTheRoomSelectableAllowlist:
    """The key that decides whether per-room brain selection exists at all.

    It shipped with no Ansible variable and no template line, so on the
    canonical deployment shape a hand edit to the rendered ``config.toml`` was
    overwritten by the next play and the feature was unreachable rather than
    merely off. Both halves are asserted here because either one alone renders
    nothing: a default with no template line is inert, and a template line with
    no default is a ``StrictUndefined`` render failure.
    """

    def test_the_default_renders_no_key_at_all(self, rendered, parsed):
        """Empty must not render ``room_selectable = []``.

        An empty list and an absent key load identically today, so this is about
        the file an operator reads: a key rendered at its own default invites
        editing the generated file, which the next play overwrites.
        """
        assert "room_selectable" not in rendered
        assert "room_selectable" not in parsed["brain"]
        assert load_config_from(rendered).brain.room_selectable == []

    def test_a_configured_list_reaches_the_loaded_config(self):
        config = load_config_from(
            render(istota_brain_room_selectable=["claude_code", "native"])
        )
        assert config.brain.room_selectable == ["claude_code", "native"]

    def test_the_rendered_list_is_offered_to_a_room(self):
        """The loaded value has to survive `room_selectable_kinds`.

        `load_config` accepting the key is not the property that matters — the
        picker reads the allowlist intersected with the kinds `make_brain` can
        build, so a template rendering a shape that survives TOML and dies there
        would still leave the feature unreachable.
        """
        from istota.brain import room_selectable_kinds

        config = load_config_from(
            render(istota_brain_room_selectable=["claude_code", "native"])
        )
        assert room_selectable_kinds(config.brain) == {"claude_code", "native"}


class TestThePackageCacheRoot:
    """ISSUE-305, ISSUE-317, ISSUE-319 — the root, the sweep keys, the bind order.

    **This key has moved twice and the reasons are different, which is why the
    third reader gets a paragraph.** It shipped blank because there was nowhere
    good to put a package cache. `cc691d6f` derived it from
    `istota_developer_repos_dir` — `{repos_dir}/.package-caches` — because uv
    hardlinks out of its cache and `link(2)` compares mounts, so the cache has
    to be inside the bind that also holds the venv, and the repos bind was the
    only such bind. That root was shared by every user, which is ISSUE-319, and
    it cost about 200 lines of sibling masks to make safe.

    It is blank again now, and *not* for the original reason. The daemon derives
    the cache itself, per user, at `{repos_dir}/{user_id}/.package-caches`, and
    `resolve_sandbox_cache_dir` does not read this key at all while `repos_dir`
    is set. A value here would name the fallback path — what a deployment
    running the sandbox *without* the developer skill uses — while reading like
    the intended one. So the assertion below is not "the key is unused"; it is
    "the developer deployment must not set it".
    """

    def test_the_root_stays_blank_whatever_the_repos_dir_says(self):
        """Both ways. A blank `repos_dir` has no tree to derive from, and a set
        one derives inside the per-user subtree without consulting this key."""
        for repos_dir in ("", "/srv/example/repos"):
            rendered = tomllib.loads(render(istota_developer_repos_dir=repos_dir))
            assert "sandbox_cache_dir" not in rendered["security"], (
                f"the role set a cache root for repos_dir={repos_dir!r}; the "
                "daemon derives it and would ignore this value"
            )

    def test_the_default_render_puts_the_cache_inside_the_bind_that_covers_it(
        self, tmp_path,
    ):
        """The rendered default tied to the argv it produces.

        The repos bind has to come *after* the cache bind: that is the single
        mount uv hardlinks across, and the whole reason the cache is derived
        rather than configured. Move either bind and this goes red. What is
        *not* asserted any more is a mask after both — there is no other user's
        cache in the namespace to mask, which is the property that replaced it.
        """
        from unittest.mock import patch

        from istota.db import Task
        from istota.executor import SandboxProfile, build_bwrap_cmd

        repos = tmp_path / "repos"
        (repos / "alice").mkdir(parents=True)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        config = load_config_from(render(
            istota_developer_enabled=True, istota_developer_repos_dir=str(repos),
        ))
        config.temp_dir = tmp_path / "temp"
        assert config.security.sandbox_cache_dir == ""
        task = Task(id=1, prompt="x", user_id="alice", source_type="cli", status="running")

        with patch("istota.executor._bwrap_available", return_value=True):
            argv = build_bwrap_cmd(
                ["claude"], config, task, True, [], user_temp,
                profile=SandboxProfile.CLAUDE,
            )

        binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        cache = str(repos / "alice" / ".package-caches")
        assert cache in binds
        assert str(repos / "alice") in binds
        assert binds.index(str(repos / "alice")) > binds.index(cache), (
            "the repos bind no longer covers the cache bind — uv stops "
            "hardlinking and every worktree pays a full copy"
        )
        assert str(repos) not in binds, "the shared root was bound"

    def test_the_sweep_keys_render_when_an_operator_sets_the_root(self):
        rendered = tomllib.loads(
            render(istota_security_sandbox_cache_dir="/srv/example/repos/.caches")
        )

        assert rendered["security"]["sandbox_cache_dir"] == "/srv/example/repos/.caches"
        assert rendered["security"]["sandbox_cache_sweep_enabled"] is True
        assert rendered["security"]["sandbox_cache_max_gb"] > 0
        assert rendered["scheduler"]["sandbox_cache_sweep_interval"] > 0

    def test_the_role_creates_the_repos_root_and_stops_there(self):
        """The root, at 0755, and nothing below it.

        Nothing else in the tree creates `developer.repos_dir`, and everything
        under it is per user: the daemon makes `{repos_dir}/{user_id}` at 0700
        as each user's first task needs it. A role that also made a per-user
        directory would be inventing a user list at a point in the play where
        it does not have one — so "and stops there" is asserted rather than
        described, by requiring exactly one creator and no `file:` task naming
        anything *below* the root.
        """
        tasks = _flatten(yaml.safe_load(TASKS_FILE.read_text()))
        creators = [
            t for t in tasks
            if isinstance(t.get("file"), dict)
            and t["file"].get("path") == "{{ istota_developer_repos_dir }}"
        ]

        assert len(creators) == 1, (
            f"expected exactly one task creating the repos root, found "
            f"{[t.get('name') for t in creators]}"
        )
        task = creators[0]
        assert task["file"]["owner"] == "{{ istota_user }}"
        assert task["file"]["mode"] == "0755"
        assert any(
            "istota_developer_repos_dir" in str(cond) for cond in task["when"]
        ), "the root is created even where no repos_dir is configured"

        below = [
            t.get("name") for t in tasks
            if isinstance(t.get("file"), dict)
            and str(t["file"].get("path", "")).startswith(
                "{{ istota_developer_repos_dir }}/"
            )
        ]
        assert not below, (
            f"the role creates something under the repos root ({below}); "
            "everything below it belongs to one user and the daemon makes it"
        )

    def test_the_directory_tasks_are_not_skipped_on_an_update_only_deploy(self):
        """Same gate as the migrator, and for the same reason.

        Update-only renders the config and restarts, so it can put the per-user
        binds on a host where nothing has made the root yet. The migrator's own
        `mkdir(parents=True)` would then create it with the daemon's umask
        rather than the owner and mode the role names.
        """
        tasks = _flatten(yaml.safe_load(TASKS_FILE.read_text()))
        paths = (
            "{{ istota_developer_repos_dir }}",
            "{{ istota_security_sandbox_cache_dir }}",
        )
        for path in paths:
            task = next(
                t for t in tasks
                if isinstance(t.get("file"), dict) and t["file"].get("path") == path
            )
            assert not any(
                "istota_update_only" in str(cond) for cond in task["when"]
            ), f"{task['name']!r} is skipped on an update-only deploy"

    def test_the_role_creates_the_fallback_root_the_resolver_requires(self):
        """`resolve_sandbox_cache_dir` refuses a root that does not already exist.

        It falls open on every refusal — a warning in the log and the caches back
        on bubblewrap's root tmpfs — so a `sandbox_cache_dir` whose directory
        nothing creates is the same no-op the key was before, with the appearance
        of a fix. That branch is only reached on a deployment running the sandbox
        without the developer skill, which is exactly where nothing else would
        have made the directory.
        """
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        creators = [
            t for t in tasks
            if isinstance(t.get("file"), dict)
            and t["file"].get("path") == "{{ istota_security_sandbox_cache_dir }}"
        ]

        assert creators, "tasks/main.yml creates no package-cache root"
        task = creators[0]
        assert task["file"]["owner"] == "{{ istota_user }}"

        # 0700, because the root holds one cache directory per user and each is
        # full of package archives uv trusts on read and re-verifies against no
        # hash.
        assert task["file"]["mode"] == "0700"

        # Skipped entirely when the key is blank, which is the shipped default,
        # so a developer deployment — where the cache is derived instead — gets
        # no stray directory out of this.
        assert any(
            "istota_security_sandbox_cache_dir" in str(cond)
            for cond in task["when"]
        )


def _flatten(tasks: list) -> list:
    """Every task, including those nested under `block`/`rescue`/`always`.

    `yaml.safe_load` returns the top-level list, and this file has one-shot
    migrators wrapped in blocks that stop and start the three units. An index
    comparison over the unflattened list cannot see those at all, which is how
    an ordering assertion ends up weaker than the sentence describing it.
    """
    out = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for key in ("block", "rescue", "always"):
            nested = task.get(key)
            if isinstance(nested, list):
                out.extend(_flatten(nested))
    return out


class TestTheReposRelocationTask:
    """The role invokes `istota.repos_relocate`, and how it reads the answer.

    The migrator is where the judgement lives — which admin owns a clone, and
    whether it is safe to move one at all — so the role's whole job is to run it
    at the right point in the play, as the right user, and to read its three
    exit codes apart. Each of those is a way to report a migration that did not
    happen, so each is asserted.
    """

    @pytest.fixture
    def tasks(self):
        return yaml.safe_load(TASKS_FILE.read_text())

    @pytest.fixture
    def migrator(self, tasks):
        found = [
            t for t in tasks
            if "repos_relocate" in str(t.get("command", ""))
        ]
        assert found, "tasks/main.yml never runs the repos migrator"
        return found[0]

    def test_it_runs_as_the_daemon_user(self, migrator):
        """Not as root, and this is the difference between a working migration
        and one the play calls green.

        The migrator creates `{repos_dir}/{user_id}` and renames the namespace
        directories into it. Created by root that directory is root-owned at
        0700, so the daemon can no longer enter the tree the sandbox binds for
        it and every developer task fails afterwards.
        """
        assert migrator.get("become") is True
        assert migrator["become_user"] == "{{ istota_user }}"

    def test_it_reads_the_admins_file_the_units_read(self, migrator):
        """Ownership comes from the admins file, so the migrator has to be
        pointed at the same one the three systemd units are — a renamed
        namespace puts it somewhere `/etc/istota/admins` is not."""
        assert (
            migrator["environment"]["ISTOTA_ADMINS_FILE"]
            == "/etc/{{ istota_namespace }}/admins"
        )

    def test_the_pass_condition_is_stated_positively(self, migrator):
        """`failed_when` replaces the module's own verdict rather than adding
        to it, so a rule naming the migrator's codes hands every *other* code
        back as success.

        `repos_relocate` returns 0, 1 or 2, but the `command` module reports its
        own failures through the same field — a missing interpreter arrives as
        rc 2, a killed process as a signal code. `failed_when: rc == 1` would
        pass both, on a task that moves repositories. So the condition is the
        inverse: pass only where the migrator demonstrably reached its own end,
        which the `done:` line is the evidence of.
        """
        condition = migrator["failed_when"]

        assert "rc != 0" in condition, (
            "the rule enumerates exit codes instead of stating what passes, so "
            "an exit the migrator cannot produce is reported as success"
        )
        assert "done: " in condition, (
            "nothing distinguishes the migrator's own exit 2 from the command "
            "module's"
        )
        assert "FAILED: " in condition, (
            "an exit 2 that moved nothing and wrote no marker is retryable, so "
            "it has to fail the play rather than be reported and passed over"
        )

    def test_a_migration_deferred_by_a_live_task_does_not_fail_the_play(
        self, migrator
    ):
        """The refusal that carries on, and the reason failing was never a
        safeguard.

        A hard failure here does not stop the new code reaching the host: the
        auto-update path resets the checkout to `main` and restarts the units
        whatever this play reports. So failing left the same half-migrated host
        *and* stopped the rest of the deploy. The refusal is transient — the
        tasks finish and the next run migrates — so it is reported instead.
        """
        condition = migrator["failed_when"]

        assert "rc == 1" in condition, (
            "no arm of the pass condition admits a refusal, so a task in "
            "flight fails the whole deploy"
        )
        # Bound to the constant rather than to a copy of its value: the play
        # and the migrator are different languages, and a renamed reason would
        # otherwise leave the play matching a token nothing prints — which
        # fails open, back to a hard failure on every busy host.
        from istota.repos_relocate import REFUSE_LIVE_TASKS

        assert f"refusal: {REFUSE_LIVE_TASKS}" in condition, (
            "the rule keys on something other than the migrator's own reason "
            "code, so it cannot tell a transient refusal from a permanent one"
        )

    def test_only_the_live_task_refusal_is_tolerated(self, migrator):
        """The other reasons stop the play, and the distinction is the whole
        point: none of them resolves on its own.

        An ambiguous set of admins, an unreadable root and a destination that
        is not contained all need a person. Retrying cannot help, so a warning
        would repeat on every deploy for ever and be read as noise.
        """
        condition = migrator["failed_when"]

        for reason in ("no_admins", "many_admins", "unreadable_root"):
            assert reason not in condition, (
                f"the play passes over a {reason} refusal, which never "
                "resolves on its own and so would go unfixed"
            )

    @pytest.mark.parametrize(
        "rc,stderr,should_fail,why",
        [
            (0, "done: 1 moved, marker written\n", False, "a clean migration"),
            (0, "", False, "already migrated: exits before it prints anything"),
            (
                1, "refusal: live_tasks\n", False,
                "transient: the tasks finish and the next deploy migrates",
            ),
            (
                1, "refusal: no_admins\n", True,
                "ownership cannot be inferred and never will be on its own",
            ),
            (
                1, "refusal: unreadable_root\n", True,
                "needs a person; retrying reads the same unreadable root",
            ),
            (
                2, "NOT repaired: /x\ndone: 1 moved, marker written\n", False,
                "moved and marked: re-running has no rename left to perform",
            ),
            (
                2, "FAILED: /x\ndone: 0 moved, marker not written\n", True,
                "nothing moved and no marker, so a re-run retries it",
            ),
            (
                2, "python: No module named istota\n", True,
                "the command module's own exit 2, with no report behind it",
            ),
            (127, "", True, "no interpreter: not the migrator's code at all"),
        ],
    )
    def test_the_pass_condition_evaluates_the_way_it_reads(
        self, migrator, rc, stderr, should_fail, why
    ):
        """The only test that *runs* the expression rather than reading it.

        Every other assertion here greps the condition for a substring, which
        cannot tell a rule that works from one that is merely worded like it —
        and this rule is now two arms joined by `or` inside a `not`, which is
        the shape precedence mistakes hide in. Rendering it through Jinja with
        the module's own result shape is what settles it.
        """
        # `Environment` is imported at module scope, like the rest of this
        # file. Not `importorskip`: jinja2 is a declared dev dependency, and a
        # guard would turn an install that lost it into a silently skipped
        # test rather than a failing one.
        rendered = Environment(undefined=StrictUndefined).from_string(
            "{{ " + migrator["failed_when"] + " }}"
        ).render(_repos_relocate={"rc": rc, "stderr": stderr})

        assert (rendered == "True") is should_fail, (
            f"rc={rc} ({why}): the play "
            f"{'passes over' if should_fail else 'fails on'} it"
        )

    def test_a_deferred_migration_is_reported(self, tasks):
        """Carrying on silently is how a host stays on the old layout with
        nobody knowing. The deploy goes green, so this message is the only
        place it is said."""
        warnings = [
            t for t in _flatten(tasks)
            if "refusal: live_tasks" in str(t.get("when", ""))
            and t.get("debug") is not None
            and "NOT migrated" in str(t.get("debug", {}).get("msg", ""))
        ]

        assert warnings, (
            "the play tolerates a deferred migration and never says so, so a "
            "host on the old layout reports a clean deploy"
        )

    def test_a_partial_that_only_needs_a_hand_does_not_fail_the_play(self, migrator):
        """The other half of the same rule, and the reason it is not just
        "fail on anything but zero".

        An exit 2 whose report carries only `NOT repaired:` moved the tree and
        wrote the marker, so re-running has no rename left to perform and the
        play would stay red for ever — which is how a task ends up skipped.
        """
        assert "rc == 2" in migrator["failed_when"]

    def test_it_runs_on_an_update_only_deploy(self, migrator):
        """The gate that matters, and the one that is easy to get backwards.

        `istota_update_only` is "pull the code, update the dependencies, render
        the config, restart" — precisely the run that puts the per-user binds on
        a host whose clones are still at the old depth. A migrator skipped there
        lands the split and skips the migration on the path most likely to carry
        it. `istota.db_relocate` and the location migrator are ungated the same
        way.
        """
        assert not any(
            "istota_update_only" in str(cond) for cond in migrator["when"]
        ), "the repos migrator is skipped on an update-only deploy"

    def test_a_no_op_run_is_not_reported_as_a_change(self, migrator):
        """It runs on every deploy and is a no-op after the first, so
        `changed_when` has to key on the migrator's own output rather than on
        the exit code, which is 0 either way."""
        condition = migrator["changed_when"]

        # Anchored to the start of a line, not a bare substring. The report
        # prints one `note:` line per entry it declined to move, each beginning
        # with that entry's own name, and entries under `repos_dir` were
        # model-writable on every deployment running the shared bind — so a
        # directory named `moved` renders `note: moved: a symlink; left in
        # place` and a substring test reports `changed` on every deploy after.
        assert "^(moved|repaired): " in condition, (
            "the change test is a bare substring, which model-written output "
            "can satisfy"
        )

        # And the marker, because a first run over an empty tree moves nothing
        # and still writes `.istota-layout` — the one run that touched the disk
        # would otherwise read the same as every run after it. `_print_report`
        # prints "marker not written" on the other branch, which does not
        # contain this substring.
        assert "marker written" in condition

    def test_it_runs_after_the_code_and_before_the_units_are_deployed(self, tasks):
        """Ordering, and both halves matter.

        It needs the new code in the venv to run at all, and it has to be done
        before the play deploys and starts the units — a daemon that picks up
        the per-user binds while the clones are still at the old depth sees an
        empty tree and clones everything again.

        Flattened first. `yaml.safe_load` returns the top-level list only, and
        this file has three one-shot migrations that stop and start all three
        units from *inside* a `block:`; an index comparison over the unflattened
        list cannot see any of them.

        **What this does not claim**, because it is not true: that nothing
        restarts the scheduler before the migration. Those three blocks do, each
        gated on a piece of legacy state (a pre-rename database, framework
        location rows, module databases on the mount) that a current deployment
        does not have. The migrator sits ahead of two of the three and behind
        the oldest. That hazard is theirs and pre-dates this task — the daemon
        is running for the whole play in any case, since nothing stops it at the
        start — so the property worth pinning is the one restart every deploy
        performs.
        """
        flat = _flatten(tasks)
        names = [t.get("name", "") for t in flat]
        migrator = next(
            i for i, t in enumerate(flat)
            if "repos_relocate" in str(t.get("command", ""))
        )
        venv = next(
            i for i, name in enumerate(names)
            if name == "Install Python dependencies with uv"
        )
        assert venv < migrator

        for name in (
            "Deploy istota-scheduler systemd service",
            "Force handlers to run now",
            "Enable and start istota-scheduler",
        ):
            index = next(i for i, other in enumerate(names) if other == name)
            assert migrator < index, f"the migration runs after {name!r}"


class TestTalkSignalingIsReachableFromTheRole:
    """The gap the emitted-keys guard above structurally cannot see.

    That guard asks whether every key the template emits exists on a dataclass,
    which catches a typo. It cannot catch a dataclass section the template
    emits **no** keys for — and `[talk.signaling]` was exactly that: the field
    was on `TalkSignalingConfig`, documented in `config.example.toml`, read by
    `render-config.sh` and passed through `docker-compose.yml`, and absent from
    this template. So the whole feature was unreachable on the Ansible shape,
    which is the canonical production one, with nothing anywhere saying so —
    an operator setting it would have got the dataclass default and a daemon
    that polled.

    Same defect class `config_mapper.py`'s docstring names, one level up: a
    value an operator can set that nothing downstream reads. `docs/deployment/
    ansible.md`'s own four-step procedure for adding a config field is what was
    half-followed.
    """

    def test_the_defaults_render_it_off(self, parsed):
        assert parsed["talk"]["signaling"]["enabled"] is False

    def test_an_operator_can_switch_it_on(self):
        """The whole point: a rendered config the loader reads as enabled.

        Through `load_config` rather than `tomllib` alone, because the question
        is what the *daemon* sees — a key the walk does not recognise is only a
        warning, so a section that parses proves nothing on its own.
        """
        text = render(
            istota_talk_signaling_enabled=True,
            istota_talk_signaling_url="https://hpb.example.com/standalone-signaling",
            istota_talk_signaling_room_sync_interval=120,
            istota_talk_signaling_reconnect_backoff_max=30,
            istota_talk_signaling_payload_direct=True,
        )
        config = load_config(Path(_write_temp(text)))

        assert config.talk.signaling.enabled is True
        assert config.talk.signaling.url == (
            "https://hpb.example.com/standalone-signaling"
        )
        assert config.talk.signaling.room_sync_interval == 120
        assert config.talk.signaling.reconnect_backoff_max == 30
        assert config.talk.signaling.payload_direct is True

    def test_every_signaling_field_is_rendered(self, parsed):
        """The inverse of the emitted-keys guard, scoped to this one section.

        A field added to `TalkSignalingConfig` later and not added here is the
        same silent hole again, and this is the cheapest place to notice.
        """
        emitted = set(parsed["talk"]["signaling"])
        declared = {
            f.name for f in fields(config_module.TalkSignalingConfig)
        }
        assert declared - emitted == set(), (
            "these signaling fields are on the dataclass but the Ansible "
            "template renders none of them, so an operator cannot set them"
        )


class TestThePerBrainModelDefaults:
    """`[brain.claude_code]` / `[brain.tmux]` model + effort (ISSUE-418).

    The top-level `model` was the claude_code brain's own default living at the
    root, where the executor applied it to whatever brain ran. The role now
    renders the per-brain keys, and `istota_brain_claude_code_model` /
    `istota_brain_tmux_model` default from `istota_model` in `defaults/main.yml`
    — which means the migration on this shape is **one Jinja expression per
    key**, and `config._apply_legacy_brain_defaults` never runs here at all,
    since the template stops writing the top-level key. A typo in either default
    is a silent model change on every existing host, and every loader-path test
    would stay green.

    The docker generator's half of the same migration shipped a duplicate
    `[brain.tmux]` table before `tests/test_render_config.py` grew the matching
    class; that is what this one is here to prevent on the other shape.
    """

    def test_the_legacy_variable_fills_the_claude_code_block(self):
        config = load_config_from(render(istota_model="claude-opus-5"))
        assert config.brain.claude_code.model == "claude-opus-5"

    def test_the_legacy_effort_fills_the_claude_code_block(self):
        config = load_config_from(
            render(istota_model="claude-opus-5", istota_effort="high")
        )
        assert config.brain.claude_code.effort == "high"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"istota_brain_kind": "tmux_claude"},
            {"istota_brain_fallback": "tmux_claude"},
            {"istota_brain_source_type_overrides": {"scheduled": "tmux_claude"}},
        ],
        ids=["kind", "fallback", "routed"],
    )
    def test_the_legacy_variable_reaches_tmux_wherever_tmux_can_run(self, overrides):
        """`[brain.tmux]` is rendered only where that brain is reachable.

        Unlike the docker generator, which writes the block whenever a value
        resolves, this template gates it on kind / fallback / a routed target —
        the same condition `[brain.native]` carries. That is not a gap: a brain
        no task can reach has no default worth writing, and the block appears
        the moment the operator makes it reachable. What must hold is that the
        migration reaches it *there*, which is what this asserts.
        """
        config = load_config_from(render(istota_model="claude-opus-5", **overrides))
        assert config.brain.tmux.model == "claude-opus-5"

    def test_no_tmux_block_where_tmux_cannot_run(self):
        rendered = render(istota_model="claude-opus-5")
        assert "\n[brain.tmux]" not in rendered

    def test_the_top_level_key_is_no_longer_rendered(self):
        rendered = render(istota_model="claude-opus-5")
        assert "\nmodel = " not in rendered.split("[brain")[0]
        assert load_config_from(rendered).model == ""

    def test_the_legacy_variable_never_reaches_the_native_brain(self):
        """The one direction the migration must refuse.

        An Anthropic model id cannot carry to an openai_compat endpoint, so
        migrating it there would be the defect inside the fix.
        """
        config = load_config_from(
            render(
                istota_brain_kind="native",
                istota_model="claude-opus-5",
                istota_brain_native_model="z-ai/glm-5.3-flash",
            )
        )
        assert config.brain.native.model == "z-ai/glm-5.3-flash"

    def test_an_explicit_per_brain_value_wins(self):
        config = load_config_from(
            render(
                istota_brain_kind="tmux_claude",
                istota_model="claude-opus-5",
                istota_brain_claude_code_model="claude-haiku-4-5",
            )
        )
        assert config.brain.claude_code.model == "claude-haiku-4-5"
        assert config.brain.tmux.model == "claude-opus-5"

    def test_neither_block_is_rendered_without_a_value(self):
        rendered = render(istota_model="", istota_effort="")
        assert "[brain.claude_code]" not in rendered
        config = load_config_from(rendered)
        assert config.brain.claude_code.model == ""
        assert config.brain.tmux.model == ""

    def test_the_tmux_kind_renders_one_table_with_both_halves(self):
        """Model keys and operability knobs share the one `[brain.tmux]`."""
        rendered = render(
            istota_brain_kind="tmux_claude", istota_model="claude-opus-5"
        )
        # Line-anchored: the `[brain]` block's own comment names the table too,
        # so a bare substring count reads 2 on a correct render.
        assert rendered.count("\n[brain.tmux]") == 1
        config = load_config_from(rendered)
        assert config.brain.tmux.model == "claude-opus-5"
        assert config.brain.tmux.cli_version_pin == "2.1.168"

    def test_the_rendered_blocks_pass_the_play_validator(self):
        """`validate_config.py` allowlists keys per brain sub-table.

        `[brain.tmux]` had one before this change and `[brain.claude_code]` did
        not; both now carry operator-facing `model` keys, and an allowlist that
        does not know about them fails the play on a host that sets one.
        """
        import subprocess
        import sys

        rendered = render(
            istota_brain_kind="tmux_claude",
            istota_model="claude-opus-5",
            istota_effort="high",
        )
        path = _write_temp(rendered)
        config = load_config_from(rendered)
        proc = subprocess.run(
            [
                sys.executable,
                str(ANSIBLE / "files" / "validate_config.py"),
                path,
                "istota",
                str(config.db_path),
                str(config.temp_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# The two sections the template did not render
# ---------------------------------------------------------------------------

SECRETS_TEMPLATE = ANSIBLE / "templates" / "secrets.env.j2"


def render_secrets(**overrides) -> str:
    """`secrets.env.j2` against the same defaults, for the one credential here."""
    env = _environment()
    variables = _resolve(
        {
            **yaml.safe_load(DEFAULTS_FILE.read_text()),
            "ansible_facts": FACTS,
            **overrides,
        },
        env,
    )
    return env.from_string(SECRETS_TEMPLATE.read_text()).render(**variables)


CALDAV_VARS = {
    "istota_caldav_url": "https://dav.example.com",
    "istota_caldav_username": "bot@example.com",
    "istota_caldav_password": "caldav-placeholder",
}


class TestTheCaldavOverride:
    """`[caldav]` was rendered by no generator at all.

    It overrides the `[nextcloud]` derivation `Config.caldav_*` otherwise uses,
    so a deployment whose calendar lives somewhere other than the Nextcloud it
    authenticates against had no way to say so from the role.
    """

    def test_the_default_render_emits_no_block(self, parsed):
        """Absent, not blank. A `[caldav]` block with an empty `url` reads the
        same to the loader, but an operator seeing it in the rendered file
        cannot tell the override off from the override half-configured."""
        assert "caldav" not in parsed

    def test_a_configured_server_reaches_the_loaded_config(self):
        config = load_config_from(render(**CALDAV_VARS))

        assert config.caldav.url == "https://dav.example.com"
        assert config.caldav.username == "bot@example.com"
        assert config.caldav_url == "https://dav.example.com"
        assert config.caldav_username == "bot@example.com"

    def test_the_password_travels_by_environment_file(self):
        """The shape the role actually deploys (`use_environment_file: true`).

        Same rule as `istota_email_imap_password`: `config.toml.j2` emits no
        password and `secrets.env.j2` carries it, which is what
        `_env_secret_overrides` reads back onto `caldav.password`. Asserted
        together with its absence from the config, because either half alone is
        equally true of a credential that reaches the daemon by no route.
        """
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        assert defaults["istota_use_environment_file"] is True

        config = tomllib.loads(render(**CALDAV_VARS))
        assert "password" not in config["caldav"]

        secrets = render_secrets(**CALDAV_VARS)
        assert "ISTOTA_CALDAV_PASSWORD=caldav-placeholder" in secrets

    def test_the_inline_shape_carries_it_instead(self):
        """`use_environment_file = false` is the other supported shape, and the
        flag being what moves the value is the claim. The role renders
        `config.toml` 0600, which is what makes that shape safe at all."""
        config = tomllib.loads(
            render(**CALDAV_VARS, istota_use_environment_file=False)
        )

        assert config["caldav"]["password"] == "caldav-placeholder"

    def test_no_password_line_when_nothing_is_configured(self):
        assert "ISTOTA_CALDAV_PASSWORD" not in render_secrets()

    def test_a_url_with_no_password_renders_nothing(self):
        """The half-shape that leaks, and the reason both variables gate.

        `Config.caldav_url` / `_username` / `_password` fall back to
        `[nextcloud]` independently, so a rendered `[caldav] url` with no
        password anywhere does not fail to authenticate — it names a foreign
        host and lets `caldav_password` fall through to the Nextcloud app
        password, which the daemon then presents to that host. Asserted on the
        loaded `Config` rather than on the rendered text, because the text is
        one step short of the claim.
        """
        config = load_config_from(
            render(
                istota_caldav_url="https://dav.example.com",
                istota_caldav_username="bot@example.com",
                istota_nextcloud_url="https://nextcloud.example.com",
                istota_nextcloud_app_password="nc-placeholder",
            )
        )

        assert config.caldav.url == ""
        assert config.caldav_url == "https://nextcloud.example.com/remote.php/dav"

    def test_a_password_with_no_url_reaches_no_environment_file(self):
        """The mirror half. It leaks nothing, and breaks calendar just as
        quietly: the variable reaches the loader through
        `_env_secret_overrides` whether or not a block rendered, and on its own
        it authenticates to Nextcloud's own DAV endpoint with the wrong
        secret."""
        assert "ISTOTA_CALDAV_PASSWORD" not in render_secrets(
            istota_caldav_password="caldav-placeholder"
        )
        assert "caldav" not in tomllib.loads(
            render(istota_caldav_password="caldav-placeholder")
        )

    def test_the_block_passes_the_play_validator(self):
        import subprocess
        import sys

        rendered = render(**CALDAV_VARS)
        path = _write_temp(rendered)
        config = load_config_from(rendered)
        proc = subprocess.run(
            [
                sys.executable,
                str(ANSIBLE / "files" / "validate_config.py"),
                path,
                "istota",
                str(config.db_path),
                str(config.temp_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


class TestTheNativeSessionLogBlock:
    """`[brain.native.session_log]` shipped with no way to set it from the role.

    Every field defaults, so its absence cost nothing until an operator wanted
    a different retention window or a different directory — and the directory
    is the one that matters, since the sweep deletes under whatever it
    resolves to.
    """

    def test_it_renders_on_a_native_deployment(self):
        parsed = tomllib.loads(render(istota_brain_kind="native"))

        assert "session_log" in parsed["brain"]["native"]

    def test_the_defaults_match_the_dataclass(self):
        """A default restated in a third place is a default that drifts. The
        role's is checked against the dataclass rather than against a literal,
        so a change in code fails here instead of on a host.

        The presence assertion is what stops this being vacuous: an omitted
        block loads as the dataclass defaults too, so the comparison alone
        would have passed before the block existed.
        """
        from istota.config import SessionLogConfig

        text = render(istota_brain_kind="native")
        assert "session_log" in tomllib.loads(text)["brain"]["native"]

        rendered = load_config_from(text)
        shipped = SessionLogConfig()

        for field in fields(SessionLogConfig):
            assert getattr(rendered.brain.native.session_log, field.name) == getattr(
                shipped, field.name
            ), f"session_log.{field.name} drifted from the dataclass default"

    def test_an_operator_value_reaches_the_loaded_config(self):
        config = load_config_from(
            render(
                istota_brain_kind="native",
                istota_brain_native_session_log_enabled=False,
                istota_brain_native_session_log_dir="/srv/app/istota/session-logs",
                istota_brain_native_session_log_retention_days=3,
                istota_brain_native_session_log_max_total_gb=0.5,
                istota_brain_native_session_log_include_thinking=False,
            )
        )

        log = config.brain.native.session_log
        assert log.enabled is False
        assert log.dir == "/srv/app/istota/session-logs"
        assert log.retention_days == 3
        assert log.max_total_gb == 0.5
        assert log.include_thinking is False

    def test_a_claude_code_deployment_renders_no_native_block_at_all(self, parsed):
        """The section sits inside the `[brain.native]` guard, so it follows
        that block rather than appearing on a deployment with no native brain
        anywhere in its routing."""
        assert "native" not in parsed.get("brain", {})

    def _validate(self, rendered: str):
        import subprocess
        import sys

        path = _write_temp(rendered)
        config = load_config_from(rendered)
        return subprocess.run(
            [
                sys.executable,
                str(ANSIBLE / "files" / "validate_config.py"),
                path,
                "istota",
                str(config.db_path),
                str(config.temp_dir),
            ],
            capture_output=True,
            text=True,
        )

    def test_the_rendered_block_passes_the_play_validator(self):
        proc = self._validate(render(istota_brain_kind="native"))

        assert proc.returncode == 0, proc.stderr

    def test_a_typo_under_the_block_fails_the_play(self):
        """`dir` is what the retention sweep unlinks beneath, and a misspelled
        key templates cleanly and falls back to the default with nothing said.
        The sibling `[brain.native.web_fetch]` allowlist exists for that reason
        and this block now has one too - asserted by breaking it, since an
        allowlist nothing tests is one that can go stale in silence."""
        rendered = render(istota_brain_kind="native").replace(
            "\nretention_days = ", "\nretention_dayz = ", 1
        )
        proc = self._validate(rendered)

        assert proc.returncode == 1
        assert "retention_dayz" in proc.stderr

    def test_the_validator_allowlist_matches_the_dataclass(self):
        """Two hand-written copies of one field list. The validator's is not
        importable, so it is read out of the source rather than restated here
        a third time."""
        import re

        from istota.config import SessionLogConfig

        source = (ANSIBLE / "files" / "validate_config.py").read_text()
        block = source.split("sl_allowlist = {", 1)[1].split("}", 1)[0]
        named = set(re.findall(r'"([a-z_]+)"', block))

        assert named == {f.name for f in fields(SessionLogConfig)}


class TestTheWebFetchValidatorAllowlist:
    """The sibling of the test above, for the allowlist it was modelled on.

    `[brain.native.web_fetch]`'s allowlist is the older of the two and had no
    test at all, which is how `admin_only` (ISSUE-449) was added to
    `WebFetchConfig` and to the template while the validator still refused it.
    That failure is loud rather than silent — the play stops with "unknown keys
    under [brain.native.web_fetch]" — but it stops it on the deploy of an
    operator who set the key, not on the change that forgot it.
    """

    def test_the_validator_allowlist_matches_the_dataclass(self):
        import re

        from istota.config import WebFetchConfig

        source = (ANSIBLE / "files" / "validate_config.py").read_text()
        block = source.split("wf_allowlist = {", 1)[1].split("}", 1)[0]
        named = set(re.findall(r'"([a-z_]+)"', block))

        assert named == {f.name for f in fields(WebFetchConfig)}


# ---------------------------------------------------------------------------
# `| default(x, true)` on a boolean discards the operator's `false`.
# ---------------------------------------------------------------------------


class TestABooleanFalseSurvivesTheTemplate:
    """Jinja's `default(x, true)` substitutes on *falsy*, not on undefined.

    So `default(true, true)` returns `true` for a variable the operator
    explicitly set to `false` — the setting is accepted by Ansible, written
    into the inventory, and silently discarded at render. `default(false,
    true)` is unharmed only by luck, since its substitute equals the value
    being discarded.

    The one that matters is `[brain.native.web_fetch] enabled`, whose own line
    in `defaults/main.yml` reads `master switch; false omits the tool`. That
    tool runs in the daemon's network namespace rather than behind the bwrap
    CONNECT allowlist, so an operator turning it off is making a security
    decision — and it did not take.

    Parametrized over every boolean the template renders this way, because all
    four are the same defect and a fix that repaired one would leave the
    others reading as deliberate.
    """

    #: (variable, section path, key). Every `| default(true, true)` in the
    #: template — derived below as well as listed, so the two must agree.
    BOOLEANS = [
        ("istota_brain_native_model_catalog_fetch",
         ("brain", "native"), "model_catalog_fetch"),
        ("istota_brain_native_bash_spill_full_output",
         ("brain", "native"), "bash_spill_full_output"),
        ("istota_brain_native_turn_budget_nudge",
         ("brain", "native"), "turn_budget_nudge"),
        ("istota_brain_native_web_fetch_enabled",
         ("brain", "native", "web_fetch"), "enabled"),
    ]

    #: `[brain.native]` renders only when native is the kind, the fallback, or a
    #: source_type_overrides target, so every case here needs that shape — the
    #: default render has no `[brain.native]` table at all and a lookup into it
    #: raises rather than reporting the value it meant to check.
    NATIVE = {"istota_brain_kind": "native"}

    @staticmethod
    def _dig(parsed: dict, path, key):
        node = parsed
        for part in path:
            node = node[part]
        return node[key]

    @pytest.mark.parametrize(("variable", "path", "key"), BOOLEANS)
    def test_the_default_is_true_so_the_test_below_means_something(
        self, variable, path, key
    ):
        """Control. Each of these renders `true` unset, which is why a
        discarded `false` looks like a working deployment."""
        parsed = tomllib.loads(render(**self.NATIVE))
        assert self._dig(parsed, path, key) is True

    @pytest.mark.parametrize(("variable", "path", "key"), BOOLEANS)
    def test_an_explicit_false_reaches_the_rendered_config(
        self, variable, path, key
    ):
        parsed = tomllib.loads(render(**self.NATIVE, **{variable: False}))
        assert self._dig(parsed, path, key) is False, (
            f"{variable}: false was discarded by the template. `default(x, "
            "true)` substitutes on falsy, so the operator's answer never "
            "reaches config.toml."
        )

    def test_the_template_has_no_falsy_discarding_boolean_default_left(self):
        """The drift guard, and the reason the list above is not the whole
        test: `default(true, true)` is a shape someone will reach for again,
        and it is wrong every time on a boolean whose default is true.

        Numeric defaults are not covered here because they are not one rule:
        `default(20, true)` discards a `0` the same way, but whether `0` is a
        meaningful answer differs per key — for `max_redirects` it is, for a
        timeout it is not. That per-key audit is ISSUE-435 and lives in
        `TestTheNumericDefaults` below.
        """
        offenders = re.findall(
            r"^(\S*?)\s*=.*\|\s*default\(\s*true\s*,\s*true\s*\)",
            TEMPLATE.read_text(),
            re.M,
        )
        assert not offenders, (
            f"config.toml.j2 renders {offenders} with `default(true, true)`, "
            "which returns true for an operator's explicit false. Use "
            "`default(true)`, which substitutes only when undefined."
        )


def _comment_line_flags(lines: list[str]) -> list[bool]:
    """One flag per line: is this line part of a comment?

    Two syntaxes and neither is decidable a line at a time. A `#` line is its
    own comment; a `{# … #}` block runs over several lines whose middles are
    ordinary prose, so the block has to be tracked open and closed as the file
    is walked forward — which is why this is a pass over the whole file rather
    than a predicate a caller can apply to one line.
    """
    flags: list[bool] = []
    inside_block = False
    for line in lines:
        stripped = line.strip()
        flags.append(
            inside_block or stripped.startswith("{#") or stripped.startswith("#")
        )
        if "{#" in line and "#}" not in line[line.index("{#"):]:
            inside_block = True
        elif "#}" in line:
            inside_block = False
    return flags


class TestTheNumericDefaults:
    """ISSUE-435 — the numeric half of the same defect, audited per key.

    `| default(N, true)` substitutes on *falsy* as well as on undefined, so an
    operator who answers `0` gets `N`. Whether that matters is a different
    question for each key, and the answer is in the code that reads it rather
    than in the name: `max_redirects = 0` means "follow no redirects" and is a
    real setting, while `timeout_seconds = 0` means every fetch fails
    immediately and is not.

    So the keys split two ways and both halves are asserted. The ones where `0`
    carries a meaning render a plain reference, with the default in
    `defaults/main.yml` — the shape `[brain.native.session_log]` already uses,
    where a missing default fails the render instead of being substituted in
    silence. The ones where it does not keep the filter *and* carry a comment
    in the template saying so, because an undocumented `default(N, true)` and a
    deliberate one would otherwise read the same way to the next person.

    Every variable named here is defined in `defaults/main.yml`, which is what
    makes the plain reference safe under `StrictUndefined`.
    """

    #: (variable, table path, key, the falsy answer, what it means to the code)
    MEANINGFUL = [
        ("istota_brain_fallback_cooldown_seconds",
         ("brain",), "fallback_cooldown_seconds", 0,
         "no breaker stickiness; every task probes the primary"),
        ("istota_brain_native_context_window",
         ("brain", "native"), "context_window", 0,
         "resolve the window from the catalog"),
        ("istota_brain_native_max_turns",
         ("brain", "native"), "max_turns", 0,
         "no turn cap: `if max_turns and turns >= max_turns`"),
        ("istota_brain_native_model_catalog_cache_ttl_hours",
         ("brain", "native"), "model_catalog_cache_ttl_hours", 0,
         "the disk cache never expires: `if ttl_hours > 0 and ...`"),
        ("istota_brain_native_turn_budget_nudge_early_percent",
         ("brain", "native"), "turn_budget_nudge_early_percent", 0,
         "no early nudge: `if 0 < early_percent <= 100`"),
        ("istota_brain_native_turn_budget_nudge_remaining",
         ("brain", "native"), "turn_budget_nudge_remaining", [],
         "no late ladder; the list is iterated, so [] is the empty ladder"),
        ("istota_brain_native_soft_deadline_percent",
         ("brain", "native"), "soft_deadline_percent", 0,
         "the soft deadline is off: `if 0 < pct < 100`"),
        ("istota_brain_native_web_fetch_max_redirects",
         ("brain", "native", "web_fetch"), "max_redirects", 0,
         "follow no redirects: `for hop in range(max_redirects + 1)`"),
        ("istota_brain_tmux_fallback_trip_threshold",
         ("brain", "tmux"), "fallback_trip_threshold", 0,
         "open the circuit on the first launch failure"),
        ("istota_brain_tmux_fallback_cooldown_seconds",
         ("brain", "tmux"), "fallback_cooldown_seconds", 0,
         "no cooldown: `(now - opened_at) < cooldown` is never true"),
    ]

    #: The other half. `0` reaches no branch in the consuming code, so the
    #: substitution stays — but only where the template says why. Mapped to the
    #: TOML key rather than derived from the variable name: the two agree for
    #: five of the six and `istota_brain_tmux_command_timeout` renders as
    #: `tmux_command_timeout`, so prefix-stripping silently looked for a line
    #: that is not in the file.
    DELIBERATE = {
        "istota_brain_native_max_tokens": "max_tokens",
        "istota_brain_native_web_fetch_timeout_seconds": "timeout_seconds",
        "istota_brain_native_web_fetch_max_bytes": "max_bytes",
        "istota_brain_native_web_fetch_max_content_chars": "max_content_chars",
        "istota_brain_tmux_ready_timeout_seconds": "ready_timeout_seconds",
        "istota_brain_tmux_command_timeout": "tmux_command_timeout",
    }

    #: `context_window`'s role default *is* 0, so `default(0, true)`
    #: substituted 0 for 0 and that line was a no-op rather than a defect. It
    #: is in the list because the plain reference is still the right shape, and
    #: it is named here because it cannot carry the control below.
    ALREADY_ZERO = "istota_brain_native_context_window"

    @staticmethod
    def _shape(variable: str) -> dict:
        """`[brain.native]` and `[brain.tmux]` each render only under their own
        kind, so a case has to ask for the block it reads."""
        if variable.startswith("istota_brain_native_"):
            return {"istota_brain_kind": "native"}
        if variable.startswith("istota_brain_tmux_"):
            return {"istota_brain_kind": "tmux_claude"}
        return {}

    @staticmethod
    def _dig(parsed: dict, path, key):
        node = parsed
        for part in path:
            node = node[part]
        return node[key]

    @pytest.mark.parametrize(
        ("variable", "path", "key", "falsy", "meaning"), MEANINGFUL
    )
    def test_the_role_default_differs_from_the_falsy_answer(
        self, variable, path, key, falsy, meaning
    ):
        """Control. A case whose role default already equals the falsy answer
        cannot tell a substituted default from an honoured one, so it is named
        rather than left in the list looking like coverage."""
        got = self._dig(
            tomllib.loads(render(**self._shape(variable))), path, key
        )
        if variable == self.ALREADY_ZERO:
            assert got == falsy
            return
        assert got != falsy, (
            f"{variable}: the role default equals the falsy answer, so the "
            "case below proves nothing."
        )

    @pytest.mark.parametrize(
        ("variable", "path", "key", "falsy", "meaning"), MEANINGFUL
    )
    def test_a_falsy_answer_reaches_the_rendered_config(
        self, variable, path, key, falsy, meaning
    ):
        """One case here cannot fail and is named rather than counted.

        `ALREADY_ZERO`'s role default is itself the falsy answer, so this
        assertion held against the pre-change template too. It is kept for the
        drift it would catch later — a role default moved off 0 with the plain
        reference reverted — not as evidence about the fix.
        """
        rendered = render(**self._shape(variable), **{variable: falsy})
        got = self._dig(tomllib.loads(rendered), path, key)
        assert got == falsy, (
            f"{variable}: {falsy!r} was discarded by the template and the "
            f"operator got {got!r}. Here {falsy!r} means: {meaning}."
        )

    @pytest.mark.parametrize(
        ("variable", "path", "key", "falsy", "meaning"), MEANINGFUL
    )
    @pytest.mark.parametrize("empty", [None, ""])
    def test_a_null_answer_never_quietly_becomes_a_working_value(
        self, variable, path, key, falsy, meaning, empty
    ):
        """The cost of dropping the filter, pinned rather than discovered.

        `default(N, true)` absorbed null and the empty string along with `0`,
        and a plain reference does not: under Ansible a bare `key:` is `None`,
        which is *defined*, so `StrictUndefined` never sees it and `{{ … }}`
        renders the literal `None` into an integer field.

        What must hold is that such a render never passes for a usable config.
        Five of the six shapes are not TOML at all and the parse itself refuses
        them; the exception is an empty string on the one list-valued key,
        where `to_json` produces a valid TOML string and only the *type* is
        wrong — so both outcomes are accepted here and neither is a value the
        daemon could act on.

        The failure is loud and early because `Deploy istota configuration`
        carries a `validate:`: the render is parsed before it can replace the
        live config.toml, so an operator's typo is a failed play rather than a
        daemon that cannot start after the next unrelated restart.
        """
        rendered = render(**self._shape(variable), **{variable: empty})
        try:
            parsed = tomllib.loads(rendered)
        except tomllib.TOMLDecodeError:
            return
        got = self._dig(parsed, path, key)
        assert not isinstance(got, type(falsy)), (
            f"{variable}: {empty!r} rendered as {got!r}, which parses as a "
            f"usable {type(falsy).__name__} and would reach the daemon."
        )

    def test_the_config_template_task_validates_before_it_replaces_the_file(
        self,
    ):
        """The other half of the case above, in the role rather than the
        template. Without `validate:` the render lands on disk and only the
        separate validation task 90 tasks later objects — by which time the
        known-good config is gone and the auto-update cron restarts the units
        without re-rendering."""
        tasks = _flatten(yaml.safe_load(TASKS_FILE.read_text()))
        deploy = [
            t for t in tasks
            if t.get("name") == "Deploy istota configuration"
        ]
        assert len(deploy) == 1, "expected exactly one config template task"
        validate = deploy[0]["template"].get("validate", "")
        assert "%s" in validate, (
            "the config template task has no `validate:`, so a render that is "
            "not TOML replaces the live config.toml before anything reads it."
        )
        assert "tomllib" in validate

    @pytest.mark.parametrize(
        ("variable", "path", "key", "falsy", "meaning"), MEANINGFUL
    )
    def test_a_falsy_answer_survives_into_the_loaded_config(
        self, variable, path, key, falsy, meaning
    ):
        """Through `load_config`, not just `tomllib` — the seam a host runs,
        and the one that would notice a key the loader drops."""
        config = load_config_from(
            render(**self._shape(variable), **{variable: falsy})
        )
        node = config
        for part in path:
            node = getattr(node, part)
        assert getattr(node, key) == falsy

    def test_every_remaining_falsy_discarding_numeric_default_is_documented(
        self,
    ):
        """The drift guard. A numeric or list `default(N, true)` added later is
        a defect until somebody answers the per-key question, so the template
        may hold one only if it is in `DELIBERATE`.

        String defaults are out of scope: `default('claude_code', true)` on
        `kind` reads an empty string as "unset", which is the ordinary idiom
        for a string and not the same question. That exclusion is carried by
        the *first argument's* first character, which is the only part of the
        expression the scope depends on — so nothing else is anchored. An
        earlier version required the variable to be the token immediately
        after `{{`, which `{{ istota_x | int | default(5, true) }}`,
        `{% set y = istota_x | default(5, true) %}` and a `{{-` marker each
        evade in silence, leaving the guard reporting a clean template.
        """
        text = TEMPLATE.read_text()
        found = set()
        for match in re.finditer(
            r"default\(\s*[-\[0-9][^)]*,\s*[Tt]rue\s*\)", text
        ):
            names = re.findall(r"istota_\w+", text[: match.start()])
            assert names, (
                "a falsy-discarding numeric default with no istota_* variable "
                f"before it: {text[match.start():match.end()]}"
            )
            found.add(names[-1])
        audited = set(self.DELIBERATE)
        assert found == audited, (
            "config.toml.j2's falsy-discarding numeric defaults have drifted "
            f"from the audited set. Unexpected: {sorted(found - audited)}. "
            f"Gone (drop them from DELIBERATE): {sorted(audited - found)}."
        )

    @pytest.mark.parametrize("variable", sorted(DELIBERATE))
    def test_each_deliberate_substitution_says_why_in_the_template(
        self, variable
    ):
        """The comment is the point of keeping the filter at all: an
        undocumented `default(N, true)` and a deliberate one must not look
        identical.

        Scoped to the lines immediately above the assignment, not to the whole
        file's commentary. A search over every comment in a 250-line template
        passes on any comment that happens to mention the key — including one
        three hundred lines away about something else — which is the shape
        that reads as coverage and is not.

        Both comment syntaxes count within that window. A `{# … #}` block
        addresses whoever edits the template and a `#` line renders into the
        operator's own config.toml; either answers the question this asks.
        """
        lines = TEMPLATE.read_text().splitlines()
        key = self.DELIBERATE[variable]
        # Located by the variable, not the TOML key: `timeout_seconds` and
        # `max_content_chars` each name a second, unrelated setting further
        # down the file, and the variable name is what is unique.
        at = [i for i, line in enumerate(lines) if variable in line]
        assert len(at) == 1, f"expected one `{variable}` line, found {len(at)}"

        in_comment = _comment_line_flags(lines)
        window = []
        i = at[0] - 1
        while i >= 0 and in_comment[i]:
            window.append(lines[i])
            i -= 1
        assert key in "\n".join(window), (
            f"{variable} keeps `default(N, true)`, so config.toml.j2 has to "
            f"carry a comment directly above `{key} =` naming it and saying "
            "that 0 reaches no branch in the code."
        )


#: `| istota_toml_escape` as the *final* filter of an expression. Present-anywhere
#: is not enough: a later `| default('a"b')` would emit an unescaped value.
_ESCAPE_LAST = re.compile(r"\|\s*istota_toml_escape\s*\}\}\s*$")


def _interpolations(template_text: str):
    """Every `{{ ... }}` the template emits, with where it lands in the TOML.

    Written here rather than imported because it is the guard's whole claim:
    a rule that says "escape everything inside a basic string" is only worth
    anything if something can tell which interpolations those are.

    Three things here are decisions rather than mechanics, and each was a
    defect in the first version of this parser.

    **Everything is masked in place, never removed.** Jinja spans are replaced
    with same-length filler that keeps their newlines, so both the reported
    line number and the column offsets stay exact. Deleting the comments
    instead made 304 of 307 reported line numbers wrong — the assertion still
    fired correctly, and pointed the reader at unrelated prose 42 lines away,
    which for a guard whose entire output is `file:line` is most of its value
    gone.

    **The scan is over the whole text, not line by line.** A per-line
    `finditer` cannot match an expression wrapped across two lines, and such a
    line yields no spans at all — so the guard's own stated failure mode, a
    new line written by copying a neighbour, passes green if the author also
    wrapped it. `_UNPARSED` below is what makes that loud instead.

    **A comment line is in scope.** It emits into the file like any other, and
    a newline in `istota_namespace` injects a live key from the header comment
    on line 1. Only a value rendered *outside* quotes is exempt, which is the
    integers and booleans.
    """
    masked = _mask_jinja(template_text)
    for match in re.finditer(r"\{\{.*?\}\}", template_text, flags=re.S):
        start, expr = match.start(), match.group(0)
        lineno = template_text.count("\n", 0, start) + 1
        line_start = template_text.rfind("\n", 0, start) + 1
        line_end = template_text.find("\n", start)
        line = template_text[line_start:line_end if line_end != -1 else None]
        prefix = masked[line_start:start]
        # A `\"` in template text is a literal quote, not a delimiter. No site
        # writes one today; counting it would silently invert the parity for
        # every expression after it on the line.
        double = len(re.findall(r'(?<!\\)"', prefix))
        single = len(re.findall(r"(?<!\\)'", prefix))
        if single % 2 == 1:
            # A TOML literal string admits no escapes at all, so the right
            # answer there is a different rule rather than this filter. Report
            # it as needing an escape it cannot have, so it fails loudly if
            # anyone introduces one.
            yield lineno, expr, line.strip(), True
            continue
        yield lineno, expr, line.strip(), double % 2 == 1 or line.lstrip().startswith("#")


def _mask_jinja(text: str) -> str:
    """Blank every Jinja span, preserving length and newlines."""
    out = list(text)
    for match in re.finditer(r"\{#.*?#\}|\{\{.*?\}\}|\{%.*?%\}", text, flags=re.S):
        for i in range(match.start(), match.end()):
            if out[i] != "\n":
                out[i] = "\x00"
    return "".join(out)


class TestAnOperatorValueCannotBreakOutOfItsTOMLString:
    """Every interpolated value is escaped, so punctuation cannot corrupt the file.

    `config.toml.j2` wrote `password = "{{ istota_caldav_password }}"` and
    every one of its ninety-odd siblings with no escaping at all. A `"` in the
    value closes the string early, a trailing `\\` escapes the closing quote
    and swallows the next line, and a literal backslash-n becomes a real
    newline in the parsed value. None of that needs a hostile operator — it
    needs a password from a generator that emits punctuation.

    Three of the shapes below fail loudly, which since ISSUE-435 means the
    play's `validate:` refuses to replace a known-good `config.toml`. The
    fourth does not: a literal `pa\\nss` renders as `pa\\nss`, parses as a real
    newline, and the daemon then authenticates with a credential that is not
    the one the operator set. That one is why this is a correctness bug and
    not only an availability one.

    **Scope is wider than credentials, and wider than the section that
    surfaced it.** ISSUE-436 fixed the same defect one layer up in
    `settings_to_vars._yaml_scalar`, so a value now survives `settings.toml` →
    vars YAML intact and is corrupted here instead. The sites are not only the
    passwords: `bot_name`, `author_credit` and the map `attribution` are
    free-form operator prose on every deployment shape, while the credentials
    reach this file only when `istota_use_environment_file` is false — so
    testing passwords alone would test the shape the role does not deploy.

    Keys are covered too. `[models.aliases]` and
    `[brain.source_type_overrides]` interpolated dict keys as *bare* TOML
    keys, which admit only `A-Za-z0-9_-`; those are quoted basic-string keys
    now, so an alias name with a space renders instead of producing a file
    that does not parse.
    """

    #: Shapes that broke the render, each for a different reason. The label is
    #: what a failure message names, so keep them distinguishable.
    DANGEROUS = [
        ("double-quote", 'pa"ss'),
        ("trailing-backslash", "pass\\"),
        ("literal-backslash-n", "pa\\nss"),
        ("real-newline", "pa\nss"),
        ("carriage-return", "pa\rss"),
        ("nul-adjacent-control", "pa\x01ss"),
        ("del", "pa\x7fss"),
    ]

    #: Shapes that always worked. Present so a "fix" that mangles ordinary
    #: passwords fails here rather than in production — and confirmed by the
    #: negative control, which turns every case above red and leaves these
    #: green.
    #:
    #: `tab` is in this list rather than the one above on TOML's own rule: it
    #: is the single control character a basic string admits raw, so it
    #: round-tripped before the escape existed. It is escaped anyway (a tab in
    #: a rendered credential is otherwise indistinguishable from layout), and
    #: it is here to prove that escaping it does not change the value.
    SAFE = [
        ("ascii-punctuation", "p@ss-w0rd_!#%^&*()+="),
        ("single-quote", "pa'ss"),
        ("unicode", "paßwörd—中文"),
        ("url-shaped", "https://user:tok@example.com/a?b=c#d"),
        ("tab", "pa\tss"),
    ]

    #: Sites, each a different reason to be here:
    #:   caldav password  — a credential, on the shape that renders one inline
    #:   nextcloud user   — ungated, so it renders on every deployment
    #:   bot_name         — top level, above every section header
    #:   map attribution  — free-form prose an operator is invited to write
    SITES = {
        "caldav-password": (
            lambda v: dict(
                istota_use_environment_file=False,
                istota_caldav_url="https://cal.example.com",
                istota_caldav_password=v,
            ),
            lambda p: p["caldav"]["password"],
        ),
        "nextcloud-username": (
            lambda v: dict(istota_nextcloud_username=v),
            lambda p: p["nextcloud"]["username"],
        ),
        "bot-name": (
            lambda v: dict(istota_bot_name=v),
            lambda p: p["bot_name"],
        ),
        "map-attribution": (
            lambda v: dict(
                istota_web_map_provider="custom", istota_web_map_attribution=v
            ),
            lambda p: p["web"]["map"]["attribution"],
        ),
    }

    @pytest.mark.parametrize("site", sorted(SITES))
    @pytest.mark.parametrize(("label", "value"), DANGEROUS + SAFE,
                             ids=[n for n, _ in DANGEROUS + SAFE])
    def test_the_value_round_trips_into_the_parsed_config(self, site, label, value):
        build, read = self.SITES[site]
        rendered = render(**build(value))
        try:
            parsed = tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            pytest.fail(
                f"{site} / {label}: the rendered config is not valid TOML "
                f"({exc}). The value broke out of its basic string."
            )
        assert read(parsed) == value, (
            f"{site} / {label}: the value changed on the way through the "
            f"template. Rendered {read(parsed)!r}, operator set {value!r}."
        )

    def test_a_literal_backslash_n_does_not_become_a_newline(self):
        """The silent one, named on its own because it is the only shape that
        produces a *valid* config carrying the wrong credential."""
        parsed = tomllib.loads(render(
            istota_use_environment_file=False,
            istota_caldav_url="https://cal.example.com",
            istota_caldav_password="pa\\nss",
        ))
        assert parsed["caldav"]["password"] == "pa\\nss"
        assert "\n" not in parsed["caldav"]["password"]

    def test_a_value_cannot_forge_a_neighbouring_key(self):
        """The reason ISSUE-435's `validate:` does not close this on its own:
        a payload can stay valid TOML and still change another setting."""
        parsed = tomllib.loads(render(
            istota_nextcloud_username='u"\nshare_default_expire_days = 999999'
        ))
        assert parsed["nextcloud"]["share_default_expire_days"] != 999999, (
            "a value forged a neighbouring key inside its own section"
        )

    def test_an_alias_name_that_is_not_a_bare_key_still_renders(self):
        """`[models.aliases]` keys come from an operator dict, and a bare TOML
        key admits only `A-Za-z0-9_-`."""
        parsed = tomllib.loads(render(
            istota_models_aliases={"my alias": "some-model"}
        ))
        assert parsed["models"]["aliases"]["my alias"] == "some-model"

    def test_a_dotted_alias_name_stays_one_key(self):
        """The silent half of the bare-key defect, and the reason the nine-line
        churn in the default render is worth paying.

        A bare TOML key splits on `.`, so `[models.aliases.gpt-4.1]` parsed as
        `aliases["gpt-4"]["1"]` — valid TOML, wrong structure, no error at any
        layer. A dot in a model name is ordinary rather than exotic
        (`gpt-4.1`, `claude-3.5`, `gemini-1.5-pro`), so this was reachable by
        an operator doing nothing unusual, and the alias simply did not exist
        at the name they gave it.
        """
        parsed = tomllib.loads(render(
            istota_models_aliases={"gpt-4.1": {"anthropic": "some-model"}}
        ))
        aliases = parsed["models"]["aliases"]
        assert "gpt-4.1" in aliases, f"the alias name was split: {aliases}"
        assert aliases["gpt-4.1"]["anthropic"] == "some-model"

    def test_a_source_type_override_key_is_escaped_too(self):
        parsed = tomllib.loads(render(
            istota_brain_source_type_overrides={'we"ird': "native"}
        ))
        assert parsed["brain"]["source_type_overrides"]['we"ird'] == "native"

    def test_the_hostile_render_still_passes_the_play_validator(self):
        """Valid TOML is not the whole bar: the role runs
        `files/validate_config.py` before any handler restarts the daemon."""
        import subprocess
        import sys

        rendered = render(
            istota_use_environment_file=False,
            istota_caldav_url="https://cal.example.com",
            istota_caldav_password='pa"ss\\',
            istota_bot_name="Bot\nName",
        )
        path = _write_temp(rendered)
        config = load_config_from(rendered)
        assert config.bot_name == "Bot\nName"
        proc = subprocess.run(
            [
                sys.executable,
                str(ANSIBLE / "files" / "validate_config.py"),
                path,
                "istota",
                str(config.db_path),
                str(config.temp_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr

    def test_every_interpolation_inside_a_basic_string_carries_the_escape(self):
        """The drift guard, and the reason the cases above are not the whole
        test.

        A new template line is written by copying the one above it, so the
        next `key = "{{ istota_new_thing }}"` will arrive unescaped and no
        case here would name it. The rule is mechanical — inside a basic
        string, escape — so it is checked mechanically over every site.

        Values rendered *outside* quotes are deliberately not in scope: those
        are the integers and booleans, where a non-numeric value is a
        different bug with a loud failure, and quoting them would change the
        rendered type.

        The escape has to be the **last** filter, not merely present.
        `{{ x | istota_toml_escape | default('a"b') }}` carries the name and
        emits an unescaped default, so a substring test would pass it.
        """
        missing = [
            (lineno, line)
            for lineno, expr, line, inside in _interpolations(TEMPLATE.read_text())
            if inside and not _ESCAPE_LAST.search(expr)
        ]
        assert not missing, (
            "these interpolations render into the file without "
            "`| istota_toml_escape` as their final filter, so a `\"` or a "
            "`\\` in the value corrupts it:\n"
            + "\n".join(f"  config.toml.j2:{n}  {line}" for n, line in missing)
        )

    def test_the_guard_above_can_see_something(self):
        """Control for the guard: a parser that matched nothing would pass it
        silently, and this template has ~100 escaped in-string sites."""
        found = [
            expr for _, expr, _, inside in _interpolations(TEMPLATE.read_text())
            if inside
        ]
        assert len(found) > 80, f"the interpolation parser found only {len(found)}"

    def test_the_guard_reports_the_line_the_reader_should_open(self):
        """The guard's whole output is `file:line`, so the number has to be
        the number in the file. Stripping the Jinja comments before counting
        made 304 of 307 wrong while every assertion still passed."""
        text = TEMPLATE.read_text()
        lines = text.splitlines()
        for lineno, expr, line, _ in _interpolations(text):
            assert lines[lineno - 1].strip() == line, (
                f"reported line {lineno} is {lines[lineno - 1].strip()!r}, "
                f"but the expression {expr!r} is on a different line"
            )

    def test_no_interpolation_escapes_the_parser(self):
        """A wrapped expression used to yield no span at all, so the guard
        passed over exactly the shape it exists to catch."""
        text = TEMPLATE.read_text()
        seen = sum(1 for _ in _interpolations(text))
        assert seen == text.count("{{"), (
            f"the template has {text.count('{{')} `{{{{` but the parser found "
            f"{seen}; an expression it cannot read is one it cannot guard"
        )


class TestTheTomlEscapeFilterItself:
    """Unit cover for the filter the template now depends on ninety-odd times.

    TOML v1.0.0 forbids every C0 control raw in a basic string and gives six
    of them a shorthand; the rest need `\\uXXXX`. The escaper is per character
    rather than a chain of `str.replace`, so the ordering bug the shell
    counterpart in `render-config.sh` has to comment about cannot occur here.
    """

    @staticmethod
    def _escape(value):
        return _custom_filters()["istota_toml_escape"](value)

    @pytest.mark.parametrize(("raw", "expected"), [
        ("plain", "plain"),
        ('a"b', 'a\\"b'),
        ("a\\b", "a\\\\b"),
        ("a\\\"b", "a\\\\\\\"b"),
        ("a\nb", "a\\nb"),
        ("a\rb", "a\\rb"),
        ("a\tb", "a\\tb"),
        ("a\bb", "a\\bb"),
        ("a\fb", "a\\fb"),
        ("a\x00b", "a\\u0000b"),
        ("a\x1fb", "a\\u001Fb"),
        ("a\x7fb", "a\\u007Fb"),
        ("café", "café"),
    ])
    def test_it_escapes_what_toml_requires(self, raw, expected):
        assert self._escape(raw) == expected

    @pytest.mark.parametrize("raw", [
        'a"b', "a\\b", "a\nb", "a\x00b", "a\x7fb", "back\\slash\\\\end\\",
    ])
    def test_the_escaped_form_parses_back_to_the_original(self, raw):
        parsed = tomllib.loads(f'k = "{self._escape(raw)}"')
        assert parsed["k"] == raw

    def test_a_backslash_is_not_doubled_by_the_quote_escape(self):
        """The ordering bug by name: escaping `"` after `\\` would turn `\\"`
        into `\\\\\\"` only if the passes ran in the wrong order."""
        assert self._escape('\\"') == '\\\\\\"'
        assert tomllib.loads(f'k = "{self._escape(chr(92) + chr(34))}"')["k"] == '\\"'

    @pytest.mark.parametrize(("raw", "expected"), [
        (None, "None"), (7, "7"), (True, "True"),
    ])
    def test_a_non_string_is_stringified_exactly_as_jinja_would(self, raw, expected):
        """Coercion rather than a raise: this runs mid-render in a play, and
        `str()` is already what Jinja does to an interpolated non-string, so
        no rendered value changes."""
        assert self._escape(raw) == expected
