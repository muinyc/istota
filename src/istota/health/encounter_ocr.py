"""Image / PDF → encounter rows pipeline.

Mirrors :mod:`istota.health.immunization_ocr` but extracts doctor's-visit
paperwork (after-visit summaries, discharge papers, referral letters) into
one or more encounter rows. A single source can carry multiple visits
(visit summary covering follow-ups), so the output is always a list.

Each row carries the encounter fields plus an optional ``diagnoses`` list
so the bulk endpoint can link them when the user confirms the import.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from istota.date_parse import is_future_date, parse_loose_date
from istota.llm_json import candidate_json_blocks


logger = logging.getLogger(__name__)


_TEXT_NATIVE_MIN_CHARS = 60

_ENCOUNTER_TYPES = (
    "visit", "procedure", "screening", "hospitalization", "er",
    "telehealth", "imaging", "dental", "other",
)


def _pdftotext(path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.stdout or None


def _pypdf_extract(path: Path) -> str | None:
    try:
        from pypdf import PdfReader  # noqa: PLC0415
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(chunks) or None
    except Exception:  # noqa: BLE001
        return None


_FIELD_RULES = """For each clinical encounter, return an object:

  {
    "encounter_date": ISO date YYYY-MM-DD (visit / admission / procedure date),
    "encounter_type": one of "visit", "procedure", "screening", "hospitalization", "er", "telehealth", "imaging", "dental", "other",
    "provider": clinician name as printed (e.g. "Dr. Jane Smith, MD") or null,
    "facility": clinic / hospital / practice name or null,
    "specialty": specialty as printed (e.g. "cardiology", "primary care") or null,
    "reason": chief complaint or reason for visit (one line) or null,
    "notes": short free-text summary — assessment, findings, plan, follow-up. Don't paste the whole document; summarise.,
    "diagnoses": array of {"name": condition name, "icd10": code or null, "status": "active"|"resolved"|"chronic", "severity": "mild"|"moderate"|"severe"|null} — empty array if none stated,
    "confidence": "high" if all required fields are unambiguous, "medium" if you inferred any field, "low" if you guessed
  }

Return JSON only — no prose, no fences. The top-level shape is:

  {"encounters": [ ... ]}

Empty source → {"encounters": []}.

Rules:
- ``encounter_date`` and ``encounter_type`` are required. If the date is genuinely missing, return date as null and confidence="low".
- US dates (M/D/YYYY) and ISO dates both occur; output ISO.
- Two-digit years: 00–69 → 20YY, 70–99 → 19YY.
- Don't fabricate fields. Use null when the source doesn't say.
- A typical after-visit summary is ONE encounter. Multi-visit packets may have several — only split when separate dates are clearly printed.
- For diagnoses, prefer the condition name as printed. Default status to "active" unless the source says "resolved" or describes a chronic / ongoing condition.
"""


def _build_text_prompt(text: str) -> str:
    return f"""Extract clinical encounters from the doctor's-visit paperwork below.

{_FIELD_RULES}
Source text (between <text> markers):

<text>
{text}
</text>
"""


def _build_vision_prompt(image_path: Path) -> str:
    return f"""Read the clinical document at the following absolute path and
extract one row per encounter:

{image_path}

{_FIELD_RULES}
"""


def _coerce_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "unknown"):
        return None
    return s


def _normalise_type(raw) -> str:
    s = _coerce_str(raw)
    if not s:
        return "visit"
    low = s.lower()
    if low in _ENCOUNTER_TYPES:
        return low
    # Common aliases.
    if "emergency" in low:
        return "er"
    if "tele" in low or "video" in low:
        return "telehealth"
    if "admit" in low or "hospital" in low or "inpatient" in low:
        return "hospitalization"
    if "screen" in low:
        return "screening"
    if "procedure" in low or "surger" in low:
        return "procedure"
    if "x-ray" in low or "mri" in low or "ct " in low or "imaging" in low:
        return "imaging"
    if "dental" in low or "dentist" in low:
        return "dental"
    return "visit"


def _normalise_diagnoses(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _coerce_str(item.get("name"))
        if not name:
            continue
        status = _coerce_str(item.get("status")) or "active"
        if status not in ("active", "resolved", "chronic"):
            status = "active"
        severity = _coerce_str(item.get("severity"))
        if severity and severity not in ("mild", "moderate", "severe"):
            severity = None
        out.append({
            "name": name,
            "icd10": _coerce_str(item.get("icd10")),
            "status": status,
            "severity": severity,
        })
    return out


def _parse_llm_response(raw: str) -> tuple[list[dict], int]:
    """Return ``(rows, dropped_future)``.

    Rows with a future date_given are dropped (likely OCR / hallucination).
    """
    for candidate in candidate_json_blocks(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "encounters" in parsed:
            items = parsed["encounters"]
        elif isinstance(parsed, list):
            items = parsed
        else:
            continue
        if not isinstance(items, list):
            continue
        out: list[dict] = []
        dropped_future = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            encounter_date = parse_loose_date(item.get("encounter_date"))
            if is_future_date(encounter_date):
                dropped_future += 1
                continue
            encounter_type = _normalise_type(item.get("encounter_type"))
            confidence = _coerce_str(item.get("confidence")) or (
                "high" if encounter_date else "low"
            )
            out.append({
                "encounter_date": encounter_date,
                "encounter_type": encounter_type,
                "provider": _coerce_str(item.get("provider")),
                "facility": _coerce_str(item.get("facility")),
                "specialty": _coerce_str(item.get("specialty")),
                "reason": _coerce_str(item.get("reason")),
                "notes": _coerce_str(item.get("notes")),
                "diagnoses": _normalise_diagnoses(item.get("diagnoses")),
                "confidence": confidence,
            })
        if out or dropped_future:
            return out, dropped_future
    return [], 0


def _call_brain(
    prompt: str, config, *, read_path: Path | None = None, user_id: str = ""
) -> str | None:
    """Run the extraction prompt through the active brain.

    ``read_path`` is the document a vision-mode prompt names by absolute path.
    Passing it is what grants the ``Read`` tool, and it is simultaneously the
    only path ``Read`` may touch — the two travel together so that granting the
    tool without a root is not expressible. ``sandbox_wrap`` is the other half:
    the Claude brains ignore those roots and take their boundary from
    bubblewrap instead. See ``health/ocr.py:_call_brain`` for the full
    reasoning (ISSUE-395, ISSUE-397).
    """
    try:
        from istota.brain import BrainRequest, make_brain  # noqa: PLC0415
    except ImportError as e:
        logger.warning("health_enc_ocr_brain_import_failed error=%s", e)
        return None
    if config is None:
        return None
    try:
        brain = make_brain(config.brain)
        model = brain.resolve_model_name("general")
    except Exception as e:  # noqa: BLE001
        logger.warning("health_enc_ocr_brain_init_failed error=%s", e)
        return None
    # Imported here rather than at module scope: `executor` imports
    # `briefings.generate`, and a top-level import from any of these callers
    # risks closing a cycle back through it.
    from istota.executor import (
        build_daemon_sandbox,
        build_model_cli_env,
        persist_brain_usage,
    )

    sandbox = build_daemon_sandbox(
        config, user_id, extra_ro_binds=[read_path] if read_path else None
    )
    if sandbox.refused and read_path:
        # Fail closed. A namespace was wanted and could not be built, and the
        # tool grant below is only safe inside one — on the Claude brains it is
        # the CLI's whole default toolset, confined by nothing else. Better no
        # extraction than an unconfined one: the caller renders "extraction
        # unavailable, add the rows by hand", which is a recoverable answer.
        logger.warning(
            "health_enc_ocr_sandbox_refused user_id=%r — not granting Read "
            "outside a namespace", user_id,
        )
        return None
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=["Read"] if read_path else [],
        cwd=sandbox.work_dir,
        # Not `dict(os.environ)`: this is a daemon-side call with no task
        # behind it, so nothing has stripped the master Fernet key, the
        # Nextcloud app password, the mail passwords or the forge tokens
        # (ISSUE-395). `build_model_cli_env` is the existing answer for a
        # daemon-side model spawn that is not a task (ISSUE-232).
        env=build_model_cli_env(config),
        fs_read_roots=[read_path] if read_path else None,
        # The Claude brains' filesystem boundary (ISSUE-397). `NativeBrain`
        # reads `native_sandbox_wrap` and not this one, and is confined by the
        # roots above instead.
        sandbox_wrap=sandbox.wrap,
        timeout_seconds=180,
        model=model,
        streaming=False,
    )
    try:
        result = brain.execute(req)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_enc_ocr_brain_failed error=%s", e)
        return None

    # One call per uploaded document, with no task row behind it.
    persist_brain_usage(
        config, None, usage=result.usage, origin="health_encounter_ocr",
        user_id=user_id, brain_kind=result.brain_kind,
        model=result.model_used or req.model,
        stop_reason=result.stop_reason, success=result.success,
    )

    if not result.success:
        logger.warning(
            "health_enc_ocr_brain_unsuccessful stop_reason=%s",
            getattr(result, "stop_reason", "?"),
        )
        return None
    return result.result_text or ""


def extract_from_file(
    source_path: Path,
    mime: str,
    *,
    config=None,
    user_id: str = "",
) -> dict:
    """Run text → fallback-vision extraction against an uploaded file.

    Returns ``{"rows": [...], "mode": "text"|"vision", "warnings": [...]}``.
    """
    text = ""
    mode = "vision"
    mime_low = (mime or "").lower()
    if mime_low == "application/pdf" or source_path.suffix.lower() == ".pdf":
        text = _pdftotext(source_path) or _pypdf_extract(source_path) or ""
        if len(text.strip()) >= _TEXT_NATIVE_MIN_CHARS:
            mode = "text"

    if mode == "text":
        prompt = _build_text_prompt(text)
        response = _call_brain(prompt, config, user_id=user_id)
    else:
        prompt = _build_vision_prompt(source_path)
        response = _call_brain(
            prompt, config, read_path=source_path, user_id=user_id
        )

    if not response:
        return {
            "rows": [],
            "mode": mode,
            "warnings": [
                "The LLM extraction step is unavailable on this instance. "
                "Add the encounter manually instead.",
            ],
        }

    rows, dropped_future = _parse_llm_response(response)
    warnings: list[str] = []
    if not rows and not dropped_future:
        warnings.append(
            "Couldn't extract any encounters from the source. "
            "Try a clearer scan or add the visit manually.",
        )
    if dropped_future:
        warnings.append(
            f"{dropped_future} row(s) had a future date and were dropped — "
            "likely OCR error or hallucination.",
        )
    missing_date = sum(1 for r in rows if not r["encounter_date"])
    if missing_date:
        warnings.append(
            f"{missing_date} row(s) are missing a date — "
            "fill it in before importing.",
        )
    return {"rows": rows, "mode": mode, "warnings": warnings}
