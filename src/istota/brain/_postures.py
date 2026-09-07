"""Fallback-compatibility posture registry for scheduled/automatic tasks (ISSUE-181).

Every scheduled or automatic task that invokes a brain declares *one* posture
for what happens when the primary brain is unavailable and the fallback is
engaged (or the brain is down entirely). The posture is a declared,
discoverable property rather than ad-hoc per-task logic, so the scheduler can
reason about each task uniformly and the operator can audit them in one place.

Postures (from ISSUE-181's Problem 3):

- **skip** — non-essential tasks (sleep cycle, shared-block synthesis, location
  discovery, reading constellation, notes frontmatter, playbook generation,
  curation) that can simply wait. Don't run them against a degraded brain;
  resume when the primary is healthy again. This is the cheapest and most
  common posture. Implemented via the shared availability breaker: the task
  consults ``primary_brain_unavailable`` before its brain call and feeds its
  failures back via ``report_brain_result`` so the breaker is a single shared
  signal across all callers.

- **pin** — essential tasks that must produce a real answer and shouldn't ride
  the fallback brain at all; pinned to a known-good model so they never land on
  the fallback. Briefings are the canonical case (ISSUE-180): a briefing on the
  GLM fallback delivers only the model note, so the direction there is to pin
  briefings off the fallback or fail them cleanly. (The pin/fail-clean decision
  for briefings is ISSUE-180's scope; this registry records the posture.)

- **fail-clean** — tasks that can't run on the fallback but whose failure should
  be *visible* (a clear "skipped, brain unavailable" notice) rather than
  silently shipping a stub, an empty body, or stale content with no staleness
  signal. Interactive-but-automatic callers (health OCR, the biomarker
  explainer) fall here today: they run on-demand from a user action, so a clean
  "couldn't generate — brain unavailable" is the right user-facing outcome.

The registry is a data structure, not an enforcement layer: each task still
owns its skip/pin/fail-clean logic at its call site (the breaker consult, the
pinned model, the clean-fail notice). Centralizing the *declaration* here makes
the policy auditable ("which tasks degrade how?") and gives a single place to
update when a task's posture changes. A task not listed here is either
interactive (routes through the executor's fallback wrapper and needs no
separate posture) or not yet assessed.

See ISSUE-181 (this file), ISSUE-180 (briefings pin/fail-clean — the inverse
face of the policy), ISSUE-177 (usage_limit classification — what makes the
breaker open), ISSUE-165 (fallback brain config).
"""

from __future__ import annotations

from dataclasses import dataclass

POSTURE_SKIP = "skip"
POSTURE_PIN = "pin"
POSTURE_FAIL_CLEAN = "fail_clean"


@dataclass(frozen=True)
class TaskPosture:
    """The declared fallback-compatibility posture for one automatic task."""

    name: str
    posture: str
    call_site: str
    notes: str


# The registry. Ordered roughly by the ISSUE-181 Problem 3 enumeration.
REGISTRY: tuple[TaskPosture, ...] = (
    TaskPosture(
        name="sleep_cycle_user",
        posture=POSTURE_SKIP,
        call_site="istota.memory.sleep_cycle:_run_sleep_cycle_brain / check_sleep_cycles",
        notes=(
            "Per-user memory extraction + curation + playbook distillation. "
            "Non-essential — skips the whole pass when the primary is degraded; "
            "the breaker cooldown gates the next scheduled run. Implemented."
        ),
    ),
    TaskPosture(
        name="sleep_cycle_channel",
        posture=POSTURE_SKIP,
        call_site="istota.memory.sleep_cycle:_run_sleep_cycle_brain / check_channel_sleep_cycles",
        notes=(
            "Per-channel shared-memory extraction. Non-essential — the first "
            "channel failure opens the breaker and the remaining channels skip "
            "in-pass. Implemented."
        ),
    ),
    TaskPosture(
        name="shared_block_synthesis",
        posture=POSTURE_SKIP,
        call_site="istota.briefings.shared_blocks:_run_section_brain / scheduler._generate_shared_block",
        notes=(
            "Synthesis shared blocks (e.g. world-headlines). Non-essential — "
            "skips the gather+synthesize and keeps last-known-good content; one "
            "operator alert fires when the breaker opens. Implemented. Structured "
            "shared blocks (e.g. markets-summary) have POSTURE N/A — no brain call."
        ),
    ),
    TaskPosture(
        name="briefing",
        posture=POSTURE_PIN,
        call_site="istota.executor.build_deferred_briefing_prompt / scheduler briefing delivery",
        notes=(
            "Scheduled briefings are essential (must not fail). The fallback "
            "brain produces an unusable body (ISSUE-180), so the direction is to "
            "pin briefings to a known-good model off the fallback, or fail them "
            "cleanly. Pin/fail-clean decision is ISSUE-180's scope."
        ),
    ),
    TaskPosture(
        name="health_ocr",
        posture=POSTURE_FAIL_CLEAN,
        call_site=(
            "istota.health.ocr / encounter_ocr / immunization_ocr:_call_brain "
            "-> istota.health._brain_call:call_health_brain"
        ),
        notes=(
            "User-triggered (upload) OCR. Interactive — a clean 'couldn't "
            "extract, brain unavailable' is the right user-facing outcome rather "
            "than a silent stub. Currently returns None on failure (the upload "
            "review UI surfaces a fallback message); not yet breaker-aware."
        ),
    ),
    TaskPosture(
        name="health_biomarker_explainer",
        posture=POSTURE_FAIL_CLEAN,
        call_site=(
            "istota.health.explainer:_call_brain "
            "-> istota.health._brain_call:call_health_brain"
        ),
        notes=(
            "User-triggered educational alert for an out-of-range marker. "
            "Interactive — already falls back to a fixed safe payload on failure "
            "so the UI never shows raw model output. Not yet breaker-aware."
        ),
    ),
    TaskPosture(
        name="scheduled_prompt_job",
        posture=POSTURE_PIN,
        call_site="istota.scheduler.check_scheduled_jobs -> executor.execute_task (fallback-wrapped)",
        notes=(
            "CRON.md ``prompt``/``prompt_file`` jobs route through the executor, "
            "so they already ride the fallback wrapper. Per-job ``model`` pins "
            "are the operator's lever to keep a job off the fallback. No separate "
            "skip logic — these are user-authored and may be essential."
        ),
    ),
    TaskPosture(
        name="scheduled_command_job",
        posture=POSTURE_FAIL_CLEAN,
        call_site="istota.scheduler._execute_command_task",
        notes=(
            "CRON.md ``command`` jobs run a shell subprocess — no brain call, so "
            "brain unavailability is N/A. A publish_shared_kv job's *result* may "
            "come from a prompt job (covered above). Listed for completeness."
        ),
    ),
    TaskPosture(
        name="location_discovery",
        posture=POSTURE_SKIP,
        call_site="istota.location (reconciler / cluster discovery)",
        notes=(
            "Place detection runs off GPS pings + heuristics — no brain call "
            "today. Listed as POSTURE_SKIP by intent: if it ever grows a brain "
            "step (e.g. LLM place-naming), it should skip when degraded. N/A now."
        ),
    ),
)


def postures_by_name() -> dict[str, TaskPosture]:
    """Return the registry as a ``{name: TaskPosture}`` dict."""
    return {p.name: p for p in REGISTRY}


__all__ = [
    "POSTURE_FAIL_CLEAN",
    "POSTURE_PIN",
    "POSTURE_SKIP",
    "REGISTRY",
    "TaskPosture",
    "postures_by_name",
]
