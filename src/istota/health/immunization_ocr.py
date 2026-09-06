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
        logger.warning("health_imm_ocr_brain_import_failed error=%s", e)
        return None
    if config is None:
        return None
    try:
        brain = make_brain(config.brain)
        model = brain.resolve_model_name("general")
    except Exception as e:  # noqa: BLE001
        logger.warning("health_imm_ocr_brain_init_failed error=%s", e)
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
            "health_imm_ocr_sandbox_refused user_id=%r — not granting Read "
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
        logger.warning("health_imm_ocr_brain_failed error=%s", e)
        return None

    # One call per uploaded document, with no task row behind it.
    persist_brain_usage(
        config, None, usage=result.usage, origin="health_immunization_ocr",
        user_id=user_id, brain_kind=result.brain_kind,
        model=result.model_used or req.model,
        stop_reason=result.stop_reason, success=result.success,
    )

    if not result.success:
        logger.warning(
            "health_imm_ocr_brain_unsuccessful stop_reason=%s",
            getattr(result, "stop_reason", "?"),
        )
        return None
    return result.result_text or ""


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
