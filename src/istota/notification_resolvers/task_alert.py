"""The fire-and-forget class: notices with no object and no close condition.

Every other source watches something. A held task stops being held, a draft is
sent, a cron job is re-enabled, a token is reconnected — the object changes and
the resolver sees it. Nothing will ever happen to a "the model raised a security
alert" or a "your emailed request failed" notice. So this source is the one with
``auto_resolve_on_seen = True``: the row closes when the panel is opened with it
visible, and :func:`istota.notification_store.sweep_expired_alerts` is the
backstop for rows that fell below the render limit or belong to a user who never
opens the panel.

Five producers write here, each with its own key:

===================================  ==================================
``task:{task_id}:{alert_type}``      deferred alerts the model wrote from
                                     inside the sandbox
``throttle:{kind}``                  held-mail throttle notices
``expired:{task_id}``                a confirmation that timed out
``dmarc:{verdict}``                  the inbound-mail DMARC canary
``undelivered:{task_id}``            a task result that reached nobody
===================================  ==================================

Three rules hold across all five, and each exists because of a specific way this
class can go wrong.

**No ``link``, and no ``LINK`` action. Unconditionally.** The deferred-alert
producer reads a JSON file the model wrote *from inside the sandbox*. A `link`
is rendered into an anchor, where a text-node rule buys nothing and a
``javascript:`` or off-origin URL sails through. There is no branch here that
could emit one, so there is nothing for a future edit to get wrong: `resolve`
returns ``link=None`` and an empty action tuple for every row of this source.

**Every stored title and body is flattened**, on the way in *and* on the way
out — the render path has no way to know which version of a producer wrote a
stored row. This class is the one carrying model-authored text,
``send_notification`` puts that text into Talk, and Talk renders markdown.

Titles take ``confirmations.flatten`` unchanged: a title is a one-line label,
which is exactly what that rule was written for. **Bodies take
:func:`flatten_body`, which strips less**, and the difference is deliberate. The
label rule deletes ``* _ ~`` and every newline along with the link characters,
and in a body that is not conservatism, it is corruption: a security finding
reading ``run rm -rf ~/Documents`` is delivered as ``run rm -rf /Documents`` — a
different command — and ``file_upload.py`` arrives as ``file upload.py``. The
body of an alert is where the evidence is, so it keeps every character that
cannot make a link, a code span, raw HTML or a table, and keeps its line breaks.
The spec's rule is that this text is flattened *before delivery*, and names "an
equivalent exported for the purpose" as the way to do it; this is that
equivalent.

**Every axis a key is built from is bounded.** ``alert_type`` arrives from the
model's own JSON and ``verdict`` from a parsed mail header; an unbounded axis
would mean one durable row per attacker-chosen value, each firing a push. The
alert type is narrowed to the three the producer actually distinguishes, and
every other component goes through :func:`_slug`.

**A grade that interrupts somebody has to be named.** The deferred-alert
producer has three grades and only two of them are pushed; ``note`` is written
to the table, read in the panel, and delivered nowhere. It is also what an
unrecognised or absent ``type`` falls back to, which is the inversion ISSUE-311
bought: the model authors this field, and while the fallback was ``security`` the
loudest grade in the system was the one reached by saying nothing. Nothing here
downgrades a grade the model asked for by name — an explicit ``security`` is
still ``danger``, and still pushed.
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

SOURCE = "task_alert"

# The three alert types `_process_deferred_user_alerts` distinguishes. The model
# writes this field, so anything else collapses onto one of them: honouring an
# arbitrary string would put a model-chosen value in a `dedup_key` — one durable
# row per value, per task.
#
# `note` is the quiet one, and it is also the *fallback* (ISSUE-311). Both loud
# grades are pushed, and for a long time an unrecognised or absent type landed on
# `security`, which is `danger` severity. That made the loudest grade the one a
# model reached by saying nothing — and the guideline's own example wrote the
# file with no `type` key at all, so the shape a model copies was the alarming
# one. A routine "I handed the thread back to you" arrived as a security alert.
# The rule is now the other way round: a grade that interrupts somebody has to be
# asked for by name.
ALERT_TYPE_SECURITY = "security"
ALERT_TYPE_ACTION_NEEDED = "action_needed"
ALERT_TYPE_NOTE = "note"
ALERT_TYPES = (ALERT_TYPE_SECURITY, ALERT_TYPE_ACTION_NEEDED, ALERT_TYPE_NOTE)

# The grades that reach a surface. A `note` is written to the table and read in
# the panel; nothing is pushed for it, so the producer keeps its `RaiseResult`
# out of `deliver_pending`. Named here rather than spelled as `!= ALERT_TYPE_NOTE`
# at the call sites, so the delivery decision is stated once.
#
# A fourth grade is four edits, not one: this tuple, `ALERT_TYPES`,
# `ALERT_SEVERITY`, and a branch in `scheduler_deferred._deferred_alert_title`.
# Two of those fail quietly if forgotten — a missing `ALERT_SEVERITY` entry
# raises `KeyError` into a blanket `except` that demotes the whole drain to the
# unrecorded fallback, and a missing title branch labels the grade `Note`. The
# first two are held by `test_the_type_axis_is_still_bounded`.
DELIVERED_ALERT_TYPES = (ALERT_TYPE_SECURITY, ALERT_TYPE_ACTION_NEEDED)

# What each grade is worth in the two columns the panel renders on. A row that is
# silent on the wire but still an actionable warning in the bell has moved the
# noise, not removed it.
ALERT_SEVERITY = {
    ALERT_TYPE_SECURITY: "danger",
    ALERT_TYPE_ACTION_NEEDED: "warning",
    ALERT_TYPE_NOTE: "info",
}

# The JSON array the model writes has no bound on entry count, and one row per
# entry would let a single task leave hundreds of durable rows. Entries collapse
# onto one row per `(task_id, alert_type)` and the count is capped — the same
# shape as `max_subtasks_per_task`, and for the same reason.
MAX_DEFERRED_ALERTS_PER_TASK = 20

# A `params` list is bounded for the reason the dedup key is. The DMARC canary
# fires on forged mail, so its senders are attacker-chosen: keying on one would
# be N rows, and appending every one to a JSON blob would be N entries in a
# single row instead. The *first* N are kept rather than the newest, so the
# stored value is stable across occurrences and the earliest evidence survives;
# a count of how often something was dropped rides alongside under
# `{key}_dropped`.
MAX_PARAM_ENTRIES = 20

# How much of a notice survives into the stored row. The full text of a rescued
# task result lives in `tasks.result`; this is the readable record of it.
MAX_ALERT_BODY_CHARS = 1000
MAX_ALERT_TITLE_CHARS = 160

# Why there are no buttons. `status_note` exists precisely so this is
# distinguishable from "no actions because nobody registered this source".
STATUS_NOTE = (
    "This notice has no in-app action. It clears itself once you have seen it."
)


def flatten(text: str | None) -> str:
    """The shared label flattener, applied to every stored field of this source.

    Imported function-locally: `confirmations` imports `db`, and every module in
    this package is written so a daemon hot path can import it without pulling
    the world in.
    """
    from ..confirmations import flatten as _flatten

    return _flatten(text or "")


# What a *body* may not contain, and nothing more. Each of these can turn
# model-authored text into something Talk renders as a link, a code span, raw
# HTML or a table:
#
#   [ ] ( )  inline and reference links, and images
#   < >      autolinks and raw HTML
#   `        code spans, which can hide the rest of the line
#   |        table cells
#
# Deliberately absent: ``* _ ~``, which can only produce emphasis or a strike,
# and a newline, which can produce nothing at all. The label rule takes those
# too — correctly, for a one-line label — and in a body it silently rewrites the
# evidence: ``~/Documents`` loses the home directory, ``file_upload.py`` gains a
# space, and a collapsed multi-alert body becomes one run-on line. A cosmetic
# markdown artefact in an alert body is a far cheaper error than a wrong path.
_BODY_MARKUP_CHARS = str.maketrans({c: " " for c in "[]()`<>|\r"})


def flatten_body(text: str | None) -> str:
    """Strip a body's link, code, HTML and table characters, keeping its lines.

    Line-by-line, so the breaks between several collapsed alerts survive while
    each line still has its whitespace collapsed. See :data:`_BODY_MARKUP_CHARS`
    for why this strips less than :func:`flatten`.
    """
    lines = [
        " ".join(line.translate(_BODY_MARKUP_CHARS).split())
        for line in str(text or "").splitlines()
    ]
    return "\n".join(line for line in lines if line)


def _slug(value: object, *, limit: int = 32, fallback: str = "other") -> str:
    """A bounded, punctuation-free key component.

    Every caller here builds a `dedup_key` from a value it did not choose — a
    verdict parsed out of a mail header, a notice kind, a type the model wrote.
    Length and alphabet are both bounded so a hostile value cannot make the key
    unrecognisable or unbounded, and a `:` cannot be smuggled in to forge a
    different key's shape.
    """
    text = str(value or "").strip().lower()
    kept = "".join(c if (c.isascii() and (c.isalnum() or c in "_-")) else "-" for c in text)
    kept = kept.strip("-")[:limit].strip("-")
    return kept or fallback


def normalize_alert_type(value: object) -> str:
    """The model's `type` field, narrowed to the three the producer branches on.

    Anything unrecognised — including a missing key — is a `note`. See
    :data:`ALERT_TYPES` for why the fallback is the quiet grade rather than the
    loud one.

    **A non-empty near-miss is logged; a missing key is not.** `"urgent"`,
    `"warning"`, `"security_alert"` all coerce to `note`, and the whole point of
    the quiet grade is that nothing downstream makes a sound about it — no push,
    no action chip, and `auto_resolve_on_seen` closes the row on the first panel
    render. That is right for a grade the model chose and wrong as the only trace
    of one it fumbled, so the coercion itself goes to the journal where a
    systematic misspelling is discoverable. A missing key stays silent: it is the
    documented default, not a mistake.
    """
    text = str(value or "").strip().lower()
    if text and text not in ALERT_TYPES:
        logger.info(
            "deferred alert type %r is not one of %s; filing it as %r",
            text[:64], ALERT_TYPES, ALERT_TYPE_NOTE,
        )
        return ALERT_TYPE_NOTE
    return text if text in ALERT_TYPES else ALERT_TYPE_NOTE


def delivers(alert_type: str) -> bool:
    """Whether this grade is pushed as well as written."""
    return normalize_alert_type(alert_type) in DELIVERED_ALERT_TYPES


def severity_for(alert_type: str) -> str:
    """The row severity for a grade. Unknown grades are already normalised."""
    return ALERT_SEVERITY[normalize_alert_type(alert_type)]


# --- keys ----------------------------------------------------------------
#
# Spelled once each. Idempotency comes from `UNIQUE (user_id, source,
# dedup_key)`, so a producer and a close path that disagree by one character
# means two rows for one thing, with only one of them closable.


def deferred_key(task_id: int | str, alert_type: str) -> str:
    return f"task:{_slug(task_id, limit=24, fallback='0')}:{normalize_alert_type(alert_type)}"


def throttle_key(kind: str) -> str:
    return f"throttle:{_slug(kind)}"


def expired_key(task_id: int | str) -> str:
    return f"expired:{_slug(task_id, limit=24, fallback='0')}"


def dmarc_key(verdict: str) -> str:
    return f"dmarc:{_slug(verdict)}"


def undelivered_key(task_id: int | str) -> str:
    return f"undelivered:{_slug(task_id, limit=24, fallback='0')}"


# --- the write path ------------------------------------------------------


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    dedup_key: str,
    title: str,
    body: str = "",
    severity: str = "warning",
    actionable: bool = False,
    params: dict | None = None,
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write one fire-and-forget row, on the caller's connection.

    Deliberately narrower than :func:`istota.notification_store.write_notification`:
    there is no `link`, no `object_type` and no `object_id` parameter, because
    this source has no object to point at and must never emit a URL. A producer
    cannot pass one by mistake.

    Returns the :class:`RaiseResult` so a producer inside a write transaction can
    buffer it. **Most producers of this source do not deliver through it** —
    their delivery gate is an in-process window (the DMARC canary's 24-hour key,
    the mail throttle's per-window notice) or a send that carries routing the
    store does not model. Those call ``send_notification`` themselves and record
    the outcome with :func:`istota.notification_store.mark_delivered`; see the
    spec's "the row is the durable record" rule.
    """
    from ..notification_store import write_notification

    # `object_type` and `object_id` go as None rather than being named on the
    # signature above, which is the same decision stated twice: this source has
    # no object to point at and a producer cannot supply one. `_common.row_kwargs`
    # offers no `link` parameter at all, for the reason the module docstring gives.
    return write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key,
            title=flatten(title)[:MAX_ALERT_TITLE_CHARS] or "Notice",
            body=flatten_body(body)[:MAX_ALERT_BODY_CHARS],
            severity=severity,
            actionable=actionable,
            params=params or {},
            room_token=room_token,
        ),
    )


def merge_param_list(
    conn: "sqlite3.Connection",
    user_id: str,
    dedup_key: str,
    key: str,
    value: str,
) -> dict:
    """Append `value` to a bounded list in the open row's `params`, and read it back.

    `write_notification` replaces `params` wholesale, which is right for a row
    whose params describe the latest occurrence and wrong for one that is
    accumulating evidence across them. The DMARC canary is the case: one row
    covers every forged sender that produced the same verdict, and the senders
    are what makes the row worth reading.

    Bounded at :data:`MAX_PARAM_ENTRIES`, keeping the first entries seen for the
    current open row and counting the rest under ``{key}_dropped``.

    **That count is drop *events*, not distinct dropped values**, and the name
    says so. Deduplicating it would mean carrying the dropped values too, which
    is the unbounded list this cap exists to refuse — so the honest cheap answer
    is a counter with an accurate name. A row whose senders list is full and
    whose `dropped` is far larger than `occurrences` is telling the operator the
    same thing either way: the axis is wide and the list is a sample.

    Never raises — a params read that failed must not cost the row.
    """
    kept: list[str] = []
    dropped = 0
    try:
        import json

        # `state = 'open'` on purpose. A closed row that a fresh alert is about
        # to reopen is a *previous* incident: carrying its full list forward
        # would mean the new incident's evidence could only ever land in the
        # drop counter, because the list is already at the cap. The list belongs
        # to the generation that collected it.
        row = conn.execute(
            "SELECT params FROM notifications "
            "WHERE user_id = ? AND source = ? AND dedup_key = ? AND state = 'open'",
            (user_id, SOURCE, dedup_key),
        ).fetchone()
        if row is not None:
            stored = json.loads(row[0] or "{}")
            if isinstance(stored, dict):
                existing = stored.get(key)
                if isinstance(existing, list):
                    kept = [str(v) for v in existing][:MAX_PARAM_ENTRIES]
                try:
                    dropped = max(0, int(stored.get(f"{key}_dropped") or 0))
                except (TypeError, ValueError):
                    dropped = 0
    except Exception:
        logger.debug("could not read params for %r, starting fresh", dedup_key, exc_info=True)
        kept, dropped = [], 0

    flat = flatten(value)[:MAX_ALERT_TITLE_CHARS]
    if flat and flat not in kept:
        if len(kept) < MAX_PARAM_ENTRIES:
            kept.append(flat)
        else:
            dropped += 1

    out: dict = {key: kept}
    if dropped:
        out[f"{key}_dropped"] = dropped
    return out


# --- the read path -------------------------------------------------------


def body_for(row: "NotificationRow") -> str:
    """What the panel shows under the title.

    Prefers the individual messages in `params` over the stored `body`, because
    the deferred-alert producer collapses several of them onto one row and the
    stored body is only the first line of the summary. Flattened again on the way
    out: the render path cannot know which version of a producer wrote a stored
    row, and this is the source whose text the model authored.
    """
    params = row.params if isinstance(row.params, dict) else {}
    messages = params.get("messages")
    if isinstance(messages, list) and messages:
        lines = [flatten_body(str(m)) for m in messages[:MAX_DEFERRED_ALERTS_PER_TASK]]
        rendered = "\n".join(f"- {line}" for line in lines if line)
        if rendered:
            return rendered[:MAX_ALERT_BODY_CHARS]
    return flatten_body(row.body)[:MAX_ALERT_BODY_CHARS]


class TaskAlertResolver:
    source = SOURCE
    # The whole point of this source. Nothing outside the table will ever close
    # one of these rows, so being read is what closes it.
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        """Always a view, never ``None``.

        `None` means "the object is gone", and `list_open` marks those rows
        `stale` and omits them. A row of this source has no object, so returning
        `None` on any path would close a notice the user has not read — silently,
        and for good, since nothing raises it a second time.

        No `link` and no actions, on every path. See the module docstring.
        """
        from ..notification_sources import NotificationView

        return NotificationView(
            title=flatten(row.title)[:MAX_ALERT_TITLE_CHARS] or "Notice",
            body=body_for(row),
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = TaskAlertResolver()
