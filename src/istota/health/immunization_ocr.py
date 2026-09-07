"""Image / PDF → immunization rows pipeline.

Same overall shape as :mod:`istota.health.ocr` but tailored to vaccine
lists rather than lab panels:

* PDF — try ``pdftotext`` / ``pypdf`` first (most MyChart-style exports
  are text-native); fall through to a vision-mode brain call when the
  text path produces nothing usable.
* Image — vision-mode brain call (Tesseract is too unreliable on the
  styled lists that vendor portals render).

The output shape mirrors :func:`istota.health.parser.parse_paste` so
the web UI and the ``/bulk`` route can consume both code paths the
same way.
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
from istota.health.models import ImmunizationRef


logger = logging.getLogger(__name__)


# Long enough that "Page 1 of 2\n" alone doesn't trigger text mode.
_TEXT_NATIVE_MIN_CHARS = 60


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


def _build_refs_block(refs: list[ImmunizationRef]) -> str:
    lines = []
    for r in refs:
        aliases = ", ".join(r.aliases) if r.aliases else ""
        suffix = f" — also: {aliases}" if aliases else ""
        lines.append(f"- {r.name}{suffix}")
    return "\n".join(lines)


_FIELD_RULES = """For each immunization, return an object:

  {
    "name": canonical vaccine family name (match the list below where possible; use "Unknown" otherwise),
    "product_name": brand or product description as printed (e.g. "Fluzone Quadrivalent", "Janssen/J&J"),
    "date_given": ISO date YYYY-MM-DD,
    "notes": any extra qualifier the source printed (e.g. "External Administration"),
    "confidence": "high" if the date is unambiguous, "medium" if you inferred any field, "low" if you guessed
  }

Return JSON only — no prose, no fences. The top-level shape is:

  {"immunizations": [ ... ]}

Empty source → {"immunizations": []}.

Rules:
- Always include every row visible in the source, even if the family doesn't match the canonical list (set name="Unknown" and put the printed text in product_name).
- US dates (M/D/YYYY) and ISO dates both occur; output ISO.
- Two-digit years: 00–69 → 20YY, 70–99 → 19YY.
- Don't fabricate dates. If a row has no date, return date_given=null.
"""


def _build_text_prompt(text: str, refs: list[ImmunizationRef]) -> str:
    refs_str = _build_refs_block(refs)
    return f"""Extract immunization records from the text below.

{_FIELD_RULES}
Canonical vaccine families (match the user's printed text to one of these
when you can; case-insensitive):

{refs_str}

Source text (between <text> markers):

<text>
{text}
</text>
"""


def _build_vision_prompt(image_path: Path, refs: list[ImmunizationRef]) -> str:
    refs_str = _build_refs_block(refs)
    return f"""Read the immunization list at the following absolute path and
extract its rows:

{image_path}

{_FIELD_RULES}
Canonical vaccine families (match the user's printed text to one of these
when you can; case-insensitive):

{refs_str}
"""


def _coerce_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "unknown"):
        return None
    return s


def _parse_llm_response(raw: str) -> tuple[list[dict], int]:
    """Return ``(rows, dropped_future)``.

    ``dropped_future`` counts rows the model emitted with a future
    date_given — likely OCR errors or hallucinations (analog of the
    bloodwork pipeline's >10× canonical-range guard). Those rows are
    discarded rather than allowed through to the bulk endpoint.
    """
    for candidate in candidate_json_blocks(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "immunizations" in parsed:
            items = parsed["immunizations"]
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
            name = _coerce_str(item.get("name")) or "Unknown"
            date_given = parse_loose_date(item.get("date_given"))
            if is_future_date(date_given):
                dropped_future += 1
                continue
            confidence = _coerce_str(item.get("confidence")) or (
                "high" if date_given else "manual"
            )
            out.append({
                "name": name,
                "product_name": _coerce_str(item.get("product_name")),
                "date_given": date_given,
                "source_line": _coerce_str(item.get("source_line")) or "",
                "confidence": confidence,
                "notes": _coerce_str(item.get("notes")),
            })
        if out or dropped_future:
            return out, dropped_future
    return [], 0


def _call_brain(
    prompt: str, config, *, read_path: Path | None = None, user_id: str = ""
) -> str | None:
    """Run the immunization extraction prompt through the active brain.

    One line of :func:`istota.health._brain_call.call_health_brain`, which
    holds the confinement rules, the fail-closed refusal and the env narrowing
    for all four health brain callers (ISSUE-395, ISSUE-397). ``log_prefix``
    differs from ``origin`` here: the log lines this path has always written
    are ``health_imm_ocr_*``.
    """
    return call_health_brain(
        prompt, config, origin="health_immunization_ocr",
        log_prefix="health_imm_ocr", log=logger, user_id=user_id,
        read_path=read_path,
    )


def extract_from_file(
    source_path: Path,
    mime: str,
    refs: list[ImmunizationRef],
    *,
    config=None,
    user_id: str = "",
) -> dict:
    """Run text → fallback-vision extraction against an uploaded file.

    Returns ``{"rows": [...], "mode": "text"|"vision", "warnings": [...]}``.
    The ``rows`` shape matches :func:`istota.health.parser.parse_paste`
    output so the frontend can review either source through the same UI.
    """
    text = ""
    mode = "vision"
    mime_low = (mime or "").lower()
    if mime_low == "application/pdf" or source_path.suffix.lower() == ".pdf":
        text = _pdftotext(source_path) or _pypdf_extract(source_path) or ""
        if len(text.strip()) >= _TEXT_NATIVE_MIN_CHARS:
            mode = "text"

    if mode == "text":
        prompt = _build_text_prompt(text, refs)
        response = _call_brain(prompt, config, user_id=user_id)
    else:
        prompt = _build_vision_prompt(source_path, refs)
        response = _call_brain(
            prompt, config, read_path=source_path, user_id=user_id
        )

    if not response:
        return {
            "rows": [],
            "mode": mode,
            "warnings": [
                "The LLM extraction step is unavailable on this instance. "
                "Paste the list as text or add rows manually.",
            ],
        }

    rows, dropped_future = _parse_llm_response(response)
    warnings: list[str] = []
    if not rows and not dropped_future:
        warnings.append(
            "Couldn't parse any immunizations from the source. "
            "Try a clearer screenshot or paste the list as text.",
        )
    if dropped_future:
        warnings.append(
            f"{dropped_future} row(s) had a future date and were dropped — "
            "likely OCR error or hallucination. Re-upload a clearer image "
            "or add those rows manually.",
        )
    unknown = sum(1 for r in rows if r["name"] == "Unknown")
    if unknown:
        warnings.append(
            f"{unknown} row(s) didn't match a canonical vaccine family — "
            "review and pick one before importing.",
        )
    return {"rows": rows, "mode": mode, "warnings": warnings}
