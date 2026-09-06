"""The resolver seam for the notification inbox: registry, view types, URL rules.

A *source* is a producer of notification rows — `confirmation`, `outbound_draft`,
`cron_job`, `task_alert`. Each registers a resolver, and the resolver is what
turns a stored row into what the panel actually renders: a title, a body, and
the actions currently available on the underlying object.

**`resolve` returning `None` means "the object is gone or already handled".**
That single return value is the whole anti-staleness story. Approving a
confirmation over Talk changes `tasks.status`; the next panel read sees the
resolver return `None` and marks the row `stale` without anyone having
remembered to close it. Resolvers are a *backstop*, not the primary close path —
every producer closes its own rows when it closes the object.

Resolvers run in the **web process**, on the read path, and may open their own
per-user module DB (which is why they receive a `Config`). So a resolver module
must not import anything heavy at module scope.

This module deliberately imports nothing from the rest of the package: the store
imports it, and so does every resolver module. Registration is explicit — see
:func:`_register_all`.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import sqlite3

    from .config import Config

logger = logging.getLogger(__name__)


# A same-origin relative path: no scheme, no `..`, no query, no fragment.
#
# Three fields carry a URL into the client — `NotificationAction.endpoint`,
# `NotificationAction.href` and `NotificationView.link` — and all three are
# server-supplied strings the browser then acts on: the first is POSTed with the
# session cookie, the other two land in an anchor's href where `javascript:` or
# an off-origin absolute URL would sail through a text-node rule.
#
# `object_id` is opaque TEXT so a source can key on something non-integer, and
# every action path is built by interpolating it. A resolver coerces or
# validates its id before interpolating (`int(row.object_id)` for the
# integer-keyed sources); this is the backstop behind that, enforced at runtime
# on every view rather than only in a test.
#
# Anchored `\A`/`\Z`, not `^`/`$`: outside MULTILINE, `$` still matches *before*
# a terminal newline, so `"/chat\n"` passes a `$`-anchored version of this
# pattern. A control character reaching a fetch target is exactly what an
# allowlist is for, and one that admits a trailing newline is not one.
SAFE_PATH_RE = re.compile(r"\A/[A-Za-z0-9][A-Za-z0-9/_-]*\Z")

ACTION_KINDS = ("primary", "default", "danger")
ACTION_METHODS = ("POST", "LINK")

SEVERITIES = ("info", "success", "warning", "danger")

# Mirrors `notifications.PURPOSES`. Duplicated rather than imported because the
# store validates a purpose on the write path, where pulling in the delivery
# module (and the transport package behind it) buys nothing — delivery happens
# later, on another call. `test_notification_store.py` asserts the two agree, so
# the copy cannot drift.
#
# Validated at all because an unrecognised purpose is not refused downstream: it
# falls through the user's routing table and lands on the default ladder, so a
# typo silently mis-routes rather than failing.
DELIVERY_PURPOSES = ("reply", "alert", "log", "briefing", "notification")
DEFAULT_PURPOSE = "alert"


def is_safe_path(value: object) -> bool:
    """True for a string that is a safe same-origin relative path.

    `None` is *not* safe by this predicate — absence is the caller's business to
    distinguish, and conflating "no link" with "a valid link" here is how the
    check ends up skipped on the field that has one.
    """
    return isinstance(value, str) and bool(SAFE_PATH_RE.match(value))


@dataclass(frozen=True)
class NotificationAction:
    """One button on a notification.

    `endpoint` is an existing producer API path (`/chat/tasks/{id}/confirm`),
    written **apiFetch-relative** — that is the form the client's fetcher takes.
    There is deliberately no generic dispatcher endpoint: those handlers already
    own their authorization, and a dispatcher would be a second, weaker gate.
    """

    id: str                     # 'confirm', 'discard', 'approve', 'reconnect'
    label: str
    kind: str                   # one of ACTION_KINDS
    method: str                 # one of ACTION_METHODS
    endpoint: str | None = None  # method == 'POST'
    href: str | None = None      # method == 'LINK'

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "method": self.method,
            "endpoint": self.endpoint,
            "href": self.href,
        }


@dataclass(frozen=True)
class NotificationView:
    """What a resolver says the row currently looks like.

    `status_note` exists so "no actions because this draft is mid-send" is
    distinguishable from "no actions because nobody registered this source". An
    empty action tuple alone conflates the two.
    """

    title: str
    body: str = ""
    severity: str = "info"
    actions: tuple[NotificationAction, ...] = ()
    link: str | None = None
    status_note: str | None = None


@dataclass(frozen=True)
class NotificationRow:
    """A stored row, as handed to a resolver. Read-only by construction."""

    id: int
    user_id: str
    source: str
    dedup_key: str
    object_type: str | None
    object_id: str | None
    severity: str
    actionable: bool
    title: str
    body: str
    params: dict = field(default_factory=dict)
    link: str | None = None
    room_token: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_delivered_at: str | None = None
    occurrences: int = 1
    seen_at: str | None = None
    state: str = "open"
    resolved_at: str | None = None
    resolved_by: str | None = None


class NotificationResolver(Protocol):
    """What a source registers.

    `auto_resolve_on_seen` answers "what closes an item that has no object to
    watch". The split is not actionable versus informational; it is whether
    there is a state change that ends the item:

    - **Object-backed** (`False`) — a held task, a pending draft, a disabled
      cron job. Something outside the table changes and the row closes.
    - **Fire-and-forget** (`True`) — a task failed, an alert the model raised.
      Nothing will ever close these, so the row closes when the panel is opened
      with it visible, and `sweep_expired_alerts` is the backstop for rows that
      fell below the render limit.
    """

    source: str
    auto_resolve_on_seen: bool

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: NotificationRow
    ) -> NotificationView | None:
        ...


_REGISTRY: dict[str, NotificationResolver] = {}
_REGISTERED = False
# Guards the one-time import pass, not every read: a dict lookup needs no lock,
# and taking one on the panel's hot path would serialize the read for nothing.
_REGISTER_LOCK = threading.Lock()


def register(resolver: NotificationResolver) -> None:
    """Add a resolver to the registry, keyed by its `source`."""
    source = getattr(resolver, "source", "")
    if not isinstance(source, str) or not source:
        logger.warning("refusing a notification resolver with no source: %r", resolver)
        return
    existing = _REGISTRY.get(source)
    if existing is not None and existing is not resolver:
        # Two modules claiming one source id is a bug, not a fallback: the loser
        # silently stops rendering. Say so and take the newer one.
        logger.warning("notification source %r re-registered, replacing", source)
    _REGISTRY[source] = resolver


def get_resolver(source: str) -> NotificationResolver | None:
    """The resolver for `source`, or None when nobody registered one.

    A row with no resolver is *not* hidden — it renders from stored text with a
    `status_note` and a working Dismiss. A row nobody can explain is still one
    the user should be able to clear.
    """
    _register_all()
    return _REGISTRY.get(source)


def all_resolvers() -> dict[str, NotificationResolver]:
    _register_all()
    return dict(_REGISTRY)


def auto_resolve_sources() -> set[str]:
    """Source ids whose rows close on being seen, for `mark_seen` and the sweep."""
    return {
        source
        for source, resolver in all_resolvers().items()
        if getattr(resolver, "auto_resolve_on_seen", False)
    }


def _register_all() -> None:
    """Import and register every built-in resolver. Idempotent, explicit.

    Explicit rather than by import side effect: the daemon process and the web
    process both reach the registry, and one that depends on which modules
    happened to be imported first is a source of surface-dependent behaviour.
    Called lazily by the readers above so no caller has to remember to prime it,
    but the work happens exactly once per process.

    **Under the lock, and the flag is set last.** This runs in the web process,
    which serves concurrent requests on a thread pool. Setting the flag first
    would let a second thread arriving mid-import see "registered" over an
    empty registry, and every row that request rendered would be silently
    downgraded to "source no longer available" — a bug that appears only under
    load, only on the first request after a restart, and never in a test.

    **Each import is guarded separately.** One broken resolver module must cost
    its own source and nothing else: an unguarded loop would leave the registry
    holding whatever had been imported before the failure, and every row of
    every later source would render as "source no longer available" — a
    plausible-looking panel with no button that works.

    The guard isolates a source's *own* failure, and there is exactly one thing
    it cannot isolate: every source imports ``notification_resolvers._common`` at
    module scope, so a break in that file fails all six guarded imports at once
    and produces the outcome above anyway. That is the trade for the four
    mechanical bodies it holds; the file is kept import-free at module scope so
    there is as little as possible in it to break.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    with _REGISTER_LOCK:
        if _REGISTERED:
            return
        for module_name in (
            "confirmation",
            "outbound_draft",
            "cron_job",
            "connected_service",
            "health_panel",
            "task_alert",
        ):
            try:
                module = importlib.import_module(
                    f".notification_resolvers.{module_name}", package=__package__,
                )
                register(module.RESOLVER)
            except Exception:
                logger.error(
                    "notification resolver %r could not be registered",
                    module_name, exc_info=True,
                )
        _REGISTERED = True


def reset_registry() -> None:
    """Drop every registration. For tests, which run in a reused process."""
    global _REGISTERED
    with _REGISTER_LOCK:
        _REGISTRY.clear()
        _REGISTERED = False


def invalid_paths(view: NotificationView) -> list[str]:
    """Every URL-carrying field of `view` that fails the allowlist.

    Returns the offending values so the caller can log *which* field a source
    got wrong. An empty list means the view is safe to emit. A non-empty one
    means the whole view is downgraded to stored text — not just the bad field —
    because a resolver that built one path wrongly has no claim on the rest.

    **Both URL fields of an action are checked, not just the one its `method`
    names.** `NotificationAction.to_dict` serializes `endpoint` and `href`
    unconditionally, so a `method='POST'` action carrying a `javascript:` href —
    or a `LINK` carrying an off-origin endpoint — would otherwise ship an
    unvalidated server-supplied URL to the client through the field the branch
    did not look at. The spec's rule is that all three URL-carrying fields are
    treated the same way; a branch on `method` is not that.
    """
    bad: list[str] = []

    if view.link is not None and not is_safe_path(view.link):
        bad.append(f"link={view.link!r}")

    for action in view.actions or ():
        if action.method not in ACTION_METHODS:
            bad.append(f"{action.id}.method={action.method!r}")
        elif action.kind not in ACTION_KINDS:
            bad.append(f"{action.id}.kind={action.kind!r}")

        if action.endpoint is not None and not is_safe_path(action.endpoint):
            bad.append(f"{action.id}.endpoint={action.endpoint!r}")
        elif action.endpoint is None and action.method == "POST":
            bad.append(f"{action.id}.endpoint is missing")

        if action.href is not None and not is_safe_path(action.href):
            bad.append(f"{action.id}.href={action.href!r}")
        elif action.href is None and action.method == "LINK":
            bad.append(f"{action.id}.href is missing")

    return bad
