"""Conversation context selection using Claude CLI."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Config
from .db import ConversationMessage, TalkMessage
from .llm_json import find_fenced_block
from .talk import clean_message_content

# What a triage inference reports it spent. The caller supplies the sink because
# it is the caller that knows which task, user and source_type the row belongs
# to; this module only knows that an inference happened and what it cost.
#
# It covers the CLI path only — the inference this module performs itself. A
# caller-supplied ``completer`` is the caller's own object, built with its own
# config in scope, so its accounting is the caller's too (see
# ``executor._build_native_completer``).
UsageSink = Callable[..., None]


def _format_created_at(created_at: str | None, user_tz: ZoneInfo | None) -> str:
    """Format a SQLite `datetime('now')` UTC string in user_tz wall-clock time.

    Falls back to a UTC-suffixed slice if parsing fails or no tz is provided.
    """
    if not created_at:
        return "unknown"
    if user_tz is None:
        return created_at[:16]
    try:
        dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return created_at[:16]
    return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).strftime("%Y-%m-%d %H:%M")

logger = logging.getLogger("istota.context")

# Source types that represent scheduled/background tasks — not real user messages
_SCHEDULED_SOURCE_TYPES = {"scheduled", "cron", "briefing", "heartbeat"}


def _speaker_label(msg: ConversationMessage) -> str:
    """Who the prompt half of this turn is attributed to.

    `external_sender` is checked first and unconditionally: it is the one field
    derived from the message's own envelope rather than from the task's
    ownership, so when it is set it is the only trustworthy answer (ISSUE-226).
    An email from an external contact must not be rendered as the principal's
    own turn — the `<email_content>` guard inside the body already tells the
    model the text is third-party input, and the speaker label used to
    contradict it.
    """
    if msg.external_sender:
        return f"External sender <{msg.external_sender}>"
    source_type = getattr(msg, "source_type", "talk") or "talk"
    if source_type in _SCHEDULED_SOURCE_TYPES:
        return "Scheduled"
    return msg.user_id if msg.user_id else "User"


def select_relevant_context(
    current_prompt: str,
    history: list[ConversationMessage],
    config: Config,
    completer: "Callable[[str], str | None] | None" = None,
    on_usage: "UsageSink | None" = None,
) -> list[ConversationMessage]:
    """
    Select which previous messages are relevant to the current request.

    Hybrid approach:
    - Recent N messages (always_include_recent) are always included without selection.
    - Older messages beyond that are triaged by a selection model.
    - If selection is disabled or history is short, all messages are included.

    ``completer`` is a ``prompt -> raw_output | None`` callable for triage
    inference. When omitted, the default `claude` CLI path runs — so the active
    brain's transport can be injected (the native brain passes its own provider
    completer instead of shelling out to the CLI it isn't using). See
    ``_triage_older_messages``.

    ``on_usage`` receives what the CLI path spent, so the caller can record it
    (ISSUE-272). This is the daemon's highest-frequency model call — it runs on
    every conversational task whose older history exceeds
    ``skip_selection_threshold`` — and it had no row of any kind. It covers the
    CLI path only; an injected ``completer`` is the caller's own object and
    reports its own spend.

    Returns a filtered list of ConversationMessages in chronological order.
    On any triage failure, fails open: includes all the older messages
    (matching the triage prompt's own "when in doubt, include" rule) so a
    transient triage hiccup never silently drops context.
    """
    if not history:
        return []

    # If selection is disabled, include all messages
    if not config.conversation.use_selection:
        logger.debug("Selection disabled, including all %d messages", len(history))
        return history

    # Skip selection for short histories - include all messages
    threshold = config.conversation.skip_selection_threshold
    if len(history) <= threshold:
        logger.debug(
            "Short history (%d msgs ≤ %d), including all",
            len(history),
            threshold,
        )
        return history

    # Split history into guaranteed recent and older triageable messages
    recent_count = config.conversation.always_include_recent
    if recent_count >= len(history):
        logger.debug(
            "History (%d msgs) within always_include_recent (%d), including all",
            len(history),
            recent_count,
        )
        return history

    guaranteed_recent = history[-recent_count:] if recent_count > 0 else []
    older_history = history[:-recent_count] if recent_count > 0 else history

    # If no older messages to triage, just return the guaranteed recent
    if not older_history:
        return guaranteed_recent

    # Triage older messages with the selection model
    selected_older = _triage_older_messages(
        current_prompt, older_history, config, completer=completer, on_usage=on_usage
    )

    # Combine: selected older + guaranteed recent (chronological order)
    selected = selected_older + guaranteed_recent

    logger.info(
        "Context: %d triaged + %d recent = %d/%d messages",
        len(selected_older),
        len(guaranteed_recent),
        len(selected),
        len(history),
    )
    return selected


def _claude_cli_triage(
    prompt: str,
    model: str,
    timeout: float,
    config: Config,
    on_usage: "UsageSink | None" = None,
) -> str | None:
    """Default triage inference: a one-shot, tool-less run through ``ClaudeCodeBrain``.

    Returns the raw model output, or None on a failed attempt / timeout /
    missing CLI / any error. JSON parsing and validation stay in
    ``_parse_relevant_ids`` so they apply uniformly across inference backends.

    **Why the brain rather than a bare `subprocess.run` (ISSUE-272).** This used
    to spawn `claude -p - --model X` itself, which meant it also had to grow its
    own copy of everything the brain already owns. Four of those mattered and
    three were missing outright: `--output-format json` and the two-shape
    envelope parse (without which there is no usage to report at all); the
    ``CLAUDE_CODE_DISABLE_ADVISOR_TOOL`` guard, absent here, so a host
    ``~/.claude/settings.json`` carrying ``advisorModel`` silently ran an
    advisor on every conversational turn; and the model namespace, since the
    caller resolves the name through a brain that may not be this one. The
    transient-API retry comes along too, bounded as described below.

    ``ClaudeCodeBrain`` is named rather than ``make_brain(config.brain)`` on
    purpose: a native deployment never reaches here (the executor injects a
    provider completer), and a tmux deployment must not — driving an
    interactive TUI session to triage a prompt is not what that brain is for.
    So this is the CLI path for every brain that isn't native, exactly as before.

    ``on_usage`` receives this attempt's ``BrainUsage`` whether the attempt
    succeeded or not: a run that reached the model and then failed spent real
    tokens. It is called before the success check for that reason, and its own
    failure is swallowed — telemetry must never turn a working triage into a
    fail-open one.

    **``selection_timeout`` bounds the whole triage, not one attempt.** The
    brain's retry ladder is three attempts with a provider-supplied backoff
    capped at 60s each, which is right for the task-less origins that share this
    path — a nightly sleep cycle has nobody waiting on it. Triage does: it sits
    between the user's message and the main brain call, holding a worker slot,
    and its failure mode is *free* (fail open, include everything, move on). So
    the request carries a deadline as its ``cancel_check``: the backoff polls it
    and stops sleeping once the budget is spent, which both caps the wall clock
    at roughly ``timeout`` and makes the wait interruptible, where before it was
    a bare ``time.sleep`` no cancel could land on. A retry that fits inside the
    budget still happens — that is the case where retrying is strictly better
    than failing open.

    ``ClaudeCodeBrain`` is imported inside the ``try`` so an import failure
    fails open like every other triage error instead of propagating into prompt
    assembly; ``executor`` imports this module at import time, so the
    ``build_model_cli_env`` import has to be deferred regardless.
    """
    budget = max(1, int(timeout))
    deadline = time.monotonic() + budget
    try:
        from .brain import BrainRequest, ClaudeCodeBrain
        from .executor import build_model_cli_env

        req = BrainRequest(
            prompt=prompt,
            # Text-only: no tools, so no --dangerously-skip-permissions and no
            # tool flags. The absence of the bypass is what keeps it tool-less.
            allowed_tools=[],
            # The executor creates this before context building, so it exists by
            # the time triage runs. It also stops the CLI from inheriting the
            # daemon's own working directory, which on a repo checkout meant
            # reading that repo's CLAUDE.md and settings on every turn.
            cwd=config.temp_dir,
            # Not the daemon's own environment (ISSUE-232). This runs on a
            # prompt assembled from conversation history, so it is the one
            # `claude` spawn driven by user-influenced input; it needs the
            # CLI's auth credential and nothing else.
            env=build_model_cli_env(config),
            # subprocess.run takes an int; a sub-second config value would
            # floor to a timeout of 0 and fail every call.
            timeout_seconds=budget,
            model=model,
            streaming=False,
            cancel_check=lambda: time.monotonic() >= deadline,
        )
        result = ClaudeCodeBrain().execute(req)
    except Exception as e:  # never let triage crash context assembly
        logger.warning("Context triage error: %s", e)
        return None

    _report_triage_usage(on_usage, result, model)

    if result.stop_reason == "cancelled":
        # Not a user cancel — nothing wires one here. The deadline above fired
        # mid-backoff, which is the budget working as intended.
        logger.warning("Context triage gave up after its %ds budget", budget)
        return None
    if not result.success:
        logger.warning(
            "Context triage failed (stop_reason=%s): %s",
            result.stop_reason,
            (result.result_text or "")[:200],
        )
        return None
    return result.result_text


def _run_triage(
    selection_prompt: str,
    config: Config,
    completer: "Callable[[str], str | None] | None",
    on_usage: "UsageSink | None",
    label: str,
) -> str | None:
    """Get raw triage output from whichever inference backend this deployment uses.

    A caller-supplied ``completer`` (the native brain's own provider) wins; the
    ``claude`` CLI is the default. Both fail open by returning None, which
    ``_parse_relevant_ids`` and its callers read as "include everything".

    The model name is resolved through ``ClaudeCodeBrain`` rather than the
    configured brain because the CLI is what runs it, and a name resolved in
    another brain's namespace (a native deployment's endpoint model, say) is not
    one `claude --model` accepts. That mismatch was reachable before: the caller
    resolved through ``make_brain(config.brain)`` and then handed the answer to
    the CLI regardless.
    """
    if completer is not None:
        try:
            return completer(selection_prompt)
        except Exception as e:  # never let triage crash context assembly
            logger.warning("%s triage error: %s", label, e)
            return None

    try:
        from .brain import ClaudeCodeBrain

        model = ClaudeCodeBrain().resolve_model_name(
            config.conversation.selection_model
        )
    except Exception as e:  # never let triage crash context assembly
        logger.warning("%s triage model resolution failed: %s", label, e)
        return None

    return _claude_cli_triage(
        selection_prompt,
        model,
        config.conversation.selection_timeout,
        config,
        on_usage,
    )


def _report_triage_usage(
    on_usage: "UsageSink | None", result: Any, requested_model: str
) -> None:
    """Hand one triage attempt's spend to the caller's sink. Never raises."""
    if on_usage is None or getattr(result, "usage", None) is None:
        return
    try:
        on_usage(
            result.usage,
            model=result.model_used or requested_model,
            brain_kind=result.brain_kind,
            stop_reason=result.stop_reason,
            success=result.success,
        )
    except Exception:
        logger.warning("Context triage usage sink failed", exc_info=True)


def _parse_relevant_ids(raw: str | None, n: int) -> list[int] | None:
    """Parse a `{"relevant_ids": [...]}` triage response.

    Returns sorted valid indices in [0, n), or None if the output is missing /
    unparseable / malformed. None signals the caller to fail open (include all
    older messages) rather than silently dropping context.
    """
    if not raw:
        return None
    output = raw.strip()

    fenced = find_fenced_block(output)
    if fenced is not None:
        output = fenced
    else:
        json_match = re.search(r"\{.*\}", output, re.DOTALL)
        if json_match:
            output = json_match.group(0)

    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        logger.warning("Context triage JSON parse error: %s (output: %s)", e, output[:200])
        return None

    relevant_ids = data.get("relevant_ids") if isinstance(data, dict) else None
    if not isinstance(relevant_ids, list):
        logger.warning("Context triage returned invalid format: %s", data)
        return None

    valid_ids = [idx for idx in relevant_ids if isinstance(idx, int) and 0 <= idx < n]
    return sorted(valid_ids)


def _triage_older_messages(
    current_prompt: str,
    older_history: list[ConversationMessage],
    config: Config,
    completer: "Callable[[str], str | None] | None" = None,
    on_usage: "UsageSink | None" = None,
) -> list[ConversationMessage]:
    """Run the selection model to triage older messages. Returns selected messages in order.

    On any triage failure (timeout, transport error, unparseable output) this
    fails open and returns *all* the older messages, matching the prompt's own
    "when in doubt, include" rule.
    """

    def _format_triage_msg(i: int, msg: ConversationMessage) -> str:
        ts = msg.created_at[:16] if msg.created_at else "unknown"
        lines = f"[{i}] ({ts}) {_speaker_label(msg)}: {msg.prompt}\nBot: {msg.result}"
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                lines += f"\n{actions_line}"
        return lines

    history_text = "\n\n".join(
        _format_triage_msg(i, msg)
        for i, msg in enumerate(older_history)
    )

    selection_prompt = f"""You are helping select relevant conversation context for a chatbot.

Current user request:
{current_prompt}

OLDER messages from this conversation (the {config.conversation.always_include_recent} most recent messages are already included separately):

{history_text}

Which of these OLDER messages contain information relevant to understanding or answering the current request?

NOTE: The {config.conversation.always_include_recent} most recent messages are already included. Select which of these older messages also provide useful context.

Respond with ONLY a JSON object in this exact format:
{{"relevant_ids": [0, 2, 5]}}

Use an empty array if none of these older messages are relevant: {{"relevant_ids": []}}

Rules:
- When in doubt, INCLUDE the message — more context is better than missing context
- Include messages that could help answer or provide background for the current request
- Include messages that establish context, preferences, or facts the user might be referring to
- Include messages about ongoing topics, even if not directly referenced
- Only exclude messages that are clearly unrelated (different topic, fully resolved, trivial small talk)
- Respond with ONLY the JSON, no other text"""

    raw = _run_triage(selection_prompt, config, completer, on_usage, "Context")

    ids = _parse_relevant_ids(raw, len(older_history))
    if ids is None:
        # Fail open: a triage hiccup should add context, not silently drop it.
        logger.warning(
            "Context triage unavailable; including all %d older messages",
            len(older_history),
        )
        return older_history

    selected = [older_history[idx] for idx in ids]
    logger.debug(
        "Triage selected %d/%d older messages (ids: %s)",
        len(selected),
        len(older_history),
        ids,
    )
    return selected


def format_context_for_prompt(
    messages: list[ConversationMessage],
    truncation: int = 3000,
    user_tz: ZoneInfo | None = None,
) -> str:
    """Format selected context messages for inclusion in the prompt.

    Args:
        messages: Conversation messages to format.
        truncation: Max chars per bot response. 0 to disable truncation.
        user_tz: If provided, render `created_at` (stored UTC) in this zone.
    """
    if not messages:
        return ""

    formatted = []
    for msg in messages:
        timestamp = _format_created_at(msg.created_at, user_tz)
        formatted.append(f"[{timestamp}] {_speaker_label(msg)}: {msg.prompt}")
        result = msg.result
        if truncation > 0 and len(result) > truncation:
            result = result[:truncation] + "...[truncated]"
        formatted.append(f"[{timestamp}] Bot: {result}")

        # Append compact actions summary if available
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                formatted.append(actions_line)

    return "\n".join(formatted)


_MAX_ACTIONS = 15


def _format_actions_line(actions_json: str) -> str | None:
    """Format actions_taken JSON into a compact summary line."""
    try:
        actions: list[Any] = json.loads(actions_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not actions:
        return None
    display = actions[:_MAX_ACTIONS]
    suffix = f" +{len(actions) - _MAX_ACTIONS} more" if len(actions) > _MAX_ACTIONS else ""
    return f"[Actions: {' | '.join(str(a) for a in display)}{suffix}]"


# ---------------------------------------------------------------------------
# Talk API-based context pipeline
# ---------------------------------------------------------------------------

_REFERENCE_ID_PATTERN = re.compile(r"^istota:task:(\d+):(\w+)$")


def _parse_reference_id(ref_id: str | None) -> tuple[int | None, str | None]:
    """Parse an istota referenceId string.

    Returns (task_id, tag) where tag is "result", "ack", or "progress".
    Returns (None, None) for non-matching or missing referenceIds.
    """
    if not ref_id:
        return None, None
    m = _REFERENCE_ID_PATTERN.match(ref_id)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def build_talk_context(
    raw_messages: list[dict],
    bot_username: str,
    task_metadata: dict[int, dict],
) -> list[TalkMessage]:
    """Convert raw Talk API messages into filtered TalkMessage list.

    Args:
        raw_messages: Messages from TalkClient.fetch_chat_history() (oldest-first).
        bot_username: Bot's Nextcloud username for identifying bot messages.
        task_metadata: Dict from get_task_metadata_for_context() mapping
            task_id -> {"actions_taken": ..., "source_type": ...}.

    Returns filtered list of TalkMessages (oldest-first), excluding system
    messages, deleted messages, ack messages, and progress messages.
    """
    result = []
    for msg in raw_messages:
        # Skip system messages
        if msg.get("messageType") == "system":
            continue

        # Skip deleted messages
        if msg.get("deleted"):
            continue

        ref_id = msg.get("referenceId") or None
        task_id, tag = _parse_reference_id(ref_id)

        # Skip ack and progress messages
        if tag in ("ack", "progress"):
            continue

        actor_id = msg.get("actorId", "")
        is_bot = actor_id == bot_username

        # Clean content (resolve placeholders)
        content = clean_message_content(msg, bot_username=bot_username if not is_bot else None)

        # Determine message role and enrich with task metadata
        actions_taken = None
        message_role = "user"
        if is_bot:
            message_role = "bot_result"
            if task_id and task_id in task_metadata:
                meta = task_metadata[task_id]
                actions_taken = meta.get("actions_taken")
                source_type = meta.get("source_type", "")
                if source_type in _SCHEDULED_SOURCE_TYPES:
                    message_role = "scheduled"

        result.append(TalkMessage(
            message_id=msg.get("id", 0),
            actor_id=actor_id,
            actor_display_name=msg.get("actorDisplayName", actor_id),
            is_bot=is_bot,
            content=content,
            timestamp=msg.get("timestamp", 0),
            actions_taken=actions_taken,
            message_role=message_role,
            task_id=task_id,
        ))

    return result


def select_relevant_talk_context(
    current_prompt: str,
    messages: list[TalkMessage],
    config: "Config",
    completer: "Callable[[str], str | None] | None" = None,
    on_usage: "UsageSink | None" = None,
) -> list[TalkMessage]:
    """Select relevant Talk messages for context, mirroring select_relevant_context().

    Uses the same hybrid approach: guaranteed recent messages + LLM triage of older.
    The Talk API may fetch many messages (talk_context_limit), but we only triage
    the most recent `lookback_count` to keep the selection prompt manageable.

    ``completer`` is a ``prompt -> raw_output | None`` callable for triage
    inference (see ``select_relevant_context``); None uses the `claude` CLI.
    ``on_usage`` records what that CLI path spent, same as there — this is the
    second of the two call sites, and measuring only one of them would
    undercount by whatever share of traffic arrives over Talk.
    On any triage failure, fails open and includes all the older messages.
    """
    if not messages:
        return []

    # Always cap at lookback_count (hard limit on context size)
    lookback = config.conversation.lookback_count
    if len(messages) > lookback:
        messages = messages[-lookback:]

    if not config.conversation.use_selection:
        return messages

    threshold = config.conversation.skip_selection_threshold
    if len(messages) <= threshold:
        return messages

    recent_count = config.conversation.always_include_recent
    if recent_count >= len(messages):
        return messages

    guaranteed_recent = messages[-recent_count:] if recent_count > 0 else []
    older = messages[:-recent_count] if recent_count > 0 else messages

    if not older:
        return guaranteed_recent

    selected_older = _triage_older_talk_messages(
        current_prompt, older, config, completer=completer, on_usage=on_usage
    )
    selected = selected_older + guaranteed_recent

    logger.info(
        "Talk context: %d triaged + %d recent = %d/%d messages",
        len(selected_older), len(guaranteed_recent), len(selected), len(messages),
    )
    return selected


def _triage_older_talk_messages(
    current_prompt: str,
    older: list[TalkMessage],
    config: "Config",
    completer: "Callable[[str], str | None] | None" = None,
    on_usage: "UsageSink | None" = None,
) -> list[TalkMessage]:
    """Run the selection model to triage older Talk messages.

    On any triage failure (timeout, transport error, unparseable output) this
    fails open and returns *all* the older messages, matching the prompt's own
    "when in doubt, include" rule.
    """

    def _format_msg(i: int, msg: TalkMessage) -> str:
        ts = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        speaker = "Bot" if msg.is_bot else msg.actor_id
        lines = f"[{i}] ({ts}) {speaker}: {msg.content}"
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                lines += f"\n{actions_line}"
        return lines

    history_text = "\n\n".join(_format_msg(i, msg) for i, msg in enumerate(older))

    selection_prompt = f"""You are helping select relevant conversation context for a chatbot.

Current user request:
{current_prompt}

OLDER messages from this conversation (the {config.conversation.always_include_recent} most recent messages are already included separately):

{history_text}

Which of these OLDER messages contain information relevant to understanding or answering the current request?

NOTE: The {config.conversation.always_include_recent} most recent messages are already included. Select which of these older messages also provide useful context.

Respond with ONLY a JSON object in this exact format:
{{"relevant_ids": [0, 2, 5]}}

Use an empty array if none of these older messages are relevant: {{"relevant_ids": []}}

Rules:
- When in doubt, INCLUDE the message — more context is better than missing context
- Include messages that could help answer or provide background for the current request
- Include messages that establish context, preferences, or facts the user might be referring to
- Include messages about ongoing topics, even if not directly referenced
- Only exclude messages that are clearly unrelated (different topic, fully resolved, trivial small talk)
- Respond with ONLY the JSON, no other text"""

    raw = _run_triage(selection_prompt, config, completer, on_usage, "Talk context")

    ids = _parse_relevant_ids(raw, len(older))
    if ids is None:
        # Fail open: a triage hiccup should add context, not silently drop it.
        logger.warning(
            "Talk context triage unavailable; including all %d older messages",
            len(older),
        )
        return older

    selected = [older[idx] for idx in ids]
    logger.debug("Talk triage selected %d/%d older messages", len(selected), len(older))
    return selected


def format_talk_context_for_prompt(
    messages: list[TalkMessage],
    truncation: int = 3000,
    user_tz: ZoneInfo | None = None,
) -> str:
    """Format Talk messages for inclusion in the prompt.

    Individual message format (not paired), showing all participants.

    Args:
        user_tz: If provided, render Talk message timestamps in this zone.
            Defaults to UTC for backward compat.
    """
    if not messages:
        return ""

    tz = user_tz or timezone.utc
    formatted = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.timestamp, tz=tz).strftime("%Y-%m-%d %H:%M")

        if msg.is_bot:
            speaker = f"Bot (task {msg.task_id})" if msg.task_id else "Bot"
            content = msg.content
            if truncation > 0 and len(content) > truncation:
                content = content[:truncation] + "...[truncated]"
            formatted.append(f"[{ts}] {speaker}: {content}")
            if msg.actions_taken:
                actions_line = _format_actions_line(msg.actions_taken)
                if actions_line:
                    formatted.append(actions_line)
        else:
            if msg.message_role == "scheduled":
                speaker = "Scheduled"
            else:
                speaker = msg.actor_id or "User"
            formatted.append(f"[{ts}] {speaker}: {msg.content}")

    return "\n".join(formatted)
