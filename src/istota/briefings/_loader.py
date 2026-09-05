"""Resolve a user's :class:`BriefingsContext` from istota's config.

Single entry point for the web routes, scheduler/executor hooks, and the
CLI/skill facade. Mirrors :mod:`istota.feeds._loader` but with no credentials —
briefings needs only paths.

Briefings is a "module": on by default for every configured user, gated by
``Config.is_module_enabled(user_id, "briefings")``. The workspace path derives
from ``nextcloud_mount_path`` + ``get_user_bot_path``; the DB relocates to
local disk via ``Config.module_db_path``. Both of those, and the four refusals
in front of them, live in :mod:`istota.module_loader` — what is here is the part
briefings owns, which is the configured briefing names.
"""

from __future__ import annotations

import sqlite3

from istota import module_loader
from istota.briefings.models import BriefingsContext
from istota.briefings.workspace import synthesize_briefings_context

MODULE = "briefings"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable briefings configuration."""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> BriefingsContext:
    """Build a briefings context for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "briefings")``. Pass ``conn``
    to reuse an existing framework-DB connection for the module-enabled check
    (hot scheduler loops).
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )

    uc = istota_config.get_user(user_id)

    return synthesize_briefings_context(
        user_id,
        workspace,
        db_path=module_loader.resolve_module_db_path(
            istota_config, user_id, MODULE,
        ),
        configured_briefing_names=tuple(
            briefing.name for briefing in uc.briefings if briefing.name
        ),
    )


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the briefings module enabled."""
    return module_loader.list_module_users(istota_config, MODULE, conn)
