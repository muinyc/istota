"""Post-template-render validation for istota config.toml.

Exits non-zero (with a human-readable error on stderr) when:
1. The TOML doesn't parse.
2. Top-level config keys leaked under the `[brain]` table — the
   ISSUE-058 failure mode where inserting a `[table]` header above
   existing root keys silently captures them under the table.
3. The fields the scheduler actually depends on
   (`db_path`, `temp_dir`) don't resolve to the values the operator
   passed in. Catches the same nesting bug from a different angle: when
   keys leak under a table, the dataclass defaults silently win and the
   scheduler comes up against `data/istota.db` rather than the deployed
   path.

Usage:
  validate_config.py CONFIG_PATH PACKAGE EXPECTED_DB_PATH EXPECTED_TEMP_DIR

Run via Ansible's `script` module against the deployed config; gate the
scheduler restart handler on this passing.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: validate_config.py CONFIG_PATH PACKAGE EXPECTED_DB_PATH EXPECTED_TEMP_DIR",
            file=sys.stderr,
        )
        return 2

    cfg_path_str, package, expected_db, expected_tmp = sys.argv[1:5]
    cfg_path = Path(cfg_path_str)

    try:
        import tomli
    except ImportError:
        print("validate_config: tomli not available in venv", file=sys.stderr)
        return 2

    try:
        with cfg_path.open("rb") as f:
            raw = tomli.load(f)
    except FileNotFoundError:
        print(f"validate_config: {cfg_path} does not exist", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"validate_config: TOML parse error in {cfg_path}: {e}", file=sys.stderr)
        return 1

    # Allowlist for the [brain] table. Update when BrainConfig grows
    # legitimate fields (see .claude/rules/brain.md). "native", "tmux",
    # "claude_code" and "source_type_overrides" are legitimate sub-tables
    # ([brain.native], [brain.tmux], [brain.claude_code],
    # [brain.source_type_overrides]); without them the corresponding brain
    # config would trip the leaked-keys guard. The template renders
    # [brain.claude_code] whenever istota_brain_claude_code_model/_effort
    # resolves, which since ISSUE-418 they do by default from istota_model.
    brain_allowlist = {
        "kind", "native", "tmux", "claude_code", "source_type_overrides",
        "fallback", "fallback_on_transient", "fallback_cooldown_seconds",
        "room_selectable",
    }
    brain = raw.get("brain", {})
    leaked = sorted(k for k in brain if k not in brain_allowlist)
    if leaked:
        print(
            "validate_config: keys leaked under [brain] table: "
            + ", ".join(leaked)
            + " — likely a [table] header in config.toml.j2 above root keys",
            file=sys.stderr,
        )
        return 1

    # Validate [brain.tmux] shape: reject unknown keys (a typo would template
    # cleanly and silently fall back to the default) and obviously-wrong types.
    tmux = brain.get("tmux", {})
    if tmux:
        if not isinstance(tmux, dict):
            print("validate_config: [brain.tmux] must be a table", file=sys.stderr)
            return 1
        tmux_allowlist = {
            # This brain's own default model / effort (ISSUE-418). The template
            # renders them, so an allowlist without them fails the play.
            "model", "effort",
            "fallback_trip_threshold", "fallback_cooldown_seconds",
            "ready_timeout_seconds", "tmux_command_timeout", "cli_version_pin",
            "ready_markers", "trust_markers", "theme_markers",
            "bypass_warning_marker", "bypass_accept_marker", "error_markers",
            "usage_limit_markers",
        }
        bad_keys = sorted(k for k in tmux if k not in tmux_allowlist)
        if bad_keys:
            print(
                "validate_config: unknown keys under [brain.tmux]: "
                + ", ".join(bad_keys)
                + f" — expected one of {sorted(tmux_allowlist)}",
                file=sys.stderr,
            )
            return 1
        for list_key in (
            "ready_markers", "trust_markers", "theme_markers", "error_markers",
            "usage_limit_markers",
        ):
            if list_key in tmux and not isinstance(tmux[list_key], list):
                print(
                    f"validate_config: [brain.tmux] {list_key} must be a list",
                    file=sys.stderr,
                )
                return 1

    # Validate [brain.claude_code] shape, for the reason [brain.tmux] gets one
    # above: a typo templates cleanly and falls back to the default in silence,
    # and since ISSUE-418 this table carries `model`, which decides what a
    # deployment bills against.
    claude_code = brain.get("claude_code", {})
    if claude_code:
        if not isinstance(claude_code, dict):
            print(
                "validate_config: [brain.claude_code] must be a table",
                file=sys.stderr,
            )
            return 1
        claude_code_allowlist = {
            "model", "effort",
            "subscription_usage", "subscription_usage_cache_ttl_seconds",
            "subscription_usage_timeout_seconds",
            "subscription_usage_warn_percent", "subscription_usage_high_percent",
            "subscription_usage_stale_after_seconds",
        }
        bad_keys = sorted(k for k in claude_code if k not in claude_code_allowlist)
        if bad_keys:
            print(
                "validate_config: unknown keys under [brain.claude_code]: "
                + ", ".join(bad_keys)
                + f" — expected one of {sorted(claude_code_allowlist)}",
                file=sys.stderr,
            )
            return 1

    # Validate [brain.native.web_fetch] shape: reject unknown keys (a typo would
    # template cleanly and silently fall back to the safe default).
    native = brain.get("native", {})
    web_fetch = native.get("web_fetch", {}) if isinstance(native, dict) else {}
    if web_fetch:
        if not isinstance(web_fetch, dict):
            print("validate_config: [brain.native.web_fetch] must be a table", file=sys.stderr)
            return 1
        wf_allowlist = {
            "enabled", "timeout_seconds", "max_bytes", "max_content_chars",
            "max_redirects", "allow_http", "allowed_ports", "user_agent",
            "allow_hosts", "block_hosts", "extra_blocked_cidrs",
            "require_url_provenance", "admin_only",
        }
        wf_bad = sorted(k for k in web_fetch if k not in wf_allowlist)
        if wf_bad:
            print(
                "validate_config: unknown keys under [brain.native.web_fetch]: "
                + ", ".join(wf_bad)
                + f" — expected one of {sorted(wf_allowlist)}",
                file=sys.stderr,
            )
            return 1

    # Same rule for [brain.native.session_log], now that the template renders
    # it. `dir` is the field that earns the check: it is what the retention
    # sweep unlinks *.jsonl beneath, and a misspelled key templates cleanly and
    # falls back to {db_path.parent}/logs with nothing said, so the operator
    # believes transcripts are somewhere they are not.
    session_log = native.get("session_log", {}) if isinstance(native, dict) else {}
    if session_log:
        if not isinstance(session_log, dict):
            print(
                "validate_config: [brain.native.session_log] must be a table",
                file=sys.stderr,
            )
            return 1
        sl_allowlist = {
            "enabled", "dir", "retention_days", "max_total_gb",
            "max_content_chars", "max_args_chars", "include_thinking",
        }
        sl_bad = sorted(k for k in session_log if k not in sl_allowlist)
        if sl_bad:
            print(
                "validate_config: unknown keys under [brain.native.session_log]: "
                + ", ".join(sl_bad)
                + f" — expected one of {sorted(sl_allowlist)}",
                file=sys.stderr,
            )
            return 1

    # [web] token_storage: a typo'd value would template cleanly and the
    # loader would silently fall back to "ephemeral" — the operator thinks
    # token retention is on, but it isn't. Fail the play instead.
    web = raw.get("web", {})
    token_storage = web.get("token_storage")
    if token_storage is not None and token_storage not in ("ephemeral", "encrypted"):
        print(
            f"validate_config: [web] token_storage={token_storage!r}; "
            "expected 'ephemeral' or 'encrypted'",
            file=sys.stderr,
        )
        return 1

    # [models.aliases]: an alias value is EITHER a bare string (legacy flat) OR a
    # per-namespace table ({anthropic = "...", openai_compat = "..."|{model,effort}}),
    # with an optional reserved ``portable = true`` boolean sibling of the
    # namespace keys. A malformed value (a number, or a namespace table with no
    # model) templates cleanly and set_alias_overrides only WARNs — the alias
    # silently has no override. Fail the play so a typo surfaces at deploy time.
    models = raw.get("models", {})
    aliases = models.get("aliases", {}) if isinstance(models, dict) else {}
    if aliases and not isinstance(aliases, dict):
        print("validate_config: [models.aliases] must be a table", file=sys.stderr)
        return 1
    for alias, value in (aliases.items() if isinstance(aliases, dict) else []):
        if isinstance(value, str):
            continue
        if isinstance(value, dict):
            for ns, nsval in value.items():
                # ``portable = true`` is a reserved flag, not a namespace.
                if str(ns).lower() == "portable":
                    if not isinstance(nsval, bool):
                        print(
                            f"validate_config: [models.aliases.{alias}] portable "
                            "must be a boolean",
                            file=sys.stderr,
                        )
                        return 1
                    continue
                if isinstance(nsval, str):
                    continue
                if isinstance(nsval, dict):
                    model = nsval.get("model")
                    if not isinstance(model, str) or not model.strip():
                        print(
                            f"validate_config: [models.aliases.{alias}] {ns} table "
                            "must contain a non-empty 'model' string",
                            file=sys.stderr,
                        )
                        return 1
                    continue
                print(
                    f"validate_config: [models.aliases.{alias}] {ns} must be a "
                    "string or a {model, effort} table",
                    file=sys.stderr,
                )
                return 1
            continue
        print(
            f"validate_config: [models.aliases] {alias} must be a string or a "
            "per-namespace table",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(cfg_path.parent.parent / "src"))
    try:
        mod = __import__(f"{package}.config", fromlist=["load_config"])
        load_config = mod.load_config
    except Exception as e:
        print(f"validate_config: cannot import {package}.config: {e}", file=sys.stderr)
        return 2

    # Validate brain.kind (and any source_type_overrides targets) against the
    # kinds make_brain() actually knows. A typo like "tmux-claude" would
    # otherwise template cleanly, pass load_config, and only blow up at task
    # time. Best-effort: if the brain module can't be imported, skip rather than
    # fail the play on an unrelated import error.
    try:
        known_kinds = __import__(
            f"{package}.brain", fromlist=["KNOWN_BRAIN_KINDS"]
        ).KNOWN_BRAIN_KINDS
    except Exception:
        known_kinds = None
    if known_kinds is not None:
        kind = brain.get("kind")
        if kind is not None and kind not in known_kinds:
            print(
                f"validate_config: unknown [brain] kind={kind!r}; "
                f"expected one of {sorted(known_kinds)}",
                file=sys.stderr,
            )
            return 1
        overrides = brain.get("source_type_overrides", {}) or {}
        bad = sorted(
            f"{st}={k!r}" for st, k in overrides.items() if k not in known_kinds
        )
        if bad:
            print(
                "validate_config: unknown brain kind in "
                "[brain.source_type_overrides]: " + ", ".join(bad)
                + f" — expected one of {sorted(known_kinds)}",
                file=sys.stderr,
            )
            return 1
        # A typo'd fallback ("natve") templates cleanly and load_config only
        # downgrades it to a WARNING — the play stays green while the failover
        # is silently disabled. Fail here instead. "" = no fallback, allowed.
        fallback_kind = brain.get("fallback")
        if fallback_kind and fallback_kind not in known_kinds:
            print(
                f"validate_config: unknown [brain] fallback={fallback_kind!r}; "
                f"expected one of {sorted(known_kinds)} (or \"\" for none)",
                file=sys.stderr,
            )
            return 1

    # Native brain needs an explicit model — it has no built-in role table, so
    # every request (including a claude_code→native failover) resolves to
    # [brain.native].model. An empty model templates cleanly but sends an empty
    # model id to the endpoint (400) at first use. This is exactly the
    # "paper fallback" class: native declared as primary OR fallback OR a
    # source-type override target, with no model pinned. Fail the play so it
    # surfaces at deploy time, not at the first failover.
    kind = brain.get("kind") or "claude_code"  # BrainConfig.kind default
    fallback_kind = brain.get("fallback")
    override_targets = set((brain.get("source_type_overrides", {}) or {}).values())
    native_active = (
        kind == "native"
        or fallback_kind == "native"
        or "native" in override_targets
    )
    native_model = (native.get("model") if isinstance(native, dict) else "") or ""
    if native_active and not native_model.strip():
        if kind == "native":
            reason = "kind"
        elif fallback_kind == "native":
            reason = "fallback"
        else:
            reason = "source_type_overrides target"
        print(
            f"validate_config: native brain is active ({reason}=\"native\") but "
            "[brain.native].model is empty — set istota_brain_native_model. An "
            "empty model id fails at the endpoint (400). "
            "(The API key lives in the EnvironmentFile and is not checked here.)",
            file=sys.stderr,
        )
        return 1

    # advisor_model — shape-only (a pairing table lives in the CLI's own model
    # catalog, not here; see istota.config._validate_advisor_model, which
    # load_config below runs too). A non-string is a real misconfiguration; a
    # non-anthropic primary can never run the advisor tool at all — the
    # executor only ever resolves `advisor` for the primary brain when its
    # namespace is anthropic, and a configured fallback doesn't change that
    # (a native->anthropic fallback never picks one up) — but that's a
    # WARNING, not a failed play — the operator may be mid-rollout onto
    # native with the advisor left set from a claude_code past.
    advisor_model = raw.get("advisor_model", "")
    if not isinstance(advisor_model, str):
        print(
            "validate_config: advisor_model must be a string, got "
            f"{type(advisor_model).__name__}",
            file=sys.stderr,
        )
        return 1
    if advisor_model.strip() and kind not in ("claude_code", "tmux_claude"):
        print(
            f"validate_config: advisor_model={advisor_model!r} is set but "
            f"brain.kind={kind!r} is not an anthropic-namespace brain; "
            "the advisor tool is Anthropic-only and will never run for "
            "this task (warning only, not failing the play)",
            file=sys.stderr,
        )

    try:
        c = load_config(cfg_path)
    except Exception as e:
        print(f"validate_config: load_config raised: {e}", file=sys.stderr)
        return 1

    actual_db = str(c.db_path)
    if actual_db != expected_db:
        print(
            f"validate_config: db_path={actual_db!r} expected={expected_db!r} "
            "(field likely fell back to dataclass default — keys nested under wrong table)",
            file=sys.stderr,
        )
        return 1

    actual_tmp = str(c.temp_dir)
    if actual_tmp != expected_tmp:
        print(
            f"validate_config: temp_dir={actual_tmp!r} expected={expected_tmp!r}",
            file=sys.stderr,
        )
        return 1

    print(f"validate_config: ok ({cfg_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
