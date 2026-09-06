"""Resolve a user's :class:`HealthContext` from istota's config.

Single entry point for the web routes, scheduler hooks, and the CLI/skill
facade. Mirrors :mod:`istota.location._loader` and :mod:`istota.feeds._loader`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from istota import module_loader
from istota.health.models import HealthContext
from istota.health.workspace import synthesize_health_context

MODULE = "health"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable health configuration."""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> HealthContext:
    """Build a health context for ``user_id``.

    Raises :class:`UserNotFoundError` if the config is missing, the user is
    unknown, the module is opted out, or the Nextcloud mount path is
    unset.

    Pass ``conn`` to reuse an existing framework-DB connection for the
    module-enabled check.
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )

    db_override = module_loader.resolve_module_db_path(
        istota_config, user_id, MODULE,
    )

    framework_db = getattr(istota_config, "db_path", None)
    ctx = synthesize_health_context(user_id, workspace, db_path=db_override)
    if framework_db:
        from dataclasses import replace
        ctx = replace(ctx, framework_db_path=Path(framework_db))
    return ctx


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the health module enabled."""
    return module_loader.list_module_users(istota_config, MODULE, conn)
