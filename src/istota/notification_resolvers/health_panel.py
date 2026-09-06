"""A bloodwork panel left in `draft` after an OCR extraction nobody confirmed.

The third silent gap, and the one the store's scoping rule was written for. A
panel uploaded to `/health/bloodwork/upload` is inserted with `draft=1` and stays
there until the extracted values are reviewed and posted back with
`confirm: true`. A draft panel is excluded from the health dashboard *and* from
the biomarker trends, so a user who closes the tab mid-review has lab results in
the system that nothing will ever show them again unless they think to pass
`include_drafts=1` by hand.

**The row goes in the framework DB, keyed by the session user; the panel id
comes from the per-user health module DB.** Those are two different databases
and every user has a panel `12` in theirs. So the producer writes against
`ctx.framework_db_path`, never `ctx.db_path`, and both close paths go through
`resolve_by_object`, which takes `user_id` first-class. `idx_notifications_object`
leads with `user_id` for the same reason. Getting this wrong would mean
confirming your panel 12 closed everyone else's.

The resolver has to open the *reading* user's health DB to answer whether the
panel is still a draft, which is why resolvers receive a `Config` at all. It
resolves the module for `row.user_id` — the row's owner — and never for a value
that came from the request.

There is no one-click confirm: `POST /health/panels/{id}/biomarkers` needs the
whole reviewed biomarker list in its body, and `PUT /health/panels/{id}` is a
PUT, which the action vocabulary does not carry. So the action is a link to the
bloodwork page, where the review UI lives.
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

SOURCE = "health_panel"
OBJECT_TYPE = "health_panel"

# Nothing is broken and nothing is at risk — there is just unfinished work
# holding data out of the trends.
SEVERITY = "info"

# The bloodwork list, from which every draft panel is one click away. The panel
# page itself is `/health/bloodwork/panel?id=N`, which the URL allowlist refuses
# (no query strings, deliberately), so the list is where the link goes.
REVIEW_HREF = "/health/bloodwork"

# How much of a lab name survives into the title. `lab_name` arrives as a form
# field on the upload with no length check anywhere on that path, and the title
# is handed to `send_notification(title=…)`, which reaches ntfy as an HTTP
# header — an oversized one is refused and the push is lost with nothing saying
# why. Same cap and same reasoning as `confirmations.describe_email`.
_LAB_CHARS = 60

# `notification_store._STALE_SWEEP_BUSY_TIMEOUT_MS`; see `close_for_panel`.
_CLOSE_BUSY_TIMEOUT_MS = 2000


def dedup_key(panel_id: int | str) -> str:
    """``panel:{id}``.

    The prefix is deliberately not ``OBJECT_TYPE`` (``health_panel``): this is
    the spelling already in the table, and the key is what idempotency is built
    on, so it is not free to tidy.
    """
    return _common.object_dedup_key("panel", panel_id)


def title_for(drawn_at: str | None, lab_name: str | None) -> str:
    """The one-line label. One spelling for the producer and the resolver."""
    from ..confirmations import flatten

    lab = flatten(lab_name or "")[:_LAB_CHARS]
    when = flatten(drawn_at or "")[:10]
    who = f" from {lab}" if lab else ""
    dated = f" ({when})" if when else ""
    return f"Lab results{who}{dated} are waiting to be reviewed"


def body_for(drawn_at: str | None, lab_name: str | None) -> str:
    from ..confirmations import flatten

    lab = flatten(lab_name or "")[:_LAB_CHARS]
    when = flatten(drawn_at or "")[:10]
    bits = []
    if lab:
        bits.append(f"from {lab}")
    if when:
        bits.append(f"drawn {when}")
    which = " ".join(bits)
    lead = f"An uploaded lab report{' ' + which if which else ''} was read but"
    return (
        f"{lead} never confirmed, so it is still a draft. Draft panels are left "
        f"out of the health dashboard and out of the biomarker trends until the "
        f"extracted values are checked."
    )


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    panel_id: int,
    drawn_at: str | None = None,
    lab_name: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the caller's connection to the **framework** DB."""
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(panel_id),
            title=title_for(drawn_at, lab_name),
            body=body_for(drawn_at, lab_name),
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=str(panel_id),
            params={"drawn_at": drawn_at or "", "lab_name": lab_name or ""},
        ),
    )


def write_for_panel(
    db_path: "Path",
    user_id: str,
    *,
    panel_id: int,
    drawn_at: str | None = None,
    lab_name: str | None = None,
) -> None:
    """Write the row and deliver nothing, on a connection of this call's own.

    Takes the framework DB path rather than a `Config`, so the one thing the
    stage line insists on — `ctx.framework_db_path`, never `ctx.db_path` — is
    the value actually written to rather than a field the caller checks beside
    one it passes.

    **No delivery, deliberately.** The producer is the upload handler, and the
    user is looking at the review screen it returns them to; pushing "lab
    results are waiting to be reviewed" into Talk or ntfy at that moment is a
    notice about something they are in the middle of doing. It is written
    always, delivered never — the same split stage 2 records for a confirmation
    whose question the Talk leg already carried, and `last_delivered_at` is left
    null so a later re-delivery sweep can tell. The bell is what carries this
    one, which is the whole point of the item: what makes an abandoned draft
    panel invisible is not a missing push, it is being excluded from the
    dashboard and the trends with nowhere else to appear.

    The caller holds no write lock on the framework DB — its only open
    connection is to the health module DB, a different file with a different
    lock — so opening one here waits on nothing. Never raises: an inbox row must
    not be able to fail an upload.
    """
    try:
        from .. import db

        with db.get_db(db_path) as conn:
            write(
                conn, user_id, panel_id=panel_id,
                drawn_at=drawn_at, lab_name=lab_name,
            )
    except Exception:
        logger.warning(
            "could not write the health panel notification for %r panel %s",
            user_id, panel_id, exc_info=True,
        )


def resolve_for_panel(
    conn: "sqlite3.Connection", user_id: str, panel_id: int, *, by: str,
) -> int:
    """Close the row for a panel that has just been confirmed.

    `conn` is the **framework** DB, and `user_id` is the session user. Both are
    load-bearing: see the module docstring.
    """
    return _common.resolve_for(
        conn, user_id, SOURCE, OBJECT_TYPE, panel_id, by=by,
    )


def close_for_panel(db_path: "Path", user_id: str, panel_id: int, *, by: str) -> None:
    """`resolve_for_panel` for a caller holding only its health-module connection.

    Framework DB path in, for the reason given on :func:`write_for_panel`. A
    short lock budget rather than `get_db`'s thirty-second default: losing the
    race is the case the resolver backstop covers, and a confirmation must not
    wait half a minute on an unrelated writer. Never raises.
    """
    try:
        from .. import db

        with db.get_db(db_path, busy_timeout_ms=_CLOSE_BUSY_TIMEOUT_MS) as conn:
            resolve_for_panel(conn, user_id, panel_id, by=by)
    except Exception:
        logger.warning(
            "could not close the health panel notification for %r panel %s",
            user_id, panel_id, exc_info=True,
        )


def _panel_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as a positive integer, or None.

    Positive, unlike the other integer-keyed sources, because this is the one
    that opens a *different database* on the strength of the id. A panel id is
    an AUTOINCREMENT rowid, so `0` or a negative names no panel that has ever
    existed — the row is malformed rather than stale-but-live, and resolving a
    user's health module to look one up is work done on a value already known to
    be wrong.
    """
    return _common.coerce_object_id(
        row, noun="panel", logger=logger, positive=True,
    )


class HealthPanelResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from ..health import db as health_db
        from ..health._loader import UserNotFoundError, resolve_for_user
        from ..notification_sources import NotificationAction, NotificationView

        panel_id = _panel_id(row)
        if panel_id is None:
            return None

        try:
            ctx = resolve_for_user(row.user_id, config)
        except UserNotFoundError:
            # The module was turned off, or the mount went away. There is no
            # panel to review and no page to review it on.
            return None
        # Any *other* failure is deliberately not caught: `list_open` degrades
        # the row to its stored text and leaves it open. Returning None here
        # would close a real draft panel on a transient fault, and nothing would
        # ever raise it again — the producer fires once, at upload.

        if not ctx.db_path.exists():
            # Not "the panel is gone": the module DB being absent is a fault,
            # and returning None would close a real draft the producer will
            # never raise again — it fires once, at upload. Raising instead
            # lands in `list_open`'s per-row guard, which degrades this row to
            # its stored text and leaves it open.
            raise FileNotFoundError(ctx.db_path)

        with health_db.connect(ctx.db_path) as health_conn:
            panel = health_db.get_panel(health_conn, panel_id)
        if panel is None:
            return None
        if not panel.draft:
            # Confirmed, here or on another surface.
            return None

        return NotificationView(
            title=title_for(panel.drawn_at, panel.lab_name),
            body=body_for(panel.drawn_at, panel.lab_name),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="review", label="Review", kind="primary",
                    method="LINK", href=REVIEW_HREF,
                ),
            ),
        )


RESOLVER = HealthPanelResolver()
