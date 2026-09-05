"""FastAPI router for the money web API.

The host application (istota) mounts ``router`` at its chosen prefix and
overrides ``require_auth`` via ``app.dependency_overrides`` so that the
session/cookie/OIDC concerns stay with the host. Per-user data config is
resolved per request through :func:`istota.money.resolve_for_user`,
fed by the istota config attached to ``request.app.state.istota_config``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import FileResponse, JSONResponse, Response

from istota.money._loader import UserNotFoundError, resolve_for_user
from istota.money.cli import UserContext
from istota.money.config_store import FILING_STATUSES
from istota.money.core.tax_data import load_tax_rates

logger = logging.getLogger(__name__)

# Per-money-DB backfill locks (see _autoclass_lock). Process-local: the
# invariant they protect is cost, not correctness.
_AUTOCLASS_LOCKS: dict[str, threading.Lock] = {}
_AUTOCLASS_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# Auth dependency — host app overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> dict:
    """Return ``{"username": ..., "display_name": ...}`` or raise 401.

    Default reads ``request.session["user"]`` (Starlette SessionMiddleware).
    Istota overrides this with its own ``_require_api_auth``.
    """
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        # No SessionMiddleware installed.
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def verify_origin(request: Request) -> None:
    """CSRF check stub for mutating routes — host overrides via dependency_overrides.

    Default is a no-op so the router stays usable in isolation (tests). The host
    app installs a real Origin/Referer check. Same shape as ``require_auth``.
    """
    return None


def get_user_config(
    request: Request,
    user: dict = Depends(require_auth),
) -> UserContext:
    istota_config = getattr(request.app.state, "istota_config", None)
    try:
        return resolve_for_user(user["username"], istota_config)
    except UserNotFoundError:
        raise HTTPException(404, "user not configured")


def _resolve_today(request: Request, username: str):
    """Return today's date in the user's configured timezone.

    Estimated-tax payment quarters hinge on the date (e.g. the Q2 payment is due
    June 15). Resolving the quarter from the server's UTC clock pushed a
    Pacific-time user past the boundary a day early — on June 15 evening Pacific
    the server already saw June 16 and jumped to Q3. Use the user's own tz.
    """
    from datetime import date, datetime

    istota_config = getattr(request.app.state, "istota_config", None)
    tz_name = "UTC"
    if istota_config is not None:
        try:
            uc = istota_config.get_user(username)
            if uc is not None and getattr(uc, "timezone", None):
                tz_name = uc.timezone
        except Exception:
            pass
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


def _load_invoicing_config(user_ctx: UserContext):
    """Load invoicing config, preferring DB over the legacy TOML path."""
    from istota.money import config_store
    from istota.money.core.invoicing import parse_invoicing_config

    db_path = getattr(user_ctx, "db_path", None)
    if db_path is not None and config_store.has_invoicing_data(db_path):
        return config_store.load_invoicing(db_path)
    if user_ctx.invoicing_config_path and user_ctx.invoicing_config_path.exists():
        return parse_invoicing_config(user_ctx.invoicing_config_path)
    return None


def _load_tax_config(user_ctx: UserContext):
    """Load tax config, preferring DB over the legacy TOML path."""
    from istota.money import config_store
    from istota.money.core.tax import parse_tax_config

    db_path = getattr(user_ctx, "db_path", None)
    if db_path is not None and config_store.has_tax_data(db_path):
        return config_store.load_tax(db_path)
    if user_ctx.tax_config_path and user_ctx.tax_config_path.exists():
        return parse_tax_config(user_ctx.tax_config_path)
    return None


def _resolve_user_ledger(user_ctx: UserContext, ledger_name: str | None):
    if not user_ctx.ledgers:
        return None
    if ledger_name:
        for entry in user_ctx.ledgers:
            if entry["name"].lower() == ledger_name.lower():
                return entry["path"]
        return None
    return user_ctx.ledgers[0]["path"]


# ---------------------------------------------------------------------------
# Router — caller chooses the prefix
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get("/me")
async def api_me(user: dict = Depends(require_auth)):
    return {
        "username": user["username"],
        "display_name": user.get("display_name", user["username"]),
    }


@router.get("/accounts")
async def api_accounts(
    ledger: str | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Return account tree with balances for the authenticated user."""
    from istota.money.core.ledger import list_open_accounts, run_bean_query

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    where = f" WHERE year = {int(year)}" if year else ""
    bql = f"SELECT account, sum(position){where} GROUP BY account ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, bql)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    # On the all-time view, surface accounts that have been opened but
    # not yet posted to. Without this, a freshly-seeded ledger renders
    # as "No accounts found" because BQL aggregates over postings only.
    if year is None:
        seen = {r["account"] for r in rows}
        try:
            extra = list_open_accounts(ledger_path)
        except Exception:
            extra = []
        for acct in extra:
            if acct not in seen:
                rows.append({"account": acct, "sum(position)": ""})
        rows.sort(key=lambda r: r["account"])
    return {"status": "ok", "accounts": rows}


@router.get("/transactions")
async def api_transactions(
    ledger: str | None = None,
    account: str | None = None,
    year: int | None = None,
    filter: str | None = None,
    page: int = 1,
    per_page: int = 100,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import run_bean_query, _sanitize_bql_string

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    conditions = []
    if account:
        safe = _sanitize_bql_string(account)
        conditions.append(f"account ~ '{safe}'")
    else:
        conditions.append("account ~ '^(Income|Expenses):'")
    if year:
        conditions.append(f"year = {int(year)}")
    if filter:
        safe = _sanitize_bql_string(filter)
        if safe.startswith("#"):
            tag = safe[1:]
            conditions.append(f"'{tag}' IN tags")
        else:
            conditions.append(f"(payee ~ '{safe}' OR narration ~ '{safe}')")

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    bql = (
        f"SELECT date, flag, payee, narration, account, position, tags,"
        f" entry_meta('id') as id"
        f"{where} ORDER BY date DESC"
    )

    try:
        rows = run_bean_query(ledger_path, bql)
        total = len(rows)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "status": "ok",
            "transactions": rows[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/postings")
async def api_postings(
    date: str,
    payee: str = "",
    narration: str = "",
    account: str = "",
    position: str = "",
    ledger: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import run_bean_query, _sanitize_bql_string

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    conditions = [f"date = {date}"]
    if payee:
        safe = _sanitize_bql_string(payee)
        conditions.append(f"payee = '{safe}'")
    if narration:
        safe = _sanitize_bql_string(narration)
        conditions.append(f"narration = '{safe}'")

    where = " WHERE " + " AND ".join(conditions)
    need_grouping = bool(account and position)
    select_cols = (
        "account, position, filename, entry_meta('lineno') as txn_line"
        if need_grouping else "account, position"
    )
    bql = f"SELECT {select_cols}{where} ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, bql)
        if need_grouping:
            from collections import defaultdict
            groups: dict[tuple, list] = defaultdict(list)
            for row in rows:
                key = (row.get("filename", ""), row.get("txn_line", ""))
                groups[key].append({"account": row["account"], "position": row["position"]})
            for postings in groups.values():
                if any(
                    p["account"].strip() == account.strip()
                    and p["position"].strip() == position.strip()
                    for p in postings
                ):
                    return {"status": "ok", "postings": postings}
        return {"status": "ok", "postings": rows}
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/transactions/update")
async def api_transaction_update(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Edit a transaction in place, located by its stable ``id:`` metadata.

    Body: ``{id, old_account, old_position, new_date?, new_payee?,
    new_narration?, new_account?, new_position?, ledger?}``. Re-validated with
    ``bean-check``; an edit that produces an invalid ledger is rolled back and
    surfaced as a 422.
    """
    from istota.money.core.edit import edit_transaction

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    txn_id = (body.get("id") or "").strip()
    if not txn_id:
        return JSONResponse(
            {"status": "error", "error": "missing transaction id"}, status_code=400
        )

    ledger_path = _resolve_user_ledger(user_ctx, body.get("ledger"))
    if not ledger_path:
        return JSONResponse({"status": "error", "error": "ledger not found"}, status_code=404)

    result = edit_transaction(
        ledger_path,
        txn_id,
        old_account=body.get("old_account"),
        old_position=body.get("old_position"),
        new_date=body.get("new_date"),
        new_payee=body.get("new_payee"),
        new_narration=body.get("new_narration"),
        new_account=body.get("new_account"),
        new_position=body.get("new_position"),
    )

    if result.get("status") == "ok":
        return result
    error = (result.get("error") or "").lower()
    if "not found" in error:
        return JSONResponse(result, status_code=404)
    # Validation failure (rolled back) or bad posting selector.
    return JSONResponse(result, status_code=422)


@router.get("/report/{report_type}")
async def api_report(
    report_type: str,
    ledger: str | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import report

    if report_type not in ("income-statement", "balance-sheet", "cash-flow"):
        return JSONResponse({"error": "unknown report type"}, status_code=400)

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    try:
        return report(ledger_path, report_type, year)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/check")
async def api_check(
    ledger: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import check

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    try:
        return check(ledger_path)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/ledgers")
async def api_ledgers(user_ctx: UserContext = Depends(get_user_config)):
    return {"ledgers": [e["name"] for e in user_ctx.ledgers]}


@router.get("/clients")
async def api_clients(user_ctx: UserContext = Depends(get_user_config)):
    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return {"status": "ok", "clients": []}

    clients = []
    for key, c in config.clients.items():
        entity = config.companies.get(c.entity or config.default_entity, config.company)
        clients.append({
            "key": key,
            "name": c.name,
            "email": c.email,
            "address": c.address,
            "terms": c.terms,
            "entity": c.entity or config.default_entity,
            "entity_name": entity.name,
            "schedule": c.schedule,
            "schedule_day": c.schedule_day,
            "ar_account": c.ar_account or config.default_ar_account,
        })
    return {"status": "ok", "clients": clients}


@router.get("/invoices")
async def api_invoices(
    client: str | None = None,
    show_all: bool = False,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import (
        get_invoice_numbers, get_entries_for_invoice, invoice_issue_date,
    )

    data_dir = user_ctx.data_dir
    if not data_dir:
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    invoice_numbers = get_invoice_numbers(data_dir)
    invoices = []
    for inv_num in invoice_numbers:
        inv_entries = get_entries_for_invoice(data_dir, inv_num)
        if not inv_entries:
            continue
        if client and not any(e.client == client for e in inv_entries):
            continue

        items = build_line_items(inv_entries, config.services)
        total = sum(item.amount for item in items)
        is_paid = all(e.paid_date is not None for e in inv_entries)
        paid_date_val = inv_entries[0].paid_date

        if is_paid and not show_all:
            continue

        client_key = inv_entries[0].client
        client_config = config.clients.get(client_key)
        client_name = client_config.name if client_config else client_key
        # The invoice's own date, not the earliest work on it — which is what
        # this used to show under the same `date` label.
        inv_date = invoice_issue_date(inv_entries)

        invoice_info = {
            "invoice_number": inv_num,
            "client": client_name,
            "client_key": client_key,
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date_val:
            invoice_info["paid_date"] = paid_date_val.isoformat()
        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]
    return {
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    }


@router.get("/business-settings")
async def api_business_settings(user_ctx: UserContext = Depends(get_user_config)):
    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        # `null`, not `{}`: the settings page branches on falsiness to render
        # its "no invoicing configuration" empty state, and `{}` is truthy in
        # JS — so an empty object rendered a card full of `undefined`s and the
        # empty-state branch could never fire.
        return {"status": "ok", "entities": [], "services": [], "defaults": None}

    entities = [{
        "key": key,
        "name": c.name,
        "address": c.address,
        "email": c.email,
        "payment_instructions": c.payment_instructions,
        "logo": c.logo,
        "ar_account": c.ar_account,
        "bank_account": c.bank_account,
        "currency": c.currency,
    } for key, c in config.companies.items()]

    services = [{
        "key": key,
        "display_name": s.display_name,
        "rate": s.rate,
        "type": s.type,
        "income_account": s.income_account,
    } for key, s in config.services.items()]

    defaults = {
        "currency": config.currency,
        "default_entity": config.default_entity,
        "default_ar_account": config.default_ar_account,
        "default_bank_account": config.default_bank_account,
        "invoice_output": config.invoice_output,
        "next_invoice_number": config.next_invoice_number,
        "notifications": config.notifications,
        "days_until_overdue": config.days_until_overdue,
    }
    return {"status": "ok", "entities": entities, "services": services, "defaults": defaults}


@router.get("/invoice-details")
async def api_invoice_details(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import get_entries_for_invoice

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"error": "no data dir"}, status_code=404)

    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no invoicing config"}, status_code=404)

    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        return JSONResponse({"error": "invoice not found"}, status_code=404)

    service_entries = [e for e in entries if e.service != "_manual"]
    items = build_line_items(service_entries, config.services)

    for e in entries:
        if e.service == "_manual":
            from istota.money.core.models import InvoiceLineItem
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item",
                description="",
                quantity=1,
                rate=e.amount or 0,
                discount=0,
                amount=e.amount or 0,
            ))

    return {
        "status": "ok",
        "invoice_number": invoice_number,
        "items": [{
            "description": item.display_name,
            "detail": item.description,
            "quantity": item.quantity,
            "rate": round(item.rate, 2),
            "discount": round(item.discount, 2),
            "amount": round(item.amount, 2),
        } for item in items],
    }


@router.post("/invoices/{invoice_number}/mark-paid")
async def api_invoice_mark_paid(
    invoice_number: str,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Record payment for an invoice (sets paid_date on its work entries).

    Web-level toggle only — sets paid_date, does not post a bank payment
    transaction to the ledger (the CLI's ``invoice paid --bank`` path is
    separate and unchanged).
    """
    from datetime import date

    from istota.money.work import (
        get_entries_for_invoice,
        record_invoice_payment,
    )

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    if not get_entries_for_invoice(data_dir, invoice_number):
        return JSONResponse({"status": "error", "error": "invoice not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    paid_date = (body or {}).get("paid_date") or date.today().isoformat()

    count = record_invoice_payment(data_dir, invoice_number, paid_date)
    return {
        "status": "ok",
        "invoice_number": invoice_number,
        "paid_date": paid_date,
        "count": count,
    }


@router.post("/invoices/{invoice_number}/mark-pending")
async def api_invoice_mark_pending(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Un-pay an invoice (clears paid_date, keeps the invoice number)."""
    from istota.money.work import clear_invoice_payment, get_entries_for_invoice

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    if not get_entries_for_invoice(data_dir, invoice_number):
        return JSONResponse({"status": "error", "error": "invoice not found"}, status_code=404)

    count = clear_invoice_payment(data_dir, invoice_number)
    return {"status": "ok", "invoice_number": invoice_number, "count": count}


@router.get("/invoices/{invoice_number}/pdf")
async def api_invoice_pdf(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Stream the generated PDF for an invoice, or 404 if not on disk."""
    from istota.money.core.invoicing import find_invoice_pdf

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    # Prefer the invoicing config's output dir; fall back to the conventional
    # location. config.invoice_output defaults to "invoices/generated".
    invoice_output_dir = data_dir / "invoices" / "generated"
    try:
        config = _load_invoicing_config(user_ctx)
        if config is not None and getattr(config, "invoice_output", None):
            invoice_output_dir = data_dir / config.invoice_output
    except Exception:
        # Fall back to the default location, but make the miss visible — a
        # custom invoice_output we couldn't read would otherwise 404 silently.
        logger.warning(
            "invoice pdf: could not load invoicing config for %s, using default output dir",
            invoice_number,
            exc_info=True,
        )

    pdf = find_invoice_pdf(invoice_output_dir, invoice_number)
    if pdf is None:
        return JSONResponse({"status": "error", "error": "pdf not found"}, status_code=404)

    return FileResponse(
        path=str(pdf),
        media_type="application/pdf",
        filename=pdf.name,
    )


# =============================================================================
# Work entries
# =============================================================================

# Fields a client may set on create/update. Identity (``uid``) and the
# invoicing lifecycle (``invoice`` / ``invoice_date`` / ``paid_date``) are owned
# by the store and the invoicing path respectively — an edit form must not be
# able to stamp an invoice number, date it, or mark work paid.
_WORK_WRITABLE_FIELDS = (
    "date", "client", "service", "qty", "amount", "discount", "description", "entity",
)

_WORK_STATUSES = ("uninvoiced", "invoiced", "paid", "all")

# Free-text fields, and the subset that must carry an actual value. The store
# writes these into a TOML basic string, so a non-string here is a traceback
# rather than the documented error envelope.
_WORK_STRING_FIELDS = ("client", "service", "description", "entity")
_WORK_REQUIRED_STRING_FIELDS = ("client", "service")

# Control characters other than newline. The serializer escapes these now, but
# there's no legitimate reason for one to arrive in a work entry, and refusing
# at the boundary keeps the corruption class dead even if the escaping regresses.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _work_row(entry, config) -> dict:
    """Enrich a stored work entry into the web row shape.

    ``computed_amount`` runs the same rate resolution invoice generation does
    (``entry_line_item``), so for an uninvoiced entry it is the number that
    will actually appear on the invoice. For an *invoiced* one it is a
    reconstruction at today's rate: nothing stores what an entry was billed
    for (the invoice list re-derives its totals the same way), so a rate change
    retroactively reprices historical rows. The UI marks those as computed at
    the current rate rather than presenting them as the billed figure.
    """
    from istota.money.core.invoicing import entry_line_item
    from istota.money.work import entry_etag

    warnings: list[str] = []
    if not entry.uid:
        warnings.append("no_uid")

    svc = config.services.get(entry.service) if config else None
    client_config = config.clients.get(entry.client) if config else None
    if config is not None:
        if svc is None:
            warnings.append("unknown_service")
        if client_config is None:
            warnings.append("unknown_client")

    computed = None
    if svc is not None:
        computed = round(entry_line_item(entry, svc).amount, 2)

    return {
        "uid": entry.uid,
        "index": entry.id,
        "etag": entry_etag(entry),
        "date": entry.date.isoformat(),
        "client": entry.client,
        "client_name": client_config.name if client_config else entry.client,
        "service": entry.service,
        "service_name": svc.display_name if svc else entry.service,
        "service_type": svc.type if svc else "",
        "qty": entry.qty,
        "amount": entry.amount,
        "discount": entry.discount,
        "description": entry.description,
        "entity": entry.entity,
        "invoice": entry.invoice,
        "paid_date": entry.paid_date.isoformat() if entry.paid_date else None,
        "computed_amount": computed,
        "editable": bool(entry.uid) and not entry.invoice,
        "warnings": warnings,
    }


def _work_totals(rows: list[dict]) -> dict:
    """Bucket counts for the toolbar summary.

    Computed over the client/period-filtered set but *before* the status
    filter, so "4 uninvoiced · $1,800" stays true while you're looking at the
    paid bucket.
    """
    uninvoiced = [r for r in rows if not r["invoice"]]
    return {
        "uninvoiced_count": len(uninvoiced),
        "uninvoiced_amount": round(sum(r["computed_amount"] or 0 for r in uninvoiced), 2),
        "invoiced_count": len([r for r in rows if r["invoice"]]),
        "paid_count": len([r for r in rows if r["paid_date"]]),
    }


def _work_config_or_none(user_ctx: UserContext):
    """Invoicing config for service/client resolution, or None if unreadable.

    A broken or absent config must not make work entries invisible — the
    entries are the record of work done. It only costs display names and
    computed amounts.
    """
    try:
        return _load_invoicing_config(user_ctx)
    except Exception as e:
        logger.warning("work_invoicing_config_unreadable error=%s", e)
        return None


def _work_entry_response(data_dir, uid: str, config) -> dict | None:
    """Re-read an entry by uid and render its row (fresh display index)."""
    from istota.money.work import load_work_entries

    for entry in load_work_entries(data_dir):
        if entry.uid and entry.uid == uid:
            return _work_row(entry, config)
    return None


def _work_mutation_error(result, config) -> JSONResponse:
    """Map a store mutation result onto the right status code."""
    if result.status == "not_found":
        return JSONResponse({"status": "error", "error": "entry not found"}, status_code=404)
    if result.status == "invoiced":
        return JSONResponse(
            {"status": "error", "error": "entry is invoiced"}, status_code=409,
        )
    if result.status == "conflict":
        return JSONResponse(
            {
                "status": "error",
                "error": "entry changed",
                "entry": _work_row(result.entry, config) if result.entry else None,
            },
            status_code=409,
        )
    return JSONResponse(
        {"status": "error", "error": "no fields to update"}, status_code=400,
    )


def _coerce_work_fields(body: dict) -> tuple[dict, str | None]:
    """Pull the writable subset out of a request body, or return an error."""
    from datetime import datetime

    fields: dict = {}
    for key in _WORK_WRITABLE_FIELDS:
        if key not in body:
            continue
        fields[key] = body[key]

    if "date" in fields:
        raw = fields["date"]
        if not isinstance(raw, str):
            return {}, "invalid date"
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return {}, "invalid date format — use YYYY-MM-DD"

    for key in _WORK_STRING_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if not isinstance(value, str):
            return {}, f"invalid {key}"
        if _CONTROL_CHARS_RE.search(value):
            return {}, f"invalid {key} — control characters are not allowed"
        if key in _WORK_REQUIRED_STRING_FIELDS:
            # Normalize here so create and update agree: a blank client was
            # refused on create and silently stored on update.
            value = value.strip()
            if not value:
                return {}, f"{key} is required"
        fields[key] = value

    for key in ("qty", "amount", "discount"):
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            # qty/amount are genuinely nullable ("this service doesn't use
            # one"); discount is not — a null there would break every amount
            # computation downstream, so read it as "no discount".
            if key == "discount":
                fields[key] = 0
            continue
        if isinstance(value, bool):
            return {}, f"invalid {key}"
        if not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return {}, f"invalid {key}"
        # NaN / ±inf reach here from a JSON literal, from float("inf"), and
        # from an overflowing "1e400" — all of which blow up in the
        # serializer's int() conversion rather than storing anything sane.
        if isinstance(value, float) and not math.isfinite(value):
            return {}, f"invalid {key}"
        fields[key] = value

    return fields, None


@router.get("/work")
async def api_work_list(
    client: str | None = None,
    period: str | None = None,
    status: str = "all",
    user_ctx: UserContext = Depends(get_user_config),
):
    """List work entries, enriched with display names and computed amounts."""
    from istota.money.work import load_work_entries

    if status not in _WORK_STATUSES:
        return JSONResponse(
            {"status": "error", "error": f"unknown status: {status}"}, status_code=400,
        )

    data_dir = user_ctx.data_dir
    if not data_dir:
        return {"status": "ok", "entries": [], "totals": _work_totals([])}

    config = _work_config_or_none(user_ctx)
    entries = load_work_entries(data_dir)
    if client:
        wanted = client.lower()
        entries = [e for e in entries if e.client.lower() == wanted]
    if period:
        entries = [e for e in entries if e.date.isoformat().startswith(period)]

    rows = [_work_row(e, config) for e in entries]
    totals = _work_totals(rows)

    if status == "uninvoiced":
        rows = [r for r in rows if not r["invoice"]]
    elif status == "invoiced":
        rows = [r for r in rows if r["invoice"] and not r["paid_date"]]
    elif status == "paid":
        rows = [r for r in rows if r["paid_date"]]

    return {"status": "ok", "entries": rows, "totals": totals}


@router.post("/work")
async def api_work_create(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create a work entry."""
    from istota.money.core.ids import new_txn_id
    from istota.money.work import add_work_entry

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    fields, err = _coerce_work_fields(body)
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=400)

    # _coerce_work_fields has already stripped and non-empty-checked these;
    # what's left is the "absent from the body entirely" case.
    entry_date = fields.get("date")
    client_key = fields.get("client", "")
    service_key = fields.get("service", "")
    if not entry_date or not client_key or not service_key:
        return JSONResponse(
            {"status": "error", "error": "date, client and service are required"},
            status_code=400,
        )

    config = _work_config_or_none(user_ctx)
    # An entry whose service isn't configured is silently dropped at invoice
    # time — the work is recorded and never billed. Refuse it at the door.
    if config is not None and service_key not in config.services:
        return JSONResponse(
            {"status": "error", "error": f"unknown service: {service_key}"},
            status_code=400,
        )

    uid = new_txn_id()
    add_work_entry(
        data_dir,
        entry_date,
        client_key,
        service_key,
        qty=fields.get("qty"),
        amount=fields.get("amount"),
        discount=fields.get("discount") or 0,
        description=fields.get("description") or "",
        entity=fields.get("entity") or "",
        uid=uid,
    )

    row = _work_entry_response(data_dir, uid, config)
    if row is None:
        return JSONResponse(
            {"status": "error", "error": "entry not readable after write"}, status_code=500,
        )
    return {"status": "ok", "entry": row}


@router.patch("/work/{uid}")
async def api_work_update(
    uid: str,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update an uninvoiced work entry, addressed by its stable uid."""
    from istota.money.work import update_work_entry_by_uid

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    fields, err = _coerce_work_fields(body)
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=400)
    if not fields:
        return JSONResponse(
            {"status": "error", "error": "no fields to update"}, status_code=400,
        )

    config = _work_config_or_none(user_ctx)
    service_key = fields.get("service")
    if service_key is not None and config is not None and service_key not in config.services:
        return JSONResponse(
            {"status": "error", "error": f"unknown service: {service_key}"},
            status_code=400,
        )

    result = update_work_entry_by_uid(
        data_dir, uid, expect_etag=body.get("etag") or None, **fields,
    )
    if not result.ok:
        return _work_mutation_error(result, config)

    # The mutation result carries the entry re-read *inside* the write lock,
    # with a display index that already accounts for a date move. Re-reading
    # here instead would return None if the entry vanished in between, and the
    # declared client type has no null case.
    return {"status": "ok", "entry": _work_row(result.entry, config)}


@router.delete("/work/{uid}")
async def api_work_delete(
    uid: str,
    etag: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete an uninvoiced work entry, addressed by its stable uid."""
    from istota.money.work import remove_work_entry_by_uid

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    result = remove_work_entry_by_uid(data_dir, uid, expect_etag=etag or None)
    if not result.ok:
        return _work_mutation_error(result, _work_config_or_none(user_ctx))
    return {"status": "ok", "uid": uid}


@router.get("/tax/estimate")
async def api_tax_estimate(
    request: Request,
    ledger: str | None = None,
    method: str = "annualized",
    quarter: int | None = None,
    year: int | None = None,
    user: dict = Depends(require_auth),
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.models import TaxConfig
    from istota.money.core.tax import (
        annualization_months,
        estimate_quarterly_tax,
        load_tax_inputs,
        payment_quarter_from_date,
        query_se_income,
    )

    try:
        config = _load_tax_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no tax config"}, status_code=404)

    saved = load_tax_inputs(user_ctx.db_path)

    tax_year = year or config.tax_year
    today = _resolve_today(request, user["username"])
    current_quarter = quarter or payment_quarter_from_date(today, tax_year)
    months = annualization_months(current_quarter, tax_year, today)
    use_method = method if method != "annualized" else saved.get("method", method)

    se_income_ytd = 0.0
    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if ledger_path:
        try:
            config_for_query = TaxConfig(
                **{**config.__dict__, "tax_year": tax_year}
            ) if tax_year != config.tax_year else config
            se_income_ytd = query_se_income(ledger_path, config_for_query, months)
        except Exception:
            pass

    def _val(key, fallback):
        v = saved.get(key)
        return v if v is not None else fallback

    result = estimate_quarterly_tax(
        se_income_ytd=se_income_ytd,
        w2_income=_val("w2_income", config.w2_income),
        w2_federal_withholding=_val("w2_federal_withholding", config.w2_federal_withholding),
        w2_state_withholding=_val("w2_state_withholding", config.w2_state_withholding),
        federal_estimated_paid=_val("federal_estimated_paid", config.federal_estimated_paid),
        state_estimated_paid=_val("state_estimated_paid", config.state_estimated_paid),
        filing_status=config.filing_status,
        tax_year=tax_year,
        method=use_method,
        prior_year_federal_tax=config.prior_year_federal_tax,
        prior_year_state_tax=config.prior_year_state_tax,
        enable_qbi=config.enable_qbi_deduction,
        current_quarter=current_quarter,
        w2_months=saved.get("w2_months", 12),
        income_months=months,
        config=config,
        state=config.state,
    )
    return {"status": "ok", **result.__dict__}


@router.post("/tax/estimate")
async def api_tax_estimate_recalculate(
    request: Request,
    ledger: str | None = None,
    user: dict = Depends(require_auth),
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money.core.models import TaxConfig
    from istota.money.core.tax import (
        annualization_months,
        estimate_quarterly_tax,
        payment_quarter_from_date,
        query_se_income,
        save_tax_inputs,
    )

    try:
        config = _load_tax_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no tax config"}, status_code=404)

    body = await request.json()
    tax_year = body.get("year", config.tax_year)
    method = body.get("method", "annualized")
    today = _resolve_today(request, user["username"])
    current_quarter = body.get("quarter") or payment_quarter_from_date(today, tax_year)
    months = annualization_months(current_quarter, tax_year, today)

    def _bval(key, fallback):
        v = body.get(key)
        return v if v is not None else fallback

    w2_income = _bval("w2_income", config.w2_income)
    w2_fed_wh = _bval("w2_federal_withholding", config.w2_federal_withholding)
    w2_state_wh = _bval("w2_state_withholding", config.w2_state_withholding)
    fed_est_paid = _bval("federal_estimated_paid", config.federal_estimated_paid)
    state_est_paid = _bval("state_estimated_paid", config.state_estimated_paid)
    w2_months = _bval("w2_months", 12)

    save_tax_inputs(user_ctx.db_path, {
        "method": method,
        "w2_income": w2_income,
        "w2_federal_withholding": w2_fed_wh,
        "w2_state_withholding": w2_state_wh,
        "federal_estimated_paid": fed_est_paid,
        "state_estimated_paid": state_est_paid,
        "w2_months": w2_months,
    })

    se_income_ytd = 0.0
    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if ledger_path:
        try:
            config_for_query = TaxConfig(
                **{**config.__dict__, "tax_year": tax_year}
            ) if tax_year != config.tax_year else config
            se_income_ytd = query_se_income(ledger_path, config_for_query, months)
        except Exception:
            pass

    result = estimate_quarterly_tax(
        se_income_ytd=se_income_ytd,
        w2_income=w2_income,
        w2_federal_withholding=w2_fed_wh,
        w2_state_withholding=w2_state_wh,
        federal_estimated_paid=fed_est_paid,
        state_estimated_paid=state_est_paid,
        filing_status=config.filing_status,
        tax_year=tax_year,
        method=method,
        prior_year_federal_tax=config.prior_year_federal_tax,
        prior_year_state_tax=config.prior_year_state_tax,
        enable_qbi=config.enable_qbi_deduction,
        current_quarter=current_quarter,
        w2_months=w2_months,
        income_months=months,
        config=config,
        state=config.state,
    )
    return {"status": "ok", **result.__dict__}


# =============================================================================
# Config CRUD routes (DB-backed money config — invoicing / tax / monarch)
# =============================================================================


def _client_to_dict(c) -> dict:
    return {
        "key": c.key, "name": c.name, "address": c.address, "email": c.email,
        "terms": c.terms, "ar_account": c.ar_account, "entity": c.entity,
        "schedule": c.schedule, "schedule_day": c.schedule_day,
        "reminder_days": c.reminder_days, "notifications": c.notifications,
        "days_until_overdue": c.days_until_overdue,
        "ledger_posting": c.ledger_posting,
        "bundles": c.bundles, "separate": c.separate,
    }


def _company_to_dict(c) -> dict:
    return {
        "key": c.key, "name": c.name, "address": c.address, "email": c.email,
        "payment_instructions": c.payment_instructions, "logo": c.logo,
        "ar_account": c.ar_account, "bank_account": c.bank_account,
        "currency": c.currency,
    }


def _service_to_dict(s) -> dict:
    return {
        "key": s.key, "display_name": s.display_name, "rate": s.rate,
        "type": s.type, "income_account": s.income_account,
    }


# --- Collection write plumbing ------------------------------------------------
#
# The `/config/*` collection routes started as a thin passthrough for a trusted
# CLI caller. What follows makes them safe for a browser form: real create
# semantics, shape-checked bodies, and delete guards. The *value* invariants
# (closed sets, ranges, account shapes) live in `config_store` so the CLI and
# the agent get them too; these helpers only cover JSON shape, and map the
# store's `ValueError` onto a 400.

# Which fields each collection accepts, split by JSON type so the coercers can
# check them uniformly. Mirrors `config_store`'s allowed sets.
_CLIENT_TEXT_FIELDS = (
    "name", "address", "email", "ar_account", "entity", "schedule", "notifications",
)
_CLIENT_INT_FIELDS = ("schedule_day", "reminder_days", "days_until_overdue")
_ENTITY_TEXT_FIELDS = (
    "name", "address", "email", "payment_instructions", "logo",
    "ar_account", "bank_account", "currency",
)
_SERVICE_TEXT_FIELDS = ("display_name", "type", "income_account")


def _coerce_text_fields(body: dict, names: tuple[str, ...]) -> tuple[dict, str | None]:
    fields: dict = {}
    for key in names:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            return {}, f"invalid {key} — expected text"
        # _CONTROL_CHARS_RE deliberately excludes \x0a, so a multi-line address
        # or payment-instructions block passes and a stray \r doesn't.
        if _CONTROL_CHARS_RE.search(value):
            return {}, f"invalid {key} — control characters are not allowed"
        fields[key] = value
    return fields, None


def _coerce_int_fields(body: dict, names: tuple[str, ...]) -> tuple[dict, str | None]:
    fields: dict = {}
    for key in names:
        if key not in body:
            continue
        value = body[key]
        # JSON `true` is an int to Python; without this it lands as day 1.
        if isinstance(value, bool) or not isinstance(value, int):
            return {}, f"invalid {key} — expected a whole number"
        fields[key] = value
    return fields, None


def _coerce_client_fields(body: dict) -> tuple[dict, str | None]:
    """Pull the writable subset out of a client body, or return an error.

    Modelled on `_coerce_work_fields`. Two rules the forms depend on: send
    `""` (never `null`) to clear an optional field, since the store skips
    `None` and would silently keep the old value; and omit `bundles` /
    `separate` entirely to preserve what's stored.
    """
    allowed = (
        set(_CLIENT_TEXT_FIELDS) | set(_CLIENT_INT_FIELDS)
        | {"terms", "ledger_posting", "bundles", "separate"}
    )
    unknown = set(body) - allowed
    if unknown:
        return {}, f"unknown keys: {sorted(unknown)}"

    fields, err = _coerce_text_fields(body, _CLIENT_TEXT_FIELDS)
    if err:
        return {}, err
    ints, err = _coerce_int_fields(body, _CLIENT_INT_FIELDS)
    if err:
        return {}, err
    fields.update(ints)

    if "terms" in body:
        terms = body["terms"]
        # The model is `int | str`: 30 and "NET 15" are both meaningful.
        if isinstance(terms, bool) or not isinstance(terms, (int, str)):
            return {}, "invalid terms — expected a number of days or a label"
        if isinstance(terms, str) and _CONTROL_CHARS_RE.search(terms):
            return {}, "invalid terms — control characters are not allowed"
        fields["terms"] = terms

    if "ledger_posting" in body:
        if not isinstance(body["ledger_posting"], bool):
            return {}, "invalid ledger_posting — expected true or false"
        fields["ledger_posting"] = body["ledger_posting"]

    if "separate" in body:
        value = body["separate"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return {}, "invalid separate — expected a list of service keys"
        fields["separate"] = value

    if "bundles" in body:
        value = body["bundles"]
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            return {}, "invalid bundles — expected a list of objects"
        fields["bundles"] = value

    return fields, None


def _coerce_entity_fields(body: dict) -> tuple[dict, str | None]:
    unknown = set(body) - set(_ENTITY_TEXT_FIELDS)
    if unknown:
        return {}, f"unknown keys: {sorted(unknown)}"
    return _coerce_text_fields(body, _ENTITY_TEXT_FIELDS)


def _coerce_service_fields(body: dict) -> tuple[dict, str | None]:
    unknown = set(body) - (set(_SERVICE_TEXT_FIELDS) | {"rate"})
    if unknown:
        return {}, f"unknown keys: {sorted(unknown)}"

    fields, err = _coerce_text_fields(body, _SERVICE_TEXT_FIELDS)
    if err:
        return {}, err

    if "rate" in body:
        rate = body["rate"]
        if isinstance(rate, bool):
            return {}, "invalid rate — expected a number"
        if not isinstance(rate, (int, float, str)):
            return {}, "invalid rate — expected a number"
        # The finite / non-negative rule is the store's; it raises and the
        # route maps it, so a rule stated once surfaces on both surfaces.
        fields["rate"] = rate

    return fields, None


_BODY_INVALID = object()


async def _read_body(request: Request):
    """Parse a JSON object body, or return `_BODY_INVALID`.

    An absent body is `{}` (a no-op update, which the upserts handle). A body
    that is present but unparseable, or parses to something other than an
    object, has to be an error: reading it as `{}` turned a broken request into
    a silent write that created a defaults-only record and answered 200.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return _BODY_INVALID
    return body if isinstance(body, dict) else _BODY_INVALID


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse(
        {"status": "error", "error": message, **extra}, status_code=status,
    )


def _bad_body() -> JSONResponse:
    return _error("invalid body — expected a JSON object", 400)


def _requires_existing(request: Request) -> bool:
    """Whether this PUT should 404 rather than create (`?create=false`)."""
    return request.query_params.get("create", "").lower() in ("false", "0", "no")


def _scan_refusal(scan, kind: str, key: str) -> JSONResponse | None:
    """Map a blocking `ReferenceScan` onto the response to send instead."""
    if scan.scan_failed is not None:
        return _error(
            f"could not check what references this {kind}: {scan.scan_failed}", 500,
        )
    if scan.blocked_reason is not None:
        return _error(scan.blocked_reason, 409, references=scan.references)
    return None


@router.get("/config/invoicing")
async def api_config_invoicing(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "settings": {
            "accounting_path": cfg.accounting_path,
            "invoice_output": cfg.invoice_output,
            "next_invoice_number": cfg.next_invoice_number,
            "default_entity": cfg.default_entity,
            "currency": cfg.currency,
            "default_ar_account": cfg.default_ar_account,
            "default_bank_account": cfg.default_bank_account,
            "notifications": cfg.notifications,
            "days_until_overdue": cfg.days_until_overdue,
        },
    }


@router.put("/config/invoicing")
async def api_config_invoicing_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update scalar invoicing settings.

    Body: a JSON object with any of the scalar setting keys. Unknown keys are
    rejected. Collection edits go through the per-entity routes below.

    Shape-checked like the collection routes rather than `setattr`-ing whatever
    arrives: a string `next_invoice_number` sails into `max(...)` in the
    generator and raises there, and a `default_entity` naming no company is
    what leaves `load_invoicing` falling back to an arbitrary one — the state
    the entity delete guard has to reason about.
    """
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    cfg = config_store.load_invoicing(user_ctx.db_path)
    text_keys = (
        "accounting_path", "invoice_output", "default_entity",
        "default_ar_account", "default_bank_account", "notifications",
    )
    int_keys = ("next_invoice_number", "days_until_overdue")
    bad = set(body) - (set(text_keys) | set(int_keys) | {"currency"})
    if bad:
        return _error(f"unknown keys: {sorted(bad)}", 400)

    fields, err = _coerce_text_fields(body, text_keys)
    if err:
        return _error(err, 400)
    ints, err = _coerce_int_fields(body, int_keys)
    if err:
        return _error(err, 400)
    fields.update(ints)
    if "currency" in body:
        if not isinstance(body["currency"], str):
            return _error("invalid currency — expected text", 400)
        fields["currency"] = body["currency"]

    if fields.get("next_invoice_number") is not None and fields["next_invoice_number"] < 1:
        return _error("invalid next_invoice_number — expected at least 1", 400)
    if fields.get("days_until_overdue") is not None and fields["days_until_overdue"] < 0:
        return _error("invalid days_until_overdue — expected at least 0", 400)
    if fields.get("default_entity") and fields["default_entity"] not in cfg.companies:
        return _error(
            f"unknown entity '{fields['default_entity']}' — create it first", 400,
        )
    try:
        config_store.check_invoicing_scalars(fields)
    except ValueError as exc:
        return _error(str(exc), 400)

    for k, v in fields.items():
        setattr(cfg, k, v)
    config_store.save_invoicing(user_ctx.db_path, cfg, replace_collections=False)
    return {"status": "ok"}


@router.get("/config/companies")
async def api_config_companies(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "companies": [_company_to_dict(c) for c in cfg.companies.values()],
    }


@router.post("/config/companies")
async def api_config_companies_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create an entity. 409 when the key is taken — create means create."""
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    key = body.get("key")
    if not key or not isinstance(key, str):
        return _error("key required", 400)
    fields, err = _coerce_entity_fields({k: v for k, v in body.items() if k != "key"})
    if err:
        return _error(err, 400)
    try:
        comp, state = config_store.upsert_company(
            user_ctx.db_path, key, create_only=True, **fields,
        )
    except config_store.KeyExistsError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "company": _company_to_dict(comp)}


@router.put("/config/companies/{key}")
async def api_config_companies_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update an entity.

    Upsert by default, for `ensure`-style callers. `?create=false` makes it
    strict, which is what the browser forms send: they only ever PUT a record
    they believe exists, so a key another tab deleted meanwhile should 404
    rather than being resurrected as a partial record built from the form's
    fields and defaults for everything else.
    """
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    fields, err = _coerce_entity_fields(body)
    if err:
        return _error(err, 400)
    if _requires_existing(request) and key not in config_store.load_invoicing(
        user_ctx.db_path,
    ).companies:
        return _error(f"entity '{key}' not found", 404)
    try:
        comp, state = config_store.upsert_company(user_ctx.db_path, key, **fields)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "company": _company_to_dict(comp)}


@router.delete("/config/companies/{key}")
async def api_config_companies_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete an entity, refusing while anything still points at it.

    Strict because the failure lands on a generated PDF: a client whose entity
    vanished falls back to whichever company `load_invoicing` picked, so the
    next invoice carries a different legal entity's name, address and payment
    instructions, with nothing on it saying so.
    """
    from istota.money import config_refs, config_store

    if key not in config_store.load_invoicing(user_ctx.db_path).companies:
        return _error(f"entity '{key}' not found", 404)

    scan = config_refs.entity_references(user_ctx.db_path, user_ctx.data_dir, key)
    refusal = _scan_refusal(scan, "entity", key)
    if refusal:
        return refusal

    removed = config_store.delete_company(user_ctx.db_path, key)
    return {"status": "ok", "removed": removed, "references": scan.references}


@router.get("/config/clients")
async def api_config_clients(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "clients": [_client_to_dict(c) for c in cfg.clients.values()],
    }


@router.post("/config/clients")
async def api_config_clients_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create a client. 409 when the key is taken — create means create."""
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    key = body.get("key")
    if not key or not isinstance(key, str):
        return _error("key required", 400)
    fields, err = _coerce_client_fields({k: v for k, v in body.items() if k != "key"})
    if err:
        return _error(err, 400)
    try:
        client, state = config_store.upsert_client(
            user_ctx.db_path, key, create_only=True, **fields,
        )
    except config_store.KeyExistsError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "client": _client_to_dict(client)}


@router.put("/config/clients/{key}")
async def api_config_clients_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update a client. Upsert by default; `?create=false` 404s a missing key."""
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    fields, err = _coerce_client_fields(body)
    if err:
        return _error(err, 400)
    if _requires_existing(request) and key not in config_store.load_invoicing(
        user_ctx.db_path,
    ).clients:
        return _error(f"client '{key}' not found", 404)
    try:
        client, state = config_store.upsert_client(user_ctx.db_path, key, **fields)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "client": _client_to_dict(client)}


@router.delete("/config/clients/{key}")
async def api_config_clients_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete a client.

    The soft case of the three: invoice grouping is by the entry's client key,
    so entries and invoices survive and only the display name degrades to the
    raw key (which the work list already flags as `unknown_client`). The
    reference count comes back so the UI can say what it cost.

    Because the delete destroys nothing, an unreadable work store degrades to
    an unknown count rather than refusing — the two strict guards refuse there,
    but doing so here would strand a user behind a stray `\\r` in a year file.
    """
    from istota.money import config_refs, config_store

    if key not in config_store.load_invoicing(user_ctx.db_path).clients:
        return _error(f"client '{key}' not found", 404)

    scan = config_refs.client_references(user_ctx.db_path, user_ctx.data_dir, key)
    removed = config_store.delete_client(user_ctx.db_path, key)
    return {"status": "ok", "removed": removed, "references": scan.references}


@router.get("/config/services")
async def api_config_services(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "services": [_service_to_dict(s) for s in cfg.services.values()],
    }


@router.post("/config/services")
async def api_config_services_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create a service. 409 when the key is taken — create means create."""
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    key = body.get("key")
    if not key or not isinstance(key, str):
        return _error("key required", 400)
    fields, err = _coerce_service_fields({k: v for k, v in body.items() if k != "key"})
    if err:
        return _error(err, 400)
    try:
        svc, state = config_store.upsert_service(
            user_ctx.db_path, key, create_only=True, **fields,
        )
    except config_store.KeyExistsError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "service": _service_to_dict(svc)}


@router.put("/config/services/{key}")
async def api_config_services_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update a service. Upsert by default; `?create=false` 404s a missing key."""
    from istota.money import config_store
    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    fields, err = _coerce_service_fields(body)
    if err:
        return _error(err, 400)
    if _requires_existing(request) and key not in config_store.load_invoicing(
        user_ctx.db_path,
    ).services:
        return _error(f"service '{key}' not found", 404)
    try:
        svc, state = config_store.upsert_service(user_ctx.db_path, key, **fields)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "state": state, "service": _service_to_dict(svc)}


@router.delete("/config/services/{key}")
async def api_config_services_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete a service, refusing while any work entry names it.

    The strictest of the three guards, because deletion breaks time in both
    directions: `build_line_items` skips an entry whose service is missing, so
    future work goes unbilled, *and* the invoice list rebuilds its totals from
    live config, so every past invoice containing such an entry re-renders
    short. To retire a service, reassign or remove the entries first.
    """
    from istota.money import config_refs, config_store

    if key not in config_store.load_invoicing(user_ctx.db_path).services:
        return _error(f"service '{key}' not found", 404)

    scan = config_refs.service_references(user_ctx.db_path, user_ctx.data_dir, key)
    refusal = _scan_refusal(scan, "service", key)
    if refusal:
        return refusal

    removed = config_store.delete_service(user_ctx.db_path, key)
    return {"status": "ok", "removed": removed, "references": scan.references}


@router.get("/config/tax")
async def api_config_tax(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_tax(user_ctx.db_path)
    return {
        "status": "ok",
        "tax": {
            "filing_status": cfg.filing_status,
            "tax_year": cfg.tax_year,
            "state": cfg.state,
            "w2_income": cfg.w2_income,
            "w2_federal_withholding": cfg.w2_federal_withholding,
            "w2_state_withholding": cfg.w2_state_withholding,
            "federal_estimated_paid": cfg.federal_estimated_paid,
            "state_estimated_paid": cfg.state_estimated_paid,
            "enable_qbi_deduction": cfg.enable_qbi_deduction,
            "prior_year_federal_tax": cfg.prior_year_federal_tax,
            "prior_year_state_tax": cfg.prior_year_state_tax,
        },
    }


@router.put("/config/tax")
async def api_config_tax_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    cfg = config_store.load_tax(user_ctx.db_path)
    allowed = {
        "filing_status", "tax_year", "state",
        "w2_income", "w2_federal_withholding", "w2_state_withholding",
        "federal_estimated_paid", "state_estimated_paid",
        "enable_qbi_deduction",
        "prior_year_federal_tax", "prior_year_state_tax",
    }
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"}, 400,
        )
    if "state" in body:
        # "" is a real choice (no state tax), so it is validated as a value
        # rather than skipped as falsy — but a typo'd code must not be stored,
        # since nothing downstream would ever resolve it.
        raw = (body["state"] or "")
        if not isinstance(body["state"], (str, type(None))):
            return JSONResponse(
                {"status": "error", "error": "state must be a string"}, 400,
            )
        code = raw.strip().upper()
        if code and load_tax_rates().jurisdiction(code) is None:
            return JSONResponse(
                {"status": "error", "error": f"unknown state: {code}"}, 400,
            )
        body = {**body, "state": code}
    if "filing_status" in body and body["filing_status"] not in FILING_STATUSES:
        return JSONResponse(
            {"status": "error",
             "error": f"unknown filing status: {body['filing_status']}"}, 400,
        )
    if "tax_year" in body:
        # Range-checked because the staleness test builds `date(tax_year, 1, 1)`,
        # which raises outside 1-9999 — and that raise lands in the estimate and
        # the settings loader, not here where it could be reported.
        year = body["tax_year"]
        if (isinstance(year, bool) or not isinstance(year, int)
                or not 1900 <= year <= 2200):
            return JSONResponse(
                {"status": "error",
                 "error": f"tax_year must be a year between 1900 and 2200: {year!r}"},
                400,
            )
    for k, v in body.items():
        setattr(cfg, k, v)
    config_store.save_tax(user_ctx.db_path, cfg, replace_collections=False)
    return {"status": "ok"}


@router.get("/config/tax/jurisdictions")
async def api_config_tax_jurisdictions(
    user_ctx: UserContext = Depends(get_user_config),
):
    """The selectable states, whether each taxes income, and what we ship.

    ``has_bundled_data`` drives the "you will need to enter brackets" hint on
    the settings page, so a user is told before they pick rather than after.
    """
    rates = load_tax_rates()
    bundled = set(rates.bundled_state_codes())
    return {
        "status": "ok",
        "jurisdictions": [
            {
                "code": j.code,
                "name": j.name,
                "taxes_income": j.taxes_income,
                "has_bundled_data": j.code in bundled,
                "note": j.note,
            }
            for j in rates.jurisdictions
        ],
    }


def _resolved_field(value, *, overridden: bool) -> dict:
    """One rate field plus whether it is the user's number or the shipped one."""
    return {"value": value, "overridden": overridden}


@router.get("/config/tax/resolved")
async def api_config_tax_resolved(
    year: int | None = None,
    filing_status: str | None = None,
    state: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """The rates actually in use, per field, with provenance.

    All three selectors default to the configured values but are overridable,
    because the settings page previews an edit before it is saved — a year the
    user is not currently filing for, or a state they have just picked from the
    dropdown. Without the override the form shows the previous state's brackets
    beside the new state's name, which reads as a bug.
    """
    from istota.money import config_store

    cfg = config_store.load_tax(user_ctx.db_path)
    tax_year = year or cfg.tax_year
    status = filing_status or cfg.filing_status
    if status not in FILING_STATUSES:
        return JSONResponse(
            {"status": "error", "error": f"unknown filing status: {status}"}, 400,
        )

    rates = load_tax_rates()
    fed_rates = rates.federal_year(tax_year)
    fed_override = config_store.get_tax_schedule(
        user_ctx.db_path, tax_year, config_store.FEDERAL_JURISDICTION, status,
    ) or {}

    def _pick(override_key: str, bundled):
        got = fed_override.get(override_key)
        return _resolved_field(
            got if got is not None else bundled, overridden=got is not None,
        )

    federal = {
        "standard_deduction": _pick(
            "standard_deduction",
            fed_rates.standard_deduction(status) if fed_rates else None,
        ),
        "brackets": _pick(
            "brackets",
            [list(b) for b in fed_rates.brackets(status)] if fed_rates else [],
        ),
        "provenance": _provenance_dict(fed_rates),
    }

    # Payroll scalars are year-keyed and status-agnostic, so they come from the
    # legacy year table rather than a schedule row.
    year_rows = {
        r["tax_year"]: r
        for r in config_store.list_tax_year_rates(user_ctx.db_path)
    }
    payroll_override = year_rows.get(tax_year) or {}
    bundled_payroll = fed_rates.payroll if fed_rates else None
    payroll = {}
    for key, bundled_value in (
        ("ss_wage_base", bundled_payroll.ss_wage_base if bundled_payroll else None),
        ("ss_rate", bundled_payroll.ss_rate if bundled_payroll else None),
        ("medicare_rate", bundled_payroll.medicare_rate if bundled_payroll else None),
        ("se_taxable_fraction",
         bundled_payroll.se_taxable_fraction if bundled_payroll else None),
    ):
        got = payroll_override.get(key)
        payroll[key] = _resolved_field(
            got if got is not None else bundled_value, overridden=got is not None,
        )

    # `state` of "" is a real selection (no state tax) and must not fall back
    # to the configured one, so this keys on None rather than falsiness.
    state_code = cfg.state if state is None else state.strip().upper()
    state_block = None
    if state_code:
        state_block = _resolved_state_block(
            user_ctx.db_path, rates, state_code, tax_year, status,
        )

    return {
        "status": "ok",
        "tax_year": tax_year,
        "filing_status": status,
        "federal": federal,
        "payroll": payroll,
        "state": state_block,
    }


def _provenance_dict(rates) -> dict:
    """The citation block for a resolved rate set.

    `overridden` is False here because this endpoint reports it per *field*
    alongside each value, not once for the whole block.
    """
    from istota.money.core.tax import build_provenance
    return build_provenance(rates, overridden=False)


def _resolved_state_block(db_path, rates, code: str, tax_year: int, status: str) -> dict:
    """The state half of the resolved-rates payload.

    Carries ``available`` plus a reason for the same purpose the estimate does:
    "no brackets yet" and "this state levies none" want different UI, and
    neither is a zero.
    """
    from istota.money import config_store

    jurisdiction = rates.jurisdiction(code)
    if jurisdiction is None:
        # Resolved before the schedule lookup, which validates the jurisdiction
        # and raises. Without this the endpoint 500s on a state the config
        # already holds — and a stored typo has no UI route back, since this is
        # the settings page's own loader.
        return {
            "code": code,
            "name": "",
            "taxes_income": False,
            "available": False,
            "reason": "unknown_state",
            "starts_from": "federal_agi",
            "installment_schedule": None,
            "standard_deduction": _resolved_field(None, overridden=False),
            "brackets": _resolved_field([], overridden=False),
            "provenance": _provenance_dict(None),
        }
    bundled = rates.state_year(code, tax_year)
    override = config_store.get_tax_schedule(db_path, tax_year, code, status) or {}

    ov_brackets = override.get("brackets")
    ov_std = override.get("standard_deduction")
    brackets = ov_brackets if ov_brackets is not None else (
        [list(b) for b in bundled.brackets(status)] if bundled else []
    )
    std_ded = ov_std if ov_std is not None else (
        bundled.standard_deduction(status) if bundled else None
    )

    reason = ""
    if not jurisdiction.taxes_income:
        reason = "no_income_tax"
    elif not brackets:
        reason = "no_brackets"

    meta = rates.state_meta(code)
    return {
        "code": code,
        "name": jurisdiction.name,
        "taxes_income": jurisdiction.taxes_income,
        "available": reason == "",
        "reason": reason,
        "starts_from": meta.starts_from if meta else "federal_agi",
        "installment_schedule": list(meta.installment_schedule) if meta else None,
        "standard_deduction": _resolved_field(std_ded, overridden=ov_std is not None),
        "brackets": _resolved_field(brackets, overridden=ov_brackets is not None),
        "provenance": _provenance_dict(bundled),
    }


# Writable on `tax_year_rates` but read by nothing since the schedules table
# took over — see `config_store.migrate_tax_schedules`.
_LEGACY_YEAR_RATE_KEYS = frozenset({
    "federal_brackets", "ca_brackets",
    "federal_standard_deduction", "ca_standard_deduction",
})


def _validate_brackets(raw) -> str | None:
    """Reject a bracket table that would misbehave inside ``apply_brackets``.

    That function indexes ``b[0]``/``b[1]`` and walks the pairs in order, so a
    ragged pair fails mid-estimate and an unsorted table computes a plausible
    but wrong figure. Both are worth refusing at the boundary instead.
    """
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        return "brackets must be a non-empty array of [threshold, rate] pairs"
    last = None
    for pair in raw:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v)
                       for v in pair)):
            # `isfinite` is not fussiness: Starlette renders with
            # allow_nan=False, so an Infinity that got stored raises on the way
            # back out and 500s both the estimate and the settings page's own
            # loader — leaving no UI route back to the value that broke it.
            return f"malformed bracket: {pair!r} (expected [threshold, rate])"
        threshold, rate = pair
        if threshold < 0:
            return f"bracket threshold must not be negative: {threshold}"
        if not 0 <= rate <= 1:
            return f"bracket rate must be a fraction between 0 and 1: {rate}"
        if last is not None and threshold <= last:
            return "bracket thresholds must ascend and not repeat"
        last = threshold
    return None


@router.get("/config/tax/schedules")
async def api_config_tax_schedules(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    return {
        "status": "ok",
        "schedules": config_store.list_tax_schedules(user_ctx.db_path),
    }


@router.put("/config/tax/schedules/{year}/{jurisdiction}/{filing_status}")
async def api_config_tax_schedules_put(
    year: int, jurisdiction: str, filing_status: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    allowed = {"brackets", "standard_deduction"}
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"}, 400,
        )
    if "brackets" in body:
        problem = _validate_brackets(body["brackets"])
        if problem:
            return JSONResponse({"status": "error", "error": problem}, 400)
    std_ded = body.get("standard_deduction", config_store.UNSET)
    if std_ded is not None and not isinstance(std_ded, config_store._Unset):
        if (isinstance(std_ded, bool) or not isinstance(std_ded, (int, float))
                or not math.isfinite(std_ded)):
            return JSONResponse(
                {"status": "error", "error": "standard_deduction must be a finite number"},
                400,
            )
        if std_ded < 0:
            return JSONResponse(
                {"status": "error",
                 "error": "standard_deduction must not be negative"}, 400,
            )
    try:
        state = config_store.upsert_tax_schedule(
            user_ctx.db_path, year, jurisdiction, filing_status,
            brackets=body.get("brackets", config_store.UNSET),
            standard_deduction=std_ded,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state}


@router.delete("/config/tax/schedules/{year}/{jurisdiction}/{filing_status}")
async def api_config_tax_schedules_delete(
    year: int, jurisdiction: str, filing_status: str,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Drop an override, reverting those fields to the bundled values."""
    from istota.money import config_store
    try:
        removed = config_store.delete_tax_schedule(
            user_ctx.db_path, year, jurisdiction, filing_status,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "removed": removed}


@router.get("/config/tax/years")
async def api_config_tax_years(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    return {"status": "ok", "years": config_store.list_tax_year_rates(user_ctx.db_path)}


@router.put("/config/tax/years/{year}")
async def api_config_tax_years_put(
    year: int, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    # The legacy bracket/deduction columns are still writable on the table but
    # nothing reads them any more, so accepting one would return 200 and change
    # nothing at all — a worse answer than a 400 that names where they moved.
    dead = _LEGACY_YEAR_RATE_KEYS & set(body)
    if dead:
        return JSONResponse(
            {"status": "error",
             "error": (f"{sorted(dead)} moved to /config/tax/schedules/"
                       "{year}/{jurisdiction}/{filing_status}, which carries the "
                       "filing-status dimension these lack")}, 400,
        )
    for key, value in body.items():
        # `null` clears the override; anything else must be a usable number.
        # Without this a string lands in a REAL column, comes back a string, and
        # `compute_se_tax` multiplies it — 500ing every later estimate.
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            return JSONResponse(
                {"status": "error",
                 "error": f"{key} must be a non-negative finite number or null"},
                400,
            )
    try:
        state = config_store.upsert_tax_year_rates(user_ctx.db_path, year, **body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state}


@router.delete("/config/tax/years/{year}")
async def api_config_tax_years_delete(
    year: int, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_tax_year_rates(user_ctx.db_path, year)
    return {"status": "ok", "removed": ok}


@router.get("/config/tax/patterns")
async def api_config_tax_patterns(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    return {
        "status": "ok",
        "patterns": config_store.list_tax_patterns(user_ctx.db_path),
    }


@router.put("/config/tax/patterns")
async def api_config_tax_patterns_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Replace-all per-kind. Body: ``{"se_income": [...], "se_expense": [...]}``."""
    from istota.money import config_store
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_tax_patterns(
            user_ctx.db_path,
            {k: v for k, v in body.items() if k in ("se_income", "se_expense")},
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch")
async def api_config_monarch(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_monarch(user_ctx.db_path)
    return {
        "status": "ok",
        "sync": {
            "lookback_days": cfg.sync.lookback_days,
            "default_account": cfg.sync.default_account,
            "recategorize_account": cfg.sync.recategorize_account,
        },
    }


@router.put("/config/monarch")
async def api_config_monarch_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    allowed = {"lookback_days", "default_account", "recategorize_account"}
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"}, 400,
        )
    try:
        config_store.set_monarch_sync(user_ctx.db_path, **body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch/profiles")
async def api_config_monarch_profiles(
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    return {
        "status": "ok",
        "profiles": config_store.list_monarch_profiles(user_ctx.db_path),
    }


@router.post("/config/monarch/profiles")
async def api_config_monarch_profiles_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"status": "error", "error": "name required"}, 400)
    fields = {k: v for k, v in body.items() if k != "name"}
    try:
        prof, state = config_store.upsert_monarch_profile(
            user_ctx.db_path, name, **fields,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state, "profile": prof}


@router.put("/config/monarch/profiles/{name}")
async def api_config_monarch_profiles_put(
    name: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    try:
        prof, state = config_store.upsert_monarch_profile(
            user_ctx.db_path, name, **body,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state, "profile": prof}


@router.delete("/config/monarch/profiles/{name}")
async def api_config_monarch_profiles_delete(
    name: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_monarch_profile(user_ctx.db_path, name)
    return {"status": "ok", "removed": ok}


def _resolve_profile_query(profile: str | None):
    if profile is None or profile == "" or profile == "global":
        return None
    return profile


@router.get("/config/monarch/account-map")
async def api_config_monarch_account_map(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        mapping = config_store.get_account_map(user_ctx.db_path, p)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "mapping": mapping}


@router.put("/config/monarch/account-map")
async def api_config_monarch_account_map_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_account_map(user_ctx.db_path, p, body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch/category-map")
async def api_config_monarch_category_map(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        mapping = config_store.get_category_map(user_ctx.db_path, p)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "mapping": mapping}


@router.put("/config/monarch/category-map")
async def api_config_monarch_category_map_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_category_map(user_ctx.db_path, p, body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch/tag-filters")
async def api_config_monarch_tag_filters(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        return {"status": "ok", "tags": config_store.get_tag_filters(user_ctx.db_path, p)}
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)


@router.put("/config/monarch/tag-filters")
async def api_config_monarch_tag_filters_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Body: ``{"include": [...], "exclude": [...]}`` — replaces both lists."""
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    include = body.get("include", []) or []
    exclude = body.get("exclude", []) or []
    try:
        config_store.replace_tag_filters(user_ctx.db_path, p, include, exclude)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Transaction rules
# ---------------------------------------------------------------------------
#
# A thin layer over `config_store`'s rule accessors. Everything that decides
# anything — validation, the duplicate refusal, the scope test, the ordered
# pass — is below this file; what is here is the HTTP shape and the two
# guards that only exist at this boundary: a scope that must be chosen rather
# than defaulted into, and a preview whose inputs come off the wire.
#
# No error message rendered here may carry the user's `match_value` or
# `target`. They are the user's own financial data, they reach a response
# body, and `config_store._duplicate_rule_error` and
# `rules.validate_rule_fields` already answer with a field name and a
# constraint for that reason.

# A preview's tag list is walked once per rule in scope, and both lengths come
# off the request. The subjects are cut to the engine's own cap, which changes
# no answer (`_subject_matches` truncates to the same length before every
# comparison) and bounds the work at one slice rather than one per rule; the
# tag list has no such free cut, since dropping a tag silently changes which
# rules fire, so it is refused instead.
_MAX_PREVIEW_TAGS = 50

# What a lost create/update race answers. Deliberately the same wording the
# store's own refusal uses, minus the id, so a client cannot tell the two
# apart and start treating one as retryable.
_DUPLICATE_RULE = "a rule with this scope, match and action already exists"

_PREVIEW_SUBJECTS = ("category", "account", "payee", "notes")
_PREVIEW_KEYS = frozenset(_PREVIEW_SUBJECTS) | {"ledger", "source", "tags"}


def _reject_unknown_rule_keys(body: dict, allowed) -> JSONResponse | None:
    """Refuse a key the store would not take.

    Cheap duplication of `validate_rule_fields`' own check, and not only
    duplication: the accessors are called as ``f(db_path, **body)``, so a body
    carrying ``db_path`` raises ``TypeError`` from the call itself and 500s
    before any validation runs.
    """
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        return _error("unknown field(s): " + ", ".join(unknown), 400)
    return None


def _explicit_scope(body: dict):
    """The two scope columns, which a caller sends rather than defaults into.

    Both default to ``''`` in the table and the engine reads ``''`` as "any".
    So an omitted ``ledger`` on a create silently writes a rule applying to
    every ledger and every source, and an omitted one on a preview silently
    answers about the global scope alone — in one direction a wider rule than
    anybody asked for, in the other a preview of a scope the caller is not
    looking at. Neither is a value somebody chose. ``''`` stays legal; it just
    has to be sent.

    Returns ``(scope, None)`` or ``(None, response)``.
    """
    missing = [key for key in ("ledger", "source") if key not in body]
    if missing:
        return None, _error(
            "send " + " and ".join(missing)
            + " explicitly; '' is the any-scope value",
            400,
        )
    wrong = [key for key in ("ledger", "source") if not isinstance(body[key], str)]
    if wrong:
        return None, _error(" and ".join(wrong) + " must be a string", 400)
    return (body["ledger"], body["source"]), None


def _preview_transaction(body: dict):
    """Build the transaction a preview is scored against.

    ``date`` and ``amount`` are required by the dataclass and are read by no
    rule — amount matching is an explicit non-goal and no ``field`` names
    either — so they are placeholders rather than inputs.

    Returns ``(txn, None)`` or ``(None, response)``.
    """
    from datetime import date

    from istota.money.core import rules as rule_engine
    from istota.money.core.importers.base import NormalizedTransaction

    cap = rule_engine.MAX_SUBJECT_CHARS
    subjects = {}
    for key in _PREVIEW_SUBJECTS:
        raw = body.get(key, "")
        if not isinstance(raw, str):
            return None, _error(f"{key} must be a string", 400)
        subjects[key] = raw[:cap]

    tags = body.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return None, _error("tags must be a list of strings", 400)
    if len(tags) > _MAX_PREVIEW_TAGS:
        return None, _error(
            f"tags: at most {_MAX_PREVIEW_TAGS} entries", 400,
        )

    return NormalizedTransaction(
        date=date.today(),
        amount=0.0,
        payee=subjects["payee"],
        category=subjects["category"],
        account_name=subjects["account"],
        notes=subjects["notes"],
        tags=[tag[:cap] for tag in tags],
    ), None


def _rule_trace(txn, compiled, resolution) -> list[dict]:
    """One line per *enabled* rule in scope: what it did, or why it did not.

    Enabled only, because ``load_rules_for_run`` filters in SQL — the same
    set an import is scored against, which is the set a preview has to
    describe. So this list is shorter than the editor's beside it, whose
    ``include_disabled`` defaults to true.

    Built here rather than in the engine because it is this surface's shape,
    and because it needs the one thing ``resolve`` deliberately does not
    return. ``Resolution.hits`` is only the rules that *filled* a slot, so a
    rule that matched into a slot already taken appears nowhere in it — and
    that is exactly the rule somebody editing priorities needs to see.
    ``rules.resolve``'s own docstring points here for it: what a rule would
    have done is recovered by re-running ``matches``.

    The half that reading ``hits`` cannot give you is ``superseded_by_skip``.
    A ``skip`` does not merely end the pass — ``resolve`` *replaces* the hit
    list with the skip's own entry and nulls both accounts — so a mapping
    rule that genuinely fired earlier in the pass is retroactively absent
    from ``hits``. Classified against ``hits`` alone it reads as ``shadowed``
    by nobody, which is both wrong and unrenderable: the outcome's whole
    meaning is that ``shadowed_by`` names the rule that beat it. Reachable
    through the API, since ``priority`` is the user's to set and nothing
    obliges a skip to sort first.
    """
    from istota.money.core import rules as rule_engine

    slots = ("posting_account", "contra_account")

    # Replay the slot assignment rather than reading it off `resolution.hits`.
    # `hits` is authoritative only for a pass that ran to the end; where a
    # skip ended it, `hits` has been replaced wholesale and cannot say which
    # of two matching rules had held a slot. Replaying answers both shapes
    # with one rule, and what is being replayed is `resolve` itself, so the
    # two agree by construction wherever `hits` survives — pinned by
    # `test_the_replayed_slots_agree_with_the_engines_own_hits`.
    matched: dict[int, bool] = {}
    first_for_slot: dict[str, int] = {}
    for item in compiled[: resolution.considered]:
        stored = item.rule
        hit = rule_engine.matches(item, txn)
        matched[stored.id] = hit
        if hit and stored.action in slots:
            first_for_slot.setdefault(stored.action, stored.id)

    trace = []
    for index, item in enumerate(compiled):
        stored = item.rule
        shadowed_by = None
        if index >= resolution.considered:
            # A `skip` ended the pass before this rule was reached.
            outcome = "not_evaluated"
        elif not matched[stored.id]:
            outcome = "no_match"
        elif stored.action == "skip":
            # `resolve` returns on the first matching skip, so a matching one
            # inside `considered` is necessarily the one that ended the pass.
            outcome = "applied"
        elif stored.action not in rule_engine.ACTIONS:
            # `resolve` steps over an action it has no slot for. Only a
            # hand-edited row can be here, and calling it shadowed would name
            # no shadowing rule.
            outcome = "ignored"
        elif first_for_slot.get(stored.action) != stored.id:
            outcome = "shadowed"
            shadowed_by = first_for_slot.get(stored.action)
        elif resolution.skip:
            # It held its slot, and then a later skip emptied it. Not
            # `applied` (nothing is posted) and not `shadowed` (no rule beat
            # it) — the two outcomes a `hits`-only reading has to choose
            # between, both of them wrong.
            outcome = "superseded_by_skip"
        else:
            outcome = "applied"
        trace.append({
            "rule_id": stored.id,
            "priority": stored.priority,
            "ledger": stored.ledger,
            "source": stored.source,
            "field": stored.field,
            "match_kind": stored.match_kind,
            "match_value": stored.match_value,
            "action": stored.action,
            "target": stored.target,
            "origin": stored.origin,
            "outcome": outcome,
            "shadowed_by": shadowed_by,
        })
    return trace


@router.get("/config/transaction-rules")
async def api_transaction_rules(
    ledger: str | None = None,
    source: str | None = None,
    include_disabled: bool = True,
    user_ctx: UserContext = Depends(get_user_config),
):
    """The rules in one scope, in evaluation order.

    ``ledger`` and ``source`` are exact matches, not the engine's wildcard
    test: an editor showing one ledger's rules must not fold in every
    ``''``-scoped one as though the user wrote it there. Omitting a parameter
    drops that filter entirely; ``?ledger=`` selects the any-ledger scope.
    """
    from istota.money import config_store

    return {
        "status": "ok",
        "rules": config_store.list_transaction_rules(
            user_ctx.db_path,
            ledger=ledger,
            source=source,
            include_disabled=include_disabled,
        ),
    }


@router.post("/config/transaction-rules")
async def api_transaction_rules_create(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create one rule. ``ledger`` and ``source`` are required — see
    :func:`_explicit_scope`."""
    from istota.money import config_store
    from istota.money.core import rules as rule_engine

    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    bad = _reject_unknown_rule_keys(body, rule_engine.RULE_FIELDS)
    if bad is not None:
        return bad
    _scope, err = _explicit_scope(body)
    if err is not None:
        return err
    try:
        rule = config_store.create_transaction_rule(user_ctx.db_path, **body)
    except sqlite3.IntegrityError:
        # The store checks for a duplicate and then inserts, which is not
        # atomic across connections — the web process and the CLI, or two web
        # workers, can race onto the same key. The loser hits the unique
        # index and must answer as the non-racing path does rather than 500.
        # The id is not named here: looking it up costs a second query on a
        # path that is already losing a race.
        return _error(_DUPLICATE_RULE, 400)
    except ValueError as exc:
        return _error(str(exc), 400)
    return {"status": "ok", "rule": rule}


@router.put("/config/transaction-rules/{rule_id}")
async def api_transaction_rules_update(
    rule_id: int,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Merge a partial change onto a stored rule.

    No scope guard here, unlike the create: the row already carries a scope
    somebody chose, and an omitted ``ledger`` means "leave it" rather than
    "any". Sending one still moves the rule.
    """
    from istota.money import config_store
    from istota.money.core import rules as rule_engine

    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    bad = _reject_unknown_rule_keys(body, rule_engine.RULE_FIELDS)
    if bad is not None:
        return bad
    try:
        rule = config_store.update_transaction_rule(
            user_ctx.db_path, rule_id, **body,
        )
    except sqlite3.IntegrityError:
        return _error(_DUPLICATE_RULE, 400)
    except ValueError as exc:
        return _error(str(exc), 400)
    if rule is None:
        return _error(f"rule {rule_id} not found", 404)
    return {"status": "ok", "rule": rule}


@router.delete("/config/transaction-rules/{rule_id}")
async def api_transaction_rules_delete(
    rule_id: int,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete one rule.

    A missing id is ``removed: false`` rather than a 404, matching
    ``delete_monarch_profile``'s existing shape: the caller asked for the rule
    to be gone and it is.
    """
    from istota.money import config_store

    return {
        "status": "ok",
        "removed": config_store.delete_transaction_rule(user_ctx.db_path, rule_id),
    }


@router.post("/config/transaction-rules/test")
async def api_transaction_rules_test(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Resolve a made-up transaction against the stored rules.

    Body: ``{ledger, source, category, account, payee, notes, tags}``. Returns
    the resolution and the ordered trace, including the rules that matched
    into a slot already filled.

    A POST that writes nothing, and it still carries ``verify_origin``: every
    other POST on this router does, and the exception would be the thing a
    reader has to check rather than the rule.

    ``load_rules_for_run`` answering ``None`` means the one-time migration did
    not complete, so an import still resolves from the legacy maps. A preview
    drawn from the table would then describe behaviour this deployment does
    not have, which is worse than no preview — so it refuses with a 409 rather
    than answering.
    """
    from istota.money import config_store
    from istota.money.core import rules as rule_engine

    body = await _read_body(request)
    if body is _BODY_INVALID:
        return _bad_body()
    bad = _reject_unknown_rule_keys(body, _PREVIEW_KEYS)
    if bad is not None:
        return bad
    scope, err = _explicit_scope(body)
    if err is not None:
        return err
    txn, err = _preview_transaction(body)
    if err is not None:
        return err

    ledger, source = scope
    stored = config_store.load_rules_for_run(user_ctx.db_path, ledger, source)
    if stored is None:
        return _error(
            "transaction rules are not in force on this deployment: the "
            "one-time migration has not completed, so an import still "
            "resolves from the legacy maps",
            409,
        )
    compiled, dropped = rule_engine.compile_rules_reporting(stored)
    resolution = rule_engine.resolve(txn, compiled)
    return {
        "status": "ok",
        "resolution": {
            "skip": resolution.skip,
            "posting_account": resolution.posting_account,
            "contra_account": resolution.contra_account,
            "considered": resolution.considered,
            "hits": [
                {"rule_id": h.rule_id, "action": h.action, "target": h.target}
                for h in resolution.hits
            ],
        },
        "trace": _rule_trace(txn, compiled, resolution),
        # Rows the engine could not compile. Dropping a `skip` imports a
        # transaction the user excluded on purpose, so the preview says so
        # rather than leaving it in a log line.
        "dropped": dropped,
    }


@router.get("/config/transaction-rules/coverage")
async def api_transaction_rules_coverage(
    field: str = "category",
    limit: int = 500,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Distinct source values recent imports carried, and what they posted to.

    ``profile`` scopes the read; absent is every profile, and ``?profile=``
    selects the rows a profile-less sync wrote. Not
    :func:`_resolve_profile_query`, which folds those two together because for
    the *config* accessors ``None`` means the global scope — here it means no
    filter, and reusing the translation would silently answer about every
    ledger whenever the caller asked for the profile-less one.

    ``untraced`` comes back only for ``field=category``. It counts rows with
    no ``src_category``, which is exactly the set the category list excludes
    and says nothing about the account column, since a row can carry a
    category and no account.
    """
    from istota.money import db as money_db

    if user_ctx.db_path is None:
        raise HTTPException(500, "money DB not configured for this user")
    # Every other money route reaches its table through a `config_store`
    # accessor, each of which calls `init_db` first; this one goes to
    # `money_db` directly, where `get_db` is a bare `sqlite3.connect` that
    # creates an empty file and no tables. Without this, a money DB that has
    # not been through `init_db` raises `OperationalError` — `no such table`
    # on a fresh one, and on a pre-migration one `no such column: profile`,
    # since that column arrives with `_migrate_monarch_synced_columns`. Both
    # escape the `except ValueError` below as a 500.
    money_db.init_db(user_ctx.db_path)
    try:
        with money_db.get_db(user_ctx.db_path) as conn:
            values = money_db.get_source_value_coverage(
                conn, field=field, limit=limit, profile=profile,
            )
            untraced = (
                money_db.get_untraced_synced_count(conn, profile=profile)
                if field == "category"
                else None
            )
    except ValueError as exc:
        return _error(str(exc), 400)
    payload: dict = {"status": "ok", "field": field, "values": values}
    if untraced is not None:
        payload["untraced"] = untraced
    return payload


@router.get("/config/export")
async def api_config_export(
    section: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Export DB config as TOML. Returns ``text/plain``."""
    from istota.money import config_store
    import tomli_w

    db_path = user_ctx.db_path
    if section == "invoicing":
        body = config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(db_path),
        )
        text = tomli_w.dumps(_strip_none(body))
    elif section == "tax":
        body = config_store.tax_to_toml_dict(config_store.load_tax(db_path))
        text = tomli_w.dumps(_strip_none(body))
    elif section == "monarch":
        body = config_store.monarch_to_toml_dict(
            config_store.load_monarch(db_path),
        )
        text = tomli_w.dumps(_strip_none(body))
    else:
        combined: dict = {}
        inv = config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(db_path),
        )
        if inv:
            combined["invoicing"] = inv
        tax = config_store.tax_to_toml_dict(config_store.load_tax(db_path))
        if tax.get("tax"):
            combined["tax"] = tax["tax"]
        mon = config_store.monarch_to_toml_dict(
            config_store.load_monarch(db_path),
        )
        if mon.get("monarch"):
            combined["monarch"] = mon["monarch"]
        text = tomli_w.dumps(_strip_none(combined))
    return Response(content=text, media_type="text/plain")


def _strip_none(value):
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


@router.post("/config/import")
async def api_config_import(
    request: Request,
    section: str | None = None,
    dry_run: int = 0,
    replace: int = 0,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Import a TOML payload (multipart or JSON {text: ...}).

    Returns a per-section list of ``STATE: …`` entries. With ``dry_run=1``
    nothing is written.
    """
    import tomli

    text: str | None = None
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        f = form.get("file")
        if f is not None:
            text = (await f.read()).decode()
    if text is None:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "error": "no payload"}, 400)
        text = body.get("text") if isinstance(body, dict) else None
    if not text:
        return JSONResponse({"status": "error", "error": "no payload"}, 400)

    try:
        parsed = tomli.loads(text)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"status": "error", "error": f"unparseable TOML: {exc}"}, 400,
        )

    sections: list[str] = []
    if section in ("invoicing", "tax", "monarch"):
        sections = [section]
    else:
        invoicing_keys = (
            "companies", "clients", "services", "company",
            "accounting_path", "next_invoice_number", "invoicing",
        )
        if any(k in parsed for k in invoicing_keys):
            sections.append("invoicing")
        if "tax" in parsed:
            sections.append("tax")
        if "monarch" in parsed:
            sections.append("monarch")
    if not sections:
        return JSONResponse(
            {"status": "error", "error": "no recognized sections"}, 400,
        )

    from istota.cli_money import (
        _apply_section_import, _compute_section_diff, _extract_section_data,
    )

    sections_out = []
    for sec in sections:
        section_data = _extract_section_data(parsed, sec)
        if section_data is None:
            continue
        diff = _compute_section_diff(user_ctx, sec, section_data, bool(replace))
        if not dry_run:
            _apply_section_import(user_ctx, sec, section_data, bool(replace))
        sections_out.append({
            "section": sec,
            "states": [{"state": s, "message": m} for s, m in diff],
        })

    return {"status": "ok", "dry_run": bool(dry_run), "sections": sections_out}


# ---------------------------------------------------------------------------
# Portfolio (positions snapshots)
# ---------------------------------------------------------------------------


def _portfolio_conn(user_ctx: UserContext):
    """Open the per-user money DB for portfolio reads/writes.

    ``resolve_for_user`` → ``ensure_initialised`` has already created the
    schema; tests initialise via ``db.init_db`` themselves.
    """
    import sqlite3

    if user_ctx.db_path is None:
        raise HTTPException(500, "money DB not configured for this user")
    conn = sqlite3.connect(str(user_ctx.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@router.post("/portfolio/import")
async def api_portfolio_import(
    file: UploadFile = FastAPIFile(...),
    dry_run: int = 0,
    replace: int | None = None,
    force: int = 0,
    source: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Import a positions CSV (Fidelity export or fina history file).

    ``?source=<name>`` names an importer-registry source explicitly (the web
    picker's declared intent — a mismatched file errors rather than falling
    back to detection); absent, the format is auto-detected. ``?dry_run=1``
    returns the parse preview without writing. A duplicate (same content
    hash) is a 409 naming the existing snapshot. A same-day snapshot with
    different content returns ``{"status": "date_collision"}`` unless
    ``?replace=<old_id>`` (delete the old one first) or ``?force=1`` (keep
    both) is passed.
    """
    import tempfile

    from istota.money import portfolio
    from istota.money.core.importers import parse_positions_file
    from istota.money.core.importers.positions_base import PositionParseError

    raw = await file.read()
    if not raw:
        return _error("empty upload", 400)

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        try:
            snapshots = parse_positions_file(tmp_path, source or None)
        except PositionParseError as exc:
            return _error(str(exc), 400)

        source_file = file.filename or "upload.csv"

        if dry_run:
            return {
                "status": "ok",
                "dry_run": True,
                "snapshots": [
                    {
                        "exported_at": s.exported_at.isoformat(),
                        "exported_at_estimated": s.exported_at_estimated,
                        "source": s.source,
                        "position_count": len(s.rows),
                        "total_value": round(
                            sum(r.value for r in s.rows if r.value is not None), 2
                        ),
                        "warnings": s.warnings,
                    }
                    for s in snapshots
                ],
            }

        conn = _portfolio_conn(user_ctx)
        try:
            if replace is not None:
                portfolio.delete_snapshot(conn, replace)
            elif len(snapshots) == 1 and not force:
                # Same-calendar-date collision check (single-export flow only —
                # a fina-history migration legitimately spans many dates).
                snap = snapshots[0]
                if (
                    portfolio._duplicate_result(
                        conn, portfolio.compute_snapshot_hash(snap.rows)
                    )
                    is None
                ):
                    existing = portfolio.find_snapshot_by_date(conn, snap.exported_at)
                    if existing is not None:
                        return {
                            "status": "date_collision",
                            "existing": {
                                "id": existing.id,
                                "exported_at": existing.exported_at,
                                "position_count": existing.position_count,
                            },
                            "position_count": len(snap.rows),
                        }

            results = [
                portfolio.insert_snapshot(conn, s, source_file=source_file)
                for s in snapshots
            ]
        finally:
            conn.close()

        # Auto-classify new symbols after the import commits. Off the event
        # loop (ticker lookups are network I/O), on its own connection
        # (sqlite conns are thread-bound), and fail-soft — a lookup outage
        # leaves symbols in unclassified_symbols, never fails the import.
        for r in results:
            if r["status"] == "ok":
                r.setdefault("auto_classified", [])
        if any(
            r["status"] == "ok" and r.get("unclassified_symbols")
            for r in results
        ):
            try:
                await asyncio.to_thread(
                    _auto_classify_imported, user_ctx, snapshots, results
                )
            except Exception:
                logger.warning(
                    "portfolio auto-classification failed", exc_info=True
                )
    finally:
        tmp_path.unlink(missing_ok=True)

    logger.info(
        "portfolio_import source_file=%s snapshots=%s ok=%s duplicates=%s",
        source_file, len(results),
        sum(1 for r in results if r["status"] == "ok"),
        sum(1 for r in results if r["status"] == "duplicate"),
    )
    if len(results) == 1:
        result = dict(results[0])
        result["source_file"] = source_file
        if result["status"] == "duplicate":
            return JSONResponse(result, status_code=409)
        return result
    from istota.money import portfolio_autoclass

    return {
        "status": "ok",
        "imported": sum(1 for r in results if r["status"] == "ok"),
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        # Hoisted so a client reading the top level sees the classification
        # outcome of a multi-snapshot import (the fina migration) at all.
        **portfolio_autoclass.summarize_auto_results(results),
        "results": results,
    }


@router.get("/portfolio/snapshots")
async def api_portfolio_snapshots(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        return {"status": "ok", "snapshots": portfolio.list_snapshots(conn)}
    finally:
        conn.close()


@router.get("/portfolio/snapshots/{snapshot_id}")
async def api_portfolio_snapshot_detail(
    snapshot_id: int,
    group: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        summary = portfolio.snapshot_summary(conn, snapshot_id, group=group)
        if summary is None:
            return _error(f"no snapshot with id {snapshot_id}", 404)
        return {"status": "ok", "summary": summary}
    finally:
        conn.close()


@router.delete("/portfolio/snapshots/{snapshot_id}")
async def api_portfolio_snapshot_delete(
    snapshot_id: int,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        if not portfolio.delete_snapshot(conn, snapshot_id):
            return _error(f"no snapshot with id {snapshot_id}", 404)
        return {"status": "ok", "deleted": snapshot_id}
    finally:
        conn.close()


@router.get("/portfolio/summary")
async def api_portfolio_summary(
    group: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Summary of the latest snapshot; ``summary: null`` when nothing imported."""
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        snapshots = portfolio.list_snapshots(conn)
        if not snapshots:
            return {"status": "ok", "summary": None}
        summary = portfolio.snapshot_summary(conn, snapshots[0]["id"], group=group)
        return {"status": "ok", "summary": summary}
    finally:
        conn.close()


@router.get("/portfolio/history")
async def api_portfolio_history(
    group_by: str = "total",
    group: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        try:
            result = portfolio.history_series(conn, group_by=group_by, group=group)
        except ValueError as exc:
            return _error(str(exc), 400)
        return {"status": "ok", **result}
    finally:
        conn.close()


@router.get("/portfolio/diff")
async def api_portfolio_diff(
    older: int,
    newer: int,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        diff = portfolio.snapshot_diff(conn, older, newer)
        if diff is None:
            return _error("one or both snapshot ids not found", 404)
        return {"status": "ok", "diff": diff}
    finally:
        conn.close()


@router.get("/portfolio/symbols/{symbol}/history")
async def api_portfolio_symbol_history(
    symbol: str,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        return {"status": "ok", "history": portfolio.symbol_history(conn, symbol)}
    finally:
        conn.close()


def _account_to_dict(account) -> dict:
    from dataclasses import asdict

    return asdict(account)


@router.get("/portfolio/accounts")
async def api_portfolio_accounts(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        return {
            "status": "ok",
            "accounts": [_account_to_dict(a) for a in portfolio.list_accounts(conn)],
        }
    finally:
        conn.close()


@router.patch("/portfolio/accounts/{account_id}")
async def api_portfolio_account_patch(
    account_id: int,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import portfolio

    try:
        body = await request.json()
    except Exception:
        return _bad_body()
    if not isinstance(body, dict):
        return _bad_body()
    allowed = {"group", "account_type", "excluded"}
    unknown = set(body) - allowed
    if unknown:
        return _error(f"unknown fields: {', '.join(sorted(unknown))}", 400)
    for key in ("group", "account_type"):
        if key in body:
            if not isinstance(body[key], str) or _CONTROL_CHARS_RE.search(body[key]):
                return _error(f"{key} must be a plain string", 400)
    if "excluded" in body and not isinstance(body["excluded"], bool):
        return _error("excluded must be a boolean", 400)
    if not body:
        return _error("no fields to update", 400)

    conn = _portfolio_conn(user_ctx)
    try:
        ok = portfolio.update_account(
            conn, account_id,
            group=body.get("group"),
            account_type=body.get("account_type"),
            excluded=body.get("excluded"),
        )
        if not ok:
            return _error(f"no account with id {account_id}", 404)
        return {"status": "ok", "account": _account_to_dict(portfolio.get_account(conn, account_id))}
    finally:
        conn.close()


def _auto_classify_imported(user_ctx, snapshots, results) -> None:
    """Runs in a worker thread: mutates each ok result's classification keys.

    One pass across every snapshot the upload produced — a fina history file
    parses into one per export date, and classifying each separately spent
    the whole lookup budget again on every one of them.

    Takes the same per-DB lock the backfill button does, so an import and a
    backfill can't run overlapping lookups for the same symbols. Blocking
    rather than skipping: the other holder is bounded by its own wall-clock
    budget, this already runs off the event loop, and the import has
    committed — the classification is worth waiting for.
    """
    from istota.money import portfolio_autoclass

    with _autoclass_lock(user_ctx):
        conn = _portfolio_conn(user_ctx)
        try:
            auto = portfolio_autoclass.auto_classify_snapshots(
                conn, snapshots,
                allow_lookups=bool(getattr(user_ctx, "autoclass_lookup", True)),
            )
            portfolio_autoclass.apply_auto_results(results, auto)
        finally:
            conn.close()


def _classification_to_dict(cls) -> dict:
    return {
        "symbol": cls.symbol_norm,
        "asset_class": cls.asset_class,
        "sub_class": cls.sub_class,
        "geography": cls.geography,
        "source": cls.source,
        "updated_at": cls.updated_at,
    }


@router.get("/portfolio/classifications")
async def api_portfolio_classifications(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        return {
            "status": "ok",
            "classifications": [
                _classification_to_dict(c)
                for c in portfolio.list_classifications(conn)
            ],
        }
    finally:
        conn.close()


def _autoclass_lock(user_ctx) -> threading.Lock:
    """One classification lock per money DB, shared by the import path and
    the backfill button.

    The card's own ``disabled`` guards one tab. Two clients, or the card plus
    an in-flight import, otherwise issue overlapping lookups for the same
    symbols — wasteful rather than corrupting now that the write is
    INSERT OR IGNORE, but there is nothing to gain by paying for it twice.
    """
    if user_ctx.db_path is None:
        raise HTTPException(500, "money DB not configured for this user")
    key = str(user_ctx.db_path)
    with _AUTOCLASS_LOCKS_GUARD:
        lock = _AUTOCLASS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _AUTOCLASS_LOCKS[key] = lock
        return lock


@router.post("/portfolio/classifications/auto")
async def api_portfolio_classifications_auto(
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Auto-classify every imported symbol that still resolves to
    Unclassified (the backfill behind the settings-page button)."""
    from istota.money import portfolio_autoclass

    lock = _autoclass_lock(user_ctx)

    def run() -> dict | None:
        # Acquired and released by the worker itself, so the lock's lifetime
        # tracks the work rather than the request: `to_thread` cancels the
        # awaiting future, never the thread, so releasing in the route's
        # `finally` would free the lock on a client disconnect while the
        # lookups it guards were still running.
        if not lock.acquire(blocking=False):
            return None
        try:
            conn = _portfolio_conn(user_ctx)
            try:
                candidates = portfolio_autoclass.candidates_from_positions(conn)
                return portfolio_autoclass.auto_classify_symbols(
                    conn, candidates,
                    allow_lookups=bool(getattr(user_ctx, "autoclass_lookup", True)),
                )
            finally:
                conn.close()
        finally:
            lock.release()

    result = await asyncio.to_thread(run)
    if result is None:
        return _error("auto-classification is already running", 409)
    return {"status": "ok", **result}


@router.put("/portfolio/classifications/{symbol}")
async def api_portfolio_classification_put(
    symbol: str,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import portfolio

    try:
        body = await request.json()
    except Exception:
        return _bad_body()
    if not isinstance(body, dict):
        return _bad_body()
    allowed = {"asset_class", "sub_class", "geography"}
    unknown = set(body) - allowed
    if unknown:
        return _error(f"unknown fields: {', '.join(sorted(unknown))}", 400)
    for key in allowed:
        value = body.get(key, "")
        if not isinstance(value, str) or _CONTROL_CHARS_RE.search(value):
            return _error(f"{key} must be a plain string", 400)
    if not body.get("asset_class", "").strip():
        return _error("asset_class is required", 400)

    conn = _portfolio_conn(user_ctx)
    try:
        try:
            norm = portfolio.set_classification(
                conn, symbol,
                asset_class=body["asset_class"],
                sub_class=body.get("sub_class", ""),
                geography=body.get("geography", ""),
            )
        except ValueError as exc:
            return _error(str(exc), 400)
        cls = next(
            c for c in portfolio.list_classifications(conn) if c.symbol_norm == norm
        )
        return {"status": "ok", "classification": _classification_to_dict(cls)}
    finally:
        conn.close()


@router.delete("/portfolio/classifications/{symbol}")
async def api_portfolio_classification_delete(
    symbol: str,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import portfolio

    conn = _portfolio_conn(user_ctx)
    try:
        if not portfolio.delete_classification(conn, symbol):
            return _error(f"no classification for {symbol}", 404)
        return {"status": "ok"}
    finally:
        conn.close()
