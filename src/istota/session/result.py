"""Result composition and malformed-output detection.

Brain-agnostic post-processing of a model run's final text. Operates on the
``(result_text, execution_trace)`` pair that any brain produces, so it lives in
the session layer rather than inside a specific brain. Extracted verbatim from
``executor.py`` in Phase 0 of the agent-loop migration; the executor re-exports
every public symbol for backward compatibility.

Two mechanisms share one ``_last_substantial_region()`` walker. Both
**replace** ``result_text`` outright — never prepend or glue. (The one path
that synthesizes text rather than choosing between candidates is
``_ensure_final_answer``, and only when there is no answer at all to protect;
see the finality rule below.)

- **Mechanism A — CM-aware (ISSUE-026):** runs whenever ``cm_boundary`` events
  exist in the trace. Segments by ``cm_boundary`` and returns the last region
  ≥ ``_CM_SEGMENT_MIN_CHARS``. Runs for automated tasks too.
- **Mechanism B — terse-recovery (ISSUE-025):** runs only on non-automated
  tasks whose ``result_text`` is terse. Segments by both ``tool`` and
  ``cm_boundary`` and returns the last region ≥ ``_TRAILING_REGION_MIN_CHARS``.

Both are bounded by the **finality rule** (ISSUE-211). The channel guidelines
promise the model that text written between tool calls streams as a progress
indicator and is not the saved reply, so a text region followed by a tool call
is mid-turn narration by construction — the model kept working after writing
it — and can never stand in as the final answer. Recovery therefore looks only
at the region after the last ``tool`` entry. The one exception is an explicit
back-reference ("see above", "done"): there the model itself says the answer
is earlier, which is the ISSUE-025 case, so reaching back is honouring it
rather than guessing. When neither the brain nor the trace yields a final
message, ``_ensure_final_answer`` says so instead of silently promoting the
last status fragment.
"""

import logging
import re

logger = logging.getLogger("istota.session.result")


# Patterns that indicate leaked tool-call XML in model output.
# These are Claude Code's internal framing and should never appear in user-facing text.
_TOOL_SYNTAX_PATTERN = re.compile(
    r"</parameter>|</invoke>|<invoke\s|<parameter\s|</?antml:|</?thinking>",
)

# Matches fenced code blocks (``` ... ```) to strip before strict checking.
#
# Deliberately *not* `llm_json`, though the shape looks identical, and this
# comment is here because the next reader will reach for it. Two reasons.
# It removes every fenced block rather than unwrapping one, which is not an
# operation that module has. And it is the one member of this family whose
# markers must stay unanchored: the question below is whether leaked tool-call
# XML sits inside a code block, so a line-anchored fence would stop matching
# an inline ```<invoke``` and flag a legitimate answer as malformed on the
# strict Talk path. It is also not the quadratic shape `llm_json` exists to
# fix — its closer is a bare backtick run, so a document of repeated openers
# supplies its own closers; measured at 0.002s on 256 KB of them, against
# 26.5s for the health expression that motivated the module.
_CODE_FENCE_PATTERN = re.compile(r"```[\s\S]*?```")


def detect_malformed_result(
    text: str,
    output_target: str | None = None,
) -> str | None:
    """Detect model output that is leaked tool-call XML rather than a real response.

    When output_target is "talk" (or "both"/"all"), applies stricter checking:
    Talk output should be valid markdown, so any tool-call XML outside of code
    fences is flagged regardless of how much other content surrounds it.

    Returns a reason string if malformed, None if the result looks okay.
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()
    # Strict mode applies whenever Talk is one of the resolved delivery
    # destinations (Talk output must be valid markdown). Parse the descriptor
    # through the routing helpers so talk / both / all / talk:<token> all gate
    # strict, rather than matching a hardcoded string set.
    from ..transport import parse_output_target, plan_has_surface
    strict = plan_has_surface(parse_output_target(output_target), "talk")

    # Check for leaked tool-call XML syntax
    if strict:
        # Strip code fences first — XML in code blocks is fine
        outside_fences = _CODE_FENCE_PATTERN.sub("", stripped)
        if _TOOL_SYNTAX_PATTERN.search(outside_fences):
            return f"leaked tool-call XML in Talk output ({len(stripped)} chars)"
    else:
        if _TOOL_SYNTAX_PATTERN.search(stripped):
            # Only flag if the entire content is syntax fragments
            non_syntax = _TOOL_SYNTAX_PATTERN.sub("", stripped).strip()
            if len(non_syntax) < 20:
                return f"leaked tool-call XML ({len(stripped)} chars, {len(non_syntax)} chars of non-syntax content)"

    return None


# Minimum joined-region length for a CM segment to override result_text.
_CM_SEGMENT_MIN_CHARS = 200

# Below this absolute char count, result_text counts as "terse" and is eligible
# for replacement by a substantial trailing trace region.
_TERSE_RESULT_MAX_CHARS = 150

# Minimum joined-region length for terse-recovery to override result_text.
# Calibration is empirical; log overrides and tune over a sprint of data.
_TRAILING_REGION_MIN_CHARS = 500

# Source types whose tasks emit structured / programmatic output and never
# benefit from terse-recovery (Mechanism B). Mechanism A still runs for these.
_AUTOMATED_SOURCE_TYPES = frozenset({"scheduled", "briefing"})

# Result texts that are clearly references rather than the answer itself,
# regardless of length.
_TERSE_REFERENCE_RE = re.compile(
    r"^(see above|as (shown|stated)( above)?|done|ok|✓|"
    r"that's everything|that('s| is) it|all done)\.?$",
    re.IGNORECASE,
)

# What a completed turn delivers when neither the brain nor the trace produced
# a final message. Better than an empty reply, and better than passing off the
# last progress note as the answer (ISSUE-211).
_NO_FINAL_ANSWER_NOTICE = "The turn ended without a final response."

# Lead-in when there *is* earlier work to show. Labelled rather than promoted:
# the user still gets what the turn produced, framed as unfinished rather than
# presented as the answer. Deliberately a statement of fact about *where* the
# text came from, not a claim about what it is — the composer can see that the
# model went on to call another tool, but not whether what it wrote first was
# narration or a complete answer it then followed with a save.
_PARTIAL_PROGRESS_LEAD = (
    f"{_NO_FINAL_ANSWER_NOTICE} This is the last text it produced before "
    "stopping:"
)


def is_no_final_answer(text: str) -> bool:
    """True when ``text`` is the composer's synthesized no-final-answer output
    rather than something the model wrote. Callers that interpret a result —
    the scheduler's confirmation gate above all — must not read the embedded
    mid-turn text as if the model had addressed it to the user."""
    return text.startswith(_NO_FINAL_ANSWER_NOTICE)


def _text_similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 Jaccard similarity between two strings using word bigrams.

    More robust than SequenceMatcher for repetitive text patterns.
    For very long strings, compare just the first 8000 chars to stay fast.
    """
    limit = 8000
    a_words = a[:limit].lower().split()
    b_words = b[:limit].lower().split()
    if len(a_words) < 2 or len(b_words) < 2:
        return 1.0 if a[:limit] == b[:limit] else 0.0
    shingles_a = {(a_words[i], a_words[i + 1]) for i in range(len(a_words) - 1)}
    shingles_b = {(b_words[i], b_words[i + 1]) for i in range(len(b_words) - 1)}
    intersection = len(shingles_a & shingles_b)
    union = len(shingles_a | shingles_b)
    return intersection / union if union else 0.0


def _last_substantial_region(
    trace: list[dict],
    delimiters: set[str],
    min_chars: int,
    *,
    trailing_only: bool = False,
) -> str | None:
    """Walk the trace, group text events into regions delimited by event types
    in ``delimiters``, then return the joined text of the last region whose
    length is ≥ ``min_chars``. Returns ``None`` if no region qualifies.

    Adjacent ``text`` events within a region are joined with ``\\n\\n``, so
    a paragraph split into multiple events by streaming aggregates back into
    one region — no per-block size filter is needed.

    ``trailing_only`` restricts the walk to what the model wrote after its last
    tool call — its final message. Everything earlier is mid-turn narration by
    construction (a tool call followed it), and promoting that to the durable
    answer is ISSUE-211.
    """
    if trailing_only:
        cut = -1
        for i, entry in enumerate(trace):
            if entry.get("type") == "tool":
                cut = i
        trace = trace[cut + 1:]

    regions: list[list[str]] = [[]]
    for entry in trace:
        et = entry.get("type")
        if et in delimiters:
            regions.append([])
        elif et == "text":
            t = entry.get("text", "").strip()
            if t:
                regions[-1].append(t)
    for seg in reversed(regions):
        joined = "\n\n".join(seg)
        if len(joined) >= min_chars:
            return joined
    return None


def _is_automated_task(task) -> bool:
    """True when the task is automated / structured-output and shouldn't
    trigger terse-recovery.

    Checks ``source_type`` plus structural fallbacks (``heartbeat_silent``,
    ``scheduled_job_id``) in case a future code path stamps a non-scheduled
    source_type on a heartbeat-style task. Defense in depth — robust to
    source_type churn without locking the gate to one string set.
    """
    if task is None:
        return False
    if getattr(task, "source_type", None) in _AUTOMATED_SOURCE_TYPES:
        return True
    if getattr(task, "heartbeat_silent", False):
        return True
    if getattr(task, "scheduled_job_id", None) is not None:
        return True
    return False


def _is_terse(text: str) -> bool:
    """True when ``text`` is short enough or matches a known reference
    pattern such that it's likely a stand-in rather than the real answer.
    Empty text is treated as terse (recovery is wanted)."""
    s = text.strip()
    if not s:
        return True
    if len(s) < _TERSE_RESULT_MAX_CHARS:
        return True
    return bool(_TERSE_REFERENCE_RE.match(s))


def _is_back_reference(text: str) -> bool:
    """True when the model's final text explicitly points at earlier output
    ("see above", "done"). That licenses recovery to reach back past a tool
    boundary — the model is telling us where the answer is, not narrating."""
    return bool(_TERSE_REFERENCE_RE.match(text.strip()))


def _ensure_final_answer(result_text: str, trace: list[dict], task) -> str:
    """Last line of defence: a completed turn must not deliver an empty reply,
    and must not pass a mid-turn status fragment off as the answer.

    When ``result_text`` is empty and recovery found no final message, say so.
    Any earlier work is appended under a label so it is still visible without
    being presented as the answer. Automated tasks are exempt because their
    output is parsed rather than read — a briefing body goes through
    ``parse_briefing_json``, so prose here would be archived as the digest.
    """
    if result_text.strip() or _is_automated_task(task):
        return result_text
    # Nothing came back from the brain, so there is no good answer to protect
    # and the size floors above don't apply: whatever the model wrote after its
    # last tool call is its final message, however short, and is adopted as-is.
    trailing = _last_substantial_region(
        trace, {"cm_boundary"}, 1, trailing_only=True,
    )
    if trailing:
        _log_compose_override(task, "empty_result", result_text, trailing)
        return trailing
    # Only mid-turn narration is left. Show it, labelled — no size floor, since
    # anything the turn produced beats nothing when the answer is gone.
    partial = _last_substantial_region(trace, {"tool", "cm_boundary"}, 1)
    logger.info(
        "compose_full_result: mechanism=no_final_answer task_id=%s "
        "source_type=%s partial_chars=%d",
        getattr(task, "id", None),
        getattr(task, "source_type", None),
        len(partial or ""),
    )
    if not partial:
        return _NO_FINAL_ANSWER_NOTICE
    return f"{_PARTIAL_PROGRESS_LEAD}\n\n{partial}"


def _log_compose_override(
    task,
    mechanism: str,
    original: str,
    recovered: str,
) -> None:
    logger.info(
        "compose_full_result: mechanism=%s task_id=%s source_type=%s "
        "original_chars=%d recovered_chars=%d",
        mechanism,
        getattr(task, "id", None),
        getattr(task, "source_type", None),
        len(original.strip()),
        len(recovered),
    )


def _compose_full_result(
    result_text: str,
    execution_trace: list[dict],
    task=None,
) -> str:
    """Reconcile the model's ResultEvent with text events from the trace.

    Recovers from two failure modes:

    - **CM mid-response truncation (ISSUE-026):** context management fires
      mid-response, so ResultEvent only sees the post-CM tail. Mechanism A
      walks segments delimited by ``cm_boundary`` and returns the last one
      whose text crosses ``_CM_SEGMENT_MIN_CHARS``. Always runs when CM
      events are present, including for automated tasks.

    - **Terse-reference ResultEvent (ISSUE-025):** the model writes the real
      answer as a text event, does one more tool call, then ResultEvent
      comes back as ``"see above"`` / ``"done"`` / a one-line reference.
      Mechanism B walks segments delimited by both ``cm_boundary`` and
      ``tool``, returning the last region ≥ ``_TRAILING_REGION_MIN_CHARS``.
      Gated by ``_is_automated_task`` and ``_is_terse(result_text)`` —
      structured-output tasks and substantial results bypass.

    Both mechanisms are bounded by the finality rule (ISSUE-211): only the
    region after the last tool call is eligible, unless ``result_text`` is an
    explicit back-reference. When nothing usable is left and ``result_text`` is
    empty, ``_ensure_final_answer`` surfaces that rather than shipping the last
    status fragment.

    Returns ``result_text`` unchanged when no override is justified. Override
    or trust — never glue; the sole synthesis path is ``_ensure_final_answer``,
    reached only when there is no answer to protect. Logs every override for
    calibration.
    """
    trace = execution_trace or []
    # A back-reference is the model pointing at its own earlier text, which is
    # the only case where mid-turn text may be adopted as the answer.
    reach_back = _is_back_reference(result_text)

    # Mechanism A — CM-aware. Always runs when CM events exist.
    if any(e.get("type") == "cm_boundary" for e in trace):
        recovered = _last_substantial_region(
            trace, {"cm_boundary"}, _CM_SEGMENT_MIN_CHARS,
            trailing_only=not reach_back,
        )
        if recovered is not None and recovered.strip() != result_text.strip():
            _log_compose_override(task, "cm_aware", result_text, recovered)
            return recovered
        return _ensure_final_answer(result_text, trace, task)

    # Mechanism B — terse-recovery. Source-type and terseness gates.
    if _is_automated_task(task) or not _is_terse(result_text):
        return _ensure_final_answer(result_text, trace, task)

    recovered = _last_substantial_region(
        trace, {"tool", "cm_boundary"}, _TRAILING_REGION_MIN_CHARS,
        trailing_only=not reach_back,
    )
    if recovered is None or recovered in result_text:
        return _ensure_final_answer(result_text, trace, task)

    _log_compose_override(task, "terse_recovery", result_text, recovered)
    return recovered
