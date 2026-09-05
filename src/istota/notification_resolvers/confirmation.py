"""A task parked in ``pending_confirmation`` — the inbox's first source.

Two producers write this row: the inbound email gate
(``transport/email/inbound.py``) and the scheduler's own confirmation park. Four
paths close it, all of them through ``confirmations.approve`` / ``decline``,
plus ``expire_stale_confirmations`` when the question times out.

The resolver is the backstop for all five. It returns ``None`` the moment the
task stops being ``pending_confirmation``, so a confirmation answered over Talk
can never render as still-waiting in the panel even if the close path was missed.

**The title never comes from ``tasks.prompt``.** For a gated email that column
*is* the untrusted message the gate is withholding. It comes from
``confirmations.describe``, which reads the sender and subject off
``processed_emails`` and flattens both.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "confirmation"
OBJECT_TYPE = "task"

# Held mail carries a decision the user has not made yet, so the row is
# actionable and warns rather than informs.
SEVERITY = "warning"

# The task status this source watches. Spelled once: `resolve` returning None
# means "the object is gone", and `list_open` feeds those ids straight to
# `mark_stale` — so a literal that drifted from what the tasks table actually
# stores would close every open row of this source with nothing logged anywhere.
HELD_STATUS = "pending_confirmation"

# How much of a bot-composed question survives into the notification body. The
# title already carries the one-line label; this is the rest of what was asked.
_BODY_CHARS = 400


def dedup_key(task_id: int | str) -> str:
    """``task:{id}``.

    The prefix is load-bearing and stays here: idempotency comes from
    ``UNIQUE (user_id, source, dedup_key)``, and one character of drift between a
    producer and the backfill means every held item shows twice, permanently,
    with only one of the two closable.
    """
    return _common.object_dedup_key(OBJECT_TYPE, task_id)


def body_for(confirmation_prompt: str | None) -> str:
    """The notification body for a held task: its own question, flattened.

    One rule for **both** producers, and that is the point. This source has two
    — the inbound email gate and the scheduler's mid-run park — and the row they
    write is indistinguishable afterwards: same `source`, same `object_type`,
    same `object_id`, and `source_type` is `email` on both, because an
    email-origin task whose answer asks a question parks exactly like any other
    (`.claude/rules/transport.md` records that as a deliberate decision). A
    resolver branching on `source_type` therefore renders the *gate's* wording
    over the *scheduler's* question — "nothing has been run, and the message
    body is not shown", on a task that ran to completion.

    `tasks.confirmation_prompt` is the one thing that is right in both cases: it
    is the gate's own composed message for a hold, and the model's question for
    a park. Neither is the withheld body — the gate's message carries the sender
    and subject only, which is exactly what `describe` is allowed to show — and
    flattening covers the fact that both spellings embed attacker-supplied text.
    """
    from ..confirmations import flatten

    return flatten(confirmation_prompt or "")[:_BODY_CHARS]


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    task_id: int,
    title: str,
    body: str = "",
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the producer's own connection, inside its transaction.

    Returns the :class:`RaiseResult` for the producer to buffer and hand to
    ``deliver_pending`` after its ``with`` block closes — see the store's module
    docstring for why the two are separate calls.
    """
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(task_id),
            title=title,
            body=body,
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=str(task_id),
            room_token=room_token,
        ),
    )


def resolve_for_task(
    conn: "sqlite3.Connection", user_id: str, task_id: int, *, by: str,
) -> int:
    """Close the row for a task whose question has just been answered."""
    return _common.resolve_for(
        conn, user_id, SOURCE, OBJECT_TYPE, task_id, by=by,
    )


def _task_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None if it is not one."""
    return _common.coerce_object_id(row, noun="task", logger=logger)


class ConfirmationResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from .. import confirmations, db
        from ..notification_sources import NotificationAction, NotificationView

        task_id = _task_id(row)
        if task_id is None:
            return None

        task = db.get_task(conn, task_id)
        if task is None:
            return None
        if task.user_id != row.user_id:
            # The row is already scoped to one user by the query that produced
            # it, so this can only be a producer that wrote somebody else's id.
            # Refuse rather than render: an action built from it would POST at
            # an endpoint that then correctly refuses, and the user would be
            # left pressing a button that never works.
            logger.error(
                "notification %s belongs to %r but names %r's task %s",
                row.id, row.user_id, task.user_id, task_id,
            )
            return None
        if task.status != HELD_STATUS:
            return None

        return NotificationView(
            title=confirmations.describe(conn, task),
            body=body_for(task.confirmation_prompt),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="confirm", label="Confirm", kind="primary", method="POST",
                    endpoint=f"/chat/tasks/{task_id}/confirm",
                ),
                NotificationAction(
                    id="discard", label="Discard", kind="danger", method="POST",
                    endpoint=f"/chat/tasks/{task_id}/cancel",
                ),
            ),
        )


RESOLVER = ConfirmationResolver()
