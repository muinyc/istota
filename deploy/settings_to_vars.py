#!/usr/bin/env python3
"""Convert an istota settings.toml file to an Ansible vars YAML file.

Settings keys mirror Ansible variable names without the istota_ prefix.
This script adds the prefix and outputs valid YAML for --extra-vars.

Uses only stdlib (Python 3.11+).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print("Python 3.11+ required, or install tomli: pip install tomli", file=sys.stderr)
            sys.exit(1)


def _yaml_scalar(value: object) -> str:
    """Format a Python value as a YAML scalar.

    Every string is double-quoted, so every string is escaped. That used to be
    a branch: a `needs_quote` test picked out the strings YAML would
    misinterpret bare, and only those got the escape — but both arms quoted,
    so a value carrying a `"` or a `\\` anywhere other than position 0 was
    wrapped in quotes with its own quotes and backslashes left raw. Three of
    the four shapes then failed the install loudly (`pa"ss`, `pa\\ss`,
    `trailing\\`, the last by escaping its own closing quote and swallowing
    the following lines); the fourth, a literal backslash-n, parsed cleanly as
    a real newline and silently changed the value. Every credential in the
    settings file comes through here.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    return str(value)


def _yaml_list(items: list, indent: int = 0) -> str:
    """Format a list as YAML."""
    prefix = " " * indent
    if not items:
        return "[]"
    # Check if all items are scalars
    if all(isinstance(item, (str, int, float, bool)) for item in items):
        lines = []
        for item in items:
            lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    # Complex items (dicts in lists)
    lines = []
    for item in items:
        if isinstance(item, dict):
            first = True
            for k, v in item.items():
                formatted = _format_value(v, indent + 4)
                if isinstance(v, dict):
                    # Nested dict: put on next lines indented
                    if first:
                        lines.append(f"{prefix}- {k}:")
                        first = False
                    else:
                        lines.append(f"{prefix}  {k}:")
                    lines.append(_yaml_dict(v, indent + 4))
                else:
                    if first:
                        lines.append(f"{prefix}- {k}: {formatted}")
                        first = False
                    else:
                        lines.append(f"{prefix}  {k}: {formatted}")
        else:
            lines.append(f"{prefix}- {_yaml_scalar(item)}")
    return "\n".join(lines)


def _format_value(value: object, indent: int = 0) -> str:
    """Format any value as YAML."""
    if isinstance(value, list):
        if not value:
            return "[]"
        # For simple scalar lists, use inline if short
        if all(isinstance(item, (str, int, float, bool)) for item in value):
            inline = "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
            if len(inline) < 80:
                return inline
        return "\n" + _yaml_list(value, indent)
    if isinstance(value, dict):
        return "\n" + _yaml_dict(value, indent)
    return _yaml_scalar(value)


def _yaml_dict(d: dict, indent: int = 0) -> str:
    """Format a dict as YAML."""
    prefix = " " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_yaml_dict(v, indent + 2))
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_yaml_list(v, indent + 2))
        else:
            lines.append(f"{prefix}{k}: {_format_value(v, indent + 2)}")
    return "\n".join(lines)


# Top-level settings keys that map directly with istota_ prefix.
# Keys not in this list are handled specially (sections, users, etc.)
_DIRECT_KEYS = {
    "home": "istota_home",
    "namespace": "istota_namespace",
    "package": "istota_package",
    "bot_name": "istota_bot_name",
    "emissaries_enabled": "istota_emissaries_enabled",
    "model": "istota_model",
    "repo_url": "istota_repo_url",
    "repo_branch": "istota_repo_branch",
    "repo_tag": "istota_repo_tag",
    "rclone_remote": "istota_rclone_remote",
    "rclone_password_obscured": "istota_rclone_password_obscured",
    "use_nextcloud_mount": "istota_use_nextcloud_mount",
    "nextcloud_mount_path": "istota_nextcloud_mount_path",
    "nextcloud_url": "istota_nextcloud_url",
    "nextcloud_username": "istota_nextcloud_username",
    "nextcloud_app_password": "istota_nextcloud_app_password",
    "claude_oauth_token": "istota_claude_code_oauth_token",
    "secret_key": "istota_secret_key",
    "admin_users": "istota_admin_users",
    "disabled_skills": "istota_disabled_skills",
    "use_environment_file": "istota_use_environment_file",
    "configure_rclone": "istota_configure_rclone",
    "install_all_extras": "istota_install_all_extras",
}

# Section keys that flatten with istota_{section}_{key} pattern
_SECTION_FLAT_KEYS = {
    "talk": {
        "enabled": "istota_talk_enabled",
        "bot_username": "istota_talk_bot_username",
    },
    "email": {
        "enabled": "istota_email_enabled",
        "imap_host": "istota_email_imap_host",
        "imap_port": "istota_email_imap_port",
        "imap_user": "istota_email_imap_user",
        "imap_password": "istota_email_imap_password",
        "smtp_host": "istota_email_smtp_host",
        "smtp_port": "istota_email_smtp_port",
        "smtp_password": "istota_email_smtp_password",
        "poll_folder": "istota_email_poll_folder",
        "bot_email": "istota_email_bot_address",
    },
    "browser": {
        "enabled": "istota_browser_enabled",
        "api_port": "istota_browser_api_port",
        "vnc_port": "istota_browser_vnc_port",
        "vnc_bind_address": "istota_browser_vnc_bind_address",
        "vnc_password": "istota_browser_vnc_password",
        "vnc_external_url": "istota_browser_vnc_external_url",
        "max_sessions": "istota_browser_max_sessions",
        "shm_size": "istota_browser_shm_size",
    },
    "location": {
        "enabled": "istota_location_enabled",
        "webhooks_port": "istota_webhooks_port",
    },
    "whisper": {
        "enabled": "istota_whisper_enabled",
        "model": "istota_whisper_model",
        "max_model": "istota_whisper_max_model",
    },
    "backup": {
        "enabled": "istota_backup_enabled",
    },
    # An external CalDAV server, overriding the [nextcloud] derivation.
    # Nothing here enforces which of the three are set together, and nothing
    # needs to: config.toml.j2 and secrets.env.j2 carry the same gate on url
    # *and* password, so a settings file naming only one of that pair renders
    # no block at all. The pair is the credential boundary — `Config.caldav_*`
    # falls back to [nextcloud] field by field, so a url with no password
    # would hand a foreign host the Nextcloud app password.
    #
    # `username` is deliberately outside that gate, and the difference is
    # worth knowing before reading it as "all three or none": a settings file
    # with url and password but no username renders the block, and
    # `Config.caldav_username` then falls back to the Nextcloud username. That
    # fails to authenticate rather than leaking a secret, which is why the
    # template gates on two rather than three.
    "caldav": {
        "url": "istota_caldav_url",
        "username": "istota_caldav_username",
        "password": "istota_caldav_password",
    },
    "site": {
        "hostname": "istota_hostname",
    },
    "web": {
        "enabled": "istota_web_enabled",
        "port": "istota_web_port",
        "oauth2_provider": "istota_web_oauth2_provider",
        "oauth2_client_id": "istota_web_oauth2_client_id",
        "oauth2_client_secret": "istota_web_oauth2_client_secret",
        "oauth2_token_endpoint": "istota_web_oauth2_token_endpoint",
        "oauth2_userinfo_endpoint": "istota_web_oauth2_userinfo_endpoint",
        "oauth2_redirect_uri": "istota_web_oauth2_redirect_uri",
        "secret_key": "istota_web_secret_key",
    },
}

# Sections that map their keys with a common prefix
_SECTION_PREFIX_MAP = {
    "conversation": "istota_conversation_",
    "logging": "istota_logging_",
    "scheduler": "istota_scheduler_",
}

# Security section has nested structure
_SECURITY_KEYS = {
    "sandbox_enabled": "istota_security_sandbox_enabled",
    "skill_proxy_enabled": "istota_security_skill_proxy_enabled",
    "skill_proxy_timeout": "istota_security_skill_proxy_timeout",
    "sandbox_ro_paths": "istota_security_sandbox_ro_paths",
    "network_enabled": "istota_security_network_enabled",
    "network_allow_pypi": "istota_security_network_allow_pypi",
    "network_extra_hosts": "istota_security_network_extra_hosts",
}

# Nested sections that map as structured dicts
_NESTED_SECTIONS = {
    "sleep_cycle": {
        "enabled": "istota_sleep_cycle_enabled",
        "cron": "istota_sleep_cycle_cron",
        "lookback_hours": "istota_sleep_cycle_lookback_hours",
        "memory_retention_days": "istota_sleep_cycle_memory_retention_days",
        "auto_load_dated_days": "istota_sleep_cycle_auto_load_dated_days",
        "curate_user_memory": "istota_sleep_cycle_curate_user_memory",
    },
    "channel_sleep_cycle": {
        "enabled": "istota_channel_sleep_cycle_enabled",
        "cron": "istota_channel_sleep_cycle_cron",
        "lookback_hours": "istota_channel_sleep_cycle_lookback_hours",
        "memory_retention_days": "istota_channel_sleep_cycle_memory_retention_days",
    },
    "memory_search": {
        "enabled": "istota_memory_search_enabled",
        "auto_index_conversations": "istota_memory_search_auto_index_conversations",
        "auto_index_memory_files": "istota_memory_search_auto_index_memory_files",
        "auto_recall": "istota_memory_search_auto_recall",
        "auto_recall_limit": "istota_memory_search_auto_recall_limit",
    },
}

# Developer section
_DEVELOPER_KEYS = {
    "enabled": "istota_developer_enabled",
    "repos_dir": "istota_developer_repos_dir",
    "gitlab_url": "istota_developer_gitlab_url",
    "gitlab_token": "istota_developer_gitlab_token",
    "gitlab_username": "istota_developer_gitlab_username",
    "gitlab_default_namespace": "istota_developer_gitlab_default_namespace",
    "gitlab_reviewer": "istota_developer_gitlab_reviewer",
    "gitlab_reviewer_id": "istota_developer_gitlab_reviewer_id",
    "github_url": "istota_developer_github_url",
    "github_token": "istota_developer_github_token",
    "github_username": "istota_developer_github_username",
    "github_default_owner": "istota_developer_github_default_owner",
    "github_reviewer": "istota_developer_github_reviewer",
    "forge_cli_extra_denied": "istota_developer_forge_cli_extra_denied",
    "forge_cli_permit": "istota_developer_forge_cli_permit",
    "gh_bin_path": "istota_developer_gh_bin_path",
    "glab_bin_path": "istota_developer_glab_bin_path",
}

# [developer.container] — where project code builds and runs.
# No "backend" entry: the key is retired and where development work runs is
# derived from `[devbox] enabled`. A mapping left here would keep writing a
# variable the role no longer reads.
_DEVELOPER_CONTAINER_KEYS = {
    "exec_socket_dir": "istota_developer_container_exec_socket_dir",
    "connect_timeout_seconds": "istota_developer_container_connect_timeout_seconds",
    "idle_timeout_seconds": "istota_developer_container_idle_timeout_seconds",
    "shim_commands": "istota_developer_container_shim_commands",
}

# [talk.signaling] — inbound Talk over the standalone signaling server.
# Separate from the flat `talk` map above because TOML nests it, and the flat
# walk would otherwise map `signaling` (a dict) onto nothing.
_TALK_SIGNALING_KEYS = {
    "enabled": "istota_talk_signaling_enabled",
    "url": "istota_talk_signaling_url",
    "room_sync_interval": "istota_talk_signaling_room_sync_interval",
    "reconnect_backoff_max": "istota_talk_signaling_reconnect_backoff_max",
    "payload_direct": "istota_talk_signaling_payload_direct",
}

# [web.map] — which basemap tiles the location views fetch. `api_key` is not a
# credential: MapLibre puts it in the tile URL, so it reaches every browser
# that loads a map. It is mapped here like any other setting for that reason.
_WEB_MAP_KEYS = {
    "provider": "istota_web_map_provider",
    "api_key": "istota_web_map_api_key",
    "dark_style": "istota_web_map_dark_style",
    "light_style": "istota_web_map_light_style",
    "attribution": "istota_web_map_attribution",
}

# [brain.native] — a deliberately partial map. `effort`, the two model-catalog
# keys, the turn-budget and soft-deadline keys and the whole [web_fetch]
# sub-table are reachable only by writing the istota_* variable into inventory,
# so this section is not one of the ones the coverage guard in
# tests/test_wizard_settings_roundtrip.py declares complete.
#
# At module scope rather than inside `convert()` because that is what lets the
# same test check its targets against defaults/main.yml — a typo in one is
# otherwise silent, since Ansible accepts an extra-var nothing reads.
_BRAIN_NATIVE_KEYS = {
    "provider": "istota_brain_native_provider",
    "model": "istota_brain_native_model",
    "base_url": "istota_brain_native_base_url",
    "api_key": "istota_brain_native_api_key",
    "context_window": "istota_brain_native_context_window",
    "max_turns": "istota_brain_native_max_turns",
    "max_tokens": "istota_brain_native_max_tokens",
    "prompt_caching": "istota_brain_native_prompt_caching",
}

# [brain.native.session_log] — the per-attempt JSONL transcript of a native
# task. A sub-table of a sub-table, which is why it needs a name of its own:
# `convert` walks [brain] and [brain.native] by hand and had no branch below
# that, so every one of these was reachable by writing the istota_* variable
# into inventory and by no settings file (ISSUE-436).
_SESSION_LOG_KEYS = {
    "enabled": "istota_brain_native_session_log_enabled",
    "dir": "istota_brain_native_session_log_dir",
    "retention_days": "istota_brain_native_session_log_retention_days",
    "max_total_gb": "istota_brain_native_session_log_max_total_gb",
    "max_content_chars": "istota_brain_native_session_log_max_content_chars",
    "max_args_chars": "istota_brain_native_session_log_max_args_chars",
    "include_thinking": "istota_brain_native_session_log_include_thinking",
}

# [brain] keys other than `kind` and the nested sub-tables.
#
# `fallback` is here but is emitted only when the settings file names it, and
# that asymmetry is deliberate: its Ansible default is an *expression* deriving
# `claude_code` for a tmux_claude deployment and "" for the rest, so any
# extra-var at all replaces the derivation. A settings file that omits the key
# must produce no variable, which `convert` gets for free by testing membership
# rather than truthiness — but a caller writing `fallback = ""` to mean "leave
# it alone" gets the opposite of what it wants, and that is what the wizard's
# "derive" answer exists to avoid.
_BRAIN_FLAT_KEYS = {
    "room_selectable": "istota_brain_room_selectable",
    "fallback": "istota_brain_fallback",
    "fallback_on_transient": "istota_brain_fallback_on_transient",
    "fallback_cooldown_seconds": "istota_brain_fallback_cooldown_seconds",
}


def convert(settings: dict) -> dict:
    """Convert a settings dict to Ansible vars dict."""
    result: dict = {}

    # Keys that should only be emitted when non-empty (empty would block
    # Ansible's auto-generation via set_fact, since extra-vars take precedence)
    _SKIP_WHEN_EMPTY = {"istota_rclone_password_obscured"}

    # Direct top-level keys
    for settings_key, ansible_key in _DIRECT_KEYS.items():
        if settings_key in settings:
            value = settings[settings_key]
            if ansible_key in _SKIP_WHEN_EMPTY and not value:
                continue
            result[ansible_key] = value

    # Flat section keys
    for section_name, key_map in _SECTION_FLAT_KEYS.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for settings_key, ansible_key in key_map.items():
                if settings_key in section:
                    result[ansible_key] = section[settings_key]

    # Nested sub-tables of two of the flat sections above. Both are reached
    # through their parent, so a settings file may write [talk.signaling] or
    # [web.map] without writing [talk] or [web] at all — which is what the
    # wizard does, since writing the parent's own keys empty would override
    # the role's defaults for them.
    for parent_name, child_name, key_map in (
        ("talk", "signaling", _TALK_SIGNALING_KEYS),
        ("web", "map", _WEB_MAP_KEYS),
    ):
        parent = settings.get(parent_name, {})
        if not isinstance(parent, dict):
            continue
        child = parent.get(child_name, {})
        if isinstance(child, dict):
            for settings_key, ansible_key in key_map.items():
                if settings_key in child:
                    result[ansible_key] = child[settings_key]

    # Prefix-mapped sections (conversation, logging, scheduler)
    for section_name, prefix in _SECTION_PREFIX_MAP.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for key, value in section.items():
                result[f"{prefix}{key}"] = value

    # Security section (can have nested [security.network])
    security = settings.get("security", {})
    if isinstance(security, dict):
        for key, ansible_key in _SECURITY_KEYS.items():
            if key in security:
                result[ansible_key] = security[key]
        # Handle nested [security.network] section
        network = security.get("network", {})
        if isinstance(network, dict):
            for key, value in network.items():
                ansible_key = f"istota_security_network_{key}"
                if ansible_key.replace("istota_security_network_", "") in ("enabled", "allow_pypi", "extra_hosts"):
                    result[_SECURITY_KEYS.get(f"network_{key}", ansible_key)] = value

    # Nested sections with explicit key maps
    for section_name, key_map in _NESTED_SECTIONS.items():
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            for settings_key, ansible_key in key_map.items():
                if settings_key in section:
                    result[ansible_key] = section[settings_key]

    # Developer section
    developer = settings.get("developer", {})
    if isinstance(developer, dict):
        for key, ansible_key in _DEVELOPER_KEYS.items():
            if key in developer:
                result[ansible_key] = developer[key]
        container = developer.get("container", {})
        if isinstance(container, dict):
            for key, ansible_key in _DEVELOPER_CONTAINER_KEYS.items():
                if key in container:
                    result[ansible_key] = container[key]

    # Brain section — nested: [brain], [brain.native], [brain.source_type_overrides].
    # Ansible defaults + config.toml.j2 already render these; we just map the
    # settings keys onto the istota_brain_* variable names.
    brain = settings.get("brain", {})
    if isinstance(brain, dict):
        if "kind" in brain:
            result["istota_brain_kind"] = brain["kind"]
        for settings_key, ansible_key in _BRAIN_FLAT_KEYS.items():
            if settings_key in brain:
                result[ansible_key] = brain[settings_key]
        native = brain.get("native", {})
        if isinstance(native, dict):
            for settings_key, ansible_key in _BRAIN_NATIVE_KEYS.items():
                if settings_key in native:
                    result[ansible_key] = native[settings_key]
            extra_headers = native.get("extra_headers", {})
            if isinstance(extra_headers, dict) and extra_headers:
                result["istota_brain_native_extra_headers"] = extra_headers
            # [brain.native.session_log]. Reached through its parents rather
            # than requiring them to be populated, the same rule
            # [talk.signaling] follows: a settings file may write the
            # grandchild alone, and writing [brain] or [brain.native] keys
            # empty to get at it would override the role's defaults for them.
            session_log = native.get("session_log", {})
            if isinstance(session_log, dict):
                for settings_key, ansible_key in _SESSION_LOG_KEYS.items():
                    if settings_key in session_log:
                        result[ansible_key] = session_log[settings_key]
        overrides = brain.get("source_type_overrides", {})
        if isinstance(overrides, dict) and overrides:
            result["istota_brain_source_type_overrides"] = overrides

    # Model alias registry ([models.aliases]) — maps aliases (tiers
    # fast/general/smart, shortcuts opus/sonnet/haiku, and any custom name) to
    # concrete model targets. Native deploys rely on this to resolve the tier
    # names internal subsystems request.
    models = settings.get("models", {})
    if isinstance(models, dict):
        aliases = models.get("aliases", {})
        if isinstance(aliases, dict) and aliases:
            result["istota_models_aliases"] = aliases

    # Shared default briefings (pass through as-is, it's a list of dicts)
    default_briefings = settings.get("default_briefings", [])
    if default_briefings:
        result["istota_default_briefings"] = default_briefings

    # Users section — pass through as-is (Ansible expects istota_users dict)
    users = settings.get("users", {})
    if users:
        result["istota_users"] = users

    return result


def to_yaml(vars_dict: dict) -> str:
    """Render vars dict as YAML string."""
    lines = ["---", "# Ansible vars generated from settings.toml by settings_to_vars.py", ""]
    for key, value in sorted(vars_dict.items()):
        if isinstance(value, dict):
            lines.append(f"{key}:")
            lines.append(_yaml_dict(value, indent=2))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}:")
            lines.append(_yaml_list(value, indent=2))
        else:
            lines.append(f"{key}: {_format_value(value, indent=2)}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert istota settings.toml to Ansible vars YAML"
    )
    parser.add_argument(
        "--settings", "-s",
        default="/etc/istota/settings.toml",
        help="Path to settings TOML file (default: /etc/istota/settings.toml)",
    )
    parser.add_argument(
        "--output", "-o",
        default="/etc/istota/vars.yml",
        help="Output YAML file path (default: /etc/istota/vars.yml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print YAML to stdout instead of writing to file",
    )
    args = parser.parse_args()

    settings_path = Path(args.settings)
    if not settings_path.exists():
        print(f"Settings file not found: {settings_path}", file=sys.stderr)
        sys.exit(1)

    with open(settings_path, "rb") as f:
        settings = tomllib.load(f)

    vars_dict = convert(settings)
    yaml_text = to_yaml(vars_dict)

    if args.dry_run:
        print(yaml_text)
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 through os.open rather than write_text then chmod: this file
        # carries every credential in the settings file — the secret key, the
        # Nextcloud app password, the forge tokens, and now the CalDAV
        # password — and `install.sh` neither narrows it nor removes it
        # afterwards, so it stays on the host at the process umask, which for
        # root is 0644. Only ansible-playbook reads it, as root.
        #
        # The parent's mode is deliberately left alone. `/etc/istota` also
        # holds the admins file, which the daemon reads as its own user, so
        # creating the directory 0700 root-owned would break a fresh install
        # in a way nothing here would notice.
        fd = os.open(
            output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(yaml_text)
        print(f"  wrote {output_path}")


if __name__ == "__main__":
    main()
