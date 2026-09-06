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
from istota.health._brain_call import call_health_brain


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
    """Run the encounter extraction prompt through the active brain.

    One line of :func:`istota.health._brain_call.call_health_brain`, which
    holds the confinement rules, the fail-closed refusal and the env narrowing
    for all four health brain callers (ISSUE-395, ISSUE-397). ``log_prefix``
    differs from ``origin`` here: the log lines this path has always written
    are ``health_enc_ocr_*``.
    """
    return call_health_brain(
        prompt, config, origin="health_encounter_ocr",
        log_prefix="health_enc_ocr", log=logger, user_id=user_id,
        read_path=read_path,
    )


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
