"""Operator-facing ``istota money …`` CLI.

Sibling of ``istota resource ensure`` and ``istota briefing ensure``.
Mutations print ``STATE: created|updated|noop`` and exit 0; hard errors
print ``error: …`` to stderr and exit 2. Safe for Ansible.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tomli
from pathlib import Path
from typing import Any

from istota.money import config_store
from istota.money.core import rules as rule_engine


def _load_user_ctx(istota_config, user_id: str):
    """Resolve the user's UserContext via the module-aware loader."""
    from istota.money._loader import UserNotFoundError, resolve_for_user

    try:
        return resolve_for_user(user_id, istota_config)
    except UserNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def _print_state(state: str, message: str) -> None:
    print(f"STATE: {state} {message}")


def _print_error(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _blocked_by_references(scan_fn, ctx, key: str) -> int | None:
    """Refuse a delete the shared reference guard blocks, or return None.

    The same guards the web routes enforce. They live in ``config_refs``
    rather than only in the route so this claim holds on every surface — an
    agent reaching for ``istota money service remove`` must not be able to
    unbill a client's work in a way the browser refuses to.
    """

    scan = scan_fn(ctx.db_path, ctx.data_dir, key)
    if scan.scan_failed is not None:
        return _print_error(
            f"could not check what references this record: {scan.scan_failed}",
        )
    if scan.blocked_reason is not None:
        return _print_error(scan.blocked_reason)
    return None


# =============================================================================
# Top-level dispatch
# =============================================================================


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Register the ``money`` subcommand group on the istota CLI."""
    p = subparsers.add_parser(
        "money", help="Money module: config management + accounting operations",
    )
    sub = p.add_subparsers(dest="money_action", required=True)

    _add_config(sub)
    _add_client(sub)
    _add_company(sub)
    _add_service(sub)
    _add_tax(sub)
    _add_monarch(sub)
    _add_rules(sub)
    _add_operational(sub)


def dispatch(args, istota_config) -> int:
    """Dispatch ``istota money …``. Returns the exit code."""
    action = args.money_action
    handler = _DISPATCH.get(action)
    if handler is None:
        return _print_error(f"unknown money action: {action}")
    try:
        return handler(args, istota_config)
    except ValueError as exc:
        return _print_error(str(exc))


# =============================================================================
# Operational commands — `istota money list|invoice|work|sync-monarch|…`
#
# The accounting operations live in the money Click command-tree
# (``istota.money.cli``). Rather than reimplement them, we resolve the user the
# istota way (``resolve_for_user`` → DB) and forward to that tree in-process
# with the resolved :class:`Context` injected, exactly as the money skill does.
# Click is an internal engine; the user-facing entry follows istota patterns
# (``-u USER``, DB-backed, no env-var config, no standalone binary).
# =============================================================================

# Maps each forwarded subcommand name to its help text. Names mirror the
# top-level command/group names in ``istota.money.cli``. (The config-management
# names — config/client/company/service/tax/monarch — are handled natively
# above and deliberately excluded here.)
_OPERATIONAL_COMMANDS = {
    "list": "List transactions in a ledger",
    "check": "Validate a ledger with bean-check",
    "balances": "Show account balances",
    "query": "Run a BQL query against a ledger",
    "report": "Generate a financial report",
    "lots": "Show tax lots (experimental: money_tax)",
    "wash-sales": "Detect wash sales (experimental: money_wash_sales)",
    "backfill-ids": "Backfill stable transaction ids",
    "add-transaction": "Append a transaction to a ledger",
    "edit-transaction": "Edit a transaction in place by id",
    "import-csv": "Import transactions from a CSV file",
    "sync-monarch": "Sync transactions from Monarch Money",
    "debug-monarch": "Health-check Monarch credentials",
    "run-scheduled": "Run the periodic sync + invoice scheduler",
    "users": "List users visible to the money CLI",
    "invoice": "Invoice management (generate/list/paid/create/void)",
    "work": "Work-entry tracking (list/add/update/remove)",
    "portfolio": "Portfolio positions (import/snapshots/summary/history/diff/accounts/classify)",
}


def _add_operational(sub) -> None:
    """Register one passthrough subparser per operational command.

    ``add_help=False`` + ``REMAINDER`` hand the command's own arguments
    (including ``--help``) verbatim to the money Click tree.
    """
    for name, help_text in _OPERATIONAL_COMMANDS.items():
        op = sub.add_parser(name, help=help_text, add_help=False)
        op.add_argument(
            "rest", nargs=argparse.REMAINDER,
            help="Arguments forwarded to the money command (use -u/--user)",
        )


def _pop_user(rest: list[str]) -> tuple[str | None, list[str]]:
    """Pull a ``-u`` / ``--user`` value out of a verbatim arg list.

    Money's Click subcommands never define their own ``-u``, so any ``-u`` /
    ``--user`` in the forwarded args is the group-level user selector. Returns
    ``(user_id, remaining_args)``.
    """
    out: list[str] = []
    user: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-u", "--user"):
            if i + 1 < len(rest):
                user = rest[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("--user="):
            user = tok[len("--user="):]
            i += 1
            continue
        if tok.startswith("-u") and len(tok) > 2:
            user = tok[2:]
            i += 1
            continue
        out.append(tok)
        i += 1
    return user, out


def _invoke_money_cli(istota_config, user_id: str, click_args: list[str]) -> int:
    """Forward to the money Click tree with a DB-resolved Context injected."""
    from click.testing import CliRunner

    from istota.money import load_user_secrets
    from istota.money.cli import Context, cli

    user_ctx = _load_user_ctx(istota_config, user_id)  # exits(2) on UserNotFound
    obj = Context()
    obj.users[user_id] = user_ctx
    obj.activate_user(user_id)
    try:
        obj.secrets = load_user_secrets(user_id, istota_config) or None
    except Exception:  # secrets are optional; never block a read-only command
        obj.secrets = None

    runner = CliRunner()
    result = runner.invoke(
        cli, ["-u", user_id, *click_args],
        obj=obj, standalone_mode=False, catch_exceptions=True,
    )
    if result.output:
        sys.stdout.write(result.output)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return _print_error(
            f"{type(result.exception).__name__}: {result.exception}"
        )
    return result.exit_code or 0


def _operational_dispatch(args, istota_config) -> int:
    return dispatch_operational(
        args.money_action, list(getattr(args, "rest", None) or []), istota_config,
    )


def is_operational(name: str) -> bool:
    """True if ``name`` is a money operational command (forwarded to Click)."""
    return name in _OPERATIONAL_COMMANDS


def dispatch_operational(name: str, raw_args: list[str], istota_config) -> int:
    """Forward an operational command + its verbatim args to the money Click tree.

    The single entry both the argparse subparser path and the ``main()`` argv
    peel funnel through (argparse ``REMAINDER`` can't capture a leading option
    such as ``-u``, so the peel handles those before ``parse_args``).
    """
    user_id, click_args = _pop_user(list(raw_args))
    if not user_id:
        return _print_error(f"money {name}: --user/-u is required")
    return _invoke_money_cli(istota_config, user_id, [name, *click_args])


# =============================================================================
# `istota money config …`
# =============================================================================


def _add_config(sub) -> None:
    cfg = sub.add_parser("config", help="Show / import / export / diff config")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)

    show = cfg_sub.add_parser("show", help="Show current DB config")
    show.add_argument("--user", "-u", required=True)
    show.add_argument(
        "--section", choices=("invoicing", "tax", "monarch"),
        help="Show only this section",
    )
    show.add_argument(
        "--format", choices=("toml", "json"), default="toml",
    )

    imp = cfg_sub.add_parser("import", help="Import a TOML file into the DB")
    imp.add_argument("--user", "-u", required=True)
    imp.add_argument("--file", required=True)
    imp.add_argument(
        "--section", choices=("invoicing", "tax", "monarch"),
        help="Limit to one section (otherwise auto-detected)",
    )
    imp.add_argument("--dry-run", action="store_true",
                     help="Show what would change; write nothing")
    imp.add_argument("--replace", action="store_true",
                     help="Truncate in-scope collections before upserting")
    imp.add_argument("--strict", action="store_true",
                     help="Fail on unknown TOML keys")

    exp = cfg_sub.add_parser("export", help="Export DB config to TOML")
    exp.add_argument("--user", "-u", required=True)
    exp.add_argument(
        "--section", choices=("invoicing", "tax", "monarch"),
        help="Export only this section (default: combined file)",
    )
    exp.add_argument("--file", help="Write to this path instead of stdout")

    diff = cfg_sub.add_parser("diff", help="Show diff of a TOML file vs DB")
    diff.add_argument("--user", "-u", required=True)
    diff.add_argument("--file", required=True)
    diff.add_argument(
        "--section", choices=("invoicing", "tax", "monarch"),
    )


def _config_dispatch(args, istota_config) -> int:
    sub = args.config_action
    if sub == "show":
        return _config_show(args, istota_config)
    if sub == "import":
        return _config_import(args, istota_config)
    if sub == "export":
        return _config_export(args, istota_config)
    if sub == "diff":
        return _config_diff(args, istota_config)
    return _print_error(f"unknown config action: {sub}")


def _config_show(args, istota_config) -> int:
    ctx = _load_user_ctx(istota_config, args.user)
    sections = [args.section] if args.section else ["invoicing", "tax", "monarch"]
    out: dict[str, Any] = {}
    for sec in sections:
        out[sec] = _section_to_dict(ctx, sec)

    if args.format == "json":
        print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
        return 0
    import tomli_w
    if args.section:
        # Single-section TOML — strip the section wrapper for tax / monarch.
        body = out[sections[0]]
        if sections[0] in ("tax", "monarch"):
            body = body.get(sections[0], body)
        print(tomli_w.dumps(_clean_for_toml(body)), end="")
    else:
        combined = {}
        if "invoicing" in out and out["invoicing"]:
            combined["invoicing"] = out["invoicing"]
        if "tax" in out and out["tax"]:
            tax = out["tax"]
            combined["tax"] = tax.get("tax", tax)
        if "monarch" in out and out["monarch"]:
            mon = out["monarch"]
            combined["monarch"] = mon.get("monarch", mon)
        print(tomli_w.dumps(_clean_for_toml(combined)), end="")
    return 0


def _section_to_dict(ctx, section: str) -> dict:
    if section == "invoicing":
        return config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(ctx.db_path),
        )
    if section == "tax":
        return config_store.tax_to_toml_dict(config_store.load_tax(ctx.db_path))
    if section == "monarch":
        return config_store.monarch_to_toml_dict(
            config_store.load_monarch(ctx.db_path),
        )
    raise ValueError(f"unknown section: {section}")


def _clean_for_toml(value: Any) -> Any:
    """Recursively drop None values so tomli_w doesn't choke."""
    if isinstance(value, dict):
        return {k: _clean_for_toml(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean_for_toml(v) for v in value]
    return value


def _detect_section(parsed: dict) -> str | None:
    """Heuristic: which section does this TOML look like?"""
    if "tax" in parsed and isinstance(parsed["tax"], dict):
        if "monarch" in parsed or any(
            k in parsed for k in ("companies", "clients", "services")
        ):
            return None  # combined
        return "tax"
    if "monarch" in parsed and isinstance(parsed["monarch"], dict):
        return "monarch"
    if any(k in parsed for k in (
        "companies", "clients", "services", "company",
        "accounting_path", "next_invoice_number",
    )):
        return "invoicing"
    return None


def _config_import(args, istota_config) -> int:
    ctx = _load_user_ctx(istota_config, args.user)
    path = Path(args.file)
    if not path.exists():
        return _print_error(f"file not found: {path}")
    parsed = tomli.loads(path.read_text())

    section = args.section
    sections_to_import: list[str] = []
    if section:
        sections_to_import = [section]
    else:
        detected = _detect_section(parsed)
        if detected:
            sections_to_import = [detected]
        else:
            invoicing_top_keys = (
                "companies", "clients", "services", "company",
                "accounting_path", "next_invoice_number",
            )
            has_invoicing = (
                "invoicing" in parsed
                or any(k in parsed for k in invoicing_top_keys)
            )
            if has_invoicing:
                sections_to_import.append("invoicing")
            if "tax" in parsed:
                sections_to_import.append("tax")
            if "monarch" in parsed:
                sections_to_import.append("monarch")
            if not sections_to_import:
                return _print_error("no recognized sections in TOML")

    for sec in sections_to_import:
        section_data = _extract_section_data(parsed, sec)
        if section_data is None:
            print(f"section={sec}  (no data — skipped)")
            continue
        unknown = _unknown_keys(sec, section_data)
        if unknown:
            if args.strict:
                return _print_error(
                    f"unknown keys in section {sec}: {sorted(unknown)}",
                )
            for key in sorted(unknown):
                print(f"warning: unknown key in {sec}: {key}", file=sys.stderr)
        print(f"section={sec}")
        diff_lines = _compute_section_diff(ctx, sec, section_data, args.replace)
        for state, msg in diff_lines:
            _print_state(state, msg)
        if not args.dry_run:
            _apply_section_import(ctx, sec, section_data, args.replace)
    return 0


_INVOICING_TOP_KEYS = {
    "accounting_path", "invoice_output", "next_invoice_number",
    "default_entity", "currency", "default_ar_account", "default_bank_account",
    "notifications", "days_until_overdue",
    "companies", "clients", "services", "company",  # collections / legacy
}
_INVOICING_COMPANY_KEYS = {
    "name", "address", "email", "payment_instructions", "logo",
    "ar_account", "bank_account", "currency",
}
_INVOICING_CLIENT_KEYS = {
    "name", "address", "email", "terms", "ar_account", "entity", "invoicing",
}
_INVOICING_CLIENT_INVOICING_KEYS = {
    "schedule", "day", "ledger_posting", "reminder_days", "notifications",
    "days_until_overdue", "bundles", "separate",
}
_INVOICING_SERVICE_KEYS = {"display_name", "rate", "type", "income_account"}

_TAX_TOP_KEYS = {
    "filing_status", "tax_year", "state",
    "w2", "options", "accounts", "estimated_payments", "safe_harbor", "rates",
}
_TAX_W2_KEYS = {"income", "federal_withholding", "state_withholding"}
_TAX_ESTIMATED_KEYS = {"federal", "state"}
_TAX_OPTIONS_KEYS = {"enable_qbi_deduction"}
_TAX_ACCOUNTS_KEYS = {"se_income", "se_expenses"}
_TAX_SAFE_HARBOR_KEYS = {"prior_year_federal_tax", "prior_year_state_tax"}
_TAX_RATES_KEYS = {
    "ss_wage_base", "ss_rate", "medicare_rate", "se_taxable_fraction",
    "federal_standard_deduction", "state_standard_deduction",
    "federal_brackets", "state_brackets",
}

_MONARCH_TOP_KEYS = {"sync", "accounts", "categories", "tags", "profiles",
                     "email", "password", "session_token"}
_MONARCH_SYNC_KEYS = {"lookback_days", "default_account", "recategorize_account"}
_MONARCH_PROFILE_KEYS = {
    "ledger", "lookback_days", "default_account", "recategorize_account",
    "sync", "accounts", "categories", "tags",
}
_MONARCH_TAG_KEYS = {"include", "exclude"}


def _unknown_keys(section: str, data: dict) -> set[str]:
    """Return any unknown TOML keys in the section. Used by ``--strict``."""
    bad: set[str] = set()
    if section == "invoicing":
        for k in data:
            if k not in _INVOICING_TOP_KEYS:
                bad.add(k)
        for company in (data.get("companies") or {}).values():
            bad |= {f"companies.*.{k}" for k in company
                    if k not in _INVOICING_COMPANY_KEYS}
        for client in (data.get("clients") or {}).values():
            bad |= {f"clients.*.{k}" for k in client
                    if k not in _INVOICING_CLIENT_KEYS}
            for k in (client.get("invoicing") or {}):
                if k not in _INVOICING_CLIENT_INVOICING_KEYS:
                    bad.add(f"clients.*.invoicing.{k}")
        for svc in (data.get("services") or {}).values():
            bad |= {f"services.*.{k}" for k in svc
                    if k not in _INVOICING_SERVICE_KEYS}
    elif section == "tax":
        tax = data.get("tax", data)
        for k in tax:
            if k not in _TAX_TOP_KEYS:
                bad.add(f"tax.{k}")
        for k in tax.get("w2", {}) or {}:
            if k not in _TAX_W2_KEYS:
                bad.add(f"tax.w2.{k}")
        for k in tax.get("estimated_payments", {}) or {}:
            if k not in _TAX_ESTIMATED_KEYS:
                bad.add(f"tax.estimated_payments.{k}")
        for k in tax.get("options", {}) or {}:
            if k not in _TAX_OPTIONS_KEYS:
                bad.add(f"tax.options.{k}")
        for k in tax.get("accounts", {}) or {}:
            if k not in _TAX_ACCOUNTS_KEYS:
                bad.add(f"tax.accounts.{k}")
        for k in tax.get("safe_harbor", {}) or {}:
            if k not in _TAX_SAFE_HARBOR_KEYS:
                bad.add(f"tax.safe_harbor.{k}")
        for k in tax.get("rates", {}) or {}:
            if k not in _TAX_RATES_KEYS:
                bad.add(f"tax.rates.{k}")
    elif section == "monarch":
        monarch = data.get("monarch", data)
        for k in monarch:
            if k not in _MONARCH_TOP_KEYS:
                bad.add(f"monarch.{k}")
        for k in monarch.get("sync", {}) or {}:
            if k not in _MONARCH_SYNC_KEYS:
                bad.add(f"monarch.sync.{k}")
        for k in monarch.get("tags", {}) or {}:
            if k not in _MONARCH_TAG_KEYS:
                bad.add(f"monarch.tags.{k}")
        for prof in (monarch.get("profiles") or {}).values():
            for k in prof:
                if k not in _MONARCH_PROFILE_KEYS:
                    bad.add(f"monarch.profiles.*.{k}")
    return bad


def _extract_section_data(parsed: dict, section: str) -> dict | None:
    """Extract the section payload from a parsed TOML dict.

    Invoicing accepts two forms: bare top-level keys (`accounting_path`,
    `[clients.X]`, ...) or a wrapped `[invoicing]` block. Unknown
    top-level keys are kept in the bare form so the strict-mode check
    in :func:`_unknown_keys` can flag them. Mixing the two forms is an
    explicit error.
    """
    if section == "invoicing":
        bare_keys = (
            "accounting_path", "invoice_output", "next_invoice_number",
            "default_entity", "currency", "default_ar_account",
            "default_bank_account", "notifications", "days_until_overdue",
            "companies", "company", "clients", "services",
        )
        bare = {
            k: v for k, v in parsed.items()
            if k in bare_keys or k not in ("tax", "monarch", "invoicing")
        }
        wrapped_block = parsed.get("invoicing")
        wrapped = (
            dict(wrapped_block) if isinstance(wrapped_block, dict) else None
        )
        # Drop the bare entries that are actually known top-level keys,
        # leaving any unknowns intact for strict-mode detection.
        bare_filtered = {
            k: v for k, v in parsed.items()
            if k in bare_keys
        }
        unknown_top = {
            k: v for k, v in parsed.items()
            if k not in bare_keys and k not in ("tax", "monarch", "invoicing")
        }
        if wrapped is not None and bare_filtered:
            raise ValueError(
                "invoicing section has both a top-level [invoicing] wrapper "
                "and bare top-level invoicing keys (companies/clients/...). "
                "Pick one form.",
            )
        if wrapped is not None:
            invoicing = dict(wrapped)
            # Don't lose top-level unknowns when the wrapper form is used.
            invoicing.update(unknown_top)
        else:
            invoicing = bare
        return invoicing if invoicing else None
    if section == "tax":
        if "tax" in parsed and isinstance(parsed["tax"], dict):
            return {"tax": parsed["tax"]}
        return None
    if section == "monarch":
        if "monarch" in parsed and isinstance(parsed["monarch"], dict):
            return {"monarch": parsed["monarch"]}
        return None
    return None


def _compute_section_diff(
    ctx, section: str, data: dict, replace: bool,
) -> list[tuple[str, str]]:
    """Return [(state, description)] tuples that describe what import will do."""
    out: list[tuple[str, str]] = []
    if section == "invoicing":
        cfg_new = config_store.invoicing_config_from_toml_dict(data)
        cfg_cur = config_store.load_invoicing(ctx.db_path)
        for key, comp in cfg_new.companies.items():
            cur = cfg_cur.companies.get(key)
            if cur is None:
                out.append(("created", f"company key={key}"))
            elif _company_eq(cur, comp):
                out.append(("noop", f"company key={key}"))
            else:
                out.append(("updated", f"company key={key}"))
        for key, client in cfg_new.clients.items():
            cur = cfg_cur.clients.get(key)
            if cur is None:
                out.append(("created", f"client key={key}"))
            elif _client_eq(cur, client):
                out.append(("noop", f"client key={key}"))
            else:
                out.append(("updated", f"client key={key}"))
        for key, svc in cfg_new.services.items():
            cur = cfg_cur.services.get(key)
            if cur is None:
                out.append(("created", f"service key={key} rate={svc.rate}"))
            elif _service_eq(cur, svc):
                out.append(("noop", f"service key={key}"))
            else:
                out.append(("updated", f"service key={key}"))
    elif section == "tax":
        cfg_new = config_store.tax_config_from_toml_dict(data)
        out.append(("updated", f"tax_year={cfg_new.tax_year} filing_status={cfg_new.filing_status}"))
        if any(v is not None for v in (
            cfg_new.federal_brackets, cfg_new.state_brackets,
            cfg_new.federal_standard_deduction, cfg_new.state_standard_deduction,
            cfg_new.ss_wage_base, cfg_new.ss_rate,
        )):
            out.append(("updated", f"year_rates year={cfg_new.tax_year}"))
        for p in cfg_new.se_income_accounts:
            out.append(("created", f"pattern kind=se_income pattern={p}"))
        for p in cfg_new.se_expense_accounts:
            out.append(("created", f"pattern kind=se_expense pattern={p}"))
    elif section == "monarch":
        cfg_new = config_store.monarch_config_from_toml_dict(data)
        cfg_cur = config_store.load_monarch(ctx.db_path)
        cur_profiles = {p.name: p for p in cfg_cur.profiles}
        for p in cfg_new.profiles:
            cur = cur_profiles.get(p.name)
            if cur is None:
                out.append(("created", f"profile name={p.name} ledger={p.ledger}"))
            elif cur.ledger == p.ledger:
                out.append(("noop", f"profile name={p.name}"))
            else:
                out.append(("updated", f"profile name={p.name}"))
            for monarch_name, account in p.accounts.items():
                out.append(("created", f"account_map profile={p.name} {monarch_name}={account}"))
            for category, account in p.categories.items():
                out.append(("created", f"category_map profile={p.name} {category}={account}"))
            for tag in p.tags.include:
                out.append(("created", f"tag_filter profile={p.name} kind=include tag={tag}"))
            for tag in p.tags.exclude:
                out.append(("created", f"tag_filter profile={p.name} kind=exclude tag={tag}"))
    return out


def _company_eq(a, b) -> bool:
    return all(getattr(a, k) == getattr(b, k) for k in (
        "name", "address", "email", "payment_instructions", "logo",
        "ar_account", "bank_account", "currency",
    ))


def _client_eq(a, b) -> bool:
    return all(getattr(a, k) == getattr(b, k) for k in (
        "name", "address", "email", "terms", "ar_account", "entity",
        "schedule", "schedule_day", "reminder_days", "notifications",
        "days_until_overdue", "ledger_posting", "bundles", "separate",
    ))


def _service_eq(a, b) -> bool:
    return all(getattr(a, k) == getattr(b, k) for k in (
        "display_name", "rate", "type", "income_account",
    ))


def _apply_section_import(ctx, section: str, data: dict, replace: bool) -> None:
    """Apply a section's import.

    With ``replace=True`` the in-scope collections are truncated and the
    full new config is written. With ``replace=False`` (merge) we read
    the existing DB config, overlay only the keys actually present in
    the imported TOML (so dataclass defaults from missing keys don't
    clobber existing scalars), and write back.
    """
    if section == "invoicing":
        if replace:
            cfg = config_store.invoicing_config_from_toml_dict(data)
            config_store.save_invoicing(ctx.db_path, cfg, replace_collections=True)
        else:
            cfg = _merge_invoicing(ctx.db_path, data)
            config_store.save_invoicing(ctx.db_path, cfg, replace_collections=False)
    elif section == "tax":
        if replace:
            cfg = config_store.tax_config_from_toml_dict(data)
            config_store.save_tax(ctx.db_path, cfg, replace_collections=True,
                                  write_schedules=True)
        else:
            cfg = _merge_tax(ctx.db_path, data)
            config_store.save_tax(ctx.db_path, cfg, replace_collections=False,
                                  write_schedules=True)
    elif section == "monarch":
        if replace:
            cfg = config_store.monarch_config_from_toml_dict(data)
            config_store.save_monarch(ctx.db_path, cfg, replace_collections=True)
        else:
            cfg = _merge_monarch(ctx.db_path, data)
            config_store.save_monarch(ctx.db_path, cfg, replace_collections=False)


def _merge_invoicing(db_path, data: dict):
    """Merge imported invoicing TOML into existing DB config.

    Scalars not present in the TOML stay at their existing DB values.
    Companies / clients / services from the TOML are upserted onto the
    existing collections (no truncation).
    """
    existing = config_store.load_invoicing(db_path)
    incoming = config_store.invoicing_config_from_toml_dict(data)

    merged = existing
    scalar_keys = (
        "accounting_path", "invoice_output", "next_invoice_number",
        "default_entity", "currency", "default_ar_account",
        "default_bank_account", "notifications", "days_until_overdue",
    )
    for key in scalar_keys:
        if key in data:
            setattr(merged, key, getattr(incoming, key))
    # company-level: TOML companies merge into existing
    for key, comp in incoming.companies.items():
        merged.companies[key] = comp
    for key, client in incoming.clients.items():
        merged.clients[key] = client
    for key, svc in incoming.services.items():
        merged.services[key] = svc
    if merged.companies and merged.default_entity not in merged.companies:
        merged.default_entity = next(iter(merged.companies))
    merged.company = (
        merged.companies.get(merged.default_entity)
        or next(iter(merged.companies.values()), merged.company)
    )
    return merged


def _merge_tax(db_path, data: dict):
    """Merge imported tax TOML into existing DB config."""
    existing = config_store.load_tax(db_path)
    tax_data = data.get("tax", data) or {}
    incoming = config_store.tax_config_from_toml_dict(data)

    # Captured before any overlay: these are the coordinates the schedule
    # overrides now sitting on `existing` were actually read under.
    loaded_year, loaded_status, loaded_state = (
        existing.tax_year, existing.filing_status, existing.state,
    )

    if "filing_status" in tax_data:
        existing.filing_status = incoming.filing_status
    if "tax_year" in tax_data:
        existing.tax_year = incoming.tax_year
    if "state" in tax_data:
        existing.state = incoming.state

    w2 = tax_data.get("w2") or {}
    if "income" in w2:
        existing.w2_income = incoming.w2_income
    if "federal_withholding" in w2:
        existing.w2_federal_withholding = incoming.w2_federal_withholding
    if "state_withholding" in w2:
        existing.w2_state_withholding = incoming.w2_state_withholding

    est = tax_data.get("estimated_payments") or {}
    if "federal" in est:
        existing.federal_estimated_paid = incoming.federal_estimated_paid
    if "state" in est:
        existing.state_estimated_paid = incoming.state_estimated_paid

    options = tax_data.get("options") or {}
    if "enable_qbi_deduction" in options:
        existing.enable_qbi_deduction = incoming.enable_qbi_deduction

    safe = tax_data.get("safe_harbor") or {}
    if "prior_year_federal_tax" in safe:
        existing.prior_year_federal_tax = incoming.prior_year_federal_tax
    if "prior_year_state_tax" in safe:
        existing.prior_year_state_tax = incoming.prior_year_state_tax

    accounts = tax_data.get("accounts") or {}
    if "se_income" in accounts:
        existing.se_income_accounts = list(incoming.se_income_accounts)
    if "se_expenses" in accounts:
        existing.se_expense_accounts = list(incoming.se_expense_accounts)

    rates = tax_data.get("rates") or {}
    rate_field_map = {
        "ss_wage_base": "ss_wage_base",
        "ss_rate": "ss_rate",
        "medicare_rate": "medicare_rate",
        "se_taxable_fraction": "se_taxable_fraction",
        "federal_standard_deduction": "federal_standard_deduction",
        "state_standard_deduction": "state_standard_deduction",
        "federal_brackets": "federal_brackets",
        "state_brackets": "state_brackets",
    }
    # Coordinates the schedule overrides on `existing` were loaded under. If
    # the incoming TOML moves any of them, those values belong to the *old*
    # coordinates and must not be carried across — `config import` is the one
    # path that opts into `write_schedules=True`, so carrying them is exactly
    # the copy-forward this table's filing-status dimension exists to prevent.
    coordinates_moved = (
        existing.tax_year != loaded_year
        or existing.filing_status != loaded_status
        or existing.state != loaded_state
    )
    if coordinates_moved:
        existing.federal_brackets = None
        existing.federal_standard_deduction = None
        existing.state_brackets = None
        existing.state_standard_deduction = None

    for toml_key, attr in rate_field_map.items():
        if toml_key in rates:
            setattr(existing, attr, getattr(incoming, attr))

    return existing


def _merge_monarch(db_path, data: dict):
    """Merge imported monarch TOML into existing DB config."""
    existing = config_store.load_monarch(db_path)
    monarch_data = (data.get("monarch") or {})
    incoming = config_store.monarch_config_from_toml_dict(data)

    sync = monarch_data.get("sync") or {}
    if "lookback_days" in sync:
        existing.sync.lookback_days = incoming.sync.lookback_days
    if "default_account" in sync:
        existing.sync.default_account = incoming.sync.default_account
    if "recategorize_account" in sync:
        existing.sync.recategorize_account = incoming.sync.recategorize_account

    # Profiles: upsert into the existing list by name.
    by_name = {p.name: p for p in existing.profiles}
    for p in incoming.profiles:
        by_name[p.name] = p
    existing.profiles = list(by_name.values())

    if "accounts" in monarch_data:
        existing.accounts.update(incoming.accounts)
    if "categories" in monarch_data:
        existing.categories.update(incoming.categories)
    tags = monarch_data.get("tags") or {}
    if "include" in tags:
        existing.tags.include = list(incoming.tags.include)
    if "exclude" in tags:
        existing.tags.exclude = list(incoming.tags.exclude)

    return existing


def _config_export(args, istota_config) -> int:
    ctx = _load_user_ctx(istota_config, args.user)
    import tomli_w
    if args.section:
        body = _section_to_dict(ctx, args.section)
        if args.section in ("tax", "monarch"):
            # Already wrapped in [tax] / [monarch] from the to_toml_dict.
            text = tomli_w.dumps(_clean_for_toml(body))
        else:
            text = tomli_w.dumps(_clean_for_toml(body))
    else:
        combined: dict[str, Any] = {}
        inv = config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(ctx.db_path),
        )
        if inv:
            combined["invoicing"] = inv
        tax = config_store.tax_to_toml_dict(config_store.load_tax(ctx.db_path))
        if tax.get("tax"):
            combined["tax"] = tax["tax"]
        mon = config_store.monarch_to_toml_dict(
            config_store.load_monarch(ctx.db_path),
        )
        if mon.get("monarch"):
            combined["monarch"] = mon["monarch"]
        text = tomli_w.dumps(_clean_for_toml(combined))
    if args.file:
        Path(args.file).write_text(text)
    else:
        print(text, end="")
    return 0


def _config_diff(args, istota_config) -> int:
    ctx = _load_user_ctx(istota_config, args.user)
    parsed = tomli.loads(Path(args.file).read_text())
    section = args.section or _detect_section(parsed)
    if section is None:
        return _print_error("could not detect section; pass --section")
    section_data = _extract_section_data(parsed, section)
    if section_data is None:
        return _print_error(f"no {section} data in file")
    print(f"section={section}")
    for state, msg in _compute_section_diff(ctx, section, section_data, replace=False):
        _print_state(state, msg)
    return 0


# =============================================================================
# Granular mutators: client / company / service
# =============================================================================


def _add_client(sub) -> None:
    p = sub.add_parser("client", help="Manage invoicing clients")
    s = p.add_subparsers(dest="client_action", required=True)
    for action in ("add", "update"):
        a = s.add_parser(action, help=f"{action} a client")
        a.add_argument("--user", "-u", required=True)
        a.add_argument("--key", required=True)
        a.add_argument("--name")
        a.add_argument("--address")
        a.add_argument("--email")
        a.add_argument("--terms")
        a.add_argument("--ar-account")
        a.add_argument("--entity")
        a.add_argument("--schedule")
        a.add_argument("--day", type=int)
        a.add_argument("--reminder-days", type=int)
        a.add_argument("--notifications")
        a.add_argument("--days-until-overdue", type=int)
        a.add_argument("--ledger-posting", action="store_true", default=None)
        a.add_argument("--no-ledger-posting", dest="ledger_posting",
                       action="store_false", default=None)
        a.add_argument("--separate-json")
        a.add_argument("--bundles-json")
    rm = s.add_parser("remove", help="Delete a client")
    rm.add_argument("--user", "-u", required=True)
    rm.add_argument("--key", required=True)
    ls = s.add_parser("list", help="List clients")
    ls.add_argument("--user", "-u", required=True)
    ls.add_argument("--format", choices=("table", "json", "toml"), default="table")


def _client_dispatch(args, istota_config) -> int:
    a = args.client_action
    ctx = _load_user_ctx(istota_config, args.user)
    if a == "remove":
        ok = config_store.delete_client(ctx.db_path, args.key)
        _print_state("noop" if not ok else "removed", f"client key={args.key}")
        return 0
    if a == "list":
        cfg = config_store.load_invoicing(ctx.db_path)
        return _print_clients(cfg, args.format)

    fields: dict[str, Any] = {}
    for k in ("name", "address", "email", "terms", "ar_account", "entity",
              "schedule", "notifications"):
        v = getattr(args, k.replace("ar_account", "ar_account"), None)
        if v is not None:
            fields[k] = v
    for k in ("day", "reminder_days", "days_until_overdue"):
        v = getattr(args, k, None)
        if v is not None:
            fields[
                "schedule_day" if k == "day" else k
            ] = v
    if args.ledger_posting is not None:
        fields["ledger_posting"] = args.ledger_posting
    if args.separate_json:
        fields["separate"] = json.loads(args.separate_json)
    if args.bundles_json:
        fields["bundles"] = json.loads(args.bundles_json)

    client, state = config_store.upsert_client(ctx.db_path, args.key, **fields)
    _print_state(state, f"client key={args.key}")
    return 0


def _print_clients(cfg, fmt: str) -> int:
    if fmt == "json":
        print(json.dumps(
            [{"key": c.key, "name": c.name, "entity": c.entity, "terms": c.terms,
              "schedule": c.schedule}
             for c in cfg.clients.values()],
            indent=2, ensure_ascii=False,
        ))
    elif fmt == "toml":
        import tomli_w
        out = {k: _client_to_toml_dict(c) for k, c in sorted(cfg.clients.items())}
        print(tomli_w.dumps({"clients": out}), end="")
    else:
        print(f"{'KEY':20} {'NAME':30} {'ENTITY':15} {'SCHEDULE':12} {'TERMS':10}")
        for c in cfg.clients.values():
            print(f"{c.key:20} {c.name:30} {c.entity or '':15} {c.schedule:12} {str(c.terms):10}")
    return 0


def _client_to_toml_dict(c) -> dict:
    out: dict[str, Any] = {"name": c.name}
    for k in ("address", "email", "ar_account", "entity"):
        v = getattr(c, k)
        if v:
            out[k] = v
    if c.terms not in ("", 30):
        out["terms"] = c.terms
    inv: dict[str, Any] = {}
    if c.schedule and c.schedule != "on-demand":
        inv["schedule"] = c.schedule
    if c.schedule_day != 1:
        inv["day"] = c.schedule_day
    if c.bundles:
        inv["bundles"] = c.bundles
    if c.separate:
        inv["separate"] = c.separate
    if inv:
        out["invoicing"] = inv
    return out


def _add_company(sub) -> None:
    p = sub.add_parser("company", help="Manage invoicing companies")
    s = p.add_subparsers(dest="company_action", required=True)
    for action in ("add", "update"):
        a = s.add_parser(action, help=f"{action} a company")
        a.add_argument("--user", "-u", required=True)
        a.add_argument("--key", required=True)
        a.add_argument("--name")
        a.add_argument("--address")
        a.add_argument("--email")
        a.add_argument("--payment-instructions")
        a.add_argument("--logo")
        a.add_argument("--ar-account")
        a.add_argument("--bank-account")
        a.add_argument("--currency")
    rm = s.add_parser("remove", help="Delete a company")
    rm.add_argument("--user", "-u", required=True)
    rm.add_argument("--key", required=True)
    s.add_parser("list", help="List companies").add_argument(
        "--user", "-u", required=True,
    )


def _company_dispatch(args, istota_config) -> int:
    a = args.company_action
    ctx = _load_user_ctx(istota_config, args.user)
    if a == "remove":
        from istota.money import config_refs
        blocked = _blocked_by_references(config_refs.entity_references, ctx, args.key)
        if blocked is not None:
            return blocked
        ok = config_store.delete_company(ctx.db_path, args.key)
        _print_state("noop" if not ok else "removed", f"company key={args.key}")
        return 0
    if a == "list":
        for c in config_store.list_companies(ctx.db_path):
            print(f"{c.key:20} {c.name:30} {c.bank_account or '':30}")
        return 0
    fields: dict[str, Any] = {}
    for cli_field, db_field in (
        ("name", "name"), ("address", "address"), ("email", "email"),
        ("payment_instructions", "payment_instructions"),
        ("logo", "logo"), ("ar_account", "ar_account"),
        ("bank_account", "bank_account"), ("currency", "currency"),
    ):
        v = getattr(args, cli_field, None)
        if v is not None:
            fields[db_field] = v
    _, state = config_store.upsert_company(ctx.db_path, args.key, **fields)
    _print_state(state, f"company key={args.key}")
    return 0


def _add_service(sub) -> None:
    p = sub.add_parser("service", help="Manage invoicing services")
    s = p.add_subparsers(dest="service_action", required=True)
    for action in ("add", "update"):
        a = s.add_parser(action, help=f"{action} a service")
        a.add_argument("--user", "-u", required=True)
        a.add_argument("--key", required=True)
        a.add_argument("--display-name")
        a.add_argument("--rate", type=float)
        a.add_argument("--type", choices=("hours", "days", "flat", "other"))
        a.add_argument("--income-account")
    rm = s.add_parser("remove", help="Delete a service")
    rm.add_argument("--user", "-u", required=True)
    rm.add_argument("--key", required=True)
    s.add_parser("list", help="List services").add_argument(
        "--user", "-u", required=True,
    )


def _service_dispatch(args, istota_config) -> int:
    a = args.service_action
    ctx = _load_user_ctx(istota_config, args.user)
    if a == "remove":
        from istota.money import config_refs
        blocked = _blocked_by_references(config_refs.service_references, ctx, args.key)
        if blocked is not None:
            return blocked
        ok = config_store.delete_service(ctx.db_path, args.key)
        _print_state("noop" if not ok else "removed", f"service key={args.key}")
        return 0
    if a == "list":
        cfg = config_store.load_invoicing(ctx.db_path)
        for s in cfg.services.values():
            print(f"{s.key:20} {s.display_name:30} {s.type:8} {s.rate:>10.2f}")
        return 0
    fields: dict[str, Any] = {}
    for cli_field, db_field in (
        ("display_name", "display_name"), ("rate", "rate"),
        ("type", "type"), ("income_account", "income_account"),
    ):
        v = getattr(args, cli_field, None)
        if v is not None:
            fields[db_field] = v
    _, state = config_store.upsert_service(ctx.db_path, args.key, **fields)
    _print_state(state, f"service key={args.key}")
    return 0


# =============================================================================
# Tax
# =============================================================================


def _add_tax(sub) -> None:
    p = sub.add_parser("tax", help="Manage tax config")
    s = p.add_subparsers(dest="tax_action", required=True)

    setp = s.add_parser("set", help="Set scalar tax fields")
    setp.add_argument("--user", "-u", required=True)
    setp.add_argument("--filing-status", choices=("mfj", "single"))
    setp.add_argument("--tax-year", type=int)
    setp.add_argument(
        "--state",
        help='Two-letter state code, or "" for no state tax',
    )
    setp.add_argument("--w2-income", type=float)
    setp.add_argument("--w2-federal-withholding", type=float)
    setp.add_argument("--w2-state-withholding", type=float)
    setp.add_argument("--federal-estimated-paid", type=float)
    setp.add_argument("--state-estimated-paid", type=float)
    setp.add_argument("--enable-qbi-deduction", action="store_true", default=None)
    setp.add_argument("--no-qbi-deduction", dest="enable_qbi_deduction",
                      action="store_false", default=None)
    setp.add_argument("--prior-year-federal-tax", type=float)
    setp.add_argument("--prior-year-state-tax", type=float)

    rates = s.add_parser("rates", help="Manage year-keyed rates")
    rs = rates.add_subparsers(dest="rates_action", required=True)
    rset = rs.add_parser("set", help="Upsert tax_year_rates row")
    rset.add_argument("--user", "-u", required=True)
    rset.add_argument("--year", type=int, required=True)
    rset.add_argument("--ss-wage-base", type=float)
    rset.add_argument("--ss-rate", type=float)
    rset.add_argument("--medicare-rate", type=float)
    rset.add_argument("--se-taxable-fraction", type=float)
    rrm = rs.add_parser("remove", help="Delete a year row")
    rrm.add_argument("--user", "-u", required=True)
    rrm.add_argument("--year", type=int, required=True)
    rls = rs.add_parser("list", help="List year rows")
    rls.add_argument("--user", "-u", required=True)

    pat = s.add_parser("pattern", help="Manage SE account patterns")
    ps = pat.add_subparsers(dest="pattern_action", required=True)
    padd = ps.add_parser("add")
    padd.add_argument("--user", "-u", required=True)
    padd.add_argument("--kind", choices=("se_income", "se_expense"), required=True)
    padd.add_argument("--pattern", required=True)
    prm = ps.add_parser("remove")
    prm.add_argument("--user", "-u", required=True)
    prm.add_argument("--kind", choices=("se_income", "se_expense"), required=True)
    prm.add_argument("--pattern", required=True)
    ps.add_parser("list").add_argument("--user", "-u", required=True)

    # Brackets and standard deductions, keyed on the three dimensions they
    # actually have. `rates` keeps the payroll scalars, which are genuinely
    # federal, year-keyed and status-agnostic.
    sched = s.add_parser(
        "schedule", help="Manage bracket / standard-deduction overrides",
    )
    ss_ = sched.add_subparsers(dest="schedule_action", required=True)
    sset = ss_.add_parser("set", help="Upsert a bracket/deduction override")
    sset.add_argument("--user", "-u", required=True)
    sset.add_argument("--year", type=int, required=True)
    sset.add_argument(
        "--jurisdiction", required=True,
        help="'federal' or a two-letter state code",
    )
    sset.add_argument("--filing-status", required=True, choices=("mfj", "single"))
    sset.add_argument("--standard-deduction", type=float)
    sset.add_argument(
        "--brackets-json",
        help='JSON array of [threshold, rate] pairs, e.g. \'[[0,0.04],[100000,0.06]]\'',
    )
    srm = ss_.add_parser("remove", help="Drop an override, reverting to bundled")
    srm.add_argument("--user", "-u", required=True)
    srm.add_argument("--year", type=int, required=True)
    srm.add_argument("--jurisdiction", required=True)
    srm.add_argument("--filing-status", required=True, choices=("mfj", "single"))
    sls = ss_.add_parser("list", help="List stored overrides")
    sls.add_argument("--user", "-u", required=True)


def _tax_dispatch(args, istota_config) -> int:
    a = args.tax_action
    if a == "set":
        return _tax_set(args, istota_config)
    if a == "rates":
        return _tax_rates_dispatch(args, istota_config)
    if a == "schedule":
        return _tax_schedule_dispatch(args, istota_config)
    if a == "pattern":
        return _tax_pattern_dispatch(args, istota_config)
    return _print_error(f"unknown tax action: {a}")


def _tax_schedule_dispatch(args, istota_config) -> int:
    sa = args.schedule_action
    ctx = _load_user_ctx(istota_config, args.user)
    if sa == "list":
        for row in config_store.list_tax_schedules(ctx.db_path):
            print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0

    label = (
        f"schedule year={args.year} jurisdiction={args.jurisdiction} "
        f"filing_status={args.filing_status}"
    )
    try:
        if sa == "remove":
            ok = config_store.delete_tax_schedule(
                ctx.db_path, args.year, args.jurisdiction, args.filing_status,
            )
            _print_state("removed" if ok else "noop", label)
            return 0
        # An absent flag leaves the field alone; `--brackets-json null` (or
        # `remove`) is how you revert one to the bundled value.
        brackets = (
            json.loads(args.brackets_json)
            if args.brackets_json is not None else config_store.UNSET
        )
        std_ded = (
            args.standard_deduction
            if args.standard_deduction is not None else config_store.UNSET
        )
        state = config_store.upsert_tax_schedule(
            ctx.db_path, args.year, args.jurisdiction, args.filing_status,
            brackets=brackets, standard_deduction=std_ded,
        )
    except ValueError as exc:
        return _print_error(str(exc))
    except json.JSONDecodeError as exc:
        return _print_error(f"--brackets-json is not valid JSON: {exc}")
    _print_state(state, label)
    return 0


def _tax_set(args, istota_config) -> int:
    ctx = _load_user_ctx(istota_config, args.user)
    cfg = config_store.load_tax(ctx.db_path)
    before = _tax_snapshot(cfg)
    if args.filing_status is not None:
        cfg.filing_status = args.filing_status
    if args.tax_year is not None:
        cfg.tax_year = args.tax_year
    if args.state is not None:
        # "" is a real choice (no state tax), so `is not None` rather than a
        # truthiness check — otherwise the state could never be cleared.
        code = args.state.strip().upper()
        if code:
            from istota.money.core.tax_data import load_tax_rates
            if load_tax_rates().jurisdiction(code) is None:
                # Validated here as well as in the web PUT: a typo'd code
                # stores fine and then resolves to nothing forever, and the
                # settings page is the surface that has to cope with it.
                return _print_error(f"unknown state: {code}")
        cfg.state = code
    if args.w2_income is not None:
        cfg.w2_income = args.w2_income
    if args.w2_federal_withholding is not None:
        cfg.w2_federal_withholding = args.w2_federal_withholding
    if args.w2_state_withholding is not None:
        cfg.w2_state_withholding = args.w2_state_withholding
    if args.federal_estimated_paid is not None:
        cfg.federal_estimated_paid = args.federal_estimated_paid
    if args.state_estimated_paid is not None:
        cfg.state_estimated_paid = args.state_estimated_paid
    if args.enable_qbi_deduction is not None:
        cfg.enable_qbi_deduction = args.enable_qbi_deduction
    if args.prior_year_federal_tax is not None:
        cfg.prior_year_federal_tax = args.prior_year_federal_tax
    if args.prior_year_state_tax is not None:
        cfg.prior_year_state_tax = args.prior_year_state_tax
    after = _tax_snapshot(cfg)
    if before == after:
        _print_state("noop", f"tax_year={cfg.tax_year}")
        return 0
    config_store.save_tax(ctx.db_path, cfg, replace_collections=False)
    _print_state("updated", f"tax_year={cfg.tax_year}")
    return 0


def _tax_snapshot(cfg) -> dict:
    return {
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
    }


def _tax_rates_dispatch(args, istota_config) -> int:
    ra = args.rates_action
    ctx = _load_user_ctx(istota_config, args.user)
    if ra == "list":
        for row in config_store.list_tax_year_rates(ctx.db_path):
            print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0
    if ra == "remove":
        ok = config_store.delete_tax_year_rates(ctx.db_path, args.year)
        _print_state("noop" if not ok else "removed", f"year_rates year={args.year}")
        return 0
    # set
    # Payroll scalars only. Brackets and standard deductions moved to
    # `tax schedule`, which carries the filing-status dimension they have.
    fields: dict[str, Any] = {}
    for k in ("ss_wage_base", "ss_rate", "medicare_rate", "se_taxable_fraction"):
        v = getattr(args, k, None)
        if v is not None:
            fields[k] = v
    state = config_store.upsert_tax_year_rates(ctx.db_path, args.year, **fields)
    _print_state(state, f"year_rates year={args.year}")
    return 0


def _tax_pattern_dispatch(args, istota_config) -> int:
    pa = args.pattern_action
    ctx = _load_user_ctx(istota_config, args.user)
    if pa == "list":
        patterns = config_store.list_tax_patterns(ctx.db_path)
        for kind in ("se_income", "se_expense"):
            for p in patterns.get(kind, []):
                print(f"{kind:12} {p}")
        return 0
    if pa == "add":
        state = config_store.add_tax_pattern(ctx.db_path, args.kind, args.pattern)
        _print_state(state, f"pattern kind={args.kind} pattern={args.pattern}")
        return 0
    if pa == "remove":
        ok = config_store.remove_tax_pattern(ctx.db_path, args.kind, args.pattern)
        _print_state("noop" if not ok else "removed",
                     f"pattern kind={args.kind} pattern={args.pattern}")
        return 0
    return _print_error(f"unknown pattern action: {pa}")


# =============================================================================
# Monarch
# =============================================================================


def _add_monarch(sub) -> None:
    p = sub.add_parser("monarch", help="Manage monarch sync config")
    s = p.add_subparsers(dest="monarch_action", required=True)

    prof = s.add_parser("profile", help="Manage profiles")
    ps = prof.add_subparsers(dest="profile_action", required=True)
    for action in ("add", "update"):
        a = ps.add_parser(action)
        a.add_argument("--user", "-u", required=True)
        a.add_argument("--name", required=True)
        a.add_argument("--ledger")
        a.add_argument("--lookback-days", type=int)
        a.add_argument("--default-account")
        a.add_argument("--recategorize-account")
    prm = ps.add_parser("remove")
    prm.add_argument("--user", "-u", required=True)
    prm.add_argument("--name", required=True)
    pls = ps.add_parser("list")
    pls.add_argument("--user", "-u", required=True)

    am = s.add_parser("account-map", help="Manage profile/global account mappings")
    ams = am.add_subparsers(dest="account_map_action", required=True)
    amset = ams.add_parser("set")
    _add_profile_or_global(amset)
    amset.add_argument("--monarch-name", required=True)
    amset.add_argument("--account", required=True)
    amunset = ams.add_parser("unset")
    _add_profile_or_global(amunset)
    amunset.add_argument("--monarch-name", required=True)
    amlist = ams.add_parser("list")
    _add_profile_or_global(amlist)

    cm = s.add_parser("category-map", help="Manage profile/global category mappings")
    cms = cm.add_subparsers(dest="category_map_action", required=True)
    cmset = cms.add_parser("set")
    _add_profile_or_global(cmset)
    cmset.add_argument("--category", required=True)
    cmset.add_argument("--account", required=True)
    cmunset = cms.add_parser("unset")
    _add_profile_or_global(cmunset)
    cmunset.add_argument("--category", required=True)
    cmlist = cms.add_parser("list")
    _add_profile_or_global(cmlist)

    tf = s.add_parser("tag-filter", help="Manage tag filters")
    tfs = tf.add_subparsers(dest="tag_filter_action", required=True)
    tfadd = tfs.add_parser("add")
    _add_profile_or_global(tfadd)
    tfadd.add_argument("--kind", choices=("include", "exclude"), required=True)
    tfadd.add_argument("--tag", required=True)
    tfrm = tfs.add_parser("remove")
    _add_profile_or_global(tfrm)
    tfrm.add_argument("--kind", choices=("include", "exclude"), required=True)
    tfrm.add_argument("--tag", required=True)
    tflist = tfs.add_parser("list")
    _add_profile_or_global(tflist)


def _add_profile_or_global(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user", "-u", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile")
    grp.add_argument("--global", dest="global_scope", action="store_true")


def _resolve_profile_arg(args) -> str | None:
    if getattr(args, "global_scope", False):
        return None
    return args.profile


def _monarch_dispatch(args, istota_config) -> int:
    a = args.monarch_action
    if a == "profile":
        return _monarch_profile_dispatch(args, istota_config)
    if a == "account-map":
        return _monarch_account_map_dispatch(args, istota_config)
    if a == "category-map":
        return _monarch_category_map_dispatch(args, istota_config)
    if a == "tag-filter":
        return _monarch_tag_filter_dispatch(args, istota_config)
    return _print_error(f"unknown monarch action: {a}")


def _monarch_profile_dispatch(args, istota_config) -> int:
    pa = args.profile_action
    ctx = _load_user_ctx(istota_config, args.user)
    if pa == "remove":
        ok = config_store.delete_monarch_profile(ctx.db_path, args.name)
        _print_state("noop" if not ok else "removed", f"profile name={args.name}")
        return 0
    if pa == "list":
        for row in config_store.list_monarch_profiles(ctx.db_path):
            print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0
    fields: dict[str, Any] = {}
    for k in ("ledger", "lookback_days", "default_account", "recategorize_account"):
        v = getattr(args, k, None)
        if v is not None:
            fields[k] = v
    _, state = config_store.upsert_monarch_profile(ctx.db_path, args.name, **fields)
    _print_state(state, f"profile name={args.name}")
    return 0


def _monarch_account_map_dispatch(args, istota_config) -> int:
    a = args.account_map_action
    ctx = _load_user_ctx(istota_config, args.user)
    profile = _resolve_profile_arg(args)
    scope = profile or "__global__"
    if a == "set":
        state = config_store.set_account_map_entry(
            ctx.db_path, profile, args.monarch_name, args.account,
        )
        _print_state(state, f"account_map profile={scope} {args.monarch_name}={args.account}")
        return 0
    if a == "unset":
        ok = config_store.unset_account_map_entry(
            ctx.db_path, profile, args.monarch_name,
        )
        _print_state("noop" if not ok else "removed",
                     f"account_map profile={scope} {args.monarch_name}")
        return 0
    if a == "list":
        for k, v in config_store.get_account_map(ctx.db_path, profile).items():
            print(f"{k}\t{v}")
        return 0
    return _print_error(f"unknown account-map action: {a}")


def _monarch_category_map_dispatch(args, istota_config) -> int:
    a = args.category_map_action
    ctx = _load_user_ctx(istota_config, args.user)
    profile = _resolve_profile_arg(args)
    scope = profile or "__global__"
    if a == "set":
        state = config_store.set_category_map_entry(
            ctx.db_path, profile, args.category, args.account,
        )
        _print_state(state, f"category_map profile={scope} {args.category}={args.account}")
        return 0
    if a == "unset":
        ok = config_store.unset_category_map_entry(
            ctx.db_path, profile, args.category,
        )
        _print_state("noop" if not ok else "removed",
                     f"category_map profile={scope} {args.category}")
        return 0
    if a == "list":
        for k, v in config_store.get_category_map(ctx.db_path, profile).items():
            print(f"{k}\t{v}")
        return 0
    return _print_error(f"unknown category-map action: {a}")


def _monarch_tag_filter_dispatch(args, istota_config) -> int:
    a = args.tag_filter_action
    ctx = _load_user_ctx(istota_config, args.user)
    profile = _resolve_profile_arg(args)
    scope = profile or "__global__"
    if a == "add":
        state = config_store.add_tag_filter(
            ctx.db_path, profile, args.kind, args.tag,
        )
        _print_state(state, f"tag_filter profile={scope} kind={args.kind} tag={args.tag}")
        return 0
    if a == "remove":
        ok = config_store.remove_tag_filter(
            ctx.db_path, profile, args.kind, args.tag,
        )
        _print_state("noop" if not ok else "removed",
                     f"tag_filter profile={scope} kind={args.kind} tag={args.tag}")
        return 0
    if a == "list":
        tf = config_store.get_tag_filters(ctx.db_path, profile)
        for tag in tf["include"]:
            print(f"include\t{tag}")
        for tag in tf["exclude"]:
            print(f"exclude\t{tag}")
        return 0
    return _print_error(f"unknown tag-filter action: {a}")


# =============================================================================
# Transaction rules -- `istota money rules ...`
#
# A top-level group rather than a verb under `monarch`, because a rule is no
# longer a Monarch concept: it is matched against the `NormalizedTransaction`
# seam every importer produces, and its scope is a ledger. The three
# `monarch account-map|category-map|tag-filter` groups stay exactly as they
# were, serving the dict views over the same table.
#
# The third front end over `config_store`'s rule accessors, after the six HTTP
# routes and the web section. What it owes them is the same validation and the
# same refusals, which it gets by calling the same accessors -- and one thing
# of its own: no *error or state* message may carry the user's `match_value`
# or `target`, which binds `_print_error` and `_print_rule_state`. They are the
# user's financial data, this stdout reaches an Ansible log, and
# `validate_rule_fields` and `_duplicate_rule_error` already answer with a
# field name and a constraint for that reason. `list` and `test` do print
# them, and are meant to: reading a rule back is what they are for.
# =============================================================================


def _add_rules(sub) -> None:
    p = sub.add_parser("rules", help="Manage transaction rules")
    s = p.add_subparsers(dest="rules_action", required=True)

    ls = s.add_parser("list", help="Rules in one scope, in evaluation order")
    ls.add_argument("--user", "-u", required=True)
    ls.add_argument("--ledger", help="Only this ledger; '' is the any-ledger scope")
    ls.add_argument("--source", help="Only this source, e.g. monarch-api")
    ls.add_argument(
        "--enabled-only", action="store_true",
        help="Drop the switched-off rules an import already ignores",
    )

    add = s.add_parser("add", help="Write one rule")
    add.add_argument("--user", "-u", required=True)
    _add_rule_scope(add, required=True)
    add.add_argument("--field", required=True, choices=rule_engine.FIELDS)
    add.add_argument("--match-value", required=True,
                     help="What the field is compared against")
    add.add_argument("--action", required=True, choices=rule_engine.ACTIONS)
    # No argparse defaults on the optional columns, deliberately: an unset
    # flag is omitted from the write and `validate_rule_fields` applies the
    # store's own default. Restating them here would be a second place for
    # `iexact` and `100` to live, and the two would drift.
    add.add_argument("--match-kind", choices=rule_engine.MATCH_KINDS,
                     help="Default iexact (case-insensitive equality)")
    add.add_argument("--target",
                     help="Beancount account to post to; omit for a skip rule")
    add.add_argument(
        "--priority", type=int,
        help="Lower runs first, "
             f"{rule_engine.MIN_PRIORITY}..{rule_engine.MAX_PRIORITY} "
             f"(default {rule_engine.DEFAULT_PRIORITY})",
    )
    add.add_argument("--note", help="Free text, shown in the editor")
    add.add_argument("--disabled", action="store_true",
                     help="Store the rule switched off")

    up = s.add_parser("update", help="Change one stored rule")
    up.add_argument("--user", "-u", required=True)
    up.add_argument("--id", type=int, required=True, dest="rule_id")
    _add_rule_scope(up, required=False)
    up.add_argument("--field", choices=rule_engine.FIELDS)
    up.add_argument("--match-value")
    up.add_argument("--action", choices=rule_engine.ACTIONS)
    up.add_argument("--match-kind", choices=rule_engine.MATCH_KINDS)
    up.add_argument("--target")
    up.add_argument("--priority", type=int)
    up.add_argument("--note")
    grp = up.add_mutually_exclusive_group()
    grp.add_argument("--enable", dest="enabled", action="store_true", default=None)
    grp.add_argument("--disable", dest="enabled", action="store_false", default=None)

    rm = s.add_parser("remove", help="Delete one rule")
    rm.add_argument("--user", "-u", required=True)
    rm.add_argument("--id", type=int, required=True, dest="rule_id")

    test = s.add_parser(
        "test", help="Resolve a made-up transaction against the stored rules",
    )
    test.add_argument("--user", "-u", required=True)
    _add_rule_scope(test, required=True)
    test.add_argument("--category", default="")
    test.add_argument("--account", default="", help="Source account display name")
    test.add_argument("--payee", default="")
    test.add_argument("--notes", default="", help="The transaction's notes")
    test.add_argument("--tag", action="append", dest="tags",
                      help="Repeatable; a tag the transaction carries")


def _add_rule_scope(p: argparse.ArgumentParser, *, required: bool) -> None:
    """The two scope columns.

    Required on a write and on a preview, for the reason
    ``routes._explicit_scope`` gives: both default to ``''`` in the table and
    the engine reads ``''`` as "any", so an omitted ``--ledger`` on a create is
    a rule silently applying to every ledger and every source, and an omitted
    one on a preview answers about a scope the operator is not looking at.
    ``''`` stays legal; it just has to be typed. On ``update`` neither is
    required -- the row already carries a scope somebody chose, and omitting
    one means "leave it".
    """
    p.add_argument("--ledger", required=required,
                   help="Ledger name; '' is the any-ledger scope")
    p.add_argument("--source", required=required,
                   help="Importer name, e.g. monarch-api; '' is any source")


def _rule_write_fields(args, *, creating: bool) -> dict[str, Any]:
    """The rule columns this argv names, and only those.

    ``update`` merges into the stored row, so an unset flag must not reach the
    store as a default that overwrites what is there. ``origin`` is
    deliberately not settable: a rule written by an operator is a user rule,
    the store reserves ``seed`` for the shipped set, and a caller-set ``seed``
    wedges every later map write in that scope.
    """
    fields: dict[str, Any] = {}
    for key in ("ledger", "source", "field", "match_kind", "match_value",
                "action", "target", "priority", "note"):
        value = getattr(args, key, None)
        if value is not None:
            fields[key] = value
    if creating:
        fields["origin"] = "user"
        fields["enabled"] = not args.disabled
    elif args.enabled is not None:
        fields["enabled"] = args.enabled
    return fields


def _rules_dispatch(args, istota_config) -> int:
    a = args.rules_action
    ctx = _load_user_ctx(istota_config, args.user)
    # Ahead of the `IntegrityError` handlers below, not inside them. Every
    # accessor calls this itself, and on a first-touch database it runs the
    # schema migration and seeds ~50 rows -- so a genuine schema fault raised
    # in there would otherwise be caught by the duplicate handler and reported
    # to the operator as "a rule with this scope already exists".
    config_store.init_db(ctx.db_path)

    if a == "list":
        for row in config_store.list_transaction_rules(
            ctx.db_path, ledger=args.ledger, source=args.source,
            include_disabled=not args.enabled_only,
        ):
            print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0

    if a == "add":
        try:
            rule = config_store.create_transaction_rule(
                ctx.db_path, **_rule_write_fields(args, creating=True),
            )
        except sqlite3.IntegrityError:
            # The store looks for a duplicate and then inserts, which is not
            # atomic across connections. A concurrent web write or a second
            # CLI run can take the key in between; answer as the non-racing
            # path does rather than with a traceback.
            return _print_error(_DUPLICATE_RULE)
        return _print_rule_state("created", rule)

    if a == "update":
        try:
            rule = config_store.update_transaction_rule(
                ctx.db_path, args.rule_id, **_rule_write_fields(args, creating=False),
            )
        except sqlite3.IntegrityError:
            return _print_error(_DUPLICATE_RULE)
        if rule is None:
            return _print_error(f"no rule with id {args.rule_id}")
        return _print_rule_state("updated", rule)

    if a == "remove":
        removed = config_store.delete_transaction_rule(ctx.db_path, args.rule_id)
        _print_state("removed" if removed else "noop", f"rule id={args.rule_id}")
        return 0

    if a == "test":
        return _rules_test(ctx, args)

    return _print_error(f"unknown rules action: {a}")


# Deliberately the store's own refusal minus the id, matching what the HTTP
# create answers when it loses the same race: looking the id up costs a second
# query on a path that is already losing one.
_DUPLICATE_RULE = "a rule with this scope, match and action already exists"


def _print_rule_state(state: str, rule: dict) -> int:
    """Name the id, the field and the action -- never the value or the target.

    The id is what a follow-up ``update`` or ``remove`` needs, and the other
    two say which rule changed without putting the user's financial data on
    stdout.
    """
    _print_state(
        state,
        f"rule id={rule['id']} field={rule['field']} action={rule['action']}",
    )
    return 0


def _rules_test(ctx, args) -> int:
    """Resolve a made-up transaction, as ``POST /config/transaction-rules/test``
    does, and print the resolution.

    The web section's ordered trace -- every rule in scope, including the ones
    that matched into a slot already filled -- is not reproduced here. Pair
    this with ``rules list``, which prints the same rules in the same
    evaluation order.
    """
    from datetime import date

    from istota.money.core.importers.base import NormalizedTransaction

    tags = args.tags or []
    if len(tags) > rule_engine.MAX_PREVIEW_TAGS:
        # Refused rather than cut, and refused here because the HTTP preview
        # refuses it: dropping a tag silently changes which rules fire, so the
        # three `test` surfaces have to answer one input one way.
        return _print_error(
            f"--tag: at most {rule_engine.MAX_PREVIEW_TAGS} entries",
        )

    stored = config_store.load_rules_for_run(ctx.db_path, args.ledger, args.source)
    if stored is None:
        # `None` is not "no rules": it says the one-time migration has not
        # completed, so an import still resolves from the legacy maps. A
        # preview off the table would describe behaviour this deployment does
        # not have, which is worse than no preview.
        return _print_error(
            "transaction rules are not in force here: the one-time migration "
            "has not completed, so an import still resolves from the legacy "
            "maps",
        )

    cap = rule_engine.MAX_SUBJECT_CHARS
    txn = NormalizedTransaction(
        date=date.today(),
        amount=0.0,
        payee=args.payee[:cap],
        category=args.category[:cap],
        account_name=args.account[:cap],
        notes=args.notes[:cap],
        tags=[tag[:cap] for tag in tags],
    )
    compiled, dropped = rule_engine.compile_rules_reporting(stored)
    resolution = rule_engine.resolve(txn, compiled)
    print(json.dumps({
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
        # Rows the engine could not compile and skipped. Dropping a `skip`
        # imports a transaction the user excluded on purpose, so it is
        # reported rather than left in a log line.
        "dropped": dropped,
    }, indent=2, ensure_ascii=False))
    return 0


# =============================================================================
# Top-level dispatch table
# =============================================================================


_DISPATCH = {
    "config": _config_dispatch,
    "client": _client_dispatch,
    "company": _company_dispatch,
    "service": _service_dispatch,
    "tax": _tax_dispatch,
    "monarch": _monarch_dispatch,
    "rules": _rules_dispatch,
    # Operational commands all forward to the money Click tree.
    **{name: _operational_dispatch for name in _OPERATIONAL_COMMANDS},
}
