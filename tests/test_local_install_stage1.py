"""Stage 1 tests for the local single-user install: config + no-auth web mode.

Covers the new ``[web] auth`` field + ``ISTOTA_WEB_AUTH`` override, the
``[caldav]`` explicit-override fields, ``Config.is_standalone`` /
``local_user_id``, the web-app no-auth branches + loopback guard, and the
``/api/admin/stats`` standalone ``runtime`` block.
"""


import json

import pytest

from istota.config import (
    CaldavConfig,
    Config,
    NextcloudConfig,
    SiteConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# Config: [web] auth + ISTOTA_WEB_AUTH override
# ---------------------------------------------------------------------------


class TestWebAuthConfig:
    def test_auth_defaults_to_nextcloud(self):
        assert WebConfig().auth == "nextcloud"
        assert Config().web.auth == "nextcloud"

    def test_parse_auth_none(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[web]\nauth = "none"\n')
        cfg = load_config(p)
        assert cfg.web.auth == "none"

    def test_parse_auth_invalid_falls_back(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[web]\nauth = "wide-open"\n')
        cfg = load_config(p)
        assert cfg.web.auth == "nextcloud"

    def test_port_default_unchanged(self):
        assert WebConfig().port == 8766

    def test_env_override_sets_none(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text("")
        monkeypatch.setenv("ISTOTA_WEB_AUTH", "none")
        cfg = load_config(p)
        assert cfg.web.auth == "none"

    def test_env_override_wins_over_toml(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text('[web]\nauth = "nextcloud"\n')
        monkeypatch.setenv("ISTOTA_WEB_AUTH", "none")
        cfg = load_config(p)
        assert cfg.web.auth == "none"

    def test_env_override_invalid_ignored(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text('[web]\nauth = "none"\n')
        monkeypatch.setenv("ISTOTA_WEB_AUTH", "bogus")
        cfg = load_config(p)
        assert cfg.web.auth == "none"  # TOML value retained


# ---------------------------------------------------------------------------
# Config: [caldav] explicit override
# ---------------------------------------------------------------------------


class TestCaldavConfig:
    def test_defaults_blank(self):
        cd = CaldavConfig()
        assert cd.url == "" and cd.username == "" and cd.password == ""

    def test_nc_derivation_when_caldav_blank(self):
        cfg = Config(nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="ncuser",
            app_password="ncpass",
        ))
        assert cfg.caldav_url == "https://cloud.example.com/remote.php/dav"
        assert cfg.caldav_username == "ncuser"
        assert cfg.caldav_password == "ncpass"

    def test_explicit_caldav_overrides_nc(self):
        cfg = Config(
            nextcloud=NextcloudConfig(
                url="https://cloud.example.com",
                username="ncuser",
                app_password="ncpass",
            ),
            caldav=CaldavConfig(
                url="https://radicale.example.com/dav/",
                username="me",
                password="secret",
            ),
        )
        assert cfg.caldav_url == "https://radicale.example.com/dav"  # trailing / stripped
        assert cfg.caldav_username == "me"
        assert cfg.caldav_password == "secret"

    def test_caldav_empty_with_no_nc_yields_blank_url(self):
        cfg = Config()
        assert cfg.caldav_url == ""

    def test_parse_caldav_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            "[caldav]\n"
            'url = "https://dav.fastmail.com"\n'
            'username = "u@fastmail.com"\n'
            'password = "app-pw"\n'
        )
        cfg = load_config(p)
        assert cfg.caldav.url == "https://dav.fastmail.com"
        assert cfg.caldav_username == "u@fastmail.com"
        assert cfg.caldav_password == "app-pw"

    def test_the_password_can_come_from_the_environment(self, tmp_path, monkeypatch):
        """`[caldav]` is the section written for the shape with no Nextcloud,
        so its password is that shape's calendar credential — and it was the
        one credential in the tree with no way into the process except the
        config file. Every other one has an `_env_secret_overrides` row, which
        is what `EnvironmentFile=` on a server, `secrets.env` under Ansible and
        the sibling `istota.env` on a standalone install all resolve through.

        Without the row the wizard had to write the password into
        `config.toml`, under a generated header whose second line says secrets
        live in `istota.env` and never here.
        """
        p = tmp_path / "config.toml"
        p.write_text(
            "[caldav]\n"
            'url = "https://dav.fastmail.com"\n'
            'username = "u@fastmail.com"\n'
        )
        monkeypatch.setenv("ISTOTA_CALDAV_PASSWORD", "from-the-environment")
        assert load_config(p).caldav_password == "from-the-environment"

    def test_the_environment_wins_over_a_password_in_the_file(
        self, tmp_path, monkeypatch
    ):
        """Same precedence as every other row in that table: the environment
        is where a deployment puts the value it does not want on disk, so a
        stale line left in the file must not outrank it."""
        p = tmp_path / "config.toml"
        p.write_text('[caldav]\nurl = "https://dav.fastmail.com"\npassword = "stale"\n')
        monkeypatch.setenv("ISTOTA_CALDAV_PASSWORD", "current")
        assert load_config(p).caldav_password == "current"

    def test_an_empty_environment_value_leaves_the_file_alone(
        self, tmp_path, monkeypatch
    ):
        """The override is `if val:`, so an exported-but-empty variable — what
        an `.env` line with nothing after the `=` produces — is a pass-through
        rather than a way to blank a working credential."""
        p = tmp_path / "config.toml"
        p.write_text('[caldav]\nurl = "https://dav.fastmail.com"\npassword = "in-file"\n')
        monkeypatch.setenv("ISTOTA_CALDAV_PASSWORD", "")
        assert load_config(p).caldav_password == "in-file"

    def test_the_nextcloud_derivation_still_wins_nothing_it_should_not(
        self, tmp_path, monkeypatch
    ):
        """Control for the arms above. `caldav_password` falls back to the
        Nextcloud app password, so the override must reach the `[caldav]`
        field rather than the fallback — otherwise a deployment with both
        would read the wrong credential and only find out at the first sync.
        """
        p = tmp_path / "config.toml"
        p.write_text(
            "[nextcloud]\n"
            'url = "https://cloud.example.com"\n'
            'username = "ncuser"\n'
            'app_password = "ncpass"\n'
        )
        monkeypatch.setenv("ISTOTA_CALDAV_PASSWORD", "caldav-only")
        cfg = load_config(p)
        assert cfg.caldav.password == "caldav-only"
        assert cfg.nextcloud.app_password == "ncpass"

    def test_the_password_is_redacted_from_the_admin_config_view(self):
        """The other half the `[talk.signaling]` docstring names: a credential
        needs an `_env_secret_overrides` entry *and* an `admin_config_view`
        redaction. `password` is caught by the name patterns rather than by a
        path entry, so this is a control that it really is caught rather than
        a restatement of the list."""
        from istota.admin_config_view import build_config_view

        cfg = Config(caldav=CaldavConfig(
            url="https://dav.fastmail.com", username="u", password="app-pw",
        ))
        assert "app-pw" not in json.dumps(build_config_view(cfg), default=str)


# ---------------------------------------------------------------------------
# Config: is_standalone / local_user_id
# ---------------------------------------------------------------------------


class TestStandaloneHelpers:
    def test_is_standalone_true(self):
        cfg = Config(web=WebConfig(auth="none"))
        assert cfg.is_standalone is True

    def test_is_standalone_false_with_nc_url(self):
        cfg = Config(
            web=WebConfig(auth="none"),
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
        )
        assert cfg.is_standalone is False

    def test_is_standalone_false_with_nextcloud_auth(self):
        cfg = Config(web=WebConfig(auth="nextcloud"))
        assert cfg.is_standalone is False

    def test_local_user_id_single_user(self):
        cfg = Config(users={"alice": UserConfig()})
        assert cfg.local_user_id == "alice"

    def test_local_user_id_no_users_falls_back(self):
        assert Config().local_user_id == "local"

    def test_local_user_id_admin_fallback(self):
        cfg = Config(admin_users={"root"})
        assert cfg.local_user_id == "root"


# ---------------------------------------------------------------------------
# Config: emissaries parsing (field default + explicit override)
# ---------------------------------------------------------------------------


class TestEmissariesConfig:
    def test_field_defaults_true(self):
        assert Config().emissaries_enabled is True

    def test_explicit_false_parsed(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("emissaries_enabled = false\n")
        cfg = load_config(p)
        assert cfg.emissaries_enabled is False

    def test_explicit_true_parsed(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("emissaries_enabled = true\n")
        cfg = load_config(p)
        assert cfg.emissaries_enabled is True


# ---------------------------------------------------------------------------
# Web app: no-auth mode + loopback guard
# ---------------------------------------------------------------------------

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient


def _standalone_config(tmp_path):
    # Mirrors the lean local defaults: no Nextcloud, Talk + email off, no-auth.
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "workspace",
        users={"alice": UserConfig(display_name="Alice")},
        talk=TalkConfig(enabled=False),
        web=WebConfig(enabled=True, port=8766, auth="none"),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    return mod.app


class TestLoopbackGuard:
    def test_guard_noop_for_nextcloud_auth(self):
        from istota.web_app import assert_no_auth_bind_safe
        # Must not raise even on a public host when auth is on.
        assert_no_auth_bind_safe("nextcloud", "0.0.0.0")

    def test_guard_allows_loopback(self):
        from istota.web_app import assert_no_auth_bind_safe
        for host in ("127.0.0.1", "::1", "localhost"):
            assert_no_auth_bind_safe("none", host)

    def test_guard_refuses_non_loopback(self):
        from istota.web_app import assert_no_auth_bind_safe
        with pytest.raises(RuntimeError):
            assert_no_auth_bind_safe("none", "0.0.0.0")
        with pytest.raises(RuntimeError):
            assert_no_auth_bind_safe("none", "192.168.1.10")


@_needs_web_deps
class TestNoAuthWebMode:
    @pytest.fixture
    def app(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        return _patch_app(cfg)

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c

    async def test_api_me_no_session_returns_local_admin(self, client):
        resp = await client.get("/istota/api/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "alice"
        assert body["is_admin"] is True
        assert body["display_name"] == "Alice"

    async def test_verify_origin_noop_without_origin_header(self, tmp_path):
        # In no-auth mode _verify_origin is a no-op even with no Origin header
        # (which would normally raise a 403 "missing origin").
        import istota.web_app as mod
        mod._config = _standalone_config(tmp_path)

        class _FakeReq:
            headers: dict = {}

        # Must not raise.
        assert mod._verify_origin(_FakeReq()) is None

    async def test_require_api_auth_returns_local_user_directly(self, tmp_path):
        import istota.web_app as mod
        cfg = _standalone_config(tmp_path)
        mod._config = cfg

        class _FakeReq:
            # No .session accessed in no-auth mode.
            pass

        user = mod._require_api_auth(_FakeReq())
        assert user["username"] == "alice"


@_needs_web_deps
class TestNextcloudAuthRegression:
    """Default auth mode still 401s without a session."""

    @pytest.fixture
    def app(self, tmp_path):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "workspace",
            site=SiteConfig(hostname="example.com"),
            users={"alice": UserConfig(display_name="Alice")},
            web=WebConfig(
                enabled=True, auth="nextcloud",
                oauth2_provider="https://cloud.example.com",
                oauth2_client_id="istota-web",
                session_secret_key="test-session-key",
            ),
            bot_name="Istota",
        )
        return _patch_app(cfg)

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            yield c

    async def test_api_me_returns_401_without_session(self, client):
        resp = await client.get("/istota/api/me")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin notice: runtime block derived caveats
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestAdminRuntimeSection:
    def _run(self, cfg):
        import istota.web_app as mod
        mod._config = cfg
        return mod._admin_runtime_section()

    def test_server_mode_when_not_standalone(self, tmp_path):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            web=WebConfig(auth="nextcloud"),
        )
        out = self._run(cfg)
        assert out["mode"] == "server"
        assert out["caveats"] == []

    def test_standalone_lean_caveats(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        out = self._run(cfg)
        assert out["mode"] == "standalone"
        titles = {c["title"] for c in out["caveats"]}
        assert "No sandbox isolation" in titles  # always present
        assert "No Nextcloud" in titles
        assert "Email polling is off" in titles
        assert "Nextcloud Talk is disabled" in titles
        assert "GPS location tracking is off" in titles

    def test_enabling_email_drops_email_caveat(self, tmp_path):
        from istota.config import EmailConfig
        cfg = _standalone_config(tmp_path)
        cfg.email = EmailConfig(enabled=True)
        out = self._run(cfg)
        titles = {c["title"] for c in out["caveats"]}
        assert "Email polling is off" not in titles
        # Security caveat is still always present in standalone mode.
        assert "No sandbox isolation" in titles

    def test_security_caveat_always_present_standalone(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        # Even if someone flips sandbox_enabled True, locally the caveat is
        # gated on the real value — assert the derived behaviour.
        out = self._run(cfg)
        assert any(c["title"] == "No sandbox isolation" for c in out["caveats"])


class TestAdminModelsSection:
    def _run(self, cfg):
        import istota.web_app as mod
        mod._config = cfg
        return mod._admin_models_section()

    def test_claude_code_default(self, tmp_path):
        cfg = Config(db_path=tmp_path / "istota.db")
        out = self._run(cfg)
        assert out["brain_kind"] == "claude_code"
        # No configured top-level model → CLI default sentinel.
        assert out["default_model"] == "CLI default"
        assert out["default_effort"] is None
        roles = {r["role"]: r["resolved"] for r in out["roles"]}
        assert set(roles) == {"fast", "general", "smart"}
        # Every role resolves to a canonical claude-* id, none left as an alias.
        assert all(v.startswith("claude-") for v in roles.values())
        # Endpoint/provider are native-only.
        assert "endpoint" not in out
        assert "provider" not in out

    def test_native_shows_endpoint_and_resolves_roles(self, tmp_path):
        from istota.config import BrainConfig, NativeBrainConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(
                    model="claude-sonnet-4-6",
                    base_url="https://openrouter.ai/api/v1",
                ),
                source_type_overrides={"scheduled": "native"},
            ),
        )
        cfg.brain.native.effort = "high"
        out = self._run(cfg)
        assert out["brain_kind"] == "native"
        assert out["default_model"] == "claude-sonnet-4-6"
        assert out["default_effort"] == "high"
        assert out["endpoint"] == "https://openrouter.ai/api/v1"
        assert out["provider"] == "openai_compat"
        # On the native brain every role collapses to the single configured model.
        assert all(r["resolved"] == "claude-sonnet-4-6" for r in out["roles"])
        assert out["source_type_overrides"] == {"scheduled": "native"}

    def test_the_card_names_the_running_brains_default_not_the_retired_key(
        self, tmp_path
    ):
        """ISSUE-418: the top-level keys were claude_code's own, not the card's.

        This card reports what the deployment runs by default, and it used to
        read the top-level `model` / `effort` — so on a native deployment it
        named the Claude model and effort while native ran its own. It now asks
        the brain that would actually run the task.
        """
        from istota.config import BrainConfig, NativeBrainConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(
                    model="z-ai/glm-5.3-flash",
                    effort="low",
                    base_url="https://openrouter.ai/api/v1",
                ),
            ),
            # The retired keys, still loadable and read by nothing.
            model="claude-opus-5",
            effort="high",
        )
        out = self._run(cfg)
        assert out["default_model"] == "z-ai/glm-5.3-flash"
        assert out["default_effort"] == "low"

    def test_native_empty_model_falls_back_to_endpoint_default(self, tmp_path):
        from istota.config import BrainConfig, NativeBrainConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            brain=BrainConfig(
                kind="native", native=NativeBrainConfig(model="")
            ),
        )
        out = self._run(cfg)
        assert out["default_model"] == "endpoint default"


class TestAdminStorageSection:
    def _run(self, cfg):
        import istota.web_app as mod
        mod._config = cfg
        return mod._admin_storage_section(cfg.db_path)

    def test_standalone_reports_nextcloud_not_configured(self, tmp_path):
        # No nextcloud.url → the mount status is meaningless; the frontend
        # hides the row on this flag.
        out = self._run(_standalone_config(tmp_path))
        assert out["nextcloud_configured"] is False

    def test_server_reports_nextcloud_configured(self, tmp_path):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            nextcloud_mount_path=tmp_path / "mount",
        )
        out = self._run(cfg)
        assert out["nextcloud_configured"] is True
