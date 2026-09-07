"""FastAPI router for the health module.

Mounted by the host application at ``/istota/api/health``. Reads/writes the
per-user workspace SQLite. Auth, CSRF, and per-user resolution mirror
:mod:`istota.feeds.routes`: the host overrides ``require_auth`` and
``verify_origin`` via ``app.dependency_overrides`` and the istota config is
read off ``request.app.state.istota_config``.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi import File as FastAPIFile
from fastapi import Form
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from istota.health import db as health_db
from istota.health import documents as health_documents
from istota.health import garmin_sync as health_garmin_sync
from istota.health._loader import UserNotFoundError, resolve_for_user
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.serialize import (
    coverage_to_dict,
    diagnosis_to_dict,
    encounter_to_dict,
    immunization_to_dict,
    unmatched_coverage_to_dict,
)
from istota.health.units import (
    all_units_agree,
    compute_bmi,
    compute_flag,
    pick_canonical_range,
    widest_canonical_range,
)
from istota.notification_resolvers import health_panel as notification_health_panel
from istota.web_router_stubs import (  # noqa: F401
    make_get_user_context,
    require_auth,  # re-exported: `web_app.py` keys `dependency_overrides` on it
    verify_origin,
)


# ---------------------------------------------------------------------------
# Auth / CSRF — host app overrides via dependency_overrides
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


get_user_context = make_get_user_context(
    cache_attr="health_initialised_dbs",
    resolve=resolve_for_user,
    ensure=lambda ctx, cfg: ensure_initialised(ctx),
    not_found=UserNotFoundError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stat_to_dict(s) -> dict:
    return {
        "id": s.id,
        "measured_at": s.measured_at,
        "metric": s.metric,
        "value": s.value,
        "unit": s.unit,
        "source": s.source,
        "source_ref": s.source_ref,
        "notes": s.notes or "",
    }


def _panel_to_dict(p, *, biomarker_count: int = 0, flagged_count: int = 0) -> dict:
    return {
        "id": p.id,
        "drawn_at": p.drawn_at,
        "lab_name": p.lab_name,
        "panel_type": p.panel_type,
        "biomarker_count": biomarker_count,
        "flagged_count": flagged_count,
        "draft": p.draft,
        "notes": p.notes,
        "has_source": bool(p.source_file),
        "encounter_id": p.encounter_id,
    }


def _encounter_to_dict(e) -> dict:
    return encounter_to_dict(e, include_created_at=True)


def _diagnosis_to_dict(d, encounter_ids: list[int] | None = None) -> dict:
    return diagnosis_to_dict(d, encounter_ids, include_created_at=True)



def _coerce_encounter_ids(raw) -> list[int]:
    """Parse an `encounter_ids` payload field. Raises ValueError on junk."""
    if not isinstance(raw, list):
        raise ValueError("encounter_ids must be a list of integers")
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError("encounter_ids must be a list of integers")
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise ValueError("encounter_ids must be a list of integers") from None
    return list(dict.fromkeys(out))


def _missing_encounter(conn, encounter_ids: list[int]) -> int | None:
    """First id in the list that names no encounter, if any."""
    for eid in encounter_ids:
        if health_db.get_encounter(conn, eid) is None:
            return eid
    return None


_ENCOUNTER_TYPES = {
    "visit", "procedure", "screening", "hospitalization", "er",
    "telehealth", "imaging", "dental", "other",
}

_DIAGNOSIS_STATUSES = {"active", "resolved", "chronic"}


def _biomarker_to_dict(b) -> dict:
    return {
        "id": b.id,
        "panel_id": b.panel_id,
        "name": b.name,
        "display_name": b.display_name,
        "value": b.value,
        "unit": b.unit,
        "ref_range_low": b.ref_range_low,
        "ref_range_high": b.ref_range_high,
        "flag": b.flag,
    }


def _document_to_dict(doc, *, links: list[dict] | None = None) -> dict:
    """Client-facing shape. Never emits ``stored_path`` — the browser gets a
    route to stream from, not a filesystem path."""
    out = {
        "id": doc.id,
        "filename": doc.filename,
        "original_filename": doc.original_filename,
        "mime": doc.mime,
        "byte_size": doc.byte_size,
        "source": doc.source,
        "notes": doc.notes,
        "created_at": doc.created_at,
        "url": f"/istota/api/health/documents/{doc.id}/file",
    }
    if links is not None:
        out["links"] = links
    return out


def _max_document_bytes(request: Request) -> int:
    cfg = getattr(request.app.state, "istota_config", None)
    health_cfg = getattr(cfg, "health", None) if cfg is not None else None
    raw = getattr(health_cfg, "max_document_bytes", None)
    if raw is None:
        return health_documents.DEFAULT_MAX_DOCUMENT_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return health_documents.DEFAULT_MAX_DOCUMENT_BYTES


def _entity_exists(conn, entity_type: str, entity_id: int) -> bool:
    """Thin shim over the DB helper the agent paths also use, so the web and
    the agent can't drift on what counts as a valid attach target."""
    try:
        return health_db.entity_exists(conn, entity_type, entity_id)
    except ValueError:
        return False


def _format_entity_label(entity_type: str, entity_id: int, record) -> str:
    """Human string for "what is this attached to".

    Takes the already-fetched record so the per-document and bulk readers
    below share one spelling of every label; ``record is None`` is a link that
    outlived its entity, which is possible because ``document_links.entity_id``
    carries no FK.
    """
    if record is not None:
        if entity_type == "encounter":
            return f"{record.encounter_date} — {record.encounter_type}"
        if entity_type == "diagnosis":
            return record.name
        if entity_type == "immunization":
            return f"{record.name} ({record.date_given})"
    return f"{entity_type} {entity_id}"


def _entity_label(conn, entity_type: str, entity_id: int) -> str:
    """Human string for "what else is this attached to"."""
    getter = {
        "encounter": health_db.get_encounter,
        "diagnosis": health_db.get_diagnosis,
        "immunization": health_db.get_immunization,
    }.get(entity_type)
    record = getter(conn, entity_id) if getter else None
    return _format_entity_label(entity_type, entity_id, record)


async def _attach_import_document(
    ctx: HealthContext,
    result,
    *,
    raw: bytes,
    filename: str,
    mime: str | None,
    max_bytes: int,
) -> dict:
    """Persist an extract-route upload and stamp ``document_id`` on its payload.

    A storage failure must not cost the user the extraction they just waited
    for — losing the file is bad, losing both is worse. So the rows come back
    with ``document_id: null`` and a warning appended instead.
    """
    if not isinstance(result, dict):
        return result

    ocr_text = result.get("ocr_text") or result.get("text") or None

    def _store():
        with health_db.connect(ctx.db_path) as conn:
            doc, _created = health_documents.store_document(
                conn, ctx, raw=raw, filename=filename, mime=mime,
                source="import", ocr_text=ocr_text, max_bytes=max_bytes,
            )
            conn.commit()
        return doc

    try:
        doc = await asyncio.to_thread(_store)
    except (health_documents.DocumentError, OSError, sqlite3.Error) as e:
        logger.error(
            "health_import_document_store_failed user=%s filename=%s error=%s",
            ctx.user_id, filename, e,
        )
        warnings = list(result.get("warnings") or [])
        warnings.append(f"The uploaded file could not be kept: {e}")
        return {**result, "document_id": None, "warnings": warnings}
    return {**result, "document_id": doc.id}


def _links_payload(conn, document_id: int) -> list[dict]:
    return [
        {
            "entity_type": t,
            "entity_id": eid,
            "label": _entity_label(conn, t, eid),
        }
        for t, eid in health_db.entity_links_for_document(conn, document_id)
    ]


def _documents_payload(conn, docs) -> list[dict]:
    """Serialize a list of documents with their links resolved (ISSUE-423).

    The Documents view renders what every document is attached to, and the
    per-row readers would make that one links query plus one label query per
    link. This is bounded instead: one query for the links, then one per
    entity type present, whatever the row count.
    """
    links_by_doc = health_db.entity_links_for_documents(
        conn, [d.id for d in docs],
    )
    wanted: dict[str, set[int]] = {}
    for pairs in links_by_doc.values():
        for entity_type, entity_id in pairs:
            wanted.setdefault(entity_type, set()).add(entity_id)
    # `document_links.entity_type` is a plain TEXT column with no CHECK, so a
    # value outside DOCUMENT_ENTITY_TYPES is storable even though every writer
    # validates. Skip it rather than letting `entities_by_id` raise: the
    # per-document reader degrades to a generic label for such a row, and this
    # is the one page that shows a document no other page does — failing the
    # whole listing over one bad link would hide every good one with it.
    records = {
        entity_type: health_db.entities_by_id(conn, entity_type, ids)
        for entity_type, ids in wanted.items()
        if entity_type in health_db.DOCUMENT_ENTITY_TYPES
    }
    return [
        _document_to_dict(
            d,
            links=[
                {
                    "entity_type": t,
                    "entity_id": eid,
                    "label": _format_entity_label(
                        t, eid, records.get(t, {}).get(eid),
                    ),
                }
                for t, eid in links_by_doc.get(d.id, ())
            ],
        )
        for d in docs
    ]


_VALID_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _validate_metric(m: str) -> str | None:
    if not isinstance(m, str) or not _VALID_METRIC.match(m):
        return "metric must be a lowercase identifier (snake_case)"
    return None


def _settings_with_defaults(stored: dict) -> dict:
    display = stored.get("display_units") or {}
    return {
        "dob": stored.get("dob"),
        "height_cm": stored.get("height_cm"),
        "sex": stored.get("sex"),
        "display_units": {
            "weight": display.get("weight", "kg"),
            "height": display.get("height", "cm"),
            "temp": display.get("temp", "C"),
        },
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


# ---- Stats ----------------------------------------------------------------


@router.get("/stats")
async def api_list_stats(
    ctx: HealthContext = Depends(get_user_context),
    metric: str = Query(default=""),
    since: str = Query(default=""),
    until: str = Query(default=""),
    limit: int = Query(default=200, le=1000, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_stats(
                conn,
                metric=metric or None,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
            )

    rows = await asyncio.to_thread(_query)
    return {"stats": [_stat_to_dict(s) for s in rows]}


@router.post("/stats")
async def api_create_stat(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    metric = body.get("metric")
    err = _validate_metric(metric or "")
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        value = float(body["value"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "value must be a number"}, status_code=400)
    unit = body.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        return JSONResponse({"error": "unit is required"}, status_code=400)

    measured_at = body.get("measured_at") or _now()
    source = body.get("source") or "manual"
    notes = body.get("notes")

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            sid = health_db.insert_stat(
                conn,
                metric=metric,
                value=value,
                unit=unit,
                measured_at=measured_at,
                source=source,
                notes=notes,
            )
            conn.commit()
        return sid

    sid = await asyncio.to_thread(_insert)
    return {"status": "ok", "id": sid}


@router.delete("/stats/{stat_id}")
async def api_delete_stat(
    stat_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_stat(conn, stat_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "stat not found")
    return {"status": "ok"}


@router.get("/stats/latest")
async def api_stats_latest(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.latest_stats(conn)

    latest = await asyncio.to_thread(_query)
    return {
        "stats": {metric: _stat_to_dict(s) for metric, s in latest.items()},
    }


@router.get("/stats/series")
async def api_stats_series(
    ctx: HealthContext = Depends(get_user_context),
    metric: str = Query(...),
    since: str = Query(default=""),
    until: str = Query(default=""),
):
    err = _validate_metric(metric)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_stats(
                conn,
                metric=metric,
                since=since or None,
                until=until or None,
                limit=5000,
            )

    rows = await asyncio.to_thread(_query)
    rows_sorted = sorted(rows, key=lambda r: r.measured_at)
    return {
        "metric": metric,
        "points": [
            {"measured_at": r.measured_at, "value": r.value, "unit": r.unit}
            for r in rows_sorted
        ],
    }


# ---- Panels ---------------------------------------------------------------


@router.get("/panels")
async def api_list_panels(
    ctx: HealthContext = Depends(get_user_context),
    since: str = Query(default=""),
    until: str = Query(default=""),
    include_drafts: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(
                conn,
                since=since or None,
                until=until or None,
                include_drafts=bool(include_drafts),
                limit=limit,
                offset=offset,
            )
            out = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                out.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            return out

    panels = await asyncio.to_thread(_query)
    return {"panels": panels}


@router.post("/panels")
async def api_create_panel(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    drawn_at = body.get("drawn_at")
    if not isinstance(drawn_at, str) or not drawn_at.strip():
        return JSONResponse({"error": "drawn_at is required"}, status_code=400)
    lab_name = body.get("lab_name") or None
    panel_type = body.get("panel_type") or None
    notes = body.get("notes")
    encounter_id = body.get("encounter_id")
    if encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer"}, status_code=400,
            )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            if encounter_id is not None and health_db.get_encounter(
                conn, encounter_id,
            ) is None:
                return None, "encounter not found"
            collision = health_db.find_panel_collision(
                conn, drawn_at=drawn_at, lab_name=lab_name,
            )
            pid = health_db.insert_panel(
                conn,
                drawn_at=drawn_at,
                lab_name=lab_name,
                panel_type=panel_type,
                notes=notes,
                encounter_id=encounter_id,
            )
            conn.commit()
        return pid, collision

    pid, collision = await asyncio.to_thread(_insert)
    if pid is None and isinstance(collision, str):
        return JSONResponse({"error": collision}, status_code=400)
    payload = {"status": "ok", "id": pid}
    if collision is not None:
        payload["collision"] = {
            "existing_id": collision.id,
            "drawn_at": collision.drawn_at,
            "lab_name": collision.lab_name,
        }
    return payload


@router.get("/panels/{panel_id}")
async def api_get_panel(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            biomarkers = health_db.list_biomarkers_for_panel(conn, panel_id)
            total, flagged = health_db.panel_counts(conn, panel_id)
            return panel, biomarkers, total, flagged

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "panel not found")
    panel, biomarkers, total, flagged = result
    return {
        "panel": _panel_to_dict(
            panel, biomarker_count=total, flagged_count=flagged,
        ),
        "biomarkers": [_biomarker_to_dict(b) for b in biomarkers],
        "source": {
            "available": bool(panel.source_file),
            "mime": panel.source_mime,
        },
    }


@router.put("/panels/{panel_id}")
async def api_update_panel(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    drawn_at = body.get("drawn_at")
    lab_name = body.get("lab_name")
    panel_type = body.get("panel_type")
    notes = body.get("notes")
    draft = body.get("draft")
    if draft is not None and not isinstance(draft, bool):
        return JSONResponse(
            {"error": "draft must be a boolean"}, status_code=400,
        )
    has_encounter_id = "encounter_id" in body
    encounter_id = body.get("encounter_id")
    if has_encounter_id and encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer or null"},
                status_code=400,
            )

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            if (
                has_encounter_id
                and encounter_id is not None
                and health_db.get_encounter(conn, encounter_id) is None
            ):
                return "encounter_not_found"
            kwargs: dict = {
                "drawn_at": drawn_at,
                "lab_name": lab_name,
                "panel_type": panel_type,
                "notes": notes,
                "draft": draft,
            }
            if has_encounter_id:
                kwargs["encounter_id"] = encounter_id
            n = health_db.update_panel(conn, panel_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if n == "encounter_not_found":
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    if not n:
        raise HTTPException(404, "panel not found")
    return {"status": "ok"}


@router.delete("/panels/{panel_id}")
async def api_delete_panel(
    panel_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    panel_dir = ctx.uploads_dir / str(panel_id)

    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return False
            # Drop derived stats first (source='lab_panel', source_ref=panel_id).
            health_db.delete_stats_for_panel(conn, panel_id)
            health_db.delete_panel(conn, panel_id)  # CASCADE -> biomarkers
            conn.commit()
        # On-disk uploads — best effort.
        if panel_dir.exists():
            try:
                shutil.rmtree(panel_dir)
            except OSError:
                pass
        return True

    ok = await asyncio.to_thread(_delete)
    if not ok:
        raise HTTPException(404, "panel not found")
    return {"status": "ok"}


@router.post("/panels/{panel_id}/biomarkers")
async def api_replace_biomarkers(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    biomarkers = body.get("biomarkers")
    if not isinstance(biomarkers, list):
        return JSONResponse(
            {"error": "biomarkers must be a list"}, status_code=400,
        )
    for b in biomarkers:
        if not isinstance(b, dict):
            return JSONResponse(
                {"error": "each biomarker must be an object"}, status_code=400,
            )
        if "name" not in b or "value" not in b or "unit" not in b:
            return JSONResponse(
                {"error": "name, value, unit are required"}, status_code=400,
            )

    confirm = bool(body.get("confirm"))

    def _save():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            # Auto-fill ranges + flags from canonical refs where missing.
            settings = health_db.get_settings(conn)
            sex = settings.get("sex")
            enriched: list[dict] = []
            for b in biomarkers:
                ref = health_db.find_biomarker_ref_by_alias(conn, str(b["name"]))
                low = b.get("ref_range_low")
                high = b.get("ref_range_high")
                canonical_low = canonical_high = None
                if ref is not None:
                    canonical_low, canonical_high = pick_canonical_range(ref, sex)
                # Flag against canonical ranges (preferred) when available,
                # falling back to lab-printed range. ``C`` from the lab is
                # preserved.
                flag_low = canonical_low if canonical_low is not None else low
                flag_high = canonical_high if canonical_high is not None else high
                computed_flag = compute_flag(
                    float(b["value"]),
                    low=flag_low,
                    high=flag_high,
                    lab_flag=b.get("flag"),
                )
                enriched.append({
                    "name": ref.name if ref else str(b["name"]),
                    "display_name": b.get("display_name") or (
                        ref.display_name if ref else None
                    ),
                    "value": float(b["value"]),
                    "unit": str(b["unit"]),
                    "ref_range_low": low,
                    "ref_range_high": high,
                    "flag": computed_flag,
                })
            n = health_db.replace_biomarkers(conn, panel_id, enriched)
            # BP / resting-HR fan-out: also write stats rows so the
            # unified time series picks them up.
            _stat_fanout = {
                "blood_pressure_systolic": ("BP_Systolic", "mmHg"),
                "blood_pressure_diastolic": ("BP_Diastolic", "mmHg"),
                "resting_hr": ("Resting_HR", "bpm"),
            }
            # Clear previous fan-out for this panel before re-creating.
            health_db.delete_stats_for_panel(conn, panel_id)
            name_to_metric = {v[0].lower(): (k, v[1]) for k, v in _stat_fanout.items()}
            for b in enriched:
                hit = name_to_metric.get(b["name"].lower())
                if not hit:
                    continue
                metric_key, default_unit = hit
                health_db.insert_stat(
                    conn,
                    metric=metric_key,
                    value=b["value"],
                    unit=b["unit"] or default_unit,
                    measured_at=panel.drawn_at,
                    source="lab_panel",
                    source_ref=panel_id,
                )
            if confirm:
                health_db.update_panel(conn, panel_id, draft=False)
            conn.commit()
        return n

    n = await asyncio.to_thread(_save)
    if n is None:
        raise HTTPException(404, "panel not found")

    if confirm and ctx.framework_db_path is not None:
        # The panel just left `draft`, which is the state the inbox row watches.
        # Framework DB and session user, for the reason given on the upload path.
        await asyncio.to_thread(
            notification_health_panel.close_for_panel,
            ctx.framework_db_path, ctx.user_id, panel_id, by="web",
        )

    return {"status": "ok", "count": n}


@router.get("/panels/{panel_id}/biomarkers")
async def api_list_panel_biomarkers(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            return health_db.list_biomarkers_for_panel(conn, panel_id)

    rows = await asyncio.to_thread(_query)
    if rows is None:
        raise HTTPException(404, "panel not found")
    return {"biomarkers": [_biomarker_to_dict(b) for b in rows]}


@router.post("/panels/upload")
async def api_panel_upload(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    drawn_at: str = Form(""),
    lab_name: str = Form(""),
    panel_type: str = Form(""),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Upload a lab result image/PDF.

    Creates a panel row with ``draft=1`` and saves the source file to
    ``{uploads_dir}/{panel_id}/original.{ext}``. The OCR + LLM extraction
    is triggered asynchronously via the ``run_ocr`` flag returned to the
    client; the frontend POSTs to ``/panels/{id}/extract`` next.

    Returns the new panel id and a collision-info object when a panel with
    the same ``(drawn_at, lab_name)`` already exists.
    """
    if not drawn_at:
        drawn_at = datetime.now(timezone.utc).date().isoformat()

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    suffix = Path(file.filename or "").suffix or mimetypes.guess_extension(mime) or ""

    def _save_and_record():
        with health_db.connect(ctx.db_path) as conn:
            collision = health_db.find_panel_collision(
                conn, drawn_at=drawn_at, lab_name=lab_name or None,
            )
            pid = health_db.insert_panel(
                conn,
                drawn_at=drawn_at,
                lab_name=lab_name or None,
                panel_type=panel_type or None,
                source_mime=mime,
                draft=True,
            )
            conn.commit()
        panel_dir = ctx.uploads_dir / str(pid)
        panel_dir.mkdir(parents=True, exist_ok=True)
        target = panel_dir / f"original{suffix}"
        target.write_bytes(raw)
        rel = str(target.relative_to(ctx.uploads_dir))
        with health_db.connect(ctx.db_path) as conn:
            health_db.update_panel(conn, pid, notes=None)  # placeholder for future
            conn.execute(
                "UPDATE panels SET source_file = ? WHERE id = ?",
                (rel, pid),
            )
            conn.commit()
        return pid, collision

    pid, collision = await asyncio.to_thread(_save_and_record)

    # The panel is a draft, and a draft is excluded from the dashboard *and*
    # from the trends — so an upload whose review is never finished is data the
    # user cannot see again unless they think to ask for drafts by hand. The row
    # goes in the **framework** DB (`ctx.framework_db_path`), never
    # `ctx.db_path`: that is this user's health module DB, and every user has a
    # panel `12` in theirs. Written, not delivered — see `write_for_panel`.
    if ctx.framework_db_path is not None:
        await asyncio.to_thread(
            notification_health_panel.write_for_panel,
            ctx.framework_db_path, ctx.user_id,
            panel_id=pid, drawn_at=drawn_at, lab_name=lab_name or None,
        )

    out = {"status": "ok", "id": pid, "draft": True}
    if collision is not None:
        out["collision"] = {
            "existing_id": collision.id,
            "drawn_at": collision.drawn_at,
            "lab_name": collision.lab_name,
        }
    return out


@router.post("/panels/{panel_id}/extract")
async def api_panel_extract(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Run the OCR + LLM extraction pipeline on an uploaded panel source.

    Synchronous; expected to complete in seconds for typical lab PDFs.
    Returns the extracted biomarkers in an editable shape; the client
    POSTs them back to ``/panels/{id}/biomarkers`` with ``confirm: true``.
    """
    from istota.health.ocr import extract_from_panel

    config = getattr(request.app.state, "istota_config", None)

    def _extract():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
        return extract_from_panel(ctx, panel, config=config)

    result = await asyncio.to_thread(_extract)
    if result is None:
        raise HTTPException(404, "panel not found")
    return result


@router.get("/panels/{panel_id}/source")
async def api_panel_source(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    """Stream the original uploaded image/PDF.

    Auth-gated. The path is resolved server-side from the panel row's
    ``source_file`` column — clients never get a raw filesystem path.
    """
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_panel(conn, panel_id)

    panel = await asyncio.to_thread(_query)
    if not panel:
        raise HTTPException(404, "panel not found")
    if not panel.source_file:
        raise HTTPException(404, "no source file")
    candidate = (ctx.uploads_dir / panel.source_file).resolve()
    uploads_root = ctx.uploads_dir.resolve()
    try:
        candidate.relative_to(uploads_root)
    except ValueError:
        raise HTTPException(400, "invalid source path")
    if not candidate.is_file():
        raise HTTPException(404, "source file missing")
    return FileResponse(
        candidate,
        media_type=panel.source_mime or "application/octet-stream",
    )


# ---- Biomarker trends -----------------------------------------------------


@router.get("/biomarkers/trend")
async def api_biomarker_trend(
    ctx: HealthContext = Depends(get_user_context),
    name: str = Query(...),
    since: str = Query(default=""),
    until: str = Query(default=""),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, name)
            canonical_name = ref.name if ref else name
            trend = health_db.biomarker_trend(
                conn,
                name=canonical_name,
                since=since or None,
                until=until or None,
            )
            settings = health_db.get_settings(conn)
            sex = settings.get("sex")
        return ref, canonical_name, trend, sex

    ref, canonical_name, trend, sex = await asyncio.to_thread(_query)
    points = [
        {
            "drawn_at": drawn_at,
            "value": b.value,
            "unit": b.unit,
            "flag": b.flag,
        }
        for b, drawn_at in trend
    ]
    units = [p["unit"] for p in points]
    canonical_low = canonical_high = None
    canonical_unit = None
    if ref is not None:
        canonical_low, canonical_high = pick_canonical_range(ref, sex)
        canonical_unit = ref.default_unit
    return {
        "name": canonical_name,
        "display_name": ref.display_name if ref else canonical_name,
        "points": points,
        "unit_mismatch": not all_units_agree(units) if points else False,
        "ref_range_low": canonical_low,
        "ref_range_high": canonical_high,
        "unit": canonical_unit,
    }


@router.get("/biomarkers/summary")
async def api_biomarker_summary(
    ctx: HealthContext = Depends(get_user_context),
):
    """Latest biomarker per name with rudimentary trend direction."""
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            rows = conn.execute(
                """
                SELECT b.*, p.drawn_at AS drawn_at FROM biomarkers b
                JOIN panels p ON p.id = b.panel_id
                WHERE p.draft = 0
                ORDER BY p.drawn_at ASC, b.id ASC
                """
            ).fetchall()
        by_name: dict[str, list[dict]] = {}
        for r in rows:
            by_name.setdefault(r["name"], []).append({
                "drawn_at": r["drawn_at"],
                "value": float(r["value"]),
                "unit": r["unit"],
                "flag": r["flag"],
            })
        out: list[dict] = []
        for name, vs in by_name.items():
            latest = vs[-1]
            prev = vs[-2] if len(vs) >= 2 else None
            direction = "flat"
            if prev:
                if latest["value"] > prev["value"] * 1.01:
                    direction = "up"
                elif latest["value"] < prev["value"] * 0.99:
                    direction = "down"
            out.append({
                "name": name,
                "latest": latest,
                "previous": prev,
                "direction": direction,
                "sample_count": len(vs),
            })
        out.sort(key=lambda x: x["name"].lower())
        return out

    summary = await asyncio.to_thread(_query)
    return {"summary": summary}


@router.get("/bloodwork/matrix")
async def api_bloodwork_matrix(
    ctx: HealthContext = Depends(get_user_context),
):
    """Spreadsheet view of every biomarker × every confirmed panel.

    Returns a structure suitable for a Date-rows / marker-columns table
    grouped by category, with the reference range pinned per column.

    ``panels`` is sorted by ``drawn_at`` ascending (oldest first), matching
    the "lab journal" layout people use offline. ``categories`` preserves
    a stable ordering from the bundled refs; markers not in the refs fall
    into an ``Other`` bucket.
    """
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(
                conn, include_drafts=False, limit=500,
            )
            panels_sorted = sorted(panels, key=lambda p: p.drawn_at)

            marker_meta: dict[str, dict] = {}
            values: dict[int, dict] = {}
            for p in panels_sorted:
                bs = health_db.list_biomarkers_for_panel(conn, p.id)
                values[p.id] = {}
                for b in bs:
                    marker_meta.setdefault(
                        b.name,
                        {"display_name": b.display_name, "unit": b.unit},
                    )
                    values[p.id][b.name] = {
                        "value": b.value,
                        "unit": b.unit,
                        "flag": b.flag,
                    }

            refs = health_db.list_biomarker_refs(conn)
            settings = health_db.get_settings(conn)
        return panels_sorted, marker_meta, values, refs, settings

    panels_sorted, marker_meta, values, refs, settings = await asyncio.to_thread(_query)

    ref_by_name = {r.name: r for r in refs}
    sex = settings.get("sex")

    # Build category buckets in the order categories first appear in refs;
    # anything unknown lands in "Other".
    cat_order: list[str] = []
    cat_markers: dict[str, list[dict]] = {}
    for r in refs:
        if r.category not in cat_markers:
            cat_order.append(r.category)
            cat_markers[r.category] = []

    for name, meta in marker_meta.items():
        ref = ref_by_name.get(name)
        cat = ref.category if ref else "Other"
        if cat not in cat_markers:
            cat_order.append(cat)
            cat_markers[cat] = []
        low = high = None
        if ref is not None:
            if sex:
                low, high = pick_canonical_range(ref, sex)
            else:
                low, high = widest_canonical_range(ref)
        cat_markers[cat].append({
            "name": name,
            "display_name": (
                (ref.display_name if ref else None)
                or meta.get("display_name")
                or name
            ),
            "unit": (
                (ref.default_unit if ref else None) or meta.get("unit") or ""
            ),
            "ref_range_low": low,
            "ref_range_high": high,
            "category": cat,
        })

    # Prune empty categories (refs whose markers nobody has measured).
    cat_order = [c for c in cat_order if cat_markers.get(c)]
    for cat in cat_markers:
        cat_markers[cat].sort(key=lambda m: m["display_name"].lower())

    return {
        "categories": [
            {"name": c, "markers": cat_markers[c]} for c in cat_order
        ],
        "panels": [
            {
                "id": p.id,
                "drawn_at": p.drawn_at,
                "lab_name": p.lab_name,
                "panel_type": p.panel_type,
            }
            for p in panels_sorted
        ],
        "values": {str(pid): vs for pid, vs in values.items()},
    }


@router.get("/biomarkers/{name}/explainer")
async def api_biomarker_explainer(
    name: str,
    request: Request,
    ctx: HealthContext = Depends(get_user_context),
    direction: str = Query(...),
):
    """Cached, brain-generated educational alert for an out-of-range value.

    ``direction`` must be ``"high"`` or ``"low"``. Returns a non-diagnostic
    summary + plausible causes + general considerations + a fixed
    disclaimer. Repeat calls for the same ``(name, direction)`` are served
    from the user's cache.
    """
    if direction not in ("high", "low"):
        return JSONResponse(
            {"error": "direction must be 'high' or 'low'"}, status_code=400,
        )

    config = getattr(request.app.state, "istota_config", None)

    def _resolve():
        from istota.health.explainer import get_or_generate

        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, name)
            settings = health_db.get_settings(conn)
        sex = settings.get("sex")
        canonical = ref.name if ref else name
        display_name = ref.display_name if ref else name
        unit = ref.default_unit if ref else None
        low = high = None
        if ref is not None:
            if sex:
                low, high = pick_canonical_range(ref, sex)
            else:
                low, high = widest_canonical_range(ref)
        return get_or_generate(
            ctx,
            name=canonical,
            display_name=display_name,
            direction=direction,
            unit=unit,
            ref_low=low,
            ref_high=high,
            category=ref.category if ref else None,
            config=config,
        )

    return await asyncio.to_thread(_resolve)


@router.get("/biomarkers/refs")
async def api_biomarker_refs(
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_biomarker_refs(conn)
        out = []
        for r in refs:
            out.append({
                "name": r.name,
                "display_name": r.display_name,
                "category": r.category,
                "default_unit": r.default_unit,
                "ref_range_low": r.ref_range_low,
                "ref_range_high": r.ref_range_high,
                "ref_range_low_m": r.ref_range_low_m,
                "ref_range_high_m": r.ref_range_high_m,
                "ref_range_low_f": r.ref_range_low_f,
                "ref_range_high_f": r.ref_range_high_f,
                "aliases": r.aliases,
                "description": r.description,
            })
        return out

    refs = await asyncio.to_thread(_query)
    return {"refs": refs}


# ---- CSV import / export --------------------------------------------------


@router.post("/csv/import")
async def api_csv_import(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Import a bloodwork CSV.

    Accepts the same shape exported by ``GET /csv/export`` (category
    banner row + ``Marker (unit)`` headers + reference-range row +
    data rows). Aliases are resolved against ``biomarker_refs`` so
    column names like ``Hgb`` / ``LDL-C`` land on canonical markers.

    Dedup is content-based: identical biomarker sets are silently
    skipped; a same-date / same-lab collision with different content
    lands as a draft for user review. No user-facing choice.
    """
    from istota.health.csv_io import import_csv

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            csv_text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return JSONResponse(
                {"error": "could not decode file as UTF-8 or latin-1"},
                status_code=400,
            )

    def _import():
        with health_db.connect(ctx.db_path) as conn:
            summary = import_csv(conn, csv_text)
            conn.commit()
        return summary

    summary = await asyncio.to_thread(_import)
    return {
        "status": "ok",
        "panels_created": summary.panels_created,
        "panels_skipped_identical": summary.panels_skipped_identical,
        "panels_needs_review": summary.panels_needs_review,
        "biomarkers_created": summary.biomarkers_created,
        "rows_processed": summary.rows_processed,
        "warnings": summary.warnings,
    }


@router.get("/csv/export")
async def api_csv_export(ctx: HealthContext = Depends(get_user_context)):
    """Stream every confirmed panel as a CSV in the import format."""
    from istota.health.csv_io import export_csv

    def _export():
        with health_db.connect(ctx.db_path) as conn:
            return export_csv(conn)

    text = await asyncio.to_thread(_export)
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="bloodwork.csv"',
        },
    )


# ---- Settings -------------------------------------------------------------


@router.get("/settings")
async def api_get_settings(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_settings(conn)

    stored = await asyncio.to_thread(_query)
    return {"settings": _settings_with_defaults(stored)}


@router.put("/settings")
async def api_put_settings(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)

    valid_keys = set(health_db.SETTINGS_KEYS)

    def _save():
        with health_db.connect(ctx.db_path) as conn:
            for k, v in body.items():
                if k not in valid_keys:
                    continue
                if k == "sex" and v not in (None, "M", "F", ""):
                    raise ValueError("sex must be 'M', 'F', or null")
                if k == "height_cm" and v is not None:
                    try:
                        float(v)
                    except (TypeError, ValueError):
                        raise ValueError("height_cm must be a number")
                if v in (None, ""):
                    health_db.delete_setting(conn, k)
                else:
                    health_db.set_setting(conn, k, v)
            conn.commit()
            return health_db.get_settings(conn)

    try:
        stored = await asyncio.to_thread(_save)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "settings": _settings_with_defaults(stored)}


# ---- Dashboard ------------------------------------------------------------


@router.get("/dashboard")
async def api_dashboard(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            latest = health_db.latest_stats(conn)
            panels = health_db.list_panels(
                conn, include_drafts=False, limit=3,
            )
            panel_dicts = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                panel_dicts.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            alerts_rows = health_db.flagged_biomarkers_latest(conn, limit=20)
            settings = health_db.get_settings(conn)
            active_diag = health_db.list_diagnoses(
                conn, status="active", limit=500,
            )
            chronic_diag = health_db.list_diagnoses(
                conn, status="chronic", limit=500,
            )
            recent_encounters = health_db.list_encounters(conn, limit=3)
        # BMI is derived from latest weight + settings height.
        bmi: float | None = None
        weight = latest.get("weight")
        height_cm = settings.get("height_cm")
        if weight and height_cm:
            try:
                bmi = compute_bmi(weight.value, float(height_cm))
            except (TypeError, ValueError):
                bmi = None
        alerts = []
        for b, p in alerts_rows:
            d = _biomarker_to_dict(b)
            d["panel_id"] = p.id
            d["drawn_at"] = p.drawn_at
            d["lab_name"] = p.lab_name
            alerts.append(d)
        return {
            "latest_stats": {
                metric: _stat_to_dict(s) for metric, s in latest.items()
            },
            "bmi": bmi,
            "recent_panels": panel_dicts,
            "alerts": alerts,
            "settings": _settings_with_defaults(settings),
            "active_diagnoses_count": len(active_diag) + len(chronic_diag),
            "recent_encounters": [
                _encounter_to_dict(e) for e in recent_encounters
            ],
        }

    payload = await asyncio.to_thread(_query)

    from istota.health.immunizations import (
        compute_coverage,
        STATUS_DUE_SOON,
        STATUS_OVERDUE,
    )

    def _imm_summary():
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
            rows = health_db.list_immunizations(conn, limit=5000)
        coverage = compute_coverage(refs, rows)
        overdue = sum(1 for c in coverage if c.status == STATUS_OVERDUE)
        due_soon = sum(1 for c in coverage if c.status == STATUS_DUE_SOON)
        # Latest single dose across all rows.
        last_given = None
        if rows:
            most_recent = max(rows, key=lambda r: r.date_given or "")
            last_given = {
                "name": most_recent.name,
                "date_given": most_recent.date_given,
            }
        return {
            "overdue_count": overdue,
            "due_soon_count": due_soon,
            "last_given": last_given,
        }

    payload["immunizations"] = await asyncio.to_thread(_imm_summary)
    return payload


# ---- Encounters -----------------------------------------------------------


@router.get("/encounters")
async def api_list_encounters(
    ctx: HealthContext = Depends(get_user_context),
    since: str = Query(default=""),
    until: str = Query(default=""),
    type: str = Query(default=""),
    limit: int = Query(default=50, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query_with_counts():
        with health_db.connect(ctx.db_path) as conn:
            rows = health_db.list_encounters(
                conn,
                since=since or None,
                until=until or None,
                encounter_type=type or None,
                limit=limit,
                offset=offset,
            )
            counts = health_db.document_counts_for_entities(
                conn, "encounter", [e.id for e in rows],
            )
        return rows, counts

    encounters, doc_counts = await asyncio.to_thread(_query_with_counts)
    return {
        "encounters": [
            {**_encounter_to_dict(e), "document_count": doc_counts.get(e.id, 0)}
            for e in encounters
        ],
    }


@router.post("/encounters")
async def api_create_encounter(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    encounter_date = body.get("encounter_date")
    encounter_type = body.get("encounter_type")
    if not isinstance(encounter_date, str) or not encounter_date.strip():
        return JSONResponse(
            {"error": "encounter_date is required"}, status_code=400,
        )
    if not isinstance(encounter_type, str) or not encounter_type.strip():
        return JSONResponse(
            {"error": "encounter_type is required"}, status_code=400,
        )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn,
                encounter_date=encounter_date.strip(),
                encounter_type=encounter_type.strip(),
                provider=body.get("provider") or None,
                facility=body.get("facility") or None,
                specialty=body.get("specialty") or None,
                reason=body.get("reason") or None,
                notes=body.get("notes") or None,
            )
            conn.commit()
        return eid

    eid = await asyncio.to_thread(_insert)
    return {"status": "ok", "id": eid}


@router.post("/encounters/extract")
async def api_encounter_extract(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """OCR/vision extraction for a doctor's-visit document.

    The upload is **kept** as a document (``source="import"``) and its id is
    returned so ``/encounters/bulk`` can link it to every row this import
    creates. A document nobody confirms is left linkless and collected by
    the orphan sweep.

    Returns rows in the review-and-confirm shape consumed by
    ``/encounters/bulk``.
    """
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    mime = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    )
    suffix = Path(file.filename or "").suffix or (
        mimetypes.guess_extension(mime) or ""
    )

    config = getattr(request.app.state, "istota_config", None)
    max_bytes = _max_document_bytes(request)

    def _run():
        import tempfile

        from istota.executor import daemon_work_dir
        from istota.health.encounter_ocr import extract_from_file

        # One level below `config.temp_dir`, not the shared root: that is the
        # directory the OCR sandbox binds read-write, and the root is bound by
        # nothing (ISSUE-397). `daemon_work_dir` creates it, and falls back to
        # the shared root when the user id names no directory under it. The
        # root itself is `config.temp_dir`, or the system temp dir when no
        # config reached this route — a separate condition, not a second step.
        tmp_dir = daemon_work_dir(config, ctx.user_id)
        with tempfile.NamedTemporaryFile(
            dir=tmp_dir, suffix=suffix or ".bin", delete=False,
        ) as tmp:
            tmp.write(raw)
            # Resolved, like `ocr._resolve_source_file`: this path becomes the
            # request's `fs_read_roots` entry as well as the path named in the
            # prompt, and `ToolEnv` compares realpaths (ISSUE-395).
            tmp_path = Path(tmp.name).resolve()
        try:
            return extract_from_file(
                tmp_path, mime, config=config, user_id=ctx.user_id
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    result = await asyncio.to_thread(_run)
    return await _attach_import_document(
        ctx, result, raw=raw, filename=file.filename or "",
        mime=file.content_type, max_bytes=max_bytes,
    )


@router.post("/encounters/bulk")
async def api_encounter_bulk(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return JSONResponse({"error": "rows must be a list"}, status_code=400)
    client_import_id = body.get("import_id") if isinstance(body, dict) else None
    if client_import_id is not None and (
        not isinstance(client_import_id, str) or not client_import_id.strip()
    ):
        return JSONResponse(
            {"error": "import_id must be a non-empty string"},
            status_code=400,
        )
    today = date.today()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            return JSONResponse(
                {"error": f"row {i} must be an object"}, status_code=400,
            )
        d_raw = r.get("encounter_date")
        if not isinstance(d_raw, str) or not d_raw.strip():
            return JSONResponse(
                {"error": f"row {i} missing encounter_date"}, status_code=400,
            )
        try:
            d = date.fromisoformat(d_raw.strip())
        except ValueError:
            return JSONResponse(
                {"error": f"row {i} encounter_date must be ISO YYYY-MM-DD"},
                status_code=400,
            )
        if d > today:
            return JSONResponse(
                {"error": f"row {i} encounter_date is in the future"},
                status_code=400,
            )
        t = r.get("encounter_type")
        if not isinstance(t, str) or not t.strip():
            return JSONResponse(
                {"error": f"row {i} missing encounter_type"}, status_code=400,
            )

    document_id = body.get("document_id") if isinstance(body, dict) else None
    if document_id is not None:
        try:
            document_id = int(document_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "document_id must be an integer"}, status_code=400,
            )

    prefix = (
        client_import_id.strip() if client_import_id else uuid.uuid4().hex
    )

    def _insert_all():
        encounter_ids: list[int] = []
        diagnosis_ids: list[int] = []
        with health_db.connect(ctx.db_path) as conn:
            # Check the document *before* any insert — a bad id must not
            # leave half an import behind.
            if (
                document_id is not None
                and health_db.get_document(conn, document_id) is None
            ):
                return "document_missing"
            for i, r in enumerate(rows):
                eid = health_db.insert_encounter(
                    conn,
                    encounter_date=r["encounter_date"].strip(),
                    encounter_type=r["encounter_type"].strip(),
                    provider=(r.get("provider") or None),
                    facility=(r.get("facility") or None),
                    specialty=(r.get("specialty") or None),
                    reason=(r.get("reason") or None),
                    notes=(r.get("notes") or None),
                    dedup_key=f"{prefix}:{i}",
                )
                encounter_ids.append(eid)
                for j, d in enumerate(r.get("diagnoses") or []):
                    if not isinstance(d, dict):
                        continue
                    name = d.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    status = d.get("status") or "active"
                    if status not in ("active", "resolved", "chronic"):
                        status = "active"
                    severity = d.get("severity") or None
                    if severity not in ("mild", "moderate", "severe", None):
                        severity = None
                    did = health_db.insert_diagnosis(
                        conn,
                        name=name.strip(),
                        status=status,
                        icd10=(d.get("icd10") or None),
                        date_diagnosed=r["encounter_date"].strip(),
                        encounter_id=eid,
                        severity=severity,
                        dedup_key=f"{prefix}:{i}:dx:{j}",
                        reconcile=True,
                    )
                    diagnosis_ids.append(did)
            if document_id is not None:
                for eid in encounter_ids:
                    health_db.link_document(
                        conn, document_id, "encounter", eid,
                    )
                for did in diagnosis_ids:
                    health_db.link_document(
                        conn, document_id, "diagnosis", did,
                    )
            conn.commit()
        return encounter_ids, diagnosis_ids

    result = await asyncio.to_thread(_insert_all)
    if result == "document_missing":
        return JSONResponse(
            {"error": "document not found"}, status_code=400,
        )
    encounter_ids, diagnosis_ids = result
    return {
        "status": "ok",
        "ids": encounter_ids,
        "count": len(encounter_ids),
        "diagnosis_ids": diagnosis_ids,
        "document_id": document_id,
    }


@router.get("/encounters/{encounter_id}")
async def api_get_encounter(
    encounter_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, encounter_id)
            if not enc:
                return None
            diagnoses = health_db.diagnoses_for_encounter(conn, encounter_id)
            panels = health_db.panels_for_encounter(conn, encounter_id)
            panel_dicts = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                panel_dicts.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            docs = health_db.documents_for_entity(
                conn, "encounter", encounter_id,
            )
            return enc, diagnoses, panel_dicts, docs

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "encounter not found")
    enc, diagnoses, panel_dicts, docs = result
    return {
        "encounter": _encounter_to_dict(enc),
        "diagnoses": [_diagnosis_to_dict(d) for d in diagnoses],
        "panels": panel_dicts,
        "documents": [_document_to_dict(d) for d in docs],
    }


@router.put("/encounters/{encounter_id}")
async def api_update_encounter(
    encounter_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    allowed = {
        "encounter_date", "encounter_type", "provider", "facility",
        "specialty", "reason", "notes",
    }
    kwargs = {k: v for k, v in body.items() if k in allowed}

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.update_encounter(conn, encounter_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if not n:
        # 0 rows could mean "no fields" or "not found"; distinguish.
        def _check():
            with health_db.connect(ctx.db_path) as conn:
                return health_db.get_encounter(conn, encounter_id)
        existing = await asyncio.to_thread(_check)
        if existing is None:
            raise HTTPException(404, "encounter not found")
    return {"status": "ok"}


@router.delete("/encounters/{encounter_id}")
async def api_delete_encounter(
    encounter_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_encounter(conn, encounter_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "encounter not found")
    return {"status": "ok"}


# ---- Diagnoses ------------------------------------------------------------


@router.get("/diagnoses")
async def api_list_diagnoses(
    ctx: HealthContext = Depends(get_user_context),
    status: str = Query(default=""),
    limit: int = Query(default=100, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    if status and status not in _DIAGNOSIS_STATUSES and status != "all":
        return JSONResponse(
            {"error": "unknown status"}, status_code=400,
        )

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            rows = health_db.list_diagnoses(
                conn,
                status=status or None,
                limit=limit,
                offset=offset,
            )
            counts = health_db.document_counts_for_entities(
                conn, "diagnosis", [d.id for d in rows],
            )
            links = health_db.encounter_ids_for_diagnoses(
                conn, [d.id for d in rows],
            )
        return rows, counts, links

    diagnoses, doc_counts, links = await asyncio.to_thread(_query)
    return {
        "diagnoses": [
            {
                **_diagnosis_to_dict(d, links.get(d.id, [])),
                "document_count": doc_counts.get(d.id, 0),
            }
            for d in diagnoses
        ],
    }


@router.post("/diagnoses")
async def api_create_diagnosis(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    status = body.get("status", "active")
    if status not in _DIAGNOSIS_STATUSES:
        return JSONResponse({"error": "unknown status"}, status_code=400)
    encounter_id = body.get("encounter_id")
    if encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer"}, status_code=400,
            )
    # `encounter_ids` is the real field; the singular one above is legacy
    # shorthand for a one-element list. Supplying both unions them.
    try:
        encounter_ids = (
            _coerce_encounter_ids(body["encounter_ids"])
            if "encounter_ids" in body
            else []
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if encounter_id is not None and encounter_id not in encounter_ids:
        encounter_ids = [encounter_id, *encounter_ids]

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            # Validate every id before inserting anything, so a bad one in the
            # list cannot leave a half-linked condition behind.
            missing = _missing_encounter(conn, encounter_ids)
            if missing is not None:
                return None
            did = health_db.insert_diagnosis(
                conn,
                name=name.strip(),
                status=status,
                icd10=body.get("icd10") or None,
                date_diagnosed=body.get("date_diagnosed") or None,
                date_resolved=body.get("date_resolved") or None,
                encounter_id=encounter_id,
                severity=body.get("severity") or None,
                notes=body.get("notes") or None,
            )
            for eid in encounter_ids:
                health_db.link_diagnosis_encounter(conn, did, eid)
            conn.commit()
        return did

    did = await asyncio.to_thread(_insert)
    if did is None:
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    return {"status": "ok", "id": did}


@router.get("/diagnoses/{diagnosis_id}")
async def api_get_diagnosis(
    diagnosis_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            d = health_db.get_diagnosis(conn, diagnosis_id)
            if not d:
                return None
            linked_encs = health_db.encounters_for_diagnosis(conn, diagnosis_id)
            docs = health_db.documents_for_entity(
                conn, "diagnosis", diagnosis_id,
            )
        return d, linked_encs, docs

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "diagnosis not found")
    d, linked_encs, docs = result
    return {
        "diagnosis": _diagnosis_to_dict(d, [e.id for e in linked_encs]),
        "encounters": [_encounter_to_dict(e) for e in linked_encs],
        "documents": [_document_to_dict(x) for x in docs],
    }


@router.put("/diagnoses/{diagnosis_id}")
async def api_update_diagnosis(
    diagnosis_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    if "status" in body and body["status"] not in _DIAGNOSIS_STATUSES:
        return JSONResponse({"error": "unknown status"}, status_code=400)
    allowed = {
        "name", "icd10", "status", "date_diagnosed", "date_resolved",
        "encounter_id", "severity", "notes",
    }
    kwargs = {k: v for k, v in body.items() if k in allowed}
    if "encounter_id" in kwargs and kwargs["encounter_id"] is not None:
        try:
            kwargs["encounter_id"] = int(kwargs["encounter_id"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer or null"},
                status_code=400,
            )
    # `encounter_ids` replaces the whole link set (an empty list clears it) and
    # wins over the legacy scalar when both are sent.
    replace_ids: list[int] | None = None
    if "encounter_ids" in body:
        try:
            replace_ids = _coerce_encounter_ids(body["encounter_ids"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            if (
                "encounter_id" in kwargs
                and kwargs["encounter_id"] is not None
                and health_db.get_encounter(conn, kwargs["encounter_id"]) is None
            ):
                return "encounter_not_found"
            if replace_ids is not None and _missing_encounter(conn, replace_ids) is not None:
                return "encounter_not_found"
            n = health_db.update_diagnosis(conn, diagnosis_id, **kwargs)
            if replace_ids is not None:
                if health_db.get_diagnosis(conn, diagnosis_id) is None:
                    return 0
                health_db.set_diagnosis_encounters(conn, diagnosis_id, replace_ids)
                # A links-only edit changes no diagnoses column, so
                # update_diagnosis reports 0 rows — that must not read as 404.
                n = n or 1
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if n == "encounter_not_found":
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    if not n:
        def _check():
            with health_db.connect(ctx.db_path) as conn:
                return health_db.get_diagnosis(conn, diagnosis_id)
        existing = await asyncio.to_thread(_check)
        if existing is None:
            raise HTTPException(404, "diagnosis not found")
    return {"status": "ok"}


@router.delete("/diagnoses/{diagnosis_id}")
async def api_delete_diagnosis(
    diagnosis_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_diagnosis(conn, diagnosis_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "diagnosis not found")
    return {"status": "ok"}


# A condition's encounters, as add/remove verbs rather than whole-set writes.
# Same shape as the document-links routes, and what the UI uses so linking one
# more visit never has to send (and risk clobbering) the existing set.


@router.post("/diagnoses/{diagnosis_id}/encounters")
async def api_link_diagnosis_encounter(
    diagnosis_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    try:
        encounter_id = int(body.get("encounter_id"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "encounter_id must be an integer"}, status_code=400,
        )

    def _link():
        with health_db.connect(ctx.db_path) as conn:
            if health_db.get_diagnosis(conn, diagnosis_id) is None:
                return "diagnosis_not_found"
            if health_db.get_encounter(conn, encounter_id) is None:
                return "encounter_not_found"
            # Re-linking an existing pair is a no-op rather than an error, so a
            # repeated click reports success — same contract as link_document.
            created = health_db.link_diagnosis_encounter(
                conn, diagnosis_id, encounter_id,
            )
            conn.commit()
        return created

    outcome = await asyncio.to_thread(_link)
    if outcome == "diagnosis_not_found":
        raise HTTPException(404, "diagnosis not found")
    if outcome == "encounter_not_found":
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    return {"status": "ok", "created": bool(outcome)}


@router.delete("/diagnoses/{diagnosis_id}/encounters/{encounter_id}")
async def api_unlink_diagnosis_encounter(
    diagnosis_id: int,
    encounter_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _unlink():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.unlink_diagnosis_encounter(
                conn, diagnosis_id, encounter_id,
            )
            conn.commit()
        return n

    n = await asyncio.to_thread(_unlink)
    if not n:
        raise HTTPException(404, "link not found")
    return {"status": "ok"}


# ---- History summary ------------------------------------------------------


@router.get("/history/summary")
async def api_history_summary(
    ctx: HealthContext = Depends(get_user_context),
):
    """New-doctor packet: active conditions, chronic conditions,
    recent encounters (last 12 months), and last 5 procedures in the last
    5 years (older procedures aren't clinically useful for a packet)."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    one_year_ago = (today - timedelta(days=365)).isoformat()
    five_years_ago = (today - timedelta(days=365 * 5)).isoformat()

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            active = health_db.list_diagnoses(conn, status="active", limit=500)
            chronic = health_db.list_diagnoses(conn, status="chronic", limit=500)
            recent_encounters = health_db.list_encounters(
                conn, since=one_year_ago, limit=500,
            )
            recent_procedures = health_db.list_encounters(
                conn,
                encounter_type="procedure",
                since=five_years_ago,
                limit=5,
            )
        return active, chronic, recent_encounters, recent_procedures

    active, chronic, recent, procedures = await asyncio.to_thread(_query)
    # Immunizations: include up-to-date routine entries (compact list) and
    # any overdue / series_incomplete actions.
    def _imm_query():
        from istota.health.immunizations import (
            compute_coverage,
            STATUS_UP_TO_DATE,
            STATUS_OVERDUE,
            STATUS_SERIES_INCOMPLETE,
            STATUS_EXPIRED,
        )

        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
            rows = health_db.list_immunizations(conn, limit=2000)
        cov = compute_coverage(refs, rows)
        up_to_date = [
            c for c in cov
            if c.status == STATUS_UP_TO_DATE and c.category in {"routine", "booster"}
        ]
        action_needed = [
            c for c in cov
            if c.status in {STATUS_OVERDUE, STATUS_SERIES_INCOMPLETE, STATUS_EXPIRED}
        ]
        return up_to_date, action_needed

    imm_up, imm_action = await asyncio.to_thread(_imm_query)
    return {
        "active_diagnoses": [_diagnosis_to_dict(d) for d in active],
        "chronic_diagnoses": [_diagnosis_to_dict(d) for d in chronic],
        "recent_encounters": [_encounter_to_dict(e) for e in recent],
        "recent_procedures": [_encounter_to_dict(e) for e in procedures],
        "immunizations": {
            "up_to_date": [_coverage_to_dict(c) for c in imm_up],
            "action_needed": [_coverage_to_dict(c) for c in imm_action],
        },
    }


# ---- Immunizations --------------------------------------------------------


_IMMUNIZATION_UPDATE_FIELDS = {
    "name", "product_name", "date_given", "manufacturer", "dose_label",
    "lot_number", "route", "site", "administered_by", "facility",
    "encounter_id", "cvx_code", "notes",
}


_immunization_to_dict = immunization_to_dict


def _immunization_ref_to_dict(r) -> dict:
    return {
        "name": r.name,
        "display_name": r.display_name,
        "category": r.category,
        "schedule": r.schedule,
        "interval_days": r.interval_days,
        "primary_series_doses": r.primary_series_doses,
        "aliases": r.aliases,
        "description": r.description,
        "typical_age_range": r.typical_age_range,
    }


_coverage_to_dict = coverage_to_dict


@router.get("/immunizations")
async def api_list_immunizations(
    ctx: HealthContext = Depends(get_user_context),
    name: str = Query(default=""),
    since: str = Query(default=""),
    until: str = Query(default=""),
    limit: int = Query(default=200, le=2000, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            rows = health_db.list_immunizations(
                conn,
                name=name or None,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
            )
            counts = health_db.document_counts_for_entities(
                conn, "immunization", [r.id for r in rows],
            )
        return rows, counts

    rows, doc_counts = await asyncio.to_thread(_query)
    return {
        "immunizations": [
            {**_immunization_to_dict(r), "document_count": doc_counts.get(r.id, 0)}
            for r in rows
        ],
    }


@router.post("/immunizations")
async def api_create_immunization(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    date_given = body.get("date_given")
    if not isinstance(date_given, str) or not date_given.strip():
        return JSONResponse(
            {"error": "date_given is required"}, status_code=400,
        )
    encounter_id = body.get("encounter_id")
    if encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer"}, status_code=400,
            )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            if encounter_id is not None and health_db.get_encounter(
                conn, encounter_id,
            ) is None:
                return None
            iid = health_db.insert_immunization(
                conn,
                name=name.strip(),
                date_given=date_given.strip(),
                product_name=body.get("product_name") or None,
                manufacturer=body.get("manufacturer") or None,
                dose_label=body.get("dose_label") or None,
                lot_number=body.get("lot_number") or None,
                route=body.get("route") or None,
                site=body.get("site") or None,
                administered_by=body.get("administered_by") or None,
                facility=body.get("facility") or None,
                encounter_id=encounter_id,
                cvx_code=body.get("cvx_code") or None,
                notes=body.get("notes") or None,
                source=body.get("source") or "manual",
            )
            conn.commit()
        return iid

    iid = await asyncio.to_thread(_insert)
    if iid is None:
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    return {"status": "ok", "id": iid}


@router.get("/immunizations/refs")
async def api_immunization_refs(
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_immunization_refs(conn)

    refs = await asyncio.to_thread(_query)
    return {"refs": [_immunization_ref_to_dict(r) for r in refs]}


@router.get("/immunizations/coverage")
async def api_immunization_coverage(
    ctx: HealthContext = Depends(get_user_context),
):
    from istota.health.immunizations import compute_coverage

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
            rows = health_db.list_immunizations(conn, limit=5000)
        coverage = compute_coverage(refs, rows)
        # "Other" bucket — names that don't match any ref.
        canonical_names = {r.name for r in refs}
        other_names: dict[str, list] = {}
        for row in rows:
            if row.name in canonical_names:
                continue
            other_names.setdefault(row.name, []).append(row)
        other = [
            unmatched_coverage_to_dict(n, group)
            for n, group in other_names.items()
        ]
        return coverage, other

    coverage, other = await asyncio.to_thread(_query)
    return {
        "coverage": [_coverage_to_dict(c) for c in coverage],
        "other": other,
    }


@router.post("/immunizations/parse")
async def api_immunization_parse(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return JSONResponse({"error": "text is required"}, status_code=400)

    from istota.health.parser import parse_paste

    def _parse():
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
        return parse_paste(text, refs)

    rows = await asyncio.to_thread(_parse)
    return {
        "rows": [
            {
                "name": r.name,
                "product_name": r.product_name,
                "date_given": r.date_given,
                "source_line": r.source_line,
                "confidence": r.confidence,
                "notes": r.notes,
            }
            for r in rows
        ],
    }


@router.post("/immunizations/extract")
async def api_immunization_extract(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """OCR/vision extraction for an immunization-list screenshot or PDF.

    The upload is **kept** as a document (``source="import"``) and its id
    returned, so ``/immunizations/bulk`` can link it to every row it creates
    — proof of immunization is a document people are asked to produce, not
    merely a fact to recall. Returns the same ``rows`` shape as ``/parse``
    so the review-and-confirm UI is identical for both paths.
    """
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    mime = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    )
    suffix = Path(file.filename or "").suffix or (
        mimetypes.guess_extension(mime) or ""
    )

    config = getattr(request.app.state, "istota_config", None)
    max_bytes = _max_document_bytes(request)

    def _run():
        import tempfile

        from istota.executor import daemon_work_dir
        from istota.health.immunization_ocr import extract_from_file

        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
        # Process-scoped tmp for the *extractor's* copy. The kept copy goes
        # through store_document into {uploads_dir}/documents/, so a crash
        # between write and unlink here can't leak a file next to the
        # confirmed panel sources.
        #
        # One level below `config.temp_dir`, not the shared root: that is the
        # directory the OCR sandbox binds read-write, and the root is bound by
        # nothing (ISSUE-397).
        tmp_dir = daemon_work_dir(config, ctx.user_id)
        with tempfile.NamedTemporaryFile(
            dir=tmp_dir, suffix=suffix or ".bin", delete=False,
        ) as tmp:
            tmp.write(raw)
            # Resolved, like `ocr._resolve_source_file`: this path becomes the
            # request's `fs_read_roots` entry as well as the path named in the
            # prompt, and `ToolEnv` compares realpaths (ISSUE-395).
            tmp_path = Path(tmp.name).resolve()
        try:
            return extract_from_file(
                tmp_path, mime, refs, config=config, user_id=ctx.user_id
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    result = await asyncio.to_thread(_run)
    return await _attach_import_document(
        ctx, result, raw=raw, filename=file.filename or "",
        mime=file.content_type, max_bytes=max_bytes,
    )


@router.post("/immunizations/bulk")
async def api_immunization_bulk(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return JSONResponse({"error": "rows must be a list"}, status_code=400)
    # Optional client-supplied idempotency key. If the frontend generates
    # an import_id and reuses it on retry, double-submits collapse via the
    # dedup_key partial unique index. Absent → fresh server-side UUID
    # (still gives every row a stable dedup_key for future replay safety,
    # but won't dedupe across separate requests).
    client_import_id = body.get("import_id") if isinstance(body, dict) else None
    if client_import_id is not None and (
        not isinstance(client_import_id, str) or not client_import_id.strip()
    ):
        return JSONResponse(
            {"error": "import_id must be a non-empty string"},
            status_code=400,
        )
    today = date.today()
    # Validate every row before writing — partial bulk inserts are a worse
    # UX than a clean "fix this row and resubmit" error.
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            return JSONResponse(
                {"error": f"row {i} must be an object"}, status_code=400,
            )
        if not isinstance(r.get("name"), str) or not r["name"].strip():
            return JSONResponse(
                {"error": f"row {i} missing name"}, status_code=400,
            )
        if not isinstance(r.get("date_given"), str) or not r["date_given"].strip():
            return JSONResponse(
                {"error": f"row {i} missing date_given"}, status_code=400,
            )
        try:
            d = date.fromisoformat(r["date_given"].strip())
        except ValueError:
            return JSONResponse(
                {"error": f"row {i} date_given must be ISO YYYY-MM-DD"},
                status_code=400,
            )
        if d > today:
            return JSONResponse(
                {"error": f"row {i} date_given is in the future"},
                status_code=400,
            )

    # Use the client-supplied import_id as the dedup prefix when present,
    # otherwise mint a per-request one. With a client-supplied id, a
    # double-submit / retry from the same import session collapses against
    # the dedup_key partial unique index. Without one, every row still
    # gets a stable dedup_key (matching the skill CLI pattern at
    # skills/health/__init__.py:1175) for future replay safety.
    document_id = body.get("document_id") if isinstance(body, dict) else None
    if document_id is not None:
        try:
            document_id = int(document_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "document_id must be an integer"}, status_code=400,
            )

    prefix = (
        client_import_id.strip() if client_import_id else uuid.uuid4().hex
    )

    def _insert_all():
        ids: list[int] = []
        with health_db.connect(ctx.db_path) as conn:
            # Checked before any insert — a bad id must not leave half an
            # import behind.
            if (
                document_id is not None
                and health_db.get_document(conn, document_id) is None
            ):
                return "document_missing"
            for i, r in enumerate(rows):
                iid = health_db.insert_immunization(
                    conn,
                    name=r["name"].strip(),
                    date_given=r["date_given"].strip(),
                    product_name=r.get("product_name") or None,
                    manufacturer=r.get("manufacturer") or None,
                    dose_label=r.get("dose_label") or None,
                    lot_number=r.get("lot_number") or None,
                    route=r.get("route") or None,
                    site=r.get("site") or None,
                    administered_by=r.get("administered_by") or None,
                    facility=r.get("facility") or None,
                    cvx_code=r.get("cvx_code") or None,
                    notes=r.get("notes") or None,
                    source=r.get("source") or "import",
                    dedup_key=f"{prefix}:{i}",
                    reconcile=True,
                )
                ids.append(iid)
            if document_id is not None:
                for iid in ids:
                    health_db.link_document(
                        conn, document_id, "immunization", iid,
                    )
            conn.commit()
        return ids

    result = await asyncio.to_thread(_insert_all)
    if result == "document_missing":
        return JSONResponse(
            {"error": "document not found"}, status_code=400,
        )
    return {
        "status": "ok",
        "ids": result,
        "count": len(result),
        "document_id": document_id,
    }


@router.get("/immunizations/{immunization_id}")
async def api_get_immunization(
    immunization_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            row = health_db.get_immunization(conn, immunization_id)
            encounter = None
            docs: list = []
            if row and row.encounter_id is not None:
                encounter = health_db.get_encounter(conn, row.encounter_id)
            if row:
                docs = health_db.documents_for_entity(
                    conn, "immunization", immunization_id,
                )
        return row, encounter, docs

    row, encounter, docs = await asyncio.to_thread(_query)
    if not row:
        raise HTTPException(404, "immunization not found")
    return {
        "immunization": _immunization_to_dict(row),
        "encounter": _encounter_to_dict(encounter) if encounter else None,
        "documents": [_document_to_dict(d) for d in docs],
    }


@router.put("/immunizations/{immunization_id}")
async def api_update_immunization(
    immunization_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    kwargs = {k: v for k, v in body.items() if k in _IMMUNIZATION_UPDATE_FIELDS}

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.update_immunization(conn, immunization_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if not n:
        def _check():
            with health_db.connect(ctx.db_path) as conn:
                return health_db.get_immunization(conn, immunization_id)
        existing = await asyncio.to_thread(_check)
        if existing is None:
            raise HTTPException(404, "immunization not found")
    return {"status": "ok"}


@router.delete("/immunizations/{immunization_id}")
async def api_delete_immunization(
    immunization_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_immunization(conn, immunization_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "immunization not found")
    return {"status": "ok"}


@router.get("/immunizations/{name}/explainer")
async def api_immunization_explainer(
    name: str,
    ctx: HealthContext = Depends(get_user_context),
):
    """Educational primer for a vaccine, served from bundled JSON.

    Status is derived from current coverage but no longer gates the
    response — the curated content is shown for every vaccine in the
    canonical refs that has an entry.
    """
    from istota.health.immunization_explainer import get_explainer
    from istota.health.immunizations import compute_coverage

    def _resolve():
        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_immunization_ref_by_alias(conn, name)
            if ref is None:
                return None
            refs = health_db.list_immunization_refs(conn)
            rows = health_db.list_immunizations(conn, limit=5000)
        coverage = compute_coverage(refs, rows)
        entry = next((c for c in coverage if c.name == ref.name), None)
        status = entry.status if entry else "never_recorded"
        return get_explainer(
            name=ref.name,
            display_name=ref.display_name,
            status=status,
        )

    result = await asyncio.to_thread(_resolve)
    if result is None:
        raise HTTPException(404, "vaccine not found")
    return result


# ---- Documents ------------------------------------------------------------


def _document_error_response(e: health_documents.DocumentError) -> JSONResponse:
    if isinstance(e, health_documents.UnsupportedDocumentType):
        return JSONResponse({"error": str(e)}, status_code=415)
    if isinstance(e, health_documents.DocumentTooLarge):
        return JSONResponse({"error": str(e)}, status_code=413)
    if isinstance(e, health_documents.UnknownEntity):
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/documents")
async def api_create_document(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    entity_type: str = Form(""),
    entity_id: str = Form(""),
    notes: str = Form(""),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Store a document, optionally attaching it to one record."""
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)

    target: tuple[str, int] | None = None
    if entity_type or entity_id:
        if entity_type not in health_db.DOCUMENT_ENTITY_TYPES:
            return JSONResponse(
                {"error": f"unknown entity type: {entity_type}"},
                status_code=400,
            )
        try:
            target = (entity_type, int(entity_id))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "entity_id must be an integer"}, status_code=400,
            )

    max_bytes = _max_document_bytes(request)

    def _store():
        with health_db.connect(ctx.db_path) as conn:
            if target is not None and not _entity_exists(conn, *target):
                return "entity_missing"
            doc, created = health_documents.store_document(
                conn, ctx, raw=raw, filename=file.filename or "",
                mime=file.content_type, source="manual",
                notes=notes or None, max_bytes=max_bytes,
            )
            linked = False
            if target is not None:
                linked = health_db.link_document(conn, doc.id, *target)
            conn.commit()
        return doc, created, linked

    try:
        result = await asyncio.to_thread(_store)
    except health_documents.DocumentError as e:
        return _document_error_response(e)
    if result == "entity_missing":
        return JSONResponse(
            {"error": f"{target[0]} not found"}, status_code=404,
        )
    doc, created, linked = result
    return {
        **_document_to_dict(doc),
        "status": "ok",
        "created": created,
        "linked": linked,
    }


@router.get("/documents")
async def api_list_documents(
    ctx: HealthContext = Depends(get_user_context),
    entity_type: str = Query(default=""),
    entity_id: int = Query(default=0),
    limit: int = Query(default=200, le=1000, ge=1),
    offset: int = Query(default=0, ge=0),
):
    if entity_type or entity_id:
        if entity_type not in health_db.DOCUMENT_ENTITY_TYPES:
            return JSONResponse(
                {"error": f"unknown entity type: {entity_type}"},
                status_code=400,
            )
        if entity_id <= 0:
            return JSONResponse(
                {"error": "entity_id must be a positive integer"},
                status_code=400,
            )

        def _for_entity():
            with health_db.connect(ctx.db_path) as conn:
                return _documents_payload(
                    conn,
                    health_db.documents_for_entity(
                        conn, entity_type, entity_id,
                    ),
                )

        return {"documents": await asyncio.to_thread(_for_entity)}

    def _all():
        with health_db.connect(ctx.db_path) as conn:
            return _documents_payload(
                conn,
                health_db.list_documents(conn, limit=limit, offset=offset),
            )

    return {"documents": await asyncio.to_thread(_all)}


@router.get("/documents/{document_id}")
async def api_get_document(
    document_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            doc = health_db.get_document(conn, document_id)
            if doc is None:
                return None
            return doc, _links_payload(conn, document_id)

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "document not found")
    doc, links = result
    return {"document": _document_to_dict(doc), "links": links}


@router.get("/documents/{document_id}/file")
async def api_document_file(
    document_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    """Stream a document's bytes. Auth-gated; path resolved server-side.

    Always ``Content-Disposition: attachment`` + ``nosniff``, unlike
    ``/panels/{id}/source``: a document may be an email attachment the agent
    filed, i.e. content from outside the trust boundary, and serving
    attacker-supplied HTML/SVG inline on the app's own origin would run it
    against the session cookie. ``<iframe>`` / ``<img>`` render PDFs and
    images regardless of the disposition header (D6).
    """
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_document(conn, document_id)

    doc = await asyncio.to_thread(_query)
    if doc is None:
        raise HTTPException(404, "document not found")
    try:
        candidate = health_documents.resolve_document_path(ctx, doc)
    except ValueError:
        raise HTTPException(400, "invalid source path")
    if not candidate.is_file():
        raise HTTPException(404, "source file missing")
    return FileResponse(
        candidate,
        media_type=doc.mime or "application/octet-stream",
        filename=doc.filename,
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="attachment",
    )


@router.post("/documents/{document_id}/links")
async def api_link_document(
    document_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    entity_type = body.get("entity_type")
    if entity_type not in health_db.DOCUMENT_ENTITY_TYPES:
        return JSONResponse(
            {"error": f"unknown entity type: {entity_type}"}, status_code=400,
        )
    try:
        entity_id = int(body.get("entity_id"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "entity_id must be an integer"}, status_code=400,
        )

    def _link():
        with health_db.connect(ctx.db_path) as conn:
            if health_db.get_document(conn, document_id) is None:
                return "document_missing"
            if not _entity_exists(conn, entity_type, entity_id):
                return "entity_missing"
            created = health_db.link_document(
                conn, document_id, entity_type, entity_id,
            )
            conn.commit()
        return created

    result = await asyncio.to_thread(_link)
    if result == "document_missing":
        return JSONResponse({"error": "document not found"}, status_code=404)
    if result == "entity_missing":
        return JSONResponse(
            {"error": f"{entity_type} not found"}, status_code=404,
        )
    return {"status": "ok", "created": result}


@router.delete("/documents/{document_id}/links/{entity_type}/{entity_id}")
async def api_unlink_document(
    document_id: int,
    entity_type: str,
    entity_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    if entity_type not in health_db.DOCUMENT_ENTITY_TYPES:
        return JSONResponse(
            {"error": f"unknown entity type: {entity_type}"}, status_code=400,
        )

    def _unlink():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.unlink_document(
                conn, document_id, entity_type, entity_id,
            )
            conn.commit()
        return n

    n = await asyncio.to_thread(_unlink)
    return {"status": "ok", "removed": bool(n)}


@router.delete("/documents/{document_id}")
async def api_delete_document(
    document_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            ok = health_documents.delete_document_fully(conn, ctx, document_id)
            conn.commit()
        return ok

    ok = await asyncio.to_thread(_delete)
    if not ok:
        raise HTTPException(404, "document not found")
    return {"status": "ok"}


# ---- Garmin ---------------------------------------------------------------


def _user_id_from_request(request: Request) -> str:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not isinstance(user, dict) or not user.get("username"):
        raise HTTPException(401, "unauthorized")
    return user["username"]


def _framework_db_path(request: Request) -> Path:
    """Framework istota.db path — where the encrypted ``secrets`` table
    (and therefore Garmin tokens) lives."""
    cfg = getattr(request.app.state, "istota_config", None)
    db_path = getattr(cfg, "db_path", None) if cfg else None
    if not db_path:
        raise HTTPException(503, "framework db_path unavailable")
    return Path(db_path)


def _user_tz(request: Request, user_id: str) -> str | None:
    cfg = getattr(request.app.state, "istota_config", None)
    if cfg is None:
        return None
    uc = cfg.get_user(user_id) if hasattr(cfg, "get_user") else None
    return getattr(uc, "timezone", None) if uc else None


# Garmin auth (status / connect / mfa / disconnect) moved to the
# module-agnostic router in ``istota.garmin_routes`` — Garmin is a
# cross-module connected service, so its auth surface is no longer gated on
# the health module. ``/garmin/sync`` stays here: it is a health consumer
# (daily summaries into the health stats table), not auth.


@router.post("/garmin/sync")
async def api_garmin_sync(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body: dict = {}
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    days_back = body.get("days_back", 7) if isinstance(body, dict) else 7
    try:
        days_back = max(1, min(90, int(days_back)))
    except (TypeError, ValueError):
        days_back = 7
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)
    user_tz = _user_tz(request, user_id)

    # Daemon-side config, for the reconnect notification an auth failure raises.
    config = getattr(request.app.state, "istota_config", None)

    def _do():
        return health_garmin_sync.sync_garmin(
            ctx, db_path, days_back=days_back, user_tz=user_tz, config=config,
        ).to_dict()

    return await asyncio.to_thread(_do)
