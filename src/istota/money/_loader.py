"""Resolve a user's money :class:`UserContext` from istota's config.

Single entry point for both web routes and the in-process skill.

Money is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "money")``. The user's workspace path is
derived from ``nextcloud_mount_path`` + ``get_user_bot_path``, and Monarch
credentials come from the encrypted secrets table. Legacy mode (the
``[[resources]] type = "money" config_path = …``-driven branch) was removed
when modules took over module gating.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import tomli

from istota import module_loader
from istota.money.cli import UserContext
from istota.money.workspace import synthesize_user_context

MODULE = "money"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable money configuration."""


def load_user_secrets(user_id: str, istota_config) -> dict:
    """Load per-user money secrets (e.g. Monarch credentials).

    Resolution order:

    1. ``MONEY_SECRETS_FILE`` env var (escape hatch for direct ``money`` CLI
       invocations and tests).
    2. The encrypted ``secrets`` table — the only durable home for Monarch
       credentials after the modules refactor.

    Returns ``{}`` if no credentials are configured — sync commands that
    require them surface their own error.
    """
    explicit = os.environ.get("MONEY_SECRETS_FILE", "")
    if explicit:
        path = Path(explicit)
        if path.exists():
            return tomli.loads(path.read_text())
        return {}

    if istota_config is None:
        return {}

    monarch: dict[str, str] = {}
    # Cookie pair is the only credential — paste once from browser DevTools
    # and it lasts months on a trusted-device login.
    env_vars = {
        "session_id": "MONARCH_SESSION_ID",
        "csrftoken": "MONARCH_CSRFTOKEN",
    }
    # Env-first: subprocess context (Phase 1.4+) gets these pre-resolved
    # by build_skill_env. Trusted-daemon context falls back to the
    # secrets_store. Master key is no longer in subprocess env, so the
    # store fallback works only in the daemon process.
    for sk, env_var in env_vars.items():
        val = os.environ.get(env_var)
        if val:
            monarch[sk] = val

    if len(monarch) < len(env_vars):
        try:
            from istota import secrets_store  # noqa: PLC0415

            db_path = getattr(istota_config, "db_path", None)
            if db_path is not None:
                for sk in env_vars:
                    if sk in monarch:
                        continue
                    val = secrets_store.get_secret(
                        db_path, user_id, "monarch", sk,
                    )
                    if val:
                        monarch[sk] = val
        except Exception:  # noqa: BLE001
            # Best-effort: a missing/unavailable secrets store yields no creds.
            pass

    return {"monarch": monarch} if monarch else {}


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> UserContext:
    """Build a money :class:`UserContext` for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "money")``. The workspace
    root is always ``{nextcloud_mount}/{get_user_bot_path(...)}``.

    Pass ``conn`` to reuse an existing framework-DB connection for the
    module-enabled check (hot scheduler loops).
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )
    db_override = module_loader.resolve_module_db_path(
        istota_config, user_id, MODULE,
    )
    ctx = synthesize_user_context(workspace, db_path=db_override)
    # Operator gate on the portfolio module's third-party ticker lookup.
    money_cfg = getattr(istota_config, "money", None)
    ctx.autoclass_lookup = bool(getattr(money_cfg, "autoclass_lookup", True))
    # Lazy import — _migrate imports config_store, which imports model
    # dataclasses. Keeping the import here avoids a startup-time cost when
    # the module isn't enabled for any user.
    from istota.money._migrate import ensure_initialised  # noqa: PLC0415
    ensure_initialised(ctx)
    return ctx


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """List istota usernames with the money module enabled.

    Pass ``conn`` to reuse an existing framework-DB connection.
    """
    return module_loader.list_module_users(istota_config, MODULE, conn)
