"""A scheduled job the scheduler switched off after N consecutive failures.

The first of the three *silent* gaps: three sites in `scheduler.py` disable a
job, write a `task_logs` warning, and tell nobody. A module-prefixed job gets
rescued on a later sweep; a user's own CRON.md job just stops running, and the
first sign of it is a briefing that never arrives.

**The close predicate is `auto_disabled_at IS NULL`** — the daemon's own
column, which is the one that says whether this job is being held back. It is
not `enabled`: that is the user's intent, authored by CRON.md, and a job the
user has switched off by hand was never the condition this row is about.

Three things lift a suspension and each of them genuinely ends the condition: a
successful run (`reset_scheduled_job_failures`), `!cron enable`
(`db.enable_scheduled_job`), and an edit in CRON.md to what the job dispatches,
which `sync_cron_jobs_to_db` reads as the user fixing it. That third path is
worth knowing about here, because it is the one that closes the row with no
surface having been touched. A deleted job has no row to read at all.

**A `_module.*` row takes a different predicate, and that is the whole of
ISSUE-391.** There is a fourth writer of `auto_disabled_at = NULL` that reaches
only those rows: the rescue arm in `_sync_module_jobs`, which lifts the
suspension on a cooldown whether or not anything was fixed. On a module row the
column therefore says nothing about the condition, so reading it as a close
turned every suspension into a row that went `stale` within the hour and was
*reopened* by the next one — and a reopen delivers. The old answer was to
notify about module jobs not at all (`should_notify`), which left a job failing
on every run with no signal anywhere: `_sync_module_jobs` also seeds every
module row `skip_log_channel = 1`, so nothing reached the log channel either,
and the only trace was `scheduled_jobs.last_error`.

What a module row closes on instead is a **success since the row was raised** —
`last_success_at`, whose one writer is `reset_scheduled_job_failures`. It is the
one column the rescue does not touch, and "since" rather than "ever" is
load-bearing: after a rescue the row is back to zero failures, a null
`auto_disabled_at` and a null `last_error`, so a job that worked for months
before it broke is indistinguishable from a healthy one on every other column.
The comparison goes to SQLite because the two timestamps are written in
different formats (`notifications.created_at` is ISO-8601 with `T` and `Z`,
`scheduled_jobs.last_success_at` is `datetime('now')`), and lexicographic
ordering across the two is simply wrong. It is measured against `updated_at`,
not `created_at`: a reopen preserves `created_at`, so an old success would close
the reopened row on its first panel read.

That holds the row open across the loop, which is what makes the notification
safe rather than what makes it noisy: the first suspension inserts and delivers,
and every later one finds the row still open and *bumps* it, which does not
deliver. One push per outage, not one per rescue cycle.

**Open is not the only state, and the resolver only ever sees rows that are.**
A closed row is never handed to a resolver, so the paragraph above holds right
up until the user presses Dismiss — after which `write_notification` reads the
row as `reopening` and delivers, once per rescue cycle, forever. `write` closes
that with the same question the resolver asks (`_same_outage_already_closed`):
a re-suspension with no success since the row was last written is the outage
already seen, so nothing is written at all. A success followed by a fresh
failure is a new outage and still reopens and delivers.

Two consequences of the predicate, both accepted rather than overlooked. A
module row closes only on a success or an explicit `!cron disable`, so a job
that can never run again — its module extra uninstalled, or `resolve_for_user`
raising so `_sync_module_jobs` never reaches its rescue — keeps an open row
saying exactly that, which is true and is the thing worth being told. And the
backstop close writes `stale` where the producer's own `resolve_for_job` writes
`resolved`, so one condition ends in two states depending on which path won;
nothing branches on that beyond open/closed, and it is recorded here rather than
reconciled.

There is no HTTP endpoint that re-enables a job — the verb is `!cron enable
<name>` in chat, and the panel does not send chat commands. So the view carries
no actions and says what to do in its `status_note`, which is the case that
field exists for. The row is still stored `actionable=1`, per the spec's source
table: something *is* waiting on the user. `list_open` renders it as
`actionable=False` (`row.actionable and bool(view.actions)`), so it lands under
"All" and not under "Needs action", which is right — a filter promising things
to act on should not list one with no button. The consequence to know about is
that `counts()` is plain SQL over the stored column, so its `actionable` figure
counts these and the panel's does not. Nothing renders that figure today (the
bell shows `open`, and the tab labels come from the list response), which is why
it is recorded here rather than reconciled.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "cron_job"
OBJECT_TYPE = "scheduled_job"

# A job that has stopped running is a warning, not a failure of the system: the
# user's other jobs are unaffected and nothing was lost.
SEVERITY = "warning"

# How much of the last error survives into the body. `last_error` is the failed
# task's own result text, which on a prompt job is model-authored, so it is
# flattened as well as cut.
_ERROR_CHARS = 300

# How much of a job name survives into the title. A job name is free text out of
# CRON.md, and the title is stored as `notifications.title` and handed to
# `send_notification(title=…)`, which reaches ntfy as an HTTP header — an
# oversized one is refused by the server and the push is lost with
# `last_delivered_at` correctly null and nothing saying why. Same cap and same
# reasoning as `confirmations.describe_email`. `flatten` does not truncate; the
# caller always does.
_NAME_CHARS = 80

# `cron_loader._MODULE_JOB_PREFIX`, re-spelled rather than imported: this module
# must stay cheap to import from a daemon hot path, and the thing it names is a
# private constant either way. Four things branch on it and `should_notify` is
# no longer one of them (ISSUE-391): `CronJobResolver.resolve` for the close
# predicate, `_same_outage_already_closed` for whether to write at all,
# `title_for` / `body_for` for the wording, and `note_for` for the verb.
MODULE_JOB_PREFIX = "_module."


def dedup_key(job_id: int | str) -> str:
    """``job:{id}``.

    The prefix is deliberately not ``OBJECT_TYPE`` (``scheduled_job``): this is
    the spelling already in the table, and the key is what idempotency is built
    on, so it is not free to tidy.
    """
    return _common.object_dedup_key("job", job_id)


def title_for(job_name: str, fail_count: int) -> str:
    """The one-line label. One spelling for the producer and the resolver.

    A module job does not say "switched off", because within the hour it is not:
    `_sync_module_jobs` lifts the suspension whether or not anything was fixed.
    The count is what is true either way.
    """
    from ..confirmations import flatten

    name = flatten(job_name or "")[:_NAME_CHARS] or "a scheduled job"
    if is_module_job(job_name or ""):
        return f"Scheduled job '{name}' has failed {fail_count} times in a row"
    return f"Scheduled job '{name}' was switched off after {fail_count} failures"


def body_for(job_name: str, cron_expression: str, last_error: str | None) -> str:
    """The panel body, and — via `_delivery_text` — the push text.

    **That second reader is why the module branch exists.** `status_note` reaches
    the panel alone, so a correction made only there never gets to the person who
    only ever sees the push, which is the surface ISSUE-391 was filed about. So
    the module note is appended here rather than left to `note_for`: "will not
    run again" is false for a row the rescue arm puts back within the hour, and
    the verb that does something (`!cron disable`) has to travel with the alert
    that provokes it — a push saying only that the job stopped invites the user
    to dismiss it, and a dismissed row is the one shape that would deliver again
    on the next suspension (see :func:`write`).
    """
    from ..confirmations import flatten

    name = flatten(job_name or "")[:_NAME_CHARS] or "the job"
    cron = flatten(cron_expression or "")[:_NAME_CHARS]
    error = flatten(last_error or "")[:_ERROR_CHARS]
    tail = f"Last error: {error}" if error else "No error text was recorded."

    if is_module_job(job_name or ""):
        lead = f"'{name}' failed on every attempt"
        lead += f" on its {cron} schedule." if cron else "."
        # `note_for` is the single spelling of the retry sentence and the verb;
        # the panel gets it again as `status_note`, which is harmless.
        return f"{lead} {tail} {note_for(job_name)}"

    lead = f"'{name}' failed on every attempt and will not run again"
    lead += f" on its {cron} schedule." if cron else "."
    return f"{lead} {tail}"


def is_module_job(job_name: str) -> bool:
    """Whether this job belongs to a module rather than to the user's CRON.md.

    Tested against the **raw** name, never a flattened one: `flatten` maps `_` to
    a space (it is a markdown emphasis character), so `_module.health.garmin_sync`
    flattens to something starting with `module.` and the check would never fire.
    """
    return (job_name or "").startswith(MODULE_JOB_PREFIX)


def should_notify(job_name: str) -> bool:
    """Whether a suspension of this job is worth telling the user about.

    Every suspension is (ISSUE-391). This used to exclude `_module.*` on two
    premises, and both have since gone.

    The first was the rescue-reopen loop: a row raised on the suspend, marked
    `stale` once the hourly rescue cleared `auto_disabled_at`, then reopened by
    the next suspension — and a reopen delivers. That is now closed at its
    source, in the resolver, which no longer reads a module row's
    `auto_disabled_at` as a close; the row stays open across the loop and the
    later suspensions bump it instead. See the module docstring.

    The second was that the user had no verb to run against a module job, which
    ISSUE-392 retired: `!cron enable` and `!cron disable` both accept a
    `_module.` name and both persist, since for a module row the table is the
    only authority there is.

    What is left is a job that fails on every run and tells nobody, which is
    worth a notification precisely because nothing else carries it —
    `_sync_module_jobs` seeds these rows `skip_log_channel = 1`, so the log
    channel is silent too. A module failure with an actionable cause still
    raises its own more specific source in parallel (a dead Garmin credential
    raises `connected_service`); this is the backstop for the ones that do not,
    where the alternative is `_module.feeds.prune` failing daily for a month
    while the feeds database grows without bound.
    """
    return True


# What a `_module.*` job name is allowed to contain before it is printed beside
# a verb. Unlike a CRON.md name it is code-generated — `_module.<module>.<job>`,
# assembled from a module's own `MODULE_PREFIX` and its `jobs.py` — so it can be
# named verbatim rather than dropped. It has to be: `flatten` deletes `_`, so
# `safe == raw` below is False for every name starting `_module.`, and the
# generic path would print `<name>` for all of them.
_MODULE_NAME_RE = re.compile(r"\A_module\.[A-Za-z0-9._-]+\Z")


def note_for(job_name: str) -> str:
    """Why the panel has no button, and what to do instead.

    Split from `body_for` because the body is also the push text, and a push
    that lands in Talk is already on the surface where `!cron enable` is typed.

    A module job gets a different note because it gets different treatment from
    the daemon: `_sync_module_jobs` puts it back within the hour on its own, so
    "re-enable it" describes something that has already happened and names the
    wrong verb. The one that changes anything is `!cron disable`, which stops
    the retry loop; `disabled_at` is what makes it stick (ISSUE-392).
    """
    from ..confirmations import flatten

    raw = job_name or ""
    if is_module_job(raw):
        named = f"`!cron disable {raw}`" if _MODULE_NAME_RE.match(raw) else (
            "`!cron disable <name>`"
        )
        return (
            "The scheduler retries this one on its own, so it will keep "
            f"failing until the cause is fixed. To stop the retries, {named}."
        )
    # The job is named only when flattening left it untouched. A job name is
    # user-authored free text out of CRON.md and this note is delivered into
    # Talk, which renders markdown — but a *flattened* name is no longer the
    # string `!cron enable` takes, so printing it would be an instruction that
    # does not work. Either the name survives verbatim or it is not named.
    safe = flatten(raw)
    verb = f"`!cron enable {safe}`" if safe and safe == raw else "`!cron enable <name>`"
    return f"Fix what it was failing on, then re-enable it with {verb}."


def _same_outage_already_closed(
    conn: "sqlite3.Connection", user_id: str, job_id: int,
) -> bool:
    """Is this suspension the outage the user has already seen and closed?

    **The hole the resolver alone does not plug.** Holding the row open makes a
    re-suspension a bump, and a bump does not deliver — but only while the row
    is open. `dismiss` writes `state='dismissed'`, `write_notification` reads
    any non-open state as `reopening`, and a reopen *delivers*. On a CRON.md job
    that is harmless, because a suspended job never fires again and so can never
    re-suspend; on a `_module.*` job the rescue arm guarantees it will, roughly
    hourly, forever. So the user's own "clear this" turns one push into the
    recurring push the old prefix exclusion existed to prevent — reintroduced
    through the one door the resolver cannot reach, since a closed row is never
    handed to a resolver at all.

    The predicate is deliberately *not* "was it dismissed". It is the same
    question the resolver asks — has the job succeeded since we last wrote about
    it — so it reads as one rule rather than as a special case for one button:
    a re-suspension with no success in between is the same outage still running,
    which is not news whatever closed the row. A success followed by a fresh
    failure is a new outage, and that reopens and delivers as it should. That
    also keeps `dismiss`'s documented "not now, not never again" true, which
    suppressing on the state alone would not.

    Never suppresses on a question it could not answer: an unreadable row or a
    failed comparison returns False and the notification is written. The cost of
    a wrong False is one extra push; the cost of a wrong True is silence.
    """
    from ..notification_store import STATE_OPEN

    try:
        found = conn.execute(
            "SELECT state, updated_at FROM notifications "
            "WHERE user_id = ? AND source = ? AND dedup_key = ?",
            (user_id, SOURCE, dedup_key(job_id)),
        ).fetchone()
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not read the existing row for job %s; writing anyway",
            job_id, exc_info=True,
        )
        return False
    if found is None:
        return False
    # Positional, not by key: every caller reaches this through `db.get_db`,
    # which sets `sqlite3.Row`, but nothing here needs that to be true.
    state, updated_at = found[0], found[1]
    if state == STATE_OPEN:
        return False
    return not _succeeded_since(conn, job_id, updated_at or "")


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    job_id: int,
    job_name: str,
    fail_count: int,
    cron_expression: str = "",
    last_error: str | None = None,
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the caller's connection, inside its transaction.

    Every one of the three producer sites sits inside `process_one_task`'s write
    transaction, so the caller buffers the result and hands it to
    `deliver_pending` after the `with` block — see the store's module docstring.

    Returns None for a module job whose current outage the user has already
    closed — see :func:`_same_outage_already_closed`, which is what stops the
    dismiss-then-re-suspend path delivering once per rescue cycle.
    """
    from ..notification_store import write_notification

    if is_module_job(job_name) and _same_outage_already_closed(
        conn, user_id, job_id,
    ):
        return None

    return write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(job_id),
            title=title_for(job_name, fail_count),
            body=body_for(job_name, cron_expression, last_error),
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=str(job_id),
            params={"job_name": job_name, "failures": int(fail_count)},
            room_token=room_token,
        ),
    )


def resolve_for_job(
    conn: "sqlite3.Connection", user_id: str, job_id: int, *, by: str,
) -> int:
    """Close the row for a job that has just recovered or been re-enabled."""
    return _common.resolve_for(
        conn, user_id, SOURCE, OBJECT_TYPE, job_id, by=by,
    )


def _job_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None."""
    return _common.coerce_object_id(row, noun="job", logger=logger)


def _succeeded_since(
    conn: "sqlite3.Connection", job_id: int, since: str,
) -> bool:
    """Has this job recorded a successful run since ``since``?

    ``since`` is a `notifications` timestamp, `strftime('%Y-%m-%dT%H:%M:%fZ')`;
    `scheduled_jobs.last_success_at` is `datetime('now')`. The two are not
    comparable as strings — `T` sorts above a space, so every success would read
    as newer than every row — so SQLite normalizes *both* through `datetime()`
    rather than Python parsing either. That lands them on one second-precision
    format, which also means a value neither side can parse comes back NULL and
    compares false: the row stays open, which is the direction to fail in.

    Second precision drops the milliseconds `%f` carries, so a success recorded
    in the same second as the row reads as *not* newer and holds the row open
    for one more cycle. That is deliberate — within a second the ordering is
    unknowable — and it costs nothing, because the producer calls
    `resolve_for_job` on the success itself and this is only the backstop.

    Guarded because this runs on the panel's render path: `list_open` sweeps the
    whole open set, and a row it cannot evaluate must not take the request down
    with it. An unanswerable question holds the row open, which is the same
    direction every other refusal in this module takes.
    """
    if not since:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE id = ? "
            "AND last_success_at IS NOT NULL "
            "AND datetime(last_success_at) > datetime(?)",
            (job_id, since),
        ).fetchone() is not None
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not compare last_success_at for job %s; holding the row open",
            job_id, exc_info=True,
        )
        return False


class CronJobResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from .. import db
        from ..notification_sources import NotificationView

        job_id = _job_id(row)
        if job_id is None:
            return None

        job = db.get_scheduled_job(conn, job_id)
        if job is None:
            # Deleted, or an orphan the CRON.md sync cleaned up. Nothing left to
            # re-enable.
            return None
        if job.user_id != row.user_id:
            logger.error(
                "notification %s belongs to %r but names %r's job %s",
                row.id, row.user_id, job.user_id, job_id,
            )
            return None
        if is_module_job(job.name):
            # `auto_disabled_at IS NULL` is not a close here: `_sync_module_jobs`
            # writes it on a cooldown as a *retry*. A success is, and it is the
            # one thing that arm does not forge. See the module docstring.
            if _succeeded_since(conn, job_id, row.updated_at):
                return None
            # The rescue has already cleared `consecutive_failures` and
            # `last_error` by the time most reads land, so recomputing the text
            # from the live job renders "failed 0 times in a row" and no error —
            # on a row whose entire purpose is to carry that error. The stored
            # text is what was true at the suspension, and `write` refreshes it
            # on every later one.
            #
            # Rendering stored text drops the flatten the recompute path applies
            # inline, which is safe here for a structural reason rather than a
            # lucky one: `UNIQUE (user_id, source, dedup_key)` plus a `job:{id}`
            # key means `write` is the only producer that can occupy this row,
            # and it composes through `title_for` / `body_for`, which flatten and
            # truncate. `notification_store._fallback` already renders stored
            # text this way for every source, so this is not a new class of
            # trust. A second producer on this key would have to flatten too.
            return NotificationView(
                title=row.title,
                body=row.body,
                severity=row.severity,
                status_note=note_for(job.name),
            )

        if job.auto_disabled_at is None:
            # The suspension lifted: a successful run, `!cron enable`, or a
            # definition edit in CRON.md. See the module docstring for why this
            # is the predicate and `enabled` is not.
            return None

        return NotificationView(
            title=title_for(job.name, job.consecutive_failures or 0),
            body=body_for(job.name, job.cron_expression, job.last_error),
            severity=row.severity,
            status_note=note_for(job.name),
        )


RESOLVER = CronJobResolver()
