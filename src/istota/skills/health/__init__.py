"""Health tracking skill CLI.

Subcommands cover both body stats and bloodwork:

* ``log <metric> <value>`` — record a stat measurement.
* ``stats [--metric M]`` — list stat entries.
* ``latest`` — latest value per metric.
* ``panels`` / ``panel <id>`` — list/show lab panels.
* ``add-panel`` — create a panel manually.
* ``add-biomarker`` — add a biomarker row to a panel.
* ``trend <name>`` — time-series for a biomarker across confirmed panels.
* ``upload <file>`` — register an uploaded source file (path inside the
  workspace) as a draft panel for later web-UI review.
* ``summary`` — dashboard-style snapshot.
* ``settings`` / ``set KEY VALUE`` — profile + display preferences.
* ``documents`` / ``document <id>`` — list/show stored paperwork.
* ``attach-document --path P --to TYPE:ID`` / ``detach-document <id>
  --from TYPE:ID`` — file paperwork against an encounter, condition, or
  immunization.

Per-user health DB resolved via ``HEALTH_DB_PATH`` (set by the
``setup_env`` hook below). Writes go through the deferred-op pattern when
``ISTOTA_DEFERRED_DIR`` / ``ISTOTA_TASK_ID`` are set (sandbox mode); the
scheduler applies them post-task. Direct mode is used by the CLI / web
shell when those env vars aren't set.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from istota.skills._cli import emit, error_envelope, run_skill_cli


_DEFER_FILENAME = "task_{task_id}_health_ops.json"


def setup_env(ctx) -> dict[str, str]:
    """Inject ``HEALTH_DB_PATH`` for the per-user health DB.

    Self-gates on ``Config.is_module_enabled(user_id, "health")`` — when
    the user has opted out the loader raises ``UserNotFoundError`` and we
    return no env contribution, so the CLI can't reach a DB it isn't
    supposed to touch.
    """
    from istota import health as _health  # noqa: PLC0415

    try:
        h = _health.resolve_for_user(ctx.task.user_id, ctx.config)
    except _health.UserNotFoundError:
        return {}
    _health.ensure_initialised(h)
    return {"HEALTH_DB_PATH": str(h.db_path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(payload: dict, *, error: bool = False) -> None:
    # `error=True` is what `_fail` uses to exit 1 on an envelope whose status
    # says something other than "error" — nothing passes it today, and it stays
    # because dropping it would make `_fail`'s exit depend on its caller's
    # spelling of the status.
    emit(payload, indent=None, ensure_ascii=True, default=str,
         exit_on_error=not error)
    if error:
        sys.exit(1)


def _fail(msg: str) -> None:
    _emit(error_envelope(msg), error=True)


def _db_path() -> str:
    db_path = os.environ.get("HEALTH_DB_PATH", "")
    if not db_path:
        _fail("HEALTH_DB_PATH not set — health module disabled or not configured")
    return db_path


def _connect() -> sqlite3.Connection:
    path = Path(_db_path())
    # In sandbox mode the file is read-only via bwrap, but we still need to
    # *read* it. Writes go through the deferred-op file.
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _panel_id_arg(raw: str):
    """argparse type for add-biomarker's panel id: an int, or a ``@NAME`` ref
    that resolves to a panel created by ``add-panel --ref NAME`` earlier in the
    same deferred batch (ISSUE-092). Returns an int or the ``@NAME`` string."""
    if raw.startswith("@"):
        if len(raw) < 2:
            raise argparse.ArgumentTypeError("@ref must name a panel (e.g. @cbc)")
        return raw
    return int(raw)  # ValueError -> argparse surfaces a clean error


def _defer_op(op: dict) -> bool:
    """Append a write op to the per-task deferred file. Returns True when
    deferred; False when no sandbox context is set (caller writes directly).
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False
    path = Path(deferred_dir) / _DEFER_FILENAME.format(task_id=task_id)
    ops: list[dict] = []
    if path.exists():
        try:
            ops = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            ops = []
    ops.append(op)
    path.write_text(json.dumps(ops), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------


def _coerce_value(metric: str, raw: str, unit: str | None) -> tuple[float, str]:
    """Coerce a value+unit into metric storage units.

    Trivial conversions only — kg/lb, cm/in, °C/°F. Unknown unit pairs
    pass through unchanged so we never silently misrecord a value.
    """
    from istota.health.units import convert_temperature

    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fail(f"value must be numeric, got {raw!r}")

    u = (unit or "").strip()
    if not u:
        # Pick the canonical metric unit when caller didn't say.
        from istota.health.models import STAT_METRICS
        return v, STAT_METRICS.get(metric, "")

    # Imperial → metric for weight.
    if metric == "weight" and u.lower() in ("lb", "lbs", "pound", "pounds"):
        return round(v * 0.45359237, 4), "kg"
    if metric == "weight" and u.lower() in ("kg", "kgs"):
        return v, "kg"
    if metric == "body_temp":
        c = convert_temperature(v, u, "C")
        if c is not None and u.lower() in ("f", "°f"):
            return round(c, 2), "°C"
    return v, u


def _parse_height(raw: str) -> float | None:
    """Parse a height value: ``178``, ``178cm``, ``5ft10in``, ``70in``, ``5'10\"``."""
    import re as _re

    s = raw.strip().lower().replace(" ", "")
    if not s:
        return None
    # plain number → cm
    try:
        return float(s)
    except ValueError:
        pass
    # 5ft10in / 5'10" (try composite before plain ``in`` so feet+inches wins)
    m = _re.match(r"^(\d+)(?:ft|')(\d+(?:\.\d+)?)(?:in|\")?$", s)
    if m:
        feet = float(m.group(1))
        inches = float(m.group(2))
        return round((feet * 12 + inches) * 2.54, 2)
    # NNcm
    if s.endswith("cm"):
        try:
            return float(s[:-2])
        except ValueError:
            return None
    # NNin or NN" → in
    if s.endswith("in"):
        try:
            return round(float(s[:-2]) * 2.54, 2)
        except ValueError:
            return None
    if s.endswith('"'):
        try:
            return round(float(s[:-1]) * 2.54, 2)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_log(args: argparse.Namespace) -> None:
    metric = args.metric
    value, unit = _coerce_value(metric, args.value, args.unit)
    op = {
        "op": "insert_stat",
        "metric": metric,
        "value": value,
        "unit": unit,
        "measured_at": args.date,
        "notes": args.notes,
        "source": "manual",
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        sid = health_db.insert_stat(
            conn, metric=metric, value=value, unit=unit,
            measured_at=args.date, source="manual", notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": sid, "metric": metric, "value": value, "unit": unit})


def cmd_stats(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        rows = health_db.list_stats(
            conn,
            metric=args.metric,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit({
        "stats": [
            {
                "id": r.id,
                "metric": r.metric,
                "value": r.value,
                "unit": r.unit,
                "measured_at": r.measured_at,
                "source": r.source,
                "notes": r.notes,
            }
            for r in rows
        ],
    })


def cmd_latest(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        latest = health_db.latest_stats(conn)
    finally:
        conn.close()
    _emit({
        "stats": {
            metric: {
                "value": s.value,
                "unit": s.unit,
                "measured_at": s.measured_at,
                "source": s.source,
            }
            for metric, s in latest.items()
        },
    })


def cmd_panels(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        panels = health_db.list_panels(
            conn, since=args.since, limit=args.limit, include_drafts=True,
        )
        out = []
        for p in panels:
            total, flagged = health_db.panel_counts(conn, p.id)
            out.append({
                "id": p.id,
                "drawn_at": p.drawn_at,
                "lab_name": p.lab_name,
                "panel_type": p.panel_type,
                "biomarker_count": total,
                "flagged_count": flagged,
                "draft": p.draft,
            })
    finally:
        conn.close()
    _emit({"panels": out})


def cmd_panel(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        panel = health_db.get_panel(conn, args.id)
        if not panel:
            _fail(f"panel {args.id} not found")
        biomarkers = health_db.list_biomarkers_for_panel(conn, args.id)
    finally:
        conn.close()
    _emit({
        "panel": {
            "id": panel.id,
            "drawn_at": panel.drawn_at,
            "lab_name": panel.lab_name,
            "panel_type": panel.panel_type,
            "draft": panel.draft,
            "notes": panel.notes,
        },
        "biomarkers": [
            {
                "id": b.id, "name": b.name, "display_name": b.display_name,
                "value": b.value, "unit": b.unit,
                "ref_range_low": b.ref_range_low,
                "ref_range_high": b.ref_range_high,
                "flag": b.flag,
            }
            for b in biomarkers
        ],
    })


def cmd_add_panel(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_panel",
        "drawn_at": args.drawn_at,
        "lab_name": args.lab,
        "panel_type": args.type,
        "notes": args.notes,
    }
    if getattr(args, "ref", None):
        op["ref"] = args.ref
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        pid = health_db.insert_panel(
            conn,
            drawn_at=args.drawn_at, lab_name=args.lab,
            panel_type=args.type, notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": pid})


def cmd_add_biomarker(args: argparse.Namespace) -> None:
    # panel_id is an int, or a "@NAME" ref to a panel created earlier in the
    # same deferred batch (ISSUE-092). A ref only resolves at deferred-apply
    # time, so it's meaningless outside a sandboxed task.
    is_ref = isinstance(args.panel_id, str) and args.panel_id.startswith("@")
    op = {
        "op": "insert_biomarker",
        "name": args.name,
        "value": float(args.value),
        "unit": args.unit,
        "ref_range_low": args.ref_low,
        "ref_range_high": args.ref_high,
        "flag": args.flag,
    }
    if is_ref:
        op["panel_ref"] = args.panel_id[1:]
    else:
        op["panel_id"] = args.panel_id
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    if is_ref:
        _emit({
            "status": "error",
            "error": (
                f"panel ref {args.panel_id!r} can only be resolved inside a "
                "deferred (sandboxed) task; pass a numeric panel id directly"
            ),
        })
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        bid = health_db.insert_biomarker(
            conn,
            panel_id=args.panel_id,
            name=args.name,
            value=float(args.value),
            unit=args.unit,
            ref_range_low=args.ref_low,
            ref_range_high=args.ref_high,
            flag=args.flag,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": bid})


def cmd_trend(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        ref = health_db.find_biomarker_ref_by_alias(conn, args.name)
        canonical = ref.name if ref else args.name
        trend = health_db.biomarker_trend(
            conn, name=canonical, since=args.since, until=args.until,
        )
    finally:
        conn.close()
    _emit({
        "name": canonical,
        "points": [
            {
                "drawn_at": d, "value": b.value, "unit": b.unit, "flag": b.flag,
            }
            for b, d in trend
        ],
    })


def cmd_upload(args: argparse.Namespace) -> None:
    """Register an existing file path as a draft panel source.

    The sandboxed Claude process can't move files into the uploads
    directory directly, so callers point at a workspace-relative path and
    the scheduler relocates it during deferred processing.
    """
    path = Path(args.file_path)
    if not path.exists():
        _fail(f"file not found: {path}")
    op = {
        "op": "register_upload",
        "drawn_at": args.drawn_at,
        "lab_name": args.lab,
        "source_path": str(path),
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    # Direct mode is used only by the operator CLI / tests — we record the
    # panel but leave the file in place; the web upload route is the real
    # write path.
    from istota.health import db as health_db

    conn = _connect()
    try:
        pid = health_db.insert_panel(
            conn,
            drawn_at=args.drawn_at,
            lab_name=args.lab,
            source_file=str(path),
            draft=True,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": pid, "draft": True})


def _parse_entity_ref(raw: str) -> tuple[str, int | str]:
    """Parse a ``TYPE:ID`` token (``encounter:42``) into its two parts.

    ``encounter:@NAME`` is also accepted and returns the ``@NAME`` string
    unchanged, mirroring ``add-biomarker``'s ``@ref``: a deferred
    ``add-encounter --ref NAME`` can't hand its new id back to the sandboxed
    CLI, so the scheduler resolves the name at replay time.

    Emits the standard error envelope and exits on anything malformed — a
    mis-parsed reference would file paperwork against the wrong record.
    """
    from istota.health.db import DOCUMENT_ENTITY_TYPES

    text = (raw or "").strip()
    if ":" not in text:
        _fail(
            f"expected TYPE:ID (e.g. encounter:42), got {raw!r}; "
            f"types: {', '.join(DOCUMENT_ENTITY_TYPES)}"
        )
    entity_type, _, id_part = text.partition(":")
    entity_type = entity_type.strip().lower()
    id_part = id_part.strip()
    if entity_type not in DOCUMENT_ENTITY_TYPES:
        _fail(
            f"unknown entity type: {entity_type!r}; "
            f"expected one of {', '.join(DOCUMENT_ENTITY_TYPES)}"
        )
    if id_part.startswith("@"):
        if entity_type != "encounter":
            _fail(f"@ref is only supported for encounter, not {entity_type}")
        if len(id_part) < 2:
            _fail("@ref must name an encounter (e.g. encounter:@visit)")
        return entity_type, id_part
    try:
        entity_id = int(id_part)
    except (TypeError, ValueError):
        _fail(f"entity id must be an integer, got {id_part!r}")
    return entity_type, entity_id


def cmd_documents(args: argparse.Namespace) -> None:
    """List stored documents, optionally scoped to one record."""
    from istota.health import db as health_db

    conn = _connect()
    try:
        if args.entity:
            entity_type, entity_id = _parse_entity_ref(args.entity)
            if isinstance(entity_id, str):
                _fail("--entity needs a numeric id, not an @ref")
            docs = health_db.documents_for_entity(conn, entity_type, entity_id)
            docs = docs[: args.limit]
        else:
            docs = health_db.list_documents(conn, limit=args.limit)
    finally:
        conn.close()
    _emit({
        "status": "ok",
        "documents": [
            {
                "id": d.id,
                "filename": d.filename,
                "mime": d.mime,
                "byte_size": d.byte_size,
                "source": d.source,
                "notes": d.notes,
                "created_at": d.created_at,
            }
            for d in docs
        ],
    })


def cmd_document(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        doc = health_db.get_document(conn, args.document_id)
        if doc is None:
            _fail(f"document {args.document_id} not found")
        links = health_db.entity_links_for_document(conn, args.document_id)
    finally:
        conn.close()
    _emit({
        "status": "ok",
        "document": {
            "id": doc.id,
            "filename": doc.filename,
            "original_filename": doc.original_filename,
            "mime": doc.mime,
            "byte_size": doc.byte_size,
            "source": doc.source,
            "notes": doc.notes,
            "created_at": doc.created_at,
        },
        "links": [{"entity_type": t, "entity_id": i} for t, i in links],
    })


def cmd_attach_document(args: argparse.Namespace) -> None:
    """File a workspace file against an encounter / condition / immunization.

    Sandboxed, the health DB is read-only and ``uploads_dir`` is not ours to
    write, so this defers an ``attach_document`` op the scheduler replays
    (the same shape ``upload`` uses for ``register_upload``).
    """
    entity_type, entity_id = _parse_entity_ref(args.to)
    path = Path(args.path)
    if not path.exists():
        _fail(f"file not found: {path}")
    if not path.is_file():
        _fail(f"not a regular file: {path}")

    op: dict[str, Any] = {
        "op": "attach_document",
        "source_path": str(path),
        "filename": path.name,
        "entity_type": entity_type,
        "notes": args.notes,
    }
    if isinstance(entity_id, str):
        op["encounter_ref"] = entity_id.lstrip("@")
    else:
        op["entity_id"] = entity_id
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return

    if isinstance(entity_id, str):
        _fail(
            "encounter:@ref only resolves inside a sandboxed task; pass a "
            "numeric encounter id directly"
        )

    import mimetypes

    from istota.health import documents as health_documents

    ctx = _direct_context()
    raw = path.read_bytes()
    conn = _connect()
    try:
        # attach_document validates the target exists before writing bytes;
        # an UnknownEntity surfaces here as the standard error envelope.
        doc, created = health_documents.attach_document(
            conn, ctx, raw=raw, filename=path.name,
            mime=mimetypes.guess_type(path.name)[0],
            entity_type=entity_type, entity_id=entity_id,
            source="agent", notes=args.notes,
            max_bytes=_document_max_bytes(),
        )
        conn.commit()
    except health_documents.DocumentError as e:
        _fail(str(e))
    finally:
        conn.close()
    _emit({
        "status": "ok",
        "id": doc.id,
        "created": created,
        "filename": doc.filename,
        "entity_type": entity_type,
        "entity_id": entity_id,
    })


def cmd_detach_document(args: argparse.Namespace) -> None:
    entity_type, entity_id = _parse_entity_ref(getattr(args, "from"))
    if isinstance(entity_id, str):
        _fail("detach-document needs a numeric id, not an @ref")
    op = {
        "op": "detach_document",
        "document_id": args.document_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return

    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.unlink_document(
            conn, args.document_id, entity_type, entity_id,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "removed": bool(n)})


def _document_max_bytes() -> int:
    """The operator's ``[health] max_document_bytes``, or the library default.

    Read here rather than left to the library default so an operator who set
    `0` (unlimited) or lowered the cap gets it honoured on the agent path too,
    not only through the web routes.
    """
    from istota.health.documents import DEFAULT_MAX_DOCUMENT_BYTES

    try:
        from istota.config import load_config

        raw = getattr(getattr(load_config(), "health", None),
                      "max_document_bytes", None)
    except Exception:  # noqa: BLE001 — any config failure keeps the default
        return DEFAULT_MAX_DOCUMENT_BYTES
    if raw is None:
        return DEFAULT_MAX_DOCUMENT_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DOCUMENT_BYTES


def _direct_context():
    """Resolve a HealthContext for the unsandboxed path.

    Prefers the real loader, because on a server deploy the DB lives on
    local disk (``Config.module_db_path``) while ``uploads_dir`` stays on
    the mount — deriving one from the other would file documents where the
    web route can't find them. Falls back to the ``HEALTH_DB_PATH``-relative
    layout (``{workspace}/health/data/health.db``) when no config is
    reachable, matching ``_cmd_garmin_sync_direct``.
    """
    from istota.health.models import HealthContext

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if user_id:
        try:
            from istota.config import load_config
            from istota.health import _loader as health_loader

            return health_loader.resolve_for_user(user_id, load_config())
        except Exception:  # noqa: BLE001 — any config failure falls back
            pass

    db_path = Path(_db_path())
    data_dir = db_path.parent.parent
    return HealthContext(
        user_id=user_id,
        workspace_root=data_dir.parent,
        data_dir=data_dir,
        db_path=db_path,
        uploads_dir=data_dir / "uploads",
    )


def cmd_import_csv(args: argparse.Namespace) -> None:
    """Import a bloodwork CSV from a workspace-accessible file path.

    In sandbox mode the read + parse happens here (CLI is allowed to read
    files), but the writes are deferred so the scheduler applies them
    against the per-user health DB outside the sandbox.
    """
    path = Path(args.file_path)
    if not path.exists():
        _fail(f"file not found: {path}")
    op = {
        "op": "import_csv",
        "source_path": str(path),
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return

    from istota.health import csv_io

    csv_text = path.read_text(encoding="utf-8-sig", errors="replace")
    conn = _connect()
    try:
        summary = csv_io.import_csv(conn, csv_text)
        conn.commit()
    finally:
        conn.close()
    _emit({
        "status": "ok",
        "panels_created": summary.panels_created,
        "panels_skipped_identical": summary.panels_skipped_identical,
        "panels_needs_review": summary.panels_needs_review,
        "biomarkers_created": summary.biomarkers_created,
        "rows_processed": summary.rows_processed,
        "warnings": summary.warnings,
    })


def cmd_export_csv(args: argparse.Namespace) -> None:
    """Export every confirmed panel as CSV. Read-only — never deferred.

    ``--output`` is a *host* path and this CLI runs host-side, spawned by the
    skill proxy with the daemon's whole filesystem view, so the flag was an
    arbitrary write as the daemon user with every bloodwork panel as its
    payload. Scoped to the roots the sandbox binds for this caller, and the
    resolved path is what gets opened — reopening the argument re-walks the
    symlinks the check just settled.
    """
    from istota.skill_host_paths import resolve_host_path, write_resolved

    resolved = None
    if args.output:
        # Before the query, not after: the export is the caller's whole health
        # record, and a refusal should not read it out of the database first.
        resolved, err = resolve_host_path(
            Path(args.output), writable=True,
            operation="health export-csv --output",
        )
        if err:
            _fail(err)
            # `_fail` exits, three call frames away. The `return` is what makes
            # that independent of it: without one, a `_fail` that ever stopped
            # exiting would fall through to the no-path branch below and print
            # every confirmed panel to stdout, where the model reads it.
            return

    from istota.health import csv_io

    conn = _connect()
    try:
        text = csv_io.export_csv(conn)
    finally:
        conn.close()
    if resolved is not None:
        # `write_resolved` rather than `write_text`: the resolution refused a
        # symlink at the leaf as of its own check, and a plain open would
        # follow one planted after it, out of the workspace and as the daemon
        # user.
        data = text.encode("utf-8")
        write_resolved(resolved, data)
        _emit({"status": "ok", "path": str(resolved), "bytes": len(text)})
        return
    # No path: print the CSV directly so it can be piped.
    sys.stdout.write(text)


def cmd_summary(args: argparse.Namespace) -> None:
    from istota.health import db as health_db
    from istota.health.units import compute_bmi

    conn = _connect()
    try:
        latest = health_db.latest_stats(conn)
        panels = health_db.list_panels(conn, include_drafts=False, limit=3)
        alerts_rows = health_db.flagged_biomarkers_latest(conn, limit=20)
        settings = health_db.get_settings(conn)
    finally:
        conn.close()
    bmi = None
    weight = latest.get("weight")
    height_cm = settings.get("height_cm")
    if weight and height_cm:
        try:
            bmi = compute_bmi(weight.value, float(height_cm))
        except (TypeError, ValueError):
            bmi = None
    _emit({
        "latest_stats": {
            m: {"value": s.value, "unit": s.unit, "measured_at": s.measured_at}
            for m, s in latest.items()
        },
        "bmi": bmi,
        "recent_panels": [
            {"id": p.id, "drawn_at": p.drawn_at, "lab_name": p.lab_name}
            for p in panels
        ],
        "alerts": [
            {
                "name": b.name, "value": b.value, "unit": b.unit, "flag": b.flag,
                "panel_id": p.id, "drawn_at": p.drawn_at, "lab_name": p.lab_name,
            }
            for b, p in alerts_rows
        ],
    })


def _framework_db_path_or_fail() -> Path:
    db = os.environ.get("ISTOTA_DB_PATH", "")
    if not db:
        _fail("ISTOTA_DB_PATH not set — Garmin commands need the framework DB")
    return Path(db)


def cmd_garmin_status(args: argparse.Namespace) -> None:
    from istota.health import garmin as gm

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")
    status = gm.get_status(_framework_db_path_or_fail(), user_id)
    _emit({"status": "ok", "garmin": status})


_GARMIN_SYNC_DELEGATED_POLL_TIMEOUT_S = 60.0
_GARMIN_SYNC_DELEGATED_POLL_INTERVAL_S = 0.5


def cmd_garmin_sync(args: argparse.Namespace) -> None:
    """Run a Garmin sync.

    Two execution modes depending on whether the master key is reachable:

    1. **Direct** — ``ISTOTA_SECRET_KEY`` is in env (operator shell that
       sourced the daemon's ``EnvironmentFile``). The CLI process can
       decrypt + re-encrypt the OAuth blob itself, so the engine runs
       inline. This is the path the web ``/garmin/sync`` endpoint and the
       cron short-circuit already use.

    2. **Delegated** — master key isn't present (LLM Bash call, hand-
       written CRON ``command:`` row, dev shell without the env file
       sourced). The CLI can't run the engine here without `ISTOTA_SECRET_KEY`
       (Fernet-at-rest blast-radius — see ``Skill proxy execution model
       and the master-key boundary`` in project notes), so it enqueues a
       ``skill="health"`` task and polls. The scheduler routes that
       through ``_run_garmin_sync_inprocess``, which runs in the daemon
       process where the key naturally lives.
    """
    from istota import secrets_store

    if secrets_store.secret_key_available():
        _cmd_garmin_sync_direct(args)
    else:
        _cmd_garmin_sync_delegated(args)


def _cmd_garmin_sync_direct(args: argparse.Namespace) -> None:
    """Run the sync engine in this process. Requires ``ISTOTA_SECRET_KEY``
    so the engine can decrypt the stored OAuth blob and persist rotated
    tokens / error flags mid-run.
    """
    from istota.health import garmin_sync as gs
    from istota.health.models import HealthContext

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")
    framework_db = _framework_db_path_or_fail()

    # Build the HealthContext from HEALTH_DB_PATH. The canonical layout
    # (workspace.py) is `{workspace}/health/data/health.db` — three
    # parents up from the db file is the workspace root (H3).
    health_db_path = Path(_db_path())
    workspace_root = health_db_path.parent.parent.parent
    data_dir = health_db_path.parent.parent
    uploads_dir = data_dir / "uploads"
    ctx = HealthContext(
        user_id=user_id,
        workspace_root=workspace_root,
        data_dir=data_dir,
        db_path=health_db_path,
        uploads_dir=uploads_dir,
    )

    user_tz = os.environ.get("ISTOTA_USER_TZ") or None
    res = gs.sync_garmin(
        ctx, framework_db, days_back=args.days_back, user_tz=user_tz,
    )
    payload = res.to_dict()
    payload["status"] = "ok" if not res.auth_error else "error"
    if res.auth_error:
        payload["error"] = "token_expired"
    _emit(payload, error=res.auth_error)


def _cmd_garmin_sync_delegated(args: argparse.Namespace) -> None:
    """Enqueue a Garmin sync task and poll until the scheduler runs it.

    The scheduler short-circuits ``skill="health"`` +
    ``skill_args[0]=="garmin-sync"`` into ``_run_garmin_sync_inprocess``
    (ISSUE-098 fix), which runs in the daemon process where the master
    key is in scope.
    """
    import time
    from istota import db as _db

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")

    db_path_str = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path_str:
        _fail(
            "Garmin sync needs either ISTOTA_SECRET_KEY (direct mode) or "
            "ISTOTA_DB_PATH (delegate to scheduler). Neither is set. Use "
            "the web UI /garmin/sync endpoint instead.",
        )
    db_path = Path(db_path_str)

    skill_args = ["garmin-sync", "--days-back", str(args.days_back)]

    try:
        with _db.get_db(db_path) as conn:
            task_id = _db.create_task(
                conn,
                user_id=user_id,
                source_type="cli",
                skill="health",
                skill_args=json.dumps(skill_args),
            )
            # One-shot. Standard retry backoff (1/4/16 min) would only
            # fire after the CLI poll already timed out — leaving the
            # user thinking the sync failed while a later attempt
            # silently writes data.
            conn.execute(
                "UPDATE tasks SET max_attempts = 1 WHERE id = ?",
                (task_id,),
            )
    except sqlite3.OperationalError as exc:
        # bwrap mounts the DB read-only inside the sandbox; an LLM Bash
        # call hits EROFS here. No way to enqueue from inside — the LLM
        # should fall back to telling the user about /garmin/sync or
        # waiting for the scheduled job.
        _fail(
            f"Cannot enqueue Garmin sync task ({exc}). Sandboxed callers "
            "should use the web UI /garmin/sync endpoint instead.",
        )

    deadline = time.monotonic() + _GARMIN_SYNC_DELEGATED_POLL_TIMEOUT_S
    final = None
    while time.monotonic() < deadline:
        time.sleep(_GARMIN_SYNC_DELEGATED_POLL_INTERVAL_S)
        with _db.get_db(db_path) as conn:
            task = _db.get_task(conn, task_id)
        if task is None:
            _fail(f"Garmin sync task {task_id} disappeared from the DB")
        if task.status in ("completed", "failed", "cancelled"):
            final = task
            break

    if final is None:
        _fail(
            f"Garmin sync task {task_id} did not finish within "
            f"{_GARMIN_SYNC_DELEGATED_POLL_TIMEOUT_S:.0f}s — the scheduler "
            "may not be running.",
        )

    if final.status == "cancelled":
        _fail(f"Garmin sync task {task_id} was cancelled")
    if final.status == "failed":
        _fail(final.error or f"Garmin sync task {task_id} failed")

    # The scheduler stored the in-process engine's JSON payload (already
    # shaped by _run_garmin_sync_inprocess: status, optional error,
    # inserted/skipped/days_processed) in task.result. Pass it through.
    try:
        payload = json.loads(final.result or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {"status": "ok", "result": final.result}
    _emit(payload)


def cmd_garmin_disconnect(args: argparse.Namespace) -> None:
    from istota.health import garmin as gm

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")
    gm.disconnect(_framework_db_path_or_fail(), user_id=user_id)
    _emit({"status": "ok"})


# `istota.health.serialize` is imported inside the function like every other
# `istota.health` import in this file: a module-scope one would load the health
# package — `_loader`, `_migrate`, `db`, `models` — for `--help` and for an
# argparse error too, which are the two paths that skip it today.
def _encounter_to_dict(e) -> dict:
    from istota.health.serialize import encounter_to_dict

    return encounter_to_dict(e)


def _diagnosis_to_dict(d, encounter_ids: list[int] | None = None) -> dict:
    from istota.health.serialize import diagnosis_to_dict

    return diagnosis_to_dict(d, encounter_ids)


def cmd_encounters(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        rows = health_db.list_encounters(
            conn,
            since=args.since,
            until=args.until,
            encounter_type=args.type,
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit({"encounters": [_encounter_to_dict(e) for e in rows]})


def cmd_encounter(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        enc = health_db.get_encounter(conn, args.id)
        if not enc:
            _fail(f"encounter {args.id} not found")
        diagnoses = health_db.diagnoses_for_encounter(conn, args.id)
        panels = health_db.panels_for_encounter(conn, args.id)
    finally:
        conn.close()
    _emit({
        "encounter": _encounter_to_dict(enc),
        "diagnoses": [_diagnosis_to_dict(d) for d in diagnoses],
        "panels": [
            {"id": p.id, "drawn_at": p.drawn_at, "lab_name": p.lab_name}
            for p in panels
        ],
    })


def cmd_add_encounter(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_encounter",
        # dedup_key makes replay-after-partial-success idempotent.
        "dedup_key": uuid.uuid4().hex,
        "encounter_date": args.date,
        "encounter_type": args.type,
        "provider": args.provider,
        "facility": args.facility,
        "specialty": args.specialty,
        "reason": args.reason,
        "notes": args.notes,
    }
    if getattr(args, "ref", None):
        op["ref"] = args.ref
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        eid = health_db.insert_encounter(
            conn,
            encounter_date=args.date,
            encounter_type=args.type,
            provider=args.provider,
            facility=args.facility,
            specialty=args.specialty,
            reason=args.reason,
            notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": eid})


_ENCOUNTER_UPDATE_KEYS = (
    "encounter_date", "encounter_type", "provider", "facility",
    "specialty", "reason", "notes",
)


def cmd_update_encounter(args: argparse.Namespace) -> None:
    updates = {
        k: getattr(args, k) for k in _ENCOUNTER_UPDATE_KEYS
        if getattr(args, k, None) is not None
    }
    if not updates:
        _fail("no fields to update")
    op = {"op": "update_encounter", "encounter_id": args.id, **updates}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.update_encounter(conn, args.id, **updates)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"encounter {args.id} not found")
    _emit({"status": "ok"})


def cmd_delete_encounter(args: argparse.Namespace) -> None:
    op = {"op": "delete_encounter", "encounter_id": args.id}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.delete_encounter(conn, args.id)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"encounter {args.id} not found")
    _emit({"status": "ok"})


def cmd_diagnoses(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        rows = health_db.list_diagnoses(
            conn, status=args.status, limit=args.limit,
        )
        links = health_db.encounter_ids_for_diagnoses(
            conn, [d.id for d in rows],
        )
    finally:
        conn.close()
    _emit({
        "diagnoses": [
            _diagnosis_to_dict(d, links.get(d.id, [])) for d in rows
        ],
    })


def cmd_diagnosis(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        d = health_db.get_diagnosis(conn, args.id)
        if not d:
            _fail(f"diagnosis {args.id} not found")
        linked = health_db.encounters_for_diagnosis(conn, args.id)
    finally:
        conn.close()
    _emit({
        "diagnosis": _diagnosis_to_dict(d, [e.id for e in linked]),
        "encounters": [_encounter_to_dict(e) for e in linked],
    })


def _resolve_encounter_arg(op: dict, raw: str) -> None:
    """Put an encounter operand on an op as either a ref or a real id.

    ``@name`` refers to an encounter created earlier in the same sandboxed
    batch (the deferred replayer resolves it), matching ``add-panel --ref`` and
    ``attach-document --to encounter:@name``.
    """
    if raw.startswith("@"):
        op["encounter_ref"] = raw[1:]
        return
    try:
        op["encounter_id"] = int(raw)
    except (TypeError, ValueError):
        _fail(f"encounter must be an integer id or @ref, got {raw!r}")


def cmd_link_encounter(args: argparse.Namespace) -> None:
    op = {"op": "link_diagnosis_encounter", "diagnosis_id": int(args.id)}
    _resolve_encounter_arg(op, args.encounter)
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    if "encounter_ref" in op:
        _fail("@ref only resolves inside a task; pass a numeric encounter id")
    from istota.health import db as health_db

    conn = _connect()
    try:
        if health_db.get_diagnosis(conn, args.id) is None:
            _fail(f"diagnosis {args.id} not found")
        if health_db.get_encounter(conn, op["encounter_id"]) is None:
            _fail(f"encounter {op['encounter_id']} not found")
        created = health_db.link_diagnosis_encounter(
            conn, int(args.id), op["encounter_id"],
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "created": created})


def cmd_unlink_encounter(args: argparse.Namespace) -> None:
    op = {"op": "unlink_diagnosis_encounter", "diagnosis_id": int(args.id)}
    _resolve_encounter_arg(op, args.encounter)
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    if "encounter_ref" in op:
        _fail("@ref only resolves inside a task; pass a numeric encounter id")
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.unlink_diagnosis_encounter(
            conn, int(args.id), op["encounter_id"],
        )
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(
            f"diagnosis {args.id} is not linked to encounter "
            f"{op['encounter_id']}",
        )
    _emit({"status": "ok"})


def cmd_add_diagnosis(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_diagnosis",
        "dedup_key": uuid.uuid4().hex,
        "name": args.name,
        "status": args.status,
        "icd10": args.icd10,
        "date_diagnosed": args.date_diagnosed,
        "date_resolved": args.date_resolved,
        "encounter_id": args.encounter_id,
        "severity": args.severity,
        "notes": args.notes,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        did = health_db.insert_diagnosis(
            conn,
            name=args.name,
            status=args.status,
            icd10=args.icd10,
            date_diagnosed=args.date_diagnosed,
            date_resolved=args.date_resolved,
            encounter_id=args.encounter_id,
            severity=args.severity,
            notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": did})


_DIAGNOSIS_UPDATE_KEYS = (
    "name", "icd10", "status", "date_diagnosed", "date_resolved",
    "encounter_id", "severity", "notes",
)


def cmd_update_diagnosis(args: argparse.Namespace) -> None:
    updates = {
        k: getattr(args, k) for k in _DIAGNOSIS_UPDATE_KEYS
        if getattr(args, k, None) is not None
    }
    if not updates:
        _fail("no fields to update")
    op = {"op": "update_diagnosis", "diagnosis_id": args.id, **updates}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.update_diagnosis(conn, args.id, **updates)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"diagnosis {args.id} not found")
    _emit({"status": "ok"})


def cmd_resolve_diagnosis(args: argparse.Namespace) -> None:
    """Shorthand for ``update-diagnosis --status resolved --date-resolved …``."""
    from datetime import date as _date

    resolved_date = args.date or _date.today().isoformat()
    op = {
        "op": "update_diagnosis",
        "diagnosis_id": args.id,
        "status": "resolved",
        "date_resolved": resolved_date,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.update_diagnosis(
            conn, args.id,
            status="resolved", date_resolved=resolved_date,
        )
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"diagnosis {args.id} not found")
    _emit({"status": "ok", "id": args.id, "date_resolved": resolved_date})


def cmd_delete_diagnosis(args: argparse.Namespace) -> None:
    op = {"op": "delete_diagnosis", "diagnosis_id": args.id}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.delete_diagnosis(conn, args.id)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"diagnosis {args.id} not found")
    _emit({"status": "ok"})


def cmd_history_summary(args: argparse.Namespace) -> None:
    from datetime import date as _date, timedelta as _td

    from istota.health import db as health_db

    one_year_ago = (_date.today() - _td(days=365)).isoformat()
    conn = _connect()
    try:
        active = health_db.list_diagnoses(conn, status="active", limit=500)
        chronic = health_db.list_diagnoses(conn, status="chronic", limit=500)
        recent = health_db.list_encounters(
            conn, since=one_year_ago, limit=500,
        )
        procedures = health_db.list_encounters(
            conn, encounter_type="procedure", limit=5,
        )
    finally:
        conn.close()
    _emit({
        "active_diagnoses": [_diagnosis_to_dict(d) for d in active],
        "chronic_diagnoses": [_diagnosis_to_dict(d) for d in chronic],
        "recent_encounters": [_encounter_to_dict(e) for e in recent],
        "recent_procedures": [_encounter_to_dict(e) for e in procedures],
    })


def _immunization_to_dict(i) -> dict:
    from istota.health.serialize import immunization_to_dict

    return immunization_to_dict(i)


def _coverage_to_dict(c) -> dict:
    from istota.health.serialize import coverage_to_dict

    return coverage_to_dict(c)


def cmd_immunizations(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        rows = health_db.list_immunizations(
            conn,
            name=args.name,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit({"immunizations": [_immunization_to_dict(r) for r in rows]})


def cmd_immunization(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        row = health_db.get_immunization(conn, args.id)
        if not row:
            _fail(f"immunization {args.id} not found")
        encounter = None
        if row.encounter_id is not None:
            encounter = health_db.get_encounter(conn, row.encounter_id)
    finally:
        conn.close()
    _emit({
        "immunization": _immunization_to_dict(row),
        "encounter": _encounter_to_dict(encounter) if encounter else None,
    })


_IMMUNIZATION_FIELDS = (
    "product_name", "manufacturer", "dose_label", "lot_number", "route",
    "site", "administered_by", "facility", "cvx_code", "notes",
)


def cmd_add_immunization(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_immunization",
        "dedup_key": uuid.uuid4().hex,
        "name": args.name,
        "date_given": args.date,
    }
    for k in _IMMUNIZATION_FIELDS:
        v = getattr(args, k.replace("-", "_"), None)
        if v is not None:
            op[k] = v
    if getattr(args, "encounter_id", None) is not None:
        op["encounter_id"] = int(args.encounter_id)
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        kwargs = {k: op[k] for k in op if k not in ("op", "dedup_key")}
        iid = health_db.insert_immunization(conn, **kwargs)
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": iid})


def cmd_update_immunization(args: argparse.Namespace) -> None:
    updates: dict = {}
    for k in ("name", "date_given") + _IMMUNIZATION_FIELDS:
        attr = k.replace("-", "_")
        v = getattr(args, attr, None)
        if v is not None:
            updates[k] = v
    if getattr(args, "encounter_id", None) is not None:
        updates["encounter_id"] = int(args.encounter_id)
    if not updates:
        _fail("no fields to update")
    op = {"op": "update_immunization", "immunization_id": args.id, **updates}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.update_immunization(conn, args.id, **updates)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"immunization {args.id} not found")
    _emit({"status": "ok"})


def cmd_delete_immunization(args: argparse.Namespace) -> None:
    op = {"op": "delete_immunization", "immunization_id": args.id}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        n = health_db.delete_immunization(conn, args.id)
        conn.commit()
    finally:
        conn.close()
    if not n:
        _fail(f"immunization {args.id} not found")
    _emit({"status": "ok"})


def cmd_vaccine_refs(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        refs = health_db.list_immunization_refs(conn)
    finally:
        conn.close()
    _emit({"refs": [
        {
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
        for r in refs
    ]})


def cmd_coverage(args: argparse.Namespace) -> None:
    from istota.health import db as health_db
    from istota.health.immunizations import compute_coverage

    conn = _connect()
    try:
        refs = health_db.list_immunization_refs(conn)
        rows = health_db.list_immunizations(conn, limit=5000)
    finally:
        conn.close()
    coverage = compute_coverage(refs, rows)
    if args.due_soon:
        coverage = [c for c in coverage if c.status == "due_soon"]
    if args.overdue:
        coverage = [c for c in coverage if c.status == "overdue"]
    _emit({"coverage": [_coverage_to_dict(c) for c in coverage]})


def cmd_import_immunizations(args: argparse.Namespace) -> None:
    from istota.health import db as health_db
    from istota.health.parser import parse_paste

    raw = args.paste
    if raw.startswith("@"):
        # @path means: read the paste from this file.
        src = Path(raw[1:])
        if not src.is_file():
            _fail(f"paste file not found: {src}")
        raw = src.read_text(encoding="utf-8")

    conn = _connect()
    try:
        refs = health_db.list_immunization_refs(conn)
    finally:
        conn.close()
    parsed = parse_paste(raw, refs)
    rows = [
        {
            "name": r.name,
            "product_name": r.product_name,
            "date_given": r.date_given,
            "source_line": r.source_line,
            "confidence": r.confidence,
            "notes": r.notes,
        }
        for r in parsed
    ]
    if args.dry_run:
        _emit({"status": "ok", "dry_run": True, "rows": rows})
        return
    if not args.confirm:
        _fail("import requires either --dry-run or --confirm")

    # Reject rows without a date — they need user confirmation in the web
    # UI before writing.
    missing_date = [
        i for i, r in enumerate(rows) if not r["date_given"]
    ]
    if missing_date:
        # Name the lines. A row can land here because the source printed no
        # date at all *or* because it printed one that is not a real day
        # (2026-02-31, 13/45/2026 — see istota.date_parse), and "missing
        # date_given" reads as a lie against a line that visibly has one.
        offenders = "; ".join(
            rows[i]["source_line"] or rows[i]["name"] for i in missing_date[:5]
        )
        more = "" if len(missing_date) <= 5 else f" (+{len(missing_date) - 5} more)"
        _fail(
            f"{len(missing_date)} row(s) have no usable date_given — "
            f"either none was printed or the printed one is not a real "
            f"date: {offenders}{more}. Add --dry-run, fix the source, "
            "and retry"
        )

    op = {
        "op": "bulk_insert_immunizations",
        "dedup_key_prefix": uuid.uuid4().hex,
        "rows": rows,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "count": len(rows)})
        return
    conn = _connect()
    try:
        ids: list[int] = []
        for i, r in enumerate(rows):
            iid = health_db.insert_immunization(
                conn,
                name=r["name"],
                date_given=r["date_given"],
                product_name=r.get("product_name"),
                notes=r.get("notes"),
                source="import",
                dedup_key=f"{op['dedup_key_prefix']}:{i}",
            )
            ids.append(iid)
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "ids": ids, "count": len(ids)})


def cmd_explain_immunization(args: argparse.Namespace) -> None:
    from istota.health import db as health_db
    from istota.health.immunization_explainer import get_explainer
    from istota.health.immunizations import compute_coverage

    conn = _connect()
    try:
        ref = health_db.find_immunization_ref_by_alias(conn, args.name)
        if ref is None:
            _fail(f"vaccine {args.name!r} not found in canonical refs")
        refs = health_db.list_immunization_refs(conn)
        rows = health_db.list_immunizations(conn, limit=5000)
    finally:
        conn.close()
    coverage = compute_coverage(refs, rows)
    entry = next((c for c in coverage if c.name == ref.name), None)
    status = entry.status if entry else "never_recorded"
    _emit(get_explainer(
        name=ref.name,
        display_name=ref.display_name,
        status=status,
    ))


def cmd_settings(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        s = health_db.get_settings(conn)
    finally:
        conn.close()
    _emit({"settings": s})


def cmd_set(args: argparse.Namespace) -> None:
    """``health set <key> <value>``.

    Whitelisted keys: ``dob``, ``height``, ``sex``, plus dotted
    ``display.weight``, ``display.height``, ``display.temp``.
    """
    key = args.key
    raw = args.value

    if key == "dob":
        op_val: Any = raw
    elif key == "height":
        h = _parse_height(raw)
        if h is None:
            _fail(f"could not parse height: {raw!r}")
        key = "height_cm"
        op_val = h
    elif key == "sex":
        if raw.upper() not in ("M", "F"):
            _fail("sex must be 'M' or 'F'")
        op_val = raw.upper()
    elif key.startswith("display."):
        dim = key.split(".", 1)[1]
        if dim not in ("weight", "height", "temp"):
            _fail(f"unknown display dimension {dim!r}")
        op_val = {dim: raw}
        key = "display_units_merge"
    else:
        _fail(f"unknown settings key {key!r}")

    op = {"op": "set_setting", "key": key, "value": op_val}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        if key == "display_units_merge":
            existing = health_db.get_settings(conn).get("display_units") or {}
            existing.update(op_val)
            health_db.set_setting(conn, "display_units", existing)
        else:
            health_db.set_setting(conn, key, op_val)
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "key": key, "value": op_val})


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Health tracking CLI")
    sub = parser.add_subparsers(dest="command")

    log = sub.add_parser("log", help="Record a stat measurement")
    log.add_argument("metric", help="Metric key (weight, resting_hr, body_fat_pct, …)")
    log.add_argument("value", help="Numeric value")
    log.add_argument("--unit", default=None, help="Source unit; default = canonical metric unit")
    log.add_argument("--date", default=None, help="ISO 8601 timestamp (default: now)")
    log.add_argument("--notes", default=None)

    stats = sub.add_parser("stats", help="List stat entries")
    stats.add_argument("--metric")
    stats.add_argument("--since")
    stats.add_argument("--until")
    stats.add_argument("--limit", type=int, default=100)

    sub.add_parser("latest", help="Latest value per metric")

    panels = sub.add_parser("panels", help="List bloodwork panels")
    panels.add_argument("--since")
    panels.add_argument("--limit", type=int, default=20)

    panel = sub.add_parser("panel", help="Show a single panel with biomarkers")
    panel.add_argument("id", type=int)

    add_panel = sub.add_parser("add-panel", help="Create a panel manually")
    add_panel.add_argument("--drawn-at", dest="drawn_at", required=True)
    add_panel.add_argument("--lab")
    add_panel.add_argument("--type", dest="type")
    add_panel.add_argument("--notes")
    add_panel.add_argument(
        "--ref",
        help="Symbolic name for this panel so add-biomarker calls in the same "
        "sandboxed task can reference it as @NAME before the real id exists "
        "(deferred chaining, ISSUE-092).",
    )

    add_bio = sub.add_parser("add-biomarker", help="Add a biomarker to a panel")
    add_bio.add_argument(
        "panel_id", type=_panel_id_arg,
        help="Panel id (int), or @NAME to reference a panel created by an "
        "add-panel --ref NAME call earlier in the same sandboxed task.",
    )
    add_bio.add_argument("name")
    add_bio.add_argument("value")
    add_bio.add_argument("unit")
    add_bio.add_argument("--ref-low", dest="ref_low", type=float)
    add_bio.add_argument("--ref-high", dest="ref_high", type=float)
    add_bio.add_argument("--flag", choices=["H", "L", "C"])

    trend = sub.add_parser("trend", help="Time series for a biomarker")
    trend.add_argument("name")
    trend.add_argument("--since")
    trend.add_argument("--until")

    upload = sub.add_parser("upload", help="Register a file as a draft panel source")
    upload.add_argument("file_path")
    upload.add_argument("--drawn-at", dest="drawn_at", required=True)
    upload.add_argument("--lab")

    import_csv = sub.add_parser(
        "import-csv",
        help="Import a bloodwork CSV (Date,Lab,Marker (unit) layout)",
    )
    import_csv.add_argument("file_path")

    export_csv = sub.add_parser(
        "export-csv",
        help="Export confirmed panels as a CSV (prints to stdout if no path)",
    )
    export_csv.add_argument("--output", "-o", default=None)

    sub.add_parser("summary", help="Dashboard-style snapshot")

    sub.add_parser("settings", help="Show current settings")

    set_p = sub.add_parser("set", help="Update a setting")
    set_p.add_argument("key", help="dob | height | sex | display.weight | display.height | display.temp")
    set_p.add_argument("value")

    # -- encounters & diagnoses --------------------------------------------
    encs = sub.add_parser("encounters", help="List medical encounters")
    encs.add_argument("--since")
    encs.add_argument("--until")
    encs.add_argument("--type")
    encs.add_argument("--limit", type=int, default=50)

    enc_show = sub.add_parser(
        "encounter", help="Show a single encounter with linked diagnoses/panels",
    )
    enc_show.add_argument("id", type=int)

    add_enc = sub.add_parser("add-encounter", help="Record a medical encounter")
    add_enc.add_argument("--date", dest="date", required=True)
    add_enc.add_argument(
        "--type", dest="type", required=True,
        help="visit | procedure | screening | hospitalization | er | "
             "telehealth | imaging | dental | other",
    )
    add_enc.add_argument("--provider")
    add_enc.add_argument("--facility")
    add_enc.add_argument("--specialty")
    add_enc.add_argument("--reason")
    add_enc.add_argument("--notes")
    add_enc.add_argument(
        "--ref",
        help="Symbolic name for this encounter so a later attach-document "
             "in the same sandboxed task can reference it as "
             "encounter:@NAME before the real id exists",
    )

    upd_enc = sub.add_parser("update-encounter", help="Update an encounter")
    upd_enc.add_argument("id", type=int)
    upd_enc.add_argument(
        "--date", dest="encounter_date",
        help="New encounter date (YYYY-MM-DD)",
    )
    upd_enc.add_argument("--type", dest="encounter_type")
    upd_enc.add_argument("--provider")
    upd_enc.add_argument("--facility")
    upd_enc.add_argument("--specialty")
    upd_enc.add_argument("--reason")
    upd_enc.add_argument("--notes")

    del_enc = sub.add_parser("delete-encounter", help="Delete an encounter")
    del_enc.add_argument("id", type=int)

    diags = sub.add_parser("diagnoses", help="List diagnoses")
    diags.add_argument(
        "--status",
        choices=["active", "resolved", "chronic", "all"],
        default=None,
    )
    diags.add_argument("--limit", type=int, default=100)

    diag_show = sub.add_parser(
        "diagnosis", help="Show a single diagnosis with linked encounter",
    )
    diag_show.add_argument("id", type=int)

    add_diag = sub.add_parser("add-diagnosis", help="Record a diagnosis")
    add_diag.add_argument("name")
    add_diag.add_argument(
        "--status", choices=["active", "resolved", "chronic"], default="active",
    )
    add_diag.add_argument("--icd10")
    add_diag.add_argument("--date-diagnosed", dest="date_diagnosed")
    add_diag.add_argument("--date-resolved", dest="date_resolved")
    add_diag.add_argument(
        "--encounter-id", dest="encounter_id", type=int, default=None,
    )
    add_diag.add_argument(
        "--severity", choices=["mild", "moderate", "severe"], default=None,
    )
    add_diag.add_argument("--notes")

    upd_diag = sub.add_parser("update-diagnosis", help="Update a diagnosis")
    upd_diag.add_argument("id", type=int)
    upd_diag.add_argument("--name")
    upd_diag.add_argument("--icd10")
    upd_diag.add_argument(
        "--status", choices=["active", "resolved", "chronic"], default=None,
    )
    upd_diag.add_argument("--date-diagnosed", dest="date_diagnosed")
    upd_diag.add_argument("--date-resolved", dest="date_resolved")
    upd_diag.add_argument(
        "--encounter-id", dest="encounter_id", type=int, default=None,
    )
    upd_diag.add_argument(
        "--severity", choices=["mild", "moderate", "severe"], default=None,
    )
    upd_diag.add_argument("--notes")

    res_diag = sub.add_parser(
        "resolve-diagnosis",
        help="Mark a diagnosis resolved (shorthand for update-diagnosis)",
    )
    res_diag.add_argument("id", type=int)
    res_diag.add_argument(
        "--date", help="Resolution date (default: today)",
    )

    del_diag = sub.add_parser("delete-diagnosis", help="Delete a diagnosis")
    del_diag.add_argument("id", type=int)

    link_enc = sub.add_parser(
        "link-encounter",
        help="Link a condition to another encounter it was seen at",
    )
    link_enc.add_argument("id", type=int, help="Diagnosis id")
    link_enc.add_argument(
        "--encounter", required=True,
        help="Encounter id, or @ref for one created earlier in this task",
    )

    unlink_enc = sub.add_parser(
        "unlink-encounter",
        help="Remove one encounter link (the condition itself is kept)",
    )
    unlink_enc.add_argument("id", type=int, help="Diagnosis id")
    unlink_enc.add_argument(
        "--encounter", required=True,
        help="Encounter id, or @ref for one created earlier in this task",
    )

    sub.add_parser(
        "history-summary",
        help="New-doctor packet: active conditions + recent encounters",
    )

    imms = sub.add_parser("immunizations", help="List immunization records")
    imms.add_argument("--name", default=None)
    imms.add_argument("--since", default=None)
    imms.add_argument("--until", default=None)
    imms.add_argument("--limit", type=int, default=200)

    imm_one = sub.add_parser("immunization", help="Show a single immunization")
    imm_one.add_argument("id", type=int)

    add_imm = sub.add_parser("add-immunization", help="Record an immunization")
    add_imm.add_argument("--name", required=True,
                         help="Vaccine canonical name (e.g. Influenza, Tdap)")
    add_imm.add_argument("--date", required=True, help="ISO date YYYY-MM-DD")
    add_imm.add_argument("--product-name", dest="product_name", default=None)
    add_imm.add_argument("--manufacturer", default=None)
    add_imm.add_argument("--dose-label", dest="dose_label", default=None)
    add_imm.add_argument("--lot-number", dest="lot_number", default=None)
    add_imm.add_argument("--route", default=None,
                         help="IM | SC | oral | nasal")
    add_imm.add_argument("--site", default=None)
    add_imm.add_argument("--administered-by", dest="administered_by",
                         default=None)
    add_imm.add_argument("--facility", default=None)
    add_imm.add_argument("--encounter-id", dest="encounter_id", default=None)
    add_imm.add_argument("--cvx-code", dest="cvx_code", default=None)
    add_imm.add_argument("--notes", default=None)

    upd_imm = sub.add_parser(
        "update-immunization", help="Update an immunization",
    )
    upd_imm.add_argument("id", type=int)
    upd_imm.add_argument("--name", default=None)
    upd_imm.add_argument("--date", dest="date_given", default=None)
    upd_imm.add_argument("--product-name", dest="product_name", default=None)
    upd_imm.add_argument("--manufacturer", default=None)
    upd_imm.add_argument("--dose-label", dest="dose_label", default=None)
    upd_imm.add_argument("--lot-number", dest="lot_number", default=None)
    upd_imm.add_argument("--route", default=None)
    upd_imm.add_argument("--site", default=None)
    upd_imm.add_argument("--administered-by", dest="administered_by",
                         default=None)
    upd_imm.add_argument("--facility", default=None)
    upd_imm.add_argument("--encounter-id", dest="encounter_id", default=None)
    upd_imm.add_argument("--cvx-code", dest="cvx_code", default=None)
    upd_imm.add_argument("--notes", default=None)

    del_imm = sub.add_parser(
        "delete-immunization", help="Delete an immunization",
    )
    del_imm.add_argument("id", type=int)

    sub.add_parser("vaccine-refs", help="Bundled canonical vaccine list")

    cov = sub.add_parser(
        "coverage", help="Coverage status per canonical vaccine",
    )
    cov.add_argument("--due-soon", action="store_true", dest="due_soon")
    cov.add_argument("--overdue", action="store_true")

    imp = sub.add_parser(
        "import-immunizations",
        help="Parse an EHR/MyChart paste; --dry-run previews, --confirm writes",
    )
    imp.add_argument("--paste", required=True,
                     help="Multi-line paste; @PATH reads from a file")
    imp.add_argument("--dry-run", action="store_true", dest="dry_run")
    imp.add_argument("--confirm", action="store_true")

    expl = sub.add_parser(
        "explain-immunization",
        help="Generate a vaccine educational explainer",
    )
    expl.add_argument("name", help="Vaccine name (canonical or alias)")

    docs_p = sub.add_parser(
        "documents", help="List stored documents (scans, letters, cards)",
    )
    docs_p.add_argument(
        "--entity",
        help="Scope to one record, as TYPE:ID (e.g. encounter:42)",
    )
    docs_p.add_argument("--limit", type=int, default=50)

    doc_p = sub.add_parser("document", help="Show one document + its links")
    doc_p.add_argument("document_id", type=int)

    attach_p = sub.add_parser(
        "attach-document",
        help="File a workspace file against a health record",
    )
    attach_p.add_argument(
        "--path", required=True, help="Path to the file to attach",
    )
    attach_p.add_argument(
        "--to", required=True, metavar="TYPE:ID",
        help="Record to attach it to (encounter:42, diagnosis:7, immunization:5)",
    )
    attach_p.add_argument("--notes", default=None)

    detach_p = sub.add_parser(
        "detach-document", help="Remove a document from one record",
    )
    detach_p.add_argument("document_id", type=int)
    detach_p.add_argument(
        "--from", dest="from", required=True, metavar="TYPE:ID",
        help="Record to detach it from",
    )

    sub.add_parser("garmin-status", help="Show Garmin Connect link status")
    g_sync = sub.add_parser("garmin-sync", help="Manually trigger a Garmin daily-summary sync")
    g_sync.add_argument("--days-back", dest="days_back", type=int, default=7)
    sub.add_parser("garmin-disconnect", help="Remove stored Garmin tokens")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "log": cmd_log,
        "stats": cmd_stats,
        "latest": cmd_latest,
        "panels": cmd_panels,
        "panel": cmd_panel,
        "add-panel": cmd_add_panel,
        "add-biomarker": cmd_add_biomarker,
        "trend": cmd_trend,
        "upload": cmd_upload,
        "import-csv": cmd_import_csv,
        "export-csv": cmd_export_csv,
        "summary": cmd_summary,
        "settings": cmd_settings,
        "set": cmd_set,
        "encounters": cmd_encounters,
        "encounter": cmd_encounter,
        "add-encounter": cmd_add_encounter,
        "update-encounter": cmd_update_encounter,
        "delete-encounter": cmd_delete_encounter,
        "diagnoses": cmd_diagnoses,
        "diagnosis": cmd_diagnosis,
        "add-diagnosis": cmd_add_diagnosis,
        "update-diagnosis": cmd_update_diagnosis,
        "resolve-diagnosis": cmd_resolve_diagnosis,
        "delete-diagnosis": cmd_delete_diagnosis,
        "link-encounter": cmd_link_encounter,
        "unlink-encounter": cmd_unlink_encounter,
        "history-summary": cmd_history_summary,
        "garmin-status": cmd_garmin_status,
        "garmin-sync": cmd_garmin_sync,
        "garmin-disconnect": cmd_garmin_disconnect,
        "immunizations": cmd_immunizations,
        "immunization": cmd_immunization,
        "add-immunization": cmd_add_immunization,
        "update-immunization": cmd_update_immunization,
        "delete-immunization": cmd_delete_immunization,
        "vaccine-refs": cmd_vaccine_refs,
        "coverage": cmd_coverage,
        "import-immunizations": cmd_import_immunizations,
        "explain-immunization": cmd_explain_immunization,
        "documents": cmd_documents,
        "document": cmd_document,
        "attach-document": cmd_attach_document,
        "detach-document": cmd_detach_document,
    }

    if args.command not in commands:
        parser.print_help()
        sys.exit(1)

    # Every handler prints its own envelope and returns nothing, so the
    # epilogue's job here is the facade's rule that a raised exception comes
    # back as one JSON line and exit 1 rather than a traceback on stderr.
    run_skill_cli(commands, args, handlers_print=True)
