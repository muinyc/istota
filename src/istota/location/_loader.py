"""Resolve a user's :class:`LocationContext` from istota's config.

Single entry point for the webhook receiver, scheduler hooks, web routes,
and the location skill's ``setup_env`` hook. Mirrors
:mod:`istota.feeds._loader` and :mod:`istota.money._loader`.

Location is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "location")``. The user's workspace
path is derived from ``nextcloud_mount_path`` + ``get_user_bot_path``.
"""

from __future__ import annotations

import sqlite3

from istota import module_loader
from istota.location.models import LocationContext
from istota.location.workspace import synthesize_location_context

MODULE = "location"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable location configuration."""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> LocationContext:
    """Build a location context for ``user_id``.

    Raises :class:`UserNotFoundError` if the config is missing, the user
    is unknown, the location module is opted out, or the Nextcloud mount
    path is unset.

    Pass ``conn`` to reuse an existing framework-DB connection for the
    module-enabled check (hot scheduler loops).
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )

    return synthesize_location_context(
        user_id,
        workspace,
        db_path=module_loader.resolve_module_db_path(
            istota_config, user_id, MODULE,
        ),
    )


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the location module enabled.

    Pass ``conn`` to reuse an existing framework-DB connection — without
    it, every per-user check opens a fresh sqlite connection.
    """
    return module_loader.list_module_users(istota_config, MODULE, conn)
