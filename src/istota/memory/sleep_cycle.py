"""Nightly sleep cycle — extract long-term memories from the day's interactions."""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from .. import db
from ..brain import BrainRequest, make_brain
from ..brain import primary_brain_unavailable, report_brain_result
from ..config import Config
from ..usage import SYSTEM_USER_ID
from ..storage import (
    _get_mount_path,
    open_user_skill_overlays,
    resolve_user_config_dir,
    get_user_memories_path,
    get_user_playbooks_path,
    get_channel_memories_path,
    get_channel_memory_path,
    read_regular_file,
    read_user_memory_v2,
    read_dated_memories,
    read_channel_memory,
    write_regular_file,
    _DATED_MEMORY_PATTERN,
)

logger = logging.getLogger("istota.sleep_cycle")

# Maximum chars of day data to include in extraction prompt
MAX_DAY_DATA_CHARS = 50000

# Minimum per-task budget to avoid tiny fragments
_MIN_TASK_BUDGET = 500

# Source type classification for extraction quality
INTERACTIVE_SOURCE_TYPES = frozenset({"talk", "email", "cli"})
AUTOMATED_SOURCE_TYPES = frozenset({"cron", "briefing", "subtask"})

# Suggested predicates with usage hints, shown in the extraction prompt.
# Not enforced — freeform predicates are accepted and treated as multi-valued
# by the knowledge graph (only SINGLE_VALUED_PREDICATES trigger supersession).
SUGGESTED_PREDICATES = {
    # Single-valued (new value supersedes old — use only when one value is correct at a time)
    "works_at": "employer or organization (single-valued, supersedes)",
    "lives_in": "permanent residence city/country (single-valued, supersedes)",
    "has_role": "job title or role (single-valued, supersedes)",
    "has_status": "current life status like 'on sabbatical' or 'job hunting' (single-valued, supersedes)",
    # Temporary (coexist with permanent facts, auto-flagged)
    "staying_in": "temporary location like a trip or hotel (temporary, use valid_from/valid_until for dates)",
    "visiting": "short visit to a place (temporary, use valid_from/valid_until for dates)",
    "traveled_to": "completed trip or visit to a location (use valid_from/valid_until for dates)",
    "has_scheduled_procedure": "ongoing medical workup or scheduled procedure — context only, NOT the date (calendar owns the date)",
    "has_medical_workup": "ongoing diagnostic process — context only, NOT the date (calendar owns the date)",
    # Multi-valued (concurrent facts allowed)
    "works_on": "project, product, or initiative",
    "uses_tech": "software, programming language, or digital tool (not physical objects)",
    "knows": "skill, language, person, or domain knowledge",
    "speaks": "spoken/written language",
    "prefers": "preference or habit (diet, communication style, tools, etc.)",
    "allergic_to": "allergy or intolerance",
    "owns": "vehicle, property, or significant possession",
    "relates_to": "relationship between entities (use when no specific predicate fits)",
    "has_family_member": "family relationship — object encodes both kind and name, e.g. 'brother: max'",
    "interested_in": "topic, film, book, or creative interest the user has expressed",
    "completed": "finished project, task, or milestone — pair with valid_from for the completion date",
    "decided": "explicit decision or commitment",
    "acquired": "purchased, ordered, or otherwise obtained an item — pair with valid_from for the acquisition date",
    "disposed_of": "returned, sold, gave away, or got rid of an item — pair with valid_from; use the same object string as the prior 'acquired' fact",
    "grew_up_in": "where the user grew up — biographical, no valid_from",
    "born_in": "birth city/country — biographical",
}

# Hard cap on object length. Long objects make fuzzy dedup unreliable and
# crowd out other prompt context when current facts are loaded back in.
# Matches the prompt instruction ("under 10 words / 100 chars").
MAX_FACT_OBJECT_CHARS = 100

# Sentinel output from Claude indicating nothing worth saving
NO_NEW_MEMORIES = "NO_NEW_MEMORIES"

# Pattern for `ref:N` markers in dated memory bullets — used to attach
# per-task topics (from the extraction's TOPICS section) to the chunk that
# contains the bullet, rather than collapsing to a single dominant topic
# per file.
_REF_PATTERN = re.compile(r"\bref:(\d+)\b")

# Used by the post-extraction sanity check — real memory output is a list of
# `- ` bullets. Anything else (process narration, error messages, single
# paragraphs) is treated as malformed.
_BULLET_PATTERN = re.compile(r"(?m)^\s*-\s+\S")


def _has_bullet_points(text: str) -> bool:
    """True if `text` contains at least one markdown bullet line."""
    return bool(_BULLET_PATTERN.search(text or ""))


def _topics_per_chunk(
    chunks: list[str], extracted_topics: dict[str, str]
) -> list[str | None]:
    """Return per-chunk topics derived from ref:N markers inside each chunk.

    Each chunk inherits the topic of the first `ref:N` token it contains.
    Chunks without a ref get None — NULL-topic chunks are always returned
    in topic-filtered searches by design, so a missing ref doesn't hide
    content. If a chunk straddles two refs (long bullets crossing chunk
    boundaries), the first ref wins.
    """
    out: list[str | None] = []
    for chunk in chunks:
        topic: str | None = None
        for m in _REF_PATTERN.finditer(chunk):
            key = f"ref:{m.group(1)}"
            t = extracted_topics.get(key)
            if t:
                topic = t
                break
        out.append(topic)
    return out


def _windows_per_chunk(
    chunks: list[str], episodic_windows: dict[str, str]
) -> list[str | None]:
    """Return per-chunk episode-close dates derived from ref:N markers (ISSUE-109 #2).

    `episodic_windows` maps `ref:N` → the `valid_until` of an episodic fact
    extracted from that task. A chunk inherits a close date only when *every*
    `ref:N` it contains is episodic (present in the map); the chunk's window is
    the latest such date, so it survives until its longest-lived episode ends.
    Conservative on purpose: a chunk with any ref absent from the map (a
    durable fact, or a bullet that produced no fact) keeps standing — no
    window — so we never suppress content we aren't sure is wholly episodic.
    Chunks with no ref at all also stay standing.
    """
    out: list[str | None] = []
    for chunk in chunks:
        refs = [f"ref:{m.group(1)}" for m in _REF_PATTERN.finditer(chunk)]
        if refs and all(r in episodic_windows for r in refs):
            out.append(max(episodic_windows[r] for r in refs))
        else:
            out.append(None)
    return out


# Sleep-cycle Claude calls run unsandboxed, with no tools, no streaming,
# no skill proxy. The prompt does all the work — model returns text/JSON.
_SLEEP_CYCLE_TIMEOUT_SECONDS = 120


def _run_sleep_cycle_brain(
    config: Config, prompt: str, model: str, label: str, user_id: str = "",
    conn=None,
) -> tuple[bool, str]:
    """Run a privileged text-only model call through the configured brain.

    Returns (success, output). On failure, output is an error description.

    The sleep cycle is privileged orchestration: no tools, no streaming, no
    sandbox, no progress callbacks, no PID tracking. The brain handles
    transient API retries; everything else stays text-only by construction
    (empty allowed_tools).

    ISSUE-181: this calls the primary brain *directly* (not through the
    executor's fallback-wrapped path), so it consults the shared availability
    breaker before each call and feeds its own failures back into it. When the
    primary is in a ``usage_limit``/``not_found`` state the breaker is open and
    this short-circuits ``(False, "")`` without paying for a doomed brain call
    — so a degraded primary doesn't grind through every channel in a pass. The
    first failure opens the breaker (arming one operator alert); subsequent
    calls in the same pass skip. The breaker cooldown also gates the next
    scheduled run, so the cycle doesn't re-attempt every tick while the primary
    stays down. A successful run closes the breaker.
    """
    # Non-essential task policy (ISSUE-181): skip when the primary is degraded
    # rather than burning a doomed call per channel/block. ``_unavailable``
    # fires the one-shot operator alert on the closed→open transition so the
    # operator learns the sleep cycle is paused, not just silent.
    available, _reason = primary_brain_unavailable(config.brain)
    if not available:
        logger.info(
            "%s skipped — primary brain unavailable (cooling down)", label
        )
        return False, ""

    # Imported here rather than at module scope: `executor` imports
    # `briefings.generate`, and a top-level import from any of these
    # callers risks closing a cycle back through it.
    from istota.executor import build_model_cli_env

    req = BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=Path(config.temp_dir) if config.temp_dir else Path("/tmp"),
        # Not `dict(os.environ)`: a daemon-side model call with no task behind
        # it, so nothing has stripped the master Fernet key, the Nextcloud app
        # password, the mail passwords or the forge tokens (ISSUE-395).
        env=build_model_cli_env(config),
        timeout_seconds=_SLEEP_CYCLE_TIMEOUT_SECONDS,
        model=make_brain(config.brain).resolve_model_name(model),
        streaming=False,
        on_progress=None,
        cancel_check=None,
        on_pid=None,
        sandbox_wrap=None,
        result_file=None,
    )
    try:
        primary_started_at = time.time()
        primary_started_monotonic = time.monotonic()
        result = make_brain(config.brain).execute(req)
    except FileNotFoundError:
        logger.error("Brain CLI not found for %s", label)
        return False, ""
    except Exception as e:
        logger.error("%s brain error: %s", label, e)
        return False, ""

    # Feed the result into the shared availability breaker so a usage_limit /
    # not_found opens it (one-shot alert) and a success closes it. Mirrors the
    # executor's task path so the breaker is a single signal across all callers.
    # Imported here rather than at module scope: `executor` imports
    # `briefings.generate`, and a top-level import from any of these callers
    # risks closing a cycle back through it.
    from istota.executor import persist_brain_usage

    # This runs nightly, per user and per channel, against a general-tier model
    # and has no task row — which made it the largest single piece of spend the
    # deployment could not see.
    # The caller's connection where it has one. Each pass holds a single write
    # transaction for its duration, so opening a second connection here would
    # block on the write lock for the full busy timeout.
    persist_brain_usage(
        config, conn, usage=result.usage, origin="sleep_cycle",
        user_id=user_id, brain_kind=result.brain_kind,
        model=result.model_used or req.model, stop_reason=result.stop_reason,
        success=result.success,
    )

    _opened_reason = report_brain_result(
        result, config.brain, config=config, started_at=primary_started_at,
        started_monotonic=primary_started_monotonic,
    )
    if _opened_reason:
        _alert_brain_unavailable(config, label, _opened_reason)

    if result.stop_reason == "timeout":
        logger.error("%s timed out", label)
        return False, ""
    if result.stop_reason == "not_found":
        logger.error("Brain CLI not found for %s", label)
        return False, ""
    if not result.success:
        logger.error(
            "%s failed (stop_reason=%s): %s",
            label,
            result.stop_reason,
            (result.result_text or "")[:200],
        )
        return False, ""
    return True, (result.result_text or "").strip()


def _excerpt(text: str, budget: int) -> str:
    """Return head+tail excerpt of text within budget chars.

    Keeps the first 40% and last 60% when truncation is needed,
    since conclusions and outcomes tend to appear at the end.
    """
    if not text or len(text) <= budget:
        return text or ""
    marker = "\n...[truncated]...\n"
    usable = budget - len(marker)
    if usable < 40:
        return text[:budget]
    head_size = int(usable * 0.4)
    tail_size = usable - head_size
    return text[:head_size] + marker + text[-tail_size:]


def _alert_brain_unavailable(
    config: Config, label: str, reason: str
) -> None:
    """Fire one operator alert when the sleep cycle trips the availability breaker.

    The breaker opens exactly once per cooldown window (``report_brain_result``
    returns the reason only on the closed→open transition), so this pages once
    per degraded episode — not per channel. The cooldown means the next
    scheduled run re-probes after it elapses; if the primary is still down it
    fails, re-opens, and re-alerts, giving a bounded "still down" heartbeat
    rather than silently retrying every cycle (ISSUE-181).
    """
    try:
        from ..notifications import send_operator_alert

        send_operator_alert(
            config,
            f"⏸️ Sleep cycle paused — primary brain unavailable ({reason}) "
            f"during '{label}'. Memory extraction will resume once the primary "
            f"recovers. Subsequent channels in this pass are skipped.",
        )
    except Exception:
        logger.debug("sleep cycle brain-unavailable alert failed", exc_info=True)


# Cap the per-task tool list so a runaway trajectory can't blow the day-data
# budget; the count is still reported in full.
_MAX_TOOL_NAMES = 25
# Leading emoji + whitespace that the trace descriptions are prefixed with.
_TOOL_DESC_PREFIX = re.compile(r"^[^\w(]+")
# Per-label truncation. A verbatim command (`raw`) gets a larger budget than a
# human description so a full script invocation survives whole for the playbook
# extraction prompt (ISSUE-174 fix 1); the description path keeps the tight cap.
_MAX_DESC_CHARS = 80
_MAX_RAW_CHARS = 200


def tool_summary(execution_trace_json: str | None) -> tuple[int, list[str]]:
    """Summarize a task's tool usage from its ``execution_trace`` JSON.

    Returns ``(count, ordered_labels)`` where ``count`` is the number of tool
    entries and ``ordered_labels`` are short descriptions of each tool call,
    capped at ``_MAX_TOOL_NAMES``. When a tool entry carries a ``raw`` field
    (the verbatim command a Bash call ran — ISSUE-174 fix 1), that literal
    invocation is used verbatim (larger truncation budget) so the extraction
    LLM distils the real command instead of the paraphrased ``text`` label.
    Used both to gate playbook-worthiness (``playbooks.min_tool_calls``) and to
    give the extraction LLM the procedure's shape. Never raises — malformed /
    empty → ``(0, [])``.
    """
    if not execution_trace_json:
        return 0, []
    try:
        trace = json.loads(execution_trace_json)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0, []
    if not isinstance(trace, list):
        return 0, []

    labels: list[str] = []
    count = 0
    for entry in trace:
        if not isinstance(entry, dict) or entry.get("type") != "tool":
            continue
        count += 1
        if len(labels) >= _MAX_TOOL_NAMES:
            continue
        raw = str(entry.get("raw", "")).strip()
        if raw:
            label = raw if len(raw) <= _MAX_RAW_CHARS else raw[: _MAX_RAW_CHARS - 3] + "..."
        else:
            text = str(entry.get("text", "")).strip()
            text = _TOOL_DESC_PREFIX.sub("", text).strip()
            if len(text) > _MAX_DESC_CHARS:
                text = text[: _MAX_DESC_CHARS - 3] + "..."
            label = text or "tool"
        labels.append(label)
    return count, labels


def speaker_labels(
    conn: "db.sqlite3.Connection",
    config: Config,
    tasks: list,
) -> dict[int, str]:
    """Who wrote each task's prompt, for the tasks where it isn't the user.

    The same false-provenance defect as ISSUE-226, and worse here: an emissary
    reply is an arbitrary external contact's text, and what this prompt extracts
    from it is written durably to `USER.md` and `knowledge_facts` as things the
    principal said. Tasks not in the returned map are the user's own.
    """
    labels: dict[int, str] = {}
    for task in tasks:
        if task.source_type != "email":
            continue
        user_config = config.users.get(task.user_id or "")
        own = list(user_config.email_addresses or []) if user_config else []
        external = db.external_email_sender(
            db.email_sender_for_task(conn, task.id), own,
        )
        if external:
            labels[task.id] = f"External sender <{external}>"
    return labels


def _format_task_section(
    tasks: list,
    per_task_budget: int,
    speakers: dict[int, str] | None = None,
) -> list[str]:
    """Format a group of tasks with conversation grouping and budget control.

    Each task carries a compact tool summary (count + ordered tool labels from
    ``execution_trace``) so the extraction LLM can judge whether a task was a
    non-trivial, reusable procedure worth distilling into a playbook.
    """
    from collections import defaultdict

    groups: dict[str | None, list] = defaultdict(list)
    for task in tasks:
        groups[task.conversation_token].append(task)

    parts = []
    for conv_token, conv_tasks in groups.items():
        if conv_token and len(conv_tasks) > 1:
            parts.append(
                f"=== Conversation {conv_token} ({len(conv_tasks)} messages) ==="
            )
        for task in conv_tasks:
            prompt_budget = int(per_task_budget * 0.4)
            result_budget = per_task_budget - prompt_budget
            prompt_text = _excerpt(task.prompt or "", prompt_budget)
            result_text = _excerpt(task.result or "", result_budget)
            tool_count, tool_labels = tool_summary(
                getattr(task, "execution_trace", None)
            )
            tools_line = ""
            if tool_count:
                tools_line = (
                    f"Tools ({tool_count}): " + " | ".join(tool_labels) + "\n"
                )
            speaker = (speakers or {}).get(task.id, "User")
            parts.append(
                f"--- Task {task.id} ({task.source_type}, {task.created_at or 'unknown'}) ---\n"
                f"{speaker}: {prompt_text}\n"
                f"Bot: {result_text}\n"
                f"{tools_line}"
            )
    return parts


def gather_day_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather the day's interaction data for memory extraction.

    Separates interactive conversations from automated/scheduled output
    with clear section headers so the extraction LLM can distinguish
    user-stated facts from bot-generated content.

    Uses dynamic per-task budget allocation and tail-biased truncation
    to preserve conclusions and decisions. Interactive tasks get 80% of
    the budget when automated tasks are also present.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    # DB stores naive UTC timestamps, so strip tzinfo for comparison
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_tasks_since(conn, user_id, since_str, after_task_id)

    if not tasks:
        return ""

    # Partition by source type
    interactive = [t for t in tasks if t.source_type in INTERACTIVE_SOURCE_TYPES]
    automated = [t for t in tasks if t.source_type not in INTERACTIVE_SOURCE_TYPES]

    # Budget split: 80/20 when both exist, full budget for single section
    if interactive and automated:
        interactive_budget = int(MAX_DAY_DATA_CHARS * 0.8)
        automated_budget = MAX_DAY_DATA_CHARS - interactive_budget
    elif interactive:
        interactive_budget = MAX_DAY_DATA_CHARS
        automated_budget = 0
    else:
        interactive_budget = 0
        automated_budget = MAX_DAY_DATA_CHARS

    parts = []
    speakers = speaker_labels(conn, config, tasks)

    if interactive:
        interactive_per_task = max(_MIN_TASK_BUDGET, interactive_budget // len(interactive))
        parts.append(
            "======== INTERACTIVE CONVERSATIONS ========\n"
            "(User spoke directly — primary source for fact extraction)\n"
        )
        parts.extend(
            _format_task_section(interactive, interactive_per_task, speakers)
        )

    if automated:
        automated_per_task = max(_MIN_TASK_BUDGET, automated_budget // len(automated))
        parts.append(
            "\n======== AUTOMATED/SCHEDULED OUTPUT ========\n"
            "(Bot-generated — do not attribute facts to people merely mentioned here)\n"
        )
        parts.extend(_format_task_section(automated, automated_per_task, speakers))

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = _excerpt(combined, MAX_DAY_DATA_CHARS)

    return combined


def build_memory_extraction_prompt(
    user_id: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
    existing_facts: str | None = None,
    playbooks_enabled: bool = False,
    min_tool_calls: int = 4,
) -> str:
    """
    Build the prompt that instructs Claude to extract memories from the day's interactions.

    Args:
        user_id: The user ID
        day_data: Concatenated interaction data from the day
        existing_memory: Current contents of memory.md (to avoid duplication)
        date_str: Date string for the memory file (e.g. "2026-01-28")
        existing_facts: Formatted current knowledge-graph facts (one per line),
            or None when the graph is empty / unavailable. When provided, the
            LLM is instructed to skip re-emitting these and only output
            updates/refinements.
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing long-term memory (memory.md)

The following information is already stored in the user's permanent memory file.
Do NOT repeat any of this information in your output.

{existing_memory}
"""

    kg_section = ""
    if existing_facts:
        kg_section = f"""
## Existing knowledge graph facts

These facts are already stored. Do NOT re-emit them. Only emit a fact if today's
data contradicts, refines, or supersedes an existing one — in which case emit
the updated version (with valid_from set to today, or valid_until set to close
out a stale fact).

{existing_facts}
"""

    predicates_str = "\n".join(
        f"  - {pred}: {hint}" for pred, hint in SUGGESTED_PREDICATES.items()
    )

    playbooks_section = ""
    if playbooks_enabled:
        playbooks_section = f"""

PLAYBOOKS:
(JSON array of reusable task procedures distilled from today's interactions)
A playbook captures "here is the multi-step way to do task X" so a future task
can follow a known-good approach instead of rediscovering it. Each entry:
{{"title": "short imperative title", "triggers": ["keyword", ...], "steps": "numbered markdown steps, which skills/CLIs to use, and known pitfalls"}}

Only distill a playbook when a task in today's interactions:
- completed successfully, AND
- used at least {min_tool_calls} tool calls (see the "Tools (N): ..." line under each task), AND
- generalizes beyond this one instance (the same procedure would help a similar future request).

Do NOT create a playbook for:
- environment-specific failures or one-off troubleshooting that won't recur
- anything containing secrets, tokens, passwords, or personal data
- a one-off narrative ("summarized this article") with no reusable procedure
- trivial single-step tasks
- executable code or scripts — a playbook is markdown guidance that REFERENCES
  existing skills/CLIs by name; it never ships code to run

Write the steps as you would teach them to your future self: name the skills
and CLI subcommands used, the order, and any gotcha hit along the way.

CRITICAL — record verified commands, never reconstructions. Each task's
"Tools (N): ..." line lists the ACTUAL commands and skill invocations that ran
and succeeded. When a step names a command, script path, module, or skill CLI,
copy it VERBATIM from that Tools line — do NOT reconstruct a plausible-looking
path from memory (e.g. never invent `python -m some.module.path` when the trace
shows `python scripts/foo.py`). If the exact invocation isn't in the Tools line,
describe the step without inventing a command string.

Don't re-narrate a script. If a task's work was essentially ONE deterministic
script or CLI call (the Tools line is dominated by a single invocation), the
script already captures the procedure — do NOT paraphrase its internal steps
into prose (that just re-encodes working logic in a lossy format that drifts).
Either skip it, or write a THIN ROUTER: recognize the trigger, give the exact
verified command to run (copied from the Tools line), and name the one gotcha —
then stop. Never expand a single script call into a multi-step narrative of what
the script does internally.

If no reusable procedures emerged today, output an empty array: []"""

    return f"""You are extracting important memories from a day of interactions with user '{user_id}'.

Date: {date_str}
{existing_section}{kg_section}
## Today's interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering long-term.
Write each item with enough context to be self-contained and useful months from now
without access to the original conversation.

What to extract:
- Facts about the user: preferences, projects, people they work with, habits, goals
- Decisions made or plans discussed, including the reasoning when given
- Corrections the user made to the bot's understanding
- Outcomes of tasks: what was sent, created, configured, or changed
- Recurring patterns or workflows observed across conversations

What to skip:
- Information already in the existing memory above
- Greetings, acknowledgments, and small talk
- Temporary states no longer relevant (e.g., "waiting for a response" when the response already came)
- Raw data dumps or lengthy command output
- Quantitative health data — specific numerical measurements (weight, BP, HR, body temp, SpO2),
  biomarker / lab values, medication doses or schedules, dates of specific labs or procedures,
  and current symptoms or transient illnesses. The `health` module owns these in its own DB.
  Stable identity-level medical facts ARE in scope: allergies and named chronic conditions
  belong in FACTS (e.g. allergic_to, has_condition).

## Source type guidance

The interactions above may be split into two sections:

INTERACTIVE CONVERSATIONS: Direct exchanges where the user stated facts, made decisions,
or gave instructions. This is the primary source for extracting memories and facts.

AUTOMATED/SCHEDULED OUTPUT: Bot-generated content (briefings, cron jobs, scheduled tasks).
The bot produced this content — the user did not state it. For this section:
- DO extract: outcomes of automated tasks (e.g., "morning briefing was sent successfully")
- DO extract: notable information the bot surfaced that the user would want to remember
- DO NOT extract facts about people merely mentioned in bot-generated content
- DO NOT attribute preferences or behaviors to anyone based on automated output

Write bullet points that are self-contained.
Bad: "Discussed project Alpha."
Good: "Project Alpha is migrating from Django to FastAPI; target completion is Q2 (ref:1234)."

Format: dated bullet points with task references.
- Project Alpha migrating from Django to FastAPI, targeting Q2 completion ({date_str}, ref:1234)
- Prefers email summaries limited to 5 bullet points, not full reports ({date_str}, ref:1235)
- Sent introduction email to Dana (dana@example.com) about consulting engagement ({date_str}, ref:1236)

When the day had few interactions, still extract what is there. A single substantive
memory is better than {NO_NEW_MEMORIES}.

If there is genuinely nothing new worth remembering (e.g., only greetings or repeated
questions with no new information), respond with exactly: {NO_NEW_MEMORIES}

## Output format

Output three sections in this exact order. Each section starts with its marker on its own line.

MEMORIES:
(bullet points as described above — this is the primary output)

Important: if a piece of information is a personal attribute, relationship, or preference
that reduces cleanly to a (subject, predicate, object) triple, put it in FACTS only —
do NOT also create a MEMORY bullet for it. MEMORIES are for events, decisions, outcomes,
and situational context that don't reduce to a simple triple.

Examples:
- "Alice is allergic to shellfish" → FACT only (alice | allergic_to | shellfish)
- "Felix has an egg allergy" → FACT only (felix | allergic_to | eggs)
- "Sent intro email to Dana about consulting" → MEMORY (event/outcome, not a triple)
- "Project Alpha migrating to FastAPI, targeting Q2" → MEMORY (situational context)

FACTS:
(JSON array of entity-relationship triples extracted from the interactions)
Each triple: {{"subject": "entity", "predicate": "relationship", "object": "value"}}
Optional temporal fields: "valid_from" and "valid_until" (YYYY-MM-DD) for time-bounded facts.
Optional traceability field: "source_ref" — the integer task ID (from a "Task NNN" header
or "ref:NNN" marker in the interaction above) where the fact was stated. Attach this
whenever a single task clearly supports the fact; omit when the evidence is diffuse.

Suggested predicates (with usage guidance):
{predicates_str}
You may use other predicates when none of the above fit — prefer short, lowercase, snake_case verbs.
Choose predicates carefully: use uses_tech for software/tools only (not physical objects like vehicles),
use has_status for life situations only (not dietary choices — use prefers for those).

Temporal facts: for trips, visits, and time-bounded states, put dates in valid_from/valid_until
fields — NOT in the object string. Example:
  {{"subject": "felix", "predicate": "visiting", "object": "japan", "valid_from": "2026-04-14", "valid_until": "2026-04-24"}}
NOT: {{"subject": "felix", "predicate": "visiting", "object": "japan, april 14-24 2026"}}

Calendar-managed events: do NOT create facts that carry the date of a scheduled event whose
date already lives on the calendar (medical appointments, meetings, deadlines, etc.). The
calendar is the sole authority for when scheduled events happen — duplicating the date in a
KG fact creates two independent stores that can disagree, with no tiebreaker. KG facts may
capture date-less metadata about the event (procedure type, fasting requirements, location
details, what the workup is for) using predicates like has_scheduled_procedure or
has_medical_workup. On any such fact, valid_from = when we learned about it, NEVER the event
date. If the event date is the only thing worth recording, write nothing — the calendar
already has it.

Normalize entity names to lowercase. Object values MUST be under 10 words (max 100 characters)
— put context, reasoning, and detail in MEMORIES, not in fact objects. Long objects break
fuzzy dedup.

Time-bound facts should age out of the always-loaded view. For one-off actions and
passing interests — `interested_in`, `completed`, `acquired`, `disposed_of`, `traveled_to`
— set `valid_from` to the event date when known; these expire automatically if you give no
`valid_until`. For a `decided` fact about a one-time action (a cancellation, a purchase, a
specific short-lived task), set `valid_until` to a date a few weeks out so it ages out.
Durable facts — lifestyle/direction/policy decisions, identity, relationships — have no
`valid_until`.

Subject constraints:
- For user preferences, habits, and decisions, the subject should be "{user_id}"
- Only create facts about other people if {user_id} explicitly stated something about them
  in an interactive conversation — not because they appeared in bot-generated output
- Facts about projects, tools, or organizations are fine when clearly discussed

Bad fact examples (do NOT produce facts like these):
- {{"subject": "max", "predicate": "prefers", "object": "morning briefings"}}
  (Max was mentioned in a briefing the bot generated — {user_id} never said this about Max)
- {{"subject": "dana", "predicate": "works_at", "object": "acme corp"}}
  (Dana appeared in an automated email summary — {user_id} didn't state this)
- {{"subject": "{user_id}", "predicate": "weighs", "object": "82.5 kg"}}
  (Measurements belong in the health module DB, not the knowledge graph)
- {{"subject": "{user_id}", "predicate": "ldl", "object": "142"}}
  (Biomarker / lab values belong in the health module DB, not the knowledge graph)

Good fact examples:
- {{"subject": "{user_id}", "predicate": "prefers", "object": "morning briefings"}}
  ({user_id} explicitly said they prefer morning briefings in conversation)
- {{"subject": "{user_id}", "predicate": "works_on", "object": "project alpha"}}
  ({user_id} discussed working on this project)
- {{"subject": "dana", "predicate": "works_at", "object": "acme corp"}}
  ({user_id} explicitly told the bot that Dana works at Acme in an interactive conversation)

If no facts to extract, output an empty array: []

TOPICS:
(JSON object mapping each task ref to a topic category)
Topics: work, tech, personal, finance, admin, learning, meta
Example: {{"ref:1234": "tech", "ref:1235": "personal"}}
If no topics to classify, output: {{}}{playbooks_section}

If you cannot produce the structured sections, output only the bullet points (the MEMORIES section).
Do not include any preamble or explanation outside these sections."""


def _validate_fact(fact: dict) -> bool:
    """Validate an extracted fact has required fields with non-empty values.

    Predicates are freeform — any non-empty string is accepted. The knowledge
    graph handles unknown predicates as multi-valued by default.

    Object length is capped at MAX_FACT_OBJECT_CHARS — long sentences in the
    object slot break fuzzy dedup and crowd out other prompt content.
    """
    if not isinstance(fact, dict):
        return False
    if not all(k in fact for k in ("subject", "predicate", "object")):
        return False
    if not fact["subject"].strip() or not fact["predicate"].strip() or not fact["object"].strip():
        return False
    if len(fact["object"].strip()) > MAX_FACT_OBJECT_CHARS:
        return False
    return True


def _normalize_fact(fact: dict) -> dict:
    """Normalize fact values to lowercase/stripped. Preserves source_ref when it
    parses as a positive int."""
    fact["subject"] = fact["subject"].strip().lower()
    fact["predicate"] = fact["predicate"].strip().lower()
    fact["object"] = fact["object"].strip().lower()
    if "source_ref" in fact:
        try:
            ref = int(fact["source_ref"])
            fact["source_ref"] = ref if ref > 0 else None
        except (TypeError, ValueError):
            fact["source_ref"] = None
    return fact


def _parse_structured_extraction(
    output: str,
) -> tuple[str, list[dict], dict[str, str], list[dict]]:
    """Parse structured extraction output into components.

    Returns (memories_text, facts_list, topics_dict, playbooks_list).
    Falls back gracefully: if structured sections are missing, treats entire
    output as memories with empty facts/topics/playbooks. A missing or
    malformed PLAYBOOKS section yields an empty list (the feature is opt-in;
    older outputs simply have no PLAYBOOKS marker).
    """
    facts: list[dict] = []
    topics: dict[str, str] = {}
    playbooks: list[dict] = []

    # Section markers, in the order they appear in the prompt. Each section
    # runs from its marker to the start of the next present marker (or EOF).
    memories_match = re.search(r"(?:^|\n)MEMORIES:\s*\n?", output)
    facts_match = re.search(r"(?:^|\n)FACTS:\s*\n?", output)
    topics_match = re.search(r"(?:^|\n)TOPICS:\s*\n?", output)
    playbooks_match = re.search(r"(?:^|\n)PLAYBOOKS:\s*\n?", output)

    if not memories_match:
        # No structured format — treat entire output as memories
        return output.strip(), facts, topics, playbooks

    def _section_end(after_pos: int) -> int:
        """First marker start strictly after ``after_pos``, else EOF."""
        starts = [
            m.start()
            for m in (facts_match, topics_match, playbooks_match)
            if m and m.start() > after_pos
        ]
        return min(starts) if starts else len(output)

    # Memories: from MEMORIES: to the next section.
    mem_start = memories_match.end()
    memories_text = output[mem_start:_section_end(mem_start)].strip()

    # Facts JSON.
    if facts_match:
        facts_raw = output[facts_match.end():_section_end(facts_match.end())].strip()
        try:
            parsed = json.loads(facts_raw)
            if isinstance(parsed, list):
                facts = [_normalize_fact(f) for f in parsed if _validate_fact(f)]
        except (json.JSONDecodeError, ValueError):
            logger.debug("Failed to parse FACTS section: %s", facts_raw[:200])

    # Topics JSON.
    if topics_match:
        topics_raw = output[topics_match.end():_section_end(topics_match.end())].strip()
        try:
            parsed = json.loads(topics_raw)
            if isinstance(parsed, dict):
                topics = {k: v for k, v in parsed.items() if isinstance(v, str)}
        except (json.JSONDecodeError, ValueError):
            logger.debug("Failed to parse TOPICS section: %s", topics_raw[:200])

    # Playbooks JSON.
    if playbooks_match:
        pb_raw = output[playbooks_match.end():_section_end(playbooks_match.end())].strip()
        try:
            parsed = json.loads(pb_raw)
            if isinstance(parsed, list):
                playbooks = [p for p in parsed if _validate_playbook(p)]
        except (json.JSONDecodeError, ValueError):
            logger.debug("Failed to parse PLAYBOOKS section: %s", pb_raw[:200])

    return memories_text, facts, topics, playbooks


def _validate_playbook(pb: dict) -> bool:
    """A playbook needs a non-empty title and non-empty steps."""
    if not isinstance(pb, dict):
        return False
    title = str(pb.get("title", "")).strip()
    steps = str(pb.get("steps", "")).strip()
    return bool(title) and bool(steps)


def process_user_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
) -> bool:
    """
    Run the sleep cycle for one user: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    sleep_config = config.sleep_cycle

    # One-time import of the pre-KV curation sidecars, then the files go.
    # Two things about where this sits are deliberate.
    #
    # It is outside the `curate_user_memory` gate further down: that flag is
    # off by default, but the runtime `memory` CLI wrote all three sidecars
    # regardless, so gating the import on it would strand the files on exactly
    # the deployments that have them.
    #
    # And it is before the early returns rather than at the end: a user with
    # no interactions in the window leaves this function within a few lines,
    # and that user's sidecars would never be reached.
    try:
        from .curation.audit import migrate_user_md_sidecars
        migrate_user_md_sidecars(config, user_id)
    except Exception as e:
        logger.debug("curation sidecar migration failed for %s: %s", user_id, e)

    # Get last run state
    last_run_at, last_task_id = db.get_sleep_cycle_last_run(conn, user_id)

    # Gather day data
    day_data = gather_day_data(
        config, conn, user_id, sleep_config.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info("Sleep cycle for %s: no new interactions, skipping", user_id)
        db.set_sleep_cycle_last_run(conn, user_id, last_task_id)
        return False

    # Load existing memory to avoid duplication
    existing_memory = read_user_memory_v2(config, user_id)

    # Load current KG facts — passed to the extraction LLM so it can skip
    # re-emitting facts the graph already has and produce informed
    # supersession/refinement updates instead.
    existing_facts = _load_kg_facts_text(config, conn, user_id)

    # Build extraction prompt — date_str follows the user's timezone so
    # filenames and bullet timestamps match the user's calendar day, not the
    # server's. Falls back to UTC for unknown tz strings. Live DB value so a
    # web-UI tz change is honored without a daemon restart (ISSUE-099).
    tz_name = config.resolve_user_timezone(user_id)
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    date_str = datetime.now(user_tz).strftime("%Y-%m-%d")
    prompt = build_memory_extraction_prompt(
        user_id, day_data, existing_memory, date_str,
        existing_facts=existing_facts,
        playbooks_enabled=config.playbooks.enabled,
        min_tool_calls=config.playbooks.min_tool_calls,
    )

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=sleep_config.extraction_model,
        label=f"Sleep cycle extraction for {user_id}",
        user_id=user_id,
        conn=conn,
    )
    if not ok:
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info("Sleep cycle for %s: no new memories to save", user_id)
        # Still update state so we don't reprocess these tasks
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Parse structured output (memories + facts + topics + playbooks)
    memories_text, extracted_facts, extracted_topics, extracted_playbooks = (
        _parse_structured_extraction(output)
    )

    if not memories_text or memories_text == NO_NEW_MEMORIES:
        logger.info("Sleep cycle for %s: no memories after parsing", user_id)
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Sanity check: real memory output is a list of `- ...` bullets. When the
    # model narrates its process instead ("Memory extraction complete..."),
    # the output passes the empty/sentinel checks but contains no bullets.
    # Treat that as malformed: don't write the file, don't insert facts, do
    # advance state so we don't reprocess the same window forever.
    if not _has_bullet_points(memories_text):
        logger.warning(
            "Sleep cycle for %s: extraction output had no bullet points "
            "(treating as malformed). First 200 chars: %r",
            user_id, memories_text[:200],
        )
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Write dated memory file (only the human-readable memories)
    if not config.use_mount:
        logger.warning("Sleep cycle requires mount mode, skipping file write for %s", user_id)
        _update_state(config, conn, user_id, last_task_id)
        return False

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    context_dir.mkdir(parents=True, exist_ok=True)

    memory_file = context_dir / f"{date_str}.md"
    memory_file.write_text(memories_text + "\n")
    logger.info("Wrote dated memory file for %s: %s (%d chars)", user_id, memory_file.name, len(memories_text))

    # Insert extracted facts into knowledge graph (non-critical). While we're
    # here, record each episodic fact's effective close date keyed by its
    # source task ref, so the dated-memory chunks it came from can self-suppress
    # once the episode is over (ISSUE-109 #2). The effective valid_until is read
    # back from the stored row so lever-1 auto-stamping is reflected too.
    episodic_windows: dict[str, str] = {}
    if extracted_facts:
        try:
            from .knowledge_graph import ensure_table, add_fact, get_fact
            ensure_table(conn)
            inserted = 0
            for fact in extracted_facts:
                fact_id = add_fact(
                    conn, user_id,
                    subject=fact["subject"],
                    predicate=fact["predicate"],
                    object_val=fact["object"],
                    valid_from=fact.get("valid_from"),
                    valid_until=fact.get("valid_until"),
                    source_task_id=fact.get("source_ref"),
                    source_type="extracted",
                )
                if fact_id is not None:
                    inserted += 1
                    ref = fact.get("source_ref")
                    stored = get_fact(conn, fact_id)
                    if ref and stored and stored.valid_until:
                        key = f"ref:{ref}"
                        prev = episodic_windows.get(key)
                        # Keep the latest close date per ref so a task's
                        # longest-lived episode governs its chunk.
                        if prev is None or stored.valid_until > prev:
                            episodic_windows[key] = stored.valid_until
            if inserted:
                logger.info("Inserted %d knowledge facts for %s", inserted, user_id)
        except Exception as e:
            logger.debug("Knowledge graph insertion failed for %s: %s", user_id, e)

    # Index memory file for semantic search (non-critical). Per-chunk topics
    # are derived from the bullets each chunk contains: each bullet's
    # `ref:N` marker maps via `extracted_topics` to a topic, and the chunk
    # inherits the topic of its first ref. Bullets without a ref leave the
    # chunk's topic NULL — NULL-topic chunks are always returned in
    # topic-filtered searches by design.
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .search import (
                chunk_text as _chunk_text,
                index_file as _index_file,
            )
            chunks = _chunk_text(memories_text)
            topic_per_chunk = (
                _topics_per_chunk(chunks, extracted_topics)
                if extracted_topics else None
            )
            valid_until_per_chunk = (
                _windows_per_chunk(chunks, episodic_windows)
                if episodic_windows else None
            )
            _index_file(
                conn, user_id, str(memory_file), memories_text, "memory_file",
                topic_per_chunk=topic_per_chunk,
                valid_until_per_chunk=valid_until_per_chunk,
            )
        except Exception as e:
            logger.debug("Memory search indexing failed for %s: %s", memory_file.name, e)

    # Write + index learned playbooks (Part B). Best-effort: a failure here
    # must not lose the memories/facts already persisted above.
    if config.playbooks.enabled and extracted_playbooks:
        try:
            _process_extracted_playbooks(
                config, conn, user_id, extracted_playbooks, date_str, last_task_id,
            )
        except Exception as e:
            logger.warning("Playbook processing failed for %s: %s", user_id, e)

    # Update state
    _update_state(config, conn, user_id, last_task_id)

    # Clean up old memory files
    cleanup_old_memory_files(config, user_id, sleep_config.memory_retention_days)

    # Clean up old playbooks (use-based age; 0 = keep forever). Pass conn so a
    # pruned playbook's memory_chunks are deleted with its file (ISSUE-174).
    if config.playbooks.enabled:
        cleanup_old_playbooks(config, user_id, config.playbooks.retention_days, conn=conn)

    # Clean up old ephemeral memory_chunks (conversation, memory_file,
    # channel_memory). Reuses the same retention setting as the file cleanup
    # so users have a single knob.
    if sleep_config.memory_retention_days > 0:
        try:
            from .search import cleanup_old_chunks
            n = cleanup_old_chunks(conn, user_id, sleep_config.memory_retention_days)
            if n:
                logger.info("Pruned %d ephemeral memory chunks for %s", n, user_id)
        except Exception as e:
            logger.debug("memory_chunks cleanup failed for %s: %s", user_id, e)

    # Knowledge graph audit pruning runs on its own knob, independent of
    # memory_retention_days. Audit rows are tiny but accumulate several
    # per night per user; tying them to the (often-unset) memory knob
    # would let the table grow unbounded by default.
    if sleep_config.knowledge_graph_audit_retention_days > 0:
        try:
            from .knowledge_graph import cleanup_old_audit_rows, ensure_table
            ensure_table(conn)
            n = cleanup_old_audit_rows(
                conn, user_id, sleep_config.knowledge_graph_audit_retention_days
            )
            if n:
                logger.info("Pruned %d KG audit rows for %s", n, user_id)
        except Exception as e:
            logger.debug("KG audit cleanup failed for %s: %s", user_id, e)

    # Curate USER.md if enabled
    if sleep_config.curate_user_memory:
        try:
            curate_user_memory(config, user_id, conn=conn)
        except Exception as e:
            logger.error("USER.md curation failed for %s: %s", user_id, e)

    return True


def _load_kg_facts_text(
    config: Config, conn: "db.sqlite3.Connection | None", user_id: str
) -> str | None:
    """Load formatted knowledge graph facts for the curation prompt.

    Returns None on any failure (graceful degradation). The KG section is
    optional — the model still gets the dated memories and current USER.md
    structure even when this returns None.
    """
    try:
        from .knowledge_graph import (
            ensure_table,
            format_facts_for_prompt,
            get_current_facts,
        )
        if conn is not None:
            ensure_table(conn)
            facts = get_current_facts(conn, user_id)
            return format_facts_for_prompt(facts) if facts else None
        with db.get_db(config.db_path) as temp_conn:
            ensure_table(temp_conn)
            facts = get_current_facts(temp_conn, user_id)
            return format_facts_for_prompt(facts) if facts else None
    except Exception:
        return None


def _skill_overlay_dir_state(config: Config, user_id: str) -> str:
    """Why there is no overlay directory to read: for a log line, nothing else.

    `"absent"` (or no mount) is the ordinary state and says nothing.
    `"outside_tree"` and `"unopenable"` are both a planted directory and are
    worth one line each — and they are **kept apart** because the first is the
    worse of the two and a single boolean reported it as the quieter one. That
    is the same three-way split `skills._resolve_overlay_dir` makes for its
    error codes, and the two should stay in step.

    Deliberately never used to reach a file. `open_user_skill_overlays` returns
    the path only together with a descriptor; this asks a question about a name
    and returns a string.
    """
    from ..storage import resolve_user_skill_overlays_dir  # noqa: PLC0415

    if not getattr(config, "use_mount", False):
        return "absent"
    try:
        d = resolve_user_skill_overlays_dir(config, user_id)
        if d is None:
            # `contained_overlay_dir` refused: it resolves outside the user's
            # own tree. The clearest plant of the set, and the one a boolean
            # keyed on `exists()` reported as an ordinary absence.
            return "outside_tree"
        return "unopenable" if d.exists() else "absent"
    except OSError:
        return "unopenable"


def _load_skill_overlay_inventory(
    config: Config, user_id: str
) -> list[tuple[str, int]]:
    """`(skill, line count)` for every per-skill overlay that reaches a prompt.

    Names and counts only — the curator has to know a topic is handled
    elsewhere, not to re-read the rules — and it is a read: the curator does
    not write overlays in v1, so nothing here is fed back into an op.

    A file the loader would refuse is left out: a name that is not a skill, a
    denylisted one, an empty one, one past the cap. Naming it would tell the
    curator a rule is stored live somewhere when it is not, and deciding what
    USER.md no longer needs is this list's one use. The disabled set is
    `effective_disabled_skills`, so an overlay switched off instance-wide, for
    this user, or by the capability gate is left out too — but that function is
    not the whole of `select_skills`' pre-filter, so an overlay for a skill
    held back by a missing dependency or by `admin_only` is still listed. The
    claim this makes is "the loader would load this file", not "this task will
    see it", which no nightly-time answer could make anyway.

    **The candidate names come from the skill index, never from the tree.**
    `{mount}/Users/{user_id}` is bound read-write into that user's own sandbox,
    so a task can put any number of files in this directory, and only a file
    named for a known skill can ever bind. Listing the directory would have
    this nightly pass open and read every planted file to the cap before
    discarding it; deriving one path per known skill bounds the work by the
    operator's own tree, which is `sandbox_cache_sweeper`'s rule for the same
    hazard. What that leaves unreported — a file named for no skill at all — is
    `doctor`'s `config.skill_overlays` to report, and it is not something the
    curator could act on.

    Everything else about reaching those files is `_loader`'s and `storage`'s
    rather than this module's. `open_user_skill_overlays` carries the
    containment rule (`config` and `skills` are both entries a task can replace
    with a symlink to anywhere the daemon can read) and hands back a descriptor
    on the directory that passed, so every read below goes through it rather
    than resolving those components again by name; and `inspect_overlay` opens
    each file `O_NOFOLLOW` and `O_NONBLOCK` and refuses anything that is not a
    regular file — a FIFO left at `notes.md` would otherwise block this read
    forever, and the nightly pass runs unsandboxed with no timeout over it.

    Returns [] on any failure, like `_load_kg_facts_text`, but logs first: the
    section vanishing from every nightly prompt for every user is the exact
    duplication this exists to stop, and silence would make a permanently
    broken index indistinguishable from a user with no overlays.
    """
    dir_fd: int | None = None
    try:
        overlay_dir, dir_fd = open_user_skill_overlays(config, user_id)
        if dir_fd is None:
            # Absent is the ordinary state and says nothing. A directory that
            # is *there* and could not be opened is a different fact, and this
            # function's own contract two paragraphs up is that silence would
            # make a permanently broken inventory look like a user with no
            # overlays — so say it once, as `reindex_skill_overlays` does for
            # the same disagreement between the two gates.
            state = _skill_overlay_dir_state(config, user_id)
            if state != "absent":
                logger.warning(
                    "skill overlay inventory skipped for %s: the overlay "
                    "directory %s",
                    user_id,
                    "resolves outside the user's own tree"
                    if state == "outside_tree"
                    else "could not be opened as a chain of plain directories",
                )
            return []
        # Function-local: `istota.skills` star-imports every skill on the way
        # to `_loader`, and this module is imported by the scheduler.
        from ..skills._loader import (
            OVERLAY_MAX_BYTES,
            effective_disabled_skills,
            inspect_overlay,
            load_skill_index,
        )

        known = load_skill_index(
            config.skills_dir, bundled_dir=config.bundled_skills_dir
        )
        disabled = effective_disabled_skills(config, user_id, known)
        rows: list[tuple[str, int]] = []
        for skill in sorted(known):
            if skill in disabled:
                continue
            found = inspect_overlay(
                overlay_dir / f"{skill}.md",
                known_skills=known,
                disabled_skills=disabled,
                max_read_bytes=OVERLAY_MAX_BYTES,
                dir_fd=dir_fd,
            )
            if found.binds and found.lines is not None:
                rows.append((found.skill, found.lines))
        return rows
    except Exception as e:  # noqa: BLE001 - a prompt section is best-effort
        logger.warning(
            "skill overlay inventory unavailable for %s: %s", user_id, e
        )
        return []
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


# Phase A observability for USER.md growth. Warning fires once the file
# crosses ~8 KB — somewhere around where it starts pushing other prompt
# sections out under typical max_memory_chars budgets. Tunable via config
# in phase B; for now it's a hard-coded soft threshold.
USER_MEMORY_SOFT_WARN_BYTES = 8 * 1024


def _maybe_warn_usermd_size(config: Config, user_id: str, size_bytes: int) -> None:
    """Post a one-line warning to log_channel when USER.md crosses 8 KB.

    Phase A: visibility only. No truncation, no failure. The audit log
    captures the size on every curation run regardless; this is the
    in-the-moment notice so users see the growth before it bites.
    """
    if size_bytes < USER_MEMORY_SOFT_WARN_BYTES:
        return
    user_cfg = config.users.get(user_id)
    log_channel = getattr(user_cfg, "log_channel", "") if user_cfg else ""
    if not log_channel:
        logger.info(
            "USER.md size warning for %s: %d bytes (no log_channel configured)",
            user_id, size_bytes,
        )
        return
    msg = (
        f"USER.md is {size_bytes:,} bytes — over the {USER_MEMORY_SOFT_WARN_BYTES:,} byte "
        "soft threshold. Consider reviewing for stale entries before it crowds out "
        "recall and KG facts in prompts."
    )
    try:
        from ..notifications import send_notification
        send_notification(
            config,
            user_id,
            msg,
            surface="talk",
            conversation_token=log_channel,
        )
    except Exception as e:
        logger.debug("USER.md size warning post failed for %s: %s", user_id, e)


def _post_curation_summary(
    config: Config, user_id: str, applied: list[dict], rejected: list[dict]
) -> None:
    """Post a one-line summary of an applied curation run to the user's log channel.

    No-op when no log channel is configured or when nothing was applied.
    """
    if not applied:
        return
    user_cfg = config.users.get(user_id)
    log_channel = getattr(user_cfg, "log_channel", "") if user_cfg else ""
    if not log_channel:
        return

    n_appended = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "append" and a.get("outcome") == "applied"
    )
    n_removed = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "remove" and a.get("outcome") == "applied"
    )
    n_headings = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "add_heading" and a.get("outcome") == "applied"
    )
    msg = (
        f"USER.md curated: +{n_appended} appended, -{n_removed} removed, "
        f"+{n_headings} new headings"
    )
    try:
        from ..notifications import send_notification  # late import to avoid cycles in tests
        send_notification(
            config,
            user_id,
            msg,
            surface="talk",
            conversation_token=log_channel,
        )
    except Exception as e:
        logger.debug("curation summary post failed for %s: %s", user_id, e)


def curate_user_memory(
    config: Config, user_id: str, conn: "db.sqlite3.Connection | None" = None
) -> bool:
    """Op-based USER.md curation.

    Reads current USER.md + last 3 days of dated memories + KG facts, asks
    Sonnet to emit a JSON list of small ops (append / add_heading / remove),
    validates and applies them against a parsed `SectionedDoc`, writes the
    result back, and audit-logs the run.

    Returns True if USER.md was updated.
    """
    import hashlib
    import json

    from .curation import (
        apply_ops,
        build_op_curation_prompt,
        parse_sectioned_doc,
        serialize_sectioned_doc,
        strip_json_fences,
        write_audit_log,
    )
    from .curation.audit import (
        detect_bypass_write,
        read_lint_seen,
        write_last_seen,
        write_lint_seen,
    )
    from .curation.file_lock import (
        MemoryMdLocked,
        deferred_lock_dir,
        memory_md_lock,
    )
    from .curation.lint import (
        filter_unseen_candidates,
        find_temporal_bullets,
        prepend_agents_header_if_missing,
    )

    if not config.use_mount:
        logger.warning("USER.md curation requires mount mode, skipping for %s", user_id)
        return False

    # `config/` is resolved **once**, here, and every later read and write in
    # this function goes through the path derived from it (ISSUE-339). Resolving
    # again after the LLM call would re-walk `config/` from scratch, so the
    # anti-clobber sha guard would compare whatever the directory points at now
    # against a write going to the path resolved minutes earlier — and an
    # in-tree link swap during the brain call is permitted and model-reachable.
    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        logger.warning(
            "memory_curation_aborted user=%s reason=config_dir_outside_user_tree",
            user_id,
        )
        return False
    memory_path = config_dir / "USER.md"

    # The tri-state matters here and nowhere else, so the curator reads the leaf
    # directly rather than through `read_user_memory_v2`, which folds every
    # refusal into None for callers that only want "is there memory".
    #
    # `... or ""` on a refusal is how this function came to destroy the file it
    # curates: an unreadable USER.md became an *empty* document, the post-LLM
    # sha guard compared a refusal against a refusal and matched by
    # construction, and the ops were then serialized over a healthy file. A
    # 1.26 MB USER.md was reduced to 456 bytes that way, with the run reporting
    # success. Refusing to curate what it cannot read is the fix; raising the
    # read cap, which made it reachable by padding, is depth behind it.
    current, read_reason = read_regular_file(memory_path)
    if read_reason is not None:
        logger.warning(
            "memory_curation_aborted user=%s reason=user_md_unreadable detail=%s",
            user_id, read_reason,
        )
        return False
    current = current or ""

    dated = read_dated_memories(config, user_id, max_days=3, max_chars=8000)
    if not dated:
        return False

    # Bypass-write detection runs once per nightly pass, before the LLM
    # call. If USER.md changed without any audit entry having updated
    # last_seen, log it as a synthetic legacy entry. This can't tell us
    # *what* changed, only *that* something did outside the ops engine.
    bypass_signal = detect_bypass_write(config, user_id, current)
    if bypass_signal is not None:
        logger.warning(
            "memory_user_md_bypass_write user_id=%s previous_size=%s current_size=%s",
            user_id,
            bypass_signal.get("previous_size_bytes"),
            bypass_signal.get("current_size_bytes"),
        )
        write_audit_log(
            config, user_id,
            applied=[], rejected=[],
            user_md_size_bytes=bypass_signal.get("current_size_bytes"),
            source="legacy",
            entry_kind="legacy_detected",
            extra={"legacy_signal": bypass_signal},
        )

    initial_sha = hashlib.sha256(current.encode("utf-8")).hexdigest()

    doc = parse_sectioned_doc(current)
    kg_text = _load_kg_facts_text(config, conn, user_id)

    # Phase A lint pass — log only. Find date-stamped bullets that look
    # like temporal facts so we can review the catch rate before flipping
    # on Phase B (active migration). Dedup against the stored seen set so
    # each bullet emits at most once per TTL window.
    lint_candidates = find_temporal_bullets(doc, kg_text)
    if lint_candidates:
        lint_candidates, lint_seen = filter_unseen_candidates(
            lint_candidates,
            read_lint_seen(config, user_id),
        )
        # Persisted whether or not anything survived the filter: the pass
        # also prunes entries past the TTL, and dropping that work when
        # every candidate was a repeat would keep expired hashes forever.
        write_lint_seen(config, user_id, lint_seen)
    if lint_candidates:
        write_audit_log(
            config, user_id,
            applied=[], rejected=[],
            user_md_size_bytes=len(current.encode("utf-8")),
            source="nightly",
            entry_kind="lint_candidate",
            extra={
                "lint_candidates": [
                    {
                        "heading": c.heading,
                        "bullet_text": c.bullet_text,
                        "suggested_predicate": c.suggested_predicate,
                        "suggested_object": c.suggested_object,
                        "suggested_valid_from": c.suggested_valid_from,
                    }
                    for c in lint_candidates
                ],
            },
        )

    overlays = _load_skill_overlay_inventory(config, user_id)
    prompt = build_op_curation_prompt(
        user_id, doc, dated, kg_text, skill_overlays=overlays
    )

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=config.sleep_cycle.curation_model,
        label=f"USER.md curation for {user_id}",
        user_id=user_id,
        conn=conn,
    )
    if not ok:
        return False

    raw = strip_json_fences(output)
    try:
        payload = json.loads(raw)
        ops = payload.get("ops", [])
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(
            "USER.md curation JSON parse failed for %s: %s; raw=%r",
            user_id,
            e,
            raw[:200],
        )
        return False

    # `config_dir` and `memory_path` come from the single resolve at the top of
    # this function. Not re-resolved here: that is the whole point of holding
    # the resolved path across the brain call.
    config_dir.mkdir(parents=True, exist_ok=True)

    header_added = False
    # Anchor under the user's deferred dir so the lock is the same inode the
    # runtime memory CLI uses (host or sandboxed) — see file_lock docstring.
    lock_dir = deferred_lock_dir(config.temp_dir / user_id)
    try:
        with memory_md_lock(memory_path, timeout_seconds=5.0, lock_dir=lock_dir):
            # Re-read USER.md from disk after the LLM call. If it changed
            # during the brain's wall time (a runtime CLI write landed
            # between read and write), abort tonight's curation rather
            # than clobber the runtime write.
            #
            # Off `memory_path`, not by re-deriving the path from the config —
            # the guard has to read the same file the write is about to go to.
            # A refusal aborts: falling back to `current` would compare a
            # refusal against itself, match, and license the write, which is
            # exactly the shape that destroyed the file above.
            latest, latest_reason = read_regular_file(memory_path)
            if latest_reason is not None:
                logger.warning(
                    "memory_curation_aborted user=%s reason=user_md_unreadable_on_recheck detail=%s",
                    user_id, latest_reason,
                )
                return False
            latest = latest or ""
            latest_sha = hashlib.sha256(latest.encode("utf-8")).hexdigest()
            if latest_sha != initial_sha:
                logger.warning(
                    "memory_curation_aborted user=%s reason=user_md_changed_during_llm_call",
                    user_id,
                )
                write_audit_log(
                    config, user_id,
                    applied=[], rejected=[],
                    user_md_size_bytes=len(latest.encode("utf-8")),
                    source="nightly",
                    entry_kind="aborted",
                    extra={"aborted_reason": "user_md_changed_during_llm_call"},
                )
                # Update last_seen so the next nightly run doesn't keep
                # replaying the bypass detection on an already-noticed
                # mismatch.
                write_last_seen(
                    config, user_id,
                    size_bytes=len(latest.encode("utf-8")),
                    sha256=latest_sha,
                )
                return False

            new_doc, applied, rejected = apply_ops(doc, ops)
            if not applied and not rejected:
                return False  # truly nothing happened — no audit, no write
            if not applied:
                write_audit_log(
                    config, user_id, applied=[], rejected=rejected,
                    user_md_size_bytes=len(current.encode("utf-8")),
                    source="nightly",
                )
                return False

            # Skip the write if every applied op was a no-op (dedup, no_match). Decide
            # this from outcomes rather than text comparison — comparing serialized
            # output against `current` is brittle when USER.md has formatting drift
            # (trailing whitespace on headings, missing trailing newline, CRLF) that
            # the round-trip normalizes away, leading to spurious nightly rewrites.
            real_changes = any(a.get("outcome") == "applied" for a in applied)
            if not real_changes:
                write_audit_log(
                    config, user_id, applied=applied, rejected=rejected,
                    user_md_size_bytes=len(current.encode("utf-8")),
                    source="nightly",
                )
                return False

            new_text = serialize_sectioned_doc(new_doc)
            # One-shot agents-header migration. Idempotent.
            new_text, header_added = prepend_agents_header_if_missing(new_text)
            # False covers both a refusal (a planted inode at USER.md) and a
            # failed write; `write_regular_file` logs which, and stages through
            # `os.replace` so neither leaves a truncated file behind. The reason
            # here stays generic rather than naming one of the two, since this
            # branch cannot tell them apart.
            if not write_regular_file(memory_path, new_text):
                logger.warning(
                    "memory_curation_aborted user=%s reason=user_md_not_written",
                    user_id,
                )
                return False
    except MemoryMdLocked:
        logger.warning(
            "memory_curation_aborted user=%s reason=lock_timeout", user_id,
        )
        return False
    logger.info(
        "Updated USER.md for %s (+%d ops, %d rejected)",
        user_id,
        len(applied),
        len(rejected),
    )
    new_size_bytes = len(new_text.encode("utf-8"))
    new_sha = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    write_audit_log(
        config, user_id, applied=applied, rejected=rejected,
        user_md_size_bytes=new_size_bytes,
        source="nightly",
    )
    write_last_seen(config, user_id, size_bytes=new_size_bytes, sha256=new_sha)

    n_ops_applied = sum(1 for a in applied if a.get("outcome") == "applied")
    n_ops_rejected = len(rejected)
    n_lint = len(lint_candidates) if lint_candidates else 0
    legacy_detected = 1 if bypass_signal is not None else 0
    logger.info(
        "memory_curation_run user=%s ops_applied=%d ops_rejected=%d "
        "lint_candidates=%d legacy_detected=%d agents_header_added=%d",
        user_id,
        n_ops_applied,
        n_ops_rejected,
        n_lint,
        legacy_detected,
        1 if header_added else 0,
    )

    # Phase A — observability only. Warn to log_channel when USER.md crosses
    # a soft threshold so growth is visible before it starts displacing
    # recall and KG facts in interactive prompts. Active size pressure is
    # phase B, deferred until we have data.
    _maybe_warn_usermd_size(config, user_id, new_size_bytes)

    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .search import index_file as _index_file
            if conn is not None:
                _index_file(conn, user_id, str(memory_path), new_text, "user_memory")
            else:
                with db.get_db(config.db_path) as temp_conn:
                    _index_file(temp_conn, user_id, str(memory_path), new_text, "user_memory")
        except Exception as e:
            logger.debug("USER.md re-index failed for %s: %s", user_id, e)

    if getattr(config.sleep_cycle, "curation_log_summary", True):
        _post_curation_summary(config, user_id, applied, rejected)

    return True


def _update_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    previous_last_task_id: int | None,
) -> None:
    """Update sleep cycle state with the latest completed task ID."""
    # Find the latest completed task ID for this user
    since = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48)).replace(tzinfo=None).isoformat()
    tasks = db.get_completed_tasks_since(conn, user_id, since, previous_last_task_id)
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_sleep_cycle_last_run(conn, user_id, latest_id)


def cleanup_old_memory_files(
    config: Config,
    user_id: str,
    retention_days: int,
) -> int:
    """
    Delete dated memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return 0

    # Cutoff in the user's timezone — filenames are user-local
    # YYYY-MM-DD, so a server-tz cutoff can be off by a day either way.
    # Live DB value so it tracks a web-UI tz change (ISSUE-099).
    tz_name = config.resolve_user_timezone(user_id)
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    cutoff = datetime.now(user_tz) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in context_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old memory file: %s", path.name)

    if deleted:
        logger.info("Cleaned up %d old memory file(s) for %s", deleted, user_id)

    return deleted


# --- Learned playbooks (Part B) --------------------------------------------

_PLAYBOOK_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _playbook_slug(title: str) -> str:
    """Filesystem-safe slug from a playbook title (lowercase-dashed)."""
    slug = _PLAYBOOK_SLUG_RE.sub("-", title.strip().lower()).strip("-")
    slug = slug[:80].strip("-")
    return slug or "playbook"


def _render_playbook(pb: dict, date_str: str) -> tuple[str, str]:
    """Render a playbook dict into (markdown_file_text, searchable_text).

    The file carries YAML frontmatter (title/triggers/created/ref_task_id/
    source) + the numbered steps. The searchable text (title + triggers +
    steps) is what gets indexed into memory_chunks so recall matches on
    triggers and title, not just the body.
    """
    title = str(pb.get("title", "")).strip()
    triggers = [str(t).strip() for t in pb.get("triggers", []) if str(t).strip()]
    steps = str(pb.get("steps", "")).strip()
    ref = pb.get("ref") or pb.get("ref_task_id") or ""

    fm_lines = [
        "---",
        f"title: {title}",
        f"triggers: [{', '.join(triggers)}]",
        f"created: {date_str}",
        f"ref_task_id: {ref}",
        "source: sleep_cycle",
        "---",
    ]
    body = f"# {title}\n\n{steps}\n"
    file_text = "\n".join(fm_lines) + "\n\n" + body
    searchable = f"{title}\n{' '.join(triggers)}\n\n{steps}"
    return file_text, searchable


# Match a top-level `pinned: true` frontmatter line, tolerating an optional
# quote, a trailing inline comment, and case. Anchored at line start (a nested /
# indented key is not a top-level flag).
_PINNED_FM_RE = re.compile(
    r"^pinned:\s*[\"']?true[\"']?\s*(?:#.*)?$", re.IGNORECASE | re.MULTILINE
)


def _playbook_is_pinned(path: Path, *, on_read_error: bool = False) -> bool:
    """True if the playbook file carries ``pinned: true`` in its frontmatter.

    A pinned playbook is a human-corrected version the sleep cycle must not
    clobber on the next same-slug re-derivation (ISSUE-174 Concern 1). Only the
    leading frontmatter block (the first complete ``---`` fence pair) is
    inspected; a match in the body doesn't count, and a file with an opening
    fence but no closing one has no valid frontmatter (so a body ``pinned: true``
    can't false-positive). Tolerates a leading BOM / blank lines, quoted value,
    and an inline comment.

    ``on_read_error`` is what an unreadable file answers, and the two callers
    need opposite answers because the consequence of being wrong is not the
    same shape (ISSUE-430). The re-derivation caller fails **open** (``False``,
    the default): the cost of overwriting is a regenerated playbook, and
    refusing to overwrite on a transient read failure would strand a stale one.
    The prune caller fails **closed** (``True``), because its next statement is
    ``path.unlink()`` — a transient read failure on the shared mount would
    otherwise delete a human-pinned playbook permanently, reported as nothing
    but a count. The loop's own ``except OSError`` cannot cover that, since the
    error is swallowed a frame down.
    """
    try:
        text = path.read_text()
    except OSError:
        return on_read_error
    text = text.lstrip("﻿").lstrip()
    if not text.startswith("---"):
        return False
    after_open = text[3:]
    m = re.search(r"\n---", after_open)
    if m is None:
        return False  # no closing fence → no valid frontmatter block
    front = after_open[: m.start()]
    return bool(_PINNED_FM_RE.search(front))


def _playbook_searchable_body(text: str) -> str:
    """Strip a leading YAML frontmatter block, returning the body for indexing.

    A pinned file is re-indexed from its own content; stripping the frontmatter
    keeps the indexed searchable text consistent with the freshly-rendered path
    (title + steps) instead of polluting the index with `pinned: true` /
    `source:` / `created:` tokens. No valid frontmatter → return the text as-is.
    """
    stripped = text.lstrip("﻿").lstrip()
    if not stripped.startswith("---"):
        return text.strip()
    after_open = stripped[3:]
    m = re.search(r"\n---[ \t]*(?:\n|$)", after_open)
    if m is None:
        return text.strip()
    return after_open[m.end():].strip()


def _process_extracted_playbooks(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    playbooks: list[dict],
    date_str: str,
    last_task_id: int | None,
) -> None:
    """Write each extracted playbook to a markdown file and index it.

    Dedup is by slug: re-deriving a playbook with the same title overwrites the
    existing file in place (and ``index_file`` replaces its chunks for that
    path) rather than proliferating near-duplicates. A file marked
    ``pinned: true`` in its frontmatter (a human-corrected version) keeps its
    on-disk content but is re-indexed from that content, so the correction
    reaches recall (which serves ``memory_chunks``, not the file) without the
    sleep cycle clobbering the human edit (ISSUE-174 Concern 1). Requires mount
    mode.
    """
    if not config.use_mount:
        logger.warning("Playbooks require mount mode, skipping writes for %s", user_id)
        return

    pb_dir = _get_mount_path(config, get_user_playbooks_path(user_id, config.bot_dir_name))
    pb_dir.mkdir(parents=True, exist_ok=True)

    index_enabled = (
        config.memory_search.enabled and config.memory_search.auto_index_memory_files
    )

    for pb in playbooks:
        title = str(pb.get("title", "")).strip()
        slug = _playbook_slug(title)
        file_text, searchable = _render_playbook(pb, date_str)
        pb_path = pb_dir / f"{slug}.md"
        existed = pb_path.exists()
        if existed and _playbook_is_pinned(pb_path):
            # Keep the human-corrected file, but REFRESH the index from its
            # current content — recall serves memory_chunks, not the file, so a
            # correction that isn't re-indexed never reaches the model
            # (ISSUE-174 fix 2). Skip only the overwrite.
            logger.info("playbook_pinned_skip slug=%s user=%s", slug, user_id)
            if index_enabled:
                try:
                    from .search import index_file as _index_file
                    body = _playbook_searchable_body(pb_path.read_text())
                    _index_file(conn, user_id, str(pb_path), body, "playbook")
                except Exception as e:
                    logger.debug("Pinned playbook reindex failed for %s: %s", pb_path.name, e)
            continue
        pb_path.write_text(file_text)
        logger.info(
            "playbook_written slug=%s ref=%s updated=%s user=%s",
            slug, pb.get("ref") or pb.get("ref_task_id") or "", existed, user_id,
        )

        if index_enabled:
            try:
                from .search import index_file as _index_file
                _index_file(conn, user_id, str(pb_path), searchable, "playbook")
            except Exception as e:
                logger.debug("Playbook indexing failed for %s: %s", pb_path.name, e)


_RETENTION_SENTINEL = ".retention_initialized"


def cleanup_old_playbooks(
    config: Config,
    user_id: str,
    retention_days: int,
    conn: "db.sqlite3.Connection | None" = None,
) -> int:
    """Delete learned-playbook files older than ``retention_days``.

    Returns the number deleted. ``retention_days <= 0`` keeps everything
    (default is 90 days). Age is measured by file mtime, which tracks
    last-*use*, not just last-write: ``_recall_playbooks`` stamps the file's
    mtime on every recall hit (ISSUE-174 Concern 3), and re-derivation rewrites
    it, so the prune targets playbooks that are neither recalled nor
    re-distilled — genuinely idle guidance — while a still-useful one stays
    fresh.

    Two guards keep the prune honest (ISSUE-174, Mulder/Scully review):
    - **Grandfather.** The first run after this use-based clock was introduced
      refreshes every existing playbook's mtime and writes a sentinel, deleting
      nothing — otherwise the first post-deploy cycle would prune by stale
      write-mtime before any recall history exists.
    - **Pinned files are never pruned**, and when ``conn`` is given the pruned
      file's ``memory_chunks`` are deleted too (recall reads chunks, so an
      unlinked-but-unindexed file would otherwise still be served).
    """
    if retention_days <= 0:
        return 0
    if not config.use_mount:
        return 0

    pb_dir = _get_mount_path(config, get_user_playbooks_path(user_id, config.bot_dir_name))
    if not pb_dir.exists():
        return 0

    now_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()

    # Grandfather one-shot: give every existing playbook a fresh window keyed
    # from first upgrade so nothing is deleted on stale write-mtime.
    sentinel = pb_dir / _RETENTION_SENTINEL
    if not sentinel.exists():
        unrefreshed = 0
        for path in pb_dir.iterdir():
            if path.is_file() and path.suffix == ".md":
                try:
                    os.utime(path, (now_ts, now_ts))
                except OSError as e:
                    # If the mount rejects utimens the grandfather can't refresh
                    # this file's clock, and the prune below would then delete it
                    # on stale *write*-mtime. Warn rather than debug: this is the
                    # point where a use-based clock silently degrades into a
                    # write-based one that deletes files.
                    logger.warning(
                        "playbook grandfather mtime refresh failed for %s: %s", path.name, e
                    )
                    unrefreshed += 1
                    continue
        if unrefreshed:
            # Leave the sentinel unwritten so the next cycle grandfathers again
            # instead of arming the prune against clocks we could not set
            # (ISSUE-430). Costs a repeated pass on a mount that never accepts
            # utimens; the alternative is deleting live playbooks by write age.
            logger.warning(
                "playbook_retention_grandfather_incomplete user=%s unrefreshed=%d "
                "(prune stays disabled until every playbook's clock can be set)",
                user_id,
                unrefreshed,
            )
            return 0
        try:
            sentinel.write_text(datetime.now(tz=ZoneInfo("UTC")).date().isoformat())
        except OSError as e:
            # Same reasoning in the other direction: without the sentinel the
            # next cycle re-grandfathers, which is the safe way to fail.
            logger.warning("playbook retention sentinel write failed: %s", e)
        logger.info("playbook_retention_grandfathered user=%s", user_id)
        return 0

    cutoff_ts = now_ts - retention_days * 86400

    deleted = 0
    for path in pb_dir.iterdir():
        if not (path.is_file() and path.suffix == ".md"):
            continue
        try:
            # Fail closed: an unreadable file is treated as pinned, because the
            # alternative on this path is an unlink we cannot undo.
            if _playbook_is_pinned(path, on_read_error=True):
                continue
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                if conn is not None:
                    try:
                        from .search import _delete_source_chunks
                        _delete_source_chunks(conn, user_id, "playbook", str(path))
                    except Exception as e:
                        logger.debug("Playbook chunk cleanup failed for %s: %s", path.name, e)
                deleted += 1
        except OSError:
            continue

    if deleted:
        logger.info("Cleaned up %d old playbook(s) for %s", deleted, user_id)
    return deleted


def check_sleep_cycles(conn: "db.sqlite3.Connection", config: Config) -> list[str]:
    """
    Evaluate sleep cycle cron for all users, process when due.

    Returns list of user_ids that were processed.
    """
    if not config.sleep_cycle.enabled:
        return []

    # ISSUE-181: non-essential task policy. The sleep cycle calls the primary
    # brain directly (not the executor's fallback path), so when the primary is
    # in a usage_limit/not_found state the whole pass is skipped — no per-user
    # extraction, no curation, no playbook distillation. ``_run_sleep_cycle_brain``
    # also short-circuits per call, so a failure mid-pass stops the remaining
    # users; this top-level check avoids even iterating when the breaker is
    # already open from a prior task or cycle.
    available, _reason = primary_brain_unavailable(config.brain)
    if not available:
        logger.info(
            "Sleep cycle skipped — primary brain unavailable (cooling down)"
        )
        return []

    sleep_config = config.sleep_cycle
    processed = []

    for user_id, user_config in config.users.items():
        # Evaluate cron in the user's timezone — live DB value so a web-UI
        # change moves the sleep-cycle schedule without a restart (ISSUE-099).
        try:
            user_tz = ZoneInfo(config.resolve_user_timezone(user_id))
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = datetime.now(user_tz)

        should_run = False
        last_run_at, _ = db.get_sleep_cycle_last_run(conn, user_id)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(sleep_config.cron, last_run.astimezone(user_tz))
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(sleep_config.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            # ISSUE-181: a prior user's failure may have opened the breaker
            # mid-pass — re-check before each user so the remaining ones skip
            # instead of repeating the doomed brain call.
            avail, _r = primary_brain_unavailable(config.brain)
            if not avail:
                logger.info(
                    "Sleep cycle: skipping remaining users — "
                    "primary brain unavailable"
                )
                break
            logger.info("Running sleep cycle for user %s", user_id)
            try:
                wrote = process_user_sleep_cycle(config, conn, user_id)
                if wrote:
                    processed.append(user_id)
            except Exception as e:
                logger.error("Sleep cycle failed for %s: %s", user_id, e)

    return processed


# ============================================================================
# Channel sleep cycle (shared channel memory extraction)
# ============================================================================


def gather_channel_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather channel interaction data for memory extraction.

    Like gather_day_data but filters by conversation_token and includes
    user_id attribution per task.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since_str, after_task_id
    )

    if not tasks:
        return ""

    per_task_budget = max(_MIN_TASK_BUDGET, MAX_DAY_DATA_CHARS // len(tasks))
    speakers = speaker_labels(conn, config, tasks)

    parts = []
    for task in tasks:
        prompt_budget = int(per_task_budget * 0.4)
        result_budget = per_task_budget - prompt_budget
        prompt_text = _excerpt(task.prompt or "", prompt_budget)
        result_text = _excerpt(task.result or "", result_budget)
        speaker = speakers.get(task.id, "User")
        parts.append(
            f"--- Task {task.id} (user: {task.user_id}, {task.source_type}, {task.created_at or 'unknown'}) ---\n"
            f"{speaker}: {prompt_text}\n"
            f"Bot: {result_text}\n"
        )

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = _excerpt(combined, MAX_DAY_DATA_CHARS)

    return combined


def build_channel_memory_extraction_prompt(
    conversation_token: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
) -> str:
    """
    Build prompt for extracting shared memories from channel conversations.

    Focuses on shared context (decisions, agreements, project status) rather
    than personal information.
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing channel memory (CHANNEL.md)

The following information is already stored in this channel's memory file.
Do NOT repeat any of this information. Respect the existing structure —
produce new items that could be appended under appropriate headings.

{existing_memory}
"""

    return f"""You are extracting shared memories from a day of conversations in channel '{conversation_token}'.

Date: {date_str}
{existing_section}
## Today's channel interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering as shared channel context.
Focus on:
- Decisions made or agreements reached by the group
- Project status updates, milestones, or blockers
- Action items assigned to specific people
- Technical decisions or architecture choices
- Important context that would help anyone in the channel
- Links between topics discussed here and other projects

Do NOT include:
- Information already in the existing channel memory above
- Personal/private information about individual users
- Trivial exchanges (greetings, acknowledgments, small talk)
- Temporary states that are no longer relevant
- Raw data or lengthy outputs

Format your output as concise bullet points with dates, attribution, and task references, like:
- Decided to migrate API to GraphQL (alice, 2026-01-28, ref:1234)
- Blocked on infrastructure approval for prod deploy (bob, 2026-01-28, ref:1235)

If there is genuinely nothing new worth remembering, respond with exactly: {NO_NEW_MEMORIES}

Output ONLY the bullet points (or {NO_NEW_MEMORIES}). No preamble, no explanation."""


def process_channel_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
) -> bool:
    """
    Run the channel sleep cycle: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    csc = config.channel_sleep_cycle

    # Get last run state
    last_run_at, last_task_id = db.get_channel_sleep_cycle_last_run(
        conn, conversation_token
    )

    # Gather channel data
    day_data = gather_channel_data(
        config, conn, conversation_token, csc.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info(
            "Channel sleep cycle for %s: no new interactions, skipping",
            conversation_token,
        )
        db.set_channel_sleep_cycle_last_run(conn, conversation_token, last_task_id)
        return False

    # Load existing channel memory to avoid duplication
    existing_memory = read_channel_memory(config, conversation_token)

    # Build extraction prompt — channel sleep cycle stays UTC because
    # channels span timezones. (`datetime.now()` is server-local, so go
    # via ZoneInfo("UTC") explicitly.)
    date_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    prompt = build_channel_memory_extraction_prompt(
        conversation_token, day_data, existing_memory, date_str
    )

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=csc.extraction_model,
        label=f"Channel sleep cycle extraction for {conversation_token}",
        # A channel pass has no single owner — same sentinel shared briefing
        # blocks use, so a per-user grouping keeps one no-owner bucket.
        user_id=SYSTEM_USER_ID,
        conn=conn,
    )
    if not ok:
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info(
            "Channel sleep cycle for %s: no new memories to save",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    # Write dated memory file
    if not config.use_mount:
        logger.warning(
            "Channel sleep cycle requires mount mode, skipping file write for %s",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    memories_dir = _get_mount_path(config, get_channel_memories_path(conversation_token))
    memories_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memories_dir / f"{date_str}.md"
    memory_file.write_text(output + "\n")
    logger.info(
        "Wrote channel memory file for %s: %s (%d chars)",
        conversation_token,
        memory_file.name,
        len(output),
    )

    # Index memory file for semantic search (non-critical)
    channel_user_id = f"channel:{conversation_token}"
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .search import index_file as _index_file

            _index_file(
                conn,
                channel_user_id,
                str(memory_file),
                output,
                "channel_memory",
            )
        except Exception as e:
            logger.debug(
                "Memory search indexing failed for channel %s: %s",
                conversation_token,
                e,
            )

    # Re-index CHANNEL.md as durable channel memory. Done unconditionally each
    # channel sleep cycle (the file is small, embedding is fast) which also
    # covers the case where humans edit CHANNEL.md directly via Nextcloud.
    _reindex_channel_durable(config, conn, conversation_token)

    # Update state
    _update_channel_state(config, conn, conversation_token, last_task_id)

    # Clean up old memory files
    cleanup_old_channel_memory_files(
        config, conversation_token, csc.memory_retention_days
    )

    # Clean up old ephemeral memory_chunks for this channel (channel_memory
    # source_type only). channel_memory_durable (CHANNEL.md) is intentionally
    # excluded — it refreshes on edit, like USER.md.
    if csc.memory_retention_days > 0:
        try:
            from .search import cleanup_old_chunks
            channel_user_id = f"channel:{conversation_token}"
            n = cleanup_old_chunks(
                conn,
                channel_user_id,
                csc.memory_retention_days,
                source_types=("channel_memory",),
            )
            if n:
                logger.info(
                    "Pruned %d channel memory chunks for %s", n, conversation_token
                )
        except Exception as e:
            logger.debug(
                "channel memory_chunks cleanup failed for %s: %s", conversation_token, e
            )

    return True


def _reindex_channel_durable(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
) -> None:
    """Re-index CHANNEL.md under source_type=channel_memory_durable.

    Like USER.md indexing — durable, refreshed on file edit (or here, on each
    channel sleep cycle). Excluded from EPHEMERAL_SOURCE_TYPES so retention
    never deletes it. No-op if the file doesn't exist or memory search is
    disabled.
    """
    if not (config.memory_search.enabled and config.memory_search.auto_index_memory_files):
        return
    if not config.use_mount:
        return
    try:
        channel_md = _get_mount_path(config, get_channel_memory_path(conversation_token))
    except Exception:
        return
    if not channel_md.is_file():
        return
    try:
        content = channel_md.read_text()
    except Exception:
        return
    if not content.strip():
        return
    try:
        from .search import index_file as _index_file
        _index_file(
            conn,
            f"channel:{conversation_token}",
            str(channel_md),
            content,
            "channel_memory_durable",
        )
    except Exception as e:
        logger.debug(
            "CHANNEL.md durable indexing failed for %s: %s", conversation_token, e
        )


def _update_channel_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    previous_last_task_id: int | None,
) -> None:
    """Update channel sleep cycle state with the latest completed task ID."""
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48))
        .replace(tzinfo=None)
        .isoformat()
    )
    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since, previous_last_task_id
    )
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_channel_sleep_cycle_last_run(conn, conversation_token, latest_id)


def cleanup_old_channel_memory_files(
    config: Config,
    conversation_token: str,
    retention_days: int,
) -> int:
    """
    Delete dated channel memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    memories_dir = _get_mount_path(
        config, get_channel_memories_path(conversation_token)
    )
    if not memories_dir.exists():
        return 0

    # Channel filenames are written in UTC (channels span timezones); cutoff
    # follows the same convention to stay aligned.
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in memories_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old channel memory file: %s", path.name)

    if deleted:
        logger.info(
            "Cleaned up %d old channel memory file(s) for %s",
            deleted,
            conversation_token,
        )

    return deleted


def check_channel_sleep_cycles(
    conn: "db.sqlite3.Connection",
    config: Config,
) -> list[str]:
    """
    Evaluate channel sleep cycle cron, auto-discover active channels, process when due.

    Returns list of conversation_tokens that were processed.
    """
    if not config.channel_sleep_cycle.enabled:
        return []

    # ISSUE-181: same non-essential-task skip as the per-user sleep cycle. The
    # channel loop is exactly the case the issue calls out — four channels, four
    # identical usage_limit errors in six seconds. Skipping the whole pass when
    # the primary is degraded stops the first failure from guaranteeing the rest.
    available, _reason = primary_brain_unavailable(config.brain)
    if not available:
        logger.info(
            "Channel sleep cycle skipped — primary brain unavailable (cooling down)"
        )
        return []

    csc = config.channel_sleep_cycle
    processed = []

    # Auto-discover active channels from recent completed tasks
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=csc.lookback_hours))
        .replace(tzinfo=None)
        .isoformat()
    )
    active_tokens = db.get_active_channel_tokens(conn, since)

    if not active_tokens:
        return []

    # Evaluate cron in UTC (channels span users in different timezones)
    now = datetime.now(ZoneInfo("UTC"))

    for token in active_tokens:
        should_run = False
        last_run_at, _ = db.get_channel_sleep_cycle_last_run(conn, token)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(csc.cron, last_run)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(csc.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            # ISSUE-181: a prior channel's failure may have opened the breaker
            # mid-pass — re-check before each channel so the remaining ones skip
            # instead of repeating the doomed brain call. (The whole pass is also
            # short-circuited at the top when the breaker was already open.)
            avail, _r = primary_brain_unavailable(config.brain)
            if not avail:
                logger.info(
                    "Channel sleep cycle: skipping remaining channels — "
                    "primary brain unavailable"
                )
                break
            logger.info("Running channel sleep cycle for %s", token)
            try:
                wrote = process_channel_sleep_cycle(config, conn, token)
                if wrote:
                    processed.append(token)
            except Exception as e:
                logger.error(
                    "Channel sleep cycle failed for %s: %s", token, e
                )

    return processed
