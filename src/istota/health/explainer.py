"""Per-biomarker out-of-range explainer.

Generates a short, educational alert when a user's latest measurement for
a biomarker falls outside the canonical reference range. The output is
cached per (name, direction) in the user's health DB so we only spend
one brain call per biomarker per direction per user.

Hard rules baked into the prompt:

* Never diagnose a specific disease.
* Never prescribe medications, dosages, or treatments.
* Always frame as "may", "can", "possible", "consider".
* Always close with a note to consult a healthcare professional.

The brain output is parsed strictly: a JSON object with ``summary``
(string), ``causes`` (list of strings), ``mitigations`` (list of strings).
Anything else is rejected and the route returns a generic fallback so
the UI never shows raw model output.
"""

from __future__ import annotations

import json
import logging
import re

from istota.llm_json import strip_fences
from istota.health._brain_call import call_health_brain
from istota.health import db as health_db
from istota.health.models import HealthContext


logger = logging.getLogger(__name__)


_DISCLAIMER = (
    "Educational information only — not medical advice or diagnosis. "
    "Discuss your results with a healthcare professional before acting on them."
)


def _build_prompt(
    *,
    name: str,
    display_name: str,
    direction: str,
    unit: str | None,
    ref_low: float | None,
    ref_high: float | None,
    category: str | None,
) -> str:
    """Compose the brain prompt for one biomarker × direction."""
    rng = ""
    if ref_low is not None and ref_high is not None:
        rng = f"{ref_low}–{ref_high} {unit or ''}".strip()
    elif ref_high is not None:
        rng = f"≤ {ref_high} {unit or ''}".strip()
    elif ref_low is not None:
        rng = f"≥ {ref_low} {unit or ''}".strip()
    category_clause = f" ({category} panel)" if category else ""
    return f"""You are writing a brief educational alert for a personal health-tracking app.

The user's biomarker {display_name}{category_clause} came in **{direction}** relative to the canonical reference range{f" of {rng}" if rng else ""}.

Produce a JSON object with exactly these keys:
- "summary": one or two plain sentences explaining what a {direction} value of {display_name} can mean physiologically. No diagnosis. Lay-readable.
- "causes": an array of 3 to 5 short strings, each one a *possible* contributor (lifestyle, diet, hydration, medications, recent illness, lab variability, etc.). Use "may", "can", "common in", "associated with". Never assert.
- "mitigations": an array of 3 to 5 short strings, each one a non-prescriptive consideration (e.g. "discuss with your doctor whether retesting is warranted", "review hydration status", "review medications and supplements with your prescriber"). Never prescribe drugs or dosages.

HARD RULES — violating any of these makes the output unusable:
1. NEVER diagnose a specific disease, condition, or syndrome.
2. NEVER prescribe medications, supplements, dosages, or specific treatments.
3. NEVER quantify risk ("you have a 30% chance of…").
4. ALWAYS hedge with "may", "can", "possible", "consider", "associated with".
5. Keep each bullet under 25 words.
6. Output ONLY the JSON object. No code fences, no commentary, no leading or trailing text.
"""


def _coerce_str_list(value, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _parse_response(raw: str) -> dict | None:
    """Strict JSON parse: must have summary (str), causes (list), mitigations (list)."""
    cleaned = strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    causes = _coerce_str_list(parsed.get("causes"))
    mitigations = _coerce_str_list(parsed.get("mitigations"))
    if not causes or not mitigations:
        return None
    return {
        "summary": summary.strip(),
        "causes": causes,
        "mitigations": mitigations,
    }


def _call_brain(prompt: str, config, user_id: str = "") -> str | None:
    """Run the explainer prompt through the active brain. ``None`` on failure.

    Text-only: no tool grant, so ``sandboxed=False`` — there is no wider grant
    for a namespace to be containing, and this call has never built one. See
    :mod:`istota.health._brain_call` for the rest.
    """
    return call_health_brain(
        prompt, config, origin="health_explainer", log=logger, user_id=user_id,
        sandboxed=False, timeout_seconds=120,
    )


def get_or_generate(
    ctx: HealthContext,
    *,
    name: str,
    display_name: str,
    direction: str,
    unit: str | None,
    ref_low: float | None,
    ref_high: float | None,
    category: str | None = None,
    config=None,
) -> dict:
    """Cache-first lookup. Returns ``{name, display_name, direction,
    summary, causes, mitigations, disclaimer, source, generated_at}``.

    ``source`` is ``"cache"`` for a cache hit, ``"generated"`` when the
    brain produced fresh content, or ``"fallback"`` when the brain was
    unavailable or returned unusable output (caller still gets a usable
    payload but should expect generic copy).
    """
    if direction not in ("high", "low"):
        raise ValueError("direction must be 'high' or 'low'")

    with health_db.connect(ctx.db_path) as conn:
        cached = health_db.get_biomarker_explainer(conn, name, direction)
    if cached:
        return {
            "name": name,
            "display_name": display_name,
            "direction": direction,
            "summary": cached["summary"],
            "causes": cached["causes"],
            "mitigations": cached["mitigations"],
            "disclaimer": _DISCLAIMER,
            "source": "cache",
            "generated_at": cached["generated_at"],
        }

    prompt = _build_prompt(
        name=name, display_name=display_name, direction=direction,
        unit=unit, ref_low=ref_low, ref_high=ref_high, category=category,
    )
    raw = _call_brain(prompt, config, user_id=ctx.user_id)
    parsed = _parse_response(raw) if raw else None

    if parsed is None:
        return {
            "name": name,
            "display_name": display_name,
            "direction": direction,
            "summary": (
                f"A {direction} {display_name} reading sits outside the typical reference "
                "range. A single value isn't enough to draw conclusions — trends, recent "
                "context, and clinical correlation matter."
            ),
            "causes": [
                "Recent illness, dehydration, or stress can shift values temporarily.",
                "Medications, supplements, and recent meals can move many markers.",
                "Inter-lab and inter-assay variability is real; repeat testing helps confirm.",
            ],
            "mitigations": [
                "Discuss the result with your healthcare provider before acting on it.",
                "Consider a repeat test in a few weeks to confirm the trend.",
                "Review recent changes in medication, diet, and lifestyle for context.",
            ],
            "disclaimer": _DISCLAIMER,
            "source": "fallback",
            "generated_at": None,
        }

    with health_db.connect(ctx.db_path) as conn:
        health_db.save_biomarker_explainer(
            conn,
            name=name,
            direction=direction,
            summary=parsed["summary"],
            causes=parsed["causes"],
            mitigations=parsed["mitigations"],
        )
        conn.commit()
        stored = health_db.get_biomarker_explainer(conn, name, direction)
    return {
        "name": name,
        "display_name": display_name,
        "direction": direction,
        "summary": parsed["summary"],
        "causes": parsed["causes"],
        "mitigations": parsed["mitigations"],
        "disclaimer": _DISCLAIMER,
        "source": "generated",
        "generated_at": stored["generated_at"] if stored else None,
    }
