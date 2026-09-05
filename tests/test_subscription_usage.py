"""Tests for ``istota.subscription_usage``.

Nothing here touches the network, the real macOS Keychain, or the real
``~/.claude/.credentials.json``. Every entry point takes its environment, its
home directory and its transport as a parameter, so the whole module is
exercised against ``tmp_path`` and a stub callable.

The payload fixture is shaped like the real 2026-08-22 capture but carries
**invented** utilization figures and **invented** codenames of the same shape as
the unreleased ones the endpoint really returns (resolved open question 3 in the
spec). The codenames matter: the point of ``test_no_codename_reaches_a_window``
is that the allowlist drops them, and a fixture without them would prove
nothing.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import subscription_usage as su

# The root conftest neutralizes both of these for the whole suite, so a doctor
# sweep on a developer's macOS laptop cannot read the real keychain or reach the
# real endpoint. This file is the one that tests them, so it captures them
# before that fixture runs and puts them back for every test here.
_REAL_GET_SNAPSHOT = su.get_snapshot
_REAL_URLLIB_TRANSPORT = su._urllib_transport


@pytest.fixture(autouse=True)
def _the_real_module(monkeypatch):
    """Undo the root network guard: this file is what proves the guarded code works.

    Safe because every test here passes its own ``env``, ``home`` and
    ``transport``, and the two ``_urllib_transport`` cases substitute the opener
    rather than opening a socket.
    """
    monkeypatch.setattr(su, "get_snapshot", _REAL_GET_SNAPSHOT)
    monkeypatch.setattr(su, "_urllib_transport", _REAL_URLLIB_TRANSPORT)


@pytest.fixture(autouse=True)
def _no_carried_over_backoff():
    """The no-credential rate limit is process-local, and pytest is one process.

    Every test uses its own ``tmp_path``, which is the key, so nothing should
    carry over — but a module-global that survives a test is worth clearing
    rather than reasoning about, especially under xdist where the ordering is
    not the file's.
    """
    su._NO_CREDENTIAL_AT.clear()
    yield
    su._NO_CREDENTIAL_AT.clear()


# ---------------------------------------------------------------------------
# Fixture payload
# ---------------------------------------------------------------------------

# Invented substitutes for the unreleased top-level keys the endpoint returns.
# `cobalt_lantern` is deliberately a *live* window below (utilization 0.0), so a
# test that only checked for null values would not exercise the allowlist.
CODENAMES = (
    "quince",
    "walrus_cravat",
    "cobalt_lantern",
    "frittata_promotional",
    "ember_inlet",
    "topaz_stairway",
    "seven_day_coworking",
)

NOW = datetime(2026, 8, 22, 16, 35, 12, tzinfo=timezone.utc).timestamp()

# Invented, like the percentages above: the endpoint's real reply carried
# microsecond-precision reset times, and a fractional part that is not round is
# a fingerprint of one request rather than a shape. What the values have to keep
# is structural — a non-round fraction (so the fractional-second parse is still
# exercised), a five-hour window inside five hours of `NOW`, a weekly one
# several days out, and two microsecond values distinct from each other.
FIVE_HOUR_RESETS = "2026-08-22T18:07:33.481027+00:00"
WEEKLY_RESETS = "2026-08-27T09:15:44.732615+00:00"

# 18:07:33.481027 − 16:35:12 = 5541.48s
FIVE_HOUR_RESETS_IN = 5541
# 2026-08-27T09:15:44.732615 − 2026-08-22T16:35:12 = 405632.73s
WEEKLY_RESETS_IN = 405632


def _dollars_block() -> dict:
    return {"limit_dollars": None, "used_dollars": None, "remaining_dollars": None}


def payload() -> dict:
    """A fresh copy of the fixture payload (tests mutate it)."""
    return {
        "five_hour": {"utilization": 37.0, "resets_at": FIVE_HOUR_RESETS, **_dollars_block()},
        "seven_day": {"utilization": 12.0, "resets_at": WEEKLY_RESETS, **_dollars_block()},
        "seven_day_sonnet": {"utilization": 4.0, "resets_at": WEEKLY_RESETS, **_dollars_block()},
        "seven_day_opus": None,
        "seven_day_oauth_apps": None,
        # --- invented codenames, one of them live ---
        "seven_day_coworking": None,
        "quince": None,
        "walrus_cravat": None,
        "cobalt_lantern": {"utilization": 0.0, "resets_at": None, **_dollars_block()},
        "frittata_promotional": None,
        "ember_inlet": None,
        "topaz_stairway": None,
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": 2000,
            "used_credits": 0.0,
            "utilization": 0.0,
            "currency": "USD",
            "decimal_places": 2,
            "disabled_reason": "out_of_credits",
        },
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 37,
                "severity": "normal",
                "resets_at": FIVE_HOUR_RESETS,
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 12,
                "severity": "normal",
                "resets_at": WEEKLY_RESETS,
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 4,
                "severity": "normal",
                "resets_at": None,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
        "spend": {
            "used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 2000, "currency": "USD", "exponent": 2},
            "percent": 0,
            "severity": "normal",
            "enabled": False,
            "disclaimer": "Estimates only.",
        },
        "member_dashboard_available": False,
    }


def _stub_transport(
    status: int = 200,
    body: object = None,
    *,
    calls: list | None = None,
    response_headers: dict | None = None,
):
    """A ``Transport`` returning a canned response and recording calls.

    ``response_headers`` is how a test spells a ``Retry-After``; it defaults to
    none at all, which is the shape of a server that states nothing.
    """
    if body is None:
        body = payload()
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    hdrs = dict(response_headers or {})

    def transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes, dict]:
        if calls is not None:
            calls.append((url, headers, timeout))
        return status, raw, dict(hdrs)

    return transport


def _config(tmp_path: Path, **claude_code) -> SimpleNamespace:
    """A minimal stand-in for ``Config``.

    ``subscription_usage.get_snapshot`` reads only ``db_path`` and the
    ``brain.claude_code.*`` fields, and reads them defensively, so a namespace
    with those attributes is a faithful stub. Stage 2 adds the real dataclass.
    """
    settings = {
        "subscription_usage": True,
        "subscription_usage_cache_ttl_seconds": 300,
        "subscription_usage_timeout_seconds": 10.0,
    }
    settings.update(claude_code)
    return SimpleNamespace(
        db_path=tmp_path / "istota.db",
        brain=SimpleNamespace(claude_code=SimpleNamespace(**settings)),
    )


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

# Values that must never make a coercion helper raise. `json.loads` really does
# produce every one of these: NaN and Infinity are accepted as bare tokens by
# default, and a long integer literal becomes an arbitrary-precision int, on
# which `float()` raises OverflowError.
HOSTILE_VALUES = [
    None,
    True,
    False,
    "40",
    "",
    [],
    {},
    float("nan"),
    float("inf"),
    float("-inf"),
    10**400,
    -(10**400),
    10**308 * 10,
]


class TestCoercionHelpersAreTotal:
    """Assert at the helper, not through a caller.

    Every hostile-input test that goes through ``parse_usage``'s ``limits[]``
    path is wrapped in that path's blanket ``except``, so it passes just as well
    against a helper that raises as against one that returns ``None``. These
    assertions cannot be satisfied that way — which is what makes them the ones
    that pin the module's headline invariant.
    """

    @pytest.mark.parametrize("value", HOSTILE_VALUES)
    def test_number_returns(self, value):
        assert su._number(value) is None

    @pytest.mark.parametrize("value", HOSTILE_VALUES)
    def test_percent_returns(self, value):
        assert su._percent(value) is None

    @pytest.mark.parametrize("value", HOSTILE_VALUES)
    def test_int_returns(self, value):
        assert su._int(value, default=-1) == -1

    @pytest.mark.parametrize("value", HOSTILE_VALUES)
    def test_unclamped_percent_returns(self, value):
        assert su._unclamped_percent(value, default=-1.0) == -1.0

    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            7,
            [],
            {},
            "",
            "not a date",
            # Valid ISO-8601 that overflows on the shift to UTC. One keystroke
            # from the sentinel expiry this codebase writes into credentials.
            "9999-12-31T23:59:59-14:00",
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59.999999+23:59",
        ],
    )
    def test_normalize_resets_at_returns(self, value):
        canonical, ts = su._normalize_resets_at(value)
        assert ts is None or isinstance(ts, float)
        assert canonical is None or isinstance(canonical, str)

    def test_a_huge_number_does_not_raise_on_the_top_level_path(self):
        """The top-level path has no blanket ``except`` around it, by design."""
        raw = payload()
        raw.pop("limits")
        raw["five_hour"] = {"utilization": 10**400, "resets_at": FIVE_HOUR_RESETS}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["seven_day", "seven_day_sonnet"]

    def test_an_overflowing_reset_time_costs_the_timestamp_not_the_window(self):
        raw = payload()
        raw.pop("limits")
        raw["five_hour"] = {"utilization": 37.0, "resets_at": "9999-12-31T23:59:59-14:00"}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].key == "five_hour"
        assert windows[0].percent == 37.0
        assert windows[0].resets_in_seconds is None

    def test_a_huge_spend_figure_does_not_raise(self):
        raw = payload()
        raw["spend"]["used"] = {"amount_minor": 10**400}
        raw["spend"]["percent"] = 10**400
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is not None
        assert spend.used_minor == 0
        assert spend.percent == 0.0

    def test_a_non_utc_offset_is_converted_not_relabelled(self):
        canonical, _ = su._normalize_resets_at("2026-08-22T20:07:33+02:00")
        assert canonical == "2026-08-22T18:07:33Z"


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------


def _write_credentials(home: Path, obj: object) -> Path:
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    p = d / ".credentials.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return p


class TestResolveToken:
    def test_env_wins(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "from-env"}, tmp_path) == (
            "from-env",
            "env",
        )

    def test_empty_env_var_falls_through_to_the_file(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": ""}, tmp_path) == (
            "from-file",
            "file",
        )

    def test_whitespace_env_var_falls_through(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "  \n"}, tmp_path) == (
            "from-file",
            "file",
        )

    def test_env_token_is_stripped(self, tmp_path):
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "tok\n"}, tmp_path) == ("tok", "env")

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        assert su.resolve_token({}, tmp_path) is None

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            {"other": {"accessToken": "x"}},
            {"claudeAiOauth": {"accessToken": ""}},
            {"claudeAiOauth": {"accessToken": None}},
            {"claudeAiOauth": "a string"},
            {"claudeAiOauth": {}},
            ["a", "list"],
        ],
        ids=[
            "malformed-json",
            "no-claudeAiOauth",
            "empty-token",
            "null-token",
            "oauth-not-a-dict",
            "no-accessToken",
            "payload-not-a-dict",
        ],
    )
    def test_unusable_file_returns_none(self, tmp_path, monkeypatch, content):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        _write_credentials(tmp_path, content)
        assert su.resolve_token({}, tmp_path) is None

    def test_the_ansible_sentinel_expiry_does_not_raise(self, tmp_path):
        """The literal string the Ansible role and the docker entrypoint write.

        The proof of concept did ``expires_at / 1000 <= time.time()`` on this
        field, which is an ``int`` in the keychain blob and a *string* here —
        a ``TypeError`` on exactly the deployment shape this has to work on.
        This module never looks at expiry at all.
        """
        _write_credentials(
            tmp_path,
            {"claudeAiOauth": {"accessToken": "x", "expiresAt": "9999-12-31T23:59:59.999Z"}},
        )
        assert su.resolve_token({}, tmp_path) == ("x", "file")

    def test_no_subprocess_is_spawned_off_darwin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        calls = []
        monkeypatch.setattr(
            su.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace()
        )
        assert su.resolve_token({}, tmp_path) is None
        assert calls == []

    def test_keychain_branch_on_darwin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            blob = json.dumps({"claudeAiOauth": {"accessToken": "kc-token"}})
            return subprocess.CompletedProcess(argv, 0, stdout=blob, stderr="")

        monkeypatch.setattr(su.subprocess, "run", fake_run)
        assert su.resolve_token({"USER": "someone"}, tmp_path) == ("kc-token", "keychain")
        assert seen["argv"][:2] == ["security", "find-generic-password"]
        assert "Claude Code-credentials" in seen["argv"]
        assert "someone" in seen["argv"]
        assert seen["kwargs"].get("timeout") == su.KEYCHAIN_PROBE_TIMEOUT

    @pytest.mark.parametrize(
        "result",
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="not found"),
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"claudeAiOauth":{}}', stderr=""),
        ],
        ids=["nonzero-exit", "not-json", "empty-object", "no-token"],
    )
    def test_keychain_failures_return_none(self, tmp_path, monkeypatch, result):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(su.subprocess, "run", lambda *a, **k: result)
        assert su.resolve_token({"USER": "someone"}, tmp_path) is None

    def test_keychain_timeout_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="security", timeout=1)

        monkeypatch.setattr(su.subprocess, "run", boom)
        assert su.resolve_token({"USER": "someone"}, tmp_path) is None

    @pytest.mark.parametrize(
        "token",
        ["tok\nX-Evil: y", "tok\rX: y", "tok\x00", "tok\x7f", "\n\n"],
        ids=["lf", "cr", "nul", "del", "only-newlines"],
    )
    def test_a_token_with_a_control_character_is_refused(self, tmp_path, monkeypatch, token):
        """Such a value cannot authenticate and can only leak.

        ``http.client`` rejects a header value containing CR/LF by raising a
        ``ValueError`` that embeds the value **as a repr** — which a substring
        redaction would not match, putting the credential into an error string
        and a log line. Refusing it at the source removes the class.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": token}, tmp_path) is None

    def test_a_control_character_in_the_credential_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "tok\nX: y"}})
        assert su.resolve_token({}, tmp_path) is None

    def test_a_none_home_skips_the_file_branch(self, tmp_path, monkeypatch):
        """A process with no resolvable home has no credential file, not a crash."""
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        assert su.resolve_token({}, None) is None
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, None) == ("t", "env")

    def test_a_non_ascii_credential_file_is_read(self, tmp_path, monkeypatch):
        """The file is UTF-8, not the locale default.

        Under a systemd unit with no ``LANG`` the preferred encoding is ASCII,
        and a locale-default read would report "no credential" for a file that
        has a perfectly good one.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        path = tmp_path / ".claude" / ".credentials.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            json.dumps(
                {"claudeAiOauth": {"accessToken": "tok", "note": "café ünïcode"}}
            ).encode("utf-8")
        )
        assert su.resolve_token({}, tmp_path) == ("tok", "file")

    def test_resolve_never_writes(self, tmp_path, monkeypatch):
        """The credential file is read-only to this module, on every branch.

        This is the test that would have caught the proof of concept's
        ``--refresh`` behaviour reaching production.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        path = _write_credentials(
            tmp_path,
            {"claudeAiOauth": {"accessToken": "x", "expiresAt": "9999-12-31T23:59:59.999Z"}},
        )
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        before_listing = sorted(p.name for p in (tmp_path / ".claude").iterdir())

        assert su.resolve_token({}, tmp_path) == ("x", "file")

        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime
        assert sorted(p.name for p in (tmp_path / ".claude").iterdir()) == before_listing
        assert not list((tmp_path / ".claude").glob("*.tmp"))


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


class TestParseUsageLimitsPath:
    def test_the_captured_payload(self):
        windows, spend = su.parse_usage(payload(), now_ts=NOW)

        assert [w.key for w in windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert [w.label for w in windows] == ["5-hour", "Weekly (all models)", "Weekly (Fable)"]
        assert [w.percent for w in windows] == [37.0, 12.0, 4.0]
        assert windows[0].resets_at == "2026-08-22T18:07:33Z"
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN
        assert windows[1].resets_at == "2026-08-27T09:15:44Z"
        assert windows[1].resets_in_seconds == WEEKLY_RESETS_IN
        assert windows[2].resets_at is None
        assert windows[2].resets_in_seconds is None
        assert [w.severity for w in windows] == ["normal", "normal", "normal"]
        assert [w.is_active for w in windows] == [True, False, False]
        assert spend is not None

    def test_no_codename_reaches_a_window(self):
        """The allowlist is the whole point: unreleased names must not render.

        The fixture really carries all seven, and one of them
        (``cobalt_lantern``) is a live window rather than a null, so this
        assertion is not satisfied by the payload being empty.
        """
        raw = payload()
        for name in CODENAMES:
            assert name in raw
        # Not merely present: one is a live window with a real utilization, so
        # a fixture edit that nulled them all would fail here rather than
        # quietly turning the assertion below into a tautology.
        assert isinstance(raw["cobalt_lantern"], dict)
        assert su._percent(raw["cobalt_lantern"]["utilization"]) is not None

        for source in (payload(), {k: v for k, v in payload().items() if k != "limits"}):
            windows, _ = su.parse_usage(source, now_ts=NOW)
            rendered = " ".join(w.key + " " + w.label for w in windows).lower()
            for name in CODENAMES:
                assert name not in rendered
                assert name.replace("_", " ") not in rendered

    def test_an_unknown_kind_is_kept_and_labelled(self):
        raw = payload()
        raw["limits"].append(
            {"kind": "monthly_all", "percent": 5, "severity": "normal", "resets_at": None}
        )
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows][-1] == "monthly_all"
        assert [w.label for w in windows][-1] == "Monthly all"

    def test_percent_is_clamped(self):
        raw = payload()
        raw["limits"][0]["percent"] = 150
        raw["limits"][1]["percent"] = -3
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].percent == 100.0
        assert windows[1].percent == 0.0

    def test_a_past_reset_floors_at_zero(self):
        raw = payload()
        raw["limits"][0]["resets_at"] = "2020-01-01T00:00:00Z"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].resets_in_seconds == 0
        assert windows[0].resets_at == "2020-01-01T00:00:00Z"

    def test_a_naive_reset_time_is_read_as_utc(self):
        raw = payload()
        raw["limits"][0]["resets_at"] = "2026-08-22T18:07:33"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].resets_at == "2026-08-22T18:07:33Z"
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN


class TestParseUsageFallbackPath:
    def test_limits_absent(self):
        raw = payload()
        raw.pop("limits")
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]
        assert [w.label for w in windows] == [
            "5-hour",
            "Weekly (all models)",
            "Weekly (Sonnet)",
        ]
        assert [w.percent for w in windows] == [37.0, 12.0, 4.0]
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN
        assert windows[0].severity == ""
        assert windows[0].is_active is None

    def test_an_empty_limits_list_is_not_mistaken_for_a_populated_one(self):
        raw = payload()
        raw["limits"] = []
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]

    def test_limits_not_a_list(self):
        raw = payload()
        raw["limits"] = "nope"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]

    def test_a_null_allowlisted_window_is_skipped(self):
        raw = payload()
        raw.pop("limits")
        raw["seven_day"] = None
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day_sonnet"]


class TestParseUsageHostileInput:
    @pytest.mark.parametrize("raw", [None, [], ["a"], "a string", 7, True])
    def test_a_non_dict_payload_yields_nothing(self, raw):
        assert su.parse_usage(raw, now_ts=NOW) == ((), None)

    @pytest.mark.parametrize(
        "percent",
        ["40", True, False, None, float("nan"), float("inf"), float("-inf"), {}, []],
        ids=["string", "true", "false", "null", "nan", "inf", "-inf", "dict", "list"],
    )
    def test_an_unusable_percent_drops_the_window(self, percent):
        raw = payload()
        raw["limits"][0]["percent"] = percent
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    def test_an_absent_percent_drops_the_window(self):
        raw = payload()
        raw["limits"][0].pop("percent")
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    def test_a_non_dict_limit_entry_is_skipped(self):
        raw = payload()
        raw["limits"].insert(0, "garbage")
        raw["limits"].insert(0, None)
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all", "weekly_scoped:fable"]

    @pytest.mark.parametrize("kind", [None, "", 7, True, {}])
    def test_an_unusable_kind_is_skipped(self, kind):
        raw = payload()
        raw["limits"][0]["kind"] = kind
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    @pytest.mark.parametrize("value", ["not a date", "", 7, True, [], {}, "2026-13-45T99:99:99"])
    def test_an_unparseable_resets_at(self, value):
        raw = payload()
        raw["limits"][0]["resets_at"] = value
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].key == "session"
        assert windows[0].resets_in_seconds is None
        # A string is carried through verbatim; a non-string becomes None.
        assert windows[0].resets_at == (value if isinstance(value, str) and value else None)

    def test_weekly_scoped_with_no_name_at_all_is_dropped(self):
        raw = payload()
        raw["limits"][2]["scope"] = {"model": {"id": None, "display_name": None}, "surface": None}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all"]

    @pytest.mark.parametrize(
        "scope",
        [None, "nope", {}, {"model": None}, {"model": "nope"}, {"model": {}}],
        ids=["null", "string", "empty", "model-null", "model-string", "model-empty"],
    )
    def test_weekly_scoped_with_an_unusable_scope_is_dropped(self, scope):
        raw = payload()
        raw["limits"][2]["scope"] = scope
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all"]

    def test_weekly_scoped_falls_back_to_the_model_id(self):
        raw = payload()
        raw["limits"][2]["scope"] = {"model": {"id": "claude-fable-4", "display_name": None}}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[2].key == "weekly_scoped:claude-fable-4"
        assert windows[2].label == "Weekly (claude-fable-4)"

    def test_two_scoped_windows_do_not_collide(self):
        raw = payload()
        raw["limits"].append(
            {
                "kind": "weekly_scoped",
                "percent": 9,
                "resets_at": None,
                "scope": {"model": {"id": None, "display_name": "Walrus"}},
            }
        )
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows][-2:] == ["weekly_scoped:fable", "weekly_scoped:walrus"]

    @pytest.mark.parametrize("severity", [None, 7, [], {}])
    def test_a_non_string_severity_becomes_empty(self, severity):
        raw = payload()
        raw["limits"][0]["severity"] = severity
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].severity == ""

    @pytest.mark.parametrize("is_active", ["yes", 1, None, {}])
    def test_a_non_bool_is_active_becomes_none(self, is_active):
        raw = payload()
        raw["limits"][0]["is_active"] = is_active
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].is_active is None

    def test_a_non_dict_top_level_window_is_skipped(self):
        raw = payload()
        raw.pop("limits")
        raw["five_hour"] = "nope"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["seven_day", "seven_day_sonnet"]


class TestParseSpend:
    def test_the_spend_block_is_preferred(self):
        _, spend = su.parse_usage(payload(), now_ts=NOW)
        assert spend == su.Spend(
            enabled=False, used_minor=0, limit_minor=2000, currency="USD", exponent=2, percent=0.0
        )

    def test_a_non_default_exponent_is_honoured(self):
        raw = payload()
        raw["spend"]["used"] = {"amount_minor": 1500, "currency": "JPY", "exponent": 0}
        raw["spend"]["limit"] = {"amount_minor": 3000, "currency": "JPY", "exponent": 0}
        raw["spend"]["enabled"] = True
        raw["spend"]["percent"] = 50
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=True,
            used_minor=1500,
            limit_minor=3000,
            currency="JPY",
            exponent=0,
            percent=50.0,
        )

    def test_extra_usage_is_the_fallback(self):
        raw = payload()
        raw.pop("spend")
        raw["extra_usage"] = {
            "is_enabled": True,
            "monthly_limit": 2000,
            "used_credits": 425.0,
            "utilization": 21.25,
            "currency": "USD",
            "decimal_places": 2,
        }
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=True,
            used_minor=425,
            limit_minor=2000,
            currency="USD",
            exponent=2,
            percent=21.25,
        )

    def test_a_missing_divisor_defaults_to_two(self):
        raw = payload()
        raw.pop("spend")
        raw["extra_usage"] = {"is_enabled": False, "monthly_limit": 2000, "used_credits": 0}
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is not None
        assert spend.exponent == 2
        assert spend.currency == "USD"

    def test_neither_block_yields_none(self):
        raw = payload()
        raw.pop("spend")
        raw.pop("extra_usage")
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is None

    @pytest.mark.parametrize("bad", [None, "nope", 7, []])
    def test_unusable_blocks_yield_none(self, bad):
        raw = payload()
        raw["spend"] = bad
        raw["extra_usage"] = bad
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is None

    def test_hostile_spend_fields_do_not_raise(self):
        raw = payload()
        raw["spend"] = {
            "used": {"amount_minor": "nope"},
            "limit": {"amount_minor": float("nan")},
            "percent": "high",
            "enabled": "yes",
        }
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=False, used_minor=0, limit_minor=0, currency="USD", exponent=2, percent=0.0
        )


# ---------------------------------------------------------------------------
# fetch_snapshot
# ---------------------------------------------------------------------------


class TestFetchSnapshot:
    def test_a_good_response(self):
        calls: list = []
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
        )
        assert snap.error == ""
        assert snap.ok
        assert snap.source == "fetch"
        assert snap.fetched_at == NOW
        assert [w.key for w in snap.windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert len(calls) == 1

    def test_the_request_shape(self):
        calls: list = []
        su.fetch_snapshot(
            "sk-ant-oat-SENTINEL", timeout=7.5, now_ts=NOW, transport=_stub_transport(calls=calls)
        )
        url, headers, timeout = calls[0]
        assert url == "https://api.anthropic.com/api/oauth/usage"
        assert headers["Authorization"] == "Bearer sk-ant-oat-SENTINEL"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        assert headers["User-Agent"] == su.USER_AGENT
        assert su.USER_AGENT.startswith("istota/")
        assert timeout == 7.5

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502])
    def test_an_http_error_becomes_an_error_snapshot(self, status):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(status, b'{"error":"forbidden"}'),
        )
        assert not snap.ok
        assert str(status) in snap.error
        assert snap.windows == ()
        assert snap.source == "none"

    def test_an_error_body_is_not_echoed(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(403, b"you shall not pass, sk-ant-oat-SENTINEL"),
        )
        assert "you shall not pass" not in snap.error

    def test_a_non_json_body(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(200, b"<html>nope</html>"),
        )
        assert not snap.ok
        assert snap.error
        assert "<html>" not in snap.error

    def test_a_payload_with_no_recognizable_window(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(200, {"quince": {"utilization": 3.0}}),
        )
        assert not snap.ok
        assert snap.error == su.NO_WINDOWS_ERROR
        assert snap.windows == ()

    def test_a_transport_raising_urlerror(self):
        def boom(url, headers, timeout):
            raise urllib.error.URLError("connection refused")

        snap = su.fetch_snapshot("t", timeout=10.0, now_ts=NOW, transport=boom)
        assert not snap.ok
        assert snap.error

    def test_a_transport_raising_something_unexpected(self):
        def boom(url, headers, timeout):
            raise RuntimeError("kaboom")

        snap = su.fetch_snapshot("t", timeout=10.0, now_ts=NOW, transport=boom)
        assert not snap.ok
        assert snap.error

    @pytest.mark.parametrize(
        "transport",
        [
            _stub_transport(),
            _stub_transport(403, b"denied"),
            _stub_transport(200, b"not json"),
        ],
        ids=["ok", "denied", "garbage"],
    )
    def test_the_token_never_reaches_the_snapshot(self, transport):
        token = "sk-ant-oat-SENTINEL-TOKEN"
        snap = su.fetch_snapshot(token, timeout=10.0, now_ts=NOW, transport=transport)
        assert token not in repr(snap)

    def test_a_token_inside_an_exception_never_reaches_the_error(self):
        """The error is built from the exception's *class*, not its message.

        An exception message is stdlib- and network-influenced text. The
        strongest version of this test is a token that a substring redaction
        would miss — ``http.client`` embeds a rejected header value as a repr,
        so the escaped form is what would actually appear.
        """
        token = "sk-ant-oat-SENTINEL-TOKEN"

        def boom(url, headers, timeout):
            raise RuntimeError(f"failed talking to {url} as {token} / {token!r}")

        snap = su.fetch_snapshot(token, timeout=10.0, now_ts=NOW, transport=boom)
        assert token not in snap.error
        assert token not in repr(snap)
        assert "failed talking to" not in snap.error
        assert "RuntimeError" in snap.error

    def test_a_failed_fetch_carries_no_fetched_at(self):
        """Every data-less snapshot reads 0.0, so an age means one thing."""
        for transport in (_stub_transport(500, b"x"), _stub_transport(200, b"nope")):
            assert su.fetch_snapshot("t", timeout=1.0, now_ts=NOW, transport=transport).fetched_at == 0.0


class TestDefaultTransport:
    """The one piece of the module that touches the real world.

    Every other test injects a stub, so without these the ``HTTPError`` →
    ``(status, body)`` conversion — the thing that routes a 401 or a 403 into
    the status branch rather than the exception branch — is only a comment.
    """

    def test_a_200_is_returned(self, monkeypatch):
        monkeypatch.setattr(su, "_build_opener", lambda: _FakeOpener(200, b'{"ok":1}'))
        assert su._urllib_transport("https://x/y", {}, 1.0) == (200, b'{"ok":1}', {})

    def test_an_http_error_becomes_a_status_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(su, "_build_opener", lambda: _FakeOpener(403, b"denied", raise_it=True))
        assert su._urllib_transport("https://x/y", {}, 1.0) == (403, b"denied", {})

    def test_an_unreadable_error_body_still_yields_the_status(self, monkeypatch):
        monkeypatch.setattr(
            su, "_build_opener", lambda: _FakeOpener(500, b"", raise_it=True, unreadable=True)
        )
        assert su._urllib_transport("https://x/y", {}, 1.0) == (500, b"", {})

    def test_the_body_is_capped(self, monkeypatch):
        monkeypatch.setattr(su, "_build_opener", lambda: _FakeOpener(200, b"x" * (su._MAX_BODY_BYTES + 5000)))
        status, body, _headers = su._urllib_transport("https://x/y", {}, 1.0)
        assert status == 200
        assert len(body) == su._MAX_BODY_BYTES

    def test_a_redirect_is_refused_rather_than_followed(self):
        """Following one would re-send the bearer token to the new host.

        ``urllib``'s default ``HTTPRedirectHandler.redirect_request`` copies
        every header except content-length/content-type onto the new request,
        and does not strip ``Authorization`` the way httpx and requests do.
        Returning ``None`` is what makes the 30x surface as an ``HTTPError``.
        """
        handler = su._NoRedirect()
        assert (
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/")
            is None
        )

    def test_the_opener_consults_no_proxy_environment(self, monkeypatch):
        """A credentialed request must not be routed through an ambient proxy.

        Asserted as a contrast against the stdlib default, because that is the
        behaviour being overridden: with ``HTTPS_PROXY`` set, ``build_opener()``
        installs a live proxy handler and ``_build_opener()`` must not.
        """
        monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.9:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.9:3128")

        def live_proxies(opener):
            return [
                h
                for h in opener.handlers
                if isinstance(h, urllib.request.ProxyHandler) and h.proxies
            ]

        assert live_proxies(urllib.request.build_opener()), "the default really would proxy"
        assert live_proxies(su._build_opener()) == []

    def test_the_opener_installs_the_refusing_redirect_handler(self):
        opener = su._build_opener()
        assert any(isinstance(h, su._NoRedirect) for h in opener.handlers)
        assert not any(
            type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers
        ), "the stdlib handler would re-send Authorization across hosts"


class _FakeOpener:
    """Stands in for the private opener ``_urllib_transport`` builds."""

    def __init__(self, status, body, *, raise_it=False, unreadable=False, headers=None):
        self.status = status
        self.body = body
        self.raise_it = raise_it
        self.unreadable = unreadable
        self.headers = dict(headers or {})

    def open(self, request, timeout=None):
        if self.raise_it:
            fp = _Unreadable() if self.unreadable else io.BytesIO(self.body)
            raise urllib.error.HTTPError(
                "https://x/y", self.status, "nope", self.headers, fp
            )
        return _FakeResponse(self.status, self.body, self.headers)


class _Unreadable:
    """An error body whose socket has already gone away."""

    def read(self, *a):
        raise OSError("socket closed")

    def close(self):
        pass


class _FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = dict(headers or {})

    def read(self, n=None):
        return self._body[:n] if n is not None else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _good_snapshot() -> su.UsageSnapshot:
    windows, spend = su.parse_usage(payload(), now_ts=NOW)
    return su.UsageSnapshot(fetched_at=NOW, windows=windows, spend=spend, source="fetch")


def _staging_names(monkeypatch, root: Path) -> list[str]:
    """Record the staging file names written under ``root``, in order.

    Both writers here go through :mod:`istota.atomic_write`, so the spy sits on
    that module's ``mkstemp`` rather than on ``su.os.open`` — which is where
    the name used to be minted and no longer is. Scoped to ``root`` because
    ``tempfile`` is a shared module: anything else in the process staging a
    file during the window would otherwise be counted here and break an exact
    count for a reason that has nothing to do with the subject.
    """
    from istota import atomic_write

    names: list[str] = []
    real = atomic_write.tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real(*a, **kw)
        if Path(name).parent == root:
            names.append(Path(name).name)
        return fd, name

    monkeypatch.setattr(atomic_write.tempfile, "mkstemp", spy)
    return names


class TestCache:
    def test_cache_path(self, tmp_path):
        assert su.cache_path(tmp_path) == tmp_path / "subscription_usage.json"

    def test_round_trip_within_ttl(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        got = su.read_cache(p, 300, now_ts=NOW + 40)
        assert got is not None
        assert got.source == "cache"
        assert got.fetched_at == NOW
        assert got.ok
        assert got.has_data
        assert [w.key for w in got.windows] == [w.key for w in _good_snapshot().windows]
        assert [w.percent for w in got.windows] == [37.0, 12.0, 4.0]
        assert [w.severity for w in got.windows] == ["normal", "normal", "normal"]
        assert [w.is_active for w in got.windows] == [True, False, False]
        assert got.spend == _good_snapshot().spend
        assert got.error == ""

    def test_the_countdown_is_recomputed_against_the_readers_clock(self, tmp_path):
        """``resets_in_seconds`` is a delta, not data.

        Restoring it verbatim would render "resets in 1h 32m" hours after the
        window actually reset — precisely the reading the stale-cache path
        exists to serve.
        """
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())

        fresh = su.read_cache(p, 3600, now_ts=NOW + 600)
        assert fresh is not None
        assert fresh.windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN - 600

        stale = su.read_cache_any_age(p, now_ts=NOW + 999_999)
        assert stale is not None
        assert stale.windows[0].resets_in_seconds == 0
        assert stale.windows[1].resets_in_seconds == 0

        # With no clock, the stored value is carried through unchanged.
        clockless = su.read_cache_any_age(p)
        assert clockless is not None
        assert clockless.windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN

    def test_a_window_with_no_reset_time_stays_none(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        got = su.read_cache_any_age(p, now_ts=NOW + 10)
        assert got is not None
        assert got.windows[2].resets_at is None
        assert got.windows[2].resets_in_seconds is None

    def test_outside_ttl_returns_none_but_any_age_returns_it(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert su.read_cache(p, 300, now_ts=NOW + 301) is None
        stale = su.read_cache_any_age(p)
        assert stale is not None
        assert stale.fetched_at == NOW

    def test_a_future_fetched_at_is_stale(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert su.read_cache(p, 300, now_ts=NOW - 86400) is None

    def test_a_missing_file_returns_none(self, tmp_path):
        p = su.cache_path(tmp_path / "nope")
        assert su.read_cache(p, 300, now_ts=NOW) is None
        assert su.read_cache_any_age(p) is None

    @pytest.mark.parametrize(
        "content",
        [
            "{truncated",
            "[]",
            '"a string"',
            "{}",
            '{"fetched_at": "soon", "windows": []}',
            '{"fetched_at": 1.0, "windows": "nope"}',
            '{"fetched_at": 1.0, "windows": []}',
            '{"fetched_at": 1.0, "windows": [{"key": 7}]}',
            '{"fetched_at": true, "windows": []}',
            # An oversized number: `float()` on an arbitrary-precision int
            # raises OverflowError, and this file is on-disk state two
            # processes write, so "corrupt" is not hypothetical.
            '{"fetched_at": ' + "9" * 400 + ', "windows": []}',
            '{"fetched_at": 1.0, "windows": [{"key": "a", "label": "b", "percent": '
            + "9" * 400
            + "}]}",
            '{"fetched_at": NaN, "windows": []}',
        ],
        ids=[
            "truncated",
            "list",
            "string",
            "empty",
            "bad-fetched-at",
            "windows-not-a-list",
            "no-windows",
            "unusable-window",
            "bool-fetched-at",
            "oversized-fetched-at",
            "oversized-percent",
            "nan-fetched-at",
        ],
    )
    def test_a_corrupt_cache_returns_none_from_both_readers(self, tmp_path, content):
        p = su.cache_path(tmp_path)
        p.write_text(content)
        assert su.read_cache(p, 300, now_ts=1.0) is None
        assert su.read_cache_any_age(p) is None
        assert su.read_cache_any_age(p, now_ts=1.0) is None

    def test_a_ttl_of_zero_expires_everything(self, tmp_path):
        """The direction an operator setting it to zero means.

        The loader floors the configured value at 1, so nobody reaches this by
        accident — but ``read_cache`` is public, and "0 means never expire"
        would be the opposite of what it reads like.
        """
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert su.read_cache(p, 0, now_ts=NOW + 1) is None
        assert su.read_cache(p, -5, now_ts=NOW + 1) is None
        assert su.read_cache(p, 0, now_ts=NOW) is not None

    def test_a_bool_countdown_in_the_cache_is_not_read_as_zero(self, tmp_path):
        """``True`` is an ``int`` subclass; read as 0 it renders "resetting now"."""
        p = su.cache_path(tmp_path)
        p.write_text(
            json.dumps(
                {
                    "fetched_at": NOW,
                    "windows": [
                        {
                            "key": "session",
                            "label": "5-hour",
                            "percent": 37.0,
                            "resets_at": None,
                            "resets_in_seconds": True,
                        }
                    ],
                }
            )
        )
        got = su.read_cache_any_age(p, now_ts=NOW)
        assert got is not None
        assert got.windows[0].resets_in_seconds is None

    def test_the_file_is_written_0600_with_no_tmp_left_behind(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob(".*.tmp")) == []
        assert sorted(x.name for x in tmp_path.iterdir()) == ["subscription_usage.json"]

    def test_the_temp_file_is_scoped_to_this_process(self, tmp_path, monkeypatch):
        """Two writers must not share one temp inode.

        ``os.replace`` is atomic with respect to the rename, not with respect to
        two writers opening one fixed temp name ``O_TRUNC`` and writing at
        independent offsets. The scheduler and the web unit are separate
        processes writing this file with no lock, and the admin doctor endpoint
        runs its shallow phase through ``asyncio.to_thread``, so the second
        writer is sometimes another thread of this one — which is why the name
        is unique per *call* rather than carrying a pid.
        """
        names = _staging_names(monkeypatch, tmp_path)
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        assert len(names) == 2
        assert names[0] != names[1]
        for name in names:
            assert name.startswith(".subscription_usage.json.")
            assert name != "subscription_usage.json.tmp"

    def test_a_failed_snapshot_is_never_cached(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        before = p.read_bytes()
        su.write_cache(p, su.UsageSnapshot(fetched_at=NOW + 1, source="none", error="boom"))
        assert p.read_bytes() == before

    def test_a_write_into_a_missing_directory_creates_it(self, tmp_path):
        p = su.cache_path(tmp_path / "deep" / "nested")
        su.write_cache(p, _good_snapshot())
        assert su.read_cache_any_age(p) is not None

    @pytest.mark.requires_dac
    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        target = tmp_path / "ro"
        target.mkdir()
        os.chmod(target, 0o500)
        try:
            su.write_cache(su.cache_path(target), _good_snapshot())
            assert su.read_cache_any_age(su.cache_path(target)) is None
            assert list(target.iterdir()) == []
        finally:
            os.chmod(target, 0o700)

    def test_an_unreadable_file_returns_none(self, tmp_path):
        p = su.cache_path(tmp_path)
        p.mkdir()  # a directory where a file belongs
        assert su.read_cache(p, 300, now_ts=NOW) is None
        assert su.read_cache_any_age(p) is None


class TestSnapshotHelpers:
    def test_ok_is_false_whenever_error_is_set(self):
        assert not su.UsageSnapshot(fetched_at=NOW, error="boom").ok
        assert _good_snapshot().ok

    def test_has_data_tracks_windows_not_error(self):
        assert not su.UsageSnapshot(fetched_at=0.0, error="boom").has_data
        assert _good_snapshot().has_data
        stale = replace(_good_snapshot(), source="stale-cache", error="HTTP 403")
        assert stale.has_data
        assert not stale.ok

    def test_age_seconds(self):
        assert _good_snapshot().age_seconds(NOW + 90) == 90


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    def test_disabled_config_makes_no_call(self, tmp_path):
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path, subscription_usage=False),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert snap.error == "disabled by config"
        assert calls == []

    def test_a_fresh_cache_makes_no_call(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 40,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "cache"
        assert snap.ok
        assert calls == []

    def test_a_stale_cache_makes_one_call(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 3600,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "fetch"
        assert snap.fetched_at == NOW + 3600
        assert len(calls) == 1

    def test_a_successful_fetch_refreshes_the_cache(self, tmp_path):
        calls: list = []
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        cached = su.read_cache(su.cache_path(tmp_path), 300, now_ts=NOW)
        assert cached is not None
        assert cached.fetched_at == NOW
        # And the next call is free.
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 10,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert len(calls) == 1

    def test_no_credential_is_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert snap.error == su.NO_CREDENTIAL_ERROR
        assert calls == []
        assert not su.cache_path(tmp_path).exists()

    def test_a_failed_fetch_with_a_stale_cache(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        before = su.cache_path(tmp_path).read_bytes()
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 3600,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "stale-cache"
        assert "403" in snap.error
        assert [w.key for w in snap.windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert snap.fetched_at == NOW
        assert snap.age_seconds(NOW + 3600) == 3600
        # The two predicates say different things here, and this is the branch
        # that makes the distinction load-bearing: a caller keying on `ok` would
        # throw away exactly the reading the stale fallback exists to deliver.
        assert not snap.ok
        assert snap.has_data
        # And the countdown is against the reader's clock, not the fetch's.
        assert snap.windows[0].resets_in_seconds == max(0, FIVE_HOUR_RESETS_IN - 3600)
        # The failure must not have overwritten the good reading.
        assert su.cache_path(tmp_path).read_bytes() == before

    def test_a_failed_fetch_with_no_cache(self, tmp_path):
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(500, b"oops"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert "500" in snap.error
        assert not su.cache_path(tmp_path).exists()

    def test_an_absent_claude_code_block_uses_the_documented_defaults(self, tmp_path):
        """Stage 2 adds the dataclass; until then every ``Config`` lacks it.

        The shipping default is on, with a 300s TTL, so an absent block must
        behave exactly like the example config's block.
        """
        config = SimpleNamespace(db_path=tmp_path / "istota.db", brain=SimpleNamespace())
        calls: list = []
        snap = su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok
        assert len(calls) == 1
        assert su.read_cache(su.cache_path(tmp_path), 300, now_ts=NOW + 299) is not None

    def test_a_config_with_no_db_path_still_fetches(self, tmp_path):
        config = SimpleNamespace(db_path=None, brain=SimpleNamespace())
        snap = su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok
        assert snap.source == "fetch"

    @pytest.mark.parametrize("ttl", [0, -5, "nope", None, True, float("nan")])
    def test_a_nonsense_ttl_falls_back_to_the_default(self, tmp_path, ttl):
        """Specifically the *documented* default, not "never expires".

        The inside-the-window call alone would pass against a `_positive` that
        returned 0, because a 0 TTL used to be read as "no expiry". The call
        past the window is what distinguishes the two readings.

        Both offsets are derived from ``DEFAULT_CACHE_TTL_SECONDS`` rather than
        written out: they were literals against the old 300s default, and both
        silently stopped testing anything when it rose to 1800 — the "expired"
        probe at 400s was still inside the new window, so it asserted a cache
        hit was a fetch and failed for the right reason only by luck.
        """
        ttl_default = su.DEFAULT_CACHE_TTL_SECONDS
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=ttl)

        calls: list = []
        fresh = su.get_snapshot(
            config,
            now_ts=NOW + ttl_default / 2,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert fresh.source == "cache"
        assert calls == []

        expired = su.get_snapshot(
            config,
            now_ts=NOW + ttl_default + 100,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert expired.source == "fetch"
        assert len(calls) == 1

    def test_the_defaults_are_used_when_no_env_or_home_is_passed(self, tmp_path, monkeypatch):
        """The production call site passes neither, and nothing else covers it.

        ``Path.home()`` raises ``RuntimeError`` when ``HOME`` is unset and the
        uid has no passwd entry — the ``docker run --user`` shape. That is a
        deployment with no credential file, not a reason to fail a boot.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        def no_home():
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr(su.Path, "home", staticmethod(no_home))
        snap = su.get_snapshot(_config(tmp_path), now_ts=NOW, transport=_stub_transport())
        assert snap.error == su.NO_CREDENTIAL_ERROR
        assert snap.source == "none"

    def test_an_unexpected_failure_inside_the_policy_is_absorbed(self, tmp_path, monkeypatch):
        """The outer guard, asserted directly.

        The helpers are total in their own right — ``TestCoercionHelpersAreTotal``
        is what pins that — so this covers the backstop rather than the
        mechanism.
        """
        monkeypatch.setattr(
            su, "_settings", lambda config: (_ for _ in ()).throw(ZeroDivisionError("boom"))
        )
        snap = su.get_snapshot(_config(tmp_path), now_ts=NOW, transport=_stub_transport())
        assert not snap.ok
        assert "ZeroDivisionError" in snap.error
        assert snap.source == "none"

    def test_the_timeout_reaches_the_transport(self, tmp_path):
        calls: list = []
        su.get_snapshot(
            _config(tmp_path, subscription_usage_timeout_seconds=3.0),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert calls[0][2] == 3.0

    def test_nothing_raises_on_a_config_that_is_not_a_config(self, tmp_path):
        snap = su.get_snapshot(
            object(),
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok

    def test_the_token_never_reaches_the_returned_snapshot(self, tmp_path):
        token = "sk-ant-oat-SENTINEL-TOKEN"
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": token}})
        for transport in (_stub_transport(), _stub_transport(403, b"denied")):
            snap = su.get_snapshot(
                _config(tmp_path),
                now_ts=NOW,
                transport=transport,
                env={"CLAUDE_CODE_OAUTH_TOKEN": token},
                home=tmp_path,
            )
            assert token not in repr(snap)
        assert token not in su.cache_path(tmp_path).read_text()


class TestTokenSource:
    """Which credential produced — or failed to produce — a reading.

    The field exists so a caller can say *which* credential the endpoint refused
    without re-resolving: on macOS that would spawn ``security`` again on every
    cache hit, which is the cost the cache exists to avoid. It is the branch name
    only, never the token, and it is populated on the failures too, since a
    rejected credential is exactly the case worth naming.
    """

    def test_a_successful_fetch_names_the_branch(self, tmp_path):
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.token_source == "env"

    def test_a_rejected_credential_still_names_the_branch(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "file-token"}})
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env={},
            home=tmp_path,
        )
        assert snap.error
        assert snap.token_source == "file"

    def test_the_stale_fallback_names_the_credential_that_was_just_refused(self, tmp_path):
        """The stale windows came from an older fetch; the rejection is current.

        Naming the branch that just failed is the useful answer — the reading is
        old precisely because *that* credential stopped working.
        """
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 3600,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "stale-cache"
        assert snap.token_source == "env"

    @pytest.mark.parametrize("overrides", [{"subscription_usage": False}, {}])
    def test_a_branch_that_resolved_nothing_leaves_it_empty(
        self, tmp_path, monkeypatch, overrides
    ):
        """Disabled, and no credential anywhere: there is no branch to name."""
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        snap = su.get_snapshot(
            _config(tmp_path, **overrides),
            now_ts=NOW,
            transport=_stub_transport(),
            env={},
            home=tmp_path,
        )
        assert snap.error
        assert snap.token_source == ""

    def test_it_survives_the_cache_round_trip(self, tmp_path):
        path = su.cache_path(tmp_path)
        su.write_cache(path, replace(_good_snapshot(), token_source="keychain"))
        assert su.read_cache(path, 300, now_ts=NOW + 10).token_source == "keychain"
        assert su.read_cache_any_age(path).token_source == "keychain"

    @pytest.mark.parametrize("stored", [None, 7, {"a": 1}, "  ", "kubernetes", "ENV"])
    def test_an_unusable_stored_value_reads_as_empty(self, tmp_path, stored):
        """`None` here is the key being absent — a cache written before the field.

        The two strings are the reason this validates against a set rather than
        just coercing to `str`: the cache is a file on disk, so what comes back
        out of it is input, and both the doctor check and the admin payload
        interpolate this value into text a person reads.
        """
        path = su.cache_path(tmp_path)
        su.write_cache(path, _good_snapshot())
        raw = json.loads(path.read_text())
        if stored is None:
            raw.pop("token_source", None)
        else:
            raw["token_source"] = stored
        path.write_text(json.dumps(raw))
        assert su.read_cache_any_age(path).token_source == ""

    def test_it_is_never_the_token(self, tmp_path):
        """The whole point of the field is that it is a *name*, not a value."""
        token = "sk-ant-oat-SENTINEL-TOKEN"
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": token},
            home=tmp_path,
        )
        assert snap.token_source == "env"
        assert token not in repr(snap)


# ---------------------------------------------------------------------------
# the failure timer
# ---------------------------------------------------------------------------


def _timer(tmp_path: Path) -> Path:
    return su.failure_path(tmp_path)


class TestFailureTimerFile:
    """The timer file itself, read and written directly.

    It is a file on disk in a shared data dir, so everything that comes back out
    of it is input — the same discipline the reading cache gets. A corrupt,
    truncated, hand-edited or hostile timer means *no backoff*, never a raise:
    failing open costs one extra request, failing closed would suppress the
    reading indefinitely on a deployment where nothing is actually wrong.
    """

    def test_failure_path(self, tmp_path):
        assert su.failure_path(tmp_path) == tmp_path / "subscription_usage.failure.json"
        assert su.failure_path(tmp_path) != su.cache_path(tmp_path)

    def test_round_trip_within_ttl(self, tmp_path):
        p = _timer(tmp_path)
        su.write_failure(
            p, now_ts=NOW, error="the usage endpoint returned HTTP 403", token_source="env"
        )
        recorded = su.read_failure(p, 300, now_ts=NOW + 10)
        assert recorded is not None
        assert recorded.error == "the usage endpoint returned HTTP 403"
        assert recorded.token_source == "env"
        assert recorded.source == "none"
        assert recorded.windows == ()
        assert recorded.fetched_at == 0.0
        assert not recorded.ok
        assert not recorded.has_data

    def test_the_timer_expires_at_the_ttl(self, tmp_path):
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="boom", token_source="env")
        assert su.read_failure(p, 300, now_ts=NOW + 299) is not None
        assert su.read_failure(p, 300, now_ts=NOW + 300) is None
        assert su.read_failure(p, 300, now_ts=NOW + 3600) is None

    def test_a_timer_from_the_future_is_no_backoff(self, tmp_path):
        """The clock moved backwards, or the file was hand-edited.

        Same direction as ``read_cache``: a negative age is not "fresh forever".
        A timer nobody can wait out would suppress the reading until somebody
        deleted the file by hand.
        """
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW + 5000, error="boom", token_source="env")
        assert su.read_failure(p, 300, now_ts=NOW) is None

    def test_a_missing_timer_is_no_backoff(self, tmp_path):
        assert su.read_failure(_timer(tmp_path), 300, now_ts=NOW) is None

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "not json at all",
            "[1, 2, 3]",
            '"a string"',
            "{}",
            '{"failed_at": null, "error": "boom"}',
            '{"failed_at": "yesterday", "error": "boom"}',
            '{"failed_at": true, "error": "boom"}',
            '{"failed_at": 1e400, "error": "boom"}',
            '{"failed_at": 99999999999999999999999999999999999999, "error": "boom"}',
        ],
        ids=[
            "empty",
            "garbage",
            "a-list",
            "a-string",
            "no-fields",
            "null-time",
            "string-time",
            "bool-time",
            "infinite-time",
            "bignum-time",
        ],
    )
    def test_a_corrupt_timer_is_no_backoff(self, tmp_path, content):
        p = _timer(tmp_path)
        p.write_text(content)
        assert su.read_failure(p, 300, now_ts=NOW) is None

    @pytest.mark.parametrize(
        "error",
        [None, "", "   ", 7, [], {"a": 1}, True],
        ids=["absent", "empty", "blank", "int", "list", "dict", "bool"],
    )
    def test_a_timer_with_no_usable_error_is_no_backoff(self, tmp_path, error):
        """An empty ``error`` would reconstruct as a *success* with no windows.

        That snapshot reads as ``ok`` to every caller while carrying nothing to
        render, which is the one shape this module promises never to emit. A
        timer that cannot name its own failure is unusable, not authoritative.
        """
        p = _timer(tmp_path)
        p.write_text(json.dumps({"version": 1, "failed_at": NOW, "error": error}))
        assert su.read_failure(p, 300, now_ts=NOW + 10) is None

    @pytest.mark.parametrize("stored", [None, 7, {"a": 1}, "  ", "kubernetes", "ENV"])
    def test_an_unusable_token_source_reads_as_empty(self, tmp_path, stored):
        """Same allowlist as the reading cache, for the same reason.

        Both the doctor ``detail`` and the admin payload interpolate this into
        text a person reads, and this file is as hand-editable as the other one.
        """
        p = _timer(tmp_path)
        raw = {"version": 1, "failed_at": NOW, "error": "boom"}
        if stored is not None:
            raw["token_source"] = stored
        p.write_text(json.dumps(raw))
        recorded = su.read_failure(p, 300, now_ts=NOW + 10)
        assert recorded is not None
        assert recorded.token_source == ""

    def test_a_hostile_error_string_is_flattened_and_capped(self, tmp_path):
        """The doctor prints this on one line of a terminal.

        A newline would forge a second check's worth of output, and an unbounded
        string would push the rest of the report off the screen.
        """
        p = _timer(tmp_path)
        p.write_text(
            json.dumps(
                {
                    "version": 1,
                    "failed_at": NOW,
                    "error": "HTTP 403\nOK   runtime.forged  all good\r\n" + "x" * 5000,
                }
            )
        )
        recorded = su.read_failure(p, 300, now_ts=NOW + 10)
        assert recorded is not None
        assert "\n" not in recorded.error
        assert "\r" not in recorded.error
        assert len(recorded.error) <= su.MAX_ERROR_CHARS
        assert recorded.error.startswith("HTTP 403")

    @pytest.mark.parametrize(
        "sentinel", [su.NO_CREDENTIAL_ERROR, su.NO_WINDOWS_ERROR, su.DISABLED_ERROR]
    )
    def test_the_sentinel_errors_survive_byte_for_byte(self, tmp_path, sentinel):
        """Both callers compare these by *equality*, not by substring.

        ``doctor.check_subscription_usage`` SKIPs on ``NO_CREDENTIAL_ERROR`` and
        picks its remedy off ``NO_WINDOWS_ERROR``; the admin section substitutes
        ``NO_WINDOWS_ERROR`` for a blank reason. A suppressed call returns the
        message off disk, so ``_clean_message`` sits in that comparison's path
        and a stray reflow would silently turn a SKIP into a WARN.
        """
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error=sentinel, token_source="env")
        recorded = su.read_failure(p, 300, now_ts=NOW + 10)
        assert recorded is not None
        assert recorded.error == sentinel

    def test_the_file_is_0600_with_no_tmp_left_behind(self, tmp_path):
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="boom", token_source="env")
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob(".*.tmp")) == []
        assert sorted(x.name for x in tmp_path.iterdir()) == [
            "subscription_usage.failure.json"
        ]

    def test_the_temp_file_is_scoped_to_this_writer(self, tmp_path, monkeypatch):
        """Two writers must not share one temp inode — see ``write_cache``.

        The scheduler and the web unit are separate processes, and the admin
        doctor endpoint runs its shallow phase through ``asyncio.to_thread``, so
        the second writer is sometimes another thread of this one.
        """
        names = _staging_names(monkeypatch, tmp_path)
        su.write_failure(_timer(tmp_path), now_ts=NOW, error="boom", token_source="env")
        su.write_failure(_timer(tmp_path), now_ts=NOW, error="boom", token_source="env")
        assert len(names) == 2
        assert names[0] != names[1]
        for name in names:
            assert name.startswith(".subscription_usage.failure.json.")
            assert name != "subscription_usage.failure.json.tmp"

    def test_a_write_with_no_error_records_nothing(self, tmp_path):
        """Symmetric with ``write_cache`` refusing to store a failed snapshot."""
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="", token_source="env")
        assert not p.exists()

    @pytest.mark.parametrize("error", [None, 7, [], {"a": 1}, True, "   "])
    def test_a_write_with_an_unusable_error_records_nothing_and_does_not_raise(
        self, tmp_path, error
    ):
        """These are public names, so "nothing here raises" has to hold directly.

        Truncating a non-string argument would have raised out of a function
        documented as best-effort, and ``get_snapshot``'s blanket guard is no
        answer for a caller that reaches this one on its own.
        """
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error=error, token_source="env")
        assert not p.exists()

    @pytest.mark.parametrize("token_source", ["kubernetes", "ENV", 7, None, ""])
    def test_an_unusable_token_source_is_refused_on_the_way_in_too(
        self, tmp_path, token_source
    ):
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="boom", token_source=token_source)
        recorded = su.read_failure(p, 300, now_ts=NOW + 10)
        assert recorded is not None
        assert recorded.token_source == ""

    @pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
    def test_a_nonsense_clock_is_no_backoff(self, tmp_path, clock):
        """A ``nan`` age satisfies neither comparison, so it must read as expired.

        Written as ``0 <= age < ttl`` rather than ``age >= ttl`` for exactly
        this: the naive form leaves ``nan`` inside the window and suppresses the
        reading permanently. Not reachable from either caller today — both pass a
        real clock — but the module's discipline is to reject a non-finite number
        at the door, and this is the one comparison that could have let one in.
        """
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="boom", token_source="env")
        assert su.read_failure(p, 300, now_ts=clock) is None

    def test_an_implausibly_large_file_is_no_backoff(self, tmp_path):
        """Capped at the read, not after rebuilding it character by character.

        Nothing outside this module guarantees what is in a shared data dir, and
        this read is on the daemon's boot path.
        """
        p = _timer(tmp_path)
        p.write_text(
            json.dumps({"version": 1, "failed_at": NOW, "error": "x" * (2 << 20)})
        )
        assert su.read_failure(p, 300, now_ts=NOW + 10) is None

    def test_clear_removes_it_and_is_idempotent(self, tmp_path):
        p = _timer(tmp_path)
        su.write_failure(p, now_ts=NOW, error="boom", token_source="env")
        su.clear_failure(p)
        assert not p.exists()
        su.clear_failure(p)  # nothing there; still no raise

    def test_a_directory_where_the_file_belongs_does_not_raise(self, tmp_path):
        p = _timer(tmp_path)
        p.mkdir()
        assert su.read_failure(p, 300, now_ts=NOW) is None
        su.write_failure(p, now_ts=NOW, error="boom", token_source="env")
        su.clear_failure(p)
        assert p.is_dir()

    @pytest.mark.requires_dac
    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        target = tmp_path / "ro"
        target.mkdir()
        os.chmod(target, 0o500)
        try:
            su.write_failure(su.failure_path(target), now_ts=NOW, error="boom")
            assert su.read_failure(su.failure_path(target), 300, now_ts=NOW) is None
            assert list(target.iterdir()) == []
        finally:
            os.chmod(target, 0o700)


class TestFailureBackoff:
    """A failed reading is retried once per TTL, not once per caller.

    The admin dashboard polls ``GET /istota/api/admin/stats`` every 60 seconds.
    Without the timer a rejected credential is roughly 1,440 live 403s a day
    against api.anthropic.com per open dashboard, and a missing one is that many
    ``security`` subprocesses on macOS — because a failed snapshot is never
    written to the *reading* cache, so nothing was bounding the retry. The spec's
    own edge case already promised "never retried in a loop — the TTL is the
    retry interval"; this is what makes it true.

    Every test here counts fetches, resolver runs or subprocesses. Asserting on
    the returned value alone would pass just as well against the unbounded code.
    """

    def test_repeated_calls_after_a_rejection_issue_one_fetch(self, tmp_path):
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        seen = []
        for offset in (0, 60, 120, 180, 240, 299):
            seen.append(
                su.get_snapshot(
                    _config(tmp_path),
                    now_ts=NOW + offset,
                    transport=transport,
                    env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
                    home=tmp_path,
                )
            )
        assert len(calls) == 1
        # And every caller still gets the answer it would have got by asking.
        for snap in seen:
            assert snap.source == "none"
            assert snap.error == "the usage endpoint returned HTTP 403"
            assert snap.token_source == "env"

    def test_the_backoff_expires_with_the_ttl(self, tmp_path):
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config, now_ts=NOW + 299, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        su.get_snapshot(
            config, now_ts=NOW + 300, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 2

    def test_recovery_is_not_delayed_past_the_ttl(self, tmp_path):
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env=env,
            home=tmp_path,
        )
        good = su.get_snapshot(
            config, now_ts=NOW + 300, transport=_stub_transport(), env=env, home=tmp_path
        )
        assert good.ok
        assert good.source == "fetch"

    def test_a_success_clears_the_timer(self, tmp_path):
        """Constraint 2: recovery must never be delayed by a stale timer."""
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env=env,
            home=tmp_path,
        )
        assert _timer(tmp_path).exists()

        su.get_snapshot(
            config, now_ts=NOW + 400, transport=_stub_transport(), env=env, home=tmp_path
        )
        assert not _timer(tmp_path).exists()

        # And the cleared timer does not suppress the *next* failure's fetch: the
        # reading cache has expired by now, so this is a real attempt.
        calls: list = []
        again = su.get_snapshot(
            config,
            now_ts=NOW + 900,
            transport=_stub_transport(500, b"oops", calls=calls),
            env=env,
            home=tmp_path,
        )
        assert len(calls) == 1
        assert "500" in again.error

    def test_the_no_credential_branch_runs_the_resolver_once(self, tmp_path, monkeypatch):
        """Constraint 1. "Cheap to re-check" is true of an env var, not a keychain."""
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        real = su.resolve_token
        runs: list = []

        def spy(env, home):
            runs.append((env, home))
            return real(env, home)

        monkeypatch.setattr(su, "resolve_token", spy)
        config = _config(tmp_path)
        for offset in (0, 60, 120, 180):
            snap = su.get_snapshot(
                config,
                now_ts=NOW + offset,
                transport=_stub_transport(),
                env={},
                home=tmp_path,
            )
            assert snap.source == "none"
            assert snap.error == su.NO_CREDENTIAL_ERROR
            assert snap.token_source == ""
        assert len(runs) == 1
        # The absent credential is still not in the *reading* cache — and not in
        # the shared timer either. It is this process's answer, not the
        # deployment's; see `test_the_no_credential_record_is_not_shared`.
        assert not su.cache_path(tmp_path).exists()
        assert not _timer(tmp_path).exists()

    def test_the_no_credential_record_is_not_shared_between_processes(
        self, tmp_path, monkeypatch
    ):
        """`resolve_token` reads *this process's* environment and home directory.

        `istota-scheduler` and `istota-web` take the token from a systemd
        `EnvironmentFile`; an operator's `istota doctor` in a shell usually does
        not, and on macOS a background agent and a login session do not see the
        same keychain. If one process's "no credential" went into the file the
        others read, one read-only diagnostic run would tell the dashboard for a
        full TTL that there is no credential while the daemon was using one.

        Clearing the module-global stands in for the second process; the file is
        the only thing the two would really share.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        config = _config(tmp_path)
        first = su.get_snapshot(
            config, now_ts=NOW, transport=_stub_transport(), env={}, home=tmp_path
        )
        assert first.error == su.NO_CREDENTIAL_ERROR
        assert not _timer(tmp_path).exists()

        su._NO_CREDENTIAL_AT.clear()  # a different process, same data dir
        calls: list = []
        second = su.get_snapshot(
            config,
            now_ts=NOW + 60,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert second.ok
        assert len(calls) == 1

    def test_a_rejected_credential_is_shared_because_the_endpoint_said_so(
        self, tmp_path
    ):
        """The other half of the same rule: a 403 *is* a deployment-wide fact.

        Which is why it goes in the file and survives the process that saw it.
        """
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        recorded = su.read_failure(_timer(tmp_path), 300, now_ts=NOW + 60)
        assert recorded is not None
        assert recorded.error == "the usage endpoint returned HTTP 403"

    def test_a_suppressed_no_credential_call_matches_the_live_one(self, tmp_path, monkeypatch):
        """Same on-disk state, one minute apart, must read the same.

        With a stale cache present the two branches could easily disagree — the
        live one returns no data, and a replay routed through the stale fallback
        would return windows beside `NO_CREDENTIAL_ERROR`, a pairing the live
        path cannot produce and that doctor's branch order is not written for.
        The dashboard would then alternate between "unavailable" and "stale
        reading" every poll with nothing on the host changing.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        config = _config(tmp_path)
        seen = [
            su.get_snapshot(
                config,
                now_ts=NOW + offset,
                transport=_stub_transport(),
                env={},
                home=tmp_path,
            )
            for offset in (400, 460, 520)
        ]
        for snap in seen:
            assert snap.source == "none"
            assert snap.error == su.NO_CREDENTIAL_ERROR
            assert not snap.has_data


    def test_the_keychain_subprocess_is_spawned_once(self, tmp_path, monkeypatch):
        """The cost the timer actually exists to bound on a developer's laptop."""
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        spawns: list = []

        def fake_run(argv, **kwargs):
            spawns.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")

        monkeypatch.setattr(su.subprocess, "run", fake_run)
        config = _config(tmp_path)
        for offset in (0, 60, 120):
            su.get_snapshot(
                config,
                now_ts=NOW + offset,
                transport=_stub_transport(),
                env={"USER": "someone"},
                home=tmp_path,
            )
        assert len(spawns) == 1

    def test_a_credential_appearing_later_waits_out_one_ttl_and_no_more(
        self, tmp_path, monkeypatch
    ):
        """The trade the amendment accepts, both halves of it.

        A token added five minutes after the check that found none is not picked
        up instantly — that is the cost of not re-resolving — but the wait is one
        TTL and the delay is bounded to the process that did the checking.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        snap = su.get_snapshot(
            config, now_ts=NOW, transport=_stub_transport(), env={}, home=tmp_path
        )
        assert snap.error == su.NO_CREDENTIAL_ERROR

        calls: list = []
        early = su.get_snapshot(
            config,
            now_ts=NOW + 60,
            transport=_stub_transport(calls=calls),
            env=env,
            home=tmp_path,
        )
        assert early.error == su.NO_CREDENTIAL_ERROR
        assert calls == []

        snap = su.get_snapshot(
            config,
            now_ts=NOW + 300,
            transport=_stub_transport(calls=calls),
            env=env,
            home=tmp_path,
        )
        assert snap.ok
        assert snap.token_source == "env"
        assert len(calls) == 1

    def test_a_stale_cache_is_still_served_during_the_backoff(self, tmp_path):
        """Constraint 3: an old real reading outranks a fresh failure."""
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}

        first = su.get_snapshot(
            config, now_ts=NOW + 400, transport=transport, env=env, home=tmp_path
        )
        second = su.get_snapshot(
            config, now_ts=NOW + 420, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        for snap, offset in ((first, 400), (second, 420)):
            assert snap.source == "stale-cache"
            assert snap.error == "the usage endpoint returned HTTP 403"
            assert snap.token_source == "env"
            assert [w.key for w in snap.windows] == [
                "session",
                "weekly_all",
                "weekly_scoped:fable",
            ]
            assert snap.fetched_at == NOW
            # The countdown is against *this* caller's clock, not the failed
            # fetch's: the suppressed call still goes through the stale reader.
            assert snap.windows[0].resets_in_seconds == max(
                0, FIVE_HOUR_RESETS_IN - offset
            )

    def test_a_corrupt_timer_does_not_suppress_the_retry(self, tmp_path):
        """Constraint 4: fail open. One extra request beats a silent blackout."""
        _timer(tmp_path).write_text("{not json")
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        for offset in (0, 60):
            snap = su.get_snapshot(
                config, now_ts=NOW + offset, transport=transport, env=env, home=tmp_path
            )
            assert "403" in snap.error
        # The first call replaced the unusable timer, so only it was unsuppressed.
        assert len(calls) == 1

    def test_an_unreadable_timer_does_not_raise(self, tmp_path):
        _timer(tmp_path).mkdir()  # a directory where the file belongs
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert "403" in snap.error
        assert _timer(tmp_path).is_dir()

    def test_a_fresh_reading_outranks_the_timer(self, tmp_path):
        """Order matters: a usable reading is never withheld by a failure record."""
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        su.write_failure(_timer(tmp_path), now_ts=NOW, error="boom", token_source="env")
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 40,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "cache"
        assert snap.ok

    def test_a_shape_change_is_rate_limited_too(self, tmp_path):
        calls: list = []
        transport = _stub_transport(200, {"limits": []}, calls=calls)
        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        for offset in (0, 60, 120):
            snap = su.get_snapshot(
                config, now_ts=NOW + offset, transport=transport, env=env, home=tmp_path
            )
            assert snap.error == su.NO_WINDOWS_ERROR
        assert len(calls) == 1

    def test_a_disabled_config_records_nothing(self, tmp_path):
        su.get_snapshot(
            _config(tmp_path, subscription_usage=False),
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert not _timer(tmp_path).exists()

    def test_a_config_with_no_db_path_has_nowhere_to_record(self, tmp_path):
        """No data dir means no cache and no timer — and still no raise."""
        config = SimpleNamespace(db_path=None, brain=SimpleNamespace())
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        for offset in (0, 60):
            snap = su.get_snapshot(
                config, now_ts=NOW + offset, transport=transport, env=env, home=tmp_path
            )
            assert "403" in snap.error
        assert len(calls) == 2

    def test_a_nonsense_ttl_bounds_the_backoff_by_the_documented_default(self, tmp_path):
        calls: list = []
        transport = _stub_transport(403, b"denied", calls=calls)
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds="nope")
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        # Derived, not written out: as literals these were 299/300 against the
        # old default, and both fell inside the window when it rose to 1800.
        ttl = su.DEFAULT_CACHE_TTL_SECONDS
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config, now_ts=NOW + ttl - 1, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        su.get_snapshot(
            config, now_ts=NOW + ttl, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 2

    def test_the_token_never_reaches_the_timer_file(self, tmp_path):
        token = "sk-ant-oat-SENTINEL-TOKEN"
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": token},
            home=tmp_path,
        )
        text = _timer(tmp_path).read_text()
        assert token not in text
        assert "env" in text

    def test_a_transport_exception_is_rate_limited_too(self, tmp_path):
        calls: list = []

        def transport(url, headers, timeout):
            calls.append(url)
            raise urllib.error.URLError("no route to host")

        config = _config(tmp_path)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        for offset in (0, 60, 120):
            snap = su.get_snapshot(
                config, now_ts=NOW + offset, transport=transport, env=env, home=tmp_path
            )
            assert "could not reach api.anthropic.com" in snap.error
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """RFC 9110 gives the header two spellings; the endpoint may use either.

    Observed in production: `HTTP 429` with `Retry-After: 2327` against a cache
    TTL that was retrying every 300 seconds, which is seven knocks inside the
    window the server asked us to wait out. That deployment never obtained a
    single successful reading.
    """

    def test_delta_seconds(self):
        assert su.parse_retry_after("2327", now_ts=NOW) == 2327.0

    def test_delta_seconds_with_surrounding_space(self):
        assert su.parse_retry_after("  2327  ", now_ts=NOW) == 2327.0

    def test_zero_is_a_real_answer_not_an_absent_one(self):
        # Distinct from None: the server said "immediately", which the caller
        # then floors at its own TTL rather than treating as no hint at all.
        assert su.parse_retry_after("0", now_ts=NOW) == 0.0

    def test_an_http_date_becomes_seconds_from_now(self):
        when = datetime.fromtimestamp(NOW, tz=timezone.utc) + timedelta(seconds=600)
        stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert su.parse_retry_after(stamp, now_ts=NOW) == pytest.approx(600, abs=1)

    def test_an_http_date_in_the_past_floors_at_zero(self):
        # Clock skew between this host and Anthropic is ordinary, and a negative
        # backoff would read as "retry before now" to arithmetic never expecting
        # one.
        when = datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(hours=3)
        stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert su.parse_retry_after(stamp, now_ts=NOW) == 0.0

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "soon",
            "-5",  # delta-seconds is unsigned
            "1.5",  # delta-seconds is an integer; do not guess for a server
            "NaN",
            "Infinity",
            True,  # a bool is not a delay, and bool is an int subclass
            False,
            [],
            {},
            "Sat, 99 Zzz 2026 99:99:99 GMT",
            "9" * 400,  # long enough that float() would overflow
        ],
    )
    def test_unusable_values_are_no_hint_rather_than_an_error(self, value):
        assert su.parse_retry_after(value, now_ts=NOW) is None

    def test_it_never_raises_on_a_hostile_object(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        assert su.parse_retry_after(Hostile(), now_ts=NOW) is None


class TestRetryAfterIsHonoured:
    def _rate_limited(self, calls, retry_after="2327"):
        return _stub_transport(
            429,
            b'{"error":{"type":"rate_limit_error"}}',
            calls=calls,
            response_headers={"Retry-After": retry_after},
        )

    def test_a_stated_retry_after_overrides_a_shorter_ttl(self, tmp_path):
        """The production case, and the whole point of the change."""
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        transport = self._rate_limited(calls)

        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        assert len(calls) == 1

        # Well past the 300s TTL, still inside the 2327s the server asked for.
        for offset in (301, 1000, 2326):
            su.get_snapshot(
                config, now_ts=NOW + offset, transport=transport, env=env, home=tmp_path
            )
        assert len(calls) == 1, "retried inside the window the server named"

        su.get_snapshot(
            config, now_ts=NOW + 2327, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 2

    def test_the_header_is_matched_case_insensitively(self, tmp_path):
        # Header names are case-insensitive per RFC 9110, and a stub is under no
        # obligation to match urllib's capitalization.
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        transport = _stub_transport(
            429, b"limited", calls=calls, response_headers={"retry-after": "2327"}
        )
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config, now_ts=NOW + 1000, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1

    def test_a_retry_after_shorter_than_the_ttl_does_not_shorten_it(self, tmp_path):
        """A server asking us back sooner is not a reason to poll harder."""
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=1800)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        transport = self._rate_limited(calls, retry_after="5")
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config, now_ts=NOW + 600, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        su.get_snapshot(
            config, now_ts=NOW + 1800, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 2

    def test_an_absurd_retry_after_is_capped(self, tmp_path):
        """A buggy or hostile header must not silence the reading for a year."""
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        transport = self._rate_limited(calls, retry_after=str(365 * 24 * 3600))
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config,
            now_ts=NOW + su.MAX_RETRY_AFTER_SECONDS - 1,
            transport=transport,
            env=env,
            home=tmp_path,
        )
        assert len(calls) == 1
        su.get_snapshot(
            config,
            now_ts=NOW + su.MAX_RETRY_AFTER_SECONDS,
            transport=transport,
            env=env,
            home=tmp_path,
        )
        assert len(calls) == 2

    def test_no_header_leaves_the_ttl_in_charge(self, tmp_path):
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        transport = _stub_transport(403, b"denied", calls=calls)
        su.get_snapshot(config, now_ts=NOW, transport=transport, env=env, home=tmp_path)
        su.get_snapshot(
            config, now_ts=NOW + 299, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        su.get_snapshot(
            config, now_ts=NOW + 300, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 2

    def test_a_success_clears_a_retry_after_backoff(self, tmp_path):
        """Recovery is never delayed, however long the server asked for."""
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        su.get_snapshot(
            config,
            now_ts=NOW,
            transport=self._rate_limited(calls),
            env=env,
            home=tmp_path,
        )
        snap = su.get_snapshot(
            config,
            now_ts=NOW + 2400,
            transport=_stub_transport(calls=calls),
            env=env,
            home=tmp_path,
        )
        assert snap.ok
        assert not su.failure_path(tmp_path).exists()

    def test_the_hint_is_recorded_on_disk_and_capped_there_too(self, tmp_path):
        p = su.failure_path(tmp_path)
        su.write_failure(
            p, now_ts=NOW, error="limited", retry_after=365 * 24 * 3600.0
        )
        raw = json.loads(p.read_text())
        assert raw["retry_after_seconds"] == float(su.MAX_RETRY_AFTER_SECONDS)

    def test_a_timer_written_before_this_field_existed_still_reads(self, tmp_path):
        """No version bump and no migration: an absent key means "no hint"."""
        p = su.failure_path(tmp_path)
        p.write_text(
            json.dumps(
                {
                    "version": 1,
                    "failed_at": NOW,
                    "error": "denied",
                    "token_source": "env",
                }
            )
        )
        assert su.read_failure(p, 300, now_ts=NOW + 299) is not None
        assert su.read_failure(p, 300, now_ts=NOW + 300) is None

    @pytest.mark.parametrize("stored", ["nope", -1, 0, None, True, float("inf")])
    def test_an_unusable_stored_hint_falls_back_to_the_ttl(self, tmp_path, stored):
        p = su.failure_path(tmp_path)
        p.write_text(
            json.dumps(
                {
                    "version": 1,
                    "failed_at": NOW,
                    "error": "denied",
                    "token_source": "env",
                    "retry_after_seconds": stored,
                },
                default=str,
            )
        )
        assert su.read_failure(p, 300, now_ts=NOW + 299) is not None
        assert su.read_failure(p, 300, now_ts=NOW + 300) is None

    def test_a_stale_reading_is_still_served_during_a_retry_after_backoff(self, tmp_path):
        """An old real reading outranks a fresh failure, backoff or not."""
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        config = _config(tmp_path, subscription_usage_cache_ttl_seconds=300)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
        transport = self._rate_limited(calls)
        su.get_snapshot(
            config, now_ts=NOW + 400, transport=transport, env=env, home=tmp_path
        )
        snap = su.get_snapshot(
            config, now_ts=NOW + 1000, transport=transport, env=env, home=tmp_path
        )
        assert len(calls) == 1
        assert snap.source == "stale-cache"
        assert snap.windows
        assert "429" in snap.error
