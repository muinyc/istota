"""The four mechanical things every notification source said for itself.

A source owns its keys, its close predicate and its wording, and the package
docstring is emphatic that keeping those in one file per source is what makes
them checkable. None of that moves here. What moves is the part that was
identical in every file and that three of the six modules documented by pointing
at a fourth — "verbatim — see the confirmation source", which is a comment where
an import belongs, and which is exactly the shape the audit found had already
gone stale elsewhere in the tree.

Four helpers, each replacing a body that was byte-identical bar a noun:

- :func:`object_dedup_key` — the ``{prefix}:{id}`` spelling. The *prefix* stays
  in each source, because that literal is the load-bearing half: idempotency
  comes from ``UNIQUE (user_id, source, dedup_key)``, and one character of drift
  between a producer and the backfill means every held item shows twice,
  permanently, with only one of the two closable. ``task_alert`` builds its five
  keys through ``_slug`` and does not use this.
- :func:`row_kwargs` — the argument set handed to
  :func:`istota.notification_store.write_notification`. Every default here is
  that function's own default, so a source omitting a key and a source passing
  it as ``None`` are the same call.
- :func:`resolve_for` — the close, which is one ``resolve_by_object`` call in
  all five object-backed sources.
- :func:`coerce_object_id` — ``object_id`` read back as an integer.

**There is no ``link`` parameter on :func:`row_kwargs`, deliberately.** No source
passes one today, and one of them must never be able to: ``task_alert`` carries
model-authored text out of the sandbox, a ``link`` is rendered into an anchor,
and its module docstring commits to there being no branch that could emit one.
A helper that offered the field would make that a thing to keep getting right
rather than a thing that cannot happen.

**The logger is a parameter** so a warning about a malformed row is still
attributed to the source that owns the row rather than to this file. Cheap, and
it keeps the log readable when one source starts producing them.

Imports nothing from the package at module scope, matching every other module in
here — a producer on a daemon hot path imports its source module directly, and
these are cheap by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    import sqlite3

    from ..notification_sources import NotificationRow


def object_dedup_key(prefix: str, value: object) -> str:
    """``{prefix}:{value}``.

    The prefix is the source's own literal and stays in the source's own file;
    what is shared is only the separator and the coercion. See the module
    docstring for why that split is where it is.
    """
    return f"{prefix}:{value}"


def row_kwargs(
    *,
    source: str,
    dedup_key: str,
    title: str,
    severity: str,
    body: str = "",
    actionable: bool = False,
    object_type: str | None = None,
    object_id: str | None = None,
    params: dict | None = None,
    room_token: str | None = None,
    purpose: str = "alert",
) -> dict:
    """The keyword arguments for ``write_notification``, as one dict.

    A dict rather than a call, because three sources need the same argument set
    at more than one entry point — ``connected_service`` writes it three ways
    (inside a caller's transaction, through the store's own connection, and from
    a skill CLI's short-lived one) and had already factored exactly this out for
    itself.

    Every default matches ``write_notification``'s own, so a source that used to
    omit ``params`` and one that passes ``None`` produce the identical call.
    """
    return {
        "source": source,
        "dedup_key": dedup_key,
        "title": title,
        "body": body,
        "severity": severity,
        "actionable": actionable,
        "object_type": object_type,
        "object_id": object_id,
        "params": params,
        "room_token": room_token,
        "purpose": purpose,
    }


def resolve_for(
    conn: "sqlite3.Connection",
    user_id: str,
    source: str,
    object_type: str,
    object_id: object,
    *,
    by: str,
) -> int:
    """Close this user's open row for one object. Returns the rows closed."""
    from ..notification_store import resolve_by_object

    return resolve_by_object(
        conn, user_id, source, object_type, str(object_id), by=by,
    )


def coerce_object_id(
    row: "NotificationRow",
    *,
    noun: str,
    logger: "logging.Logger",
    positive: bool = False,
) -> int | None:
    """The row's ``object_id`` as an integer, or ``None`` if it is not one.

    Coerced before it is ever interpolated into a path. ``object_id`` is opaque
    TEXT, so an id of ``1/../../admin/x`` would otherwise build a
    server-supplied path the client POSTs with the session cookie. The runtime
    allowlist in ``list_open`` is the backstop behind this, not a substitute for
    it — a resolver that hands the allowlist a hostile value has already
    admitted it does not know what its own ids look like.

    ``positive`` additionally refuses ``0`` and below. One source asks for it:
    ``health_panel`` opens a *different database* on the strength of the id, and
    a panel id is an AUTOINCREMENT rowid, so a non-positive value names no panel
    that has ever existed — the row is malformed rather than stale-but-live, and
    resolving a user's health module to look one up is work done on a value
    already known to be wrong.
    """
    try:
        value = int(str(row.object_id).strip())
    except (TypeError, ValueError):
        logger.warning(
            "notification %s names a non-numeric %s id %r",
            row.id, noun, row.object_id,
        )
        return None
    if positive and value <= 0:
        logger.warning(
            "notification %s names an impossible %s id %r",
            row.id, noun, row.object_id,
        )
        return None
    return value
