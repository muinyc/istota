"""Resolve a user's :class:`FeedsContext` from istota's config.

Single entry point for the web routes, scheduler hooks, and the CLI/skill
facade. Mirrors :mod:`istota.money._loader`.

Feeds is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "feeds")``. The user's workspace path is
derived from ``nextcloud_mount_path`` + ``get_user_bot_path``; per-user
overrides (``data_dir``, ``db_path``, …) and the Tumblr API key live in the
encrypted secrets table once Phase 2 of the refactor lands. For now the
loader still consults the secrets table as the only source of
``tumblr_api_key``.
"""

from __future__ import annotations

import os
import sqlite3

from istota import module_loader
from istota.feeds.models import FeedsContext
from istota.feeds.workspace import synthesize_feeds_context

MODULE = "feeds"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable feeds configuration."""


def _read_credential(
    istota_config, user_id: str, service: str, key: str, env_var: str,
) -> str:
    """Env-first credential read.

    In subprocess context (Phase 1.4+), the executor pre-resolves
    credentials via build_skill_env and injects them through the skill
    proxy — the env var is set; the master Fernet key is not in os.environ
    so secrets_store would silently fail anyway.

    In trusted-daemon context (scheduler-internal calls like
    _sync_feeds_module_jobs that enumerate users without spawning a task)
    the env var is unset but ISTOTA_SECRET_KEY is present, so the
    secrets_store fallback works.

    Empty strings are treated as unset to match _resolve_env_spec.
    """
    val = os.environ.get(env_var)
    if val:
        return val
    if istota_config is None:
        return ""
    try:
        from istota import secrets_store  # noqa: PLC0415

        db_path = getattr(istota_config, "db_path", None)
        if db_path is None:
            return ""
        stored = secrets_store.get_secret(db_path, user_id, service, key)
        return stored or ""
    except Exception:  # noqa: BLE001
        return ""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> FeedsContext:
    """Build a feeds context for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "feeds")``. The workspace
    root is always ``{nextcloud_mount}/{get_user_bot_path(...)}``.

    Pass ``conn`` to reuse an existing framework-DB connection for the
    module-enabled check (hot scheduler loops).
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )

    tumblr_api_key = _read_credential(
        istota_config, user_id, MODULE, "tumblr_api_key", "TUMBLR_API_KEY",
    )

    return synthesize_feeds_context(
        user_id,
        workspace,
        tumblr_api_key=tumblr_api_key,
        db_path=module_loader.resolve_module_db_path(
            istota_config, user_id, MODULE,
        ),
    )


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the feeds module enabled.

    Pass ``conn`` to reuse an existing framework-DB connection.
    """
    return module_loader.list_module_users(istota_config, MODULE, conn)
