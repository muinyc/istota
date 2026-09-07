"""Click CLI for moneyman."""

from __future__ import annotations

import copy
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import click

from istota.experimental import requires_feature


def _output(result: dict) -> None:
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        sys.exit(1)


@dataclass
class UserContext:
    """Per-user configuration resolved from a [users.*] section."""
    data_dir: Path
    ledgers: list[dict] = field(default_factory=list)
    invoicing_config_path: Path | None = None
    monarch_config_path: Path | None = None
    tax_config_path: Path | None = None
    db_path: Path | None = None
    # Operator gate on the third-party ticker-metadata lookup ([money]
    # autoclass_lookup). Resolved from istota config at context-build time,
    # so both the web routes and the CLI honour one switch.
    autoclass_lookup: bool = True


class Context:
    """CLI context holding resolved configuration.

    All paths are resolved to absolute filesystem paths at config load time.
    """
    def __init__(self):
        self.data_dir: Path | None = None
        self.ledgers: list[dict] = []
        self.monarch_config_path: Path | None = None
        self.invoicing_config_path: Path | None = None
        self.tax_config_path: Path | None = None
        self.db_path: Path | None = None
        self.secrets: dict | None = None
        self.api_key: str | None = None
        self.users: dict[str, UserContext] = {}
        self.active_user: str | None = None
        self.autoclass_lookup: bool = True

    @property
    def has_single_user(self) -> bool:
        return len(self.users) <= 1

    @property
    def available_users(self) -> list[str]:
        return sorted(self.users.keys())

    def activate_user(self, user_key: str) -> None:
        """Activate a user, setting data_dir/ledgers/etc from their config."""
        if user_key not in self.users:
            raise click.ClickException(f"Unknown user: {user_key}")
        uctx = self.users[user_key]
        self.active_user = user_key
        self.data_dir = uctx.data_dir
        self.ledgers = uctx.ledgers
        self.invoicing_config_path = uctx.invoicing_config_path
        self.monarch_config_path = uctx.monarch_config_path
        self.tax_config_path = uctx.tax_config_path
        self.db_path = uctx.db_path
        self.autoclass_lookup = uctx.autoclass_lookup

    def for_user(self, user_key: str) -> Context:
        """Return a shallow copy with the given user activated.

        Safe for concurrent use (each request gets its own copy).
        """
        ctx = copy.copy(self)
        ctx.users = self.users  # share the users dict (read-only)
        ctx.activate_user(user_key)
        return ctx

    def for_default_user(self) -> Context:
        """Return a copy with the single/default user activated."""
        if not self.users:
            return self
        key = next(iter(self.users))
        return self.for_user(key)


pass_ctx = click.make_pass_decorator(Context, ensure=True)


def _resolve(data_dir: Path, raw: str) -> Path:
    """Resolve a path relative to data_dir. Absolute paths are returned as-is."""
    p = Path(raw)
    if p.is_absolute():
        return p
    return data_dir / raw


def resolve_ledger(ledger: str | None, config_ledgers: list[dict]) -> Path:
    if not config_ledgers:
        raise click.ClickException("No ledgers configured")
    if ledger:
        for entry in config_ledgers:
            if entry["name"].lower() == ledger.lower():
                return entry["path"]
        available = [entry["name"] for entry in config_ledgers]
        raise click.ClickException(f"Ledger '{ledger}' not found. Available: {', '.join(available)}")
    return config_ledgers[0]["path"]


def _ledger_scope_name(ledger: str | None, config_ledgers: list[dict]) -> str:
    """The ledger name a transaction rule's scope is matched against.

    ``resolve_ledger`` matches case-insensitively and hands back a *path*, so
    a name has to be recovered separately. The configured spelling is what
    travels rather than the user's ``--ledger`` argument, which is only a
    lookup key and may be cased however they typed it.

    This is deliberately **not** the same string as the scope a profile run
    uses: that one is ``monarch_profiles.ledger``, which the migration copied
    into ``transaction_rules.ledger``, while this one comes from the money
    TOML's ledger list. The two are allowed to differ in case, and
    ``load_rules_for_run`` folds case on the comparison for that reason — a
    ledger-scoped rule matching nothing is indistinguishable from the user
    having written none.

    ``''`` where nothing resolves, which the engine reads as the global scope
    — never a guess, and never the raw argument.
    """
    if not ledger:
        return config_ledgers[0]["name"] if config_ledgers else ""
    for entry in config_ledgers:
        if entry["name"].lower() == ledger.lower():
            return entry["name"]
    return ""


def _require_db(ctx: Context):
    """Get a DB connection or fail."""
    conn = _get_db_conn(ctx)
    if not conn:
        raise click.ClickException("No database configured")
    return conn


def _autoclass_lookup_enabled(ctx: Context) -> bool:
    """Whether this run may send tickers to the third-party metadata API.

    Held symbols are private financial data and the lookup runs outside the
    sandbox's CONNECT allowlist, so the operator gets a switch. Default on —
    an absent attribute (a hand-built Context in a test) reads as on.
    """
    return bool(getattr(ctx, "autoclass_lookup", True))


def _require_active_user(ctx: Context) -> None:
    """Ensure a user is active when multi-user config is used."""
    if not ctx.has_single_user and ctx.active_user is None:
        raise click.ClickException(
            "Multiple users configured. Use --user to select one: "
            + ", ".join(ctx.available_users)
        )


def _require_data_dir(ctx: Context) -> Path:
    """Get data_dir or fail."""
    if not ctx.data_dir:
        _require_active_user(ctx)
        raise click.ClickException("No data_dir configured")
    return ctx.data_dir


def _get_db_conn(ctx: Context):
    if not ctx.db_path:
        return None
    from istota import sqlite_util
    from istota.money.db import init_db
    ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(ctx.db_path)
    # timeout=5.0 (sqlite3's own default) and no busy_timeout pragma: stated
    # rather than inherited, because `open_db` defaults to 30 and raising a CLI
    # command's lock budget is a change to what succeeds, not a refactor.
    return sqlite_util.connect(
        ctx.db_path, timeout=5.0, busy_timeout_ms=None, foreign_keys=False,
    )


def _load_invoicing_config(ctx: Context):
    """Parse invoicing config and resolve accounting_path + invoice_output_dir.

    Returns (config, accounting_path, invoice_output_dir) where all paths are
    absolute.  ``invoice_output_dir`` is resolved relative to ``data_dir`` so
    that PDF writes always land inside the data directory, regardless of what
    ``accounting_path`` points to.
    """
    from istota.money import config_store
    if ctx.db_path is None or not config_store.has_invoicing_data(ctx.db_path):
        raise click.ClickException(
            "No invoicing config in the DB for this user. Seed it via "
            "`istota money config import` or `istota money client|company|service add`.",
        )
    config = config_store.load_invoicing(ctx.db_path)
    if ctx.data_dir:
        accounting_path = _resolve(ctx.data_dir, config.accounting_path)
        invoice_output_dir = _resolve(ctx.data_dir, config.invoice_output)
    else:
        accounting_path = Path(config.accounting_path).resolve()
        invoice_output_dir = Path(config.invoice_output).resolve()
    return config, accounting_path, invoice_output_dir


def _require_injected_context(ctx) -> "Context":
    """The money CLI runs only with an istota-resolved Context injected.

    There is no standalone config loader anymore — money is part of istota.
    Both entry points (the ``istota money …`` operator CLI and the money skill)
    resolve the user via ``istota.money.resolve_for_user`` (DB-backed) and inject
    the :class:`Context` through ``CliRunner.invoke(obj=...)``.
    """
    if isinstance(ctx.obj, Context) and ctx.obj.users:
        return ctx.obj
    raise click.ClickException(
        "money CLI must be invoked through istota (e.g. `istota money …`); "
        "there is no standalone config.",
    )


@click.group()
@click.option("--user", "-u", "user_key", help="Active user key (resolved by istota)")
@click.pass_context
def cli(ctx, user_key):
    """Money — accounting operations CLI (driven by istota).

    The caller (the ``istota money …`` CLI or the money skill) resolves the
    user's :class:`Context` via ``istota.money.resolve_for_user`` and injects it
    through ``CliRunner.invoke(obj=...)``. There is no file-based config loader.
    """
    mctx = _require_injected_context(ctx)
    if user_key and user_key in mctx.users:
        mctx.activate_user(user_key)


@cli.command("users")
@pass_ctx
def list_users(ctx):
    """List configured users."""
    users = []
    for key in ctx.available_users:
        uctx = ctx.users[key]
        users.append({
            "key": key,
            "data_dir": str(uctx.data_dir),
            "ledger_count": len(uctx.ledgers),
        })
    _output({
        "status": "ok",
        "user_count": len(users),
        "users": users,
    })


# =============================================================================
# Work entry commands
# =============================================================================


@cli.group()
def work():
    """Manage work log entries."""
    pass


@work.command("add")
@click.option("--date", "-d", "entry_date", required=True, help="Date (YYYY-MM-DD)")
@click.option("--client", "-c", required=True, help="Client key")
@click.option("--service", "-s", required=True, help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity (hours, days, etc.)")
@click.option("--amount", "-a", type=float, help="Fixed amount (for 'other' service type)")
@click.option("--discount", type=float, default=0, help="Discount amount")
@click.option("--description", help="Description of work")
@click.option("--entity", "-e", help="Entity override")
@pass_ctx
def work_add(ctx, entry_date, client, service, qty, amount, discount, description, entity):
    """Add a work entry."""
    from istota.money.work import add_work_entry

    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return

    data_dir = _require_data_dir(ctx)
    entry_id = add_work_entry(
        data_dir, entry_date, client.lower(), service,
        qty=qty, amount=amount, discount=discount,
        description=description or "", entity=entity or "",
    )
    _output({"status": "ok", "id": entry_id, "message": f"Added work entry #{entry_id}"})


@work.command("list")
@click.option("--client", "-c", help="Filter by client")
@click.option("--period", "-p", help="Filter by period (YYYY-MM)")
@click.option("--uninvoiced", is_flag=True, help="Show only uninvoiced entries")
@click.option("--invoiced", is_flag=True, help="Show only invoiced entries")
@pass_ctx
def work_list(ctx, client, period, uninvoiced, invoiced):
    """List work entries."""
    from istota.money.work import list_work_entries

    invoiced_filter = None
    if uninvoiced:
        invoiced_filter = False
    elif invoiced:
        invoiced_filter = True

    data_dir = _require_data_dir(ctx)
    entries = list_work_entries(data_dir, client=client, invoiced=invoiced_filter, period=period)

    _output({
        "status": "ok",
        "count": len(entries),
        "entries": [
            {
                "id": e.id,
                "uid": e.uid or None,
                "date": e.date.isoformat(),
                "client": e.client,
                "service": e.service,
                "qty": e.qty,
                "amount": e.amount,
                "discount": e.discount,
                "description": e.description,
                "entity": e.entity or None,
                "invoice": e.invoice or None,
                "paid_date": e.paid_date.isoformat() if e.paid_date else None,
            }
            for e in entries
        ],
    })


@work.command("backfill-ids")
@pass_ctx
def work_backfill_ids(ctx):
    """Stamp a stable ``uid`` on every work entry lacking one.

    Idempotent. Runs automatically on money workspace init; exposed here for
    operators who hand-add entries to the year files.
    """
    from istota.money.work import backfill_work_ids

    data_dir = _require_data_dir(ctx)
    stamped = backfill_work_ids(data_dir)
    _output({
        "status": "ok",
        "stamped": stamped,
        "message": f"Stamped {stamped} work {'entry' if stamped == 1 else 'entries'}",
    })


@work.command("remove")
@click.argument("entry_id", type=int)
@pass_ctx
def work_remove(ctx, entry_id):
    """Remove an uninvoiced work entry."""
    from istota.money.work import remove_work_entry

    data_dir = _require_data_dir(ctx)
    if remove_work_entry(data_dir, entry_id):
        _output({"status": "ok", "message": f"Removed work entry #{entry_id}"})
    else:
        _output({"status": "error", "error": f"Entry #{entry_id} not found or already invoiced"})


@work.command("update")
@click.argument("entry_id", type=int)
@click.option("--date", "-d", "entry_date", help="Date (YYYY-MM-DD)")
@click.option("--client", "-c", help="Client key")
@click.option("--service", "-s", help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity")
@click.option("--amount", "-a", type=float, help="Fixed amount")
@click.option("--discount", type=float, help="Discount")
@click.option("--description", help="Description")
@click.option("--entity", "-e", help="Entity override")
@click.option("--invoice", help="Manually assign invoice number")
@pass_ctx
def work_update(ctx, entry_id, entry_date, client, service, qty, amount, discount, description, entity, invoice):
    """Update a work entry."""
    from istota.money.work import update_work_entry

    fields = {}
    if entry_date is not None:
        fields["date"] = entry_date
    if client is not None:
        fields["client"] = client.lower()
    if service is not None:
        fields["service"] = service
    if qty is not None:
        fields["qty"] = qty
    if amount is not None:
        fields["amount"] = amount
    if discount is not None:
        fields["discount"] = discount
    if description is not None:
        fields["description"] = description
    if entity is not None:
        fields["entity"] = entity
    if invoice is not None:
        fields["invoice"] = invoice

    if not fields:
        _output({"status": "error", "error": "No fields to update"})
        return

    data_dir = _require_data_dir(ctx)
    if update_work_entry(data_dir, entry_id, **fields):
        _output({"status": "ok", "message": f"Updated work entry #{entry_id}"})
    else:
        _output({"status": "error", "error": f"Entry #{entry_id} not found or already invoiced"})


# =============================================================================
# Ledger commands
# =============================================================================


@cli.command("list")
@pass_ctx
def list_ledgers(ctx):
    """List available ledgers."""
    _require_active_user(ctx)
    if ctx.ledgers:
        _output({
            "status": "ok",
            "ledger_count": len(ctx.ledgers),
            "ledgers": [{"name": e["name"], "path": str(e["path"])} for e in ctx.ledgers],
        })
    else:
        _output({"status": "error", "error": "No ledgers configured"})


@cli.command()
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def check(ctx, ledger):
    """Validate ledger file."""
    from istota.money.core.ledger import check as ledger_check
    _output(ledger_check(resolve_ledger(ledger, ctx.ledgers)))


@cli.command()
@click.option("--account", "-a", help="Filter by account pattern (regex)")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def balances(ctx, account, ledger):
    """Show account balances."""
    from istota.money.core.ledger import balances as ledger_balances
    _output(ledger_balances(resolve_ledger(ledger, ctx.ledgers), account))


@cli.command()
@click.argument("bql")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def query(ctx, bql, ledger):
    """Run a BQL query."""
    from istota.money.core.ledger import query as ledger_query
    _output(ledger_query(resolve_ledger(ledger, ctx.ledgers), bql))


@cli.command()
@click.argument("report_type", type=click.Choice(["income-statement", "balance-sheet"]))
@click.option("--year", "-y", type=int, help="Year for report")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def report(ctx, report_type, year, ledger):
    """Generate financial report."""
    from istota.money.core.ledger import report as ledger_report
    _output(ledger_report(resolve_ledger(ledger, ctx.ledgers), report_type, year))


@cli.command()
@click.argument("symbol")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
@requires_feature("money_tax")
def lots(ctx, symbol, ledger):
    """Show open lots for a security. Experimental — gated by ``money_tax``."""
    from istota.money.core.ledger import lots as ledger_lots
    _output(ledger_lots(resolve_ledger(ledger, ctx.ledgers), symbol))


@cli.command("wash-sales")
@click.option("--year", "-y", type=int, help="Year to analyze")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
@requires_feature("money_wash_sales")
def wash_sales(ctx, year, ledger):
    """Detect wash sale violations. Experimental — gated by ``money_wash_sales``."""
    from istota.money.core.ledger import wash_sales as ledger_wash_sales
    _output(ledger_wash_sales(resolve_ledger(ledger, ctx.ledgers), year))


# =============================================================================
# Transaction commands
# =============================================================================


@cli.command("backfill-ids")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def backfill_ids(ctx, ledger):
    """Stamp a stable ``id:`` on every transaction lacking one.

    One-time, idempotent migration that makes transactions editable. Backs up
    each touched file, validates with ``bean-check``, and rolls back on
    failure.
    """
    from istota.money.core.edit import backfill_ledger_ids
    _output(backfill_ledger_ids(resolve_ledger(ledger, ctx.ledgers)))


@cli.command("add-transaction")
@click.option("--date", "-d", "txn_date", required=True, help="Transaction date (YYYY-MM-DD)")
@click.option("--payee", "-p", required=True, help="Payee name")
@click.option("--narration", "-n", required=True, help="Transaction description")
@click.option("--debit", required=True, help="Debit account")
@click.option("--credit", required=True, help="Credit account")
@click.option("--amount", "-a", required=True, type=float, help="Transaction amount")
@click.option("--currency", default="USD", help="Currency")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def add_transaction(ctx, txn_date, payee, narration, debit, credit, amount, currency, ledger):
    """Add a transaction to the ledger."""
    from istota.money.core.transactions import add_transaction as core_add
    try:
        parsed_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return
    _output(core_add(
        resolve_ledger(ledger, ctx.ledgers),
        parsed_date, payee, narration, debit, credit, amount, currency,
    ))


@cli.command("edit-transaction")
@click.option("--id", "txn_id", required=True, help="Stable transaction id")
@click.option("--old-account", help="Account of the posting to edit (disambiguator)")
@click.option("--old-position", help="Amount of the posting to edit (disambiguator)")
@click.option("--date", "-d", "new_date", help="New date (YYYY-MM-DD)")
@click.option("--payee", "-p", "new_payee", help="New payee")
@click.option("--narration", "-n", "new_narration", help="New narration")
@click.option("--account", "-a", "new_account", help="New posting account")
@click.option("--position", "new_position", help="New posting amount (e.g. '-12.50 USD')")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def edit_transaction(
    ctx, txn_id, old_account, old_position,
    new_date, new_payee, new_narration, new_account, new_position, ledger,
):
    """Edit a transaction located by its stable ``id:`` metadata.

    Validates the result with ``bean-check`` and rolls back on failure (e.g.
    an amount edit that unbalances the entry).
    """
    from istota.money.core.edit import edit_transaction as core_edit

    if new_date:
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
        except ValueError:
            _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
            return

    _output(core_edit(
        resolve_ledger(ledger, ctx.ledgers),
        txn_id,
        old_account=old_account,
        old_position=old_position,
        new_date=new_date,
        new_payee=new_payee,
        new_narration=new_narration,
        new_account=new_account,
        new_position=new_position,
    ))


@cli.command("import-csv")
@click.argument("file", type=click.Path(exists=True))
@click.option("--account", "-a", required=True, help="Bank/credit card account")
@click.option("--tag", "-t", multiple=True, help="Only include transactions with this tag")
@click.option("--exclude-tag", "-x", multiple=True, help="Exclude transactions with this tag")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def import_csv(ctx, file, account, tag, exclude_tag, ledger):
    """Import transactions from Monarch Money CSV export."""
    from istota.money.core.transactions import import_csv as core_import
    db_conn = _get_db_conn(ctx)
    try:
        _output(core_import(
            ledger_path=resolve_ledger(ledger, ctx.ledgers),
            file_path=Path(file), account=account, db_conn=db_conn,
            include_tags=list(tag) if tag else None,
            exclude_tags=list(exclude_tag) if exclude_tag else None,
            db_path=ctx.db_path,
            ledger_name=_ledger_scope_name(ledger, ctx.ledgers),
        ))
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()


def _run_monarch_sync(
    ctx,
    dry_run: bool,
    ledger: str | None,
    *,
    match_invoices: bool = True,
    tolerance: float = 0.0,
) -> dict:
    """Sync from Monarch, then settle the invoices the new credits pay for.

    Used by the ``sync-monarch`` command and folded into ``run-scheduled``
    so periodic syncs happen as part of the daily run.

    Matching (ISSUE-083) runs after the ledger write and never on a dry run.
    It is reported under ``invoice_matching`` on whichever result carries the
    transactions — the flat result, or each profile's.
    """
    result = _sync_monarch_ledgers(ctx, dry_run, ledger)
    if match_invoices and not dry_run:
        _apply_invoice_matching(ctx, result, tolerance)
    # ``imported`` is plumbing for the matcher, not output. Every other
    # row-level detail in this result is a count, and this one would put a
    # record per booked transaction into a skill response that goes straight
    # into a prompt — hundreds of rows on a first 30-day sync.
    for sync_result in _sync_results(result):
        sync_result.pop("imported", None)
    return result


def _sync_monarch_ledgers(ctx, dry_run: bool, ledger: str | None) -> dict:
    """Run the Monarch sync itself, across profiles or a single ledger."""
    from istota.money import config_store
    from istota.money.core.transactions import (
        annotate_rule_drops,
        load_import_rules,
        load_import_rules_for_ledgers,
        sync_all_profiles,
        sync_monarch as core_sync,
    )
    if ctx.db_path is None or not config_store.has_monarch_data(ctx.db_path):
        return {
            "status": "error",
            "error": "No monarch config in the DB for this user. Seed it via "
                     "`istota money monarch …` or `istota money config import`.",
        }
    config = config_store.load_monarch(ctx.db_path, secrets=ctx.secrets)
    db_conn = _get_db_conn(ctx)
    try:
        if ledger:
            # Specific ledger: find matching profile(s) or use flat config
            ledger_path = resolve_ledger(ledger, ctx.ledgers)
            matching = [p for p in config.profiles if p.ledger.lower() == ledger.lower()]
            if matching:
                # Sync only the matching profile(s)
                import asyncio
                from istota.money.core.transactions import fetch_monarch_transactions
                lookback = max(p.sync.lookback_days for p in matching)
                txns = asyncio.run(fetch_monarch_transactions(config, lookback))
                from istota.money.core.models import MonarchConfig as MC
                # Ahead of the loop: see load_import_rules_for_ledgers.
                rules_by_ledger = load_import_rules_for_ledgers(
                    ctx.db_path, [p.ledger for p in matching], "monarch-api",
                )
                results = []
                for profile in matching:
                    profile_config = MC(
                        credentials=config.credentials,
                        sync=profile.sync,
                        accounts=profile.accounts,
                        categories=profile.categories,
                        tags=profile.tags,
                    )
                    rules, dropped = rules_by_ledger[profile.ledger]
                    r = annotate_rule_drops(
                        core_sync(
                            ledger_path, profile_config, db_conn=db_conn,
                            dry_run=dry_run, transactions=txns,
                            profile=profile.name, rules=rules,
                        ),
                        dropped,
                    )
                    r["name"] = profile.name
                    r["ledger"] = profile.ledger
                    results.append(r)
                return {"status": "ok", "profiles": results}
            else:
                # A ledger no profile claims. On a deployment that has
                # profiles this is the only way such a ledger is ever synced,
                # which is why the migrated global rules cannot be dropped:
                # they are the whole rule set this branch sees.
                rules, dropped = load_import_rules(
                    ctx.db_path, _ledger_scope_name(ledger, ctx.ledgers),
                    "monarch-api",
                )
                return annotate_rule_drops(
                    core_sync(
                        ledger_path, config, db_conn=db_conn, dry_run=dry_run,
                        rules=rules,
                    ),
                    dropped,
                )
        else:
            return sync_all_profiles(
                config, ctx.ledgers, db_conn=db_conn, dry_run=dry_run,
                db_path=ctx.db_path,
            )
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()


def _sync_results(result: dict) -> list[dict]:
    """The per-ledger sync results inside a ``_sync_monarch_ledgers`` return.

    Three shapes come back from that function: a flat ``sync_monarch`` result,
    and two ``{"profiles": [...]}`` envelopes. Normalising here keeps the
    matcher from caring which one it got.
    """
    if result.get("status") != "ok":
        return []
    profiles = result.get("profiles")
    if isinstance(profiles, list):
        return [p for p in profiles if isinstance(p, dict) and p.get("status") == "ok"]
    return [result]


def _tolerance_error(tolerance: float) -> str | None:
    """Reject a `--tolerance` the matcher can't use, before anything runs.

    Click's `type=float` accepts `nan` and `inf`, and both slip past a plain
    `< 0` test — NaN because every comparison with it is False. Caught here so
    the failure is a JSON error envelope rather than a traceback out of a sync
    that has already written the ledger.
    """
    import math

    if math.isnan(tolerance):
        return "--tolerance must be a number, got nan"
    if math.isinf(tolerance):
        return "--tolerance must be finite"
    if tolerance < 0:
        return "--tolerance must not be negative"
    return None


def _open_invoices(config, data_dir: Path) -> list:
    """Wholly unpaid invoices whose total can be stated exactly.

    Three kinds of invoice are deliberately left out, because matching on a
    total that isn't the amount the client owes is how a wrong invoice gets
    settled:

    * **Partly paid** — some entries carry a `paid_date` and some don't. The
      total of all its entries is no longer the outstanding balance.
    * **Partly unrecognised** — `build_line_items` silently skips an entry
      whose service is missing from the config, so the total would be less
      than what was billed and a smaller unrelated credit could match it.
    * Fully paid, or with no billable lines at all.

    ``date`` is the invoice's issue date where one was recorded, and a lower
    bound on it otherwise — see :func:`work.invoice_issue_date`. Invoices
    raised before the issue date was stored fall back to the latest work
    billed, which never rejects a real payment but admits credits from the gap
    between the last work and the actual issue. For those, amount uniqueness
    rather than the date is what keeps a match honest.
    """
    from istota.money.core.invoice_matching import OpenInvoice
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import (
        get_invoice_numbers, get_entries_for_invoice, invoice_issue_date,
    )

    invoices = []
    for number in get_invoice_numbers(data_dir):
        entries = get_entries_for_invoice(data_dir, number)
        if not entries or any(e.paid_date is not None for e in entries):
            continue
        items = build_line_items(entries, config.services)
        if not items or len(items) != len(entries):
            continue
        invoices.append(OpenInvoice(
            number=number,
            client=entries[0].client,
            date=invoice_issue_date(entries),
            total=sum(item.amount for item in items),
        ))
    return invoices


def _apply_invoice_matching(ctx, result: dict, tolerance: float) -> None:
    """Settle the invoices this sync's credits pay for, in place on ``result``.

    Best-effort, and the whole body is guarded to make that true. By the time
    this runs the ledger has already been appended to, the staging file
    written and the dedup rows committed; raising here would report a failure
    for work that succeeded, and on the ``run-scheduled`` cron path would also
    stop the invoice generation that follows. A malformed work-entry TOML must
    not be able to break a bank sync. Everything is logged and skipped.
    """
    try:
        _match_invoices_unguarded(ctx, result, tolerance)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("istota.money.cli").warning(
            "auto-match: skipped after a successful sync: %s", exc, exc_info=True,
        )


def _match_invoices_unguarded(ctx, result: dict, tolerance: float) -> None:
    """The body of :func:`_apply_invoice_matching`. Never call directly."""
    from istota.money.core.invoice_matching import (
        Payment, match_payments_to_invoices, summarize_matches,
    )
    from istota.money.work import record_invoice_payment

    log = logging.getLogger("istota.money.cli")

    sync_results = _sync_results(result)
    if not any(r.get("imported") for r in sync_results):
        return
    if ctx.data_dir is None:
        return

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException:
        return  # invoicing isn't configured for this user; nothing to match

    open_invoices = _open_invoices(config, ctx.data_dir)
    if not open_invoices:
        return

    # Every profile's credits are matched in one pass, not per profile.
    # ``sync_all_profiles`` fetches from Monarch once and hands the same
    # transaction list to each profile, and dedup is per profile, so one
    # credit can legitimately land in two profiles' ``imported`` lists.
    # Matching per profile would let the first one settle an invoice and
    # leave the second silently unreported — and which one won would depend
    # on profile order. One pass makes the whole run's contention visible.
    owner_of_payment: list[int] = []  # index into sync_results, per payment
    payments: list = []
    for index, sync_result in enumerate(sync_results):
        for row in sync_result.get("imported") or []:
            try:
                paid_on = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except (KeyError, TypeError, ValueError):
                # Can't happen today — `imported` always carries an ISO date
                # from a `date`. Logged rather than passed over silently
                # because if the shape of `imported` ever changes, the failure
                # mode is every credit becoming invisible to the matcher.
                log.warning("auto-match: unusable date on an imported row: %r",
                            row.get("date"))
                continue
            owner_of_payment.append(index)
            payments.append(Payment(
                date=paid_on, amount=row.get("amount") or 0.0,
                payee=row.get("payee", ""),
            ))

    matches = match_payments_to_invoices(payments, open_invoices, tolerance)
    for match in matches:
        if match.status != "matched" or not match.invoice_number:
            continue
        try:
            stamped = record_invoice_payment(
                ctx.data_dir, match.invoice_number, match.payment.date,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-match: could not mark %s paid: %s",
                        match.invoice_number, exc)
            match.status = "review"
            match.note = f"could not record payment: {exc}"
            match.invoice_number = None
            continue

        # Open invoices were read without the work lock, so a web-UI payment
        # or a void can land in the gap before this write. `record_invoice_
        # payment` only touches entries that are still unpaid, so a zero count
        # means someone else got there first — reporting it as settled by this
        # credit would be a plain lie about where the money went.
        if stamped == 0:
            log.warning("auto-match: %s was already settled or voided "
                        "before this run could stamp it", match.invoice_number)
            match.status = "review"
            match.note = "already settled or voided by something else mid-sync"
            match.invoice_number = None

    # The matcher returns one verdict per payment, in order, so verdicts pair
    # with `owner_of_payment` by position and each profile reports only its
    # own credits.
    matches_per_owner: list[list] = [[] for _ in sync_results]
    for owner_index, match in zip(owner_of_payment, matches):
        matches_per_owner[owner_index].append(match)

    for sync_result, owned in zip(sync_results, matches_per_owner):
        summary = summarize_matches(owned, open_invoices)
        if summary:
            summary["tolerance"] = tolerance
            sync_result["invoice_matching"] = summary


@cli.command("sync-monarch")
@click.option("--dry-run", is_flag=True, help="Preview without writing")
@click.option("--ledger", "-l", help="Ledger name")
@click.option("--match-invoices/--no-match-invoices", default=True,
              help="Mark an open invoice paid when a synced credit uniquely fits it")
@click.option("--tolerance", type=float, default=0.0, show_default=True,
              help="Dollar slack allowed between a credit and an invoice total")
@pass_ctx
def sync_monarch(ctx, dry_run, ledger, match_invoices, tolerance):
    """Sync transactions from Monarch Money API.

    Without --ledger, syncs all configured profiles (or the default ledger
    if no profiles are defined). With --ledger, syncs only profiles targeting
    that ledger.

    A credit that uniquely fits one open invoice marks it paid without
    posting to the ledger — the sync already booked the income. Two invoices
    that fit, or two credits that fit one invoice, are reported under
    ``invoice_matching.review`` instead of guessed at.
    """
    error = _tolerance_error(tolerance)
    if error:
        _output({"status": "error", "error": error})
        return
    _output(_run_monarch_sync(
        ctx, dry_run, ledger,
        match_invoices=match_invoices, tolerance=tolerance,
    ))


@cli.command("debug-monarch")
@pass_ctx
def debug_monarch(ctx):
    """Health-check Monarch credentials.

    Calls the cheapest possible GraphQL query (``me { id email }``) so
    operators / heartbeats can quickly tell whether the stored cookies
    still authenticate. Output is a JSON envelope:

    - ``{"status":"ok", "auth_ok":true, "who":{...}}`` on success
    - ``{"status":"error", "auth_ok":false, "error":"..."}`` on rejection
      or missing creds.
    """
    import asyncio

    from istota.money import config_store
    from istota.money._vendor.monarch_client import (
        MonarchAuthError, MonarchClient, MonarchCookieAuth,
    )

    if ctx.db_path is None or not config_store.has_monarch_data(ctx.db_path):
        _output({
            "status": "error", "auth_ok": False,
            "error": "No monarch config in the DB for this user",
        })
        return
    config = config_store.load_monarch(ctx.db_path, secrets=ctx.secrets)

    creds = config.credentials
    if not (creds.session_id and creds.csrftoken):
        _output({
            "status": "error", "auth_ok": False,
            "error": "Missing session_id and/or csrftoken cookies. "
                     "Set them via the money settings page.",
        })
        return

    async def _probe() -> dict:
        client = MonarchClient(MonarchCookieAuth(
            session_id=creds.session_id, csrftoken=creds.csrftoken,
        ))
        return await client.whoami()

    try:
        who = asyncio.run(_probe())
    except MonarchAuthError as exc:
        _output({"status": "error", "auth_ok": False, "error": str(exc)})
        return
    except Exception as exc:  # noqa: BLE001
        _output({"status": "error", "auth_ok": False, "error": str(exc)})
        return
    _output({"status": "ok", "auth_ok": True, "who": who})


# =============================================================================
# Invoice commands
# =============================================================================


@cli.group()
def invoice():
    """Invoice management."""
    pass


@invoice.command("generate")
@click.option("--period", "-p", help="Billing period upper bound (YYYY-MM)")
@click.option("--client", "-c", help="Filter by client key")
@click.option("--entity", "-e", help="Filter by entity key")
@click.option("--dry-run", is_flag=True, help="Preview without generating files")
@pass_ctx
def invoice_generate(ctx, period, client, entity, dry_run):
    """Generate invoices for uninvoiced work entries."""
    from istota.money.core.invoicing import generate_invoices_for_period
    from istota.money.db import set_invoice_schedule_generation

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
    try:
        results = generate_invoices_for_period(
            config=config, config_path=ctx.invoicing_config_path,
            accounting_path=accounting_path, data_dir=data_dir,
            period=period, client_filter=client,
            entity_filter=entity, dry_run=dry_run,
            invoice_output_dir=invoice_output_dir,
            db_path=ctx.db_path,
        )
        if not dry_run:
            scheduled_clients = {
                result["client_key"]
                for result in results
                if config.clients[result["client_key"]].schedule == "monthly"
            }
            if scheduled_clients:
                db_conn = _require_db(ctx)
                try:
                    for client_key in scheduled_clients:
                        set_invoice_schedule_generation(db_conn, client_key)
                    db_conn.commit()
                finally:
                    db_conn.close()
    except Exception as e:
        _output({"status": "error", "error": str(e)})
        return

    if not results:
        period_desc = f" for period {period}" if period else ""
        _output({"status": "ok", "message": f"No uninvoiced entries found{period_desc}", "invoices": []})
        return

    total = sum(r["total"] for r in results)
    result = {
        "status": "ok",
        "invoice_count": len(results),
        "total": round(total, 2),
        "dry_run": dry_run,
        "invoices": results,
    }
    if period:
        result["period"] = period
    for invoice_result in results:
        invoice_result.pop("client_key", None)
    _output(result)


@invoice.command("list")
@click.option("--client", "-c", help="Filter by client")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all invoices (including paid)")
@pass_ctx
def invoice_list(ctx, client, show_all):
    """List invoices (outstanding by default)."""
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import (
        get_invoice_numbers, get_entries_for_invoice, invoice_issue_date,
    )

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
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
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date_val:
            invoice_info["paid_date"] = paid_date_val.isoformat()
        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]
    _output({
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    })


@invoice.command("paid")
@click.argument("invoice_number")
@click.option("--date", "-d", "payment_date", required=True, help="Payment date (YYYY-MM-DD)")
@click.option("--bank", "-b", help="Bank account")
@click.option("--no-post", is_flag=True, help="Skip ledger posting")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def invoice_paid(ctx, invoice_number, payment_date, bank, no_post, ledger):
    """Record payment for an invoice."""
    from istota.money.core.invoicing import (
        compute_income_lines, create_income_posting,
        resolve_bank_account, resolve_currency, resolve_entity,
    )
    from istota.money.core.transactions import append_to_ledger
    from istota.money.core.ledger import run_bean_check
    from istota.money.work import get_entries_for_invoice, record_invoice_payment

    try:
        parsed_date = datetime.strptime(payment_date, "%Y-%m-%d").date()
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        _output({"status": "error", "error": f"Invoice {invoice_number} not found"})
        return

    if all(e.paid_date is not None for e in entries):
        _output({"status": "error", "error": f"Invoice {invoice_number} is already paid"})
        return

    first_entry = entries[0]
    client_config = config.clients.get(first_entry.client)
    if not client_config:
        _output({"status": "error", "error": f"Client '{first_entry.client}' not found in config"})
        return

    entity = resolve_entity(config, entry=first_entry, client_config=client_config)
    bank_account = bank or resolve_bank_account(entity, config)
    currency = resolve_currency(entity, config)

    income_lines = compute_income_lines(entries, config.services)
    if not income_lines:
        _output({"status": "error", "error": f"No billable items found for {invoice_number}"})
        return

    total = sum(income_lines.values())
    ledger_path = None

    should_post = not no_post and client_config.ledger_posting
    if should_post:
        posting = create_income_posting(
            invoice_number=invoice_number, client_name=client_config.name,
            income_lines=income_lines, payment_date=parsed_date,
            bank_account=bank_account, currency=currency,
        )
        ledger_path = resolve_ledger(ledger, ctx.ledgers)
        append_to_ledger(ledger_path, [posting])

        success, errors = run_bean_check(ledger_path)
        if not success:
            _output({
                "status": "error",
                "error": "Payment recorded but ledger validation failed",
                "validation_errors": errors[:5],
                "file": str(ledger_path),
            })
            return

    record_invoice_payment(data_dir, invoice_number, parsed_date.isoformat())

    result = {
        "status": "ok",
        "invoice_number": invoice_number,
        "client": client_config.name,
        "amount": round(total, 2),
        "payment_date": parsed_date.isoformat(),
        "bank_account": bank_account,
    }
    if ledger_path:
        result["file"] = str(ledger_path)
    if not should_post:
        result["no_post"] = True
    _output(result)


@invoice.command("create")
@click.argument("client_key")
@click.option("--service", "-s", help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity")
@click.option("--description", help="Line item description")
@click.option("--item", multiple=True, help='Manual item: "description" amount')
@click.option("--entity", "-e", help="Entity key")
@pass_ctx
def invoice_create(ctx, client_key, service, qty, description, item, entity):
    """Create a manual single invoice.

    Creates work entries with the invoice number pre-assigned and
    generates a PDF. The invoice is visible to 'invoice list' and 'invoice paid'.
    """
    from istota.money.core.invoicing import (
        generate_invoice_html, generate_invoice_pdf,
        format_invoice_number, highest_existing_invoice_number,
        persist_next_invoice_number,
        resolve_entity as resolve_entity_fn, build_line_items,
    )
    from istota.money.core.models import InvoiceLineItem, Invoice
    from istota.money.work import add_work_entry, get_entries_for_invoice

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    client_config = config.clients.get(client_key)
    if not client_config:
        available = list(config.clients.keys())
        _output({"status": "error", "error": f"Client '{client_key}' not found. Available: {', '.join(available)}"})
        return

    if entity:
        if entity not in config.companies:
            available = list(config.companies.keys())
            _output({"status": "error", "error": f"Entity '{entity}' not found. Available: {', '.join(available)}"})
            return
        resolved_entity = config.companies[entity]
    else:
        resolved_entity = resolve_entity_fn(config, client_config=client_config)

    service_entries = []
    if service:
        if service not in config.services:
            available = list(config.services.keys())
            _output({"status": "error", "error": f"Service '{service}' not found. Available: {', '.join(available)}"})
            return
        service_entries.append((service, qty, description or "", entity or ""))

    manual_items = []
    if item:
        for item_str in item:
            parts = item_str.rsplit(" ", 1)
            if len(parts) != 2:
                _output({"status": "error", "error": f"Invalid item format: {item_str}. Use: \"description\" amount"})
                return
            desc = parts[0].strip('"').strip("'")
            try:
                amt = float(parts[1])
            except ValueError:
                _output({"status": "error", "error": f"Invalid amount in item: {parts[1]}"})
                return
            manual_items.append((desc, amt))

    if not service_entries and not manual_items:
        _output({"status": "error", "error": "No line items specified. Use --service/--qty or --item"})
        return

    data_dir = _require_data_dir(ctx)
    invoice_number = max(
        config.next_invoice_number,
        highest_existing_invoice_number(data_dir) + 1,
    )
    invoice_date = date.today()
    number_str = format_invoice_number(invoice_number)

    # Insert service-based work entries with invoice pre-assigned
    for svc_key, svc_qty, svc_desc, svc_entity in service_entries:
        add_work_entry(
            data_dir, invoice_date.isoformat(), client_key, svc_key,
            qty=svc_qty, description=svc_desc, entity=svc_entity,
            invoice=number_str, invoice_date=invoice_date,
        )

    # Insert manual items with invoice pre-assigned
    for desc, amt in manual_items:
        add_work_entry(
            data_dir, invoice_date.isoformat(), client_key, "_manual",
            amount=amt, description=desc, entity=entity or "",
            invoice=number_str, invoice_date=invoice_date,
        )

    # Build line items from the entries we just created
    inv_entries = get_entries_for_invoice(data_dir, number_str)

    items = build_line_items(
        [e for e in inv_entries if e.service != "_manual"],
        config.services,
    )
    for e in inv_entries:
        if e.service == "_manual":
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item",
                description="",
                quantity=1, rate=e.amount or 0,
                discount=0, amount=e.amount or 0,
            ))

    total = sum(i.amount for i in items)
    due_date = invoice_date + timedelta(days=client_config.terms) if isinstance(client_config.terms, int) else None

    inv = Invoice(
        number=number_str,
        date=invoice_date, due_date=due_date,
        client=client_config, company=resolved_entity,
        items=items, total=total, group_name="",
    )

    logo_path = None
    if resolved_entity.logo:
        logo_path = accounting_path / resolved_entity.logo
        if not logo_path.exists():
            logo_path = None
    html = generate_invoice_html(inv, logo_path=logo_path)
    output_dir = invoice_output_dir / str(invoice_date.year)
    pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
    pdf_path = output_dir / pdf_filename
    generate_invoice_pdf(html, pdf_path)

    persist_next_invoice_number(
        invoice_number + 1, db_path=ctx.db_path,
        config_path=ctx.invoicing_config_path,
    )

    _output({
        "status": "ok",
        "invoice_number": inv.number,
        "client": client_config.name,
        "total": round(total, 2),
        "due_date": due_date.isoformat() if due_date else str(client_config.terms),
        "file": str(pdf_path),
    })


@invoice.command("unpaid")
@click.argument("invoice_number")
@pass_ctx
def invoice_unpaid(ctx, invoice_number):
    """Reopen a paid invoice, keeping the invoice number.

    The inverse of ``invoice paid``, and the way back from an auto-match the
    sync got wrong. ``invoice void`` is not that inverse — it clears the
    invoice number too, un-invoicing the work itself.

    Any ledger posting made when the payment was recorded is left alone;
    reverse it with ``edit-transaction`` if there was one.
    """
    from istota.money.work import clear_invoice_payment, get_entries_for_invoice

    data_dir = _require_data_dir(ctx)
    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        _output({"status": "error", "error": f"Invoice {invoice_number} not found"})
        return
    if all(e.paid_date is None for e in entries):
        _output({
            "status": "error",
            "error": f"Invoice {invoice_number} is not marked paid",
        })
        return

    count = clear_invoice_payment(data_dir, invoice_number)
    _output({
        "status": "ok",
        "invoice_number": invoice_number,
        "entries_cleared": count,
    })


@invoice.command("void")
@click.argument("invoice_number")
@click.option("--force", is_flag=True, help="Void even if invoice has been paid")
@click.option("--delete-pdf", is_flag=True, help="Delete the generated PDF file")
@pass_ctx
def invoice_void(ctx, invoice_number, force, delete_pdf):
    """Void an invoice, clearing it from work entries.

    Removes the invoice number and paid_date from all associated work entries,
    cleans up DB state (overdue notifications), and optionally deletes the PDF.
    """
    from istota.money.work import get_entries_for_invoice, void_invoice

    data_dir = _require_data_dir(ctx)
    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        _output({"status": "error", "error": f"Invoice {invoice_number} not found"})
        return

    is_paid = any(e.paid_date is not None for e in entries)
    if is_paid and not force:
        _output({
            "status": "error",
            "error": f"Invoice {invoice_number} has been marked as paid. Use --force to void anyway.",
        })
        return

    count = void_invoice(data_dir, invoice_number)

    # Clean up DB state
    db_cleanup = {}
    db_conn = _get_db_conn(ctx)
    if db_conn:
        try:
            from istota.money.db import clear_invoice_state
            db_cleanup = clear_invoice_state(db_conn, invoice_number)
            db_conn.commit()
        finally:
            db_conn.close()

    # Optionally delete PDF
    pdf_deleted = False
    if delete_pdf:
        try:
            _, _, invoice_output_dir = _load_invoicing_config(ctx)
            from istota.money.core.invoicing import delete_invoice_pdf
            pdf_deleted = delete_invoice_pdf(invoice_output_dir, invoice_number)
        except click.ClickException:
            pass

    result = {
        "status": "ok",
        "invoice_number": invoice_number,
        "entries_voided": count,
        "was_paid": is_paid,
        "message": f"Voided invoice {invoice_number} ({count} work entries cleared)",
    }
    if db_cleanup:
        result["db_cleanup"] = db_cleanup
    if delete_pdf:
        result["pdf_deleted"] = pdf_deleted
    _output(result)


def _apply_monarch_status(out: dict, monarch_result: dict | None) -> None:
    """Roll up nested Monarch sync errors to the outer envelope.

    The scheduler's JSON-error envelope detector (see
    `scheduler._execute_command_task`) only fires on top-level
    `status == "error"`. Without this rollup a broken Monarch sync would
    nest its error under `out["monarch"]` and the task would silently
    succeed. We promote a hard error if the sync itself failed, or a
    `partial_error` if any individual profile failed.
    """
    if monarch_result is None:
        return
    if monarch_result.get("status") == "error":
        out["status"] = "error"
        out["error"] = f"monarch sync failed: {monarch_result.get('error', 'unknown error')}"
        return
    profiles = monarch_result.get("profiles") or []
    failed = [p for p in profiles if isinstance(p, dict) and p.get("status") == "error"]
    if failed:
        out["status"] = "partial_error"
        names = ", ".join(p.get("name") or p.get("ledger") or "?" for p in failed)
        out["monarch_errors"] = f"{len(failed)} profile(s) failed: {names}"


@cli.command("run-scheduled")
@click.option("--dry-run", is_flag=True, help="Preview without generating files")
@click.option("--skip-monarch", is_flag=True, help="Skip the monarch sync step")
@click.option("--match-invoices/--no-match-invoices", default=True,
              help="Mark an open invoice paid when a synced credit uniquely fits it")
@click.option("--tolerance", type=float, default=0.0, show_default=True,
              help="Dollar slack allowed between a credit and an invoice total")
@pass_ctx
def run_scheduled(ctx, dry_run, skip_monarch, match_invoices, tolerance):
    """Run periodic money tasks: monarch sync (if configured) + invoice schedule check.

    Meant to be called periodically by cron. The monarch sync runs first
    when ``monarch_config`` is set; the invoice scheduler then checks each
    client's invoicing schedule and generates invoices when due. Either
    half is optional — users with only one feature configured get only
    that step.

    This is the unattended path, so it carries the same auto-matching
    controls as ``sync-monarch``: ``--no-match-invoices`` turns off marking
    invoices paid without also skipping the ledger sync, which is what
    ``--skip-monarch`` would do.
    """
    from istota.money.core.invoicing import check_scheduled_invoices, generate_invoices_for_period
    from istota.money.db import set_invoice_schedule_generation

    error = _tolerance_error(tolerance)
    if error:
        _output({"status": "error", "error": error})
        return

    monarch_result: dict | None = None
    if ctx.monarch_config_path and not skip_monarch:
        monarch_result = _run_monarch_sync(
            ctx, dry_run=dry_run, ledger=None,
            match_invoices=match_invoices, tolerance=tolerance,
        )

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException:
        # No invoicing config — return whatever monarch did (if anything)
        out = {"status": "ok", "message": "No invoicing config; nothing to schedule"}
        if monarch_result is not None:
            out["monarch"] = monarch_result
        _apply_monarch_status(out, monarch_result)
        _output(out)
        return

    data_dir = _require_data_dir(ctx)
    db_conn = _require_db(ctx)

    try:
        due_clients = check_scheduled_invoices(
            config, db_conn, data_dir=data_dir,
        )
        if not due_clients:
            out = {
                "status": "ok",
                "message": "No scheduled invoices due",
                "clients_checked": len(
                    [c for c in config.clients.values() if c.schedule == "monthly"]
                ),
            }
            if monarch_result is not None:
                out["monarch"] = monarch_result
            _apply_monarch_status(out, monarch_result)
            _output(out)
            return

        all_results = []
        for client_key in due_clients:
            results = generate_invoices_for_period(
                config=config, config_path=ctx.invoicing_config_path,
                accounting_path=accounting_path, data_dir=data_dir,
                client_filter=client_key, dry_run=dry_run,
                invoice_output_dir=invoice_output_dir,
                db_path=ctx.db_path,
            )
            if results and not dry_run:
                set_invoice_schedule_generation(db_conn, client_key)
            all_results.extend(results)

        db_conn.commit()

        total = sum(r["total"] for r in all_results)
        out = {
            "status": "ok",
            "dry_run": dry_run,
            "clients_due": due_clients,
            "invoice_count": len(all_results),
            "total": round(total, 2),
            "invoices": all_results,
        }
        if monarch_result is not None:
            out["monarch"] = monarch_result
        _apply_monarch_status(out, monarch_result)
        _output(out)
    finally:
        db_conn.commit()
        db_conn.close()


# =============================================================================
# Portfolio commands (positions snapshots)
# =============================================================================


@cli.group("portfolio")
def portfolio_group():
    """Portfolio positions snapshots (Fidelity CSV imports)."""


@portfolio_group.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--source", "-s", "source_name",
              help="Import source name (auto-detected when omitted)")
@click.option("--dry-run", is_flag=True, help="Parse and preview without writing")
@click.option("--replace", "replace_id", type=int,
              help="Delete this snapshot id before importing (same-day replace)")
@pass_ctx
def portfolio_import(ctx, file, source_name, dry_run, replace_id):
    """Import a positions CSV (Fidelity export or fina history file)."""
    from istota.money import portfolio
    from istota.money.core.importers import parse_positions_file
    from istota.money.core.importers.positions_base import PositionParseError

    try:
        snapshots = parse_positions_file(Path(file), source_name)
    except PositionParseError as exc:
        _output({"status": "error", "error": str(exc)})
        return

    if dry_run:
        _output({
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
        })
        return

    from istota.money import portfolio_autoclass

    conn = _require_db(ctx)
    try:
        if replace_id is not None:
            portfolio.delete_snapshot(conn, replace_id)
        results = [
            portfolio.insert_snapshot(conn, s, source_file=Path(file).name)
            for s in snapshots
        ]
        # Auto-classify new symbols (ticker lookup, then description
        # heuristics). One pass across every snapshot the file produced, so
        # the lookup budget is spent once per import rather than once per
        # export date. Fail-soft — a lookup outage leaves symbols reported in
        # unclassified_symbols and never fails an import that has committed.
        for r in results:
            if r["status"] == "ok":
                r.setdefault("auto_classified", [])
        if any(
            r["status"] == "ok" and r.get("unclassified_symbols") for r in results
        ):
            try:
                auto = portfolio_autoclass.auto_classify_snapshots(
                    conn, snapshots,
                    allow_lookups=_autoclass_lookup_enabled(ctx),
                )
                portfolio_autoclass.apply_auto_results(results, auto)
            except Exception:
                logging.getLogger("istota.money.cli").warning(
                    "portfolio auto-classification failed", exc_info=True,
                )
    finally:
        conn.close()

    if len(results) == 1:
        _output(results[0])
    else:
        _output({
            "status": "ok",
            "imported": sum(1 for r in results if r["status"] == "ok"),
            "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
            # Hoisted: anything living only inside a per-snapshot result is
            # invisible to a caller reading the top level.
            **portfolio_autoclass.summarize_auto_results(results),
            "results": results,
        })


@portfolio_group.command("snapshots")
@pass_ctx
def portfolio_snapshots(ctx):
    """List imported snapshots (read-time totals over non-excluded accounts)."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        _output({"status": "ok", "snapshots": portfolio.list_snapshots(conn)})
    finally:
        conn.close()


@portfolio_group.command("summary")
@click.option("--snapshot", "snapshot_id", type=int, help="Snapshot id (default: latest)")
@click.option("--group", "-g", help="Filter by account group")
@pass_ctx
def portfolio_summary(ctx, snapshot_id, group):
    """Current-state summary: totals, allocation, aggregated holdings."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        if snapshot_id is None:
            snaps = portfolio.list_snapshots(conn)
            if not snaps:
                _output({"status": "error", "error": "No snapshots imported yet"})
                return
            snapshot_id = snaps[0]["id"]
        summary = portfolio.snapshot_summary(conn, snapshot_id, group=group)
        if summary is None:
            _output({"status": "error", "error": f"No snapshot with id {snapshot_id}"})
            return
        _output({"status": "ok", "summary": summary})
    finally:
        conn.close()


@portfolio_group.command("history")
@click.option("--group-by", "group_by",
              type=click.Choice(["total", "group", "account_type", "asset_class"]),
              default="total", help="Stack the series by this dimension")
@click.option("--group", "-g", help="Filter by account group")
@pass_ctx
def portfolio_history(ctx, group_by, group):
    """Per-snapshot value totals over time."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        result = portfolio.history_series(conn, group_by=group_by, group=group)
        _output({"status": "ok", **result})
    finally:
        conn.close()


@portfolio_group.command("diff")
@click.argument("older", type=int)
@click.argument("newer", type=int)
@pass_ctx
def portfolio_diff(ctx, older, newer):
    """Positions opened/closed/changed between two snapshots."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        diff = portfolio.snapshot_diff(conn, older, newer)
        if diff is None:
            _output({"status": "error", "error": "One or both snapshot ids not found"})
            return
        _output({"status": "ok", "diff": diff})
    finally:
        conn.close()


@portfolio_group.command("symbol")
@click.argument("symbol")
@pass_ctx
def portfolio_symbol(ctx, symbol):
    """Quantity/price/value per snapshot for one symbol."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        _output({"status": "ok", "history": portfolio.symbol_history(conn, symbol)})
    finally:
        conn.close()


@portfolio_group.command("delete-snapshot")
@click.argument("snapshot_id", type=int)
@click.option("--confirmed", is_flag=True, help="Required: this is a hard delete")
@pass_ctx
def portfolio_delete_snapshot(ctx, snapshot_id, confirmed):
    """Hard-delete one snapshot and its position rows."""
    from istota.money import portfolio

    if not confirmed:
        _output({
            "status": "error",
            "error": "Deleting a snapshot is irreversible; re-run with --confirmed",
        })
        return
    conn = _require_db(ctx)
    try:
        if portfolio.delete_snapshot(conn, snapshot_id):
            _output({"status": "ok", "deleted": snapshot_id})
        else:
            _output({"status": "error", "error": f"No snapshot with id {snapshot_id}"})
    finally:
        conn.close()


@portfolio_group.command("accounts")
@click.option("--set-group", "set_group", type=(int, str), default=None,
              help="Set an account's group label: ID GROUP (an owner, a purpose — any grouping)")
@click.option("--set-type", "set_type", type=(int, str), default=None,
              help="Set an account's type: ID TYPE (retirement/trading/cash/taxable or free text)")
@click.option("--exclude", "exclude_id", type=int, default=None,
              help="Exclude an account from all summaries and charts")
@click.option("--include", "include_id", type=int, default=None,
              help="Re-include a previously excluded account")
@pass_ctx
def portfolio_accounts(ctx, set_group, set_type, exclude_id, include_id):
    """List the account registry, or update one row via the options."""
    from dataclasses import asdict

    from istota.money import portfolio

    mutations: list[tuple[int, dict]] = []
    if set_group:
        mutations.append((set_group[0], {"group": set_group[1]}))
    if set_type:
        mutations.append((set_type[0], {"account_type": set_type[1]}))
    if exclude_id is not None:
        mutations.append((exclude_id, {"excluded": True}))
    if include_id is not None:
        mutations.append((include_id, {"excluded": False}))

    conn = _require_db(ctx)
    try:
        for account_id, kwargs in mutations:
            if not portfolio.update_account(conn, account_id, **kwargs):
                _output({"status": "error", "error": f"No account with id {account_id}"})
                return
        _output({
            "status": "ok",
            "updated": bool(mutations),
            "accounts": [asdict(a) for a in portfolio.list_accounts(conn)],
        })
    finally:
        conn.close()


@portfolio_group.command("classifications")
@pass_ctx
def portfolio_classifications(ctx):
    """List explicit symbol classifications (the seeded map plus user edits)."""
    from dataclasses import asdict

    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        _output({
            "status": "ok",
            "classifications": [
                asdict(c) for c in portfolio.list_classifications(conn)
            ],
        })
    finally:
        conn.close()


@portfolio_group.command("classify")
@click.argument("symbol")
@click.option("--asset-class", "asset_class", required=True)
@click.option("--sub-class", "sub_class", default="")
@click.option("--geography", default="")
@pass_ctx
def portfolio_classify(ctx, symbol, asset_class, sub_class, geography):
    """Set a symbol's classification (applies to all history at read time)."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        try:
            norm = portfolio.set_classification(
                conn, symbol,
                asset_class=asset_class, sub_class=sub_class, geography=geography,
            )
        except ValueError as exc:
            _output({"status": "error", "error": str(exc)})
            return
        _output({"status": "ok", "symbol": norm})
    finally:
        conn.close()


@portfolio_group.command("autoclass")
@pass_ctx
def portfolio_autoclass_cmd(ctx):
    """Auto-classify every imported symbol that resolves to Unclassified.

    Ticker metadata lookup first, offline description heuristics second;
    writes source='auto' rows and never touches an existing classification.
    """
    from istota.money import portfolio_autoclass

    conn = _require_db(ctx)
    try:
        candidates = portfolio_autoclass.candidates_from_positions(conn)
        result = portfolio_autoclass.auto_classify_symbols(
            conn, candidates, allow_lookups=_autoclass_lookup_enabled(ctx),
        )
    finally:
        conn.close()
    _output({"status": "ok", **result})


@portfolio_group.command("unclassify")
@click.argument("symbol")
@pass_ctx
def portfolio_unclassify(ctx, symbol):
    """Remove a symbol's explicit classification (fallback rules apply again)."""
    from istota.money import portfolio

    conn = _require_db(ctx)
    try:
        if portfolio.delete_classification(conn, symbol):
            _output({"status": "ok", "symbol": portfolio.normalize_symbol(symbol)})
        else:
            _output({
                "status": "error",
                "error": f"No classification for {portfolio.normalize_symbol(symbol)}",
            })
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
