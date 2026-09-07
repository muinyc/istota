"""Tests for security hardening: clean env, stripped env, allowed tools, config overrides."""

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from istota import db
from istota.config import (
    Config,
    SecurityConfig,
    load_config,
)
from istota.executor import (
    _CREDENTIAL_ENV_PATTERNS,
    _PROXY_LOOKUP_BLOCKED,
    _SHELL_STARTUP_ENV_VARS,
    CLAUDE_RUNTIME_ENV_VARS,
    _split_credential_env,
    build_allowed_tools,
    build_clean_env,
    build_model_cli_env,
    resolve_sandbox_cache_dir,
    build_stripped_env,
    derive_authorized_skills,
    derive_credential_set,
    derive_lookup_allowlist,
    derive_skill_credential_map,
    execute_task,
    without_claude_runtime_env,
)
from istota.shell_exec import PIPEFAIL_SHELLOPTS, SHELLOPTS_VAR
from istota.skills._env import EnvContext, build_identity_env, build_skill_env
from istota.skills._types import EnvSpec, SkillMeta


class TestBuildCleanEnv:
    def test_returns_minimal_env(self):
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "SECRET_KEY": "abc",
            "SOME_TOKEN": "xyz",
        }, clear=True):
            env = build_clean_env(config)
        # PATH includes the active venv bin dir + the original PATH
        import sys
        venv_bin = str(Path(sys.prefix).resolve() / "bin")
        assert venv_bin in env["PATH"]
        assert "/usr/bin" in env["PATH"]
        assert env["HOME"] == "/home/test"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert "SECRET_KEY" not in env
        assert "SOME_TOKEN" not in env

    def test_includes_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["LANG", "TZ"],
        ))
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "LANG": "en_US.UTF-8",
            "TZ": "America/New_York",
            "OTHER_VAR": "should-not-appear",
        }, clear=True):
            env = build_clean_env(config)
        assert env["LANG"] == "en_US.UTF-8"
        assert env["TZ"] == "America/New_York"
        assert "OTHER_VAR" not in env

    def test_skips_missing_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["LANG", "TZ"],
        ))
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "LANG" not in env
        assert "TZ" not in env

    def test_default_path_when_missing(self):
        config = Config()
        with patch.dict(os.environ, {"HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        # Should include default system paths and the venv bin dir
        assert "/usr/local/bin" in env["PATH"]
        assert "/usr/bin" in env["PATH"]
        import sys
        venv_bin = str(Path(sys.prefix).resolve() / "bin")
        assert venv_bin in env["PATH"]


    def test_passes_user_identity_vars(self):
        """USER/LOGNAME reach the subprocess so the macOS Keychain lookup the
        `claude` CLI uses to find its OAuth credential works (the standalone
        install's default brain reported 'Not logged in' without them)."""
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "USER": "alice",
            "LOGNAME": "alice",
        }, clear=True):
            env = build_clean_env(config)
        assert env["USER"] == "alice"
        assert env["LOGNAME"] == "alice"

    def test_omits_user_identity_vars_when_unset(self):
        config = Config()
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "USER" not in env
        assert "LOGNAME" not in env

    def test_includes_oauth_token(self):
        """CLAUDE_CODE_OAUTH_TOKEN is passed through for auth."""
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-secret",
        }, clear=True):
            env = build_clean_env(config)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-secret"

    def test_every_credential_shaped_var_it_injects_is_named_in_the_strip_set(self):
        """ISSUE-390's drift guard.

        The failure it exists to catch is a second credential-shaped variable
        hand-set in this function later, which would reach a NativeBrain tool
        subprocess exactly as the OAuth token did while
        ``CLAUDE_RUNTIME_ENV_VARS`` went on naming only the first one. Rather
        than guessing at future names, the environment answers every lookup
        with a sentinel, so a variable this function copies through is present
        under its own key whatever the daemon's real environment holds.

        The assertion is over **keys**, not over sentinel values: a value-based
        test sees only an untransformed identity copy, and misses both a rename
        and a transformation — ``PATH`` is already transformed a few lines up,
        so that blind spot is live in the function under test. It covers the
        credential shapes as well as the ``CLAUDE`` family, because the name
        that would actually slip through is `ANTHROPIC_API_KEY`-shaped rather
        than Claude-prefixed.

        This is not a claim about every producer of a task env. `execute_task`
        adds its own names afterwards and a skill's ``setup_env`` hook can add
        arbitrary ones; those are the skill proxy's credential split to answer
        for, not this set's.
        """
        class _AnswersEverything(dict):
            def get(self, key, default=None):
                return f"sentinel-{key}"

            def __getitem__(self, key):
                return f"sentinel-{key}"

            def __contains__(self, key):
                return True

            def keys(self):  # pragma: no cover — see below
                raise AssertionError(
                    "build_clean_env enumerated the environment; this fake "
                    "answers lookups only, so a copy-then-filter refactor "
                    "would silently find nothing to check"
                )

            __iter__ = keys

        config = Config()
        with patch.object(os, "environ", _AnswersEverything()):
            env = build_clean_env(config)

        suspicious = {
            k for k in env
            if k.upper().startswith("CLAUDE")
            or any(p in k.upper() for p in _CREDENTIAL_ENV_PATTERNS)
        }
        # Positive control: a guard that finds nothing to check is not a guard.
        assert suspicious, "build_clean_env injected no credential-shaped var"
        assert suspicious <= CLAUDE_RUNTIME_ENV_VARS

    def test_propagates_admins_file_path(self):
        """ISTOTA_ADMINS_FILE (a path, not a secret) reaches subprocesses so a
        custom-namespace deploy's admins file resolves instead of the hardcoded
        /etc/istota default."""
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "ISTOTA_ADMINS_FILE": "/etc/istota/admins",
        }, clear=True):
            env = build_clean_env(config)
        assert env["ISTOTA_ADMINS_FILE"] == "/etc/istota/admins"

    def test_omits_admins_file_when_unset(self):
        config = Config()
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "ISTOTA_ADMINS_FILE" not in env

    def test_propagates_config_path(self):
        """The loaded config file reaches subprocesses so a `-c /custom` daemon's
        on-demand `skills` loader re-applies guards from the SAME config that
        built the catalogue, not the default search order."""
        from pathlib import Path
        config = Config()
        config.config_path = Path("/srv/app/istota/config.toml")
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert env["ISTOTA_CONFIG_PATH"] == "/srv/app/istota/config.toml"

    def test_omits_config_path_when_unset(self):
        config = Config()  # config_path defaults to None
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "ISTOTA_CONFIG_PATH" not in env


class TestWithoutClaudeRuntimeEnv:
    """ISSUE-390. The environment half of the profile split ISSUE-389 made for
    the sandbox mounts: what a task env carries because the outer process is
    the `claude` CLI, removed where the outer process is istota's own code."""

    def test_strips_every_name_in_the_set(self):
        env = {k: "secret" for k in CLAUDE_RUNTIME_ENV_VARS}
        assert without_claude_runtime_env(env) == {}

    def test_keeps_everything_else(self):
        env = {"PATH": "/usr/bin", "ISTOTA_USER_ID": "alice", "HOME": "/home/a"}
        assert without_claude_runtime_env(dict(env)) == env

    def test_returns_a_copy_and_never_mutates(self):
        """`req.env` is shared: `ClaudeCodeBrain` hands it to the CLI, and
        `_run_fallback` carries it across a reroute with `dataclasses.replace`
        without rebuilding it. An in-place strip would unauthenticate the brain
        that needs the token on the deployment where it is the credential."""
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-fake", "PATH": "/usr/bin"}
        out = without_claude_runtime_env(env)
        assert out is not env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-fake"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in out

    def test_none_stays_none(self):
        """`ToolEnv.subprocess_env` reads `None` as 'inherit the parent env',
        which is a different instruction from an empty mapping."""
        assert without_claude_runtime_env(None) is None

    def test_an_empty_mapping_stays_an_empty_mapping(self):
        """And is still a copy. `{}` must not degrade into `None`: on this path
        that would turn 'an empty environment' into 'inherit the daemon's'."""
        env = {}
        out = without_claude_runtime_env(env)
        assert out == {}
        assert out is not env


class TestTheTaskEnvCanImportIstota:
    """ISSUE-398. The task env is what the tool server is spawned with.

    `NativeBrain._start_tool_server` hands `req.env` — the env this function
    builds — straight to `create_subprocess_exec`, and the argv is
    `[sys.executable, "-m", "istota.tool_server"]`. So the env has to be one
    that interpreter can import `istota` from. Nothing in the env says so, and
    nothing needs to: `PATH` is built from `sys.prefix` because the venv it
    names is where the package is installed. That is a fact about the
    *install*, and it was written down nowhere, so an install could stop
    meeting it in silence.

    One did. `docker/test/Dockerfile` synced the dependencies with
    `--no-install-project`, and the Linux tier's whole native surface failed at
    the handshake with `ModuleNotFoundError: No module named 'istota'` — 47
    tests reading as a code regression on a clean `main`, while `pythonpath =
    ["src", "."]` kept the pytest process itself importing fine. Every shipped
    shape does meet it: Ansible runs `uv sync` and the runtime image runs
    `uv sync --frozen --no-dev --extra all`, both of which install the root
    project.

    Against a real subprocess rather than against the mapping, because the
    mapping cannot show it — what is missing lives in the venv, not in the env.
    """

    def test_a_subprocess_started_with_it_imports_the_tool_server(self, tmp_path):
        env = build_clean_env(Config())
        # Stated, not incidental: carrying `PYTHONPATH` here would make this
        # pass on an uninstalled layout, and it is an import inlet into every
        # python the model runs inside the sandbox — the class `BASH_ENV` is
        # stripped for a few lines above. The install is what has to be right.
        assert "PYTHONPATH" not in env
        # `cwd` outside the tree, because `python -c` puts the working
        # directory on `sys.path`: run from anywhere holding an `istota/`, the
        # import would succeed without the venv having anything to do with it.
        proc = subprocess.run(
            [sys.executable, "-c", "import istota.tool_server"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr


class TestBuildCleanEnvTurnsPipefailOn:
    """ISSUE-321. The Claude Code CLI runs its Bash tool in a shell istota
    never sees, so the option has to arrive through the env the CLI inherits.

    This is the *only* lever that reaches that shell: `shell_argv` fixes shells
    istota spawns itself, and the CLI spawns its own.
    """

    def test_the_env_carries_shellopts(self):
        config = Config()
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert env[SHELLOPTS_VAR] == PIPEFAIL_SHELLOPTS

    def test_a_shell_startup_control_is_never_passed_through(self):
        """The hole the ISSUE-321 review found, and the reason it mattered.

        BASH_ENV names a *file* bash sources before every non-interactive
        shell, so forwarding one is arbitrary code execution before every
        command the model runs. `build_stripped_env` has filtered
        `_SHELL_STARTUP_ENV_VARS` since the interpreter swap; `build_clean_env`
        never did, and its passthrough loop was an unfiltered
        `env[key] = os.environ[key]` — so an operator listing BASH_ENV in
        `passthrough_env_vars` got it forwarded, while the comment added
        alongside the pipefail fix claimed the strip covered this path.

        This is the discriminating case: without the filter the variable is in
        the returned env. The allowlist alone does not cover it, which is why
        asserting `"BASH_ENV" not in env` with an empty passthrough list proves
        nothing — it was never read there.
        """
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["BASH_ENV", "LANG"],
        ))
        with patch.dict(os.environ, {
            "PATH": "/usr/bin", "HOME": "/home/test",
            "BASH_ENV": "/tmp/evil.sh", "LANG": "en_US.UTF-8",
        }, clear=True):
            env = build_clean_env(config)
        assert "BASH_ENV" not in env
        assert env["LANG"] == "en_US.UTF-8", (
            "control: the loop must still pass through an ordinary variable"
        )

    def test_an_inherited_shellopts_is_stripped_before_ours_is_set(self):
        """Strip first, set second.

        The point is not that SHELLOPTS is absent — the fix sets it — but that
        no *inherited* value survives. An operator whose daemon environment
        carried `SHELLOPTS=xtrace` would otherwise have every expanded command
        line, credential values included, echoed into the model's tool output.
        """
        config = Config(security=SecurityConfig(
            passthrough_env_vars=[SHELLOPTS_VAR],
        ))
        with patch.dict(os.environ, {
            "PATH": "/usr/bin", "HOME": "/home/test",
            SHELLOPTS_VAR: "xtrace:verbose",
        }, clear=True):
            env = build_clean_env(config)
        assert env[SHELLOPTS_VAR] == PIPEFAIL_SHELLOPTS
        assert "xtrace" not in env[SHELLOPTS_VAR]

    def test_the_startup_family_is_named_in_one_place(self):
        """Both filters read the same set, so neither can drift alone."""
        assert {"BASH_ENV", "SHELLOPTS", "BASHOPTS"} <= _SHELL_STARTUP_ENV_VARS
        assert "ENV" not in _SHELL_STARTUP_ENV_VARS, (
            "deliberately excluded — POSIX shells read it only when "
            "interactive, and an operator may use it as a deployment name"
        )

    @pytest.mark.skipif(not shutil.which("bash"), reason="no bash on this host")
    def test_a_bash_started_under_this_env_reports_the_failing_stage(self):
        """The reported bug, run through the env the code actually builds.

        A dict assertion cannot make this claim: `SHELLOPTS` sitting in an env
        bash ignores would satisfy one and fix nothing. Pre-fix this returns 0.
        """
        config = Config()
        env = build_clean_env(config)
        failing = subprocess.run(
            ["bash", "-c", "false | head -1"],
            env={**os.environ, **env}, capture_output=True, timeout=30,
        )
        passing = subprocess.run(
            ["bash", "-c", "echo hi | head -1"],
            env={**os.environ, **env}, capture_output=True, timeout=30,
        )
        assert failing.returncode != 0
        assert passing.returncode == 0, "control: a good pipeline must still pass"


class TestSandboxCacheDirEnv:
    """The cache environment, and the two places it must not reach.

    `resolve_sandbox_cache_dir` is the single predicate behind the RW bind in
    `build_bwrap_cmd` and these variables, so the environment can never name a
    cache the sandbox did not mount (ISSUE-305). The variables are set in
    `execute_task`, deliberately not in `build_clean_env`: that function also
    feeds host-side, unsandboxed skill CLIs through the proxy's base env, and a
    daemon-privileged process resolving a cache out of a model-writable
    directory is the confused-deputy shape the PATH handling already guards.
    """

    def _config(self, tmp_path, cache_dir):
        return Config(
            db_path=tmp_path / "data" / "istota.db",
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(sandbox_cache_dir=str(cache_dir)),
        )

    def test_build_clean_env_never_names_a_cache(self, tmp_path):
        """The proxy's base env is built from this, and hands it to host-side
        skill CLIs running outside the sandbox as the daemon user."""
        cache = tmp_path / "uvcache"
        cache.mkdir()
        config = self._config(tmp_path, cache)
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        for var in ("UV_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache", "HF_HOME"):
            assert var not in env, f"{var} would reach every host-side skill CLI"

    def test_resolver_returns_a_per_user_directory_and_creates_it(self, tmp_path):
        cache = tmp_path / "uvcache"
        cache.mkdir()
        config = self._config(tmp_path, cache)
        alice = resolve_sandbox_cache_dir(config, "alice")
        bob = resolve_sandbox_cache_dir(config, "bob")
        assert alice == cache / "alice"
        assert bob == cache / "bob"
        assert alice.is_dir() and bob.is_dir()

    def test_resolver_returns_the_path_as_written_not_resolved(self, tmp_path):
        """`_bind` uses the string it is handed as the sandbox destination, and
        the developer-repos bind passes `repos_dir` unresolved. Resolving here
        would put a symlinked repos_dir and a cache under it at two names inside
        the namespace — two mounts — and `link(2)` returns EXDEV between them,
        which is the full-copy cost the placement recommendation exists to
        avoid, failing silently."""
        real = tmp_path / "realrepos"
        real.mkdir()
        link = tmp_path / "repos"
        link.symlink_to(real)
        (link / "cache").mkdir()

        config = self._config(tmp_path, link / "cache")
        resolved = resolve_sandbox_cache_dir(config, "alice")
        assert resolved == link / "cache" / "alice"
        assert str(real) not in str(resolved)

    def test_unset_returns_none(self, tmp_path):
        config = Config(db_path=tmp_path / "data" / "istota.db", temp_dir=tmp_path / "temp")
        assert resolve_sandbox_cache_dir(config, "alice") is None

    def test_a_missing_directory_returns_none(self, tmp_path):
        config = self._config(tmp_path, tmp_path / "never-created")
        assert resolve_sandbox_cache_dir(config, "alice") is None

    def test_a_relative_path_returns_none(self, tmp_path):
        config = self._config(tmp_path, "relative/cache")
        assert resolve_sandbox_cache_dir(config, "alice") is None

    def test_a_root_above_the_task_workspace_returns_none(self, tmp_path):
        """`config.temp_dir` holds every user's workspace and the read-only
        `.developer` credential helpers inside it."""
        temp = tmp_path / "temp"
        temp.mkdir()
        config = self._config(tmp_path, temp)
        assert resolve_sandbox_cache_dir(config, "alice") is None

    def test_a_broken_config_never_raises(self, tmp_path):
        """Both callers are on the task path — for NativeBrain, per Bash call.
        An exception here would fail every task, which is the outcome failing
        open exists to prevent."""
        cache = tmp_path / "uvcache"
        cache.mkdir()
        # Two shapes nothing should produce. Each makes a path helper inside the
        # resolver raise something that is not ValueError; the contract is that
        # the caller still gets an answer.
        for attr in ("db_path", "temp_dir"):
            broken = self._config(tmp_path, cache)
            setattr(broken, attr, None)
            assert resolve_sandbox_cache_dir(broken, "alice") is None

    def test_each_refusal_warns_once_per_process(self, tmp_path, caplog):
        """Both callers run on every task; two warnings per task forever is not
        a log, it is noise that hides the one that matters."""
        import istota.executor as executor_mod

        executor_mod._cache_dir_refusals.clear()
        config = self._config(tmp_path, "relative/cache")
        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            resolve_sandbox_cache_dir(config, "alice")
            resolve_sandbox_cache_dir(config, "alice")
        hits = [r for r in caplog.records if "sandbox_cache_dir" in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in hits]


class TestBuildStrippedEnv:
    def test_strips_password_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "DB_PASSWORD": "secret",
            "IMAP_PASSWORD": "secret",
            "SMTP_PASSWORD": "secret",
        }, clear=True):
            env = build_stripped_env()
        assert "PATH" in env
        assert "HOME" in env
        assert "DB_PASSWORD" not in env
        assert "IMAP_PASSWORD" not in env
        assert "SMTP_PASSWORD" not in env

    def test_strips_token_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "GITLAB_TOKEN": "glpat-xxx",
            "API_TOKEN": "tok-123",
        }, clear=True):
            env = build_stripped_env()
        assert "GITLAB_TOKEN" not in env
        assert "API_TOKEN" not in env

    def test_strips_secret_and_api_key_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "MY_SECRET": "shh",
            "SERVICE_API_KEY": "key-123",
        }, clear=True):
            env = build_stripped_env()
        assert "MY_SECRET" not in env
        assert "SERVICE_API_KEY" not in env

    def test_strips_bash_env(self):
        """Cron rows and heartbeat commands run under bash now, not `/bin/sh`.

        Bash sources `$BASH_ENV` for a non-interactive shell where dash sources
        nothing, so an inherited value would newly execute a file before every
        one of those commands — a capability the previous interpreter did not
        have. Needs control of the daemon's environment to reach, so it is a
        capability removal rather than a hole being closed.
        """
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "BASH_ENV": "/tmp/evil.sh",
        }, clear=True):
            env = build_stripped_env()
        assert "BASH_ENV" not in env
        assert "PATH" in env

    def test_does_not_strip_env(self):
        """`ENV` is a deployment name far more often than a startup file.

        POSIX shells read it only for *interactive* shells, and `bash -c` is
        not in POSIX mode and reads `BASH_ENV` instead — so stripping it would
        buy nothing and break an operator command that reads `$ENV`.
        """
        with patch.dict(os.environ, {"PATH": "/usr/bin", "ENV": "production"}, clear=True):
            env = build_stripped_env()
        assert env.get("ENV") == "production"

    def test_strips_nc_pass(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "NC_PASS": "nextcloud-pw",
        }, clear=True):
            env = build_stripped_env()
        assert "NC_PASS" not in env

    def test_strips_app_password(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ISTOTA_NEXTCLOUD_APP_PASSWORD": "pw-123",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_NEXTCLOUD_APP_PASSWORD" not in env

    def test_strips_private_key(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "SSH_PRIVATE_KEY": "-----BEGIN",
        }, clear=True):
            env = build_stripped_env()
        assert "SSH_PRIVATE_KEY" not in env

    def test_preserves_non_credential_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "LANG": "en_US.UTF-8",
            "ISTOTA_TASK_ID": "42",
        }, clear=True):
            env = build_stripped_env()
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/test"
        assert env["LANG"] == "en_US.UTF-8"
        assert env["ISTOTA_TASK_ID"] == "42"

    def test_strips_istota_secret_key(self):
        # Phase 1.4 of the unified credential resolution refactor: the
        # master Fernet key never enters any subprocess env. Per-user
        # secrets are pre-resolved on the trusted side via skill manifest
        # ``env: from: "secret"`` blocks and routed through the proxy.
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ISTOTA_SECRET_KEY": "a" * 64,
            "OTHER_SECRET": "should-be-stripped",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_SECRET_KEY" not in env
        assert "OTHER_SECRET" not in env


class TestBuildAllowedTools:
    def test_includes_file_tools(self):
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        for tool in ["Read", "Write", "Edit", "Grep", "Glob"]:
            assert tool in tools

    def test_includes_bash(self):
        """All bash commands allowed — clean env is the security boundary."""
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert "Bash" in tools

    def test_admin_changes_nothing_here_by_default(self):
        """`is_admin` decides no tool unless an operator asks it to.

        It briefly decided one — `WebFetch`, on an egress argument ISSUE-449
        answered with an egress policy instead. `web_fetch_admin_only` is the
        only thing that puts identity back into this list, and
        `tests/test_executor_allowed_tools.py` owns that half. Kept in the
        "same regardless of admin" form the file had before so a second
        identity-scoped tool arriving without its own reasoning turns this red.
        """
        admin_tools = build_allowed_tools(is_admin=True, skill_names=[])
        non_admin_tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert admin_tools == non_admin_tools

    def test_returns_same_tools_regardless_of_skills(self):
        base = build_allowed_tools(is_admin=False, skill_names=[])
        with_dev = build_allowed_tools(is_admin=False, skill_names=["developer"])
        assert base == with_dev

    def test_read_is_present_for_the_claude_code_image_path(self):
        """`Read` is how the two CLI brains deliver an image at all.

        `build_image_prompt` renders the mandatory inspection directive only for
        a request with tools, and the tool it names is this one — so dropping
        `Read` here would turn every image attachment into a named omission with
        nothing failing. Deliberately duplicating `test_includes_file_tools`
        above: that one is about the file toolset, this one is about a contract
        with a specific consumer.
        """
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert "Read" in tools

    def test_includes_web_tools(self):
        """Both web tools go to everyone; page reading is steered to browse in
        the prompt, not by withholding the tools. What bounds `WebFetch` is the
        egress policy on `[brain.native.web_fetch]`, which binds every caller
        the same way."""
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert "WebSearch" in tools
        assert "WebFetch" in tools


class TestConfigEnvVarOverrides:
    def _write_minimal_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[nextcloud]\nurl = "https://nc.example.com"\nusername = "istota"\n'
            'app_password = "toml-password"\n'
            '[email]\nimap_password = "toml-imap"\nsmtp_password = "toml-smtp"\n'
            '[developer]\ngitlab_token = "toml-token"\n'
        )
        return config_file

    def test_nc_app_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_NEXTCLOUD_APP_PASSWORD": "env-password"}, clear=False):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "env-password"

    def test_imap_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_EMAIL_IMAP_PASSWORD": "env-imap"}, clear=False):
            config = load_config(config_file)
        assert config.email.imap_password == "env-imap"

    def test_smtp_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_EMAIL_SMTP_PASSWORD": "env-smtp"}, clear=False):
            config = load_config(config_file)
        assert config.email.smtp_password == "env-smtp"

    def test_gitlab_token_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_DEVELOPER_GITLAB_TOKEN": "env-gl-token"}, clear=False):
            config = load_config(config_file)
        assert config.developer.gitlab_token == "env-gl-token"

    def test_missing_env_var_keeps_toml_value(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        # Ensure none of the override env vars are set
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in {
                "ISTOTA_NEXTCLOUD_APP_PASSWORD", "ISTOTA_EMAIL_IMAP_PASSWORD", "ISTOTA_EMAIL_SMTP_PASSWORD",
                "ISTOTA_DEVELOPER_GITLAB_TOKEN",
            }
        }
        with patch.dict(os.environ, env_clean, clear=True):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "toml-password"
        assert config.email.imap_password == "toml-imap"
        assert config.email.smtp_password == "toml-smtp"
        assert config.developer.gitlab_token == "toml-token"

    def test_security_config_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        config = load_config(config_file)
        assert config.security.sandbox_enabled is True
        assert config.security.skill_proxy_enabled is True

    def test_security_config_overrides(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[security]\nsandbox_enabled = false\nskill_proxy_enabled = false\n'
        )
        config = load_config(config_file)
        assert config.security.sandbox_enabled is False
        assert config.security.skill_proxy_enabled is False


def _bundled_skill_index():
    """Load the real bundled skill manifests for credential-derivation tests."""
    from istota.skills._loader import load_skill_index
    return load_skill_index(Path("config/skills"), bundled_dir=None)


def _ctx_with_config(config: Config) -> EnvContext:
    """Minimal EnvContext for resolution tests."""
    class _T:
        user_id = "alice"
    return EnvContext(
        config=config,
        task=_T(),
        user_resources=[],
        user_config=None,
        user_temp_dir=Path("/tmp"),
        is_admin=False,
    )


class TestDeriveSkillCredentialMap:
    """Per-skill credential map derived from manifests."""

    def test_email_skills_get_email_credentials(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["email"], idx)
        assert result == {"email": {"SMTP_PASSWORD", "IMAP_PASSWORD"}}

    def test_developer_skills_get_developer_credentials(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["developer"], idx)
        assert result == {"developer": {"GITLAB_TOKEN", "GITHUB_TOKEN"}}

    def test_calendar_gets_caldav_password(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["calendar"], idx)
        assert result == {"calendar": {"CALDAV_PASSWORD"}}

    def test_location_gets_caldav_password(self):
        """Location skill needs CALDAV_PASSWORD for attendance subcommand."""
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["location"], idx)
        assert result == {"location": {"CALDAV_PASSWORD"}}

    def test_no_creds_returns_empty(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["browse", "markets"], idx)
        assert result == {}

    def test_empty_skill_list(self):
        idx = _bundled_skill_index()
        assert derive_skill_credential_map([], idx) == {}

    def test_nextcloud_gets_nc_pass(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["nextcloud"], idx)
        assert "NC_PASS" in result["nextcloud"]

    def test_bookmarks_gets_karakeep(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["bookmarks"], idx)
        assert result == {"bookmarks": {"KARAKEEP_API_KEY"}}

    def test_money_gets_monarch_credentials(self):
        """money's Monarch credentials are pre-resolved on the trusted
        side via the manifest ``from: "secret"`` blocks. The cookie pair is
        the only credential we store (programmatic email/password login is
        a transient input flow handled separately)."""
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["money"], idx)
        assert result == {
            "money": {"MONARCH_SESSION_ID", "MONARCH_CSRFTOKEN"},
        }

    def test_feeds_gets_tumblr_key(self):
        idx = _bundled_skill_index()
        assert derive_skill_credential_map(["feeds"], idx) == {
            "feeds": {"TUMBLR_API_KEY"},
        }


class TestMenuSkillDeclarativeEnv:
    """Regression: a menu-loaded skill's non-sensitive declarative env vars
    must be resolved over the FULL skill index, not just ``authorized_skills``.

    A skill the model self-selects from the menu at runtime (via ``skills
    show``) is neither eagerly selected nor — absent its secret — credential-
    authorized. The executor previously resolved ``build_skill_env`` over only
    ``authorized_skills`` (selected ∪ credential-authorized), so such a skill's
    ``from:user_id`` vars (``MONEY_USER`` / ``FEEDS_USER``) were never set and
    the proxied CLI failed with "MONEY_USER not set". ``setup_env`` hooks
    escaped this by iterating the full index; ``build_skill_env`` now does too
    (matching ``scheduler._execute_skill_task``). Sensitive vars stay gated via
    ``_split_credential_env`` + the proxy's ``skill_credential_map``.
    """

    def test_unauthorized_menu_skill_dropped_when_scoped(self):
        """The old, buggy scoping: money is not authorized, so scoping env to
        ``authorized_skills`` omits MONEY_USER."""
        idx = _bundled_skill_index()
        ctx = _ctx_with_config(Config())
        authorized = derive_authorized_skills([], idx, ctx)
        assert "money" not in authorized  # no Monarch secret, not selected
        scoped = build_skill_env(authorized, idx, ctx)
        assert "MONEY_USER" not in scoped

    def test_identity_env_resolves_menu_skill_user_vars(self):
        """The fix: ``build_identity_env`` resolves ``source="user_id"`` vars
        (MONEY_USER / FEEDS_USER) for menu-loaded skills over the full index,
        even without a credential or eager selection."""
        idx = _bundled_skill_index()
        ctx = _ctx_with_config(Config())
        identity = build_identity_env(idx, ctx)
        assert identity.get("MONEY_USER") == "alice"
        assert identity.get("FEEDS_USER") == "alice"

    def test_identity_env_excludes_config_derived_vars(self):
        """Env minimisation: identity resolution is *only* ``user_id`` specs —
        config/secret-derived vars (e.g. KARAKEEP_BASE_URL) are NOT pulled in
        for unselected skills; those stay gated on ``authorized_skills``."""
        idx = _bundled_skill_index()
        ctx = _ctx_with_config(Config())
        identity = build_identity_env(idx, ctx)
        assert "KARAKEEP_BASE_URL" not in identity
        assert "GITHUB_URL" not in identity

    @patch("istota.executor.subprocess.run")
    def test_menu_skill_env_reaches_brain(self, mock_run, tmp_path):
        """End-to-end guard on the executor call site: for a plain task where
        money/feeds are only menu skills (not eagerly selected, no secret),
        their ``from:user_id`` vars must still be in the env handed to the
        brain subprocess. Fails if the executor reverts to resolving
        ``build_skill_env`` over ``authorized_skills`` instead of the full
        index (the "MONEY_USER not set" regression).
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        config = Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,          # empty operator overrides
            bundled_skills_dir=None,        # real bundled skills → money/feeds present
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="how much did I invoice?",
                user_id="alice", source_type="cli",
            )
            task = db.get_task(conn, tid)
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args.kwargs["env"]
        assert env.get("MONEY_USER") == "alice"
        assert env.get("FEEDS_USER") == "alice"


class TestDeriveLookupAllowlist:
    """Lookup allowlist is the union of per-skill credentials, minus the
    instance-wide block list."""

    def test_email_skills_get_email_credentials(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(["email"], idx) == {
            "SMTP_PASSWORD", "IMAP_PASSWORD",
        }

    def test_multiple_skills_union(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(
            ["email", "developer", "calendar"], idx,
        ) == {
            "SMTP_PASSWORD", "IMAP_PASSWORD",
            "GITLAB_TOKEN", "GITHUB_TOKEN",
            "CALDAV_PASSWORD",
        }

    def test_skills_with_no_credentials(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(
            ["browse", "transcribe", "markets"], idx,
        ) == set()

    def test_master_key_blocked_even_if_injected(self):
        """Defense-in-depth: a setup_env hook injecting ISTOTA_SECRET_KEY
        cannot make it through the lookup endpoint. Build an ad-hoc skill
        that declares the var sensitive and verify subtraction."""
        idx = {
            "evil": SkillMeta(
                name="evil",
                description="",
                env_specs=[EnvSpec(
                    var="ISTOTA_SECRET_KEY", source="setup_env",
                    sensitive=True,
                )],
            ),
        }
        assert derive_lookup_allowlist(["evil"], idx) == set()


class TestSplitCredentialEnv:
    """``_split_credential_env`` takes the credential set and partitions
    the env dict."""

    def test_routes_pre_resolved_secrets(self):
        env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp",
            "MONARCH_SESSION_ID": "sid-abc",
            "TUMBLR_API_KEY": "tk-xyz",
            "ISTOTA_TASK_ID": "42",
        }
        credential_env, clean_env = _split_credential_env(
            env, frozenset({"MONARCH_SESSION_ID", "TUMBLR_API_KEY"}),
        )
        assert credential_env == {
            "MONARCH_SESSION_ID": "sid-abc",
            "TUMBLR_API_KEY": "tk-xyz",
        }
        assert "MONARCH_SESSION_ID" not in clean_env
        assert "TUMBLR_API_KEY" not in clean_env
        assert clean_env["PATH"] == "/usr/bin"
        assert clean_env["ISTOTA_TASK_ID"] == "42"

    def test_empty_credential_set_passes_env_through(self):
        env = {"PATH": "/usr/bin", "TOKEN": "tok"}
        credential_env, clean_env = _split_credential_env(env, frozenset())
        assert credential_env == {}
        assert clean_env == env


class TestProxyLookupBlocked:
    """ISTOTA_SECRET_KEY is on the defense-in-depth block list so a
    setup_env hook injecting it cannot leak it via credential-fetch.

    (Regression for the c1055d0 follow-up: pre-patch, money/feeds were
    auto-authorized on every host because the master key was set
    instance-wide, and `.developer/credential-fetch ISTOTA_SECRET_KEY`
    returned the raw Fernet key.)
    """

    def test_master_key_in_block_set(self):
        assert "ISTOTA_SECRET_KEY" in _PROXY_LOOKUP_BLOCKED


class TestPhase1MasterKeyEgress:
    """Phase 1 acceptance: ISTOTA_SECRET_KEY must never enter any
    subprocess env after Phase 1.4."""

    def test_credential_set_excludes_master_key(self):
        """ISTOTA_SECRET_KEY is not declared sensitive on any skill
        manifest, so it never appears in the derived credential set —
        it stays on the clean env (the trusted daemon needs it)."""
        idx = _bundled_skill_index()
        assert "ISTOTA_SECRET_KEY" not in derive_credential_set(idx)

    def test_skill_credential_map_excludes_master_key(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(list(idx.keys()), idx)
        for creds in result.values():
            assert "ISTOTA_SECRET_KEY" not in creds

    def test_build_clean_env_excludes_master_key(self):
        """Even with the master key in the parent env, Claude's clean env
        omits it. (build_clean_env strictly allowlists what flows through.)"""
        from istota.executor import build_clean_env
        from istota.config import Config
        with patch.dict(os.environ, {
            "ISTOTA_SECRET_KEY": "k" * 64,
            "PATH": "/usr/bin",
        }, clear=True):
            env = build_clean_env(Config())
        assert "ISTOTA_SECRET_KEY" not in env

    def test_build_stripped_env_excludes_master_key(self):
        """Phase 1.4 — build_stripped_env (heartbeat / command-task path)
        also strips the master key. Operator-defined heartbeat shells that
        called istota-skill feeds/money directly stop working; documented
        in CHANGELOG."""
        with patch.dict(os.environ, {
            "ISTOTA_SECRET_KEY": "k" * 64,
            "PATH": "/usr/bin",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_SECRET_KEY" not in env


class TestBuildModelCliEnv:
    """`build_model_cli_env` is the env every daemon-side `claude` spawn
    uses: the clean allowlist plus the CLI's own auth credential."""

    def test_is_clean_env_plus_api_key(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "ANTHROPIC_API_KEY": "sk-ant-parent",
            "ISTOTA_SECRET_KEY": "k" * 64,
            "NC_PASS": "nextcloud-app-password",
        }, clear=True):
            env = build_model_cli_env(Config())
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-parent"
        assert env["HOME"] == "/home/test"
        assert "ISTOTA_SECRET_KEY" not in env
        assert "NC_PASS" not in env

    def test_omits_api_key_when_parent_has_none(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            env = build_model_cli_env(Config())
        assert "ANTHROPIC_API_KEY" not in env

    def test_does_not_override_a_key_already_on_the_clean_env(self):
        """The inheritance is a fallback, not an override.

        Stubbing `build_clean_env` is the only way to make the two sources
        differ: its passthrough loop reads `os.environ` too, so through the
        public API both values are the same string by construction and the
        guard cannot be observed.
        """
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-parent",
        }, clear=True):
            with patch(
                "istota.executor.build_clean_env",
                return_value={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-resolved"},
            ):
                env = build_model_cli_env(Config())
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-resolved"

    def test_passthrough_listed_key_survives(self):
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["ANTHROPIC_API_KEY"],
        ))
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-parent",
        }, clear=True):
            env = build_model_cli_env(config)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-parent"


class TestClaudeCliTriageEnv:
    """ISSUE-232 — the context-triage `claude -p` spawn was the one `claude`
    subprocess in the tree with no `env=` kwarg, so it inherited the daemon's
    whole environment (master key, Nextcloud app password, every token) on a
    prompt built from user-influenced conversation history."""

    def _triage_env(self, parent_env: dict[str, str]) -> dict[str, str]:
        """Run `_claude_cli_triage` under `parent_env`, return the child env."""
        from istota import context

        with patch.dict(os.environ, parent_env, clear=True):
            with patch("istota.brain.claude_code.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout='{"relevant_ids": []}',
                )
                context._claude_cli_triage("prompt", "some-model", 30.0, Config())
        assert mock_run.call_count == 1
        env = mock_run.call_args.kwargs.get("env")
        assert env is not None, "triage spawned `claude` without an explicit env"
        return env

    def test_master_key_not_inherited(self):
        env = self._triage_env({
            "PATH": "/usr/bin",
            "ISTOTA_SECRET_KEY": "k" * 64,
        })
        assert "ISTOTA_SECRET_KEY" not in env

    def test_service_credentials_not_inherited(self):
        env = self._triage_env({
            "PATH": "/usr/bin",
            "NC_PASS": "nextcloud-app-password",
            "SMTP_PASSWORD": "smtp-secret",
            "IMAP_PASSWORD": "imap-secret",
            "MONARCH_SESSION_ID": "sid-abc",
        })
        for key in ("NC_PASS", "SMTP_PASSWORD", "IMAP_PASSWORD", "MONARCH_SESSION_ID"):
            assert key not in env

    def test_cli_auth_still_reaches_the_child(self):
        """The triage spawn is a `claude` invocation — stripping the model
        credential along with everything else would leave it permanently
        unauthenticated, and triage fails open, so the breakage would be
        silent. Both auth shapes have to survive."""
        env = self._triage_env({
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-parent",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-parent",
        })
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-parent"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-parent"

    def test_child_keeps_what_it_needs_to_run(self):
        env = self._triage_env({"PATH": "/usr/bin", "HOME": "/home/test"})
        assert "/usr/bin" in env["PATH"]
        assert env["HOME"] == "/home/test"
