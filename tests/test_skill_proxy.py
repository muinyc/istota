"""Tests for skill proxy server, client, and executor credential splitting."""

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.config import SecurityConfig
from istota.skill_client import SKILL_CLIENT_WAIT_SECONDS
from istota.skill_proxy import (
    CONNECTION_SLACK_SECONDS,
    DEFAULT_SKILL_TIMEOUTS,
    MAX_SKILL_TIMEOUT_SECONDS,
    SkillProxy,
    describe_skill_timeouts,
    resolve_skill_timeout,
)
from istota.executor import (
    _split_credential_env,
    derive_authorized_skills,
    derive_credential_set,
)
from istota.skills._env import EnvContext
from istota.skills._types import EnvSpec, SkillMeta


# Credential set used by the manifest-agnostic _split_credential_env tests.
# Mirrors the bundled-manifest derivation for the skills these tests touch.
_TEST_CREDENTIAL_SET = frozenset({
    "CALDAV_PASSWORD", "NC_PASS", "SMTP_PASSWORD", "IMAP_PASSWORD",
    "KARAKEEP_API_KEY", "GITLAB_TOKEN", "GITHUB_TOKEN",
    "GOOGLE_WORKSPACE_CLI_TOKEN", "NTFY_TOKEN", "NTFY_PASSWORD",
    "MONARCH_EMAIL", "MONARCH_PASSWORD", "MONARCH_SESSION_TOKEN",
    "TUMBLR_API_KEY",
})


@pytest.fixture
def sock_path():
    """Short socket path that fits AF_UNIX limit (~104 chars on macOS)."""
    d = tempfile.mkdtemp(prefix="sp_", dir="/tmp")
    p = Path(d) / "s.sock"
    yield p
    p.unlink(missing_ok=True)
    Path(d).rmdir()


# ---------------------------------------------------------------------------
# _split_credential_env
# ---------------------------------------------------------------------------


class TestSplitCredentialEnv:
    def test_splits_known_credentials(self):
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret1",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
            "CALDAV_URL": "https://dav.example.com",
            "NC_URL": "https://cloud.example.com",
        }
        cred, clean = _split_credential_env(env, _TEST_CREDENTIAL_SET)
        assert cred == {
            "CALDAV_PASSWORD": "secret1",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
        }
        assert "CALDAV_PASSWORD" not in clean
        assert "NC_PASS" not in clean
        assert clean["PATH"] == "/usr/bin"
        assert clean["CALDAV_URL"] == "https://dav.example.com"
        assert clean["NC_URL"] == "https://cloud.example.com"

    def test_empty_env(self):
        cred, clean = _split_credential_env({}, _TEST_CREDENTIAL_SET)
        assert cred == {}
        assert clean == {}

    def test_no_credentials_present(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        cred, clean = _split_credential_env(env, _TEST_CREDENTIAL_SET)
        assert cred == {}
        assert clean == env

    def test_only_credentials(self):
        env = {"CALDAV_PASSWORD": "x", "NC_PASS": "y"}
        cred, clean = _split_credential_env(env, _TEST_CREDENTIAL_SET)
        assert len(cred) == 2
        assert clean == {}

    def test_strips_developer_tokens(self):
        """GITLAB_TOKEN and GITHUB_TOKEN are stripped to credential env."""
        env = {
            "GITLAB_TOKEN": "glpat-xxx",
            "GITHUB_TOKEN": "ghp_xxx",
            "CALDAV_PASSWORD": "secret",
        }
        cred, clean = _split_credential_env(env, _TEST_CREDENTIAL_SET)
        assert "GITLAB_TOKEN" in cred
        assert "GITHUB_TOKEN" in cred
        assert "GITLAB_TOKEN" not in clean
        assert "GITHUB_TOKEN" not in clean
        assert "CALDAV_PASSWORD" in cred


class TestProxySocketPermissions:
    """Socket file must be 0o600 so other local users cannot connect."""

    def test_socket_is_owner_only(self, sock_path):
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


class TestProxyCredentialLookup:
    """Test the credential lookup protocol extension."""

    def _send_request(self, sock_path, request_dict):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_returns_credential_value(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret", "NC_PASS": "nc_secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "GITLAB_TOKEN",
            })
        assert resp == {"value": "glpat-secret"}

    def test_returns_error_for_unknown_credential(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "NONEXISTENT",
            })
        assert "error" in resp
        assert resp["reason"] == "credential_not_present"
        assert resp["name"] == "NONEXISTENT"

    def test_does_not_leak_base_env(self, sock_path):
        """Credential lookup only returns values from credential_env, not base_env."""
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        base_env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        with SkillProxy(sock_path, cred_env, base_env):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "PATH",
            })
        assert "error" in resp

    @patch("istota.skill_proxy.subprocess.run")
    def test_skill_requests_still_work(self, mock_run, sock_path):
        """Existing skill requests (no 'type' field) continue to work."""
        mock_run.return_value = MagicMock(
            stdout='{"ok": true}', stderr="", returncode=0,
        )
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })
        assert resp["returncode"] == 0


class TestProxyScopedCredentials:
    """Test credential scoping via allowed_credentials and skill_credential_map."""

    def _send_request(self, sock_path, request_dict):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_allowed_credential_returned(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret", "NC_PASS": "nc_secret"}
        allowed = {"GITLAB_TOKEN"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        allowed_credentials=allowed):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "GITLAB_TOKEN",
            })
        assert resp == {"value": "glpat-secret"}

    def test_disallowed_credential_rejected(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret", "NC_PASS": "nc_secret"}
        allowed = {"GITLAB_TOKEN"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        allowed_credentials=allowed):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "NC_PASS",
            })
        assert "error" in resp
        assert resp["reason"] == "not_authorized_credential"
        assert resp["name"] == "NC_PASS"

    def test_none_allowed_credentials_allows_all(self, sock_path):
        """allowed_credentials=None means no scoping (backward compat)."""
        cred_env = {"GITLAB_TOKEN": "glpat-secret", "NC_PASS": "nc_secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        allowed_credentials=None):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "NC_PASS",
            })
        assert resp == {"value": "nc_secret"}

    def test_empty_allowed_set_blocks_all(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        allowed_credentials=set()):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "GITLAB_TOKEN",
            })
        assert "error" in resp

    @patch("istota.skill_proxy.subprocess.run")
    def test_skill_cli_gets_only_mapped_credentials(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        cred_env = {"SMTP_PASSWORD": "smtp_secret", "GITLAB_TOKEN": "gl_secret"}
        skill_map = {"email": {"SMTP_PASSWORD"}}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        skill_credential_map=skill_map):
            self._send_request(sock_path, {"skill": "email", "args": ["send"]})

        called_env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
        assert called_env["SMTP_PASSWORD"] == "smtp_secret"
        assert "GITLAB_TOKEN" not in called_env

    @patch("istota.skill_proxy.subprocess.run")
    def test_skill_not_in_map_gets_no_credentials(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        cred_env = {"SMTP_PASSWORD": "smtp_secret", "GITLAB_TOKEN": "gl_secret"}
        skill_map = {"email": {"SMTP_PASSWORD"}}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        skill_credential_map=skill_map):
            self._send_request(sock_path, {"skill": "calendar", "args": ["list"]})

        called_env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
        assert "SMTP_PASSWORD" not in called_env
        assert "GITLAB_TOKEN" not in called_env

    @patch("istota.skill_proxy.subprocess.run")
    def test_none_skill_map_allows_all_credentials(self, mock_run, sock_path):
        """skill_credential_map=None means all credentials (backward compat)."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        cred_env = {"SMTP_PASSWORD": "smtp_secret", "GITLAB_TOKEN": "gl_secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"},
                        skill_credential_map=None):
            self._send_request(sock_path, {"skill": "email", "args": ["send"]})

        called_env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
        assert called_env["SMTP_PASSWORD"] == "smtp_secret"
        assert called_env["GITLAB_TOKEN"] == "gl_secret"


class TestDeriveAuthorizedSkills:
    """Decoupled authorization (Phase 3): manifest-derived. A skill is
    authorized if it was selected OR if any of its sensitive specs
    resolves for the current user."""

    def _stub_index(self, **cli_overrides) -> dict[str, SkillMeta]:
        """Build a stub skill index with sensitive specs pointing at
        ``ctx.config.<attr>`` flags so individual tests can flip them on
        and off without coupling to bundled manifests."""
        cli = {
            "files": True, "calendar": True, "email": True, "bookmarks": True,
            "feeds": True, "developer": True, "google_workspace": True,
            "nextcloud": True, "location": True, "notes": True,
        }
        cli.update(cli_overrides)

        def _spec(var, path):
            return EnvSpec(
                var=var, source="config", config_path=path,
                when=path, sensitive=True,
            )

        env_specs = {
            "calendar": [_spec("CALDAV_PASSWORD", "caldav_password")],
            "location": [_spec("CALDAV_PASSWORD", "caldav_password")],
            "email": [
                _spec("SMTP_PASSWORD", "email.smtp_password"),
                _spec("IMAP_PASSWORD", "email.imap_password"),
            ],
            "bookmarks": [_spec("KARAKEEP_API_KEY", "karakeep.api_key")],
            "developer": [
                _spec("GITLAB_TOKEN", "developer.gitlab_token"),
                _spec("GITHUB_TOKEN", "developer.github_token"),
            ],
            "google_workspace": [_spec("GOOGLE_WORKSPACE_CLI_TOKEN", "gw.token")],
            "nextcloud": [_spec("NC_PASS", "nextcloud.app_password")],
        }
        return {
            name: SkillMeta(
                name=name, description="", cli=cli[name],
                env_specs=env_specs.get(name, []),
            )
            for name in cli
        }

    def _ctx_with_attrs(self, **attrs) -> EnvContext:
        """Build an EnvContext whose config exposes the given attrs."""
        class _Cfg:
            pass
        cfg = _Cfg()
        for k, v in attrs.items():
            head, _, rest = k.partition(".")
            if not rest:
                setattr(cfg, head, v)
                continue
            sub = getattr(cfg, head, None)
            if sub is None:
                sub = _Cfg()
                setattr(cfg, head, sub)
            setattr(sub, rest, v)

        class _T:
            user_id = "alice"

        return EnvContext(
            config=cfg, task=_T(), user_resources=[],
            user_config=None, user_temp_dir=Path("/tmp"), is_admin=False,
        )

    def test_no_creds_no_authorization(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs()
        assert derive_authorized_skills([], idx, ctx) == []

    def test_user_with_only_karakeep(self):
        """User has karakeep → only `bookmarks` is auto-authorized."""
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(**{"karakeep.api_key": "x"})
        assert derive_authorized_skills([], idx, ctx) == ["bookmarks"]

    def test_user_with_email_creds(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(
            **{"email.smtp_password": "x", "email.imap_password": "y"},
        )
        assert derive_authorized_skills([], idx, ctx) == ["email"]

    def test_developer_creds_authorize_developer(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(
            **{"developer.gitlab_token": "x", "developer.github_token": "y"},
        )
        assert derive_authorized_skills([], idx, ctx) == ["developer"]

    def test_developer_one_token_only_authorizes(self):
        """``any``, not ``all``: a single configured provider triggers
        auto-auth (developer, ntfy multi-provider pattern)."""
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(**{"developer.gitlab_token": "x"})
        assert derive_authorized_skills([], idx, ctx) == ["developer"]

    def test_caldav_authorizes_calendar_and_location(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(caldav_password="x")
        assert derive_authorized_skills([], idx, ctx) == ["calendar", "location"]

    def test_admin_with_all_creds(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(
            caldav_password="a",
            **{
                "nextcloud.app_password": "b",
                "email.smtp_password": "c",
                "email.imap_password": "d",
                "karakeep.api_key": "e",
                "developer.gitlab_token": "g",
                "developer.github_token": "h",
                "gw.token": "i",
            },
        )
        assert derive_authorized_skills([], idx, ctx) == [
            "bookmarks", "calendar", "developer", "email",
            "google_workspace", "location", "nextcloud",
        ]

    def test_doc_only_skill_is_authorized(self):
        """Doc-only skills (no CLI) auto-authorize when their sensitive
        specs resolve. The developer skill is doc-only but consumes its
        tokens via credential-fetch from helper scripts."""
        idx = self._stub_index(developer=False)
        ctx = self._ctx_with_attrs(**{"developer.gitlab_token": "x"})
        assert derive_authorized_skills([], idx, ctx) == ["developer"]

    def test_selected_skills_carried_through(self):
        """Selection always authorizes — keyword-driven selection still
        flows through, even when the user has no credentials yet."""
        idx = self._stub_index()
        ctx = self._ctx_with_attrs()
        assert derive_authorized_skills(["browse"], idx, ctx) == ["browse"]

    def test_selected_unioned_with_auto_authorized(self):
        idx = self._stub_index()
        ctx = self._ctx_with_attrs(**{"karakeep.api_key": "x"})
        # selected skill `browse` doesn't have credentials but is included;
        # `bookmarks` auto-authorizes via karakeep.
        assert derive_authorized_skills(["browse"], idx, ctx) == [
            "bookmarks", "browse",
        ]


class TestProxyInformativeRejections:
    """The proxy returns structured reason fields plus an authorized-skills list."""

    def _send_request(self, sock_path, request_dict):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_unknown_skill_includes_authorized_list(self, sock_path):
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            allowed_skills=frozenset({"calendar", "email"}),
            authorized_skills=frozenset({"email"}),
            task_id=42,
        ):
            resp = self._send_request(sock_path, {"skill": "evil_skill", "args": []})
        assert resp["returncode"] == 1
        assert resp["reason"] == "unknown_skill"
        assert resp["skill"] == "evil_skill"
        assert resp["authorized_skills"] == ["email"]
        assert "Authorized skills" in resp["stderr"]
        assert "email" in resp["stderr"]

    def test_unknown_skill_falls_back_to_allowed_when_no_authorized(self, sock_path):
        """If authorized_skills not set, error message lists all CLI skills."""
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            allowed_skills=frozenset({"calendar", "email"}),
        ):
            resp = self._send_request(sock_path, {"skill": "evil_skill", "args": []})
        assert resp["authorized_skills"] == ["calendar", "email"]

    def test_credential_not_authorized_response_shape(self, sock_path):
        with SkillProxy(
            sock_path, {"GITLAB_TOKEN": "secret"}, {"PATH": "/usr/bin"},
            allowed_credentials={"NC_PASS"},
            task_id=99,
        ):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "GITLAB_TOKEN",
            })
        assert resp["reason"] == "not_authorized_credential"
        assert resp["name"] == "GITLAB_TOKEN"

    def test_master_key_rejected_by_lookup_endpoint(self, sock_path):
        """The master Fernet key flows into specific skill subprocess envs
        (via skill_credential_map) but must never be returned by the
        credential-lookup endpoint — even when the proxy holds it in
        credential_env. The executor enforces this by excluding
        ISTOTA_SECRET_KEY from `allowed_credentials`; this test pins the
        proxy's behavior under that configuration so a future executor
        regression that re-adds the var to allowed_credentials gets caught
        by a separate test, while this one stays green for the supported
        config."""
        with SkillProxy(
            sock_path,
            credential_env={
                "ISTOTA_SECRET_KEY": "k" * 64,
                "GITLAB_TOKEN": "tok",
            },
            base_env={"PATH": "/usr/bin"},
            allowed_credentials={"GITLAB_TOKEN"},
            task_id=11,
        ):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "ISTOTA_SECRET_KEY",
            })
        assert resp["reason"] == "not_authorized_credential"
        assert resp["name"] == "ISTOTA_SECRET_KEY"
        assert "k" * 64 not in json.dumps(resp)

    def test_proxy_logs_warning_on_rejection(self, sock_path, caplog):
        """Every rejection emits a structured WARNING for observability."""
        import logging
        with caplog.at_level(logging.WARNING, logger="istota.skill_proxy"):
            with SkillProxy(
                sock_path, {}, {"PATH": "/usr/bin"},
                allowed_skills=frozenset({"calendar"}),
                task_id=7,
            ):
                self._send_request(sock_path, {"skill": "feeds", "args": []})

        rejection_logs = [r for r in caplog.records if "proxy_rejected" in r.message]
        assert len(rejection_logs) == 1
        assert "task_id=7" in rejection_logs[0].message
        assert "skill=feeds" in rejection_logs[0].message
        assert "reason=unknown_skill" in rejection_logs[0].message


class TestDeriveCredentialSetCoverage:
    """Verify the manifest-derived credential set covers what executor.py
    actually injects (Phase 3: derived from bundled skill manifests, no
    hand-maintained constant)."""

    def _set(self):
        from istota.skills._loader import load_skill_index
        idx = load_skill_index(Path("config/skills"), bundled_dir=None)
        return derive_credential_set(idx)

    def test_caldav_password_in_set(self):
        assert "CALDAV_PASSWORD" in self._set()

    def test_nc_pass_in_set(self):
        assert "NC_PASS" in self._set()

    def test_smtp_password_in_set(self):
        assert "SMTP_PASSWORD" in self._set()

    def test_imap_password_in_set(self):
        assert "IMAP_PASSWORD" in self._set()

    def test_karakeep_api_key_in_set(self):
        assert "KARAKEEP_API_KEY" in self._set()

    def test_gitlab_token_in_set(self):
        assert "GITLAB_TOKEN" in self._set()

    def test_github_token_in_set(self):
        assert "GITHUB_TOKEN" in self._set()


# ---------------------------------------------------------------------------
# SkillProxy
# ---------------------------------------------------------------------------


class TestSkillProxyLifecycle:
    def test_start_stop(self, sock_path):
        proxy = SkillProxy(sock_path, {}, {})
        proxy.start()
        assert sock_path.exists()
        proxy.stop()
        assert not sock_path.exists()

    def test_context_manager(self, sock_path):
        with SkillProxy(sock_path, {}, {}):
            assert sock_path.exists()
        assert not sock_path.exists()

    def test_cleans_up_stale_socket(self, sock_path):
        sock_path.write_text("stale")
        with SkillProxy(sock_path, {}, {}):
            assert sock_path.exists()

    def test_double_stop_is_safe(self, sock_path):
        proxy = SkillProxy(sock_path, {}, {})
        proxy.start()
        proxy.stop()
        proxy.stop()  # Should not raise

    def test_stop_returns_promptly(self, sock_path):
        """The accept loop must be woken, not polled out.

        It used to sit in ``accept()`` on a 1s socket timeout, so every
        teardown — one per task, and one per executor test — paid a full
        second waiting for the loop to notice the stop event.
        """
        proxy = SkillProxy(sock_path, {}, {})
        proxy.start()
        started = time.monotonic()
        proxy.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.25, f"stop() took {elapsed:.2f}s"

    def test_serves_several_requests_on_one_instance(self, sock_path):
        """The loop takes one connection per readiness event; it must not go
        deaf after the first.

        Asserting on the *response* is the point. ``connect()`` alone succeeds
        out of the listen backlog whether or not anything ever calls
        ``accept()``, so a connect-and-close test passes against a proxy with
        no accept loop at all.
        """
        with SkillProxy(sock_path, {}, {}, allowed_skills=frozenset()):
            for _ in range(3):
                resp = TestSkillProxyProtocol()._send_request(
                    sock_path, {"skill": "nope", "args": []},
                )
                assert resp["returncode"] == 1
                assert "Unknown skill" in resp["stderr"]

    def test_transient_accept_failure_does_not_kill_the_loop(self, sock_path):
        """One failed accept() must not deafen the proxy for the rest of the task.

        The listening socket stays bound whether or not the loop is alive, so
        a dead loop turns every later skill call into a hang in the backlog
        rather than the clean refusal skill_client knows how to report.
        """
        proxy = SkillProxy(sock_path, {}, {}, allowed_skills=frozenset())
        real_accept = socket.socket.accept
        calls = {"n": 0}

        def flaky_accept(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionAbortedError("simulated ECONNABORTED")
            return real_accept(self)

        with patch.object(socket.socket, "accept", flaky_accept):
            with proxy:
                resp = TestSkillProxyProtocol()._send_request(
                    sock_path, {"skill": "nope", "args": []},
                )
        assert calls["n"] >= 2, "accept() was not retried after the failure"
        assert resp["returncode"] == 1
        assert "Unknown skill" in resp["stderr"]

    def test_restart_after_stop_serves_again(self, sock_path):
        """start() must clear the stop state, or the second run is silently deaf."""
        proxy = SkillProxy(sock_path, {}, {}, allowed_skills=frozenset())
        proxy.start()
        proxy.stop()
        proxy.start()
        try:
            resp = TestSkillProxyProtocol()._send_request(
                sock_path, {"skill": "nope", "args": []},
            )
            assert "Unknown skill" in resp["stderr"]
        finally:
            proxy.stop()


class TestSkillProxyProtocol:
    def _send_request(self, sock_path, request_dict):
        """Helper: connect to proxy and send a request, return parsed response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_rejects_unknown_skill(self, sock_path):
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"},
                        allowed_skills=frozenset({"calendar"})):
            resp = self._send_request(sock_path, {"skill": "evil_skill", "args": []})
            assert resp["returncode"] == 1
            assert "Unknown skill" in resp["stderr"]

    def test_rejects_invalid_json(self, sock_path):
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(str(sock_path))
            sock.sendall(b"not json\n")
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            sock.close()
            resp = json.loads(b"".join(chunks).decode().strip())
            assert resp["returncode"] == 1
            assert "Invalid JSON" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_runs_allowed_skill(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(
            stdout='{"status": "ok"}', stderr="", returncode=0,
        )
        base_env = {"PATH": "/usr/bin", "HOME": "/tmp"}
        cred_env = {"SMTP_PASSWORD": "secret"}

        with SkillProxy(sock_path, cred_env, base_env):
            resp = self._send_request(sock_path, {
                "skill": "email",
                "args": ["send", "--to", "bob@example.com"],
            })

        assert resp["returncode"] == 0
        assert resp["stdout"] == '{"status": "ok"}'

        # Verify subprocess was called with merged env
        call_kwargs = mock_run.call_args
        called_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert called_env["SMTP_PASSWORD"] == "secret"
        assert called_env["PATH"] == "/usr/bin"

    @patch("istota.skill_proxy.subprocess.run")
    def test_passes_args_correctly(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            self._send_request(sock_path, {
                "skill": "calendar",
                "args": ["list", "--date", "today", "--tz", "America/New_York"],
            })

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "istota.skills.calendar"]
        assert cmd[3:] == ["list", "--date", "today", "--tz", "America/New_York"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_handles_subprocess_failure(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(
            stdout="", stderr="Error: invalid arguments", returncode=2,
        )

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["bad-command"],
            })

        assert resp["returncode"] == 2
        assert "invalid arguments" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_handles_timeout(self, mock_run, sock_path):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=5)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}, timeout=5):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })

        assert resp["returncode"] == 124
        assert "timed out" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_concurrent_requests(self, mock_run, sock_path):
        """Multiple concurrent skill calls should all succeed."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        results = []

        def make_request():
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })
            results.append(resp)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            threads = [threading.Thread(target=make_request) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == 5
        assert all(r["returncode"] == 0 for r in results)


class TestCliSkillConsistency:
    """Verify cli: true in skill metadata matches actual __main__.py files."""

    def test_cli_skills_have_main(self):
        """Skills with cli: true must have __main__.py."""
        from istota.skills._loader import load_skill_index
        skills_dir = Path(__file__).parent.parent / "src" / "istota" / "skills"
        index = load_skill_index(
            skills_dir.parent.parent.parent / "config" / "skills",
            bundled_dir=skills_dir,
        )
        for name, meta in index.items():
            if meta.cli:
                main_file = skills_dir / name / "__main__.py"
                assert main_file.exists(), f"Skill {name!r} has cli: true but no __main__.py"

    def test_main_skills_are_cli(self):
        """Skills with __main__.py must have cli: true."""
        from istota.skills._loader import load_skill_index
        skills_dir = Path(__file__).parent.parent / "src" / "istota" / "skills"
        index = load_skill_index(
            skills_dir.parent.parent.parent / "config" / "skills",
            bundled_dir=skills_dir,
        )
        for child in skills_dir.iterdir():
            if child.is_dir() and (child / "__main__.py").exists():
                assert child.name in index and index[child.name].cli, (
                    f"Skill {child.name!r} has __main__.py but cli is not true"
                )


# ---------------------------------------------------------------------------
# skill_client
# ---------------------------------------------------------------------------


class TestSkillClientDirect:
    """Test the direct-execution fallback path."""

    @patch("istota.skill_client.subprocess.run")
    def test_direct_fallback_without_env(self, mock_run):
        """Without ISTOTA_SKILL_PROXY_SOCK, runs skill directly."""
        mock_run.return_value = MagicMock(returncode=0)
        from istota.skill_client import _run_direct
        with pytest.raises(SystemExit) as exc_info:
            _run_direct("email", ["send", "--to", "x@y.com"])
        assert exc_info.value.code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[1:3] == ["-m", "istota.skills.email"]

    def test_proxy_path_connects(self, sock_path):
        """With proxy socket, client connects and parses response."""
        from istota.skill_client import _run_via_proxy

        # Set up a mock server
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(65536)
            response = json.dumps({"stdout": "ok\n", "stderr": "", "returncode": 0})
            conn.sendall((response + "\n").encode())
            conn.close()
            server.close()

        t = threading.Thread(target=serve)
        t.start()

        with pytest.raises(SystemExit) as exc_info:
            _run_via_proxy(str(sock_path), "email", ["send"])

        t.join(timeout=5)
        assert exc_info.value.code == 0

    def test_proxy_connection_refused(self):
        """Client handles missing proxy gracefully."""
        from istota.skill_client import _run_via_proxy
        with pytest.raises(SystemExit) as exc_info:
            _run_via_proxy("/tmp/nonexistent.sock", "email", [])
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Executor integration: credential splitting with proxy
# ---------------------------------------------------------------------------


class TestExecutorProxyIntegration:
    """Test that execute_task splits credentials when proxy is enabled."""

    def _make_config(self, tmp_path, proxy_enabled=False):
        from istota.config import (
            Config, SecurityConfig, NextcloudConfig, SchedulerConfig,
        )
        empty_bundled = tmp_path / "_empty_bundled"
        empty_bundled.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        return Config(
            db_path=tmp_path / "test.db",
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="bot",
                app_password="nc_secret",
            ),
            security=SecurityConfig(
                mode="restricted",
                skill_proxy_enabled=proxy_enabled,
                skill_proxy_timeout=30,
            ),
            scheduler=SchedulerConfig(task_timeout_minutes=5),
            skills_dir=skills_dir,
            temp_dir=tmp_path / "tmp",
            bundled_skills_dir=empty_bundled,
        )

    def test_credentials_removed_when_proxy_enabled(self, tmp_path):
        """When skill_proxy_enabled, credential vars should not be in Claude's env."""
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
            "CALDAV_URL": "https://dav.example.com",
        }
        cred, clean = _split_credential_env(env, _TEST_CREDENTIAL_SET)

        # Clean env should not have any credential vars
        for var in _TEST_CREDENTIAL_SET:
            assert var not in clean

        # All non-credential vars should remain
        assert "PATH" in clean
        assert "CALDAV_URL" in clean

        # Credential env should have all the secrets
        assert cred["CALDAV_PASSWORD"] == "secret"
        assert cred["NC_PASS"] == "secret2"

    def test_credentials_present_when_proxy_disabled(self):
        """When proxy is disabled, no splitting happens — env is unchanged."""
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret",
            "NC_PASS": "secret2",
        }
        # Proxy disabled means _split_credential_env is never called.
        # Verify the env dict is passed through as-is.
        assert "CALDAV_PASSWORD" in env
        assert "NC_PASS" in env


class TestPerSkillTimeout:
    """ISSUE-448. `security.skill_proxy_timeout` is one number applied to every
    proxied skill call, and `code_review` is the only skill that drives model
    calls of its own. Raising it for the review raised it for everything; not
    raising it capped every review at the global minus an assembly reserve. A
    per-skill entry is the lever that was missing.
    """

    def test_a_skill_with_no_entry_anywhere_gets_the_global(self):
        assert resolve_skill_timeout(300, {}, "email") == 300
        assert resolve_skill_timeout(300, None, "email") == 300

    def test_the_shipped_policy_is_not_something_an_operator_table_can_clobber(self):
        """The half of ISSUE-448 that a config default could not express. A
        `dict` field *replaces* its default rather than merging — `coerce_dict`
        hands the operator's table straight through, and Ansible replaces a hash
        override too — so a shipped `code_review` entry inside
        `security.skill_proxy_timeouts` would be dropped by anyone who wrote
        that table to configure a different skill, quietly taking the review's
        ceiling back to the global and reproducing the bug."""
        assert resolve_skill_timeout(300, {"browse": 90}, "code_review") == 540
        assert resolve_skill_timeout(300, {}, "code_review") == 540
        assert resolve_skill_timeout(300, None, "code_review") == 540

    def test_an_operator_entry_still_beats_the_shipped_policy(self):
        """Layered, not fixed: naming the skill is how it is overridden, in
        either direction."""
        assert resolve_skill_timeout(300, {"code_review": 120}, "code_review") == 120
        assert resolve_skill_timeout(300, {"code_review": 560}, "code_review") == 560

    def test_the_config_default_ships_empty(self):
        """Paired with the two above: they only mean what they say while the
        dataclass carries no entry of its own to be clobbered."""
        assert SecurityConfig().skill_proxy_timeouts == {}

    def test_an_entry_wins_for_its_own_skill_only(self):
        overrides = {"browse": 120}
        assert resolve_skill_timeout(300, overrides, "browse") == 120
        assert resolve_skill_timeout(300, overrides, "email") == 300

    def test_an_entry_may_lower_as_well_as_raise(self):
        """Not a maximum. An operator narrowing one chatty skill is the same
        mechanism as one widening the review, and reading the entry as a floor
        would silently ignore half of what it is for."""
        assert resolve_skill_timeout(300, {"browse": 60}, "browse") == 60

    def test_nothing_may_outlive_the_client_own_wait(self):
        """`skill_client` sets a fixed socket timeout before it sends, and it
        cannot read the config — it runs in the sandbox, where there is none. A
        server timeout past that wait means the client gives up first and the
        model is told the proxy answered nothing, which is a worse failure than
        the short budget it was trying to escape."""
        resolved = resolve_skill_timeout(300, {"browse": 5000}, "browse")
        assert resolved == MAX_SKILL_TIMEOUT_SECONDS
        assert resolved + CONNECTION_SLACK_SECONDS < SKILL_CLIENT_WAIT_SECONDS

    def test_the_shipped_policy_fits_under_that_bound(self):
        """The bound is only worth having if the values shipped respect it
        without being clamped, since a clamped default is a number one file
        states and the deployment does not use."""
        assert DEFAULT_SKILL_TIMEOUTS, "the shipped policy is what makes a review fit"
        for skill, seconds in DEFAULT_SKILL_TIMEOUTS.items():
            assert seconds <= MAX_SKILL_TIMEOUT_SECONDS
            assert resolve_skill_timeout(300, {}, skill) == seconds

    def test_junk_falls_back_to_the_global_rather_than_raising(self):
        """`config_mapper` passes a table's values through uncoerced, so what
        arrives here is whatever the TOML said. The proxy handles every skill
        call on the deployment; a bad entry must cost that entry and not the
        proxy."""
        for bad in ({"browse": "soon"}, {"browse": None}, {"browse": 0},
                    {"browse": -5}, {"browse": True}, "not a table"):
            assert resolve_skill_timeout(300, bad, "browse") == 300
        # `True` is its own branch: `int(True)` is 1, so without the bool guard
        # `browse = true` would resolve to a one-second budget rather than
        # falling through. A float truncates rather than falling through, which
        # is the documented coercion and not a rejection.
        assert resolve_skill_timeout(300, {"browse": 540.9}, "browse") == 540

    def test_a_bad_entry_is_reported_once_rather_than_per_call(self):
        """`resolve_skill_timeout` runs on every proxied skill call, so a
        warning there describes a fact about the configuration at the cadence of
        traffic. `describe_skill_timeouts` answers the same question once, and
        does it by *calling* the resolver, so it cannot describe a decision the
        resolver did not make."""
        notes = describe_skill_timeouts(300, {"browse": "soon", "email": 0})
        assert len(notes) == 2
        assert any("'browse'" in n and "'soon'" in n for n in notes)
        assert any("'email'" in n and "300s" in n for n in notes)

    def test_a_global_past_the_client_wait_is_reported_too(self):
        """The clamp applies to the global as well, and that is the arm an
        operator who never wrote a per-skill table can still trip."""
        notes = describe_skill_timeouts(900, {})
        assert len(notes) == 1
        assert "skill_proxy_timeout of 900s" in notes[0]
        assert str(MAX_SKILL_TIMEOUT_SECONDS) in notes[0]

    def test_a_configuration_that_resolves_as_written_reports_nothing(self):
        """The control for the three above: a report that fires on a healthy
        configuration is one nobody reads."""
        assert describe_skill_timeouts(300, {}) == []
        assert describe_skill_timeouts(300, None) == []
        assert describe_skill_timeouts(300, {"code_review": 480}) == []

    @patch("istota.skill_proxy.subprocess.run")
    def test_the_proxy_runs_a_skill_under_its_own_entry(self, mock_run, sock_path):
        """The end the whole thing is for: the number reaching `subprocess.run`
        is the skill's, not the global's."""
        mock_run.return_value = MagicMock(stdout="{}", stderr="", returncode=0)
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            timeout=300, skill_timeouts={"code_review": 480},
        ):
            TestSkillProxyProtocol()._send_request(
                sock_path, {"skill": "code_review", "args": ["run"]}
            )
        assert mock_run.call_args.kwargs["timeout"] == 480

    @patch("istota.skill_proxy.subprocess.run")
    def test_the_shipped_policy_reaches_the_subprocess_with_no_map_at_all(
        self, mock_run, sock_path
    ):
        """The default deployment, which configures nothing."""
        mock_run.return_value = MagicMock(stdout="{}", stderr="", returncode=0)
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}, timeout=300):
            TestSkillProxyProtocol()._send_request(
                sock_path, {"skill": "code_review", "args": ["run"]}
            )
        assert mock_run.call_args.kwargs["timeout"] == 540

    @patch("istota.skill_proxy.subprocess.run")
    def test_another_skill_on_the_same_proxy_keeps_the_global(
        self, mock_run, sock_path
    ):
        """One proxy serves every skill a task may call, so the two answers have
        to come apart per connection rather than per proxy."""
        mock_run.return_value = MagicMock(stdout="{}", stderr="", returncode=0)
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            timeout=300, skill_timeouts={"code_review": 480},
        ):
            TestSkillProxyProtocol()._send_request(
                sock_path, {"skill": "email", "args": ["list"]}
            )
        assert mock_run.call_args.kwargs["timeout"] == 300

    @patch("istota.skill_proxy.subprocess.run")
    def test_the_response_send_is_armed_at_the_skill_own_budget(
        self, mock_run, sock_path
    ):
        """`settimeout` bounds each blocking *operation*, not the connection, so
        this is not an end-to-end deadline and could never have expired while
        the handler sat in `subprocess.run` — which is what an earlier version of
        this comment claimed. What the re-arm buys is that the response send is
        scaled to this skill's own budget rather than to an unrelated global: a
        skill allowed nine minutes may take longer to hand back its stdout than
        one allowed five. The first arm happens before the request is parsed, so
        it cannot know the skill."""
        mock_run.return_value = MagicMock(stdout="{}", stderr="", returncode=0)
        proxy = SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            timeout=300, skill_timeouts={"code_review": 540},
        )
        armed: list[float] = []
        real_handle = proxy._handle_connection

        class Recorder:
            """Records what the handler arms the connection with, and is
            otherwise the socket."""

            def __init__(self, conn):
                self._conn = conn

            def settimeout(self, value):
                armed.append(value)
                return self._conn.settimeout(value)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        proxy._handle_connection = lambda conn: real_handle(Recorder(conn))
        with proxy:
            TestSkillProxyProtocol()._send_request(
                sock_path, {"skill": "code_review", "args": ["run"]}
            )
        assert armed[-1] >= 540
