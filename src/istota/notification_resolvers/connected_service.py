"""A connected third-party service whose stored credential stopped working.

Two services: Garmin, and the user-scoped Nextcloud OAuth pair (ISSUE-333).

Garmin. A six-hourly sync hits an auth error,
`garmin.mark_token_error` wipes the OAuth blob so the settings card stops
claiming "Connected", and nothing at all is pushed — the "row, no push" half of
the inventory. Worse than merely silent: `health/jobs.py` renders the sync job
only for a user who *has* stored tokens, so the scheduler's idempotent sync pass
deletes the job row on its next tick and the failure stops recurring. The row
has to be written at the moment of the error, because there will not be a second
one.

Nextcloud. The web login's OAuth pair is deleted by a refresh rejection or by a
key rotation, and the login callback is the only thing that ever writes it — so,
exactly like Garmin, the moment of the error is the only moment anything knows.
`web_tokens.note_credential_lost` writes and delivers; `web_tokens.store_tokens`
closes on the next successful login, and the settings Disconnect handler closes
on a deliberate teardown.

**`object_id` is a service name, not an integer**, so it gets the explicit
segment check the spec asks for in place of the `int()` coercion the other
sources use: only a name in `SERVICES` below is ever rendered. The reconnect
link is now per-service rather than the one fixed path it began as, but it is
still **code-owned** — `RECONNECT_HREFS` is a literal table keyed by the same
allowlist, so `object_id` selects a path and never supplies one, and an
unknown name is refused before the lookup. `SAFE_PATH_RE` remains the runtime
backstop behind that rather than the check.

The consequence sentence and the remedy sentence are per service too, because
the two services fail differently: Garmin stops collecting data, while Nextcloud
loses nothing and silently changes how two features behave. A warning that
describes the wrong failure is worse than a generic one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "connected_service"

# `secret`, matching the schema comment: the object being watched is the stored
# credential, not the remote account.
OBJECT_TYPE = "secret"

# Only the user can fix either of these, and both get worse the longer they
# stand — Garmin loses data, Nextcloud misattributes messages the user wrote.
SEVERITY = "warning"

# Where "Reconnect" goes, per service. A frontend-relative path, not an API one;
# `NotificationItem` renders it as `{base}{href}`, and `SAFE_PATH_RE` is enforced
# on it at runtime on every view.
#
# Garmin's is the settings page, which mounts its card
# (`web/src/routes/settings/+page.svelte` → `GarminCard`) and holds the whole
# reconnect flow inline. Nextcloud's is not: its reconnect *is* an OAuth
# authorize round trip, and the settings card is where the user was already told
# to log out and back in — linking there returns them to the mystery rather than
# to the remedy. `/reconnect` is the auth route that runs the flow and comes back
# with the session intact (ISSUE-333).
RECONNECT_HREFS: dict[str, str] = {
    "garmin": "/settings",
    "nextcloud": "/reconnect",
}

# Retained as the fallback for a service with no entry above, and because it is
# the spelling the original single-service version of this module exported.
RECONNECT_HREF = "/settings"

# The allowlist that stands in for `int()` here. Keys are the `object_id` values
# this source may carry; the value is what the user calls the service.
SERVICES: dict[str, str] = {
    "garmin": "Garmin Connect",
    "nextcloud": "Nextcloud",
}

# What stops working, per service, in the user's own terms. Garmin's is a sync
# that stops and data that stops arriving; Nextcloud's is neither — nothing is
# lost and nothing stops, two features silently change behaviour. Saying "no new
# data is coming in" there would be false, and a warning that describes the wrong
# failure is worse than a generic one.
_CONSEQUENCE: dict[str, str] = {
    "garmin": (
        "The stored {label} credentials were rejected, so syncing has stopped "
        "and no new data is coming in."
    ),
    "nextcloud": (
        "The stored {label} connection was lost, so messages you send from web "
        "chat are being reposted by the bot instead of appearing under your own "
        "name, and read state is no longer syncing with Talk."
    ),
}

_DEFAULT_CONSEQUENCE = (
    "The stored {label} credentials were rejected, so syncing has stopped "
    "and no new data is coming in."
)

# The closing sentence, per service — the remedy differs, so the instruction has
# to as well.
_REMEDY: dict[str, str] = {
    "garmin": "Reconnect under Settings → Connected services.",
    "nextcloud": "Reconnect to restore both — it takes one round trip and keeps you signed in.",
}

_DEFAULT_REMEDY = "Reconnect under Settings → Connected services."

# `notification_store._STALE_SWEEP_BUSY_TIMEOUT_MS`, for the same reason: see
# :func:`close_for_service`.
_CLOSE_BUSY_TIMEOUT_MS = 2000


def dedup_key(service: str) -> str:
    """``service:{name}``.

    The prefix is deliberately not ``OBJECT_TYPE``, unlike the integer-keyed
    sources: the object type is ``secret``, because what is being watched is the
    stored credential, while the key names the service the user reconnects.
    """
    return _common.object_dedup_key("service", service)


def label_for(service: str) -> str:
    return SERVICES.get(service, service or "a connected service")


def title_for(service: str) -> str:
    return f"{label_for(service)} needs to be reconnected"


def reconnect_href(service: str) -> str:
    return RECONNECT_HREFS.get(service, RECONNECT_HREF)


def body_for(service: str, reason: str = "") -> str:
    """The stored body, which is also what the push says."""
    from ..confirmations import flatten

    label = label_for(service)
    detail = flatten(reason or "")[:200]
    lead = _CONSEQUENCE.get(service, _DEFAULT_CONSEQUENCE).format(label=label)
    if detail:
        lead += f" ({detail})"
    return f"{lead} {_REMEDY.get(service, _DEFAULT_REMEDY)}"


def _row_kwargs(service: str, reason: str) -> dict:
    """The row itself, spelled once for all three entry points below."""
    return _common.row_kwargs(
        source=SOURCE,
        dedup_key=dedup_key(service),
        title=title_for(service),
        body=body_for(service, reason),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=service,
        params={"service": service, "reason": reason},
    )


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    service: str,
    reason: str = "",
) -> "RaiseResult | None":
    """Write the row on the caller's connection, inside its transaction."""
    from ..notification_store import write_notification

    return write_notification(conn, user_id, **_row_kwargs(service, reason))


def raise_for_service(
    config: "Config", user_id: str, service: str, reason: str = "",
) -> int | None:
    """Write **and** deliver, on a connection of the store's own.

    `raise_notification` rather than the buffered pair, and the reason is the
    one its docstring demands be named. `sync_garmin` holds
    no framework-DB transaction at any of its three call sites. It reaches the
    framework DB only through `secrets_store`, and every one of those helpers
    opens and closes a connection of its own around a single statement — there
    is no open write lock for a second connection to wait thirty seconds on. The
    second caller, `web_tokens.note_credential_lost`, reaches the framework DB
    only through that module's own short-lived `_connect`, and calls this
    *outside* the per-user refresh lock for the neighbouring reason: this
    delivers, and holding a lock across an outbound HTTP call would serialize
    every other token read for that user behind it.

    Never raises: the whole point of this source is a sync that failed, and the
    failure must not turn into a traceback out of the sync engine. The guard is
    here rather than only in the store because `_row_kwargs` is evaluated in
    *this* frame, outside the store's own.
    """
    try:
        from ..notification_store import raise_notification

        return raise_notification(config, user_id, **_row_kwargs(service, reason))
    except Exception:
        logger.warning(
            "could not raise the %s notification for %r", service, user_id,
            exc_info=True,
        )
        return None


def write_for_service(
    db_path: "Path", user_id: str, service: str, reason: str = "",
) -> None:
    """Write the row and deliver nothing, on a connection of this call's own.

    For the skill-CLI leg of `sync_garmin`: a short-lived host-side process the
    skill proxy spawns, where `send_notification`'s Talk and ntfy fan-out does
    not belong. The *row* still has to be written there — this source exists
    because a Garmin auth failure removes the job that would have noticed it
    again, so "no config, no row" would leave the one process that saw the
    failure saying nothing at all. Same split the email skill CLI takes for a
    held outbound draft.

    Never raises, for the reason above.
    """
    try:
        from .. import db
        from ..notification_store import write_notification

        with db.get_db(db_path) as conn:
            write_notification(conn, user_id, **_row_kwargs(service, reason))
    except Exception:
        logger.warning(
            "could not write the %s notification for %r", service, user_id,
            exc_info=True,
        )


def resolve_for_service(
    conn: "sqlite3.Connection", user_id: str, service: str, *, by: str,
) -> int:
    """Close the row for a service whose credentials are working again."""
    return _common.resolve_for(
        conn, user_id, SOURCE, OBJECT_TYPE, service, by=by,
    )


def close_for_service(db_path: "Path", user_id: str, service: str, *, by: str) -> None:
    """`resolve_for_service` for a caller that holds no connection at all.

    The two close sites are `garmin.store_tokens` and `garmin.clear_tokens`,
    which reach the framework DB only through `secrets_store` — see
    :func:`raise_for_service` for why opening a connection there is safe. Never
    raises: closing an inbox row must not be able to fail a reconnect.
    """
    try:
        from .. import db

        # A short lock budget rather than `get_db`'s 30-second default. This runs
        # from `store_tokens`, which `acquire_client` calls while holding a
        # host-local exclusive flock, so a close that queued behind an unrelated
        # writer would hold that lock for half a minute. Losing the race is the
        # case the resolver backstop covers: the row goes `stale` on the next
        # panel read instead of `resolved` now.
        with db.get_db(db_path, busy_timeout_ms=_CLOSE_BUSY_TIMEOUT_MS) as conn:
            resolve_for_service(conn, user_id, service, by=by)
    except Exception:
        logger.warning(
            "could not close the %s notification for %r",
            service, user_id, exc_info=True,
        )


class ConnectedServiceResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from ..notification_sources import NotificationAction, NotificationView

        service = str(row.object_id or "").strip()
        if service not in SERVICES:
            logger.warning(
                "notification %s names an unknown service %r", row.id, row.object_id,
            )
            return None

        if _is_connected(config, row.user_id, service):
            return None

        reason = ""
        if isinstance(row.params, dict):
            reason = str(row.params.get("reason") or "")

        return NotificationView(
            title=title_for(service),
            body=body_for(service, reason),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="reconnect", label="Reconnect", kind="primary",
                    method="LINK", href=reconnect_href(service),
                ),
            ),
        )


def _is_connected(config: "Config", user_id: str, service: str) -> bool:
    """Whether the stored credential is usable again.

    Reads the framework DB through the service's own status helper rather than
    the panel's connection: `secrets_store` opens its own, and it is the only
    thing that knows how to decrypt. It answers `{}` — and so this answers
    False — when `ISTOTA_SECRET_KEY` is out of scope, which is the safe
    direction: an unreadable store leaves the row open rather than closing a
    warning nobody has acted on.
    """
    db_path = getattr(config, "db_path", None)
    if db_path is None:
        return False
    if service == "garmin":
        from ..health import garmin

        return bool(garmin.get_status(db_path, user_id).get("connected"))
    if service == "nextcloud":
        # `token_status` reads the row without decrypting, so this answers
        # correctly in a process that has no `ISTOTA_WEB_TOKEN_KEY` — the panel
        # is served by the web unit, which does, but the resolver is also reached
        # by the store's liveness sweep and must not depend on that. A row that
        # is present but undecryptable reads as connected here and is deleted by
        # the next `get_access_token`, which raises the row again.
        from .. import web_tokens

        return web_tokens.token_status(db_path, user_id) is not None
    return False


RESOLVER = ConnectedServiceResolver()
