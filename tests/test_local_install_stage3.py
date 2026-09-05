"""Stage 3 tests for the local single-user install: the ``istota setup`` wizard.

Drives ``setup_wizard.run_setup`` with mocked stdin + ``shutil.which`` across
the three brain branches, asserts the written ``config.toml`` / ``istota.env``
fields, the DB + workspace bootstrap, and the clobber guard.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import setup_wizard
from istota.setup_wizard import Answers, render_config_toml, render_env_file


@pytest.fixture(autouse=True)
def _isolate_config_path_env():
    """Keep the wizard's ``ISTOTA_CONFIG_PATH`` write out of the worker process.

    ``setup_wizard._bootstrap`` sets it on ``os.environ`` directly — correct for
    a one-shot CLI process, but in-process it leaks into every later test in the
    same xdist worker (it broke ``test_config_path_absent_when_unset``, which
    asserts the var is unset). Snapshot/restore by hand: ``monkeypatch.delenv``
    can't help because it records no undo entry when the var starts out absent,
    which is exactly this case.
    """
    saved = os.environ.get("ISTOTA_CONFIG_PATH")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ISTOTA_CONFIG_PATH", None)
        else:
            os.environ["ISTOTA_CONFIG_PATH"] = saved


def _args(**kw):
    base = dict(
        config=None, workspace=None, brain=None, native_base_url=None,
        native_model=None, native_api_key=None, user=None, display_name=None,
        timezone=None, port=None, email=False, location=False,
        no_money=False, no_health=False, no_feeds=False, no_briefings=False,
        yes=False, force=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_config_has_local_defaults(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice", web_port=8766)
        toml = render_config_toml(a)
        assert 'auth = "none"' in toml
        assert "[talk]\nenabled = false" in toml
        assert "sandbox_enabled = false" in toml
        assert "[users.alice]" in toml
        assert "port = 8766" in toml
        assert 'kind = "claude_code"' in toml

    def test_config_native_brain_block(self, tmp_path):
        a = Answers(
            workspace=tmp_path / "ws", user_id="alice", brain_kind="native",
            native_base_url="https://api.example.com/v1", native_model="my-model",
        )
        toml = render_config_toml(a)
        assert "[brain.native]" in toml
        assert 'base_url = "https://api.example.com/v1"' in toml
        assert 'model = "my-model"' in toml
        # The API key never lands in TOML.
        assert "api_key" not in toml

    def test_config_parses_back(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice", web_port=9000)
        p = tmp_path / "config.toml"
        p.write_text(render_config_toml(a))
        from istota.config import load_config
        cfg = load_config(p)
        assert cfg.web.auth == "none"
        assert cfg.web.port == 9000
        assert cfg.talk.enabled is False
        assert cfg.security.sandbox_enabled is False
        assert "alice" in cfg.users

    def test_config_disables_emissaries(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice")
        p = tmp_path / "config.toml"
        p.write_text(render_config_toml(a))
        from istota.config import load_config
        cfg = load_config(p)
        assert cfg.emissaries_enabled is False

    def test_env_file_keys(self, tmp_path):
        a = Answers(
            workspace=tmp_path / "ws", brain_kind="native",
            native_api_key="sk-test", session_secret="deadbeef",
        )
        env = render_env_file(a)
        assert "ISTOTA_WEB_INSECURE_COOKIES=1" in env
        assert "ISTOTA_WEB_SESSION_SECRET_KEY=deadbeef" in env
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-test" in env

    def test_env_file_no_native_key_when_claude(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", brain_kind="claude_code", session_secret="x")
        env = render_env_file(a)
        assert "ISTOTA_BRAIN_NATIVE_API_KEY" not in env

    def test_money_on_by_default_no_disabled_modules(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice")
        toml = render_config_toml(a)
        assert "disabled_modules" not in toml

    def test_money_off_writes_disabled_modules(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice", money_enabled=False)
        toml = render_config_toml(a)
        assert 'disabled_modules = ["money"]' in toml


# ---------------------------------------------------------------------------
# Timezone resolution (a "PDT" abbreviation must never be stored)
# ---------------------------------------------------------------------------


class TestWebPortPrompt:
    """A typo at the port prompt used to end the installer in a traceback.

    `port = int(_ask(...))` with no guard, so `8766 ` with a stray character,
    a pasted URL, or a mis-sequenced answer raised `ValueError` straight out
    of `collect_answers` — before the config, the env file, the admins file or
    the database existed, so nothing was half-written, but a first-run
    installer that ends in a stack trace on a typo is a bad first impression
    of the thing being installed. `_ask_yes_no` was fixed for the same class
    of input a change earlier; this is the other prompt that parses.
    """

    def _collect(self, answers, out=None):
        args = _args(workspace="/tmp/ws-unused", user="alice", brain="claude_code")
        args.port = None
        it = iter(answers)
        return setup_wizard.collect_answers(
            args,
            input_fn=lambda prompt: next(it, ""),
            which_fn=lambda _: "/usr/bin/claude",
            out=out or (lambda *a: None),
            getpass_fn=lambda *a, **k: "",
        )

    def test_a_non_integer_re_asks_rather_than_raising(self):
        said = []
        # display name, timezone, then the bad port, then a good one.
        a = self._collect(["alice", "UTC", "me@fastmail.com", "9000"], out=said.append)
        assert a.web_port == 9000
        assert any("whole number" in line for line in said)

    def test_a_blank_takes_the_default(self):
        assert self._collect(["alice", "UTC", ""]).web_port == setup_wizard.DEFAULT_PORT

    def test_a_port_outside_the_range_re_asks(self):
        """0 and 70000 are not ports. `[web] port` reaches a bind, and the
        failure would land at `istota serve` rather than here."""
        a = self._collect(["alice", "UTC", "70000", "0", "8080"])
        assert a.web_port == 8080

    def test_it_gives_up_on_the_default_rather_than_looping(self):
        """Bounded like `_ask_yes_no`, and for its reason: a non-interactive
        `input_fn` returning the same value forever must not spin."""
        a = self._collect(["alice", "UTC", "nope", "nope", "nope", "nope"])
        assert a.web_port == setup_wizard.DEFAULT_PORT


class TestTimezone:
    def test_is_valid_timezone(self):
        assert setup_wizard._is_valid_timezone("America/Los_Angeles")
        assert setup_wizard._is_valid_timezone("UTC")
        # Abbreviations are NOT valid IANA names.
        assert not setup_wizard._is_valid_timezone("PDT")
        assert not setup_wizard._is_valid_timezone("PST")
        assert not setup_wizard._is_valid_timezone("")

    def test_default_timezone_from_tz_env(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        assert setup_wizard._default_timezone() == "America/New_York"

    def test_default_timezone_ignores_abbreviation_tz_env(self, monkeypatch):
        # A bogus TZ shouldn't win; fall through to /etc/localtime or UTC.
        monkeypatch.setenv("TZ", "PDT")
        assert setup_wizard._default_timezone() != "PDT"

    def test_default_timezone_is_always_a_valid_zone(self):
        # Whatever the host, the derived default must be ZoneInfo-loadable.
        assert setup_wizard._is_valid_timezone(setup_wizard._default_timezone())

    def test_collect_rejects_abbreviation_flag(self):
        # --timezone PDT must not be stored verbatim.
        args = _args(yes=True, timezone="PDT", user="alice")
        out_lines: list[str] = []
        a = setup_wizard.collect_answers(
            args, input_fn=lambda p: "", which_fn=lambda n: "/usr/bin/claude",
            out=out_lines.append, getpass_fn=lambda p: "",
        )
        assert a.timezone != "PDT"
        assert setup_wizard._is_valid_timezone(a.timezone)
        assert any("not a valid IANA timezone" in line for line in out_lines)

    def test_collect_accepts_valid_flag(self):
        args = _args(yes=True, timezone="Europe/Berlin", user="alice")
        a = setup_wizard.collect_answers(
            args, input_fn=lambda p: "", which_fn=lambda n: "/usr/bin/claude",
            out=lambda s: None, getpass_fn=lambda p: "",
        )
        assert a.timezone == "Europe/Berlin"


# ---------------------------------------------------------------------------
# Wizard branches
# ---------------------------------------------------------------------------


def _run(args, tmp_path, which_result, inputs=None):
    """Run setup with config dir under tmp_path and mocked which/input."""
    config_path = tmp_path / "cfg" / "config.toml"
    args.config = str(config_path)
    inputs = list(inputs or [])
    it = iter(inputs)

    def fake_input(prompt):
        try:
            return next(it)
        except StopIteration:
            return ""

    def fake_which(name):
        return which_result

    out_lines: list[str] = []
    rc = setup_wizard.run_setup(
        args, input_fn=fake_input, which_fn=fake_which, out=out_lines.append,
        # The API key is read via getpass; share the same input iterator so the
        # flat `inputs` list keeps working in order.
        getpass_fn=fake_input,
    )
    return rc, config_path, out_lines


class TestWizardBranches:
    def test_claude_detected_and_accepted(self, tmp_path):
        # --yes with claude present → claude_code, defaults.
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "claude_code"' in toml
        assert (config_path.parent / "istota.env").exists()

    def test_claude_declined_falls_to_native(self, tmp_path):
        # Interactive: decline claude, then supply native details.
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        inputs = [
            "n",                    # decline claude
            "https://api.x/v1",     # base url
            "my-model",             # model
            "sk-abc",               # api key
            "alice",               # display name
            "UTC",                  # timezone
            "8766",                 # port
            "n",                    # location
            "y",                    # money
            "y",                    # health
            "y",                    # feeds
            "y",                    # briefings
            "n",                    # email
            "n",                    # caldav
        ]
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude", inputs=inputs)
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "native"' in toml
        assert 'model = "my-model"' in toml
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-abc" in env

    def test_no_claude_native_noninteractive(self, tmp_path):
        args = _args(
            yes=True, workspace=str(tmp_path / "ws"), user="alice",
            brain="native", native_model="m", native_api_key="k",
            native_base_url="https://api.y/v1",
        )
        rc, config_path, _ = _run(args, tmp_path, which_result=None)
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "native"' in toml

    def test_native_without_key_errors(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice", brain="native", native_model="m")
        with pytest.raises(setup_wizard.SetupError, match="API key"):
            _run(args, tmp_path, which_result=None)

    def test_native_empty_key_reprompts(self, tmp_path):
        # A stray blank line before the key (paste artifact) must not silently
        # leave it empty — the secret reader re-prompts until a real value.
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        inputs = [
            "n",                    # decline claude
            "https://api.x/v1",     # base url
            "my-model",             # model
            "",                     # API key: stray empty line (re-prompts)
            "sk-real",              # API key: real value
            "alice",               # display name
            "UTC",                  # timezone
            "8766",                 # port
            "n",                    # location
            "y",                    # money
            "y",                    # health
            "y",                    # feeds
            "y",                    # briefings
            "n",                    # email
            "n",                    # caldav
        ]
        rc, config_path, out = _run(
            args, tmp_path, which_result="/usr/bin/claude", inputs=inputs,
        )
        assert rc == 0
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-real" in env
        assert any("API key is required" in line for line in out)

    def test_native_interactive_key_not_echoed_via_input(self, tmp_path):
        # The key must come from getpass_fn, not input_fn — assert input_fn is
        # never handed the raw key value.
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        seen_input_prompts: list[str] = []

        config_path = tmp_path / "cfg" / "config.toml"
        args.config = str(config_path)
        answers = iter([
            "n", "https://api.x/v1", "my-model",  # brain
            "SECRET-KEY",                           # getpass reads this
            "alice", "UTC", "8766",                 # display name, tz, port
            "n",                                    # location
            "y", "y", "y", "y",                     # money, health, feeds, briefings
            "n",                                    # email
            "n",                                    # caldav
        ])

        def fake_input(prompt):
            seen_input_prompts.append(prompt)
            return next(answers)

        def fake_getpass(prompt):
            assert "API key" in prompt
            return next(answers)

        rc = setup_wizard.run_setup(
            args, input_fn=fake_input, which_fn=lambda _n: "/usr/bin/claude",
            out=lambda _l: None, getpass_fn=fake_getpass,
        )
        assert rc == 0
        assert not any("API key" in p for p in seen_input_prompts)

    def test_bootstrap_inits_db_and_workspace(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        # DB created under the workspace.
        db_path = tmp_path / "ws" / "istota.db"
        assert db_path.exists()
        # Workspace dirs seeded.
        assert (tmp_path / "ws" / "Users" / "alice").is_dir()
        # User profile row exists.
        from istota import user_profiles
        assert user_profiles.get_profile(db_path, "alice") is not None

    def test_no_money_disables_module_end_to_end(self, tmp_path):
        args = _args(
            yes=True, workspace=str(tmp_path / "ws"), user="alice", no_money=True,
        )
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        from istota import user_profiles
        from istota.config import load_config
        db_path = tmp_path / "ws" / "istota.db"
        prof = user_profiles.get_profile(db_path, "alice")
        assert prof is not None and "money" in prof.disabled_modules
        cfg = load_config(config_path)
        assert cfg.is_module_enabled("alice", "money") is False
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_env_file_is_chmod_600(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env_path = tmp_path / "cfg" / "istota.env"
        import stat
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600


class TestClobberGuard:
    def test_refuses_without_force_noninteractive(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")  # first run
        # Second run without --force must refuse.
        args2 = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        with pytest.raises(setup_wizard.SetupError, match="already exists"):
            _run(args2, tmp_path, which_result="/usr/bin/claude")

    def test_force_overwrites(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="bob")
        rc, config_path, _ = _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert "[users.bob]" in config_path.read_text()

    def test_interactive_decline_update_aborts(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        # Interactive re-run, answer "n" to "update in place?".
        args2 = _args(workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args2, tmp_path, which_result="/usr/bin/claude", inputs=["n"])
        assert rc == 1
        assert any("aborted" in line.lower() for line in out)


def _env_values(path):
    """Parse the wizard's ``istota.env`` the way ``serve.load_env_file`` does."""
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class TestSecretKey:
    """The master Fernet key the standalone wizard never generated.

    Without ``ISTOTA_SECRET_KEY`` every stored credential is unreachable —
    `istota secret`, the per-user ntfy service, Garmin, Monarch and the Google
    Workspace tokens all raise `SecretKeyMissingError`. Regenerating it on a
    ``--force`` re-run is worse than not having one: it makes every already
    stored credential permanently undecryptable.
    """

    def test_the_env_file_carries_a_usable_key(self, tmp_path):
        from istota.secrets_store import _MIN_KEY_LEN

        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env = _env_values(tmp_path / "cfg" / "istota.env")
        assert "ISTOTA_SECRET_KEY" in env
        assert len(env["ISTOTA_SECRET_KEY"]) >= _MIN_KEY_LEN

    def test_the_renderer_emits_whatever_key_it_is_given(self):
        a = Answers(secret_key="k" * 64, session_secret="s" * 64)
        assert "ISTOTA_SECRET_KEY=" + "k" * 64 in render_env_file(a)

    def test_force_rerun_preserves_the_exact_prior_key(self, tmp_path):
        """The boundary. `--force` rewrites `istota.env` wholesale, and a new
        key there orphans every credential already in the secrets table."""
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env_path = tmp_path / "cfg" / "istota.env"
        first = _env_values(env_path)["ISTOTA_SECRET_KEY"]

        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="bob")
        rc, _, _ = _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert _env_values(env_path)["ISTOTA_SECRET_KEY"] == first

    def test_force_rerun_preserves_the_session_secret_too(self, tmp_path):
        """Smaller harm (invalidated cookies, not lost credentials), same rule:
        a re-run is a config rewrite, not a key rotation."""
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env_path = tmp_path / "cfg" / "istota.env"
        first = _env_values(env_path)["ISTOTA_WEB_SESSION_SECRET_KEY"]

        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="bob")
        _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert _env_values(env_path)["ISTOTA_WEB_SESSION_SECRET_KEY"] == first

    def test_a_blank_or_short_prior_key_is_replaced(self, tmp_path):
        """Preservation is for a usable key. An empty or truncated line must
        not be carried forward, or the guard would pin the broken state."""
        from istota.secrets_store import _MIN_KEY_LEN

        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env_path = tmp_path / "cfg" / "istota.env"
        env_path.write_text("ISTOTA_SECRET_KEY=\nISTOTA_WEB_SESSION_SECRET_KEY=short\n")

        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args2, tmp_path, which_result="/usr/bin/claude")
        env = _env_values(env_path)
        assert len(env["ISTOTA_SECRET_KEY"]) >= _MIN_KEY_LEN
        assert len(env["ISTOTA_WEB_SESSION_SECRET_KEY"]) >= _MIN_KEY_LEN

    def test_the_env_file_is_created_private_rather_than_chmodded_after(
        self, tmp_path, monkeypatch
    ):
        """`write_text` then `chmod` leaves the file world-readable with
        secrets in it for the interval between. Asserting on the final mode
        passes against that bug, so this asserts on the mode the file was
        *created* with."""
        import stat

        real_open = os.open
        seen: dict[str, int] = {}

        def recording_open(path, flags, mode=0o777, *a, **kw):
            if str(path).endswith("istota.env"):
                seen["mode"] = mode
                seen["flags"] = flags
            return real_open(path, flags, mode, *a, **kw)

        monkeypatch.setattr(os, "open", recording_open)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")

        assert seen.get("mode") == 0o600, (
            "istota.env was not created through os.open with mode 0600"
        )
        assert seen["flags"] & os.O_CREAT
        env_path = tmp_path / "cfg" / "istota.env"
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    def test_an_existing_wide_env_file_is_narrowed_before_the_secrets_land(
        self, tmp_path, monkeypatch
    ):
        """`O_CREAT`'s mode applies only to a file this call creates, so on the
        `--force` re-run — the one path where it pre-exists — the mode argument
        is ignored and the key would be written at 0644 and narrowed after.
        That is the same window, on the only shape that has it."""
        import stat

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        env_path = cfg_dir / "istota.env"
        prior = "b" * 64
        env_path.write_text(f"ISTOTA_SECRET_KEY={prior}\n")
        env_path.chmod(0o644)

        events: list[tuple] = []
        real_fchmod, real_fdopen = os.fchmod, os.fdopen

        def recording_fchmod(fd, mode):
            events.append(("fchmod", mode))
            return real_fchmod(fd, mode)

        def recording_fdopen(fd, *a, **kw):
            events.append(("fdopen",))
            return real_fdopen(fd, *a, **kw)

        monkeypatch.setattr(os, "fchmod", recording_fchmod)
        monkeypatch.setattr(os, "fdopen", recording_fdopen)
        args = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")

        # The narrowing precedes the handle every byte is written through.
        assert events[:2] == [("fchmod", 0o600), ("fdopen",)]
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        assert _env_values(env_path)["ISTOTA_SECRET_KEY"] == prior


class TestSecretPreservationPrecedence:
    """Which of several candidate values is the one to keep.

    Getting this wrong is not a cosmetic bug: it writes a key the daemon is
    not using into the file, and orphans everything encrypted under the one
    it is.
    """

    def _seed(self, tmp_path, body):
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "istota.env").write_text(body)
        (cfg_dir / "config.toml").write_text("")
        return cfg_dir / "istota.env"

    def test_the_first_of_two_lines_wins(self, tmp_path):
        """`serve.load_env_file` skips a name already in `os.environ`, so the
        *first* line is the one the daemon uses and later ones are dead. A
        last-wins parse here would keep the wrong one — and that file shape is
        exactly what following the doctor remedy against a file that already
        had the line produces."""
        used, dead = "c" * 64, "d" * 64
        env_path = self._seed(
            tmp_path, f"ISTOTA_SECRET_KEY={used}\nISTOTA_SECRET_KEY={dead}\n"
        )
        args = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        assert _env_values(env_path)["ISTOTA_SECRET_KEY"] == used

    def test_an_exported_key_outranks_the_file(self, tmp_path, monkeypatch):
        """`load_env_file` is non-clobbering, so an exported value is the key
        actually in use. Preserving the file's instead would write a value the
        daemon ignores, and the install would switch keys silently the day the
        export went away."""
        exported, in_file = "e" * 64, "f" * 64
        env_path = self._seed(tmp_path, f"ISTOTA_SECRET_KEY={in_file}\n")
        monkeypatch.setenv("ISTOTA_SECRET_KEY", exported)
        args = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        _, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert _env_values(env_path)["ISTOTA_SECRET_KEY"] == exported
        # The losing value is about to be deleted, so the operator is told.
        assert any("ISTOTA_SECRET_KEY differs" in line for line in out)
        # Names only, never values.
        assert not any(exported in line or in_file in line for line in out)

    def test_the_web_token_key_is_carried_forward(self, tmp_path):
        """A second Fernet key with identical loss semantics: `web_tokens.py`
        derives its own from it for the `web_user_tokens` rows. The wizard has
        never written it, so the wholesale rewrite would delete it."""
        token_key = "g" * 64
        env_path = self._seed(tmp_path, f"ISTOTA_WEB_TOKEN_KEY={token_key}\n")
        args = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        assert _env_values(env_path)["ISTOTA_WEB_TOKEN_KEY"] == token_key

    def test_no_web_token_key_is_invented(self, tmp_path):
        """Carried forward, never generated: encrypted token storage is opt-in
        and standalone does not use it."""
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        assert "ISTOTA_WEB_TOKEN_KEY" not in _env_values(
            tmp_path / "cfg" / "istota.env"
        )


class TestAdminsFile:
    """The wizard writes a real authorization artifact.

    Without one `admin_users` is empty, and `is_shared_kv_writer` fails closed,
    so shared briefing blocks could not be written on a standalone install.
    """

    def test_written_and_named_in_the_env_file(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        admins = tmp_path / "cfg" / "admins"
        assert admins.is_file()
        assert "alice" in admins.read_text().split()
        env = _env_values(tmp_path / "cfg" / "istota.env")
        assert env["ISTOTA_ADMINS_FILE"] == str(admins)

    def test_the_admins_route_authorizes_on_its_own(self, tmp_path, monkeypatch):
        """The admins file is the primary route, so it must authorize with the
        standalone exemption unable to account for it: the same `admin_users`
        on a config that is *not* standalone still writes shared content."""
        from istota.config import NextcloudConfig, load_config

        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env = _env_values(tmp_path / "cfg" / "istota.env")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", env["ISTOTA_ADMINS_FILE"])

        cfg = load_config(tmp_path / "cfg" / "config.toml")
        assert cfg.admin_users == {"alice"}
        assert cfg.is_shared_kv_writer("alice") is True
        assert cfg.is_shared_kv_writer("mallory") is False

        # Not standalone, so only the allowlist can be answering.
        cfg.nextcloud = NextcloudConfig(url="https://nextcloud.example.com")
        assert cfg.is_standalone is False
        assert cfg.is_shared_kv_writer("alice") is True

    def test_an_existing_admins_file_naming_the_user_is_left_alone(self, tmp_path):
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        existing = "# hand-edited\nalice\nbob\n"
        (cfg_dir / "admins").write_text(existing)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        assert (cfg_dir / "admins").read_text() == existing
        # Not a vacuous pass: the env file has to point at it either way.
        env = _env_values(cfg_dir / "istota.env")
        assert env["ISTOTA_ADMINS_FILE"] == str(cfg_dir / "admins")

    def test_an_existing_admins_file_is_never_widened(self, tmp_path):
        """Appending would be a silent authorization widening, and the path is
        derived from `config_path.parent` with nothing asserting the standalone
        shape — so `istota setup -c /etc/istota/config.toml --force` would add
        the wizard's user to the server's own production allowlist, which is
        exactly where `load_admin_users` looks by default."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        existing = "# hand-edited\nbob\n"
        (cfg_dir / "admins").write_text(existing)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert (cfg_dir / "admins").read_text() == existing
        # Refusing silently would lock the user out with no way to know why.
        assert any("does not name 'alice'" in line for line in out)

    def test_the_membership_test_is_load_admin_users(self, tmp_path):
        """The writer and the reader of this file must not drift on what a
        line means, so the check asks the reader rather than re-parsing."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "admins").write_text("# owner\n\n  alice  \n")
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert not any("does not name" in line for line in out)


# ---------------------------------------------------------------------------
# Prompt-keyed driver, for everything below
# ---------------------------------------------------------------------------


class _Prompts:
    """Answer prompts by matching their text, and record what was asked.

    ``_run``'s positional list is fine for the branches it was written for and
    is the wrong instrument here: a question added anywhere earlier shifts it,
    and one question (money) is skipped outright on an install without
    ``beancount``, so position is not stable across environments either. Two
    channels, deliberately kept apart — ``input`` and ``getpass`` — so a test
    can assert which one a secret was read through.

    Needles are matched in insertion order, so a more specific one goes first.
    An unmatched prompt gets "", which is the same "take the default" a bare
    Enter gives.
    """

    def __init__(self, answers=None, secrets=None):
        self.answers = dict(answers or {})
        self.secrets = dict(secrets or {})
        self.asked: list[str] = []
        self.asked_secretly: list[str] = []

    def _pick(self, table, prompt):
        for needle, value in table.items():
            if needle in prompt:
                return value
        return ""

    def input(self, prompt):
        self.asked.append(prompt)
        return self._pick(self.answers, prompt)

    def getpass(self, prompt):
        self.asked_secretly.append(prompt)
        return self._pick(self.secrets, prompt)

    def was_asked(self, needle: str) -> bool:
        return any(needle in p for p in self.asked)

    def was_asked_secretly(self, needle: str) -> bool:
        return any(needle in p for p in self.asked_secretly)


def _run_prompted(args, tmp_path, prompts, which_result="/usr/bin/claude"):
    config_path = tmp_path / "cfg" / "config.toml"
    args.config = str(config_path)
    out_lines: list[str] = []
    rc = setup_wizard.run_setup(
        args, input_fn=prompts.input, which_fn=lambda _n: which_result,
        out=out_lines.append, getpass_fn=prompts.getpass,
    )
    return rc, config_path, out_lines


# ---------------------------------------------------------------------------
# Module prompts: health, feeds and briefings had none at all
# ---------------------------------------------------------------------------


@pytest.fixture
def _all_modules_available(monkeypatch):
    """Pin every module available, so a prompt-count assertion is about the code.

    ``_collect_modules`` skips a module whose install extra is missing, and
    ``money`` is the one entry in ``MODULE_DEPENDENCIES``. Without this the
    money prompt is present because `uv sync --extra test` happens to install
    beancount, and the assertion goes red on the lean install shape
    ``modules.module_available`` exists to support — for an environment reason
    rather than a code one.
    """
    from istota import modules
    monkeypatch.setattr(modules, "module_available", lambda _name: True)


class TestModulePrompts:
    def test_every_opt_out_module_is_offered(self, tmp_path, _all_modules_available):
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts()
        rc, _, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        for module in ("money", "health", "feeds", "briefings"):
            assert p.was_asked(module), f"no prompt mentioned {module}"

    def test_the_prompt_list_covers_every_module_but_location(self):
        """The drift this whole stage exists to fix: health, feeds and
        briefings were in ``MODULE_NAMES`` and in no prompt. A sixth module
        added later should fail here rather than repeat it."""
        from istota import modules
        asked = {module for _field, module, _q in setup_wizard._OPT_OUT_MODULES}
        assert asked | {"location"} == set(modules.MODULE_NAMES)

    def test_disabled_modules_is_derived_from_that_one_list(self):
        """One source of truth for the set, so the prompts and the rendered
        list cannot disagree."""
        a = Answers()
        for field_name, _module, _q in setup_wizard._OPT_OUT_MODULES:
            setattr(a, field_name, False)
        assert a.disabled_modules == sorted(
            module for _f, module, _q in setup_wizard._OPT_OUT_MODULES
        )

    def test_declining_three_reaches_both_the_toml_and_the_profile_row(self, tmp_path):
        """``disabled_modules`` has two homes and the DB row is the effective
        one, so a prompt that only reaches the TOML changes nothing at runtime."""
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts({
            "health module": "n",
            "feeds module": "n",
            "briefings module": "n",
            "money module": "y",
        })
        rc, config_path, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0

        toml = config_path.read_text()
        assert 'disabled_modules = ["briefings", "feeds", "health"]' in toml

        from istota import user_profiles
        from istota.config import load_config
        db_path = tmp_path / "ws" / "istota.db"
        prof = user_profiles.get_profile(db_path, "alice")
        assert sorted(prof.disabled_modules) == ["briefings", "feeds", "health"]

        cfg = load_config(config_path)
        for module in ("briefings", "feeds", "health"):
            assert cfg.is_module_enabled("alice", module) is False
        assert cfg.is_module_enabled("alice", "money") is True

    def test_the_default_answer_leaves_every_module_on(self, tmp_path):
        # Bare Enter at each prompt — the opt-out shape.
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run_prompted(args, tmp_path, _Prompts())
        assert rc == 0
        assert "disabled_modules" not in config_path.read_text()

    def test_the_non_interactive_path_has_an_answer_for_each(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert "disabled_modules" not in config_path.read_text()

    def test_the_real_parser_produces_the_attributes_the_wizard_reads(self, monkeypatch):
        """``_collect_modules`` reads ``getattr(args, f"no_{module}", False)``,
        so a wrong or renamed argparse dest is a silent no-op rather than an
        AttributeError — and ``_args``' SimpleNamespace would never notice,
        since the test writes the attribute itself. Drive the real parser."""
        import sys
        from istota import cli, setup_wizard as wiz
        captured: dict = {}

        def fake_run_setup(args, **_kw):
            captured["args"] = args
            return 0

        monkeypatch.setattr(wiz, "run_setup", fake_run_setup)
        monkeypatch.setattr(sys, "argv", [
            "istota", "setup", "--yes",
            "--no-money", "--no-health", "--no-feeds", "--no-briefings",
        ])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        for module in ("money", "health", "feeds", "briefings"):
            assert getattr(captured["args"], f"no_{module}") is True

    @pytest.mark.parametrize(
        "flag,module",
        [("no_health", "health"), ("no_feeds", "feeds"), ("no_briefings", "briefings")],
    )
    def test_each_flag_disables_without_a_prompt(self, tmp_path, flag, module):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice", **{flag: True})
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert f'disabled_modules = ["{module}"]' in config_path.read_text()
        from istota import user_profiles
        prof = user_profiles.get_profile(tmp_path / "ws" / "istota.db", "alice")
        assert prof.disabled_modules == [module]

    def test_an_unavailable_module_is_neither_asked_about_nor_recorded(
        self, tmp_path, monkeypatch,
    ):
        """``module_available()`` already hides it, so the prompt has no good
        answer — and recording it in ``disabled_modules`` would turn a missing
        install extra into a stored decision that outlives installing it."""
        from istota import modules
        monkeypatch.setattr(modules, "module_available", lambda name: name != "money")
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts()
        rc, config_path, out = _run_prompted(args, tmp_path, p)
        assert rc == 0
        assert not p.was_asked("money module")
        assert p.was_asked("health module")
        assert "disabled_modules" not in config_path.read_text()
        assert any("money module's optional dependencies" in line for line in out)

    def test_an_explicit_flag_still_wins_over_unavailability(self, tmp_path, monkeypatch):
        """A flag is a decision the operator made; unavailability is one derived
        from the environment, so the flag is honoured either way."""
        from istota import modules
        monkeypatch.setattr(modules, "module_available", lambda name: name != "money")
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice", no_money=True)
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert 'disabled_modules = ["money"]' in config_path.read_text()

    def test_location_stays_out_of_disabled_modules(self, tmp_path):
        """It is gated by ``[location] enabled``; two homes for one answer can
        disagree."""
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run_prompted(args, tmp_path, _Prompts({"location": "n"}))
        assert rc == 0
        toml = config_path.read_text()
        assert "[location]\nenabled = false" in toml
        assert "disabled_modules" not in toml


# ---------------------------------------------------------------------------
# [caldav] — the section written for this shape that no generator emitted
# ---------------------------------------------------------------------------


class TestCaldavRenderer:
    def test_absent_by_default(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="alice")
        assert "[caldav]" not in render_config_toml(a)

    def test_rendered_when_a_url_is_given(self, tmp_path):
        a = Answers(
            workspace=tmp_path / "ws", user_id="alice",
            caldav_url="https://caldav.example.com/dav",
            caldav_username="alice@example.com",
            caldav_password="hunter2",
        )
        toml = render_config_toml(a)
        assert "[caldav]" in toml
        assert 'url = "https://caldav.example.com/dav"' in toml
        assert 'username = "alice@example.com"' in toml
        # The password is a credential, so it goes to istota.env through
        # `ISTOTA_CALDAV_PASSWORD` like every other one. The block names where.
        assert "hunter2" not in toml
        assert "ISTOTA_CALDAV_PASSWORD" in toml

    def test_it_parses_back_into_the_caldav_properties(self, tmp_path, monkeypatch):
        """The two halves rejoin at `load_config`: url and username off the
        TOML, password off `ISTOTA_CALDAV_PASSWORD` through the
        `_env_secret_overrides` table. `istota serve` sources istota.env, so
        this is the state the daemon actually runs in."""
        a = Answers(
            workspace=tmp_path / "ws", user_id="alice",
            caldav_url="https://caldav.example.com/dav/",
            caldav_username="alice@example.com",
            caldav_password="hunter2",
        )
        p = tmp_path / "config.toml"
        p.write_text(render_config_toml(a))
        assert "ISTOTA_CALDAV_PASSWORD=hunter2" in render_env_file(a)
        monkeypatch.setenv("ISTOTA_CALDAV_PASSWORD", "hunter2")
        from istota.config import load_config
        cfg = load_config(p)
        assert cfg.caldav.url == "https://caldav.example.com/dav/"
        assert cfg.caldav_url == "https://caldav.example.com/dav"
        assert cfg.caldav_username == "alice@example.com"
        assert cfg.caldav_password == "hunter2"

    @pytest.mark.parametrize(
        "value",
        ["ab\x1bc", "pa\x7fss", "nul\x00here", "tab\there", "back\\slash", 'qu"ote'],
    )
    def test_a_pasted_control_character_still_parses(self, tmp_path, value):
        """The config file must stay parseable whatever was pasted, and moving
        the password out of it does not settle that on its own — the *username*
        is the same paste and still lands in a TOML basic string, which forbids
        a raw control character. An unparseable file here raises out of
        `_bootstrap`'s `load_config`, after the config, env file, admins file
        and database have all been written."""
        import tomllib
        a = Answers(
            workspace=tmp_path / "ws", user_id="alice",
            caldav_url="https://caldav.example.com/dav",
            caldav_username=value,
            caldav_password=value,
        )
        parsed = tomllib.loads(render_config_toml(a))
        assert parsed["caldav"]["username"] == value
        # And the password reaches the env file whole, on the one line that
        # carries it. A newline is the shape that would split it into a second
        # KEY=VALUE line, so it is asserted rather than assumed.
        rendered = render_env_file(a)
        assert f"ISTOTA_CALDAV_PASSWORD={value}" in rendered.splitlines()

    def test_the_header_keeps_claiming_no_secrets_live_here(self, tmp_path):
        """The generated header says secrets live in istota.env and never in
        this file. That was true of every credential except the CalDAV
        password, which had no `_env_secret_overrides` row and so nowhere else
        to go; adding the row is what makes the sentence unconditional again.

        Asserted on both branches, because the earlier version of this weakened
        the header when the block was present — and a header with an exception
        in it is one a reader stops trusting."""
        without = render_config_toml(Answers(workspace=tmp_path / "ws"))
        with_block = render_config_toml(
            Answers(
                workspace=tmp_path / "ws", caldav_url="https://caldav.example.com",
                caldav_username="alice@example.com", caldav_password="hunter2",
            )
        )
        assert "never here" in without
        assert "never here" in with_block
        assert "hunter2" not in with_block


class TestCaldavCollection:
    def _prompts(self):
        return _Prompts(
            {
                "external CalDAV": "y",
                "CalDAV URL": "https://caldav.example.com/dav",
                "CalDAV username": "alice@example.com",
            },
            secrets={"CalDAV password": "hunter2"},
        )

    def test_end_to_end(self, tmp_path):
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run_prompted(args, tmp_path, self._prompts())
        assert rc == 0
        from istota.config import load_config
        cfg = load_config(config_path)
        assert cfg.caldav_url == "https://caldav.example.com/dav"
        assert cfg.caldav_username == "alice@example.com"
        assert _caldav_password_on_disk(config_path) == "hunter2"

    def test_the_password_is_read_without_echo(self, tmp_path):
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = self._prompts()
        rc, _, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        assert p.was_asked_secretly("CalDAV password")
        assert not p.was_asked("CalDAV password")

    def test_the_password_goes_to_the_env_file_and_not_the_config(self, tmp_path):
        """The placement, asserted from both ends.

        This used to be the other way round — the password in `config.toml`,
        with that file narrowed to 0600 to compensate — because
        `Config.caldav_password` read only the TOML field. Adding the
        `_env_secret_overrides` row moved it onto the same channel every other
        credential uses, which is what let the config file go back to the umask
        default and the mode exception disappear.

        Both halves are asserted because either alone passes in a broken state:
        absent from the TOML is equally true of a password that reached
        nowhere, and present in istota.env is equally true of one written to
        both."""
        import stat
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run_prompted(args, tmp_path, self._prompts())
        assert rc == 0

        assert "hunter2" not in config_path.read_text()
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_CALDAV_PASSWORD=hunter2" in env.splitlines()
        assert stat.S_IMODE((config_path.parent / "istota.env").stat().st_mode) == 0o600

    def test_a_config_with_no_caldav_password_is_not_narrowed(self, tmp_path):
        """The private write is scoped to the shape that needs it: a config
        holding no secret keeps the umask default, so an operator can still
        read it as they always could. Compared against a control file rather
        than against 0644, since the umask is the host's to choose."""
        import stat
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        control = tmp_path / "control.txt"
        control.write_text("x")
        assert (
            stat.S_IMODE(config_path.stat().st_mode)
            == stat.S_IMODE(control.stat().st_mode)
        )

    def test_a_blank_url_leaves_the_block_out_and_asks_for_nothing_more(self, tmp_path):
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts({"external CalDAV": "y", "CalDAV URL": ""})
        rc, config_path, out = _run_prompted(args, tmp_path, p)
        assert rc == 0
        assert "[caldav]" not in config_path.read_text()
        assert not p.was_asked_secretly("CalDAV password")
        assert any("No CalDAV URL given" in line for line in out)

    def test_declined_by_default_and_absent_non_interactively(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert "[caldav]" not in config_path.read_text()

    def test_a_url_with_no_password_is_not_written(self, tmp_path):
        """It would override the [nextcloud] derivation with something that
        cannot authenticate — worse than leaving the section out."""
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts(
            {"external CalDAV": "y", "CalDAV URL": "https://caldav.example.com/dav"},
            secrets={},  # the password prompt gives up empty
        )
        rc, config_path, out = _run_prompted(args, tmp_path, p)
        assert rc == 0
        assert "[caldav]" not in config_path.read_text()
        assert any("No CalDAV password given" in line for line in out)


def _caldav_password_on_disk(config_path):
    """What the daemon would resolve, from the two files the wizard wrote.

    `load_config` alone is not the answer any more: the url and username come
    off `config.toml` while the password arrives through
    `ISTOTA_CALDAV_PASSWORD`, which `istota serve` sources from the sibling
    `istota.env`. A test asserting on `load_config(config_path)` in a process
    that never sourced that file reads a blank password on a correct install.
    """
    values = setup_wizard._read_env_values(config_path.parent / "istota.env")
    return values.get("ISTOTA_CALDAV_PASSWORD", "")


class TestCaldavSurvivesARerun:
    """``--force`` rewrites config.toml wholesale, and it is the only home this
    password has — so a re-run that collects nothing must not delete it."""

    def _first_run(self, tmp_path):
        args = _args(workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts(
            {
                "external CalDAV": "y",
                "CalDAV URL": "https://caldav.example.com/dav",
                "CalDAV username": "alice@example.com",
            },
            secrets={"CalDAV password": "hunter2"},
        )
        rc, config_path, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        return config_path

    def test_read_existing_caldav_reads_the_block_back(self, tmp_path):
        config_path = self._first_run(tmp_path)
        assert setup_wizard.read_existing_caldav(
            config_path, config_path.parent / "istota.env"
        ) == {
            "url": "https://caldav.example.com/dav",
            "username": "alice@example.com",
            "password": "hunter2",
        }
        # Without the env file it recovers the server but not the credential,
        # which is the state `_collect_caldav` must not write a block from.
        assert setup_wizard.read_existing_caldav(config_path)["password"] == ""

    def test_read_existing_caldav_never_raises(self, tmp_path):
        assert setup_wizard.read_existing_caldav(tmp_path / "nope.toml") == {}
        broken = tmp_path / "broken.toml"
        broken.write_text("this is not = = toml")
        assert setup_wizard.read_existing_caldav(broken) == {}
        no_block = tmp_path / "plain.toml"
        no_block.write_text('bot_name = "Istota"\n')
        assert setup_wizard.read_existing_caldav(no_block) == {}

    def test_a_non_interactive_force_rerun_keeps_it(self, tmp_path):
        """`--yes` asks nothing and there is no flag to re-supply it, so the
        alternative is deleting a credential with no copy anywhere."""
        config_path = self._first_run(tmp_path)
        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path2, _ = _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert config_path2 == config_path
        from istota.config import load_config
        assert load_config(config_path).caldav_url == "https://caldav.example.com/dav"
        assert _caldav_password_on_disk(config_path) == "hunter2"

    def test_an_interactive_rerun_offers_to_keep_it(self, tmp_path):
        config_path = self._first_run(tmp_path)
        args2 = _args(force=True, workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts()  # bare Enter everywhere; the keep prompt defaults to yes
        rc, _, _ = _run_prompted(args2, tmp_path, p)
        assert rc == 0
        assert p.was_asked("Keep the configured CalDAV server")
        # And it did not ask to set one up from scratch.
        assert not p.was_asked("Point calendar at an external")
        assert _caldav_password_on_disk(config_path) == "hunter2"

    def test_declining_the_keep_falls_through_to_a_fresh_setup(self, tmp_path):
        """Otherwise the block would be preserved but not editable."""
        config_path = self._first_run(tmp_path)
        args2 = _args(force=True, workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts(
            {
                "Keep the configured CalDAV server": "n",
                "external CalDAV": "y",
                "CalDAV URL": "https://dav2.example.com/dav",
                "CalDAV username": "bob@example.com",
            },
            secrets={"CalDAV password": "correcthorse"},
        )
        rc, _, _ = _run_prompted(args2, tmp_path, p)
        assert rc == 0
        from istota.config import load_config
        assert load_config(config_path).caldav_url == "https://dav2.example.com/dav"
        assert _caldav_password_on_disk(config_path) == "correcthorse"

    def test_declining_both_drops_it_and_says_so(self, tmp_path):
        config_path = self._first_run(tmp_path)
        args2 = _args(force=True, workspace=str(tmp_path / "ws"), user="alice")
        p = _Prompts({
            "Keep the configured CalDAV server": "n",
            "external CalDAV": "n",
        })
        rc, _, out = _run_prompted(args2, tmp_path, p)
        assert rc == 0
        assert "[caldav]" not in config_path.read_text()
        # A silent deletion of a credential is the thing being avoided.
        assert any("Dropping the existing [caldav] block" in line for line in out)


# ---------------------------------------------------------------------------
# Email: no-echo password, and a real smtp_host question
# ---------------------------------------------------------------------------


class TestEmailPrompts:
    def _args_and_prompts(self, tmp_path, smtp="smtp.example.com"):
        args = _args(workspace=str(tmp_path / "ws"), user="alice", email=True)
        p = _Prompts(
            {
                "IMAP host": "imap.example.com",
                "IMAP user": "alice@example.com",
                "SMTP host": smtp,
            },
            secrets={"IMAP password": "hunter2"},
        )
        return args, p

    def test_smtp_host_can_differ_from_the_imap_host(self, tmp_path):
        args, p = self._args_and_prompts(tmp_path)
        rc, config_path, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        from istota.config import load_config
        cfg = load_config(config_path)
        assert cfg.email.imap_host == "imap.example.com"
        assert cfg.email.smtp_host == "smtp.example.com"

    def test_it_defaults_to_the_imap_host(self, tmp_path):
        # Bare Enter at the SMTP prompt — the common case stays one keystroke.
        args, p = self._args_and_prompts(tmp_path, smtp="")
        rc, config_path, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        from istota.config import load_config
        cfg = load_config(config_path)
        assert cfg.email.smtp_host == "imap.example.com"

    def test_the_password_goes_through_the_no_echo_path(self, tmp_path):
        """It used to be read with ``input_fn``: echoed to the terminal, and
        left in shell history when the wizard is driven from a pipe."""
        args, p = self._args_and_prompts(tmp_path)
        rc, config_path, _ = _run_prompted(args, tmp_path, p)
        assert rc == 0
        assert p.was_asked_secretly("IMAP password")
        assert not p.was_asked("IMAP password")
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_EMAIL_IMAP_PASSWORD=hunter2" in env
        assert "ISTOTA_EMAIL_SMTP_PASSWORD=hunter2" in env
        assert "hunter2" not in config_path.read_text()

    def test_the_non_interactive_path_still_fills_smtp_host(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice", email=True)
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        from istota.config import load_config
        cfg = load_config(config_path)
        assert cfg.email.smtp_host == cfg.email.imap_host


# ---------------------------------------------------------------------------
# The two directories config.toml names and nothing created
# ---------------------------------------------------------------------------


class TestDirectories:
    def test_both_exist_after_a_run(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        from istota.config import load_config
        cfg = load_config(config_path)
        # Read the paths back off the config rather than recomputing them, so a
        # directory made somewhere other than where the file points fails here.
        assert Path(cfg.temp_dir).is_dir()
        assert Path(cfg.scheduler.db_backup_dir).is_dir()

    def test_they_are_private(self, tmp_path):
        """``temp_dir`` is the parent of every task's ``.control`` tree and
        holds prepared attachments; ``db_backup_dir`` holds whole database
        snapshots."""
        import stat
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        for name in ("tmp", "db-backups"):
            mode = stat.S_IMODE((tmp_path / "ws" / name).stat().st_mode)
            assert mode == 0o700, f"{name} is {oct(mode)}"

    def test_a_rerun_is_idempotent(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        args2 = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice", force=True)
        rc, _, _ = _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert (tmp_path / "ws" / "db-backups").is_dir()


# ---------------------------------------------------------------------------
# The closing self-check
# ---------------------------------------------------------------------------


class TestSelfCheck:
    def test_it_runs_and_reports(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert any(line.startswith("Self-check (") for line in out)

    def test_it_names_the_config_it_just_wrote(self, tmp_path, monkeypatch):
        """``config_visibility`` is the gate that stops a run with no config
        resolved from reporting on a default ``Config`` while reading exactly
        like a run about this deployment, so the path has to be explicit."""
        from istota import doctor
        seen: dict = {}
        real_gate = doctor.config_visibility

        def spy_gate(config, requested=None, scope=""):
            seen["requested"] = requested
            seen["config_path"] = config.config_path
            return real_gate(config, requested=requested, scope=scope)

        monkeypatch.setattr(doctor, "config_visibility", spy_gate)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert seen["requested"] == config_path
        # And the config really did load, so the gate opened rather than firing.
        assert Path(seen["config_path"]) == config_path

    def test_it_does_not_spawn(self, tmp_path, monkeypatch):
        """A wizard that hangs ten seconds per binary probe, with the operator
        sitting at a prompt, is worse than one that checked less."""
        from istota import doctor
        calls: list[dict] = []
        real = doctor.run_checks

        def spy(config, **kw):
            calls.append(kw)
            return real(config, **kw)

        monkeypatch.setattr(doctor, "run_checks", spy)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        # `load_config` runs a narrowed registry pass of its own on every load,
        # so select the full-registry run rather than reading the last call.
        full = [kw for kw in calls if not kw.get("only")]
        assert len(full) == 1
        assert full[0]["probe"] is False
        assert full[0].get("deep", False) is False
        assert full[0].get("live", False) is False

    def test_a_failing_check_is_printed_and_does_not_fail_the_install(
        self, tmp_path, monkeypatch,
    ):
        from istota import doctor
        monkeypatch.setattr(
            doctor, "run_checks",
            lambda config, **kw: [
                doctor.CheckResult(
                    "runtime.invented", doctor.FAIL, "a contrived failure",
                    remedy="do the thing",
                ),
                doctor.CheckResult("runtime.fine", doctor.OK, "all good"),
            ],
        )
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        # Setup succeeded: the files are written and the DB is initialized.
        assert rc == 0
        assert config_path.exists()
        text = "\n".join(out)
        assert "runtime.invented" in text
        assert "a contrived failure" in text
        assert "do the thing" in text
        assert "Setup itself succeeded" in text
        # Only the failures are printed; a passing check is not news here.
        assert "runtime.fine" not in text

    def test_the_gate_short_circuits_the_registry(self, tmp_path, monkeypatch):
        from istota import doctor
        gate = doctor.CheckResult(
            "config.visibility", doctor.FAIL, "nothing resolved", remedy="pass -c",
        )
        calls: list[dict] = []

        def spy(config, **kw):
            calls.append(kw)
            return []

        monkeypatch.setattr(doctor, "config_visibility", lambda *a, **kw: gate)
        monkeypatch.setattr(doctor, "run_checks", spy)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        # `load_config`'s own narrowed pass still runs; a full-registry one
        # would mean the gate was ignored.
        assert [kw for kw in calls if not kw.get("only")] == []
        assert "nothing resolved" in "\n".join(out)

    def test_a_raising_check_run_does_not_break_setup(self, tmp_path, monkeypatch):
        from istota import doctor

        def boom(*a, **kw):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(doctor, "run_checks", boom)
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        text = "\n".join(out)
        assert "Self-check could not run" in text
        assert "kaboom" in text

    def test_it_runs_before_the_next_steps_block(self, tmp_path):
        """Order matters in a terminal: the thing needing action must not
        scroll off above the thing telling you how to start."""
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        check_at = next(i for i, ln in enumerate(out) if ln.startswith("Self-check ("))
        done_at = next(i for i, ln in enumerate(out) if ln == "Setup complete.")
        assert check_at < done_at

    def test_a_warning_count_is_never_left_with_nothing_behind_it(
        self, tmp_path, monkeypatch,
    ):
        """Only failures are printed, so a bare warn count in the summary is a
        number the operator cannot act on."""
        from istota import doctor
        monkeypatch.setattr(
            doctor, "run_checks",
            lambda config, **kw: [
                doctor.CheckResult("runtime.a", doctor.WARN, "something to know"),
                doctor.CheckResult("runtime.b", doctor.OK, "fine"),
            ],
        )
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        text = "\n".join(out)
        assert "no failures, 1 warning." in text
        assert f"istota -c {config_path} doctor" in text

    def test_every_command_the_self_check_prints_actually_parses(
        self, tmp_path, monkeypatch,
    ):
        """The hint was `istota doctor -c PATH`, which argparse rejects.

        `-c` is a *global* option on this CLI, declared on the top-level parser
        before `add_subparsers`, so it has to precede the subcommand; after it,
        argparse reports a usage error and exits 2. Every one of the three
        places the self-check offers a follow-up printed it the wrong way
        round, so an operator copying the line — which is the entire point of
        printing it — got an error instead of a report.

        Nothing caught it because the assertion above compared the string
        against itself: the test was written from the same wrong line as the
        code. So this runs the real CLI rather than matching text. The config
        path does not exist on purpose, which makes it cheap — `config_visibility`
        refuses at the gate and no check runs — while still proving the argv
        parses, since an unparseable one never reaches the gate at all.
        """
        import re as _re
        import shlex
        import shutil
        import subprocess

        from istota import doctor
        monkeypatch.setattr(
            doctor, "run_checks",
            lambda config, **kw: [
                doctor.CheckResult("runtime.a", doctor.WARN, "something to know"),
            ],
        )
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, config_path, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0

        # Every `istota …` line, then narrowed to the doctor hints. Both steps
        # are load-bearing and each was learned the hard way.
        #
        # Anchoring the scan on `istota -c` instead would find nothing under
        # the defect and fail on "printed no command" — which is equally what a
        # self-check that stopped printing anything looks like, so the guard
        # would be catching this by accident of its own pattern rather than by
        # testing the argv.
        #
        # And the run is scoped to `doctor` because this executes what it
        # scrapes: `_print_next_steps` also prints `istota serve`, and the
        # first version of this test started a real server and sat there until
        # the 120-second timeout. Only read-only diagnostics are ever run.
        commands = {
            match.group(1).strip(" .`")
            for line in out
            for match in [_re.search(r"(istota .*?)(?:$|`)", line)]
            if match
        }
        printed = {c for c in commands if "doctor" in shlex.split(c)}
        assert printed, (
            f"the self-check printed no doctor command to check; saw {sorted(commands)}"
        )

        # The console script, not `python -m istota`: the package ships no
        # `__main__.py`, so `-m` exits 1 with "cannot be directly executed"
        # before argparse is reached — identically for a correct argv and a
        # broken one. Written that way first, this test passed against the very
        # defect it was added for.
        binary = shutil.which("istota")
        if binary is None:
            pytest.skip("istota console script is not on PATH in this environment")

        for command in printed:
            argv = shlex.split(command)
            assert argv[0] == "istota"
            result = subprocess.run(
                # `--only` bounds the work: the argv has to parse, and the 31
                # checks behind it are covered by their own tests.
                [binary, *argv[1:], "--only", "runtime.platform"],
                capture_output=True, text=True, timeout=120,
            )
            # 2 is argparse's usage error. Any other status means the argv was
            # understood, which is the whole property.
            assert result.returncode != 2, (
                f"`{command}` does not parse:\n{result.stderr[-600:]}"
            )

    def test_the_failure_path_redacts(self, tmp_path, monkeypatch):
        """An exception out of a check can carry a config value in its message,
        and terminal output is where a pasted credential ends up in a report.

        The value is over `doctor._MIN_SECRET_LEN`; below that `_redact`
        deliberately leaves it, since a short string over-matches.
        """
        from istota import doctor
        secret = "s3cret-app-password"

        def boom(*a, **kw):
            raise RuntimeError(f"connecting as https://alice:{secret}@dav.example.com")

        # `load_config` runs its own narrowed registry pass, so this raises
        # there first; that one is caught and logged by `_validate_forge_clis`,
        # and the self-check's own call is the one under test.
        monkeypatch.setattr(doctor, "run_checks", boom)
        monkeypatch.setattr(doctor, "config_secrets", lambda _c: [secret])
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="alice")
        rc, _, out = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        text = "\n".join(out)
        assert "Self-check could not run" in text
        assert secret not in text
        assert "dav.example.com" in text  # the rest of the message survives


# ---------------------------------------------------------------------------
# The prompt helpers themselves
# ---------------------------------------------------------------------------


class TestAskYesNo:
    def test_empty_takes_the_default(self):
        assert setup_wizard._ask_yes_no(lambda _p: "", "Q?", True) is True
        assert setup_wizard._ask_yes_no(lambda _p: "", "Q?", False) is False

    def test_an_unrecognised_answer_re_asks_rather_than_reading_as_no(self):
        """It used to be `raw in ("y", "yes")`, so on a default-Yes prompt a
        typo flipped *away* from the `[Y/n]` it had just printed and disabled a
        module durably."""
        answers = iter(["yeah", "1", "y"])
        seen: list[str] = []
        said: list[str] = []

        def ask(prompt):
            seen.append(prompt)
            return next(answers)

        assert setup_wizard._ask_yes_no(ask, "Q?", True, out=said.append) is True
        assert len(seen) == 3
        assert any("answer y or n" in line for line in said)

    def test_it_is_bounded_and_falls_back_to_the_default(self):
        """A non-interactive input_fn returning the same bad value forever must
        not make the wizard unexitable."""
        calls: list[str] = []

        def ask(prompt):
            calls.append(prompt)
            return "maybe"

        assert setup_wizard._ask_yes_no(ask, "Q?", True) is True
        assert len(calls) == 3


class TestReadSecret:
    def test_ctrl_c_aborts_rather_than_returning_empty(self):
        """It used to be swallowed, so the wizard carried on and wrote a whole
        install around a blank credential. `cli.cmd_setup` catches this and
        exits 1 with "Setup cancelled"."""
        def interrupt(_prompt):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            setup_wizard._read_secret(interrupt, "API key", lambda _l: None)

    def test_eof_still_gives_up_quietly(self):
        """A closed stdin is "there is no more input", which is the give-up
        case the retry exists for — a different thing from Ctrl-C."""
        def eof(_prompt):
            raise EOFError

        assert setup_wizard._read_secret(eof, "API key", lambda _l: None) == ""

    def test_ctrl_c_at_the_imap_prompt_aborts_the_whole_wizard(self, tmp_path):
        """The regression this guards: before the no-echo change the IMAP
        password came from a plain `input()`, so Ctrl-C propagated."""
        args = _args(workspace=str(tmp_path / "ws"), user="alice", email=True)
        args.config = str(tmp_path / "cfg" / "config.toml")

        def getpass_fn(prompt):
            if "IMAP password" in prompt:
                raise KeyboardInterrupt
            return "x"

        with pytest.raises(KeyboardInterrupt):
            setup_wizard.run_setup(
                args, input_fn=lambda _p: "", which_fn=lambda _n: "/usr/bin/claude",
                out=lambda _l: None, getpass_fn=getpass_fn,
            )
