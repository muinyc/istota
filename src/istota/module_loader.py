"""What the five module loaders answer identically: is this user's module on,
and where does its workspace live.

``briefings``, ``feeds``, ``health``, ``location`` and ``money`` each expose
``resolve_for_user`` and ``list_users``, and each of the five carried its own
copy of the same four refusals in the same order — no config, module disabled,
user unknown, no mount — followed by the same ``{mount}/{get_user_bot_path(...)}``
derivation and the same ``module_db_path`` lookup. The bodies agreed on every
message string; only the module name differed. What each loader actually owns is
what comes *after* that: feeds reads a Tumblr key, money runs
``ensure_initialised``, health replaces ``framework_db_path``, briefings passes
the configured briefing names through. Those stay where they are.

**The exception type is a parameter, and that is the point rather than a
detail.** Each module keeps its own ``UserNotFoundError`` class, and this module
raises the one it is handed, so ``except istota.health._loader.UserNotFoundError``
still catches exactly what it caught before and nothing more. A single shared
class re-exported five ways would have widened every one of those clauses to the
other four modules' failures — cheap to write, invisible until a caller wrapped
two modules in one ``try``. The five classes do subclass
:class:`UserNotFoundError` below, which widens nothing existing and gives a
caller that wants the whole family one name to ask for.

**The user-presence check is the membership form**, ``user_id not in
istota_config.users``. Three of the five spelled it ``get_user(user_id)`` and
tested the result for truthiness; on a ``dict[str, UserConfig]`` of plain
dataclasses the two are the same question, and the membership form is the one
that does not depend on a method. ``briefings`` still calls ``get_user`` for its
own reason — it needs the ``UserConfig`` afterwards — and by then membership has
already been established here.

The equivalence has one input it does not hold on, and it is worth naming rather
than implying: with ``users`` set to ``None``, ``get_user`` raises
``AttributeError`` where the membership form refuses with the module's own error.
No producer seeds that field with ``None`` — every write into it stores a real
``UserConfig`` — so this is unreachable rather than merely unlikely, and the
membership form is the better of the two answers anyway.

Stdlib-only leaf: ``pathlib`` and nothing else at module scope. The storage
helper is imported inside the function, as all five copies already did, because
``istota.storage`` pulls in the package.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


class UserNotFoundError(Exception):
    """Base for the five modules' own ``UserNotFoundError`` classes.

    Never raised by name from here: :func:`resolve_module_workspace` raises
    whatever ``error`` it was given, which is the calling module's own subclass,
    and that argument is **required** for the reason this class exists. Nothing
    in the tree catches this base — all twenty-odd handlers name one module's own
    class — so a loader that omitted the argument would raise something no caller
    can catch, which is a worse failure than the duplication being removed.
    """


def resolve_module_workspace(
    istota_config,
    user_id: str,
    *,
    module: str,
    conn: "sqlite3.Connection | None" = None,
    error: type[Exception],
) -> Path:
    """The user's workspace root for ``module``, or ``error`` saying why not.

    The four refusals, in the order every loader had them: the config is not
    loaded, the module is opted out for this user, the user is not in the
    config, the deployment has no Nextcloud mount. ``conn`` is threaded through
    to ``is_module_enabled`` so a hot scheduler loop can reuse one framework-DB
    connection instead of opening one per user.
    """
    if istota_config is None:
        raise error("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, module, conn=conn):
        raise error(f"{module} module disabled for '{user_id}'")

    if user_id not in (getattr(istota_config, "users", None) or {}):
        raise error(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise error(
            f"{module} module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path  # noqa: PLC0415 - import cycle

    return Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")


def get_module_user(istota_config, user_id: str, *, error: type[Exception]):
    """The user's ``UserConfig``, or ``error`` with the same refusal as above.

    One caller: ``briefings`` is the only loader that needs the value rather
    than just the fact, and it dereferences ``uc.briefings``. It lives here so
    the refusal string stays spelled in exactly one file — a loader that
    re-spelled it would be the start of the drift this module removed, and the
    guard in ``tests/test_module_loader.py`` refuses it for that reason.

    The membership test :func:`resolve_module_workspace` already ran does not
    make this redundant: that one establishes the key is present, this one
    establishes the value is usable, and they are different failures. It costs a
    dict lookup.
    """
    uc = getattr(istota_config, "get_user", lambda _uid: None)(user_id)
    if uc is None:
        raise error(f"user '{user_id}' not in istota config")
    return uc


def resolve_module_db_path(istota_config, user_id: str, module: str):
    """Where this module's per-user DB lives, or ``None`` for the default.

    The DB is on local disk (WAL-safe) while the workspace files stay on the
    mount. ``module_db_path`` is read through ``getattr`` and checked with
    ``callable`` because every one of the five copies did: a config object
    without the resolver is a shape the loaders already tolerate.
    """
    resolver = getattr(istota_config, "module_db_path", None)
    if callable(resolver):
        return resolver(user_id, module)
    return None


def list_module_users(
    istota_config,
    module: str,
    conn: "sqlite3.Connection | None" = None,
) -> list[str]:
    """Istota usernames with ``module`` enabled, in config order.

    Pass ``conn`` to reuse an existing framework-DB connection — without it,
    every per-user check opens a fresh sqlite connection.
    """
    if istota_config is None:
        return []
    return [
        uid for uid in (getattr(istota_config, "users", None) or {})
        if istota_config.is_module_enabled(uid, module, conn=conn)
    ]
