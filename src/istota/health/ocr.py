"""OCR + LLM extraction pipeline for uploaded lab reports.

Two stages:

1. **Text extraction** — input-format dispatch:
   - PDF: try ``pdftotext`` first (most modern lab PDFs are text-native;
     OCR loses precision). Fall back to rasterising via ``pdftoppm`` +
     Tesseract per page if the text extraction yields too little content.
   - Image: hand to Tesseract directly.

2. **LLM extraction** — pass the extracted text + the canonical
   ``biomarker_refs`` list (names + aliases) to the configured brain and
   ask for structured JSON. The brain returns a list of
   ``{name, value, unit, ref_range_low?, ref_range_high?, flag?}``
   objects which we sanity-check against the canonical ranges.

Best-effort: every stage falls back to ``[]`` on a missing optional
dependency (``pdftotext``, ``pytesseract``, the brain CLI). The web UI's
review step expects the user to fill in or correct the table either way,
so a partial / empty extraction is still useful.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from istota.llm_json import candidate_json_blocks
from istota.health._brain_call import call_health_brain
from istota.health import db as health_db
from istota.health.models import HealthContext, Panel


logger = logging.getLogger(__name__)


# Below this many extracted chars from text-native PDF parsing we assume
# the PDF is a scan and fall back to OCR.
_TEXT_NATIVE_MIN_CHARS = 200


def _resolve_source_file(ctx: HealthContext, panel: Panel) -> Path | None:
    if not panel.source_file:
        return None
    candidate = (ctx.uploads_dir / panel.source_file).resolve()
    try:
        candidate.relative_to(ctx.uploads_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _pdftotext(path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pypdf_extract(path: Path) -> str | None:
    try:
        import pypdf  # noqa: PLC0415
    except ImportError:
        return None
    try:
        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        return "\n\f\n".join(pages)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_pypdf_failed path=%s error=%s", path, e)
        return None


def _ocr_image(path: Path) -> str | None:
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return pytesseract.image_to_string(img)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_image_failed path=%s error=%s", path, e)
        return None


def _ocr_pdf_via_pdftoppm(path: Path) -> str | None:
    if not shutil.which("pdftoppm"):
        return None
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", str(path), str(prefix), "-png"],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
            logger.warning("health_ocr_pdftoppm_failed path=%s error=%s", path, e)
            return None
        pages: list[str] = []
        for image_path in sorted(Path(tmp).glob("page-*.png")):
            try:
                with Image.open(image_path) as img:
                    pages.append(pytesseract.image_to_string(img))
            except Exception:  # noqa: BLE001
                continue
        return "\n\f\n".join(pages) if pages else None


def extract_text(ctx: HealthContext, panel: Panel) -> str:
    """Return the best-effort extracted text for ``panel.source_file``.

    Empty string when nothing usable could be extracted. Caller should
    decide whether to surface a warning to the user.
    """
    source = _resolve_source_file(ctx, panel)
    if source is None:
        return ""
    mime = (panel.source_mime or "").lower()

    if mime.startswith("image/"):
        return _ocr_image(source) or ""

    if mime == "application/pdf" or source.suffix.lower() == ".pdf":
        text = _pdftotext(source) or _pypdf_extract(source) or ""
        if len(text.strip()) >= _TEXT_NATIVE_MIN_CHARS:
            return text
        fallback = _ocr_pdf_via_pdftoppm(source)
        return fallback or text

    return ""


def _build_refs_block(refs: list[dict]) -> str:
    """Format canonical refs without parenthesised units next to the name.

    Putting `Hemoglobin (g/dL)` in the prompt taught the model that the
    canonical *name* included the unit, no matter what the prose rules
    said — so it produced `"name": "Hemoglobin (g/dL)"` for every row.
    Keep the columns visually distinct (name | unit | aliases) so the
    model can't conflate them.
    """
    out: list[str] = []
    for r in refs:
        parts = [f"- name: {r['name']}", f"unit: {r['default_unit']}"]
        if r["aliases"]:
            parts.append("aliases: " + ", ".join(r["aliases"]))
        out.append(" | ".join(parts))
    return "\n".join(out)


# Shared per-field rules + JSON-shape contract used by both the text-mode and
# vision-mode prompts. Kept here once so we can't drift between the two paths.
_FIELD_RULES = """Respond with a single JSON object and nothing else. No prose before or after.
No code fences. No commentary. The first character of your response must be
``{`` and the last must be ``}``.

Shape:

{
  "drawn_at": "2025-11-28",
  "lab_name": "Kaiser",
  "panel_type": "CBC",
  "biomarkers": [
    {
      "name": "Hemoglobin",
      "display_name": "HGB",
      "value": 14.6,
      "unit": "g/dL",
      "ref_range_low": 13.5,
      "ref_range_high": 17.5,
      "flag": null
    }
  ]
}

Panel-level fields (at the top level, alongside ``biomarkers``):
- ``drawn_at`` (string ``YYYY-MM-DD``, required) — the date the sample was
  collected, NOT the date the report was generated or received. Look for
  phrases like "Collected", "Drawn", "Specimen received". If absent, use
  the report-date as a fallback. If no date is parseable, omit the field.
- ``lab_name`` (string, optional) — the lab or clinic that ran the test
  (e.g. "Kaiser", "Quest Diagnostics", "Labcorp"). Omit or set null when
  not clearly stated.
- ``panel_type`` (string, optional) — a short tag for the panel grouping
  (e.g. "CBC", "CMP", "Lipid Panel", "Thyroid"). Omit when the report
  doesn't name a panel or covers many.

Field rules per biomarker element:
- ``name`` (string, required) — when the report's marker matches a canonical
  name or alias below, use the canonical name VERBATIM. Otherwise use the
  name as printed on the report. NEVER append units, parentheses, or extra
  qualifiers to ``name``. Correct: ``"Hemoglobin"``. Wrong: ``"Hemoglobin
  (g/dL)"``, ``"Hematocrit (%)"``, ``"MCV (fL)"``. The unit lives in
  ``unit``, not in ``name``.
- ``display_name`` (string, optional) — the exact label from the report (e.g.
  ``HGB`` when the canonical is ``Hemoglobin``). Omit or set null if it would
  duplicate ``name``.
- ``value`` (number, required) — numeric only. Strip ``%``, ``H``, ``L``, ``*``.
- ``unit`` (string, required) — exactly as printed.
- ``ref_range_low`` / ``ref_range_high`` (number or null) — the lab's printed
  bounds. For one-sided ranges (``< 100`` or ``≥ 40``) set the missing side to
  null. Omit when no range is printed.
- ``flag`` (one of "H", "L", "C", null) — only when the lab flagged the row.

Skip:
- Header / footer text, patient demographics, clinic address, accession ids.
- Non-quantitative rows (comments, "see note", "pending").
- Rows that are duplicates of a row above (some labs reprint the same marker
  in a summary block).

Include even when the value looks impossible — the user reviews every row.

If the source contains no biomarker results at all, return
``{"biomarkers": [], "drawn_at": null, "lab_name": null, "panel_type": null}``.
"""


def _build_extraction_prompt(text: str, refs: list[dict]) -> str:
    """Prompt for text-mode extraction (OCR'd or pdftotext'd text)."""
    refs_str = _build_refs_block(refs)
    return f"""Extract structured biomarker results from the lab report text below.

{_FIELD_RULES}
Canonical biomarker names + units (match these when the report does):

{refs_str}

Lab report text (between <text> markers):

<text>
{text}
</text>
"""


def _build_vision_prompt(image_path: Path, refs: list[dict]) -> str:
    """Prompt for vision-mode extraction. The model uses the Read tool to
    load the image, then returns the same JSON shape as the text path.

    Used for image uploads and scanned PDFs where Tesseract loses the
    values (e.g. screenshots where the measured number is rendered as a
    styled pill on a colored slider).
    """
    refs_str = _build_refs_block(refs)
    return f"""Read the lab report at the following absolute path and extract its
biomarker results:

{image_path}

{_FIELD_RULES}
Canonical biomarker names + units (match these when the report does):

{refs_str}
"""


def _parse_llm_json(raw: str) -> list[dict]:
    """Back-compat shim: return just the biomarker list.

    Newer callers should use :func:`_parse_llm_response`, which returns
    the panel-level metadata alongside the biomarkers.
    """
    parsed = _parse_llm_response(raw)
    return parsed["biomarkers"]


def _parse_llm_response(raw: str) -> dict:
    """Parse the LLM response into ``{biomarkers, drawn_at, lab_name, panel_type}``.

    Falls back to an empty payload (``biomarkers=[]``, metadata fields
    None) when the response can't be coerced into the expected shape.
    """
    empty: dict = {
        "biomarkers": [],
        "drawn_at": None,
        "lab_name": None,
        "panel_type": None,
    }
    for candidate in candidate_json_blocks(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "biomarkers" in parsed:
            items = parsed["biomarkers"]
            metadata = {
                "drawn_at": _coerce_str(parsed.get("drawn_at")),
                "lab_name": _coerce_str(parsed.get("lab_name")),
                "panel_type": _coerce_str(parsed.get("panel_type")),
            }
        elif isinstance(parsed, list):
            items = parsed
            metadata = {"drawn_at": None, "lab_name": None, "panel_type": None}
        else:
            continue
        if not isinstance(items, list):
            continue
        out = [item for item in items if isinstance(item, dict)]
        if out:
            return {"biomarkers": out, **metadata}
    return empty


def _coerce_str(v) -> str | None:
    """Normalise LLM-returned strings: trim, drop empties / null sentinels."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "unknown"):
        return None
    return s


def _call_brain(
    prompt: str, config, *, read_path: Path | None = None, user_id: str = ""
) -> str | None:
    """Run the bloodwork extraction prompt through the active brain.

    One line of :func:`istota.health._brain_call.call_health_brain`, which
    holds the confinement rules, the fail-closed refusal and the env narrowing
    for all four health brain callers (ISSUE-395, ISSUE-397).
    """
    return call_health_brain(
        prompt, config, origin="health_ocr", user_id=user_id,
        read_path=read_path,
    )


def _sanity_check(biomarkers: list[dict], refs_by_name: dict[str, dict]) -> list[str]:
    """Flag obvious extraction errors against canonical ranges.

    Returns a list of human-readable warning strings; we don't mutate the
    biomarker list — the user decides whether to keep or correct each row
    in the review UI.
    """
    warnings: list[str] = []
    for b in biomarkers:
        name = str(b.get("name") or "").strip()
        try:
            val = float(b.get("value"))
        except (TypeError, ValueError):
            warnings.append(f"{name or '<unnamed>'}: non-numeric value")
            continue
        ref = refs_by_name.get(name)
        if ref is None:
            continue
        # Use the widest canonical range (unisex if present, else sex-specific
        # min/max). This is a sanity check, not the final flag computation.
        lows = [
            v for v in (
                ref.get("ref_range_low"),
                ref.get("ref_range_low_m"),
                ref.get("ref_range_low_f"),
            ) if v is not None
        ]
        highs = [
            v for v in (
                ref.get("ref_range_high"),
                ref.get("ref_range_high_m"),
                ref.get("ref_range_high_f"),
            ) if v is not None
        ]
        widest_low = min(lows) if lows else None
        widest_high = max(highs) if highs else None
        # 10x outside the widest canonical bound is almost certainly an
        # OCR/parsing error (decimal place lost, unit confused).
        if widest_high is not None and val > widest_high * 10:
            warnings.append(
                f"{name}: value {val} {b.get('unit', '')} is far above the "
                f"expected canonical range ({widest_high}). Possible OCR error.",
            )
        elif widest_low is not None and widest_low > 0 and val < widest_low / 10:
            warnings.append(
                f"{name}: value {val} {b.get('unit', '')} is far below the "
                f"expected canonical range ({widest_low}). Possible OCR error.",
            )
    return warnings


def extract_from_panel(ctx: HealthContext, panel: Panel, *, config=None) -> dict:
    """Run the full OCR + LLM pipeline for a panel.

    Returns a dict ready to send back to the web client:
    ``{biomarkers, warnings, raw_text}``. The web UI shows the extracted
    rows in an editable table next to the source preview; on confirm it
    POSTs the (possibly edited) list to ``/panels/{id}/biomarkers``.

    The ``config`` arg is only used for the brain call. When omitted, we
    pick it up off the panel's environment via the same mechanism the
    routes use; falling back to "no extraction" if unavailable.
    """
    source_path = _resolve_source_file(ctx, panel)
    mime = (panel.source_mime or "").lower()

    # Decide between text-mode and vision-mode extraction. Text-mode wins
    # whenever we can pull enough text without OCR (text-native PDFs);
    # vision-mode handles images and scanned PDFs where Tesseract loses
    # values that are rendered as styled pills, low-contrast cells, or
    # otherwise non-flowing layout.
    text = ""
    mode = "vision"
    if source_path is not None:
        if mime == "application/pdf" or source_path.suffix.lower() == ".pdf":
            text = _pdftotext(source_path) or _pypdf_extract(source_path) or ""
            if len(text.strip()) >= _TEXT_NATIVE_MIN_CHARS:
                mode = "text"

    if mode == "vision" and source_path is None:
        # No file on disk — can't do vision either.
        with health_db.connect(ctx.db_path) as conn:
            conn.execute(
                "UPDATE panels SET ocr_text = '' WHERE id = ?", (panel.id,),
            )
            conn.commit()
        return {
            "biomarkers": [],
            "warnings": [
                "Could not access the source file. Add biomarkers manually below.",
            ],
            "raw_text": "",
        }

    # Persist whatever text we have (may be empty for image uploads).
    with health_db.connect(ctx.db_path) as conn:
        conn.execute(
            "UPDATE panels SET ocr_text = ? WHERE id = ?",
            (text, panel.id),
        )
        conn.commit()
        ref_rows = health_db.list_biomarker_refs(conn)
    refs = [
        {
            "name": r.name,
            "display_name": r.display_name,
            "default_unit": r.default_unit,
            "aliases": r.aliases,
            "ref_range_low": r.ref_range_low,
            "ref_range_high": r.ref_range_high,
            "ref_range_low_m": r.ref_range_low_m,
            "ref_range_high_m": r.ref_range_high_m,
            "ref_range_low_f": r.ref_range_low_f,
            "ref_range_high_f": r.ref_range_high_f,
        }
        for r in ref_rows
    ]

    if mode == "text":
        prompt = _build_extraction_prompt(text, refs)
        response = _call_brain(prompt, config, user_id=ctx.user_id)
    else:
        prompt = _build_vision_prompt(source_path, refs)
        response = _call_brain(
            prompt, config, read_path=source_path, user_id=ctx.user_id
        )

    if not response:
        return {
            "biomarkers": [],
            "drawn_at": None,
            "lab_name": None,
            "panel_type": None,
            "warnings": [
                "The LLM extraction step is unavailable on this instance. "
                "Add biomarkers manually below.",
            ],
            "raw_text": text,
            "mode": mode,
        }

    parsed = _parse_llm_response(response)
    biomarkers = parsed["biomarkers"]
    refs_by_name = {r["name"]: r for r in refs}
    warnings = _sanity_check(biomarkers, refs_by_name)

    if not biomarkers:
        warnings.insert(
            0,
            "The LLM returned a response we couldn't parse as biomarker JSON. "
            "Add rows manually or try again.",
        )

    return {
        "biomarkers": biomarkers,
        "drawn_at": parsed["drawn_at"],
        "lab_name": parsed["lab_name"],
        "panel_type": parsed["panel_type"],
        "warnings": warnings,
        "raw_text": text,
        "raw_response": response,
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Debug entrypoint — ``python -m istota.health.ocr <file>``
# ---------------------------------------------------------------------------


def _debug_main(argv: list[str]) -> int:
    """One-shot extraction against a local image / PDF for prompt iteration.

    Doesn't touch the per-user health DB — uses an ephemeral context so the
    panel + uploads dir live in a tmp directory and get torn down on exit.
    Prints the OCR text, the raw brain response, and the parsed biomarkers
    so we can see exactly where extraction is breaking.
    """
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(
        prog="python -m istota.health.ocr",
        description="Run the health OCR + LLM extraction against a local file.",
    )
    parser.add_argument("file", help="Path to a lab image (PNG/JPG) or PDF")
    parser.add_argument(
        "--show-prompt", action="store_true",
        help="Print the full prompt sent to the brain",
    )
    parser.add_argument(
        "--show-text", action="store_true",
        help="Print the full OCR'd source text",
    )
    args = parser.parse_args(argv)

    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(f"error: file not found: {src}", flush=True)
        return 2

    from istota.config import load_config  # noqa: PLC0415
    from istota.health._migrate import ensure_initialised as _ensure  # noqa: PLC0415
    from istota.health.workspace import synthesize_health_context  # noqa: PLC0415

    config = load_config()
    with tempfile.TemporaryDirectory(prefix="health-ocr-debug-") as tmp:
        ctx = synthesize_health_context("debug", Path(tmp))
        _ensure(ctx)

        mime = "image/png" if src.suffix.lower() in (".png", ".jpg", ".jpeg") else (
            "application/pdf" if src.suffix.lower() == ".pdf" else "application/octet-stream"
        )
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-01-01", lab_name=None,
                source_mime=mime, draft=True,
            )
            conn.commit()

        target_dir = ctx.uploads_dir / str(pid)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"original{src.suffix}"
        target.write_bytes(src.read_bytes())
        rel = str(target.relative_to(ctx.uploads_dir))
        with health_db.connect(ctx.db_path) as conn:
            conn.execute("UPDATE panels SET source_file = ? WHERE id = ?", (rel, pid))
            conn.commit()
            panel = health_db.get_panel(conn, pid)
            refs = health_db.list_biomarker_refs(conn)

        print(f"file: {src}")
        print(f"mime: {mime}")

        # Run the same routing as extract_from_panel so debug output reflects
        # production behavior.
        result = extract_from_panel(ctx, panel, config=config)
        text = result.get("raw_text", "")
        response = result.get("raw_response")
        biomarkers = result.get("biomarkers", [])
        mode = result.get("mode", "?")

        print(f"\nmode: {mode}")
        print(f"\n--- source text ({len(text)} chars) ---")
        if args.show_text:
            print(text)
        else:
            preview = text[:600] + ("…" if len(text) > 600 else "")
            print(preview or "(empty — vision mode)")

        if args.show_prompt:
            refs_dicts = [
                {
                    "name": r.name, "display_name": r.display_name,
                    "default_unit": r.default_unit, "aliases": r.aliases,
                    "ref_range_low": r.ref_range_low, "ref_range_high": r.ref_range_high,
                    "ref_range_low_m": r.ref_range_low_m, "ref_range_high_m": r.ref_range_high_m,
                    "ref_range_low_f": r.ref_range_low_f, "ref_range_high_f": r.ref_range_high_f,
                }
                for r in refs
            ]
            prompt = (
                _build_extraction_prompt(text, refs_dicts) if mode == "text"
                else _build_vision_prompt(target.resolve(), refs_dicts)
            )
            print(f"\n--- prompt ({len(prompt)} chars) ---")
            print(prompt)

        print(f"\n--- brain response ({len(response) if response else 0} chars) ---")
        print(response or "(no response)")
        print("---")

        print(f"\nparsed: {len(biomarkers)} biomarkers")
        print(json.dumps(biomarkers, indent=2))
        if result.get("warnings"):
            print("\nwarnings:")
            for w in result["warnings"]:
                print(f"  - {w}")
        return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_debug_main(sys.argv[1:]))
