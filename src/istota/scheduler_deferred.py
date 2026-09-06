"""Deferred-op file handlers for the scheduler.

When a task runs under the bubblewrap sandbox the DB is read-only, so
sandboxed Claude / skill CLI invocations write JSON to the always-RW user
temp dir instead of mutating the DB directly. After the task completes
(or before it retries) the scheduler — which runs unsandboxed — drains
those files via the handlers in this module.

File layout: ``{user_temp_dir}/task_{task_id}_{suffix}.json`` where the
suffix names the consumer (``subtasks``, ``kv_ops``, ``kg_ops``, etc.).
``_KNOWN_DEFERRED_SUFFIXES`` is the source of truth used by both
``_purge_deferred_files_for_retry`` (clear the slate before a retry) and
``_warn_unconsumed_deferred_files`` (catch hallucinated filenames that
would otherwise be silently dropped). ``_RETIRED_DEFERRED_SUFFIXES`` is a
subset of it: names the framework used to honour and no longer replays,
kept recognized so neither of those two mechanisms treats a once-real name
as a hallucination.

``_load_deferred_email_output`` lives in ``scheduler.py`` rather than
here — it returns parsed dict content for the email-delivery path, not
an op count, and its lifecycle is owned by the result-delivery code.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from . import db
from .config import Config
from .brain import model_namespace_for_kind, resolve_brain_kind
from .kv_namespaces import is_reserved_namespace
from .notification_store import deliver_pending
from .user_scope import is_scopable_user_id

# Use the parent scheduler's logger name so log lines remain identical to
# pre-extraction output and any operator-side log routing keeps working.
logger = logging.getLogger("istota.scheduler")


# Recognized deferred-file suffixes — files matching task_{id}_{suffix}.json
# are consumed by their respective `_process_deferred_*` handlers. Anything
# else in the user temp dir that mentions the task id is unrecognized and
# was silently dropped. We log it so misnamed deferred writes (e.g. a
# hallucinated filename from the model) become visible.
_KNOWN_DEFERRED_SUFFIXES = (
    "subtasks",
    "tracked_transactions",
    "sent_emails",
    "kv_ops",
    "kg_ops",
    "user_alerts",
    "email_output",
    "health_ops",
    "garmin_import",
)

# Suffixes the framework once consumed and no longer does. They stay in
# ``_KNOWN_DEFERRED_SUFFIXES`` above so the two mechanisms that read that
# tuple keep covering them: the retry purge still clears a leftover between
# attempts, and ``_warn_unconsumed_deferred_files`` does not report the name
# as a hallucination — it is a name the framework used to honour. What they
# no longer get is a handler that replays the contents. A file turning up
# under one of these names is logged and discarded by
# ``_process_retired_deferred_files``.
#
# ISSUE-427: ``tracked_transactions`` fed a second copy of the money module's
# transaction dedup, living in the framework DB. That copy predated the money
# module and had no writer — money runs host-side through the skill proxy
# against its own per-user database and emits no deferred ops at all — so
# replaying such a file wrote rows no reader consults, which is worse than
# dropping it because it looks like it worked.
_RETIRED_DEFERRED_SUFFIXES = (
    "tracked_transactions",
)

# Operator-facing recovery artifacts. Written by deferred-op handlers when
# an op fails mid-batch; preserved so an operator can recover the lost rows.
# Recognized by ``_warn_unconsumed_deferred_files`` but NOT purged on retry
# (the operator inspects them after the task settles).
_KNOWN_ARTIFACT_SUFFIXES = (
    "health_op_failures",
)


def _load_deferred_json(
    user_temp_dir: Path,
    task_id: int,
    suffix: str,
    *,
    expected_type: type = list,
) -> tuple[Path, list | dict] | None:
    """Open ``task_<id>_<suffix>.json`` in ``user_temp_dir`` for a deferred-op handler.

    Returns ``(path, data)`` on success. Returns ``None`` for absent or
    malformed files; malformed files are unlinked and a WARN is logged. The
    path is returned so the caller can ``unlink`` after processing — keeping
    the lifecycle (and any task-specific invariants) explicit at the call-site.

    ``expected_type`` is checked with ``isinstance``; mismatches are treated
    as malformed (warned and unlinked).

    The read names UTF-8 explicitly. The producer is a task subprocess (a skill
    CLI, or the model's own shell heredoc for the subtask idiom) whose env was
    rebuilt from scratch, so it may not share the daemon's locale — and an
    undecodable file must land here as a warning, not as an exception escaping
    into ``_drain_deferred_ops``, which runs its handlers in sequence with no
    guard between them and would silently skip every one after this.
    """
    path = user_temp_dir / f"task_{task_id}_{suffix}.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.warning("Bad deferred %s file for task %d: %s", suffix, task_id, e)
        path.unlink(missing_ok=True)
        return None

    if not isinstance(data, expected_type):
        logger.warning(
            "Deferred %s for task %d is not a %s",
            suffix, task_id, expected_type.__name__,
        )
        path.unlink(missing_ok=True)
        return None

    return path, data


def _inherited_model(
    task: db.Task, config: Config,
) -> tuple[str | None, tuple[str, str | None, str | None] | None]:
    """The parent's ``model`` for its subtasks, and what was dropped getting there.

    A stored model name is a bare string and its namespace comes from where it
    was written. ``executor._pin_origin_namespace`` reads an unpinned row's
    namespace off that row's own ``source_type``, so copying a parent's name
    onto a ``subtask`` row means the child resolves in the ``subtask`` lane a
    name the parent's lane produced. On a deployment routing the two to kinds
    with different namespaces the id then reaches a wire that never spoke it,
    and ``resolve_model_name`` passes an unknown name straight through, so
    nothing downstream catches it (ISSUE-421).

    Dropping is the direction ``_resolve_crossing_model_effort`` and
    ``commands._clear_pin_across_namespaces`` take for a pin whose portability
    is not established, and it restores what happened before ISSUE-419 — the
    child runs the routed brain's own default. It deliberately does **not**
    mirror that rule's other arm, which re-resolves a *portable* intent in the
    target namespace. The parent's ``model`` is normally a concrete id by the
    time it is stored, so there is usually no intent left to carry; the
    exception is an operator-declared portable alias that the writing brain's
    table had no entry for, which is passed through by name and would be
    re-resolvable here. Carrying it would mean reaching into the executor for
    ``config_alias_portable_names``, and the outcome for the narrow case is the
    child's own default either way, so the deviation is stated rather than
    closed.

    ``effort`` is not namespaced and is inherited unconditionally, matching the
    crossing rule's drop path.

    Two cases return the name unchanged. An empty pin has nothing to disagree
    about, and a parent with ``tasks.brain`` set carries that column down to the
    child, so both rows' namespace is read off the same pinned kind and no lane
    is consulted on either side.

    The second element is the dropped name and the two namespaces, for the
    caller to log at the point a child actually takes the default. Logging here
    would announce a consequence for a file the depth gate, a malformed entry or
    a per-entry ``model`` override may mean never has one.

    The routing read is guarded because this runs inside the scheduler's
    deferred drain, which calls its handlers in sequence with no guard between
    them, so a read that raised would cost every later handler for this task.
    Its residue is a drop, not a carry: an origin that could not be established
    is exactly what the crossing rule refuses to treat as a match.
    """
    if not (task.model or "").strip():
        return task.model, None
    if (task.brain or "").strip():
        return task.model, None
    try:
        # The recorded fact first, and the lane only for a row that predates the
        # column — the preference `executor._pin_origin_namespace` and
        # `commands._clear_pin_across_namespaces` both apply (ISSUE-420). Without
        # it this is a third reader of the same inference, and it drops a model
        # the column has already established as safe to carry: a room turn whose
        # namespace was recorded, spawning a subtask, would lose the pin here on
        # a lane-routed deployment even though parent and child agree.
        parent_ns = (task.model_namespace or "").strip() or model_namespace_for_kind(
            resolve_brain_kind(task.source_type, config.brain).kind,
        )
        child_ns = model_namespace_for_kind(
            resolve_brain_kind("subtask", config.brain).kind,
        )
    except Exception:  # noqa: BLE001 — a routing read must not break the drain
        return None, (task.model, None, None)
    # `None` is "not established" and must never compare equal, the same
    # reading `_resolve_crossing_model_effort` gives it.
    if parent_ns is not None and parent_ns == child_ns:
        return task.model, None
    return None, (task.model, parent_ns, child_ns)


def _process_deferred_subtasks(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred subtask creation requests from JSON file.

    Returns count of subtasks created.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "subtasks")
    if loaded is None:
        return 0
    path, data = loaded

    # Admin-only: non-admin users cannot create subtasks
    if not config.is_admin(task.user_id):
        logger.warning(
            "Non-admin user %s attempted deferred subtask creation (task %d), ignoring",
            task.user_id, task.id,
        )
        path.unlink(missing_ok=True)
        return 0

    # `task.user_id` is pinned onto every subtask below and does not vary
    # across the loop, so an id `db.create_task` refuses fails all of them
    # identically — and would escape into `_drain_deferred_ops`, which calls
    # its handlers in sequence with no guard between them and so would skip
    # every later handler for this task (ISSUE-402). Refused once, here.
    if not is_scopable_user_id(task.user_id):
        logger.warning(
            "Task %d deferred subtasks ignored: user id %r cannot name a "
            "per-user directory",
            task.id, task.user_id,
        )
        path.unlink(missing_ok=True)
        return 0

    max_subtasks = config.scheduler.max_subtasks_per_task
    max_depth = config.scheduler.max_subtask_depth
    max_chars = config.scheduler.max_subtask_prompt_chars
    # Resolved once: it depends on the parent row and the config, not on the
    # entry. The drop is reported from the loop instead, at the point a child
    # actually takes the default, and only for the first such child.
    inherited_model, dropped_model = _inherited_model(task, config)
    drop_reported = False
    count = 0
    with db.get_db(config.db_path) as conn:
        # Depth gate: refuse to extend a chain that's already at or past the cap.
        # parent_depth >= max_depth means a new child would land at depth+1 > max.
        if max_depth > 0:
            parent_depth = db.get_subtask_depth(conn, task.id)
            if parent_depth >= max_depth:
                logger.warning(
                    "Task %d at subtask depth %d >= max_subtask_depth %d, "
                    "refusing %d deferred subtask(s)",
                    task.id, parent_depth, max_depth, len(data),
                )
                path.unlink(missing_ok=True)
                return 0

        for entry in data:
            if count >= max_subtasks:
                logger.warning(
                    "Task %d hit deferred subtask limit (%d), ignoring remaining entries",
                    task.id, max_subtasks,
                )
                break
            # A malformed entry must never be dropped silently (ISSUE-135): a
            # deferred subtask that goes unexecuted with no log, no retry, and
            # no signal is the worst failure mode. The one documented key is
            # `prompt`; `command` is NOT supported. Warn loudly (naming the keys
            # present) instead of a silent `continue` so the miss is diagnosable.
            if not isinstance(entry, dict):
                logger.warning(
                    "Task %d deferred subtask entry is not an object (%r), skipping",
                    task.id, type(entry).__name__,
                )
                continue
            prompt = entry.get("prompt", "")
            if not prompt:
                keys = sorted(entry.keys())
                hint = ""
                if "command" in entry:
                    hint = (
                        " — 'command' is not a supported deferred-subtask key; "
                        "subtasks take a natural-language 'prompt'"
                    )
                logger.warning(
                    "Task %d deferred subtask entry has no 'prompt' (keys: %s), skipping%s",
                    task.id, keys, hint,
                )
                continue
            if max_chars > 0 and len(prompt) > max_chars:
                logger.warning(
                    "Task %d deferred subtask prompt too long (%d > %d chars), skipping",
                    task.id, len(prompt), max_chars,
                )
                continue
            # Pin conversation_token to parent task — deferred JSON cannot
            # override this to prevent prompt-injection-driven routing. The
            # parent's `withheld_from_room` is pinned with it, and for the same
            # reason it is not the JSON's to choose: inheriting the token without
            # it would index the subtask's prompt and result under the origin
            # room's memory namespace and collect it into that room's sleep
            # cycle, which is exactly what the parent is being kept out of
            # (ISSUE-255).
            conv_token = task.conversation_token
            output_target = entry.get("output_target")
            if not output_target and conv_token:
                output_target = "talk"
            if dropped_model and not entry.get("model") and not drop_reported:
                dropped_name, parent_ns, child_ns = dropped_model
                logger.info(
                    "Task %d subtask: not inheriting model %r across a "
                    "namespace change (%s -> %s); the child uses its own "
                    "brain's default",
                    task.id, dropped_name, parent_ns, child_ns,
                )
                drop_reported = True
            db.create_task(
                conn,
                prompt=prompt,
                user_id=task.user_id,
                source_type="subtask",
                parent_task_id=task.id,
                conversation_token=conv_token,
                withheld_from_room=task.withheld_from_room,
                priority=entry.get("priority", 5),
                queue=task.queue,
                output_target=output_target,
                talk_delivery_token=task.talk_delivery_token,
                # Inherit parent's model / effort overrides — a task spawned
                # via `!model opus-46-high` should run its children at the
                # same level unless the deferred JSON explicitly overrides.
                # The model only travels where the child will read it in the
                # namespace it was written in; see `_inherited_model`. The
                # JSON's own `model` is a raw name nobody has resolved yet, so
                # the child's own lane is already the right place for it.
                model=entry.get("model") or inherited_model,
                effort=entry.get("effort") or task.effort,
                # And the parent's brain, which is not the deferred JSON's to
                # choose. A subtask's source_type is "subtask", so without this
                # it would take `[brain.source_type_overrides]["subtask"]` and
                # could silently run a different brain from the parent that
                # spawned it.
                brain=task.brain,
                # The recorded namespace travels with the model it describes
                # and only with it (ISSUE-420), which after ISSUE-421(b) means
                # keying on the name that actually lands rather than on the
                # parent's. A model the deferred JSON chose was written by the
                # model inside the sandbox in whatever vocabulary it saw, so the
                # parent's namespace would be a guess about somebody else's
                # string; and a parent pin `_inherited_model` *dropped* leaves
                # no model here to describe. Both cases record nothing and let
                # the executor infer, which is what this path did before.
                model_namespace=(
                    task.model_namespace
                    if not entry.get("model") and inherited_model
                    else None
                ),
            )
            count += 1

    if count:
        logger.info(
            "Created %d deferred subtasks for task %d (prompts: %s)",
            count, task.id,
            ", ".join(repr(e.get("prompt", "")[:80]) for e in data[:count]),
        )
    path.unlink(missing_ok=True)
    return count


def _process_retired_deferred_files(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Log and discard deferred files whose handler has been retired.

    ISSUE-427. The names in ``_RETIRED_DEFERRED_SUFFIXES`` stay recognized, so
    neither the retry purge nor the unconsumed-file warning changes behaviour
    for them; what changes is that nothing replays the contents. A model can
    still be prompted into writing one of these files, and the honest outcome
    is a log line saying it was discarded rather than either silence or a
    warning calling a once-real name a hallucination.

    ``config`` is unused and kept so the signature matches every other handler
    ``_drain_deferred_ops`` calls in sequence.

    Returns the number of files removed.
    """
    if not user_temp_dir.is_dir():
        return 0

    count = 0
    for suffix in _RETIRED_DEFERRED_SUFFIXES:
        path = user_temp_dir / f"task_{task.id}_{suffix}.json"
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as e:
            # Reachable: the per-user temp dir is writable from the sandbox, so
            # a task can plant a *directory* under this name and `unlink` raises
            # IsADirectoryError. Say so rather than reporting a discard that did
            # not happen — the entry then stays, and because the name is in
            # `_KNOWN_DEFERRED_SUFFIXES` the unconsumed-file pass will not flag
            # it either, so this line is the only trace. `_purge_deferred_files_for_retry`
            # shares the blind spot.
            logger.warning(
                "Could not remove retired deferred %s for task %d: %s",
                suffix, task.id, e,
            )
            continue
        logger.warning(
            "Discarded deferred %s file for task %d: the framework no longer "
            "processes this op (ISSUE-427)",
            suffix, task.id,
        )
        count += 1
    return count


def _process_deferred_sent_emails(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred sent email records from JSON file.

    When Claude sends emails via `email send` inside the sandbox, the skill
    writes a deferred file with message metadata. The scheduler processes it
    here to record outbound emails for emissary thread matching.

    Returns count of sent emails recorded.
    """
    from .transport import routing

    loaded = _load_deferred_json(user_temp_dir, task.id, "sent_emails")
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    with db.get_db(config.db_path) as conn:
        for entry in data:
            message_id = entry.get("message_id", "")
            to_addr = entry.get("to_addr", "")
            if not message_id or not to_addr:
                continue
            try:
                db.record_sent_email(
                    conn,
                    user_id=task.user_id,
                    message_id=message_id,
                    to_addr=to_addr,
                    subject=entry.get("subject"),
                    task_id=task.id,
                    conversation_token=task.conversation_token,
                    talk_delivery_token=task.talk_delivery_token,
                    origin_target=routing.origin_descriptor(task, conn),
                )
                count += 1
            except Exception as e:
                logger.warning(
                    "Failed to record sent email for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Recorded %d deferred sent emails for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_kv_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred KV store operations from JSON file.

    When Claude runs `istota-skill kv set|delete|set-add|set-remove|set-trim`
    inside the sandbox, the skill CLI writes operations to a deferred file. The
    scheduler processes them here. The set ops re-read the current value from
    the DB before applying their change, so concurrent ops across tasks compose
    correctly.

    Returns count of operations processed.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "kv_ops")
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    with db.get_db(config.db_path) as conn:
        for entry in data:
            op = entry.get("op")
            namespace = entry.get("namespace", "")
            key = entry.get("key", "")
            if not namespace or not key:
                continue
            # The sandbox has no database, so a task's `kv set` lands here as
            # JSON rather than as a CLI call — which means the skill's own
            # refusal never ran for it. This is the enforcement point that
            # covers a sandboxed task, and the op file is model-written, so
            # the namespace is checked against the value in the JSON.
            if is_reserved_namespace(namespace):
                logger.warning(
                    "Deferred KV op for task %d names reserved namespace %r; refused",
                    task.id, namespace,
                )
                continue
            # Shared-scope writes are gated on the task's *trusted* identity
            # (task.user_id, never the JSON), fail-closed via is_shared_kv_writer.
            # Set-ops never carry scope:"shared" (rejected at the skill), so the
            # shared branch only handles whole-value set/delete.
            scope = entry.get("scope")
            if scope == "shared":
                if not config.is_shared_kv_writer(task.user_id):
                    logger.warning(
                        "shared KV write denied for task %d user %s (%s %s/%s)",
                        task.id, task.user_id, op, namespace, key,
                    )
                    continue
                try:
                    if op == "set":
                        db.shared_kv_set(
                            conn, namespace, key,
                            entry.get("value", ""), task.user_id,
                        )
                        count += 1
                    elif op == "delete":
                        db.shared_kv_delete(conn, namespace, key)
                        count += 1
                    else:
                        logger.warning(
                            "Unsupported shared KV op %r for task %d", op, task.id,
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Failed to process shared KV op for task %d: %s",
                        task.id, e,
                    )
                continue
            try:
                if op == "set":
                    value = entry.get("value", "")
                    db.kv_set(conn, task.user_id, namespace, key, value)
                    count += 1
                elif op == "delete":
                    db.kv_delete(conn, task.user_id, namespace, key)
                    count += 1
                elif op in ("set-add", "set-remove", "set-trim"):
                    members = entry.get("members") or []
                    keep_newest = entry.get("keep_newest")
                    if op == "set-trim":
                        # bool is an int subclass, and `keep_newest: true`
                        # would otherwise trim to one member.
                        if (
                            not isinstance(keep_newest, int)
                            or isinstance(keep_newest, bool)
                            or keep_newest < 0
                        ):
                            logger.warning(
                                "Bad set-trim op for task %d: keep_newest=%r",
                                task.id, keep_newest,
                            )
                            continue
                    elif not isinstance(members, list):
                        logger.warning(
                            "Bad %s op for task %d: members not a list", op, task.id,
                        )
                        continue
                    row = db.kv_get(conn, task.user_id, namespace, key)
                    if row is None and op == "set-trim":
                        # Nothing to trim; don't create the key.
                        continue
                    current: list = []
                    if row is not None:
                        try:
                            parsed = json.loads(row["value"])
                        except json.JSONDecodeError:
                            logger.warning(
                                "Skipping %s for task %d: %s/%s is not valid JSON",
                                op, task.id, namespace, key,
                            )
                            continue
                        if not isinstance(parsed, list):
                            logger.warning(
                                "Skipping %s for task %d: %s/%s is not a JSON array",
                                op, task.id, namespace, key,
                            )
                            continue
                        current = parsed
                    if op == "set-add":
                        seen = set(current)
                        new_members = list(current)
                        for m in members:
                            if m not in seen:
                                new_members.append(m)
                                seen.add(m)
                    elif op == "set-trim":
                        # Re-read means the trim composes with set-adds queued
                        # earlier in the same task, and lands on the real size.
                        # Negative index, not len()-keep: the latter clamps at
                        # -len when keep > len and silently drops members.
                        new_members = current[-keep_newest:] if keep_newest else []
                    else:
                        to_remove = set(members)
                        new_members = [m for m in current if m not in to_remove]
                    db.kv_set(
                        conn, task.user_id, namespace, key, json.dumps(new_members),
                    )
                    count += 1
                else:
                    logger.warning(
                        "Unknown KV op %r in deferred file for task %d", op, task.id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to process KV op for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Processed %d deferred KV ops for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_user_alerts(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred user alert requests from JSON file.

    When the agent detects suspicious inbound content (social engineering,
    prompt injection, exfiltration attempts), it writes alerts to a deferred
    file. The scheduler posts them to the user's alerts channel after task
    completion.

    **Collapsed onto one row per (task, alert type), and capped.** The array is
    model-authored and nothing bounds its length, so one durable notification per
    entry would let a single task leave hundreds of rows in the user's bell, each
    firing its own push. The entries a type accumulates become that row's body and
    its ``params``; past ``MAX_DEFERRED_ALERTS_PER_TASK`` they are dropped with a
    warning naming the count, the same shape ``max_subtasks_per_task`` uses.

    Delivery goes through the notification store rather than a bare
    ``send_notification``, so the push and the durable row cannot disagree: this
    used to send and then unlink the file, which meant a user with no alert
    destination configured had the model's security finding deleted with nothing
    anywhere to show for it.

    Returns the number of alert entries accepted.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "user_alerts")
    if loaded is None:
        return 0
    path, data = loaded

    from .notification_resolvers import task_alert

    # Insertion-ordered, so the first type seen leads the log line.
    by_type: dict[str, list[str]] = {}
    count = 0
    dropped = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("message")
        message = task_alert.flatten(raw if isinstance(raw, str) else "")
        if not message:
            continue
        if count >= task_alert.MAX_DEFERRED_ALERTS_PER_TASK:
            dropped += 1
            continue
        by_type.setdefault(task_alert.normalize_alert_type(entry.get("type")), []).append(
            message
        )
        count += 1

    if dropped:
        logger.warning(
            "Task %d wrote %d deferred user alert(s) over the cap of %d; the "
            "rest were dropped",
            task.id, dropped, task_alert.MAX_DEFERRED_ALERTS_PER_TASK,
        )

    recorded = not by_type
    if by_type:
        # Guarded as a whole. `write_notification` and `deliver_pending` never
        # raise, but `db.get_db` can — and `_drain_deferred_ops` calls its nine
        # handlers in sequence with nothing between them, so an exception
        # escaping here would silently skip `_deliver_deferred_email_output` and
        # the unconsumed-file warning. The same reasoning `_load_deferred_json`
        # records for its `UnicodeDecodeError` catch.
        try:
            results = []
            # A connection of this function's own, and that is safe here rather
            # than asserted to be: `_drain_deferred_ops` runs after
            # `process_one_task`'s status-writing transaction has closed, from
            # function-body scope in both of its callers. Nothing holds a write
            # lock on the framework DB.
            with db.get_db(config.db_path) as conn:
                for alert_type, messages in by_type.items():
                    results.append((alert_type, task_alert.write(
                        conn, task.user_id,
                        dedup_key=task_alert.deferred_key(task.id, alert_type),
                        title=_deferred_alert_title(alert_type, task.id),
                        body=_deferred_alert_body(messages),
                        severity=task_alert.severity_for(alert_type),
                        # Actionable by name, for the two grades that are pushed
                        # — an "action needed" notice says so and a security
                        # finding the model raised is something to look at. The
                        # panel renders no button because there is no in-app
                        # object to act on; `status_note` says so. A `note` is
                        # not actionable: nothing is being asked of the reader.
                        actionable=task_alert.delivers(alert_type),
                        params={"messages": messages, "alert_type": alert_type,
                                "task_id": task.id},
                        room_token=task.conversation_token,
                    )))
            # Only the loud grades are handed to the sender. A `note` is a row in
            # the bell and nothing more — the thread it is about is already in
            # the room the user reads, so a push would restate something visible
            # a few lines up (ISSUE-311).
            deliver_pending(config, [
                r for alert_type, r in results if task_alert.delivers(alert_type)
            ])
            # A written row is enough to release the evidence file, whether or
            # not anything was pushed for it. That was already true for a send
            # that reached nobody; a grade that deliberately sends nothing is the
            # same case arrived at on purpose.
            recorded = any(r is not None for _, r in results)
        except Exception:
            logger.warning(
                "Could not record the deferred user alerts for task %d",
                task.id, exc_info=True,
            )
            # Fall back to what this path did before the inbox existed: send
            # directly, with no row. Routing the alert through the database
            # bought durability, and it must not also become a way to lose the
            # alert outright — a locked or unwritable DB would otherwise mean no
            # row, no push, and the file deleted below, which is a strictly
            # worse outcome than the one this whole change set out to fix.
            recorded = _send_deferred_alerts_unrecorded(config, task, by_type)

    if count:
        logger.info(
            "Recorded %d deferred user alert(s) for task %d in %d notification(s)",
            count, task.id, len(by_type),
        )

    if recorded:
        path.unlink(missing_ok=True)
    else:
        # Nothing holds this alert now: no row was written and no destination
        # accepted the fallback push. Deleting the file here is exactly the
        # "the model raised an alert and the evidence was deleted" failure the
        # inbox exists to end, so the file stays and the unconsumed-file warning
        # at the end of the drain reports it.
        logger.error(
            "Deferred user alerts for task %d were neither recorded nor "
            "delivered; leaving %s in place",
            task.id, path.name,
        )
    return count


def _deferred_alert_body(messages: list[str]) -> str:
    """One message on its own, several as a list."""
    if len(messages) > 1:
        return "\n".join(f"- {m}" for m in messages)
    return messages[0]


def _send_deferred_alerts_unrecorded(
    config: Config, task: db.Task, by_type: dict[str, list[str]],
) -> bool:
    """Push the alerts with no row behind them. Returns whether any got through.

    The pre-inbox behaviour, kept as the fallback for a framework DB that could
    not be opened. It touches no database, which is the property that makes it
    a usable last resort here.

    **A `note` is skipped rather than rescued, and skipping one withholds the
    return value.** This path exists to save a push that would otherwise be lost,
    and the quiet grade has no push to save — sending one here would reintroduce
    the exact interruption it exists to withhold, on the one path where the
    reader cannot dismiss it from the panel, because no row was written.

    But the caller unlinks the evidence file on a True return, and a mixed drain
    would otherwise report True on the strength of the security alert it *did*
    push while the note it skipped was held by nothing at all: no row, no send,
    and then no file. So a skip is reported as "not fully delivered" even when
    another grade got through. The cost is a file left behind next to an alert
    that did reach someone, which the unconsumed-file warning names; the
    alternative is silently discarding the thing the model wrote.
    """
    from .notifications import send_notification
    from .notification_resolvers import task_alert

    delivered = False
    skipped: list[str] = []
    for alert_type, messages in by_type.items():
        if not task_alert.delivers(alert_type):
            # Named in the log, because this is the one path where a note is
            # neither stored nor sent and the file is its only record.
            skipped.append(f"{alert_type} ({len(messages)})")
            continue
        title = _deferred_alert_title(alert_type, task.id)
        text = f"{title}\n\n{_deferred_alert_body(messages)}"
        try:
            if send_notification(config, task.user_id, text, purpose="alert",
                                 title=title):
                delivered = True
        except Exception:
            logger.warning(
                "Fallback delivery of a deferred user alert for task %d failed",
                task.id, exc_info=True,
            )
    if skipped:
        logger.warning(
            "Task %d wrote %s deferred alert(s) of a grade this fallback does "
            "not deliver, and no row was written for them; keeping the "
            "evidence file",
            task.id, ", ".join(skipped),
        )
    return delivered and not skipped


def _deferred_alert_title(alert_type: str, task_id: int) -> str:
    """The bot's own framing around the model's text.

    An em dash rather than parentheses because the title is flattened before it
    is stored and delivered, and `confirmations._MARKUP_CHARS` takes brackets of
    every kind — `Security alert (task #3)` would arrive as `Security alert task
    #3`.
    """
    from .notification_resolvers import task_alert

    if alert_type == task_alert.ALERT_TYPE_ACTION_NEEDED:
        return f"Action needed — task #{task_id}"
    if alert_type == task_alert.ALERT_TYPE_SECURITY:
        return f"Security alert — task #{task_id}"
    # The quiet grade's label has to be quiet too, and it is also the
    # fall-through: `normalize_alert_type` admits nothing else, so this is what
    # an ordinary FYI ends up wearing. "Security alert" over a note about a
    # handed-off email thread is the wrong word in the one place a reader
    # scanning the panel actually reads — which is what the unguarded
    # fall-through used to produce, and what a fourth grade added without a
    # branch here would produce again.
    return f"Note — task #{task_id}"


def _process_deferred_kg_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred knowledge-graph operations from JSON file.

    `istota-skill memory_search add-fact / invalidate / delete-fact` write
    a JSON op here when the DB is read-only inside the sandbox; we apply
    them post-task with task.user_id always wins over any user_id in the
    file (defense-in-depth).

    Returns count of operations processed.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "kg_ops")
    if loaded is None:
        return 0
    path, data = loaded

    from .memory.knowledge_graph import (
        add_fact as kg_add_fact,
        delete_fact as kg_delete_fact,
        ensure_table as kg_ensure_table,
        invalidate_fact as kg_invalidate_fact,
    )

    count = 0
    with db.get_db(config.db_path) as conn:
        kg_ensure_table(conn)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            op = entry.get("op")
            try:
                if op == "add_fact":
                    subject = entry.get("subject", "")
                    predicate = entry.get("predicate", "")
                    object_val = entry.get("object", "")
                    if not (subject and predicate and object_val):
                        continue
                    kg_add_fact(
                        conn, task.user_id, subject, predicate, object_val,
                        valid_from=entry.get("valid_from"),
                        source_task_id=task.id,
                        source_type=entry.get("source_type", "user_stated"),
                    )
                    count += 1
                elif op == "invalidate":
                    fact_id = entry.get("fact_id")
                    if fact_id is None:
                        continue
                    kg_invalidate_fact(conn, int(fact_id), ended=entry.get("ended"))
                    count += 1
                elif op == "delete":
                    fact_id = entry.get("fact_id")
                    if fact_id is None:
                        continue
                    kg_delete_fact(conn, int(fact_id))
                    count += 1
                else:
                    logger.warning(
                        "Unknown KG op %r in deferred file for task %d", op, task.id,
                    )
                    continue
                # Per-op commit (ISSUE-074): a failure later in the loop must
                # not roll back ops we've already accepted. `delete` and
                # `invalidate` are not idempotent, so a partial replay would
                # otherwise re-apply work the next time this file was read.
                conn.commit()
            except Exception as e:
                logger.warning(
                    "Failed to process KG op for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Processed %d deferred KG ops for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _health_max_document_bytes(config: Config) -> int:
    """The operator's document cap, for the agent paths.

    Only the web routes read this before; the deferred replayer fell through
    to the library default, so ``max_document_bytes = 0`` ("unlimited, for a
    scanner that produces genuinely large files") silently didn't apply to a
    document the agent filed — and a lowered cap wasn't enforced there either.
    """
    from istota.health.documents import DEFAULT_MAX_DOCUMENT_BYTES

    health_cfg = getattr(config, "health", None)
    raw = getattr(health_cfg, "max_document_bytes", None)
    if raw is None:
        return DEFAULT_MAX_DOCUMENT_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DOCUMENT_BYTES


def _resolved_source_path(
    src: Path, user_temp_dir: Path, config: Config, user_id: str, ctx,
) -> Path | None:
    """The symlink-resolved path, or ``None`` if it isn't ours to read.

    Callers must read *this* path rather than the original: resolving twice
    leaves a window in which a symlink under the workspace is swapped between
    the check and the read, and the daemon doing the reading is not sandboxed.
    """
    try:
        resolved = src.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    if not _source_path_allowed(resolved, user_temp_dir, config, user_id, ctx):
        return None
    return resolved


def _source_path_allowed(
    src: Path, user_temp_dir: Path, config: Config, user_id: str, ctx,
) -> bool:
    """Is ``src`` somewhere a sandboxed task legitimately produced a file?

    The deferred op file is written from inside the sandbox, so its
    ``source_path`` is attacker-influenced text. The daemon replaying it is
    *not* sandboxed, so without this an op could name any readable file on
    the host and have its bytes filed into the user's health records.

    Two roots: the task's own deferred dir, and the user's base workspace
    (``{mount}/Users/{uid}``) — not merely the *bot* subdir, because the
    driving case is an email attachment the executor dropped in
    ``inbox/``. Symlinks are resolved first, so a link inside the workspace
    pointing out of it is caught too.
    """
    user_root = None
    resolver = getattr(config, "workspace_root", None)
    if callable(resolver):
        try:
            user_root = resolver(user_id)
        except (TypeError, ValueError):
            user_root = None
    if user_root is None:
        # Fall back to the bot workspace's parent — the same directory
        # `workspace_root(user_id)` would have named.
        bot_workspace = getattr(ctx, "workspace_root", None)
        user_root = Path(bot_workspace).parent if bot_workspace else None

    roots: list[Path] = []
    for candidate in (user_temp_dir, user_root):
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).resolve())
        except OSError:
            continue
    if not roots:
        return False
    try:
        resolved = src.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _process_deferred_health_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Apply deferred ``health`` skill operations to the user's health DB.

    Resolves the user's :class:`HealthContext` and replays insert / update
    ops written by sandboxed ``istota-skill health`` invocations. The
    user id always comes from the task (defense-in-depth).
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "health_ops")
    if loaded is None:
        return 0
    path, data = loaded

    try:
        from . import health as _health
        from .health import db as health_db
        from .health.documents import DocumentError
    except ImportError as e:
        logger.warning(
            "Health module unavailable for deferred ops on task %d: %s",
            task.id, e,
        )
        path.unlink(missing_ok=True)
        return 0

    try:
        ctx = _health.resolve_for_user(task.user_id, config)
    except _health.UserNotFoundError as e:
        logger.warning(
            "Skipping health ops for task %d: %s", task.id, e,
        )
        path.unlink(missing_ok=True)
        return 0

    _health.ensure_initialised(ctx)

    count = 0
    failures: list[dict[str, Any]] = []
    # Within-batch ref resolution (ISSUE-092): a deferred `insert_panel` can't
    # return its new id to the sandboxed CLI, so a later `insert_biomarker` in
    # the same batch can't carry a real `panel_id`. Instead the panel op may
    # declare a symbolic `ref` and the biomarker op a `panel_ref`; here we
    # capture each panel's real id under its ref and substitute it in.
    refs: dict[str, int] = {}
    # Same mechanism for encounters, so an `attach_document` op can name an
    # encounter created earlier in the same batch. Kept in its own dict so a
    # panel ref and an encounter ref sharing a name can't cross-resolve.
    encounter_refs: dict[str, int] = {}
    with health_db.connect(ctx.db_path) as conn:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            op = entry.get("op")
            try:
                if op == "insert_stat":
                    health_db.insert_stat(
                        conn,
                        metric=entry["metric"],
                        value=float(entry["value"]),
                        unit=entry["unit"],
                        measured_at=entry.get("measured_at"),
                        source=entry.get("source", "manual"),
                        notes=entry.get("notes"),
                    )
                    count += 1
                elif op == "insert_panel":
                    enc_id = entry.get("encounter_id")
                    pid = health_db.insert_panel(
                        conn,
                        drawn_at=entry["drawn_at"],
                        lab_name=entry.get("lab_name"),
                        panel_type=entry.get("panel_type"),
                        notes=entry.get("notes"),
                        encounter_id=(
                            int(enc_id) if enc_id is not None else None
                        ),
                    )
                    ref = entry.get("ref")
                    if ref:
                        refs[str(ref)] = pid
                    count += 1
                elif op == "insert_biomarker":
                    panel_ref = entry.get("panel_ref")
                    if panel_ref is not None:
                        resolved = refs.get(str(panel_ref))
                        if resolved is None:
                            # The named panel wasn't created earlier in this
                            # batch — fail loudly rather than silently mis-file
                            # the biomarker (ISSUE-092).
                            raise KeyError(
                                f"unresolved panel_ref {panel_ref!r} "
                                f"(known refs: {sorted(refs)})"
                            )
                        panel_id = resolved
                    else:
                        panel_id = int(entry["panel_id"])
                    health_db.insert_biomarker(
                        conn,
                        panel_id=panel_id,
                        name=entry["name"],
                        value=float(entry["value"]),
                        unit=entry["unit"],
                        ref_range_low=entry.get("ref_range_low"),
                        ref_range_high=entry.get("ref_range_high"),
                        flag=entry.get("flag"),
                    )
                    count += 1
                elif op == "register_upload":
                    # Copy the source file into the uploads dir under the
                    # new panel id, then record the panel row.
                    import mimetypes
                    import shutil as _shutil

                    # Same untrusted-path rule as attach_document: the op
                    # file is written inside the sandbox and the daemon
                    # replaying it is not.
                    src = _resolved_source_path(
                        Path(entry["source_path"]), user_temp_dir, config,
                        task.user_id, ctx,
                    )
                    if src is None:
                        logger.warning(
                            "register_upload skipped for task %d: source "
                            "missing or outside the user's workspace: %s",
                            task.id, entry.get("source_path"),
                        )
                        continue
                    mime = (
                        mimetypes.guess_type(src.name)[0]
                        or "application/octet-stream"
                    )
                    pid = health_db.insert_panel(
                        conn,
                        drawn_at=entry["drawn_at"],
                        lab_name=entry.get("lab_name"),
                        source_mime=mime,
                        draft=True,
                    )
                    target_dir = ctx.uploads_dir / str(pid)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"original{src.suffix}"
                    try:
                        _shutil.copyfile(src, target)
                    except OSError as e:
                        logger.warning(
                            "register_upload copy failed for task %d: %s",
                            task.id, e,
                        )
                        continue
                    rel = str(target.relative_to(ctx.uploads_dir))
                    conn.execute(
                        "UPDATE panels SET source_file = ? WHERE id = ?",
                        (rel, pid),
                    )
                    count += 1
                elif op == "attach_document":
                    from .health import documents as _health_documents

                    # Validate the reference before a single byte is written:
                    # attach_document refuses an unknown type/entity anyway,
                    # and doing it here keeps a bad op from costing disk.
                    entity_type = entry["entity_type"]
                    if entity_type not in health_db.DOCUMENT_ENTITY_TYPES:
                        raise ValueError(
                            f"unknown entity type: {entity_type!r}"
                        )
                    enc_ref = entry.get("encounter_ref")
                    if enc_ref is not None:
                        resolved = encounter_refs.get(str(enc_ref))
                        if resolved is None:
                            # Fail loudly rather than mis-file paperwork
                            # against whatever id happens to be around.
                            raise KeyError(
                                f"unresolved encounter_ref {enc_ref!r} "
                                f"(known refs: {sorted(encounter_refs)})"
                            )
                        entity_id = resolved
                    else:
                        entity_id = int(entry["entity_id"])

                    # The op file is written inside the sandbox, so the path
                    # is untrusted: confine it to the task's own deferred dir
                    # or the user's workspace before the daemon (which is not
                    # sandboxed) reads the bytes. Read the *resolved* path the
                    # guard approved, not the original — re-resolving would
                    # reopen the symlink-swap window the check just closed.
                    src = _resolved_source_path(
                        Path(entry["source_path"]), user_temp_dir, config,
                        task.user_id, ctx,
                    )
                    if src is None:
                        logger.warning(
                            "attach_document skipped for task %d: source "
                            "missing or outside the user's workspace: %s",
                            task.id, entry.get("source_path"),
                        )
                        continue
                    _health_documents.attach_document(
                        conn, ctx,
                        raw=src.read_bytes(),
                        filename=entry.get("filename") or src.name,
                        mime=None,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        source="agent",
                        notes=entry.get("notes"),
                        max_bytes=_health_max_document_bytes(config),
                    )
                    count += 1
                elif op == "detach_document":
                    health_db.unlink_document(
                        conn,
                        int(entry["document_id"]),
                        entry["entity_type"],
                        int(entry["entity_id"]),
                    )
                    count += 1
                elif op == "import_csv":
                    from .health import csv_io as _csv_io

                    src = Path(entry["source_path"])
                    if not src.is_file():
                        logger.warning(
                            "import_csv skipped for task %d: source missing %s",
                            task.id, src,
                        )
                        continue
                    csv_text = src.read_text(encoding="utf-8-sig", errors="replace")
                    summary = _csv_io.import_csv(conn, csv_text)
                    logger.info(
                        "Imported CSV for task %d: created=%d skipped_identical=%d "
                        "needs_review=%d biomarkers=%d",
                        task.id, summary.panels_created,
                        summary.panels_skipped_identical,
                        summary.panels_needs_review,
                        summary.biomarkers_created,
                    )
                    count += 1
                elif op == "insert_encounter":
                    eid = health_db.insert_encounter(
                        conn,
                        encounter_date=entry["encounter_date"],
                        encounter_type=entry["encounter_type"],
                        provider=entry.get("provider"),
                        facility=entry.get("facility"),
                        specialty=entry.get("specialty"),
                        reason=entry.get("reason"),
                        notes=entry.get("notes"),
                        dedup_key=entry.get("dedup_key"),
                    )
                    enc_ref = entry.get("ref")
                    if enc_ref:
                        encounter_refs[str(enc_ref)] = eid
                    count += 1
                elif op == "update_encounter":
                    health_db.update_encounter(
                        conn,
                        int(entry["encounter_id"]),
                        **{
                            k: entry[k] for k in (
                                "encounter_date", "encounter_type", "provider",
                                "facility", "specialty", "reason", "notes",
                            ) if k in entry
                        },
                    )
                    count += 1
                elif op == "delete_encounter":
                    health_db.delete_encounter(
                        conn, int(entry["encounter_id"]),
                    )
                    count += 1
                elif op == "insert_diagnosis":
                    health_db.insert_diagnosis(
                        conn,
                        name=entry["name"],
                        status=entry.get("status", "active"),
                        icd10=entry.get("icd10"),
                        date_diagnosed=entry.get("date_diagnosed"),
                        date_resolved=entry.get("date_resolved"),
                        encounter_id=(
                            int(entry["encounter_id"])
                            if entry.get("encounter_id") is not None else None
                        ),
                        severity=entry.get("severity"),
                        notes=entry.get("notes"),
                        dedup_key=entry.get("dedup_key"),
                        reconcile=True,
                    )
                    count += 1
                elif op == "update_diagnosis":
                    update_kwargs = {
                        k: entry[k] for k in (
                            "name", "icd10", "status", "date_diagnosed",
                            "date_resolved", "encounter_id", "severity", "notes",
                        ) if k in entry
                    }
                    if (
                        "encounter_id" in update_kwargs
                        and update_kwargs["encounter_id"] is not None
                    ):
                        update_kwargs["encounter_id"] = int(
                            update_kwargs["encounter_id"],
                        )
                    health_db.update_diagnosis(
                        conn, int(entry["diagnosis_id"]), **update_kwargs,
                    )
                    count += 1
                elif op == "delete_diagnosis":
                    health_db.delete_diagnosis(
                        conn, int(entry["diagnosis_id"]),
                    )
                    count += 1
                elif op in ("link_diagnosis_encounter", "unlink_diagnosis_encounter"):
                    # A condition is seen at several visits, so these add and
                    # remove one link without disturbing the rest. `@ref`
                    # resolves an encounter created earlier in this same batch,
                    # matching attach_document's grammar.
                    enc_ref = entry.get("encounter_ref")
                    if enc_ref is not None:
                        resolved = encounter_refs.get(str(enc_ref))
                        if resolved is None:
                            raise KeyError(
                                f"unresolved encounter_ref {enc_ref!r} "
                                f"(known refs: {sorted(encounter_refs)})"
                            )
                        eid = resolved
                    else:
                        eid = int(entry["encounter_id"])
                    did = int(entry["diagnosis_id"])
                    if op == "link_diagnosis_encounter":
                        if health_db.get_diagnosis(conn, did) is None:
                            raise ValueError(f"unknown diagnosis: {did}")
                        if health_db.get_encounter(conn, eid) is None:
                            raise ValueError(f"unknown encounter: {eid}")
                        health_db.link_diagnosis_encounter(conn, did, eid)
                    else:
                        health_db.unlink_diagnosis_encounter(conn, did, eid)
                    count += 1
                elif op == "insert_immunization":
                    enc_id = entry.get("encounter_id")
                    health_db.insert_immunization(
                        conn,
                        name=entry["name"],
                        date_given=entry["date_given"],
                        product_name=entry.get("product_name"),
                        manufacturer=entry.get("manufacturer"),
                        dose_label=entry.get("dose_label"),
                        lot_number=entry.get("lot_number"),
                        route=entry.get("route"),
                        site=entry.get("site"),
                        administered_by=entry.get("administered_by"),
                        facility=entry.get("facility"),
                        encounter_id=(
                            int(enc_id) if enc_id is not None else None
                        ),
                        cvx_code=entry.get("cvx_code"),
                        notes=entry.get("notes"),
                        source=entry.get("source", "manual"),
                        dedup_key=entry.get("dedup_key"),
                    )
                    count += 1
                elif op == "update_immunization":
                    update_kwargs = {
                        k: entry[k] for k in (
                            "name", "date_given", "product_name",
                            "manufacturer", "dose_label", "lot_number",
                            "route", "site", "administered_by", "facility",
                            "encounter_id", "cvx_code", "notes",
                        ) if k in entry
                    }
                    if (
                        "encounter_id" in update_kwargs
                        and update_kwargs["encounter_id"] is not None
                    ):
                        update_kwargs["encounter_id"] = int(
                            update_kwargs["encounter_id"],
                        )
                    health_db.update_immunization(
                        conn, int(entry["immunization_id"]),
                        **update_kwargs,
                    )
                    count += 1
                elif op == "delete_immunization":
                    health_db.delete_immunization(
                        conn, int(entry["immunization_id"]),
                    )
                    count += 1
                elif op == "bulk_insert_immunizations":
                    prefix = entry.get("dedup_key_prefix") or ""
                    for i, r in enumerate(entry.get("rows") or []):
                        if not isinstance(r, dict):
                            continue
                        if not r.get("name") or not r.get("date_given"):
                            continue
                        dk = f"{prefix}:{i}" if prefix else None
                        health_db.insert_immunization(
                            conn,
                            name=r["name"],
                            date_given=r["date_given"],
                            product_name=r.get("product_name"),
                            manufacturer=r.get("manufacturer"),
                            notes=r.get("notes"),
                            source=r.get("source", "import"),
                            dedup_key=dk,
                            reconcile=True,
                        )
                        count += 1
                elif op == "set_setting":
                    key = entry["key"]
                    value = entry.get("value")
                    if key == "display_units_merge":
                        existing = health_db.get_settings(conn).get(
                            "display_units"
                        ) or {}
                        if isinstance(value, dict):
                            existing.update(value)
                            health_db.set_setting(conn, "display_units", existing)
                            count += 1
                    else:
                        health_db.set_setting(conn, key, value)
                        count += 1
                else:
                    logger.warning(
                        "Unknown health op %r in deferred file for task %d",
                        op, task.id,
                    )
                    continue
                conn.commit()
            except (
                KeyError, ValueError, OSError, sqlite3.Error, DocumentError,
            ) as e:
                # OSError + DocumentError cover the document ops, which touch
                # the filesystem and can refuse a type / size — one bad
                # attachment must not abort the rest of the batch.
                #
                # Discard whatever the failing op wrote before it raised.
                # Without this its partial work is still open on the
                # connection, and the *next* op's commit would sweep it in —
                # so an attach_document that inserted its row and then failed
                # to write the bytes would silently persist a row pointing at
                # a file that never landed. Prior ops are already committed,
                # so this can only ever discard the failed one.
                conn.rollback()
                # Log at ERROR so operators see silent op losses (health
                # records are non-idempotent — "silently lost" is a sharp
                # failure mode). Also persist the failing entry to a
                # sidecar file so the user/operator can recover it.
                logger.error(
                    "Failed to process health op %r for task %d: %s",
                    entry.get("op") if isinstance(entry, dict) else entry,
                    task.id, e,
                )
                failures.append({
                    "op": entry if isinstance(entry, dict) else {"raw": entry},
                    "error": f"{type(e).__name__}: {e}",
                })

    if count:
        logger.info("Processed %d deferred health ops for task %d", count, task.id)
    if failures:
        failure_path = (
            user_temp_dir / f"task_{task.id}_health_op_failures.json"
        )
        try:
            existing: list[Any] = []
            if failure_path.exists():
                try:
                    existing = json.loads(failure_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    existing = []
            failure_path.write_text(
                json.dumps(existing + failures, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(
                "Failed to write health op failure sidecar for task %d: %s",
                task.id, e,
            )
    path.unlink(missing_ok=True)
    return count


def _process_deferred_garmin_import(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Run a delegated Garmin GPS-track import into the user's location.db.

    A sandboxed ``istota-skill location import-garmin-tracks`` call can't
    decrypt the Garmin token blob (the master key is stripped in the
    sandbox), so it writes ``task_<id>_garmin_import.json`` and the
    scheduler runs the import here, in the daemon process where
    ``ISTOTA_SECRET_KEY`` is in scope. The result is pushed back to the user
    as a notification. User id always comes from the task.
    """
    loaded = _load_deferred_json(
        user_temp_dir, task.id, "garmin_import", expected_type=dict,
    )
    if loaded is None:
        return 0
    path, data = loaded

    if not config.is_module_enabled(task.user_id, "location"):
        logger.info(
            "Skipping deferred garmin import for task %d: location module disabled",
            task.id,
        )
        path.unlink(missing_ok=True)
        return 0

    try:
        days_back = max(1, min(365, int(data.get("days_back", 7) or 7)))
    except (TypeError, ValueError):
        days_back = 7

    from .notifications import send_notification

    try:
        from istota.health import garmin as gm
        from istota.location.garmin_import import ImportOptions, import_tracks
    except ImportError as e:
        logger.warning("Garmin import unavailable for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return 0

    try:
        result = import_tracks(
            task.user_id, framework_db_path=config.db_path, config=config,
            options=ImportOptions(days_back=days_back),
        )
    except gm.GarminAuthError:
        send_notification(
            config, task.user_id,
            "🗺️ Garmin track import couldn't run — Garmin isn't connected. "
            "Connect it in Settings → Connected services.",
            purpose="notification",
        )
        path.unlink(missing_ok=True)
        return 0
    except gm.GarminRateLimited:
        send_notification(
            config, task.user_id,
            "🗺️ Garmin track import was rate-limited by Garmin — try again later.",
            purpose="notification",
        )
        path.unlink(missing_ok=True)
        return 0
    except Exception as e:  # noqa: BLE001 — a bad import must not wedge the drain
        logger.warning("Deferred garmin import failed for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return 0

    if result.activities:
        msg = (
            f"🗺️ Garmin track import: added {result.inserted_total} GPS points "
            f"from {result.activities} activit"
            f"{'y' if result.activities == 1 else 'ies'} to your location "
            "history (last %d days)." % days_back
        )
    else:
        msg = (
            "🗺️ Garmin track import: no new GPS activities found in the last "
            f"{days_back} days."
        )
    send_notification(config, task.user_id, msg, purpose="notification")
    logger.info(
        "Deferred garmin import task %d: inserted=%d activities=%d",
        task.id, result.inserted_total, result.activities,
    )
    path.unlink(missing_ok=True)
    return result.inserted_total


def _purge_deferred_files_for_retry(task: db.Task, user_temp_dir: Path) -> None:
    """Delete the task's accumulated deferred-op files before a retry.

    ISSUE-074: producers like ``_defer_kg_op`` *append* to ``task_{id}_*.json``;
    on retry the same task.id is reused, so a previously-failed attempt's ops
    would replay alongside the new attempt's. Non-idempotent ops (``invalidate``,
    ``delete`` for KG; subtask creation; outbound emails; user alerts) make
    replays harmful, not just redundant. Clear the slate on every retry.

    The result file is left in place — it's scoped per-task, not per-attempt,
    and the model overwrites it. It is also the only such file still in this
    directory: both prompt halves moved to the task control directory
    (``{temp_dir}/.control/{user_id}/task_{id}``), which this function has
    never walked.
    """
    if not user_temp_dir.is_dir():
        return
    purged: list[str] = []
    for suffix in _KNOWN_DEFERRED_SUFFIXES:
        path = user_temp_dir / f"task_{task.id}_{suffix}.json"
        if path.exists():
            try:
                path.unlink()
                purged.append(suffix)
            except OSError as e:
                logger.warning(
                    "Could not purge deferred %s for task %d retry: %s",
                    suffix, task.id, e,
                )
    if purged:
        logger.info(
            "Purged deferred files for task %d retry: %s",
            task.id, ", ".join(purged),
        )


def _warn_unconsumed_deferred_files(task: db.Task, user_temp_dir: Path) -> None:
    """Log a WARN for any task-scoped file in user_temp_dir that doesn't
    match a recognized deferred-file name.

    Catches three failure shapes:
    - Hallucinated names that drop the ``task_`` prefix (e.g.
      ``{id}_skip_log.json``) — would never match the consumers' exact
      filename lookup.
    - Canonical ``task_{id}_<unknown>.json`` shapes for handlers that don't
      exist — also silently ignored by the dispatch.
    - A descriptive name with the id as a *suffix* (e.g.
      ``cleanup_stray_files_{id}.json``, the shape that triggered ISSUE-135) —
      the id is delimited by ``_`` so it can't collide with another task's id
      as a substring.
    """
    if not user_temp_dir.is_dir():
        return
    known_filenames = {
        f"task_{task.id}_{suffix}.json" for suffix in _KNOWN_DEFERRED_SUFFIXES
    }
    known_filenames.update(
        f"task_{task.id}_{suffix}.json" for suffix in _KNOWN_ARTIFACT_SUFFIXES
    )
    # The one static task-scoped file the framework still puts in this
    # directory: the model writes `task_{id}_result.txt` from inside the
    # sandbox and the daemon reads it back, so a writable per-user directory
    # is where it belongs.
    #
    # The two prompt halves used to be named here as well. They live in the
    # task control directory now (`{temp_dir}/.control/{user_id}/task_{id}`),
    # which nothing sandboxed can write, so a `task_{id}_prompt.txt` or
    # `task_{id}_system_prompt.txt` appearing *here* is by definition not
    # ours — exactly the case worth seeing, and the reason the names are gone
    # rather than kept as harmless. `briefing_meta.json` moved with them and
    # is deliberately not listed either: it was never in this set, so before
    # the move it warned on every briefing task, and adding it now would
    # silence a name the framework no longer writes here.
    known_filenames.add(f"task_{task.id}_result.txt")

    suspicious: list[Path] = []
    seen: set[Path] = set()

    def _flag(path: Path) -> None:
        if path not in seen:
            seen.add(path)
            suspicious.append(path)

    # Shape 1: missing the ``task_`` prefix entirely (id as a leading token).
    for path in user_temp_dir.glob(f"{task.id}_*"):
        _flag(path)
    # Shape 2: canonical prefix but unknown suffix.
    for path in user_temp_dir.glob(f"task_{task.id}_*"):
        if path.name not in known_filenames:
            _flag(path)
    # Shape 3: descriptive name with the id as a trailing token (ISSUE-135).
    for path in user_temp_dir.glob(f"*_{task.id}.json"):
        if path.name not in known_filenames:
            _flag(path)

    # The hint names what a caller should write, so a retired suffix is left
    # out of it — recognized enough not to be called a hallucination above,
    # not offered back as a name to use.
    expected = "|".join(
        s for s in _KNOWN_DEFERRED_SUFFIXES if s not in _RETIRED_DEFERRED_SUFFIXES
    )
    for path in suspicious:
        logger.warning(
            "Unrecognized deferred file for task %d: %s "
            "(expected name: task_%d_<%s>.json)",
            task.id, path.name, task.id, expected,
        )
