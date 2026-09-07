"""Module-agnostic Garmin auth routes.

Garmin is a cross-module connected service: the health module syncs daily
summaries and the location module imports GPS tracks, both off one shared
token blob in the framework ``secrets`` table (``service="garmin"``, keyed
on ``user_id``). Its *auth* surface therefore lives here — not under the
health router — so a user who has opted out of the health module can still
connect Garmin (for location) via Settings → Connected services.

What stays health-owned: ``/garmin/sync`` (daily-summary sync into the
health ``stats`` table) remains on the health router — it is a health
*consumer*, not auth.

The four routes here mirror the ones they replaced verbatim except for the
dropped ``HealthContext`` dependency (which had gated them on the health
module being enabled). ``require_auth`` / ``verify_origin`` are placeholder
dependencies overridden by ``web_app`` (same pattern as the health / money
routers), so session auth + CSRF are wired identically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from istota.health import garmin as health_garmin
from istota.web_router_stubs import require_auth, verify_origin


router = APIRouter()


def _user_id(request: Request) -> str:
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


@router.get("/status")
async def api_garmin_status(
    request: Request, _user: dict = Depends(require_auth),
):
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _query():
        return health_garmin.get_status(db_path, user_id)
    return await asyncio.to_thread(_query)


@router.post("/connect")
async def api_garmin_connect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return JSONResponse(
            {"error": "email and password are required"}, status_code=400,
        )
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.connect(
            db_path, user_id=user_id, email=email, password=password,
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)
    except health_garmin.GarminNotInstalled as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)


@router.post("/mfa")
async def api_garmin_mfa(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("code"), str):
        return JSONResponse({"error": "code is required"}, status_code=400)
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.complete_mfa(
            db_path, user_id=user_id, code=body["code"],
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


@router.post("/disconnect")
async def api_garmin_disconnect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.disconnect(db_path, user_id=user_id)

    return await asyncio.to_thread(_do)


@router.post("/import-tracks")
async def api_garmin_import_tracks(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    """One-click GPS-track import into the user's location.db. A Location
    feature — gated on the location module, not health. Runs the shared
    importer inline (in a thread); returns the per-activity summary."""
    from istota.location.garmin_import import ImportOptions, import_tracks

    user_id = _user_id(request)
    db_path = _framework_db_path(request)
    cfg = getattr(request.app.state, "istota_config", None)
    if cfg is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    if not cfg.is_module_enabled(user_id, "location"):
        return JSONResponse(
            {"status": "error", "error": "location module disabled"},
            status_code=403,
        )

    body: dict = {}
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    days_back = body.get("days_back", 7) if isinstance(body, dict) else 7
    try:
        days_back = max(1, min(365, int(days_back)))
    except (TypeError, ValueError):
        days_back = 7
    dry_run = bool(body.get("dry_run")) if isinstance(body, dict) else False

    def _do():
        return import_tracks(
            user_id, framework_db_path=db_path, config=cfg,
            options=ImportOptions(days_back=days_back, dry_run=dry_run),
        ).to_dict()

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)
    except health_garmin.GarminRateLimited:
        return JSONResponse(
            {"status": "error", "error": "Garmin rate-limited — try again later"},
            status_code=429,
        )
