"""DB-backed config store for the money module.

Single home for per-user money configuration: invoicing, tax, monarch.
Lives in the per-user ``money.db`` alongside the existing transaction-tracking
schema. Mirrors the role :mod:`istota.feeds.db` plays for feeds.

The TOML files (``invoicing.toml`` / ``tax.toml`` / ``monarch.toml``) remain
the human-editable seed and the import/export wire format, but they are no
longer the runtime source of truth. The ``parse_*_config`` helpers in
``core/`` stay as thin wrappers over the dict-based ``*_from_toml_dict``
functions here so any escape-hatch caller (the standalone ``money`` CLI
invoked outside istota) keeps working.

Round-trip identity guarantee:

    toml_dict → from_dict() → save → load → to_dict() == toml_dict

modulo defaults the dataclass fills in for missing keys; export does not
write keys that match the dataclass default unless they were present in the
input.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from istota.money.core import rules as rule_engine
from istota.money.core.models import (
    ClientConfig,
    CompanyConfig,
    InvoicingConfig,
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
    ServiceConfig,
    TaxConfig,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Schema
# =============================================================================


SCHEMA = """\
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_companies (
    key                  TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    address              TEXT,
    email                TEXT,
    payment_instructions TEXT,
    logo                 TEXT,
    ar_account           TEXT,
    bank_account         TEXT,
    currency             TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_clients (
    key                 TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    address             TEXT,
    email               TEXT,
    terms               TEXT,
    ar_account          TEXT,
    entity              TEXT,
    schedule            TEXT NOT NULL DEFAULT 'on-demand',
    schedule_day        INTEGER NOT NULL DEFAULT 1,
    reminder_days       INTEGER NOT NULL DEFAULT 3,
    notifications       TEXT,
    days_until_overdue  INTEGER NOT NULL DEFAULT 0,
    ledger_posting      INTEGER NOT NULL DEFAULT 1,
    bundles_json        TEXT NOT NULL DEFAULT '[]',
    separate_json       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoicing_services (
    key            TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    rate           REAL NOT NULL,
    type           TEXT NOT NULL DEFAULT 'hours',
    income_account TEXT
);

CREATE TABLE IF NOT EXISTS tax_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tax_account_patterns (
    kind    TEXT NOT NULL,
    pattern TEXT NOT NULL,
    PRIMARY KEY(kind, pattern)
);

-- Payroll scalars are genuinely federal, year-keyed and status-agnostic, so
-- they stay here. The four bracket/deduction columns are legacy: they are read
-- once by `migrate_tax_schedules` and never again. Dropping them would be a
-- data migration for no gain.
CREATE TABLE IF NOT EXISTS tax_year_rates (
    tax_year                       INTEGER PRIMARY KEY,
    ss_wage_base                   REAL,
    ss_rate                        REAL,
    medicare_rate                  REAL,
    se_taxable_fraction            REAL,
    federal_standard_deduction     REAL,
    ca_standard_deduction          REAL,
    federal_brackets_json          TEXT,
    ca_brackets_json               TEXT
);

-- Brackets and deductions with the dimensions they actually have. The legacy
-- columns above are keyed on the year alone, so a stored bracket override was
-- filing-status-agnostic while the shipped values it overrode are keyed
-- (year, filing_status) — an override entered while filing jointly silently
-- continued to apply after switching to single.
CREATE TABLE IF NOT EXISTS tax_schedules (
    tax_year           INTEGER NOT NULL,
    jurisdiction       TEXT NOT NULL,   -- 'federal' or a two-letter state code
    filing_status      TEXT NOT NULL,   -- 'mfj' | 'single'
    brackets_json      TEXT,
    standard_deduction REAL,
    PRIMARY KEY (tax_year, jurisdiction, filing_status)
);

CREATE TABLE IF NOT EXISTS monarch_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS monarch_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL UNIQUE,
    ledger                TEXT NOT NULL,
    lookback_days         INTEGER,
    default_account       TEXT,
    recategorize_account  TEXT
);

CREATE TABLE IF NOT EXISTS monarch_account_map (
    profile_id        INTEGER NOT NULL,
    monarch_name      TEXT NOT NULL,
    beancount_account TEXT NOT NULL,
    PRIMARY KEY(profile_id, monarch_name),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS monarch_category_map (
    profile_id        INTEGER NOT NULL,
    monarch_category  TEXT NOT NULL,
    beancount_account TEXT NOT NULL,
    PRIMARY KEY(profile_id, monarch_category),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS monarch_tag_filters (
    profile_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY(profile_id, kind, tag),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);

-- Source-agnostic mapping rules. Scope is (ledger, source), each '' meaning
-- "any" — never profile_id, because a profile is a Monarch concept and a
-- Fidelity import has none while every import has a ledger. The three
-- monarch_* map tables above are kept rather than dropped, so a rollback to
-- the previous release boots and reads a coherent config instead of an empty
-- one. They are frozen at migration time, not dual-written: after the
-- migration nothing writes them except the include-tag list, so a rollback
-- restores the pre-migration maps and loses every edit made since.
CREATE TABLE IF NOT EXISTS transaction_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    field       TEXT NOT NULL,
    match_kind  TEXT NOT NULL DEFAULT 'iexact',
    match_value TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 100,
    enabled     INTEGER NOT NULL DEFAULT 1,
    origin      TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transaction_rules_order
    ON transaction_rules(ledger, source, priority, id);

-- Two rules with the same scope, match and action differ only in target, and
-- which one wins is then a function of insertion order — an ambiguity with no
-- good answer. Refused at write time, naming the existing rule's id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_rules_dedup
    ON transaction_rules(ledger, source, field, match_kind, match_value, action);
"""


SCHEMA_VERSION = "1"
GLOBAL_PROFILE_ID = 0


def init_db(db_path: Path | str) -> None:
    """Create config tables if missing. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        # config_store owns first-touch of money.db (ensure_initialised calls it
        # before money.db.init_db), so set WAL here so the DB is born WAL like
        # the other module DBs. It lives on local disk (Config.module_db_path),
        # so WAL's mmap'd -shm is safe — the SIGBUS that forced DELETE (ISSUE-157)
        # was a FUSE-mount artifact. Set once at init (persists in the file
        # header); not re-issued per _connect, since re-issuing takes a write
        # lock that races sibling readers (the dispatch-stall cause).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT OR IGNORE INTO monarch_profiles(id, name, ledger) "
            "VALUES (?, ?, ?)",
            (GLOBAL_PROFILE_ID, "__global__", ""),
        )
        # Independent of each other, despite reading like a sequence: a
        # migrated map rule carries source='monarch-api' and a seeded one '',
        # so the unique index never sees them as the same row and both always
        # exist. What separates the tiers is priority alone, 100 against 900 —
        # the same separation `map_monarch_category_with_config` already had
        # between the config map and the shipped constant beneath it.
        _migrate_transaction_rules(conn)
        seed_transaction_rules(conn)


@contextmanager
def _connect(db_path: Path | str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# JSON-encoded scalar helpers
# =============================================================================


def _kv_get(conn: sqlite3.Connection, table: str, key: str) -> Any:
    row = conn.execute(
        f"SELECT value FROM {table} WHERE key = ?", (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return row["value"]


def _kv_set(conn: sqlite3.Connection, table: str, key: str, value: Any) -> None:
    encoded = json.dumps(value)
    conn.execute(
        f"INSERT INTO {table}(key, value) VALUES (?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, encoded),
    )


def _kv_delete(conn: sqlite3.Connection, table: str, key: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))


def _kv_all(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute(f"SELECT key, value FROM {table}").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            out[row["key"]] = row["value"]
    return out


# =============================================================================
# Invoicing
# =============================================================================


_INVOICING_SCALAR_KEYS = (
    "accounting_path",
    "invoice_output",
    "next_invoice_number",
    "default_entity",
    "currency",
    "default_ar_account",
    "default_bank_account",
    "notifications",
    "days_until_overdue",
)


def invoicing_config_from_toml_dict(data: dict) -> InvoicingConfig:
    """Hydrate :class:`InvoicingConfig` from a parsed TOML dict.

    Accepts both the modern ``[companies.X]`` form and the legacy singular
    ``[company]`` block (which becomes ``companies["default"]``).
    """
    companies: dict[str, CompanyConfig] = {}
    for key, comp in (data.get("companies") or {}).items():
        companies[key] = _company_from_dict(key, comp)
    if not companies:
        legacy = data.get("company")
        if legacy:
            companies["default"] = _company_from_dict("default", legacy)

    default_entity = data.get("default_entity") or ""
    if not default_entity and companies:
        default_entity = next(iter(companies))
    company = companies.get(default_entity) if companies else None
    if company is None and companies:
        company = next(iter(companies.values()))
    if company is None:
        company = CompanyConfig(name="", key="default")

    clients: dict[str, ClientConfig] = {}
    for key, raw in (data.get("clients") or {}).items():
        invoicing_block = raw.get("invoicing", {}) or {}
        clients[key] = ClientConfig(
            key=key,
            name=raw.get("name", key),
            address=raw.get("address", ""),
            email=raw.get("email", ""),
            terms=raw.get("terms", 30),
            ar_account=raw.get("ar_account", ""),
            entity=raw.get("entity", ""),
            schedule=invoicing_block.get("schedule", "on-demand"),
            schedule_day=invoicing_block.get("day", 1),
            reminder_days=invoicing_block.get("reminder_days", 3),
            notifications=invoicing_block.get("notifications", ""),
            days_until_overdue=invoicing_block.get("days_until_overdue", 0),
            ledger_posting=invoicing_block.get("ledger_posting", True),
            bundles=list(invoicing_block.get("bundles", []) or []),
            separate=list(invoicing_block.get("separate", []) or []),
        )

    services: dict[str, ServiceConfig] = {}
    for key, svc in (data.get("services") or {}).items():
        services[key] = ServiceConfig(
            key=key,
            display_name=svc.get("display_name", key),
            rate=float(svc.get("rate", 0)),
            type=svc.get("type", "hours"),
            income_account=svc.get("income_account", ""),
        )

    return InvoicingConfig(
        accounting_path=data.get("accounting_path", ""),
        invoice_output=data.get("invoice_output", "invoices/generated"),
        next_invoice_number=data.get("next_invoice_number", 1),
        company=company,
        clients=clients,
        services=services,
        default_ar_account=data.get(
            "default_ar_account", "Assets:Accounts-Receivable",
        ),
        default_bank_account=data.get(
            "default_bank_account", "Assets:Bank:Checking",
        ),
        currency=data.get("currency", "USD"),
        companies=companies,
        default_entity=default_entity or "default",
        notifications=data.get("notifications", ""),
        days_until_overdue=data.get("days_until_overdue", 0),
    )


def _company_from_dict(key: str, raw: dict) -> CompanyConfig:
    return CompanyConfig(
        name=raw.get("name", ""),
        address=raw.get("address", ""),
        email=raw.get("email", ""),
        payment_instructions=raw.get("payment_instructions", ""),
        logo=raw.get("logo", ""),
        key=key,
        ar_account=raw.get("ar_account", ""),
        bank_account=raw.get("bank_account", ""),
        currency=raw.get("currency", ""),
    )


def invoicing_to_toml_dict(cfg: InvoicingConfig) -> dict:
    """Render :class:`InvoicingConfig` back to a TOML-shaped dict.

    Determinism: alphabetical key ordering, companies/clients/services
    emitted in lexical key order. Values matching the dataclass default
    are still emitted when they differ from the empty defaults the user
    is likely to have set.
    """
    out: dict[str, Any] = {}
    if cfg.accounting_path:
        out["accounting_path"] = cfg.accounting_path
    if cfg.invoice_output and cfg.invoice_output != "invoices/generated":
        out["invoice_output"] = cfg.invoice_output
    if cfg.next_invoice_number and cfg.next_invoice_number != 1:
        out["next_invoice_number"] = cfg.next_invoice_number
    if cfg.default_entity and cfg.default_entity != "default":
        out["default_entity"] = cfg.default_entity
    if cfg.default_ar_account and cfg.default_ar_account != "Assets:Accounts-Receivable":
        out["default_ar_account"] = cfg.default_ar_account
    if cfg.default_bank_account and cfg.default_bank_account != "Assets:Bank:Checking":
        out["default_bank_account"] = cfg.default_bank_account
    if cfg.currency and cfg.currency != "USD":
        out["currency"] = cfg.currency
    if cfg.notifications:
        out["notifications"] = cfg.notifications
    if cfg.days_until_overdue:
        out["days_until_overdue"] = cfg.days_until_overdue

    if cfg.companies:
        out["companies"] = {
            k: _company_to_dict(c)
            for k, c in sorted(cfg.companies.items())
        }
    if cfg.clients:
        out["clients"] = {
            k: _client_to_dict(c)
            for k, c in sorted(cfg.clients.items())
        }
    if cfg.services:
        out["services"] = {
            k: _service_to_dict(s)
            for k, s in sorted(cfg.services.items())
        }
    return out


def _company_to_dict(c: CompanyConfig) -> dict:
    out: dict[str, Any] = {"name": c.name}
    for attr in ("address", "email", "payment_instructions", "logo",
                 "ar_account", "bank_account", "currency"):
        v = getattr(c, attr)
        if v:
            out[attr] = v
    return out


def _client_to_dict(c: ClientConfig) -> dict:
    out: dict[str, Any] = {"name": c.name}
    for attr in ("address", "email", "ar_account", "entity"):
        v = getattr(c, attr)
        if v:
            out[attr] = v
    if c.terms not in ("", 30, None):
        out["terms"] = c.terms

    inv: dict[str, Any] = {}
    if c.schedule and c.schedule != "on-demand":
        inv["schedule"] = c.schedule
    if c.schedule_day and c.schedule_day != 1:
        inv["day"] = c.schedule_day
    if c.reminder_days and c.reminder_days != 3:
        inv["reminder_days"] = c.reminder_days
    if c.notifications:
        inv["notifications"] = c.notifications
    if c.days_until_overdue:
        inv["days_until_overdue"] = c.days_until_overdue
    if not c.ledger_posting:
        inv["ledger_posting"] = False
    if c.bundles:
        inv["bundles"] = list(c.bundles)
    if c.separate:
        inv["separate"] = list(c.separate)
    if inv:
        out["invoicing"] = inv
    return out


def _service_to_dict(s: ServiceConfig) -> dict:
    out: dict[str, Any] = {
        "display_name": s.display_name,
        "rate": s.rate,
    }
    if s.type and s.type != "hours":
        out["type"] = s.type
    if s.income_account:
        out["income_account"] = s.income_account
    return out


def load_invoicing(db_path: Path | str) -> InvoicingConfig:
    """Load :class:`InvoicingConfig` from the DB."""
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "invoicing_settings")

        companies: dict[str, CompanyConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_companies ORDER BY key"
        ).fetchall():
            companies[row["key"]] = CompanyConfig(
                name=row["name"] or "",
                address=row["address"] or "",
                email=row["email"] or "",
                payment_instructions=row["payment_instructions"] or "",
                logo=row["logo"] or "",
                key=row["key"],
                ar_account=row["ar_account"] or "",
                bank_account=row["bank_account"] or "",
                currency=row["currency"] or "",
            )

        clients: dict[str, ClientConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_clients ORDER BY key"
        ).fetchall():
            terms_raw = row["terms"]
            terms: int | str
            if terms_raw is None:
                terms = 30
            else:
                try:
                    terms = int(terms_raw)
                except (TypeError, ValueError):
                    terms = terms_raw
            clients[row["key"]] = ClientConfig(
                key=row["key"],
                name=row["name"] or row["key"],
                address=row["address"] or "",
                email=row["email"] or "",
                terms=terms,
                ar_account=row["ar_account"] or "",
                entity=row["entity"] or "",
                schedule=row["schedule"] or "on-demand",
                schedule_day=row["schedule_day"] if row["schedule_day"] is not None else 1,
                reminder_days=row["reminder_days"] if row["reminder_days"] is not None else 3,
                notifications=row["notifications"] or "",
                days_until_overdue=row["days_until_overdue"] or 0,
                ledger_posting=bool(row["ledger_posting"]),
                bundles=json.loads(row["bundles_json"] or "[]"),
                separate=json.loads(row["separate_json"] or "[]"),
            )

        services: dict[str, ServiceConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_services ORDER BY key"
        ).fetchall():
            services[row["key"]] = ServiceConfig(
                key=row["key"],
                display_name=row["display_name"] or row["key"],
                rate=float(row["rate"] or 0),
                type=row["type"] or "hours",
                income_account=row["income_account"] or "",
            )

    default_entity = scalars.get("default_entity") or ""
    if not default_entity and companies:
        default_entity = next(iter(companies))
    company = companies.get(default_entity) if default_entity else None
    if company is None and companies:
        company = next(iter(companies.values()))
    if company is None:
        company = CompanyConfig(name="", key="default")

    return InvoicingConfig(
        accounting_path=scalars.get("accounting_path", ""),
        invoice_output=scalars.get("invoice_output", "invoices/generated"),
        next_invoice_number=scalars.get("next_invoice_number", 1),
        company=company,
        clients=clients,
        services=services,
        default_ar_account=scalars.get(
            "default_ar_account", "Assets:Accounts-Receivable",
        ),
        default_bank_account=scalars.get(
            "default_bank_account", "Assets:Bank:Checking",
        ),
        currency=scalars.get("currency", "USD"),
        companies=companies,
        default_entity=default_entity or "default",
        notifications=scalars.get("notifications", ""),
        days_until_overdue=scalars.get("days_until_overdue", 0),
    )


def save_invoicing(
    db_path: Path | str,
    cfg: InvoicingConfig,
    *,
    replace_collections: bool = True,
) -> None:
    """Save :class:`InvoicingConfig` to the DB.

    With ``replace_collections=True`` (default), the companies/clients/
    services tables are truncated before insert — matching ``--replace``
    semantics. With ``False``, rows are upserted by key (merge semantics).
    Scalar settings are always upserted.
    """
    _sanitize_collections(cfg)
    init_db(db_path)
    with _connect(db_path) as conn:
        for key in _INVOICING_SCALAR_KEYS:
            value = _invoicing_scalar(cfg, key)
            if value is None:
                _kv_delete(conn, "invoicing_settings", key)
            else:
                _kv_set(conn, "invoicing_settings", key, value)

        if replace_collections:
            conn.execute("DELETE FROM invoicing_companies")
            conn.execute("DELETE FROM invoicing_clients")
            conn.execute("DELETE FROM invoicing_services")
        for key, comp in cfg.companies.items():
            _upsert_company_row(conn, key, comp)
        for key, client in cfg.clients.items():
            _upsert_client_row(conn, key, client)
        for key, svc in cfg.services.items():
            _upsert_service_row(conn, key, svc)


def _sanitize_collections(cfg: InvoicingConfig) -> None:
    """Bring a whole-config write in line with the closed-set invariants.

    ``save_invoicing`` is the bulk path — the legacy-TOML migration and
    ``money config import`` — and it bypassed the per-field validation the
    granular ``upsert_*`` ops enforce, so exactly the values those ops exist to
    keep out (a service typed ``hourly``, a client scheduled ``weekly``) could
    still land in the store and bill wrong.

    Coerce rather than raise: refusing would strand a user mid-migration on
    data that has been in their TOML for a year, and each coercion lands on the
    behaviour the code *already* had for that value — ``entry_line_item`` has
    no branch for ``hourly`` so it billed as hours, ``check_scheduled_invoices``
    only ever acted on ``monthly``. The WARNING is what turns a silent
    mis-billing into something an operator can see and fix.
    """
    for key, svc in cfg.services.items():
        if svc.type and svc.type not in SERVICE_TYPES:
            logger.warning(
                "money_config_sanitized kind=service key=%s field=type value=%r -> %r "
                "(expected one of %s)",
                key, svc.type, "hours", ", ".join(SERVICE_TYPES),
            )
            svc.type = "hours"
        if not isinstance(svc.rate, (int, float)) or isinstance(svc.rate, bool) \
                or not math.isfinite(svc.rate) or svc.rate < 0:
            logger.warning(
                "money_config_sanitized kind=service key=%s field=rate value=%r -> 0",
                key, svc.rate,
            )
            svc.rate = 0.0
    for key, client in cfg.clients.items():
        if client.schedule and client.schedule not in CLIENT_SCHEDULES:
            logger.warning(
                "money_config_sanitized kind=client key=%s field=schedule value=%r -> %r "
                "(expected one of %s)",
                key, client.schedule, "on-demand", ", ".join(CLIENT_SCHEDULES),
            )
            client.schedule = "on-demand"


def set_next_invoice_number(db_path: Path | str, new_number: int) -> None:
    """Persist just the ``next_invoice_number`` scalar to the DB.

    A targeted update so the invoice generator can advance the counter without
    rewriting the whole invoicing config (which would also truncate/replace the
    companies/clients/services tables).
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        _kv_set(conn, "invoicing_settings", "next_invoice_number", new_number)


def _invoicing_scalar(cfg: InvoicingConfig, key: str) -> Any:
    if key == "accounting_path":
        return cfg.accounting_path or None
    if key == "invoice_output":
        return cfg.invoice_output or None
    if key == "next_invoice_number":
        return cfg.next_invoice_number
    if key == "default_entity":
        return cfg.default_entity or None
    if key == "currency":
        return cfg.currency or None
    if key == "default_ar_account":
        return cfg.default_ar_account or None
    if key == "default_bank_account":
        return cfg.default_bank_account or None
    if key == "notifications":
        return cfg.notifications or None
    if key == "days_until_overdue":
        return cfg.days_until_overdue or None
    return None


def _upsert_company_row(conn: sqlite3.Connection, key: str, c: CompanyConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_companies(
            key, name, address, email, payment_instructions, logo,
            ar_account, bank_account, currency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            name = excluded.name,
            address = excluded.address,
            email = excluded.email,
            payment_instructions = excluded.payment_instructions,
            logo = excluded.logo,
            ar_account = excluded.ar_account,
            bank_account = excluded.bank_account,
            currency = excluded.currency
        """,
        (key, c.name, c.address, c.email, c.payment_instructions, c.logo,
         c.ar_account, c.bank_account, c.currency),
    )


def _upsert_client_row(conn: sqlite3.Connection, key: str, c: ClientConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_clients(
            key, name, address, email, terms, ar_account, entity,
            schedule, schedule_day, reminder_days, notifications,
            days_until_overdue, ledger_posting, bundles_json, separate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            name = excluded.name,
            address = excluded.address,
            email = excluded.email,
            terms = excluded.terms,
            ar_account = excluded.ar_account,
            entity = excluded.entity,
            schedule = excluded.schedule,
            schedule_day = excluded.schedule_day,
            reminder_days = excluded.reminder_days,
            notifications = excluded.notifications,
            days_until_overdue = excluded.days_until_overdue,
            ledger_posting = excluded.ledger_posting,
            bundles_json = excluded.bundles_json,
            separate_json = excluded.separate_json
        """,
        (
            key, c.name, c.address, c.email, str(c.terms),
            c.ar_account, c.entity,
            c.schedule, c.schedule_day, c.reminder_days, c.notifications,
            c.days_until_overdue, 1 if c.ledger_posting else 0,
            json.dumps(c.bundles), json.dumps(c.separate),
        ),
    )


def _upsert_service_row(conn: sqlite3.Connection, key: str, s: ServiceConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_services(
            key, display_name, rate, type, income_account
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            display_name = excluded.display_name,
            rate = excluded.rate,
            type = excluded.type,
            income_account = excluded.income_account
        """,
        (key, s.display_name, s.rate, s.type, s.income_account),
    )


# Validation -------------------------------------------------------------------
#
# These live in the store rather than the web route because they are the
# invariants whose violation changes behaviour *silently* — a service typed
# "hourly" has no branch in ``entry_line_item`` and quietly bills as hours, a
# client scheduled "weekly" is never picked up by ``check_scheduled_invoices``.
# Putting them here holds the CLI and the agent to the same rules the web forms
# enforce. Route-level *shape* checks (JSON types, unknown keys named in the
# error) stay in ``money/routes.py``.
#
# Only the fields a caller actually passes are checked: validating the merged
# record would make an existing non-conforming row uneditable, which is a
# worse failure than the one being prevented.

# Usable as a bare TOML table name in `money client list --format toml` and as
# a `--key` argument without quoting.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# One component of a beancount account: letters or digits (any script) and
# hyphens, no underscore. Mirrors beancount's `[\p{L}\p{Nd}\-]`, which Python's
# `re` can't spell — the uppercase-initial rule is checked separately below.
_ACCOUNT_COMPONENT_RE = re.compile(r"^[^\W_](?:[^\W_]|-)*$", re.UNICODE)
# Beancount commodity shape: `[A-Z][A-Z0-9'._-]*[A-Z0-9]?`, so a single-letter
# commodity is legal.
_COMMODITY_RE = re.compile(r"^[A-Z](?:[A-Z0-9'._-]*[A-Z0-9])?$")

_ACCOUNT_FIELDS = ("ar_account", "bank_account", "income_account")


def _is_account(value: str) -> bool:
    """Whether ``value`` is a beancount account name.

    Beancount's own `ACCOUNT_RE` is Unicode-aware
    (`(?:[\\p{Lu}][\\p{L}\\p{Nd}\\-]*)(?::[\\p{Lu}\\p{Nd}][\\p{L}\\p{Nd}\\-]*)+`),
    so `Assets:Forderungen:Müller` is a valid account and an ASCII-only check
    here would lock a non-English ledger out of the account it has been posting
    to all along. Structure is checked by regex; the uppercase-initial rule per
    component is checked in Python, since `re` has no Unicode uppercase class.
    Deliberately not pinned to the five default roots — a ledger can rename them.
    """
    parts = value.split(":")
    if len(parts) < 2:
        return False
    for index, part in enumerate(parts):
        if not _ACCOUNT_COMPONENT_RE.match(part):
            return False
        first = part[0]
        if index == 0:
            if not (first.isalpha() and first.isupper()):
                return False
        elif not (first.isdigit() or (first.isalpha() and first.isupper())):
            return False
    return True

_COMPANY_FIELDS = frozenset({
    "name", "address", "email", "payment_instructions", "logo",
    "ar_account", "bank_account", "currency",
})
_CLIENT_FIELDS = frozenset({
    "name", "address", "email", "terms", "ar_account", "entity",
    "schedule", "schedule_day", "reminder_days", "notifications",
    "days_until_overdue", "ledger_posting", "bundles", "separate",
})
_SERVICE_FIELDS = frozenset({"display_name", "rate", "type", "income_account"})

SERVICE_TYPES = ("hours", "days", "flat", "other")
CLIENT_SCHEDULES = ("on-demand", "monthly")


def _reject_unknown(kind: str, fields: dict, allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown {kind} fields: {sorted(bad)}")


def unchanged_fields(fields: dict, current: dict | None) -> frozenset[str]:
    """Fields whose submitted value already equals what's stored.

    These are exempted from validation. An existing non-conforming row has to
    stay editable, and a form seeds every input from the stored value and sends
    the lot back — so validating a field the caller didn't actually change
    makes a legacy row with one bad value (a service typed ``hourly``, an
    account predating the shape check) permanently unsaveable, and the error
    names a field the user never touched. Changing such a field still has to
    produce a valid value, so the rule only ever grandfathers what is already
    on disk.
    """
    if not current:
        return frozenset()
    return frozenset(
        name for name, value in fields.items()
        if name in current and value == current[name]
    )


def _check_key(kind: str, key: str, *, lowercase: bool = False) -> None:
    if not _KEY_RE.match(key or ""):
        raise ValueError(
            f"invalid {kind} key: {key!r} — use letters, digits, '-' or '_' "
            "(max 64 characters, starting with a letter or digit)",
        )
    if lowercase and key != key.lower():
        raise ValueError(
            f"invalid {kind} key: {key!r} — use lowercase. Work entries are "
            "stored with a lowercased client, so a mixed-case key would never "
            "match one and the client's work would never be billed.",
        )


def _check_accounts(fields: dict, skip: frozenset[str] = frozenset()) -> None:
    for name in _ACCOUNT_FIELDS:
        if name in skip:
            continue
        value = fields.get(name)
        if value in (None, ""):
            continue  # empty clears the field; the default applies instead
        if not isinstance(value, str) or not _is_account(value):
            raise ValueError(
                f"invalid {name}: {value!r} — expected a beancount account "
                "like Assets:Accounts-Receivable",
            )


def _check_currency(fields: dict, skip: frozenset[str] = frozenset()) -> None:
    if "currency" in skip:
        return
    value = fields.get("currency")
    if value in (None, ""):
        return
    if not isinstance(value, str) or not _COMMODITY_RE.match(value):
        raise ValueError(
            f"invalid currency: {value!r} — expected a commodity like USD",
        )


def _check_logo(fields: dict, skip: frozenset[str] = frozenset()) -> None:
    """Keep an entity logo inside the accounting workspace.

    ``core/invoicing`` resolves it as ``accounting_path / entity.logo`` and
    base64-embeds the result into the rendered invoice, and pathlib's ``/``
    lets an absolute operand replace the left-hand side outright — so an
    absolute path (or a ``..`` climb) reads a file from anywhere the daemon
    can and ships it. Harmless while only an operator could set it; the web
    form is the first path putting browser input here.
    """
    if "logo" in skip:
        return
    value = fields.get("logo")
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise ValueError(f"invalid logo: {value!r} — expected a path")
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or value.startswith("~") or ".." in candidate.parts:
        raise ValueError(
            f"invalid logo: {value!r} — expected a path inside the accounting "
            "folder, like invoices/logo.png",
        )


def _check_int(
    fields: dict, name: str, *, minimum: int, maximum: int | None = None,
    skip: frozenset[str] = frozenset(),
) -> None:
    if name in skip or name not in fields or fields[name] is None:
        return
    value = fields[name]
    # JSON `true` is an int to Python and would otherwise sail into a day field.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {name}: {value!r} — expected a whole number")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}–{maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"invalid {name}: {value} — expected {bound}")


def check_invoicing_scalars(fields: dict) -> None:
    """Validate the account/commodity-shaped invoicing scalars.

    The instance-wide defaults every entity and client falls back to, so the
    same shape rules apply as to the per-record ones.
    """
    accounts = {
        "ar_account": fields.get("default_ar_account"),
        "bank_account": fields.get("default_bank_account"),
    }
    for name, value in accounts.items():
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not _is_account(value):
            printed = "default_ar_account" if name == "ar_account" else "default_bank_account"
            raise ValueError(
                f"invalid {printed}: {value!r} — expected a beancount account "
                "like Assets:Accounts-Receivable",
            )
    _check_currency(fields)


def _validate_company_fields(fields: dict, skip: frozenset[str] = frozenset()) -> None:
    _reject_unknown("company", fields, _COMPANY_FIELDS)
    _check_accounts(fields, skip)
    _check_currency(fields, skip)
    _check_logo(fields, skip)


def _validate_client_fields(fields: dict, skip: frozenset[str] = frozenset()) -> None:
    _reject_unknown("client", fields, _CLIENT_FIELDS)
    _check_accounts(fields, skip)

    schedule = fields.get("schedule")
    if "schedule" not in skip and schedule is not None and schedule not in CLIENT_SCHEDULES:
        raise ValueError(
            f"invalid schedule: {schedule!r} — expected one of "
            f"{', '.join(CLIENT_SCHEDULES)}",
        )
    _check_int(fields, "schedule_day", minimum=1, maximum=31, skip=skip)
    _check_int(fields, "reminder_days", minimum=0, skip=skip)
    _check_int(fields, "days_until_overdue", minimum=0, skip=skip)

    terms = fields.get("terms")
    if "terms" not in skip and terms is not None:
        # The model is `int | str`: 30 and "NET 15" are both meaningful.
        if isinstance(terms, bool):
            raise ValueError(f"invalid terms: {terms!r}")
        if isinstance(terms, int):
            if terms < 0:
                raise ValueError(f"invalid terms: {terms} — expected at least 0")
        elif isinstance(terms, str):
            if not terms.strip():
                raise ValueError("invalid terms: expected a number of days or a label")
            # The column is TEXT and `load_invoicing` coerces a numeric string
            # back to int, so "-5" is the same stored value as -5 and has to
            # meet the same rule — otherwise the due date lands before the
            # invoice date.
            try:
                as_int = int(terms.strip())
            except ValueError:
                pass
            else:
                if as_int < 0:
                    raise ValueError(f"invalid terms: {terms!r} — expected at least 0")
        else:
            raise ValueError(f"invalid terms: {terms!r}")


def _validate_service_fields(fields: dict, skip: frozenset[str] = frozenset()) -> dict:
    """Validate a service's fields; returns them with ``rate`` coerced to float."""
    _reject_unknown("service", fields, _SERVICE_FIELDS)
    _check_accounts(fields, skip)

    svc_type = fields.get("type")
    if "type" not in skip and svc_type is not None and svc_type not in SERVICE_TYPES:
        raise ValueError(
            f"invalid type: {svc_type!r} — expected one of {', '.join(SERVICE_TYPES)}",
        )

    if "rate" not in skip and fields.get("rate") is not None:
        raw = fields["rate"]
        if isinstance(raw, bool):
            raise ValueError(f"invalid rate: {raw!r}")
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"invalid rate: {raw!r} — expected a number") from None
        # A NaN propagates into every total it touches; float("1e400") is inf.
        if not math.isfinite(rate) or rate < 0:
            raise ValueError(f"invalid rate: {raw!r} — expected a finite amount >= 0")
        fields = {**fields, "rate": rate}
    return fields


# Granular ops -----------------------------------------------------------------


def list_companies(db_path: Path | str) -> list[CompanyConfig]:
    return list(load_invoicing(db_path).companies.values())


def get_invoicing_setting(db_path: Path | str, key: str) -> Any:
    """Read one raw invoicing scalar, with no derivation applied.

    ``load_invoicing`` *derives* a ``default_entity`` when none is stored
    (falling back to the first company), which is right for rendering an
    invoice and wrong for asking "did the user actually pin this entity as the
    default?" — the delete guard needs the second question.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        return _kv_get(conn, "invoicing_settings", key)


class KeyExistsError(ValueError):
    """Raised by a ``create_only`` upsert when the key is already taken.

    Distinct from a validation ``ValueError`` so a route can answer 409 rather
    than 400. Detected inside the write transaction, so two concurrent creates
    can't both pass a pre-check and have the second silently overwrite the first.
    """


def upsert_company(
    db_path: Path | str, key: str, *, create_only: bool = False, **fields: Any,
) -> tuple[CompanyConfig, str]:
    _reject_unknown("company", fields, _COMPANY_FIELDS)
    init_db(db_path)
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM invoicing_companies WHERE key = ?", (key,),
        ).fetchone()
        if existing is not None and create_only:
            raise KeyExistsError(f"entity '{key}' already exists")
        if existing is None:
            _check_key("company", key)
        merged = {
            "name": "", "address": "", "email": "", "payment_instructions": "",
            "logo": "", "ar_account": "", "bank_account": "", "currency": "",
        }
        if existing is not None:
            for col in merged:
                merged[col] = existing[col] or ""
        _validate_company_fields(fields, unchanged_fields(fields, merged if existing else None))
        merged.update({k: v for k, v in fields.items() if v is not None})
        comp = CompanyConfig(
            key=key,
            name=merged["name"] or "",
            address=merged["address"] or "",
            email=merged["email"] or "",
            payment_instructions=merged["payment_instructions"] or "",
            logo=merged["logo"] or "",
            ar_account=merged["ar_account"] or "",
            bank_account=merged["bank_account"] or "",
            currency=merged["currency"] or "",
        )
        _upsert_company_row(conn, key, comp)
        if existing is None:
            return comp, "created"
        unchanged = all(
            (existing[col] or "") == (merged[col] or "") for col in merged
        )
        return comp, ("noop" if unchanged else "updated")


def delete_company(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_companies WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


def upsert_client(
    db_path: Path | str, key: str, *, create_only: bool = False, **fields: Any,
) -> tuple[ClientConfig, str]:
    _reject_unknown("client", fields, _CLIENT_FIELDS)
    init_db(db_path)
    defaults: dict[str, Any] = {
        "name": key, "address": "", "email": "", "terms": 30,
        "ar_account": "", "entity": "",
        "schedule": "on-demand", "schedule_day": 1, "reminder_days": 3,
        "notifications": "", "days_until_overdue": 0,
        "ledger_posting": True,
        "bundles": [], "separate": [],
    }
    with _connect(db_path) as conn:
        existing_row = conn.execute(
            "SELECT * FROM invoicing_clients WHERE key = ?", (key,),
        ).fetchone()
        if existing_row is not None and create_only:
            raise KeyExistsError(f"client '{key}' already exists")
        if existing_row is None:
            # Lowercase-only, unlike the other two collections: `add_work_entry`
            # stores `client.lower()`, so a mixed-case key matches no entry and
            # `build_line_items` skips every one of that client's rows — the
            # client's work is silently never billed.
            _check_key("client", key, lowercase=True)
        if existing_row is not None:
            terms_raw = existing_row["terms"]
            try:
                terms = int(terms_raw) if terms_raw is not None else 30
            except (TypeError, ValueError):
                terms = terms_raw
            defaults.update({
                "name": existing_row["name"] or key,
                "address": existing_row["address"] or "",
                "email": existing_row["email"] or "",
                "terms": terms,
                "ar_account": existing_row["ar_account"] or "",
                "entity": existing_row["entity"] or "",
                "schedule": existing_row["schedule"] or "on-demand",
                "schedule_day": existing_row["schedule_day"] or 1,
                "reminder_days": existing_row["reminder_days"] or 3,
                "notifications": existing_row["notifications"] or "",
                "days_until_overdue": existing_row["days_until_overdue"] or 0,
                "ledger_posting": bool(existing_row["ledger_posting"]),
                "bundles": json.loads(existing_row["bundles_json"] or "[]"),
                "separate": json.loads(existing_row["separate_json"] or "[]"),
            })
        _validate_client_fields(
            fields, unchanged_fields(fields, defaults if existing_row else None),
        )
        merged = dict(defaults)
        for k, v in fields.items():
            if v is None:
                continue
            merged[k] = v
        client = ClientConfig(
            key=key,
            name=merged["name"],
            address=merged["address"],
            email=merged["email"],
            terms=merged["terms"],
            ar_account=merged["ar_account"],
            entity=merged["entity"],
            schedule=merged["schedule"],
            schedule_day=merged["schedule_day"],
            reminder_days=merged["reminder_days"],
            notifications=merged["notifications"],
            days_until_overdue=merged["days_until_overdue"],
            ledger_posting=bool(merged["ledger_posting"]),
            bundles=list(merged["bundles"]),
            separate=list(merged["separate"]),
        )
        _upsert_client_row(conn, key, client)
        if existing_row is None:
            return client, "created"
        return client, ("noop" if merged == defaults else "updated")


def delete_client(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_clients WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


def upsert_service(
    db_path: Path | str, key: str, *, create_only: bool = False, **fields: Any,
) -> tuple[ServiceConfig, str]:
    _reject_unknown("service", fields, _SERVICE_FIELDS)
    init_db(db_path)
    defaults: dict[str, Any] = {
        "display_name": key, "rate": 0.0, "type": "hours", "income_account": "",
    }
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM invoicing_services WHERE key = ?", (key,),
        ).fetchone()
        if existing is not None and create_only:
            raise KeyExistsError(f"service '{key}' already exists")
        if existing is None:
            _check_key("service", key)
        if existing is not None:
            defaults.update({
                "display_name": existing["display_name"] or key,
                "rate": float(existing["rate"] or 0),
                "type": existing["type"] or "hours",
                "income_account": existing["income_account"] or "",
            })
        fields = _validate_service_fields(
            fields, unchanged_fields(fields, defaults if existing else None),
        )
        merged = dict(defaults)
        for k, v in fields.items():
            if v is None:
                continue
            merged[k] = v
        svc = ServiceConfig(
            key=key,
            display_name=merged["display_name"],
            rate=float(merged["rate"]),
            type=merged["type"],
            income_account=merged["income_account"],
        )
        _upsert_service_row(conn, key, svc)
        if existing is None:
            return svc, "created"
        return svc, ("noop" if merged == defaults else "updated")


def delete_service(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_services WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


# =============================================================================
# Tax
# =============================================================================


FILING_STATUSES = ("mfj", "single")

# The reserved jurisdiction key for federal rates, alongside the two-letter
# state codes. Kept distinct from a state code so one table carries both.
FEDERAL_JURISDICTION = "federal"


class _Unset:
    """Sentinel distinguishing "leave this field alone" from "clear it".

    The two fields of a schedule are edited independently, so a caller passing
    only one must not blank the other — but an explicit ``None`` has to mean
    revert-to-bundled, which is the per-field revert the settings editor needs.
    ``None`` cannot carry both meanings.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()

_TAX_SCALAR_KEYS = (
    "filing_status",
    "tax_year",
    "state",
    "w2.income",
    "w2.federal_withholding",
    "w2.state_withholding",
    "estimated_payments.federal",
    "estimated_payments.state",
    "options.enable_qbi_deduction",
    "safe_harbor.prior_year_federal_tax",
    "safe_harbor.prior_year_state_tax",
)


def tax_config_from_toml_dict(data: dict) -> TaxConfig:
    """Hydrate :class:`TaxConfig` from a parsed TOML dict.

    Accepts both the wrapped ``[tax]`` form and a flat top-level dict.
    """
    tax = data.get("tax", data)
    w2 = tax.get("w2", {}) or {}
    options = tax.get("options", {}) or {}
    accounts = tax.get("accounts", {}) or {}
    safe_harbor = tax.get("safe_harbor", {}) or {}
    estimated = tax.get("estimated_payments", {}) or {}
    rates = tax.get("rates", {}) or {}

    return TaxConfig(
        filing_status=tax.get("filing_status", "mfj"),
        tax_year=tax.get("tax_year", 2026),
        state=(tax.get("state") or "").upper(),
        w2_income=w2.get("income", 0),
        w2_federal_withholding=w2.get("federal_withholding", 0),
        w2_state_withholding=w2.get("state_withholding", 0),
        federal_estimated_paid=estimated.get("federal", 0),
        state_estimated_paid=estimated.get("state", 0),
        enable_qbi_deduction=options.get("enable_qbi_deduction", False),
        se_income_accounts=list(accounts.get("se_income", ["Income:ScheduleC"])),
        se_expense_accounts=list(accounts.get("se_expenses", ["Expenses:Business"])),
        prior_year_federal_tax=safe_harbor.get("prior_year_federal_tax", 0),
        prior_year_state_tax=safe_harbor.get("prior_year_state_tax", 0),
        federal_brackets=rates.get("federal_brackets"),
        state_brackets=rates.get("state_brackets"),
        federal_standard_deduction=rates.get("federal_standard_deduction"),
        state_standard_deduction=rates.get("state_standard_deduction"),
        ss_wage_base=rates.get("ss_wage_base"),
        ss_rate=rates.get("ss_rate"),
        medicare_rate=rates.get("medicare_rate"),
        se_taxable_fraction=rates.get("se_taxable_fraction"),
    )


def tax_to_toml_dict(cfg: TaxConfig) -> dict:
    """Render :class:`TaxConfig` back to a TOML-shaped dict (with ``[tax]``)."""
    tax: dict[str, Any] = {
        "filing_status": cfg.filing_status,
        "tax_year": cfg.tax_year,
    }
    if cfg.state:
        tax["state"] = cfg.state
    w2: dict[str, Any] = {}
    if cfg.w2_income:
        w2["income"] = cfg.w2_income
    if cfg.w2_federal_withholding:
        w2["federal_withholding"] = cfg.w2_federal_withholding
    if cfg.w2_state_withholding:
        w2["state_withholding"] = cfg.w2_state_withholding
    if w2:
        tax["w2"] = w2

    estimated: dict[str, Any] = {}
    if cfg.federal_estimated_paid:
        estimated["federal"] = cfg.federal_estimated_paid
    if cfg.state_estimated_paid:
        estimated["state"] = cfg.state_estimated_paid
    if estimated:
        tax["estimated_payments"] = estimated

    if cfg.enable_qbi_deduction:
        tax["options"] = {"enable_qbi_deduction": True}

    accounts: dict[str, Any] = {}
    if cfg.se_income_accounts and cfg.se_income_accounts != ["Income:ScheduleC"]:
        accounts["se_income"] = list(cfg.se_income_accounts)
    if cfg.se_expense_accounts and cfg.se_expense_accounts != ["Expenses:Business"]:
        accounts["se_expenses"] = list(cfg.se_expense_accounts)
    if accounts:
        tax["accounts"] = accounts

    safe_harbor: dict[str, Any] = {}
    if cfg.prior_year_federal_tax:
        safe_harbor["prior_year_federal_tax"] = cfg.prior_year_federal_tax
    if cfg.prior_year_state_tax:
        safe_harbor["prior_year_state_tax"] = cfg.prior_year_state_tax
    if safe_harbor:
        tax["safe_harbor"] = safe_harbor

    rates: dict[str, Any] = {}
    if cfg.ss_wage_base is not None:
        rates["ss_wage_base"] = cfg.ss_wage_base
    if cfg.ss_rate is not None:
        rates["ss_rate"] = cfg.ss_rate
    if cfg.medicare_rate is not None:
        rates["medicare_rate"] = cfg.medicare_rate
    if cfg.se_taxable_fraction is not None:
        rates["se_taxable_fraction"] = cfg.se_taxable_fraction
    if cfg.federal_standard_deduction is not None:
        rates["federal_standard_deduction"] = cfg.federal_standard_deduction
    if cfg.state_standard_deduction is not None:
        rates["state_standard_deduction"] = cfg.state_standard_deduction
    if cfg.federal_brackets is not None:
        rates["federal_brackets"] = [list(b) for b in cfg.federal_brackets]
    if cfg.state_brackets is not None:
        rates["state_brackets"] = [list(b) for b in cfg.state_brackets]
    if rates:
        tax["rates"] = rates

    return {"tax": tax}


def load_tax(db_path: Path | str) -> TaxConfig:
    """Load :class:`TaxConfig` from the DB."""
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "tax_settings")
        patterns = {"se_income": [], "se_expense": []}
        for row in conn.execute(
            "SELECT kind, pattern FROM tax_account_patterns ORDER BY kind, pattern"
        ).fetchall():
            patterns.setdefault(row["kind"], []).append(row["pattern"])

        tax_year = int(scalars.get("tax_year", 2026))
        filing_status = scalars.get("filing_status", "mfj")
        state = (scalars.get("state") or "").upper()
        rate_row = conn.execute(
            "SELECT * FROM tax_year_rates WHERE tax_year = ?", (tax_year,),
        ).fetchone()
        # Overrides are scoped to (year, jurisdiction, filing_status), so
        # switching filing status stops carrying the other status's numbers.
        fed_schedule = _fetch_schedule(
            conn, tax_year, FEDERAL_JURISDICTION, filing_status,
        )
        state_schedule = (
            _fetch_schedule(conn, tax_year, state, filing_status) if state else None
        )

    # When the DB has no patterns, return empty lists rather than baking in
    # the heuristic defaults (`["Income:ScheduleC"]` etc). Otherwise a
    # round-trip ``load_tax → save_tax`` of an empty DB would inject those
    # defaults into the patterns table and falsely flag the section as
    # "DB-populated", blocking the legacy migration.
    se_income = patterns.get("se_income") or []
    se_expense = patterns.get("se_expense") or []

    ss_wage_base = None
    ss_rate = None
    medicare_rate = None
    se_taxable_fraction = None
    if rate_row is not None:
        ss_wage_base = rate_row["ss_wage_base"]
        ss_rate = rate_row["ss_rate"]
        medicare_rate = rate_row["medicare_rate"]
        se_taxable_fraction = rate_row["se_taxable_fraction"]

    fed_brackets = fed_schedule["brackets"] if fed_schedule else None
    fed_std_ded = fed_schedule["standard_deduction"] if fed_schedule else None
    state_brackets = state_schedule["brackets"] if state_schedule else None
    state_std_ded = state_schedule["standard_deduction"] if state_schedule else None

    return TaxConfig(
        filing_status=filing_status,
        tax_year=tax_year,
        state=state,
        w2_income=scalars.get("w2.income", 0),
        w2_federal_withholding=scalars.get("w2.federal_withholding", 0),
        w2_state_withholding=scalars.get("w2.state_withholding", 0),
        federal_estimated_paid=scalars.get("estimated_payments.federal", 0),
        state_estimated_paid=scalars.get("estimated_payments.state", 0),
        enable_qbi_deduction=scalars.get("options.enable_qbi_deduction", False),
        se_income_accounts=list(se_income),
        se_expense_accounts=list(se_expense),
        prior_year_federal_tax=scalars.get("safe_harbor.prior_year_federal_tax", 0),
        prior_year_state_tax=scalars.get("safe_harbor.prior_year_state_tax", 0),
        federal_brackets=fed_brackets,
        state_brackets=state_brackets,
        federal_standard_deduction=fed_std_ded,
        state_standard_deduction=state_std_ded,
        ss_wage_base=ss_wage_base,
        ss_rate=ss_rate,
        medicare_rate=medicare_rate,
        se_taxable_fraction=se_taxable_fraction,
    )


def save_tax(
    db_path: Path | str,
    cfg: TaxConfig,
    *,
    replace_collections: bool = True,
    write_schedules: bool = False,
) -> None:
    """Save :class:`TaxConfig` to the DB.

    ``write_schedules`` is off by default, and that default is load-bearing.
    Bracket and standard-deduction overrides are keyed on
    ``(tax_year, jurisdiction, filing_status)``, but a ``TaxConfig`` carries
    only one set of them with no record of which coordinates they were read
    under — so a load-modify-save that changes the year, the state or the
    filing status would file the *old* coordinates' values under the new ones.
    That is the very defect the status dimension was added to fix: an override
    entered while filing jointly reappearing after switching to single.

    Every load-modify-save caller (``PUT /config/tax``, ``istota money tax
    set``) therefore leaves it off and edits overrides through
    :func:`upsert_tax_schedule` / :func:`delete_tax_schedule`, which name their
    coordinates explicitly. Only the importers pass True, where the values
    genuinely are the user's own rates for the config's own year and status.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        for key in _TAX_SCALAR_KEYS:
            value = _tax_scalar(cfg, key)
            if value is None or value == 0 or value is False:
                # Don't write zero/false defaults — keep the DB lean.
                _kv_delete(conn, "tax_settings", key)
            else:
                _kv_set(conn, "tax_settings", key, value)
        # Always write filing_status + tax_year (load needs them).
        _kv_set(conn, "tax_settings", "filing_status", cfg.filing_status)
        _kv_set(conn, "tax_settings", "tax_year", cfg.tax_year)

        if replace_collections:
            conn.execute("DELETE FROM tax_account_patterns")
        for p in cfg.se_income_accounts or []:
            conn.execute(
                "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                "VALUES (?, ?)",
                ("se_income", p),
            )
        for p in cfg.se_expense_accounts or []:
            conn.execute(
                "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                "VALUES (?, ?)",
                ("se_expense", p),
            )

        # Payroll scalars stay year-keyed; brackets and deductions go to the
        # (year, jurisdiction, filing_status) table.
        if any(v is not None for v in (
            cfg.ss_wage_base, cfg.ss_rate, cfg.medicare_rate,
            cfg.se_taxable_fraction,
        )):
            _upsert_year_rates(conn, cfg.tax_year, cfg)

        if write_schedules:
            _merge_schedule(
                conn, cfg.tax_year, FEDERAL_JURISDICTION, cfg.filing_status,
                cfg.federal_brackets, cfg.federal_standard_deduction,
            )
            if cfg.state:
                _merge_schedule(
                    conn, cfg.tax_year, cfg.state.upper(), cfg.filing_status,
                    cfg.state_brackets, cfg.state_standard_deduction,
                )


# =============================================================================
# Tax schedules — brackets and deductions per (year, jurisdiction, status)
# =============================================================================


def _validate_jurisdiction(jurisdiction: str) -> str:
    """Normalize and check a jurisdiction key: 'federal' or a real state code."""
    from istota.money.core.tax_data import load_tax_rates

    if not jurisdiction:
        raise ValueError("jurisdiction is required")
    if jurisdiction.lower() == FEDERAL_JURISDICTION:
        return FEDERAL_JURISDICTION
    code = jurisdiction.upper()
    if load_tax_rates().jurisdiction(code) is None:
        raise ValueError(f"unknown jurisdiction: {jurisdiction}")
    return code


def _validate_filing_status(filing_status: str) -> str:
    if filing_status not in FILING_STATUSES:
        raise ValueError(
            f"unknown filing status: {filing_status} "
            f"(expected one of {', '.join(FILING_STATUSES)})"
        )
    return filing_status


def _fetch_schedule(
    conn: sqlite3.Connection, year: int, jurisdiction: str, filing_status: str,
) -> dict | None:
    row = conn.execute(
        "SELECT brackets_json, standard_deduction FROM tax_schedules "
        "WHERE tax_year = ? AND jurisdiction = ? AND filing_status = ?",
        (year, jurisdiction, filing_status),
    ).fetchone()
    if row is None:
        return None
    return {
        "brackets": json.loads(row["brackets_json"]) if row["brackets_json"] else None,
        "standard_deduction": row["standard_deduction"],
    }


def _write_schedule(
    conn: sqlite3.Connection,
    year: int,
    jurisdiction: str,
    filing_status: str,
    brackets: list | None,
    standard_deduction: float | None,
) -> None:
    """Upsert one schedule row, or delete it when both fields are cleared.

    Deleting rather than storing a row of NULLs is what makes "revert to
    bundled" work: resolution order is override, then bundled, and a NULL row
    is still an override row.
    """
    if brackets is None and standard_deduction is None:
        conn.execute(
            "DELETE FROM tax_schedules "
            "WHERE tax_year = ? AND jurisdiction = ? AND filing_status = ?",
            (year, jurisdiction, filing_status),
        )
        return
    conn.execute(
        """
        INSERT INTO tax_schedules(
            tax_year, jurisdiction, filing_status, brackets_json, standard_deduction
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tax_year, jurisdiction, filing_status) DO UPDATE SET
            brackets_json = excluded.brackets_json,
            standard_deduction = excluded.standard_deduction
        """,
        (
            year, jurisdiction, filing_status,
            json.dumps([list(b) for b in brackets]) if brackets is not None else None,
            standard_deduction,
        ),
    )


def _merge_schedule(
    conn: sqlite3.Connection,
    year: int,
    jurisdiction: str,
    filing_status: str,
    brackets: list | None,
    standard_deduction: float | None,
) -> None:
    """Set whichever schedule fields are present; never clear one that is None.

    :func:`save_tax` is how a dozen callers across the CLI and the API persist
    an unrelated *scalar* edit, and the ``TaxConfig`` it is handed may have been
    loaded under a different year, state or filing status — in which case the
    schedule fields are None because they were never **read**, not because the
    user cleared them.

    Treating that None as "clear" was destructive: loading a config with no
    state selected, setting ``state = "CA"`` and saving deleted whatever
    California override the user (or the legacy migration) had already put
    there. Clearing an override is :func:`delete_tax_schedule`'s job, which is
    what the settings page's per-field revert calls.
    """
    if brackets is None and standard_deduction is None:
        return
    existing = _fetch_schedule(conn, year, jurisdiction, filing_status) or {}
    _write_schedule(
        conn, year, jurisdiction, filing_status,
        brackets if brackets is not None else existing.get("brackets"),
        standard_deduction if standard_deduction is not None
        else existing.get("standard_deduction"),
    )


def list_tax_schedules(db_path: Path | str) -> list[dict]:
    """Every stored bracket/deduction override, newest year first."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tax_schedules "
            "ORDER BY tax_year DESC, jurisdiction, filing_status"
        ).fetchall()
    return [
        {
            "tax_year": row["tax_year"],
            "jurisdiction": row["jurisdiction"],
            "filing_status": row["filing_status"],
            "standard_deduction": row["standard_deduction"],
            "brackets": (
                json.loads(row["brackets_json"]) if row["brackets_json"] else None
            ),
        }
        for row in rows
    ]


def get_tax_schedule(
    db_path: Path | str, year: int, jurisdiction: str, filing_status: str,
) -> dict | None:
    init_db(db_path)
    jurisdiction = _validate_jurisdiction(jurisdiction)
    filing_status = _validate_filing_status(filing_status)
    with _connect(db_path) as conn:
        return _fetch_schedule(conn, year, jurisdiction, filing_status)


def upsert_tax_schedule(
    db_path: Path | str,
    year: int,
    jurisdiction: str,
    filing_status: str,
    *,
    brackets: list | None | _Unset = UNSET,
    standard_deduction: float | None | _Unset = UNSET,
) -> str:
    """Merge fields into one schedule row. Returns 'created'/'updated'/'noop'.

    Merge rather than replace: the two fields are edited independently in the
    UI, so passing only one must not blank the other. Passing an explicit
    ``None`` *does* blank it — that is the per-field revert to the bundled
    value, which is why the default is :data:`UNSET` rather than ``None``.
    """
    init_db(db_path)
    jurisdiction = _validate_jurisdiction(jurisdiction)
    filing_status = _validate_filing_status(filing_status)
    with _connect(db_path) as conn:
        existing = _fetch_schedule(conn, year, jurisdiction, filing_status)
        merged_brackets = (
            existing["brackets"] if existing else None
        ) if isinstance(brackets, _Unset) else brackets
        merged_std = (
            existing["standard_deduction"] if existing else None
        ) if isinstance(standard_deduction, _Unset) else standard_deduction
        _write_schedule(
            conn, year, jurisdiction, filing_status, merged_brackets, merged_std,
        )
        if existing is None:
            return "created" if (merged_brackets is not None
                                 or merged_std is not None) else "noop"
        changed = (
            merged_brackets != existing["brackets"]
            or merged_std != existing["standard_deduction"]
        )
        return "updated" if changed else "noop"


def delete_tax_schedule(
    db_path: Path | str, year: int, jurisdiction: str, filing_status: str,
) -> bool:
    """Drop an override, reverting that field set to the bundled values."""
    init_db(db_path)
    jurisdiction = _validate_jurisdiction(jurisdiction)
    filing_status = _validate_filing_status(filing_status)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tax_schedules "
            "WHERE tax_year = ? AND jurisdiction = ? AND filing_status = ?",
            (year, jurisdiction, filing_status),
        )
        return cur.rowcount > 0


_TAX_SCHEDULES_MIGRATION_MARKER = "tax_schedules_migrated_v1"


def migrate_tax_schedules(db_path: Path | str) -> int:
    """Fold the legacy year-keyed bracket columns into ``tax_schedules``.

    One-time and markered. The legacy rows never recorded a filing status, so
    they are filed under the *configured* one — the honest reading of data that
    never had the dimension. ``INSERT OR IGNORE`` on top of the marker means a
    re-run cannot clobber an edit the user made after the first pass.

    Returns the number of rows written.
    """
    init_db(db_path)
    if get_meta(db_path, _TAX_SCHEDULES_MIGRATION_MARKER):
        return 0

    with _connect(db_path) as conn:
        status = _kv_all(conn, "tax_settings").get("filing_status", "mfj")
        if status not in FILING_STATUSES:
            status = "mfj"
        written = 0
        for row in conn.execute("SELECT * FROM tax_year_rates").fetchall():
            year = row["tax_year"]
            legacy = (
                (FEDERAL_JURISDICTION,
                 row["federal_brackets_json"], row["federal_standard_deduction"]),
                ("CA", row["ca_brackets_json"], row["ca_standard_deduction"]),
            )
            for jurisdiction, brackets_json, std_ded in legacy:
                if not brackets_json and std_ded is None:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tax_schedules("
                    "  tax_year, jurisdiction, filing_status,"
                    "  brackets_json, standard_deduction"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (year, jurisdiction, status, brackets_json, std_ded),
                )
                written += cur.rowcount
    set_meta(db_path, _TAX_SCHEDULES_MIGRATION_MARKER, "1")
    return written


def _tax_scalar(cfg: TaxConfig, key: str) -> Any:
    if key == "filing_status":
        return cfg.filing_status
    if key == "tax_year":
        return cfg.tax_year
    if key == "state":
        return cfg.state
    if key == "w2.income":
        return cfg.w2_income
    if key == "w2.federal_withholding":
        return cfg.w2_federal_withholding
    if key == "w2.state_withholding":
        return cfg.w2_state_withholding
    if key == "estimated_payments.federal":
        return cfg.federal_estimated_paid
    if key == "estimated_payments.state":
        return cfg.state_estimated_paid
    if key == "options.enable_qbi_deduction":
        return cfg.enable_qbi_deduction
    if key == "safe_harbor.prior_year_federal_tax":
        return cfg.prior_year_federal_tax
    if key == "safe_harbor.prior_year_state_tax":
        return cfg.prior_year_state_tax
    return None


def _upsert_year_rates(
    conn: sqlite3.Connection, year: int, cfg: TaxConfig,
) -> None:
    """Write the payroll scalars for a year.

    Deliberately does not touch the legacy bracket/deduction columns — those
    are read once by `migrate_tax_schedules` and never written again, since
    they lack the filing-status dimension the values actually have.
    """
    conn.execute(
        """
        INSERT INTO tax_year_rates(
            tax_year, ss_wage_base, ss_rate, medicare_rate, se_taxable_fraction
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tax_year) DO UPDATE SET
            ss_wage_base = excluded.ss_wage_base,
            ss_rate = excluded.ss_rate,
            medicare_rate = excluded.medicare_rate,
            se_taxable_fraction = excluded.se_taxable_fraction
        """,
        (year, cfg.ss_wage_base, cfg.ss_rate, cfg.medicare_rate,
         cfg.se_taxable_fraction),
    )


def add_tax_pattern(db_path: Path | str, kind: str, pattern: str) -> str:
    """Add an SE account pattern. Returns 'created' or 'noop'."""
    init_db(db_path)
    if kind not in ("se_income", "se_expense"):
        raise ValueError(f"unknown pattern kind: {kind}")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
            "VALUES (?, ?)",
            (kind, pattern),
        )
        return "created" if cur.rowcount > 0 else "noop"


def remove_tax_pattern(db_path: Path | str, kind: str, pattern: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tax_account_patterns WHERE kind = ? AND pattern = ?",
            (kind, pattern),
        )
        return cur.rowcount > 0


def replace_tax_patterns(
    db_path: Path | str, kind_to_patterns: dict[str, list[str]],
) -> None:
    """Replace-all per kind. Keys not present are left untouched."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for kind, patterns in kind_to_patterns.items():
            if kind not in ("se_income", "se_expense"):
                raise ValueError(f"unknown pattern kind: {kind}")
            conn.execute(
                "DELETE FROM tax_account_patterns WHERE kind = ?", (kind,),
            )
            for p in patterns or []:
                conn.execute(
                    "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                    "VALUES (?, ?)", (kind, p),
                )


def list_tax_patterns(db_path: Path | str) -> dict[str, list[str]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, pattern FROM tax_account_patterns ORDER BY kind, pattern"
        ).fetchall()
    out: dict[str, list[str]] = {"se_income": [], "se_expense": []}
    for row in rows:
        out.setdefault(row["kind"], []).append(row["pattern"])
    return out


def list_tax_year_rates(db_path: Path | str) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tax_year_rates ORDER BY tax_year"
        ).fetchall()
    out = []
    for row in rows:
        out.append({
            "tax_year": row["tax_year"],
            "ss_wage_base": row["ss_wage_base"],
            "ss_rate": row["ss_rate"],
            "medicare_rate": row["medicare_rate"],
            "se_taxable_fraction": row["se_taxable_fraction"],
            "federal_standard_deduction": row["federal_standard_deduction"],
            "ca_standard_deduction": row["ca_standard_deduction"],
            "federal_brackets": (
                json.loads(row["federal_brackets_json"])
                if row["federal_brackets_json"] else None
            ),
            "ca_brackets": (
                json.loads(row["ca_brackets_json"])
                if row["ca_brackets_json"] else None
            ),
        })
    return out


def upsert_tax_year_rates(db_path: Path | str, year: int, **fields: Any) -> str:
    """Upsert a single ``tax_year_rates`` row. Returns 'created'/'updated'/'noop'.

    An omitted field is left alone; an explicit ``None`` clears it, matching
    :func:`upsert_tax_schedule`. The four legacy bracket/deduction fields are
    still accepted and still written, but nothing reads them any more — see
    :func:`migrate_tax_schedules`.
    """
    init_db(db_path)
    allowed = {
        "ss_wage_base", "ss_rate", "medicare_rate", "se_taxable_fraction",
        "federal_standard_deduction", "ca_standard_deduction",
        "federal_brackets", "ca_brackets",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown tax_year_rates fields: {sorted(bad)}")
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM tax_year_rates WHERE tax_year = ?", (year,),
        ).fetchone()
        merged: dict[str, Any] = {k: None for k in allowed}
        if existing is not None:
            for k in allowed:
                if k in ("federal_brackets", "ca_brackets"):
                    merged[k] = (
                        json.loads(existing[f"{k}_json"])
                        if existing[f"{k}_json"] else None
                    )
                else:
                    merged[k] = existing[k]
        before = dict(merged)
        for k, v in fields.items():
            # An explicit None clears the override, the way it does for a
            # schedule. Skipping it made a payroll override unclearable — the
            # settings page has no Revert for these fields, so an entered wage
            # base could never be returned to the shipped one.
            if isinstance(v, _Unset):
                continue
            merged[k] = v
        fed_brackets_json = (
            json.dumps([list(b) for b in merged["federal_brackets"]])
            if merged["federal_brackets"] is not None else None
        )
        ca_brackets_json = (
            json.dumps([list(b) for b in merged["ca_brackets"]])
            if merged["ca_brackets"] is not None else None
        )
        conn.execute(
            """
            INSERT INTO tax_year_rates(
                tax_year, ss_wage_base, ss_rate, medicare_rate, se_taxable_fraction,
                federal_standard_deduction, ca_standard_deduction,
                federal_brackets_json, ca_brackets_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tax_year) DO UPDATE SET
                ss_wage_base = excluded.ss_wage_base,
                ss_rate = excluded.ss_rate,
                medicare_rate = excluded.medicare_rate,
                se_taxable_fraction = excluded.se_taxable_fraction,
                federal_standard_deduction = excluded.federal_standard_deduction,
                ca_standard_deduction = excluded.ca_standard_deduction,
                federal_brackets_json = excluded.federal_brackets_json,
                ca_brackets_json = excluded.ca_brackets_json
            """,
            (year, merged["ss_wage_base"], merged["ss_rate"],
             merged["medicare_rate"], merged["se_taxable_fraction"],
             merged["federal_standard_deduction"], merged["ca_standard_deduction"],
             fed_brackets_json, ca_brackets_json),
        )
    if existing is None:
        return "created"
    return "noop" if before == merged else "updated"


def delete_tax_year_rates(db_path: Path | str, year: int) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tax_year_rates WHERE tax_year = ?", (year,),
        )
        return cur.rowcount > 0


# =============================================================================
# Monarch
# =============================================================================


def monarch_config_from_toml_dict(
    data: dict, secrets: dict | None = None,
) -> MonarchConfig:
    """Hydrate :class:`MonarchConfig` from a parsed TOML dict.

    ``secrets`` is the optional credentials overlay (typically pulled from
    the encrypted ``secrets`` table).
    """
    monarch = data.get("monarch", {}) or {}
    secret_creds = (secrets or {}).get("monarch", {}) or {}
    credentials = MonarchCredentials(
        session_id=(
            secret_creds.get("session_id") or monarch.get("session_id")
        ),
        csrftoken=(
            secret_creds.get("csrftoken") or monarch.get("csrftoken")
        ),
    )

    sync_data = monarch.get("sync", {}) or {}
    sync = MonarchSyncSettings(
        lookback_days=sync_data.get("lookback_days", 30),
        default_account=sync_data.get(
            "default_account", "Assets:Bank:Checking",
        ),
        recategorize_account=sync_data.get(
            "recategorize_account", "Expenses:Personal-Expense",
        ),
    )

    accounts = dict(monarch.get("accounts") or {})
    categories = dict(monarch.get("categories") or {})
    tags_data = monarch.get("tags", {}) or {}
    tags = MonarchTagFilters(
        include=list(tags_data.get("include", []) or []),
        exclude=list(tags_data.get("exclude", []) or []),
    )

    profiles: list[MonarchProfile] = []
    for name, raw in (monarch.get("profiles") or {}).items():
        profile_sync_data = raw.get("sync", {}) or {}
        profile_sync = MonarchSyncSettings(
            lookback_days=profile_sync_data.get(
                "lookback_days", raw.get("lookback_days", sync.lookback_days),
            ),
            default_account=raw.get(
                "default_account",
                profile_sync_data.get("default_account", sync.default_account),
            ),
            recategorize_account=raw.get(
                "recategorize_account",
                profile_sync_data.get(
                    "recategorize_account", sync.recategorize_account,
                ),
            ),
        )
        profile_tags_data = raw.get("tags", {}) or {}
        profile_tags = MonarchTagFilters(
            include=list(profile_tags_data.get("include", []) or []),
            exclude=list(profile_tags_data.get("exclude", []) or []),
        )
        profile_accounts = raw.get("accounts")
        profile_accounts = dict(profile_accounts) if profile_accounts else dict(accounts)
        profile_categories = raw.get("categories")
        profile_categories = (
            dict(profile_categories) if profile_categories else dict(categories)
        )
        profiles.append(MonarchProfile(
            name=name,
            ledger=raw.get("ledger", name),
            sync=profile_sync,
            accounts=profile_accounts,
            categories=profile_categories,
            tags=profile_tags,
        ))

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=accounts,
        categories=categories,
        tags=tags,
        profiles=profiles,
    )


def monarch_to_toml_dict(cfg: MonarchConfig) -> dict:
    """Render :class:`MonarchConfig` back to a TOML-shaped dict.

    Credentials are intentionally omitted — they live in the encrypted
    ``secrets`` table.
    """
    monarch: dict[str, Any] = {}
    sync = {
        "lookback_days": cfg.sync.lookback_days,
        "default_account": cfg.sync.default_account,
        "recategorize_account": cfg.sync.recategorize_account,
    }
    monarch["sync"] = sync

    if cfg.accounts:
        monarch["accounts"] = dict(sorted(cfg.accounts.items()))
    if cfg.categories:
        monarch["categories"] = dict(sorted(cfg.categories.items()))
    if cfg.tags.include or cfg.tags.exclude:
        tags: dict[str, Any] = {}
        if cfg.tags.include:
            tags["include"] = list(cfg.tags.include)
        if cfg.tags.exclude:
            tags["exclude"] = list(cfg.tags.exclude)
        monarch["tags"] = tags

    if cfg.profiles:
        profiles: dict[str, Any] = {}
        for p in sorted(cfg.profiles, key=lambda x: x.name):
            entry: dict[str, Any] = {"ledger": p.ledger}
            if p.sync.lookback_days != cfg.sync.lookback_days:
                entry["lookback_days"] = p.sync.lookback_days
            if p.sync.default_account != cfg.sync.default_account:
                entry["default_account"] = p.sync.default_account
            if p.sync.recategorize_account != cfg.sync.recategorize_account:
                entry["recategorize_account"] = p.sync.recategorize_account
            if p.accounts and p.accounts != cfg.accounts:
                entry["accounts"] = dict(sorted(p.accounts.items()))
            if p.categories and p.categories != cfg.categories:
                entry["categories"] = dict(sorted(p.categories.items()))
            if p.tags.include or p.tags.exclude:
                ptags: dict[str, Any] = {}
                if p.tags.include:
                    ptags["include"] = list(p.tags.include)
                if p.tags.exclude:
                    ptags["exclude"] = list(p.tags.exclude)
                entry["tags"] = ptags
            profiles[p.name] = entry
        monarch["profiles"] = profiles

    return {"monarch": monarch}


def load_monarch(
    db_path: Path | str, secrets: dict | None = None,
) -> MonarchConfig:
    """Load :class:`MonarchConfig` from the DB.

    ``secrets`` overlays credentials onto the loaded config.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "monarch_settings")
        sync = MonarchSyncSettings(
            lookback_days=scalars.get("sync.lookback_days", 30),
            default_account=scalars.get(
                "sync.default_account", "Assets:Bank:Checking",
            ),
            recategorize_account=scalars.get(
                "sync.recategorize_account", "Expenses:Personal-Expense",
            ),
        )

        global_accounts = _load_account_map(conn, GLOBAL_PROFILE_ID)
        global_categories = _load_category_map(conn, GLOBAL_PROFILE_ID)
        global_tags = _load_tag_filters(conn, GLOBAL_PROFILE_ID)

        profiles: list[MonarchProfile] = []
        rows = conn.execute(
            "SELECT * FROM monarch_profiles WHERE id != ? ORDER BY name",
            (GLOBAL_PROFILE_ID,),
        ).fetchall()
        for row in rows:
            pid = row["id"]
            psync = MonarchSyncSettings(
                lookback_days=row["lookback_days"] if row["lookback_days"] is not None
                else sync.lookback_days,
                default_account=row["default_account"] or sync.default_account,
                recategorize_account=row["recategorize_account"] or sync.recategorize_account,
            )
            paccounts = _load_account_map(conn, pid) or dict(global_accounts)
            pcategories = _load_category_map(conn, pid) or dict(global_categories)
            ptags = _load_tag_filters(conn, pid)
            profiles.append(MonarchProfile(
                name=row["name"],
                ledger=row["ledger"],
                sync=psync,
                accounts=paccounts,
                categories=pcategories,
                tags=ptags,
            ))

    secret_creds = (secrets or {}).get("monarch", {}) or {}
    credentials = MonarchCredentials(
        session_id=secret_creds.get("session_id"),
        csrftoken=secret_creds.get("csrftoken"),
    )

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=global_accounts,
        categories=global_categories,
        tags=global_tags,
        profiles=profiles,
    )


def _load_account_map(conn: sqlite3.Connection, profile_id: int) -> dict[str, str]:
    """The account map for one profile, as a view over `transaction_rules`."""
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        return _legacy_account_map(conn, profile_id)
    return _map_view(conn, scope, "account", "contra_account")


def _load_category_map(conn: sqlite3.Connection, profile_id: int) -> dict[str, str]:
    """The category map for one profile, as a view over `transaction_rules`."""
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        return _legacy_category_map(conn, profile_id)
    return _map_view(conn, scope, "category", "posting_account")


def _load_tag_filters(conn: sqlite3.Connection, profile_id: int) -> MonarchTagFilters:
    """Include from the old table, exclude from the rules.

    The two halves live apart on purpose. An exclude tag translates exactly to
    a `skip` rule, which is the general form and covers more; an include list
    is a gate over the whole set — *if any are configured, the row must carry
    one* — so a rule expressing it would mean something different depending on
    whether its siblings existed, and deleting the last one would silently
    change every other rule's behaviour.
    """
    legacy = _legacy_tag_filters(conn, profile_id)
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        return legacy
    return MonarchTagFilters(
        include=legacy.include,
        exclude=_tag_skip_view(conn, scope),
    )


def save_monarch(
    db_path: Path | str,
    cfg: MonarchConfig,
    *,
    replace_collections: bool = True,
) -> None:
    """Save :class:`MonarchConfig` to the DB.

    Credentials are NOT persisted here — they belong to the encrypted
    ``secrets`` table managed by :mod:`istota.secrets_store`.

    **Two profiles bound to one ledger share one rule scope**, and that is the
    one shape this cannot round-trip. Their maps used to be `profile_id`-keyed
    and independent; a rule carries a ledger, so the two are written into the
    same scope and a key they disagree about has one answer. Writing them in
    turn would be worse than lossy — each `clear=True` pass deletes what the
    previous profile just wrote, so the *whole* of the first profile's map
    disappears — so they are merged first and the earlier profile wins a
    contested key, which is the rule `_migrate_insert_rule` already applies to
    the same contradiction.
    """
    _check_profile_accounts({
        "default_account": cfg.sync.default_account,
        "recategorize_account": cfg.sync.recategorize_account,
    }, allow_empty=False)
    init_db(db_path)
    with _connect(db_path) as conn:
        _kv_set(conn, "monarch_settings",
                "sync.lookback_days", cfg.sync.lookback_days)
        _kv_set(conn, "monarch_settings",
                "sync.default_account", cfg.sync.default_account)
        _kv_set(conn, "monarch_settings",
                "sync.recategorize_account", cfg.sync.recategorize_account)

        if replace_collections:
            # Cascade clears child rows for global, then profiles.
            conn.execute(
                "DELETE FROM monarch_account_map WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_category_map WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_tag_filters WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_profiles WHERE id != ?",
                (GLOBAL_PROFILE_ID,),
            )
            # Rules carry a ledger, not a profile id, so the FK cascade above
            # does not reach them: a profile this config drops would otherwise
            # leave its map behind in a scope nothing owns.
            _clear_all_map_views(conn)

        _replace_account_map(conn, GLOBAL_PROFILE_ID, cfg.accounts, clear=False)
        _replace_category_map(conn, GLOBAL_PROFILE_ID, cfg.categories, clear=False)
        _replace_tag_filters(conn, GLOBAL_PROFILE_ID, cfg.tags, clear=False)

        # One pass to create the rows, so every profile has an id and a scope
        # before anything is written against it.
        pids = [(p, _upsert_profile_row(conn, p, cfg.sync)) for p in cfg.profiles]

        merged: dict[str, tuple[dict[str, str], dict[str, str], list[str]]] = {}
        for p, pid in pids:
            scope = _rule_scope(conn, pid)
            if scope is None:
                # No rule scope: this profile's maps stay `profile_id`-keyed in
                # the old tables, where they round-trip exactly as before.
                _replace_account_map(conn, pid, p.accounts, clear=True)
                _replace_category_map(conn, pid, p.categories, clear=True)
                _replace_tag_filters(conn, pid, p.tags, clear=True)
                continue
            # The include list is per-profile whatever the scope, so it is
            # written here rather than folded into the merge.
            _replace_tag_filters(
                conn, pid, MonarchTagFilters(include=p.tags.include, exclude=[]),
                clear=True,
            )
            accounts, categories, excludes = merged.setdefault(scope, ({}, {}, []))
            for key, value in (p.accounts or {}).items():
                accounts.setdefault(key, value)
            for key, value in (p.categories or {}).items():
                categories.setdefault(key, value)
            for tag in (p.tags.exclude or []):
                if tag not in excludes:
                    excludes.append(tag)

        for scope, (accounts, categories, excludes) in merged.items():
            for key, value in accounts.items():
                _check_map_key("account-map", key)
                _check_map_account("account-map", key, value)
            for key, value in categories.items():
                _check_map_key("category-map", key)
                _check_map_account("category-map", key, value)
            for tag in excludes:
                _check_map_key("tag-filter", tag)
            _sync_map_rules(
                conn, ledger=scope, view="account", mapping=accounts,
                origin="user",
            )
            _sync_map_rules(
                conn, ledger=scope, view="category", mapping=categories,
                origin="user",
            )
            _sync_tag_skip_rules(conn, scope, excludes, clear=True)


_SYNC_SCALARS = ("lookback_days", "default_account", "recategorize_account")


def set_monarch_sync(db_path: Path | str, **fields: Any) -> dict:
    """Write only the global sync settings the caller named.

    `save_monarch` takes a whole `MonarchConfig` and so validates the whole
    record, which is right for an import and wrong for an edit: this module's
    rule is that only the fields a caller actually passes are checked, or an
    existing non-conforming row makes everything around it uneditable.
    """
    unknown = set(fields) - set(_SYNC_SCALARS)
    if unknown:
        raise ValueError(f"unknown sync settings: {sorted(unknown)}")
    _check_profile_accounts(fields, allow_empty=False)
    init_db(db_path)
    with _connect(db_path) as conn:
        for name in _SYNC_SCALARS:
            if name in fields:
                _kv_set(conn, "monarch_settings", f"sync.{name}", fields[name])
    return dict(fields)


def _upsert_profile_row(
    conn: sqlite3.Connection,
    p: MonarchProfile,
    global_sync: MonarchSyncSettings,
) -> int:
    _check_profile_accounts({
        "default_account": p.sync.default_account,
        "recategorize_account": p.sync.recategorize_account,
    })
    lookback = (
        p.sync.lookback_days
        if p.sync.lookback_days != global_sync.lookback_days
        else None
    )
    default_acc = (
        p.sync.default_account
        if p.sync.default_account != global_sync.default_account
        else None
    )
    recat_acc = (
        p.sync.recategorize_account
        if p.sync.recategorize_account != global_sync.recategorize_account
        else None
    )
    conn.execute(
        """
        INSERT INTO monarch_profiles(
            name, ledger, lookback_days, default_account, recategorize_account
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            ledger = excluded.ledger,
            lookback_days = excluded.lookback_days,
            default_account = excluded.default_account,
            recategorize_account = excluded.recategorize_account
        """,
        (p.name, p.ledger, lookback, default_acc, recat_acc),
    )
    row = conn.execute(
        "SELECT id FROM monarch_profiles WHERE name = ?", (p.name,),
    ).fetchone()
    return row["id"]


_LEGACY_MAP_TABLES = {
    "account": ("monarch_account_map", "monarch_name", "account-map"),
    "category": ("monarch_category_map", "monarch_category", "category-map"),
}


def _legacy_replace_map(
    conn: sqlite3.Connection, profile_id: int, view: str,
    mapping: dict[str, str], *, clear: bool,
) -> None:
    table, key_column, _ = _LEGACY_MAP_TABLES[view]
    if clear:
        conn.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))
    for key, account in (mapping or {}).items():
        conn.execute(
            f"INSERT OR REPLACE INTO {table}(profile_id, {key_column}, "
            "beancount_account) VALUES (?, ?, ?)",
            (profile_id, key, account),
        )


def _replace_map(
    conn: sqlite3.Connection, profile_id: int, view: str,
    mapping: dict[str, str], *, clear: bool,
) -> None:
    """Write one dict view. `clear=False` merges, as the old upsert did."""
    kind = _LEGACY_MAP_TABLES[view][2]
    for key, value in (mapping or {}).items():
        _check_map_account(kind, key, value)
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        # The fallback path is the previous release, so it takes the previous
        # release's validation: `_check_map_key` refuses two key shapes the old
        # tables accept, and applying it here would make a deployment whose
        # migration failed reject a config it used to store.
        _legacy_replace_map(conn, profile_id, view, mapping, clear=clear)
        return
    for key in (mapping or {}):
        _check_map_key(kind, key)
    field, action = _MAP_VIEWS[view]
    effective = dict(mapping or {})
    if not clear:
        effective = {**_map_view(conn, scope, field, action), **effective}
    _sync_map_rules(
        conn, ledger=scope, view=view, mapping=effective, origin="user",
    )


def _replace_account_map(
    conn: sqlite3.Connection, profile_id: int, mapping: dict[str, str], *, clear: bool,
) -> None:
    _replace_map(conn, profile_id, "account", mapping, clear=clear)


def _replace_category_map(
    conn: sqlite3.Connection, profile_id: int, mapping: dict[str, str], *, clear: bool,
) -> None:
    _replace_map(conn, profile_id, "category", mapping, clear=clear)


def _replace_tag_filters(
    conn: sqlite3.Connection, profile_id: int, tags: MonarchTagFilters, *, clear: bool,
) -> None:
    if clear:
        conn.execute(
            "DELETE FROM monarch_tag_filters WHERE profile_id = ? AND kind = ?",
            (profile_id, "include"),
        )
    for tag in (tags.include or []):
        conn.execute(
            "INSERT OR IGNORE INTO monarch_tag_filters("
            "profile_id, kind, tag) VALUES (?, ?, ?)",
            (profile_id, "include", tag),
        )
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        if clear:
            conn.execute(
                "DELETE FROM monarch_tag_filters WHERE profile_id = ? AND kind = ?",
                (profile_id, "exclude"),
            )
        for tag in (tags.exclude or []):
            conn.execute(
                "INSERT OR IGNORE INTO monarch_tag_filters("
                "profile_id, kind, tag) VALUES (?, ?, ?)",
                (profile_id, "exclude", tag),
            )
        return
    for tag in (tags.exclude or []):
        _check_map_key("tag-filter", tag)
    _sync_tag_skip_rules(conn, scope, list(tags.exclude or []), clear=clear)


# Granular monarch ops ---------------------------------------------------------


def _resolve_profile_id(conn: sqlite3.Connection, name: str | None) -> int:
    if name is None:
        return GLOBAL_PROFILE_ID
    row = conn.execute(
        "SELECT id FROM monarch_profiles WHERE name = ?", (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown monarch profile: {name}")
    return row["id"]


def list_monarch_profiles(db_path: Path | str) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, ledger, lookback_days, default_account, "
            "recategorize_account FROM monarch_profiles "
            "WHERE id != ? ORDER BY name", (GLOBAL_PROFILE_ID,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_monarch_profile(
    db_path: Path | str, name: str, **fields: Any,
) -> tuple[dict, str]:
    """Upsert a monarch profile. ``ledger`` required for create."""
    _check_profile_accounts(fields)
    init_db(db_path)
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM monarch_profiles WHERE name = ?", (name,),
        ).fetchone()
        if existing is None:
            ledger = fields.get("ledger")
            if not ledger:
                raise ValueError(f"creating profile '{name}' requires --ledger")
            conn.execute(
                "INSERT INTO monarch_profiles(name, ledger, lookback_days, "
                "default_account, recategorize_account) VALUES (?, ?, ?, ?, ?)",
                (name, ledger,
                 fields.get("lookback_days"),
                 fields.get("default_account"),
                 fields.get("recategorize_account")),
            )
            new_row = conn.execute(
                "SELECT * FROM monarch_profiles WHERE name = ?", (name,),
            ).fetchone()
            return dict(new_row), "created"
        before = dict(existing)
        merged = dict(before)
        for k in ("ledger", "lookback_days", "default_account", "recategorize_account"):
            if k in fields and fields[k] is not None:
                merged[k] = fields[k]
        # `ledger` is required on create and was blankable on update, which is
        # a plain gap in a NOT NULL column and became load-bearing when the
        # maps moved into a ledger-scoped table: an empty ledger has no rule
        # scope of its own, so the profile falls back to the old tables and its
        # map stops being visible to anything reading rules.
        if not merged["ledger"]:
            raise ValueError(f"profile '{name}' requires a non-empty ledger")
        conn.execute(
            "UPDATE monarch_profiles SET ledger = ?, lookback_days = ?, "
            "default_account = ?, recategorize_account = ? WHERE name = ?",
            (merged["ledger"], merged["lookback_days"],
             merged["default_account"], merged["recategorize_account"], name),
        )
        return merged, ("noop" if merged == before else "updated")


def delete_monarch_profile(db_path: Path | str, name: str) -> bool:
    """Delete a profile, and with it the maps only that profile expressed.

    The old maps were `profile_id`-keyed and went with the row on the FK
    cascade, so deleting and recreating a profile left it inheriting the global
    map again. Rules are keyed on a *ledger*, which the cascade cannot reach,
    so the same scope is cleared here — but only when no surviving profile
    still names that ledger, since two profiles on one ledger share one scope
    and deleting one must not take the other's map with it. Only the
    representable subset goes: a `contains` rule or a rule on another source
    was never part of this profile's map.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, ledger FROM monarch_profiles WHERE name = ? AND id != ?",
            (name, GLOBAL_PROFILE_ID),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "DELETE FROM monarch_profiles WHERE name = ? AND id != ?",
            (name, GLOBAL_PROFILE_ID),
        )
        ledger = row["ledger"] or ""
        still_used = conn.execute(
            "SELECT 1 FROM monarch_profiles WHERE ledger = ? AND id != ? LIMIT 1",
            (ledger, GLOBAL_PROFILE_ID),
        ).fetchone()
        if ledger and still_used is None and _rules_migrated(conn):
            _sync_map_rules(
                conn, ledger=ledger, view="account", mapping={}, origin="user",
            )
            _sync_map_rules(
                conn, ledger=ledger, view="category", mapping={}, origin="user",
            )
            _sync_tag_skip_rules(conn, ledger, [], clear=True)
        return cur.rowcount > 0


class InvalidAccountError(ValueError):
    """A configured value that is not a beancount account.

    A subclass so every existing ``except ValueError`` keeps working, and a
    distinct type so the legacy importer can tell content it should refuse from
    a coercion failure it should not swallow.
    """


def _check_map_account(kind: str, key: str, value: str) -> None:
    """Reject a mapping target that beancount could not parse.

    A Monarch map is the only config that names an account the sync writes
    straight into the ledger, so an unparseable one here is not caught until a
    later `check` fails on a transaction already appended.
    """
    if not isinstance(value, str) or not _is_account(value):
        raise InvalidAccountError(
            f"invalid {kind} account for {key!r}: {value!r} — expected a "
            "beancount account like Expenses:Internet-Services",
        )


_PROFILE_ACCOUNT_FIELDS = ("default_account", "recategorize_account")


def _check_profile_accounts(fields: dict, *, allow_empty: bool = True) -> None:
    """Reject a profile account beancount could not parse.

    `map_monarch_account` returns `default_account` verbatim for a Monarch
    account with no mapping, so it reaches the ledger the same way a map target
    does.

    ``allow_empty`` is the difference between the two stores. On a profile row
    an empty value clears the column and `load_monarch` falls back to the
    global setting, so it is a way of saying "inherit". The global settings have
    nothing beneath them and `_kv_set` writes the empty string through, so there
    ``""`` is a posting with no account rather than a default.
    """
    for name in _PROFILE_ACCOUNT_FIELDS:
        value = fields.get(name)
        if value is None or (allow_empty and value == ""):
            continue
        if not isinstance(value, str) or not _is_account(value):
            raise InvalidAccountError(
                f"invalid {name}: {value!r} — expected a beancount account "
                "like Assets:Bank:Checking",
            )


def _set_map_entry(
    conn: sqlite3.Connection, profile_id: int, view: str, key: str, account: str,
) -> str:
    """Set one key of a dict view, re-deriving the scope's whole emission.

    Re-deriving rather than touching one row is what the case-collision
    encoding requires: adding a key that collides with an existing one has to
    introduce an `exact` tier that was not there before, and only the whole map
    knows that. `_sync_rules` then writes just the difference.
    """
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        table, key_column, _ = _LEGACY_MAP_TABLES[view]
        existing = conn.execute(
            f"SELECT beancount_account FROM {table} "
            f"WHERE profile_id = ? AND {key_column} = ?",
            (profile_id, key),
        ).fetchone()
        if existing is None:
            conn.execute(
                f"INSERT INTO {table}(profile_id, {key_column}, "
                "beancount_account) VALUES (?, ?, ?)",
                (profile_id, key, account),
            )
            return "created"
        if existing["beancount_account"] == account:
            return "noop"
        conn.execute(
            f"UPDATE {table} SET beancount_account = ? "
            f"WHERE profile_id = ? AND {key_column} = ?",
            (account, profile_id, key),
        )
        return "updated"

    _check_map_key(_LEGACY_MAP_TABLES[view][2], key)
    field, action = _MAP_VIEWS[view]
    current = _map_view(conn, scope, field, action)
    if current.get(key) == account:
        return "noop"
    state = "updated" if key in current else "created"
    current[key] = account
    _sync_map_rules(conn, ledger=scope, view=view, mapping=current, origin="user")
    return state


def _unset_map_entry(
    conn: sqlite3.Connection, profile_id: int, view: str, key: str,
) -> bool:
    scope = _rule_scope(conn, profile_id)
    if scope is None:
        table, key_column, _ = _LEGACY_MAP_TABLES[view]
        cur = conn.execute(
            f"DELETE FROM {table} WHERE profile_id = ? AND {key_column} = ?",
            (profile_id, key),
        )
        return cur.rowcount > 0
    field, action = _MAP_VIEWS[view]
    current = _map_view(conn, scope, field, action)
    if key not in current:
        return False
    del current[key]
    _sync_map_rules(conn, ledger=scope, view=view, mapping=current, origin="user")
    return True


def set_account_map_entry(
    db_path: Path | str, profile: str | None,
    monarch_name: str, beancount_account: str,
) -> str:
    _check_map_account("account-map", monarch_name, beancount_account)
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _set_map_entry(conn, pid, "account", monarch_name, beancount_account)


def unset_account_map_entry(
    db_path: Path | str, profile: str | None, monarch_name: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _unset_map_entry(conn, pid, "account", monarch_name)


def get_account_map(
    db_path: Path | str, profile: str | None,
) -> dict[str, str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _load_account_map(conn, pid)


def replace_account_map(
    db_path: Path | str, profile: str | None, mapping: dict[str, str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_account_map(conn, pid, mapping, clear=True)


def set_category_map_entry(
    db_path: Path | str, profile: str | None,
    category: str, beancount_account: str,
) -> str:
    _check_map_account("category-map", category, beancount_account)
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _set_map_entry(conn, pid, "category", category, beancount_account)


def unset_category_map_entry(
    db_path: Path | str, profile: str | None, category: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _unset_map_entry(conn, pid, "category", category)


def get_category_map(
    db_path: Path | str, profile: str | None,
) -> dict[str, str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _load_category_map(conn, pid)


def replace_category_map(
    db_path: Path | str, profile: str | None, mapping: dict[str, str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_category_map(conn, pid, mapping, clear=True)


def add_tag_filter(
    db_path: Path | str, profile: str | None, kind: str, tag: str,
) -> str:
    init_db(db_path)
    if kind not in ("include", "exclude"):
        raise ValueError(f"unknown tag filter kind: {kind}")
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        scope = _rule_scope(conn, pid)
        if kind == "include" or scope is None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO monarch_tag_filters("
                "profile_id, kind, tag) VALUES (?, ?, ?)",
                (pid, kind, tag),
            )
            return "created" if cur.rowcount > 0 else "noop"
        _check_map_key("tag-filter", tag)
        if tag in _tag_skip_view(conn, scope):
            return "noop"
        _sync_tag_skip_rules(conn, scope, [tag], clear=False)
        return "created"


def remove_tag_filter(
    db_path: Path | str, profile: str | None, kind: str, tag: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        scope = _rule_scope(conn, pid)
        if kind == "include" or scope is None:
            cur = conn.execute(
                "DELETE FROM monarch_tag_filters WHERE profile_id = ? "
                "AND kind = ? AND tag = ?", (pid, kind, tag),
            )
            return cur.rowcount > 0
        current = _tag_skip_view(conn, scope)
        if tag not in current:
            return False
        _sync_tag_skip_rules(
            conn, scope, [t for t in current if t != tag], clear=True,
        )
        return True


def get_tag_filters(
    db_path: Path | str, profile: str | None,
) -> dict[str, list[str]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        tf = _load_tag_filters(conn, pid)
        return {"include": list(tf.include), "exclude": list(tf.exclude)}


def replace_tag_filters(
    db_path: Path | str, profile: str | None,
    include: list[str], exclude: list[str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_tag_filters(
            conn, pid, MonarchTagFilters(include=list(include), exclude=list(exclude)),
            clear=True,
        )


# =============================================================================
# Transaction rules
# =============================================================================
#
# One ordered rule set, source-agnostic, replacing three per-scope Monarch map
# tables. The engine that reads it is `core/rules.py`; this half owns the
# table, the one-time migration off the old tables, the shipped seed, and the
# dict-shaped **compatibility views** the three old endpoints, the two CLI
# surfaces and `load_monarch` keep working against.
#
# Three things here are decisions rather than consequences.
#
# **The migration materializes the effective map per profile, and the global
# tier layers beneath it.** Old inheritance is replacement, not layering —
# `_load_account_map(conn, pid) or dict(global_accounts)` means a profile with
# one own rule ignored the entire global map — so a profile that inherited the
# global map gets its own copy of it at its own ledger, and a later edit to a
# global rule no longer propagates into that copy.
#
# Materialization alone does not reproduce replacement, which is the part that
# is easy to get wrong: a `ledger=''` rule is in scope for *every* run, so the
# global map is layered underneath whether or not anybody chose it. What the
# tier priorities above settle is the order of that layering. See them for the
# measurement and for the two accepted consequences.
#
# **A case-colliding group's representative is its first key in the order the
# map is read back in.** The lookup being reproduced
# (`map_monarch_category_with_config`) scans exact over the whole map, then
# case-insensitive over the whole map in iteration order, and `PRIMARY KEY
# (profile_id, monarch_category)` permits two keys that differ only in case.
# `_emit_map_entries` reproduces that with an `exact` tier ahead of the `iexact`
# tier and one `iexact` rule per group, taking the group's first key in the
# mapping's own iteration order.
#
# Which order that has to be is the part worth stating, because the obvious
# answer is wrong in one direction. It is not the order a caller built a dict
# in — it is the order the *view* produces, since the view is what every reader
# of these maps sees. Both the old `_legacy_category_map` and the new
# `_map_view` sort (`ORDER BY monarch_category`, `ORDER BY match_value`), so
# the migration's maps arrive already in it and a caller's dict does not.
# `_sync_map_rules` therefore sorts before emitting; without that a
# `{"food": X, "Food": Y}` PUT stored `food` as the representative while the
# view read `Food` first, and the engine and the view would then answer
# different accounts for `FOOD`.
#
# **Every write of a dict view re-derives the whole scope's emission** rather
# than touching one row. The collision encoding is a property of the map, not
# of a key: setting a second key that collides with an existing one has to add
# an `exact` tier that was not there before, and unsetting a group's
# representative has to promote the next member. `_sync_map_rules` computes the
# desired emission, then deletes, updates and inserts only what differs, so an
# unrelated rule keeps its id, its priority and its note.

_RULES_MIGRATION_SENTINEL = "transaction_rules_migrated_at"
_RULES_SEED_SENTINEL = "transaction_rules_seeded_at"
# Everything the migration could not carry, in one place: the duplicate rows it
# dropped and the two map-key shapes no rule can represent.
_RULES_MIGRATION_NOTES = "transaction_rules_migration_conflicts"

# Four tiers in one ordered list, lowest first. Skip rules run ahead of every
# mapping rule; a ledger's own map runs ahead of the global map; the shipped
# map runs behind everything, which is where the fallthrough tier already sat
# as a module constant.
#
# **The global tier is 200 and that is a decision, not a spacing convention.**
# A `ledger=''` rule is in scope for *every* run, so the global map is layered
# beneath a profile's whether or not anybody wanted it to be — writing both
# tiers at 100 does not prevent layering, it just leaves the order to the ids,
# and since the migration writes the global scope first those ids are lower.
# Measured before the fix: a profile owning `Software` lost to the global
# `Software` rule and posted to the global account, on the one deployment
# shape whose whole point is that the profile overrides. Two consequences,
# both accepted: a profile's own rule wins for a key it carries, which is the
# old answer restored; and a key only the global map carries now resolves from
# the global rule where old replacement semantics dropped through to the seed
# tier. The alternative — excluding wildcard-ledger rules when the run's
# ledger has migrated rules of its own — is a scope test branching on
# `origin`, which is the implicit specificity this design rejects, arriving
# through the back door.
_TAG_SKIP_PRIORITY = 50
_MAP_PRIORITY = 100
_GLOBAL_MAP_PRIORITY = 200
_SEED_PRIORITY = 900

# A collision group's `exact` tier sits just ahead of the `iexact` tier it
# belongs to, so it is derived rather than named: pinning it to one constant
# put a *global* exact rule at 90, ahead of a profile's `iexact` at 100, which
# is the same inversion the tiers above exist to remove — restricted to
# case-colliding keys, where it would have been that much harder to see.
_EXACT_TIER_OFFSET = 10

# A map rule is Monarch-shaped: an account *display name* genuinely is
# source-specific. A skip on a tag is a statement about the transaction rather
# than about where it came from, so it carries no source.
_MAP_SOURCE = "monarch-api"
_TAG_SOURCE = ""

# What a dict view can represent. `origin = 'seed'` is excluded because the
# shipped map was a module constant before this table existed and was never in
# any of these dicts; `map_monarch_category` still carries it as the fallback
# tier, so including it here would double it into `MonarchConfig.categories`
# and change every export.
_VIEW_SOURCES = ("", "monarch-api")
_VIEW_KINDS = ("exact", "iexact")

# view name -> (field, action)
_MAP_VIEWS = {
    "account": ("account", "contra_account"),
    "category": ("category", "posting_account"),
}

_RULE_COLUMNS = (
    "id", "ledger", "source", "field", "match_kind", "match_value",
    "action", "target", "priority", "enabled", "origin", "note",
    "created_at", "updated_at",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rules_migrated(conn: sqlite3.Connection) -> bool:
    """Whether the one-time migration off the old map tables has completed.

    Every compatibility view reads this first. A migration that failed leaves
    the sentinel unwritten and its own writes rolled back, so the views go on
    reading — and writing — the old tables and the deployment behaves exactly
    as it did before the upgrade. That fallback is the one thing that makes a
    failed migration safe.
    """
    row = conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = ?", (_RULES_MIGRATION_SENTINEL,),
    ).fetchone()
    return row is not None


def _rule_scope(conn: sqlite3.Connection, profile_id: int) -> str | None:
    """The rule scope one Monarch profile's maps live in, or `None` for none.

    The global profile is `''`, which the engine reads as "any ledger" — the
    same reach the global map had as the fallback beneath every profile.

    `None` says this profile has no scope in the rules table and every
    accessor must fall back to the old `profile_id`-keyed tables for it. Two
    states produce it, and they are the same statement: the rules table cannot
    express this profile's map, so nothing pretends it can.

    The first is the migration not having completed. The second is a
    non-global profile whose `ledger` column is empty, which would otherwise
    resolve to `''` and put that profile's map in the **global** scope — where
    a write clears and rewrites the global map, destroying it. The old tables
    were keyed on `profile_id` and were immune. `upsert_monarch_profile`
    refuses to create or to blank a ledger, but `save_monarch` writes
    `MonarchProfile.ledger` through `_upsert_profile_row` with no check, a
    stored row predating that guard is not revisited, and neither is a hand
    edit — so the collapse is refused here rather than assumed away.
    """
    if not _rules_migrated(conn):
        return None
    if profile_id == GLOBAL_PROFILE_ID:
        return ""
    row = conn.execute(
        "SELECT ledger FROM monarch_profiles WHERE id = ?", (profile_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["ledger"] or "") or None


# --- The dict views -----------------------------------------------------------


def _map_tier_priority(ledger: str) -> int:
    """Which mapping tier a scope's rules belong to.

    The global scope is `''`, which the engine reads as "any ledger", so its
    rules apply to every run alongside that run's own. They therefore sit a
    tier behind, and this is the one place that says so — the migration and
    every compatibility write take it from here, or a map written through the
    old endpoints after the migration would land at the profile tier and put
    the inversion back.
    """
    return _MAP_PRIORITY if ledger else _GLOBAL_MAP_PRIORITY


def _emit_map_entries(
    mapping: dict[str, str], priority: int,
) -> list[tuple[str, str, str, int]]:
    """One flat map as `(match_kind, match_value, target, priority)` rules.

    Groups of one emit a single `iexact` rule at `priority`. A group whose keys
    collide case-insensitively emits every member as an `exact` rule one tier
    ahead of that, plus one `iexact` rule for the group's **first key in map
    order** — the key the old case-insensitive scan would have returned.
    """
    groups: dict[str, list[str]] = {}
    for key in mapping:
        groups.setdefault(key.lower(), []).append(key)

    exact_priority = priority - _EXACT_TIER_OFFSET
    out: list[tuple[str, str, str, int]] = []
    for keys in groups.values():
        if len(keys) > 1:
            for key in keys:
                out.append(("exact", key, mapping[key], exact_priority))
    for keys in groups.values():
        out.append(("iexact", keys[0], mapping[keys[0]], priority))
    return out


def _check_map_key(kind: str, key: Any) -> None:
    """Refuse a map key no rule can carry.

    Both shapes are storable in the old tables today, because
    `_check_map_account` validates the value and never the key. An empty key
    cannot become a rule at all: `match_value` is required non-empty, and an
    empty needle would match every transaction.

    The length bound is `MAX_MATCH_VALUE_CHARS` and **not** the subject cap,
    which is the wider of the two and the tempting one — a key past the subject
    cap makes a rule that can never fire, so it looks like the limit that
    matters. It is not the binding one. `validate_rule_fields` caps
    `match_value` at 200, and `update_transaction_rule` revalidates the whole
    record, so a key between the two caps stores a rule that works and that the
    rules API then refuses every edit to, including switching it off. A write
    is refused rather than silently dropped; the migration, which cannot refuse
    anything, records both bands instead.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{kind} key must be a non-empty string")
    if len(key) > rule_engine.MAX_MATCH_VALUE_CHARS:
        raise ValueError(
            f"{kind} key is longer than "
            f"{rule_engine.MAX_MATCH_VALUE_CHARS} characters",
        )


def _view_rows(
    conn: sqlite3.Connection,
    ledger: str,
    field: str,
    action: str,
    *,
    enabled_only: bool,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT * FROM transaction_rules WHERE ledger = ? AND field = ? "
        "AND action = ? AND source IN (?, ?) AND match_kind IN (?, ?) "
        "AND origin != 'seed'"
    )
    params: list[Any] = [ledger, field, action, *_VIEW_SOURCES, *_VIEW_KINDS]
    if enabled_only:
        sql += " AND enabled = 1"
    # `match_value` first so the dict comes back in the order the old
    # `ORDER BY monarch_category` produced. That order is what the legacy
    # case-insensitive scan walks, so it is part of the answer rather than
    # presentation. `priority, id` breaks a tie between the `exact` and
    # `iexact` rules a collision group emits for the same key.
    sql += " ORDER BY match_value, priority, id"
    return conn.execute(sql, params).fetchall()


def _map_view(
    conn: sqlite3.Connection, ledger: str, field: str, action: str,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _view_rows(conn, ledger, field, action, enabled_only=True):
        out.setdefault(row["match_value"], row["target"])
    return out


def _sync_rules(
    conn: sqlite3.Connection,
    *,
    ledger: str,
    source: str,
    field: str,
    action: str,
    desired: dict[tuple[str, str], tuple[str, int]],
    origin: str,
) -> None:
    """Make the rules in one scope say exactly what `desired` says.

    `desired` is keyed `(match_kind, match_value)` and carries
    `(target, priority)`. Deletes, updates and inserts only what differs, so a
    rule the write did not touch keeps its id, its priority and its note — the
    priority of an existing row is never rewritten, since a user who set one
    meant it. A rule the dict view cannot represent — a `contains` kind, a
    seeded row, a rule on a source outside `_VIEW_SOURCES` — is outside the
    query and survives untouched, which is the contract a full `PUT` of the
    dict view is tested against.

    A **disabled** row is matched but never deleted. It is invisible to the
    view for the same reason a `contains` rule is, so a write that does not
    mention its key must leave it alone; a write that does mention the key
    switches it back on rather than inserting a second row the unique index
    would refuse.
    """
    existing = {
        (row["source"], row["match_kind"], row["match_value"]): row
        for row in _view_rows(conn, ledger, field, action, enabled_only=False)
    }
    now = _iso_now()

    for key, row in existing.items():
        if key not in desired and row["enabled"]:
            conn.execute("DELETE FROM transaction_rules WHERE id = ?", (row["id"],))

    for (row_source, kind, value), (target, priority) in desired.items():
        row = existing.get((row_source, kind, value))
        if row is None:
            # `ON CONFLICT` rather than a bare INSERT, because `_view_rows` is
            # narrower than the unique index: a row the view filters out —
            # `origin='seed'` is the reachable one — is invisible above and
            # still occupies this index entry, so a bare INSERT would raise a
            # raw IntegrityError for that key on every map write from then on.
            # The scope's map is authoritative for the tuple, so the row is
            # taken over rather than duplicated or refused.
            conn.execute(
                "INSERT INTO transaction_rules("
                "ledger, source, field, match_kind, match_value, action, "
                "target, priority, enabled, origin, note, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, '', ?, ?) "
                "ON CONFLICT(ledger, source, field, match_kind, match_value, "
                "action) DO UPDATE SET target = excluded.target, "
                "priority = excluded.priority, enabled = 1, "
                "origin = excluded.origin, updated_at = excluded.updated_at",
                (ledger, row_source, field, kind, value, action,
                 target, priority, origin, now, now),
            )
        elif row["target"] != target or not row["enabled"]:
            conn.execute(
                "UPDATE transaction_rules SET target = ?, enabled = 1, "
                "updated_at = ? WHERE id = ?",
                (target, now, row["id"]),
            )


def _sync_map_rules(
    conn: sqlite3.Connection,
    *,
    ledger: str,
    view: str,
    mapping: dict[str, str],
    origin: str,
) -> None:
    """Write one map into its scope, in the order the view will read it back.

    The sort is not cosmetic. `_emit_map_entries` picks a case-colliding
    group's representative by the mapping's own iteration order, and the order
    that decides the answer is the one `_map_view` produces — `ORDER BY
    match_value` — not the order a caller happened to build a dict in. The
    migration's maps arrive sorted already, because `_legacy_category_map`
    reads `ORDER BY monarch_category`; a caller-supplied dict does not, and
    without this a `{"food": …, "Food": …}` PUT stored `food` as the
    representative while the view read `Food` first.
    """
    field, action = _MAP_VIEWS[view]
    ordered = dict(sorted((mapping or {}).items()))
    _sync_rules(
        conn, ledger=ledger, source=_MAP_SOURCE, field=field, action=action,
        desired={
            (_MAP_SOURCE, kind, value): (target, priority)
            for kind, value, target, priority in _emit_map_entries(
                ordered, _map_tier_priority(ledger),
            )
        },
        origin=origin,
    )


def _tag_skip_view(conn: sqlite3.Connection, ledger: str) -> list[str]:
    seen: list[str] = []
    for row in _view_rows(conn, ledger, "tag", "skip", enabled_only=True):
        if row["match_value"] not in seen:
            seen.append(row["match_value"])
    return seen


def _sync_tag_skip_rules(
    conn: sqlite3.Connection, ledger: str, tags: list[str], *, clear: bool,
) -> None:
    """One `iexact` skip rule per excluded tag.

    No collision tier, unlike a map: a skip takes no target, so two tags
    differing only in case say the same thing and one rule covers both.
    """
    wanted = list(tags)
    if not clear:
        current = _tag_skip_view(conn, ledger)
        wanted = current + [tag for tag in tags if tag not in current]
    _sync_rules(
        conn, ledger=ledger, source=_TAG_SOURCE, field="tag", action="skip",
        desired={
            (_TAG_SOURCE, "iexact", tag): ("", _TAG_SKIP_PRIORITY)
            for tag in wanted
        },
        origin="user",
    )


def _clear_all_map_views(conn: sqlite3.Connection) -> None:
    """Drop every rule a dict view can represent, in every scope.

    What `save_monarch(replace_collections=True)` used to get from deleting the
    global map rows and cascading the profile rows away. Only the representable
    subset goes: a `contains` rule, a seeded row and a rule on another source
    are not part of any map a `MonarchConfig` can express, so a wholesale
    rewrite of that config must not take them.
    """
    conn.execute(
        "DELETE FROM transaction_rules WHERE origin != 'seed' "
        "AND source IN (?, ?) AND match_kind IN (?, ?) AND ("
        "  (field = 'account' AND action = 'contra_account')"
        "  OR (field = 'category' AND action = 'posting_account')"
        "  OR (field = 'tag' AND action = 'skip'))",
        (*_VIEW_SOURCES, *_VIEW_KINDS),
    )


# --- Migration ----------------------------------------------------------------


def _legacy_account_map(
    conn: sqlite3.Connection, profile_id: int,
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT monarch_name, beancount_account FROM monarch_account_map "
        "WHERE profile_id = ? ORDER BY monarch_name",
        (profile_id,),
    ).fetchall()
    return {r["monarch_name"]: r["beancount_account"] for r in rows}


def _legacy_category_map(
    conn: sqlite3.Connection, profile_id: int,
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT monarch_category, beancount_account FROM monarch_category_map "
        "WHERE profile_id = ? ORDER BY monarch_category",
        (profile_id,),
    ).fetchall()
    return {r["monarch_category"]: r["beancount_account"] for r in rows}


def _legacy_tag_filters(
    conn: sqlite3.Connection, profile_id: int,
) -> MonarchTagFilters:
    rows = conn.execute(
        "SELECT kind, tag FROM monarch_tag_filters WHERE profile_id = ? "
        "ORDER BY kind, tag",
        (profile_id,),
    ).fetchall()
    include = [r["tag"] for r in rows if r["kind"] == "include"]
    exclude = [r["tag"] for r in rows if r["kind"] == "exclude"]
    return MonarchTagFilters(include=include, exclude=exclude)


def _migration_map(
    mapping: dict[str, str], notes: list[dict], *, scope: str, view: str,
) -> dict[str, str]:
    """Drop the keys the migration cannot carry, recording each one.

    `_check_map_key` refuses these at a write boundary. The migration has no
    such option — the rows already exist — so it reports instead. The empty-key
    case is a genuine behaviour change on a deployment that has one:
    `map_monarch_category_with_config('', {'': 'Expenses:X'})` answers
    `Expenses:X` today and falls through to the uncategorized account after.

    Three bands, and only the first two are dropped. A key past
    `MAX_SUBJECT_CHARS` makes a rule that can never fire, since every subject
    is truncated to that length before matching, so carrying it would be a
    silently dead row. A key between `MAX_MATCH_VALUE_CHARS` and that cap
    *works* — the rule fires and the map keeps answering — but is past what
    `validate_rule_fields` admits, so the rules API refuses every edit to it.
    That one is carried and reported rather than dropped, because dropping it
    would change what the deployment posts to fix an editing problem.
    """
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key.strip():
            notes.append({
                "reason": "empty-key", "scope": scope, "view": view,
                "target": value,
            })
            continue
        if len(key) > rule_engine.MAX_SUBJECT_CHARS:
            notes.append({
                "reason": "over-long-key", "scope": scope, "view": view,
                "key_prefix": key[:40], "key_length": len(key),
                "limit": rule_engine.MAX_SUBJECT_CHARS,
            })
            continue
        if len(key) > rule_engine.MAX_MATCH_VALUE_CHARS:
            notes.append({
                "reason": "uneditable-key", "scope": scope, "view": view,
                "key_prefix": key[:40], "key_length": len(key),
                "limit": rule_engine.MAX_MATCH_VALUE_CHARS,
            })
        out[key] = value
    return out


def _migrate_insert_rule(
    conn: sqlite3.Connection,
    notes: list[dict],
    *,
    ledger: str,
    source: str,
    field: str,
    match_kind: str,
    match_value: str,
    action: str,
    target: str,
    priority: int,
    now: str,
) -> None:
    """Insert one migrated rule, recording a genuine conflict rather than raising.

    Two profiles on one ledger land in the same scope, and a category they map
    to two different accounts is a contradiction the old per-profile tables
    permitted. The first one written survives, the second is recorded, and the
    migration carries on: a config that already contained a contradiction must
    not stop a deployment booting. The pre-check also makes a re-run a no-op,
    which is what makes a partially-applied migration safe to retry.
    """
    existing = conn.execute(
        "SELECT id, target FROM transaction_rules WHERE ledger = ? AND source = ? "
        "AND field = ? AND match_kind = ? AND match_value = ? AND action = ?",
        (ledger, source, field, match_kind, match_value, action),
    ).fetchone()
    if existing is not None:
        if existing["target"] != target:
            notes.append({
                "reason": "duplicate",
                "ledger": ledger, "field": field, "match_kind": match_kind,
                "match_value": match_value, "action": action,
                "kept_rule_id": existing["id"], "kept_target": existing["target"],
                "dropped_target": target,
            })
        return
    conn.execute(
        "INSERT INTO transaction_rules("
        "ledger, source, field, match_kind, match_value, action, target, "
        "priority, enabled, origin, note, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'migrated', '', ?, ?)",
        (ledger, source, field, match_kind, match_value, action, target,
         priority, now, now),
    )


def _migrate_one_map(
    conn: sqlite3.Connection,
    notes: list[dict],
    *,
    ledger: str,
    view: str,
    mapping: dict[str, str],
    scope: str,
    now: str,
) -> None:
    field, action = _MAP_VIEWS[view]
    carried = _migration_map(mapping, notes, scope=scope, view=view)
    for match_kind, match_value, target, priority in _emit_map_entries(
        carried, _map_tier_priority(ledger),
    ):
        _migrate_insert_rule(
            conn, notes, ledger=ledger, source=_MAP_SOURCE, field=field,
            match_kind=match_kind, match_value=match_value, action=action,
            target=target, priority=priority, now=now,
        )


def _run_guarded(
    conn: sqlite3.Connection, savepoint: str, work, failure_message: str,
) -> int:
    """Run one boot-path step in its own savepoint, swallowing any failure.

    Both steps below run inside `init_db`, which every money accessor calls, so
    neither may raise: a failure there reaches `load_monarch`, every money web
    request and every skill invocation. The savepoint is what makes swallowing
    safe — `init_db`'s connection commits whatever reached it, so a half-
    applied step with no sentinel would double its rows on the next boot.

    The recovery statements are guarded too, and that is not belt and braces:
    a SQLite error that aborts the whole transaction makes `ROLLBACK TO
    SAVEPOINT` itself raise, from inside the `except` clause, which is exactly
    the path that must not throw. A `RELEASE` that fails after that is the same
    story. All three are logged and none of them escapes.
    """
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
    except Exception:
        logger.exception(failure_message)
        return 0
    result = 0
    try:
        result = work()
    except Exception:
        logger.exception(failure_message)
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        except Exception:
            logger.exception("%s (and the rollback failed)", failure_message)
    try:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        logger.exception("%s (and the savepoint release failed)", failure_message)
    return result


def _migrate_transaction_rules(conn: sqlite3.Connection) -> int:
    """Carry the three Monarch map tables into `transaction_rules`. Runs once.

    Sentinel-gated, idempotent, and wrapped in a savepoint of its own so a
    failure leaves nothing behind: `init_db`'s connection commits whatever
    reached it, and a half-migrated table with no sentinel would double its
    rows on the next boot. Never raises — the caller is the boot path, and the
    compatibility views keep reading the old tables while the sentinel is
    absent.

    The include half of `monarch_tag_filters` is deliberately not carried. An
    include list is a gate over the whole rule set — *if any include tags are
    configured, the row must carry one* — so a rule expressing it would mean
    something different depending on whether its siblings existed.
    """
    if _rules_migrated(conn):
        return 0
    notes: list[dict] = []
    now = _iso_now()

    def work() -> int:
        global_accounts = _legacy_account_map(conn, GLOBAL_PROFILE_ID)
        global_categories = _legacy_category_map(conn, GLOBAL_PROFILE_ID)
        _migrate_one_map(
            conn, notes, ledger="", view="account", mapping=global_accounts,
            scope="__global__", now=now,
        )
        _migrate_one_map(
            conn, notes, ledger="", view="category", mapping=global_categories,
            scope="__global__", now=now,
        )

        profiles = conn.execute(
            "SELECT id, name, ledger FROM monarch_profiles WHERE id != ? "
            "ORDER BY name",
            (GLOBAL_PROFILE_ID,),
        ).fetchall()
        for row in profiles:
            pid, name, ledger = row["id"], row["name"], row["ledger"] or ""
            # The effective map, exactly as `load_monarch` computes it: a
            # profile's own rows if it has any, otherwise a copy of the global
            # ones. Old inheritance is replacement, so a profile with one own
            # account rule ignored the whole global account map.
            accounts = _legacy_account_map(conn, pid) or dict(global_accounts)
            categories = _legacy_category_map(conn, pid) or dict(global_categories)
            _migrate_one_map(
                conn, notes, ledger=ledger, view="account", mapping=accounts,
                scope=name, now=now,
            )
            _migrate_one_map(
                conn, notes, ledger=ledger, view="category", mapping=categories,
                scope=name, now=now,
            )

        for pid, ledger in [(GLOBAL_PROFILE_ID, "")] + [
            (r["id"], r["ledger"] or "") for r in profiles
        ]:
            for tag in _legacy_tag_filters(conn, pid).exclude:
                if not isinstance(tag, str) or not tag.strip():
                    continue
                _migrate_insert_rule(
                    conn, notes, ledger=ledger, source=_TAG_SOURCE, field="tag",
                    match_kind="iexact", match_value=tag, action="skip",
                    target="", priority=_TAG_SKIP_PRIORITY, now=now,
                )

        if notes:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (_RULES_MIGRATION_NOTES, json.dumps(notes)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_RULES_MIGRATION_SENTINEL, now),
        )
        return len(notes)

    return _run_guarded(
        conn, "transaction_rules_migration", work,
        "transaction_rules migration failed; old maps still in use",
    )


def seed_transaction_rules(conn: sqlite3.Connection) -> int:
    """Write the shipped `MONARCH_CATEGORY_MAP` out as rules. Runs once.

    Behind every migrated and user rule, at `ledger=''` and `source=''` — a
    Monarch category name is not a Monarch-specific concept once it is written
    down, and this is the tier `map_monarch_category` already occupied as a
    module constant. Seeded rows are deletable and a deleted one does not come
    back, the same rule `portfolio.seed_classifications` follows.

    They are excluded from every dict view. The constant was never in
    `MonarchConfig.categories`, and `map_monarch_category` still carries it as
    the fallback beneath the config maps, so surfacing it here would double it
    into every export.
    """
    row = conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = ?", (_RULES_SEED_SENTINEL,),
    ).fetchone()
    if row is not None:
        return 0
    from istota.money.core.transactions import MONARCH_CATEGORY_MAP

    now = _iso_now()

    def work() -> int:
        count = 0
        for category, account in MONARCH_CATEGORY_MAP.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO transaction_rules("
                "ledger, source, field, match_kind, match_value, action, target, "
                "priority, enabled, origin, note, created_at, updated_at"
                ") VALUES ('', '', 'category', 'iexact', ?, 'posting_account', ?, "
                "?, 1, 'seed', '', ?, ?)",
                (category, account, _SEED_PRIORITY, now, now),
            )
            count += cur.rowcount
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_RULES_SEED_SENTINEL, now),
        )
        return count

    # Guarded for the reason the migration is: `init_db` used to end in two
    # `INSERT OR IGNORE`s and now ends in fifty writes, and it is on the path
    # of every money accessor.
    return _run_guarded(
        conn, "transaction_rules_seed", work,
        "transaction_rules seed failed; the shipped map stays a constant",
    )


# --- CRUD ---------------------------------------------------------------------


def _rule_row_to_dict(row: sqlite3.Row) -> dict:
    out = {key: row[key] for key in _RULE_COLUMNS}
    out["enabled"] = bool(out["enabled"])
    return out


def _find_duplicate_rule(conn: sqlite3.Connection, fields: dict) -> int | None:
    row = conn.execute(
        "SELECT id FROM transaction_rules WHERE ledger = ? AND source = ? "
        "AND field = ? AND match_kind = ? AND match_value = ? AND action = ?",
        (fields["ledger"], fields["source"], fields["field"],
         fields["match_kind"], fields["match_value"], fields["action"]),
    ).fetchone()
    return row["id"] if row is not None else None


def _duplicate_rule_error(rule_id: int) -> ValueError:
    # The id and never the value: this message reaches an HTTP response and a
    # Talk-delivered error, and `match_value` is the user's financial data.
    return ValueError(
        f"a rule with this scope, match and action already exists (id {rule_id})",
    )


def list_transaction_rules(
    db_path: Path | str,
    *,
    ledger: str | None = None,
    source: str | None = None,
    include_disabled: bool = True,
) -> list[dict]:
    """Every rule in an exact scope, in evaluation order.

    `ledger` and `source` select one scope, not the engine's wildcard test:
    this is the editor's list, and an editor showing a ledger's rules must not
    silently fold in every `''`-scoped one as though it belonged there.

    The `ledger` comparison folds case, matching `load_rules_for_run` and every
    other ledger comparison in the module. Nothing normalizes `ledger` on the
    way in, so a rule stored as `Personal` is in force for a run on `personal`;
    matching exactly here would hide it from the editor filtered to that
    ledger, and a preview could then name a rule id the list beside it does not
    carry. `source` stays exact — it is an `ImportSource.name`, a code-owned
    identifier rather than a user-typed one, and `load_rules_for_run` compares
    it exactly too.

    **The fold closes the case divergence and deliberately not the wildcard
    one.** A rule at `ledger=''` is in force for every run and is still absent
    from a list filtered to one ledger, because the editor's job is to say
    which rules were *written* in a scope and folding the wildcard tier in
    would present ~50 seeded rows as though the user had put them there. So a
    filtered list is not the whole set an import is scored against, and any
    surface rendering one beside a trace has to say so. Two things follow that
    a reader will otherwise assume: the fold does not make
    `idx_transaction_rules_dedup` case-insensitive, so `Personal` and
    `personal` remain two storable rows that are one scope at run time; and
    `lower()` is not sargable against `idx_transaction_rules_order`, so this
    read is a scan — irrelevant at the hundreds of rows the table holds, and
    stated so it is not a surprise later.
    """
    init_db(db_path)
    sql = "SELECT * FROM transaction_rules"
    where: list[str] = []
    params: list[Any] = []
    if ledger is not None:
        where.append("lower(ledger) = lower(?)")
        params.append(ledger)
    if source is not None:
        where.append("source = ?")
        params.append(source)
    if not include_disabled:
        where.append("enabled = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority, id"
    with _connect(db_path) as conn:
        return [_rule_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def get_transaction_rule(db_path: Path | str, rule_id: int) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM transaction_rules WHERE id = ?", (rule_id,),
        ).fetchone()
        return _rule_row_to_dict(row) if row is not None else None


SEED_ORIGIN = "seed"


def _reject_seed_origin(fields: dict) -> None:
    """`origin='seed'` is the store's to write, never a caller's.

    `ORIGINS` admits it because the seeder's rows carry it, but the seeder
    writes them here rather than through the CRUD. A caller-set one is a wedge
    rather than a mislabel: every dict view excludes `origin='seed'` while the
    unique index does not, so a seed-labelled row inside a map's scope is
    invisible to `_sync_rules`, which then takes the INSERT branch and raises a
    raw `IntegrityError` for that key on every map write afterwards.
    `_clear_all_map_views` excludes it too, so not even a wholesale
    `save_monarch` clears the wedge.
    """
    if fields.get("origin") == SEED_ORIGIN:
        raise ValueError(
            f"origin '{SEED_ORIGIN}' is reserved for the shipped rule set",
        )


def create_transaction_rule(db_path: Path | str, **fields: Any) -> dict:
    """Validate and store one rule. Raises on a bad field or a duplicate."""
    _reject_seed_origin(fields)
    clean = rule_engine.validate_rule_fields(fields)
    init_db(db_path)
    now = _iso_now()
    with _connect(db_path) as conn:
        duplicate = _find_duplicate_rule(conn, clean)
        if duplicate is not None:
            raise _duplicate_rule_error(duplicate)
        cur = conn.execute(
            "INSERT INTO transaction_rules("
            "ledger, source, field, match_kind, match_value, action, target, "
            "priority, enabled, origin, note, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (clean["ledger"], clean["source"], clean["field"],
             clean["match_kind"], clean["match_value"], clean["action"],
             clean["target"], clean["priority"], int(clean["enabled"]),
             clean["origin"], clean["note"], now, now),
        )
        row = conn.execute(
            "SELECT * FROM transaction_rules WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
        return _rule_row_to_dict(row)


def update_transaction_rule(
    db_path: Path | str, rule_id: int, **fields: Any,
) -> dict | None:
    """Merge a partial change onto a stored rule, validate the whole, store it.

    The whole record, not the change: a `skip` action and a target arriving in
    separate requests are still checked against each other. `None` back means
    no such rule, which the route turns into a 404.
    """
    unknown = sorted(set(fields) - set(rule_engine.RULE_FIELDS))
    if unknown:
        raise ValueError("unknown rule field(s): " + ", ".join(unknown))
    _reject_seed_origin(fields)
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM transaction_rules WHERE id = ?", (rule_id,),
        ).fetchone()
        if row is None:
            return None
        merged = {key: row[key] for key in rule_engine.RULE_FIELDS}
        merged["enabled"] = bool(merged["enabled"])
        merged.update(fields)
        clean = rule_engine.validate_rule_fields(merged)
        duplicate = _find_duplicate_rule(conn, clean)
        if duplicate is not None and duplicate != rule_id:
            raise _duplicate_rule_error(duplicate)
        conn.execute(
            "UPDATE transaction_rules SET ledger = ?, source = ?, field = ?, "
            "match_kind = ?, match_value = ?, action = ?, target = ?, "
            "priority = ?, enabled = ?, origin = ?, note = ?, updated_at = ? "
            "WHERE id = ?",
            (clean["ledger"], clean["source"], clean["field"],
             clean["match_kind"], clean["match_value"], clean["action"],
             clean["target"], clean["priority"], int(clean["enabled"]),
             clean["origin"], clean["note"], _iso_now(), rule_id),
        )
        updated = conn.execute(
            "SELECT * FROM transaction_rules WHERE id = ?", (rule_id,),
        ).fetchone()
        return _rule_row_to_dict(updated)


def delete_transaction_rule(db_path: Path | str, rule_id: int) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM transaction_rules WHERE id = ?", (rule_id,),
        )
        return cur.rowcount > 0


def load_rules_for_run(
    db_path: Path | str, ledger: str, source: str,
) -> list[rule_engine.Rule] | None:
    """The enabled rules one import run is scored against, in evaluation order.

    `''` on either scope column means "any", so this is the engine's own scope
    test rather than the editor's exact one. Disabled rows never leave here.

    **`None` means the rules table is not authoritative** and every caller must
    fall back to the dict path, which is the same answer `_rule_scope` gives an
    accessor for the same reason. It is not `[]`: an empty list says the table
    was asked and had nothing in scope, and an import path reads that as "no
    mapping applies", which is a wrong answer rather than a missing one.

    The state that produces it is a migration that did not complete. `init_db`
    runs the migration and the seed as two independent guarded savepoints, so a
    failed migration leaves ~50 *seed* rows behind with its own sentinel
    unwritten — a non-empty list that looks authoritative and carries none of
    the user's own map. The views already read the sentinel and go on serving
    the old tables; without this an import run would take the rules path while
    the `MonarchConfig` beside it was still being served from the legacy
    tables, and the two halves of one sync would disagree with nothing
    reporting it. Every transaction would post to the shipped constant's
    account or to `Expenses:Uncategorized`, and every excluded tag would book.

    The ledger comparison is case-insensitive, matching every other ledger
    comparison in the module — `resolve_ledger`, `sync_all_profiles`'
    `ledger_by_name` and `_sync_monarch_ledgers` all fold case. The scope is
    written from `monarch_profiles.ledger` while some callers can only name a
    ledger from the money TOML's own list, and the two spellings are allowed
    to differ; an exact match there silently selects no ledger-scoped rule at
    all, which reads as "the user has written none".
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        if not _rules_migrated(conn):
            return None
        rows = conn.execute(
            "SELECT * FROM transaction_rules WHERE enabled = 1 "
            "AND (ledger = '' OR lower(ledger) = lower(?)) "
            "AND (source = '' OR source = ?) "
            "ORDER BY priority, id",
            (ledger, source),
        ).fetchall()
    return [
        rule_engine.Rule(
            id=r["id"], ledger=r["ledger"], source=r["source"], field=r["field"],
            match_kind=r["match_kind"], match_value=r["match_value"],
            action=r["action"], target=r["target"], priority=r["priority"],
            enabled=bool(r["enabled"]), origin=r["origin"], note=r["note"],
        )
        for r in rows
    ]


def get_transaction_rules_migration_notes(db_path: Path | str) -> list[dict]:
    """What the migration could not carry: dropped duplicates and dead keys."""
    raw = get_meta(db_path, _RULES_MIGRATION_NOTES)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# =============================================================================
# Schema-meta helpers
# =============================================================================


def get_meta(db_path: Path | str, key: str) -> str | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else None


def set_meta(db_path: Path | str, key: str, value: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def has_invoicing_data(db_path: Path | str) -> bool:
    """True if invoicing collection tables have any rows."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for table in ("invoicing_clients", "invoicing_services",
                      "invoicing_companies"):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            if row:
                return True
    return False


def has_tax_data(db_path: Path | str) -> bool:
    """True if tax *collection* tables have any rows.

    Deliberately excludes ``tax_settings`` because ``save_tax`` always
    writes ``filing_status`` / ``tax_year`` even when the rest of the
    config is just dataclass defaults — using it as a populated-check
    would falsely lock out legacy migration after any save round-trip.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        for query in (
            "SELECT 1 FROM tax_account_patterns LIMIT 1",
            "SELECT 1 FROM tax_year_rates LIMIT 1",
        ):
            row = conn.execute(query).fetchone()
            if row:
                return True
    return False


def has_monarch_config_rows(db_path: Path | str) -> bool:
    """True if any monarch profile *or* map row exists, global included.

    What the legacy importer must ask before it replaces the global maps
    wholesale. Distinct from :func:`has_monarch_data`, which answers
    sync-monarch's "is there anything to sync" and so stays profile-only.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        for sql, params in (
            ("SELECT 1 FROM monarch_profiles WHERE id != ? LIMIT 1",
             (GLOBAL_PROFILE_ID,)),
            ("SELECT 1 FROM monarch_account_map LIMIT 1", ()),
            ("SELECT 1 FROM monarch_category_map LIMIT 1", ()),
            # The maps live here now, so the question has to be asked of the
            # subset that *is* a map: `origin != 'seed'` keeps the shipped tier
            # — written into every money.db at first init — from answering yes
            # everywhere, and the field/action pairs keep a payee or notes rule
            # from doing the same. `_migrate._section_already_populated` reads
            # this to decide whether the legacy ACCOUNTING.md import may
            # overwrite stored config, and a rule that is not part of any map
            # is not the config it is asking about.
            ("SELECT 1 FROM transaction_rules WHERE origin != 'seed' AND ("
             "  (field = 'account' AND action = 'contra_account')"
             "  OR (field = 'category' AND action = 'posting_account')"
             "  OR (field = 'tag' AND action = 'skip')) LIMIT 1", ()),
        ):
            if conn.execute(sql, params).fetchone() is not None:
                return True
        return False


def has_monarch_data(db_path: Path | str) -> bool:
    """True if any non-global ``monarch_profiles`` row exists.

    Excludes ``monarch_settings`` for the same reason as :func:`has_tax_data`.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM monarch_profiles WHERE id != ? LIMIT 1",
            (GLOBAL_PROFILE_ID,),
        ).fetchone()
        return row is not None
