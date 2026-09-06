"""Money accounting operations -- in-process facade.

Invokes the in-tree ``istota.money`` Click CLI in-process. The user's
:class:`UserContext` is resolved up front via :func:`istota.money.resolve_for_user`
against the istota config and injected into Click via ``obj=``. No env-var
marshaling, no subprocess, no HTTP.

Two shapes, and which one a verb takes follows the split the two CLIs already
keep. An accounting operation goes through the Click tree via :func:`_run`,
which is every verb here bar two. A verb reading or writing the money *config*
calls :mod:`istota.money.config_store` directly against ``ctx.db_path``, the
way ``istota/cli_money.py`` does, because the Click tree exposes no config
commands. Both resolve the user through :func:`_resolve_context`.
"""

import argparse
import json
import os
import sys


def _unwrap_inner_error(raw: str) -> str:
    """If ``raw`` is itself an ``{"status":"error","error":"..."}``
    envelope, return the inner ``error`` string. Otherwise return
    ``raw`` unchanged. Keeps user-visible messages from gated subcommands
    readable instead of becoming escaped JSON nested inside the facade's
    outer envelope.
    """
    if not raw.startswith("{"):
        return raw
    try:
        inner = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if (
        isinstance(inner, dict)
        and inner.get("status") == "error"
        and isinstance(inner.get("error"), str)
    ):
        return inner["error"]
    return raw


def _resolve_context():
    """Resolve this task's money UserContext.

    Returns ``(user_id, istota_config, user_ctx, None)``, or
    ``(None, None, None, error_envelope)``. ``MONEY_USER`` is set by the
    framework from the task's own user_id and is the only thing that decides
    whose data is reached; no verb takes a user as an argument.
    """
    from istota.config import load_config
    from istota.money import UserNotFoundError, resolve_for_user

    user_id = os.environ.get("MONEY_USER", "") or ""
    if not user_id:
        return None, None, None, {"status": "error", "error": "MONEY_USER not set"}

    istota_cfg = load_config()
    try:
        user_ctx = resolve_for_user(user_id, istota_cfg)
    except UserNotFoundError as e:
        return None, None, None, {"status": "error", "error": str(e)}
    return user_id, istota_cfg, user_ctx, None


def _run(args: list[str]) -> dict:
    """Resolve the user's UserContext, invoke money.cli.cli, return parsed JSON."""
    from click.testing import CliRunner

    from istota.money import load_user_secrets
    from istota.money.cli import Context, cli

    user_id, istota_cfg, user_ctx, err = _resolve_context()
    if err:
        return err

    obj = Context()
    obj.users[user_id] = user_ctx
    obj.activate_user(user_id)
    obj.secrets = load_user_secrets(user_id, istota_cfg) or None

    runner = CliRunner()
    result = runner.invoke(
        cli, ["-u", user_id, *args],
        obj=obj,
        standalone_mode=False,
        catch_exceptions=True,
    )

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return {"status": "error", "error": f"{type(result.exception).__name__}: {result.exception}"}
    if result.exit_code not in (0, None):
        raw = (result.output or f"exit {result.exit_code}").strip()
        return {"status": "error", "error": _unwrap_inner_error(raw)}

    output = (result.output or "").strip()
    if not output:
        return {"status": "error", "error": "no output from money CLI"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"status": "error", "error": f"invalid JSON from CLI: {output[:200]}"}


def _output(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))
    if isinstance(data, dict) and data.get("status") == "error":
        sys.exit(1)


# ---------------------------------------------------------------------------
# Ledger commands
# ---------------------------------------------------------------------------


def cmd_list(args):
    _output(_run(["list"]))


def cmd_check(args):
    cli_args = ["check"]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_balances(args):
    cli_args = ["balances"]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    if args.account:
        cli_args += ["--account", args.account]
    _output(_run(cli_args))


def cmd_query(args):
    cli_args = ["query", args.bql]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_report(args):
    cli_args = ["report", args.report_type]
    if args.year:
        cli_args += ["--year", str(args.year)]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_lots(args):
    cli_args = ["lots", args.symbol]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_wash_sales(args):
    cli_args = ["wash-sales"]
    if args.year:
        cli_args += ["--year", str(args.year)]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Transaction commands
# ---------------------------------------------------------------------------


def cmd_backfill_ids(args):
    cli_args = ["backfill-ids"]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_add_transaction(args):
    cli_args = [
        "add-transaction",
        "--date", args.txn_date,
        "--payee", args.payee,
        "--narration", args.narration,
        "--debit", args.debit,
        "--credit", args.credit,
        "--amount", str(args.amount),
        "--currency", args.currency,
    ]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_edit_transaction(args):
    cli_args = ["edit-transaction", "--id", args.id]
    if args.old_account:
        cli_args += ["--old-account", args.old_account]
    if args.old_position:
        cli_args += ["--old-position", args.old_position]
    if args.new_date:
        cli_args += ["--date", args.new_date]
    if args.new_payee:
        cli_args += ["--payee", args.new_payee]
    if args.new_narration:
        cli_args += ["--narration", args.new_narration]
    if args.new_account:
        cli_args += ["--account", args.new_account]
    if args.new_position:
        cli_args += ["--position", args.new_position]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_sync_monarch(args):
    cli_args = ["sync-monarch"]
    if args.dry_run:
        cli_args.append("--dry-run")
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    if args.no_match_invoices:
        cli_args.append("--no-match-invoices")
    if args.tolerance is not None:
        cli_args += ["--tolerance", str(args.tolerance)]
    _output(_run(cli_args))


def cmd_debug_monarch(args):  # noqa: ARG001
    _output(_run(["debug-monarch"]))


# The category map is config, not a ledger operation, so it goes to
# config_store the way `cli_money` does rather than through the Click tree.


def _map_scope(args) -> str | None:
    """``None`` is the global map, which every profile falls back to."""
    return None if args.global_scope else args.profile


def cmd_monarch_category_map_list(args):
    _, _, ctx, err = _resolve_context()
    if err:
        _output(err)
        return
    from istota.money import config_store

    profile = _map_scope(args)
    try:
        mapping = config_store.get_category_map(ctx.db_path, profile)
    except ValueError as exc:
        _output({"status": "error", "error": str(exc)})
        return
    _output({
        "status": "ok",
        "profile": profile or "__global__",
        "mapping": mapping,
    })


def cmd_monarch_category_map_set(args):
    _, _, ctx, err = _resolve_context()
    if err:
        _output(err)
        return
    from istota.money import config_store

    profile = _map_scope(args)
    try:
        state = config_store.set_category_map_entry(
            ctx.db_path, profile, args.category, args.account,
        )
    except ValueError as exc:
        _output({"status": "error", "error": str(exc)})
        return
    _output({
        "status": "ok",
        "state": state,
        "profile": profile or "__global__",
        "category": args.category,
        "account": args.account,
    })


# ---------------------------------------------------------------------------
# Transaction rules
#
# The model-facing front end over `config_store`'s rule accessors, beside the
# six HTTP routes, the web section and `istota money rules`. Three verbs where
# the operator CLI has five: no delete, matching `monarch-category-map` — a
# rule the model can delete is a `skip` the model can quietly stop applying,
# and an operator has `istota money rules remove`.
#
# The two things this surface owes the others: the same validation, which it
# gets by calling the same accessors, and messages that never carry the user's
# `match_value` or `target`. A skill error is returned to the model verbatim
# and lands in a Talk room; `validate_rule_fields` and `_duplicate_rule_error`
# already answer with a field name and a constraint for that reason.
# ---------------------------------------------------------------------------

# The rule columns a `set` can name, in the order the help lists them.
# `origin` is absent deliberately: a rule the model writes is a user rule, the
# store reserves `seed` for the shipped set, and a caller-set `seed` wedges
# every later map write in that scope.
_RULE_SET_FIELDS = (
    "ledger", "source", "field", "match_kind", "match_value", "action",
    "target", "priority", "note",
)


def _rule_fields_from(args) -> dict:
    """The rule columns this argv names, and only those.

    An unset flag is omitted rather than sent as a default, because `set --id`
    merges into the stored row and a default would overwrite a column the
    caller said nothing about.
    """
    fields = {
        key: getattr(args, key)
        for key in _RULE_SET_FIELDS
        if getattr(args, key, None) is not None
    }
    if args.enabled is not None:
        fields["enabled"] = args.enabled
    return fields


def _missing_scope(args) -> dict | None:
    """Refuse a create that did not choose a scope.

    Both columns default to `''` and the engine reads `''` as "any", so an
    omitted `--ledger` is a rule applying to every ledger and every source —
    the widest scope, arrived at by saying nothing. `''` stays legal; it just
    has to be sent. `routes._explicit_scope` refuses the same thing for the
    same reason.
    """
    missing = [
        name for name in ("ledger", "source") if getattr(args, name) is None
    ]
    if not missing:
        return None
    return {
        "status": "error",
        "error": "send " + " and ".join(f"--{name}" for name in missing)
                 + " explicitly; '' is the any-scope value",
    }


def cmd_transaction_rules_list(args):
    _, _, ctx, err = _resolve_context()
    if err:
        _output(err)
        return
    from istota.money import config_store

    try:
        rules = config_store.list_transaction_rules(
            ctx.db_path, ledger=args.ledger, source=args.source,
            include_disabled=not args.enabled_only,
        )
    except ValueError as exc:
        _output({"status": "error", "error": str(exc)})
        return
    _output({"status": "ok", "rules": rules})


def cmd_transaction_rules_set(args):
    """Write one rule: create without ``--id``, merge a change with one.

    Not an upsert. A second create for a scope, match and action already
    written is refused by the unique index, and the refusal names the existing
    id — which is the id to pass to ``--id``. That loop is closed and visible;
    an upsert would instead have ``--match-kind`` silently decide between
    editing a rule and adding a second one beside it.
    """
    _, _, ctx, err = _resolve_context()
    if err:
        _output(err)
        return
    import sqlite3

    from istota.money import config_store

    fields = _rule_fields_from(args)
    try:
        if args.rule_id is None:
            bad = _missing_scope(args)
            if bad:
                _output(bad)
                return
            rule = config_store.create_transaction_rule(
                ctx.db_path, origin="user", **fields,
            )
            state = "created"
        else:
            rule = config_store.update_transaction_rule(
                ctx.db_path, args.rule_id, **fields,
            )
            state = "updated"
    except sqlite3.IntegrityError:
        # The store looks for a duplicate and then writes, which is not atomic
        # across connections: a concurrent web edit can take the key in
        # between. Answer as the non-racing path does rather than with a
        # traceback, minus the id, which costs a second query here.
        _output({
            "status": "error",
            "error": "a rule with this scope, match and action already exists",
        })
        return
    except ValueError as exc:
        _output({"status": "error", "error": str(exc)})
        return
    if rule is None:
        _output({
            "status": "error",
            "error": f"no rule with id {args.rule_id}",
        })
        return
    _output({"status": "ok", "state": state, "rule": rule})


def cmd_transaction_rules_test(args):
    """Resolve a made-up transaction against the stored rules.

    The web section's ordered trace — every rule in scope, including the ones
    that matched into a slot already filled — is not reproduced here. Pair
    this with ``list``, which returns the same rules in the same evaluation
    order.
    """
    _, _, ctx, err = _resolve_context()
    if err:
        _output(err)
        return
    from datetime import date

    from istota.money import config_store
    from istota.money.core import rules as rule_engine
    from istota.money.core.importers.base import NormalizedTransaction

    stored = config_store.load_rules_for_run(ctx.db_path, args.ledger, args.source)
    if stored is None:
        # `None` is not "no rules": it says the one-time migration has not
        # completed, so an import still resolves from the legacy maps. A
        # preview off the table would describe behaviour this deployment does
        # not have, which is worse than no preview.
        _output({
            "status": "error",
            "error": "transaction rules are not in force here: the one-time "
                     "migration has not completed, so an import still "
                     "resolves from the legacy maps",
        })
        return

    cap = rule_engine.MAX_SUBJECT_CHARS
    txn = NormalizedTransaction(
        date=date.today(),
        amount=0.0,
        payee=args.payee[:cap],
        category=args.category[:cap],
        account_name=args.account[:cap],
        notes=args.notes[:cap],
        tags=[tag[:cap] for tag in (args.tags or [])],
    )
    compiled, dropped = rule_engine.compile_rules_reporting(stored)
    resolution = rule_engine.resolve(txn, compiled)
    _output({
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
    })


def cmd_import_csv(args):
    cli_args = ["import-csv", args.file, "--account", args.account]
    if args.tag:
        for t in args.tag:
            cli_args += ["--tag", t]
    if args.exclude_tag:
        for t in args.exclude_tag:
            cli_args += ["--exclude-tag", t]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_run_scheduled(args):
    cli_args = ["run-scheduled"]
    if args.dry_run:
        cli_args.append("--dry-run")
    if args.skip_monarch:
        cli_args.append("--skip-monarch")
    if args.no_match_invoices:
        cli_args.append("--no-match-invoices")
    if args.tolerance is not None:
        cli_args += ["--tolerance", str(args.tolerance)]
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Invoice commands
# ---------------------------------------------------------------------------


def cmd_invoice_generate(args):
    cli_args = ["invoice", "generate"]
    if args.period:
        cli_args += ["--period", args.period]
    if args.client:
        cli_args += ["--client", args.client]
    if args.entity:
        cli_args += ["--entity", args.entity]
    if args.dry_run:
        cli_args.append("--dry-run")
    _output(_run(cli_args))


def cmd_invoice_list(args):
    cli_args = ["invoice", "list"]
    if args.client:
        cli_args += ["--client", args.client]
    if args.show_all:
        cli_args.append("--all")
    _output(_run(cli_args))


def cmd_invoice_paid(args):
    cli_args = ["invoice", "paid", args.invoice_number, "--date", args.payment_date]
    if args.bank:
        cli_args += ["--bank", args.bank]
    if args.no_post:
        cli_args.append("--no-post")
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_invoice_create(args):
    cli_args = ["invoice", "create", args.client_key]
    if args.service:
        cli_args += ["--service", args.service]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.description:
        cli_args += ["--description", args.description]
    if args.entity:
        cli_args += ["--entity", args.entity]
    if args.item:
        for i in args.item:
            cli_args += ["--item", i]
    _output(_run(cli_args))


def cmd_invoice_unpaid(args):
    _output(_run(["invoice", "unpaid", args.invoice_number]))


def cmd_invoice_void(args):
    cli_args = ["invoice", "void", args.invoice_number]
    if args.force:
        cli_args.append("--force")
    if args.delete_pdf:
        cli_args.append("--delete-pdf")
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Work commands
# ---------------------------------------------------------------------------


def cmd_work_list(args):
    cli_args = ["work", "list"]
    if args.client:
        cli_args += ["--client", args.client]
    if args.period:
        cli_args += ["--period", args.period]
    if args.uninvoiced:
        cli_args.append("--uninvoiced")
    if args.invoiced:
        cli_args.append("--invoiced")
    _output(_run(cli_args))


def cmd_work_add(args):
    cli_args = [
        "work", "add",
        "--date", args.entry_date,
        "--client", args.client,
        "--service", args.service,
    ]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.amount is not None:
        cli_args += ["--amount", str(args.amount)]
    if args.discount is not None:
        cli_args += ["--discount", str(args.discount)]
    if args.description:
        cli_args += ["--description", args.description]
    if args.entity:
        cli_args += ["--entity", args.entity]
    _output(_run(cli_args))


def cmd_work_update(args):
    cli_args = ["work", "update", str(args.entry_id)]
    if args.entry_date is not None:
        cli_args += ["--date", args.entry_date]
    if args.client is not None:
        cli_args += ["--client", args.client]
    if args.service is not None:
        cli_args += ["--service", args.service]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.amount is not None:
        cli_args += ["--amount", str(args.amount)]
    if args.discount is not None:
        cli_args += ["--discount", str(args.discount)]
    if args.description is not None:
        cli_args += ["--description", args.description]
    if args.entity is not None:
        cli_args += ["--entity", args.entity]
    _output(_run(cli_args))


def cmd_work_remove(args):
    _output(_run(["work", "remove", str(args.entry_id)]))


# ---------------------------------------------------------------------------
# Portfolio commands
# ---------------------------------------------------------------------------


def cmd_portfolio_import(args):
    cli_args = ["portfolio", "import", args.file]
    if args.source:
        cli_args += ["--source", args.source]
    if args.dry_run:
        cli_args.append("--dry-run")
    if args.replace is not None:
        cli_args += ["--replace", str(args.replace)]
    _output(_run(cli_args))


def cmd_portfolio_snapshots(args):  # noqa: ARG001
    _output(_run(["portfolio", "snapshots"]))


def cmd_portfolio_summary(args):
    cli_args = ["portfolio", "summary"]
    if args.snapshot is not None:
        cli_args += ["--snapshot", str(args.snapshot)]
    if args.group:
        cli_args += ["--group", args.group]
    _output(_run(cli_args))


def cmd_portfolio_history(args):
    cli_args = ["portfolio", "history"]
    if args.group_by:
        cli_args += ["--group-by", args.group_by]
    if args.group:
        cli_args += ["--group", args.group]
    _output(_run(cli_args))


def cmd_portfolio_diff(args):
    _output(_run(["portfolio", "diff", str(args.older), str(args.newer)]))


def cmd_portfolio_symbol(args):
    _output(_run(["portfolio", "symbol", args.symbol]))


def cmd_portfolio_delete_snapshot(args):
    cli_args = ["portfolio", "delete-snapshot", str(args.snapshot_id)]
    if args.confirmed:
        cli_args.append("--confirmed")
    _output(_run(cli_args))


def cmd_portfolio_accounts(args):
    cli_args = ["portfolio", "accounts"]
    if args.set_group:
        cli_args += ["--set-group", str(args.set_group[0]), args.set_group[1]]
    if args.set_type:
        cli_args += ["--set-type", str(args.set_type[0]), args.set_type[1]]
    if args.exclude is not None:
        cli_args += ["--exclude", str(args.exclude)]
    if args.include is not None:
        cli_args += ["--include", str(args.include)]
    _output(_run(cli_args))


def cmd_portfolio_classifications(args):  # noqa: ARG001
    _output(_run(["portfolio", "classifications"]))


def cmd_portfolio_classify(args):
    cli_args = ["portfolio", "classify", args.symbol, "--asset-class", args.asset_class]
    if args.sub_class:
        cli_args += ["--sub-class", args.sub_class]
    if args.geography:
        cli_args += ["--geography", args.geography]
    _output(_run(cli_args))


def cmd_portfolio_unclassify(args):
    _output(_run(["portfolio", "unclassify", args.symbol]))


def cmd_portfolio_autoclass(args):
    _output(_run(["portfolio", "autoclass"]))


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def _add_map_scope(p) -> None:
    """Which map to act on. Required, so a write is never silently global."""
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile", help="A Monarch profile's own map")
    grp.add_argument("--global", dest="global_scope", action="store_true",
                     help="The global map, which every profile falls back to")


def build_parser():
    # The three rule enums, read off the engine rather than restated here: a
    # choice list offering a value `validate_rule_fields` refuses would fail
    # after the write was composed, and one missing a value it accepts would
    # be unreachable from this surface. Function scope rather than module
    # scope, like every other `istota.money` import in this file:
    # `istota.skills.__init__` star-imports every skill, so a module-level one
    # would put the money package's ~34ms on the import of any skill at all.
    from istota.money.core.rules import (
        ACTIONS as _RULE_ENGINE_ACTIONS,
        FIELDS as _RULE_ENGINE_FIELDS,
        MATCH_KINDS as _RULE_ENGINE_MATCH_KINDS,
    )

    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.money",
        description="Money accounting operations",
    )
    sub = parser.add_subparsers(dest="command")

    # --- Ledger commands ---
    sub.add_parser("list", help="List available ledgers")

    p_check = sub.add_parser("check", help="Validate ledger")
    p_check.add_argument("--ledger", "-l", help="Ledger name")

    p_bal = sub.add_parser("balances", help="Show account balances")
    p_bal.add_argument("--ledger", "-l", help="Ledger name")
    p_bal.add_argument("--account", "-a", help="Filter by account pattern")

    p_query = sub.add_parser("query", help="Run a BQL query")
    p_query.add_argument("bql", help="BQL query string")
    p_query.add_argument("--ledger", "-l", help="Ledger name")

    p_report = sub.add_parser("report", help="Generate financial report")
    p_report.add_argument("report_type", choices=["income-statement", "balance-sheet"])
    p_report.add_argument("--year", "-y", type=int, help="Year for report")
    p_report.add_argument("--ledger", "-l", help="Ledger name")

    p_lots = sub.add_parser("lots", help="Show open lots for a security")
    p_lots.add_argument("symbol", help="Security symbol")
    p_lots.add_argument("--ledger", "-l", help="Ledger name")

    p_ws = sub.add_parser("wash-sales", help="Detect wash sale violations")
    p_ws.add_argument("--year", "-y", type=int, help="Year to analyze")
    p_ws.add_argument("--ledger", "-l", help="Ledger name")

    # --- Transaction commands ---
    p_backfill = sub.add_parser("backfill-ids", help="Stamp stable ids on transactions")
    p_backfill.add_argument("--ledger", "-l", help="Ledger name")

    p_add = sub.add_parser("add-transaction", help="Add a transaction")
    p_add.add_argument("--date", "-d", dest="txn_date", required=True, help="Date (YYYY-MM-DD)")
    p_add.add_argument("--payee", "-p", required=True, help="Payee name")
    p_add.add_argument("--narration", "-n", required=True, help="Description")
    p_add.add_argument("--debit", required=True, help="Debit account")
    p_add.add_argument("--credit", required=True, help="Credit account")
    p_add.add_argument("--amount", "-a", required=True, type=float, help="Amount")
    p_add.add_argument("--currency", default="USD", help="Currency")
    p_add.add_argument("--ledger", "-l", help="Ledger name")

    p_edit = sub.add_parser("edit-transaction", help="Edit a transaction by stable id")
    p_edit.add_argument("--id", required=True, help="Stable transaction id")
    p_edit.add_argument("--old-account", dest="old_account", help="Posting account to edit")
    p_edit.add_argument("--old-position", dest="old_position", help="Posting amount to edit")
    p_edit.add_argument("--date", "-d", dest="new_date", help="New date (YYYY-MM-DD)")
    p_edit.add_argument("--payee", "-p", dest="new_payee", help="New payee")
    p_edit.add_argument("--narration", "-n", dest="new_narration", help="New narration")
    p_edit.add_argument("--account", "-a", dest="new_account", help="New posting account")
    p_edit.add_argument("--position", dest="new_position", help="New posting amount")
    p_edit.add_argument("--ledger", "-l", help="Ledger name")

    p_sync = sub.add_parser("sync-monarch", help="Sync from Monarch Money")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_sync.add_argument("--ledger", "-l", help="Ledger name")
    p_sync.add_argument(
        "--no-match-invoices", action="store_true",
        help="Don't mark an open invoice paid when a synced credit uniquely fits it",
    )
    p_sync.add_argument(
        "--tolerance", type=float,
        help="Dollar slack allowed between a credit and an invoice total (default exact)",
    )

    p_mcm = sub.add_parser(
        "monarch-category-map",
        help="Read or set the Monarch category to beancount account mapping",
    )
    mcm_sub = p_mcm.add_subparsers(dest="category_map_action")

    p_mcm_list = mcm_sub.add_parser("list", help="Show the mapping")
    _add_map_scope(p_mcm_list)

    p_mcm_set = mcm_sub.add_parser("set", help="Map one category to an account")
    _add_map_scope(p_mcm_set)
    p_mcm_set.add_argument("--category", required=True,
                           help="Monarch category name, exactly as Monarch spells it")
    p_mcm_set.add_argument("--account", required=True,
                           help="Beancount account, e.g. Expenses:Internet-Services")

    p_tr = sub.add_parser(
        "transaction-rules",
        help="Read, write and preview the rules that decide what an import posts to",
    )
    tr_sub = p_tr.add_subparsers(dest="transaction_rules_action")

    p_tr_list = tr_sub.add_parser("list", help="Rules in evaluation order")
    p_tr_list.add_argument("--ledger", help="Only this ledger; '' is the any-ledger scope")
    p_tr_list.add_argument("--source", help="Only this source, e.g. monarch-api")
    p_tr_list.add_argument(
        "--enabled-only", action="store_true",
        help="Drop the switched-off rules an import already ignores",
    )

    p_tr_set = tr_sub.add_parser(
        "set", help="Create a rule, or change one by --id",
    )
    p_tr_set.add_argument("--id", type=int, dest="rule_id",
                          help="Change this stored rule instead of creating one")
    p_tr_set.add_argument("--ledger", help="Ledger name; '' is the any-ledger scope")
    p_tr_set.add_argument("--source",
                          help="Importer name, e.g. monarch-api; '' is any source")
    p_tr_set.add_argument("--field", choices=_RULE_ENGINE_FIELDS,
                          help="Which part of the transaction is matched")
    p_tr_set.add_argument("--match-value", help="What that part is compared against")
    p_tr_set.add_argument("--action", choices=_RULE_ENGINE_ACTIONS,
                          help="What a match does")
    p_tr_set.add_argument("--match-kind", choices=_RULE_ENGINE_MATCH_KINDS,
                          help="How it is compared (default iexact)")
    p_tr_set.add_argument("--target",
                          help="Beancount account to post to; omit for a skip rule")
    p_tr_set.add_argument("--priority", type=int,
                          help="Lower runs first (default 100)")
    p_tr_set.add_argument("--note", help="Free text, shown in the settings editor")
    tr_enabled = p_tr_set.add_mutually_exclusive_group()
    tr_enabled.add_argument("--enable", dest="enabled", action="store_true",
                            default=None, help="Switch the rule on")
    tr_enabled.add_argument("--disable", dest="enabled", action="store_false",
                            default=None, help="Store it, switched off")

    p_tr_test = tr_sub.add_parser(
        "test", help="Resolve a made-up transaction against the stored rules",
    )
    p_tr_test.add_argument("--ledger", required=True,
                           help="The ledger the run would be for; '' is any")
    p_tr_test.add_argument("--source", required=True,
                           help="The importer the run would be, e.g. monarch-api")
    p_tr_test.add_argument("--category", default="")
    p_tr_test.add_argument("--account", default="",
                           help="Source account display name")
    p_tr_test.add_argument("--payee", default="")
    p_tr_test.add_argument("--notes", default="", help="The transaction's notes")
    p_tr_test.add_argument("--tag", action="append", dest="tags",
                           help="Repeatable; a tag the transaction carries")

    sub.add_parser(
        "debug-monarch",
        help="Health-check Monarch credentials (whoami probe).",
    )

    p_csv = sub.add_parser("import-csv", help="Import transactions from CSV")
    p_csv.add_argument("file", help="CSV file path")
    p_csv.add_argument("--account", "-a", required=True, help="Bank account")
    p_csv.add_argument("--tag", "-t", action="append", help="Include tag")
    p_csv.add_argument("--exclude-tag", "-x", action="append", help="Exclude tag")
    p_csv.add_argument("--ledger", "-l", help="Ledger name")

    p_run = sub.add_parser("run-scheduled", help="Run periodic money tasks (monarch sync + invoice scheduler)")
    p_run.add_argument("--dry-run", action="store_true", help="Preview without generating files")
    p_run.add_argument("--skip-monarch", action="store_true", help="Skip the monarch sync step")
    p_run.add_argument(
        "--no-match-invoices", action="store_true",
        help="Don't mark an open invoice paid when a synced credit uniquely fits it",
    )
    p_run.add_argument(
        "--tolerance", type=float,
        help="Dollar slack allowed between a credit and an invoice total (default exact)",
    )

    # --- Invoice commands (nested subparser) ---
    p_inv = sub.add_parser("invoice", help="Invoice management")
    inv_sub = p_inv.add_subparsers(dest="invoice_command")

    p_inv_gen = inv_sub.add_parser("generate", help="Generate invoices")
    p_inv_gen.add_argument("--period", "-p", help="Billing period (YYYY-MM)")
    p_inv_gen.add_argument("--client", "-c", help="Filter by client")
    p_inv_gen.add_argument("--entity", "-e", help="Filter by entity")
    p_inv_gen.add_argument("--dry-run", action="store_true", help="Preview only")

    p_inv_list = inv_sub.add_parser("list", help="List invoices")
    p_inv_list.add_argument("--client", "-c", help="Filter by client")
    p_inv_list.add_argument("--all", "-a", dest="show_all", action="store_true", help="Include paid")

    p_inv_paid = inv_sub.add_parser("paid", help="Record payment")
    p_inv_paid.add_argument("invoice_number", help="Invoice number")
    p_inv_paid.add_argument("--date", "-d", dest="payment_date", required=True, help="Payment date")
    p_inv_paid.add_argument("--bank", "-b", help="Bank account")
    p_inv_paid.add_argument("--no-post", action="store_true", help="Skip ledger posting")
    p_inv_paid.add_argument("--ledger", "-l", help="Ledger name")

    p_inv_create = inv_sub.add_parser("create", help="Create manual invoice")
    p_inv_create.add_argument("client_key", help="Client key")
    p_inv_create.add_argument("--service", "-s", help="Service key")
    p_inv_create.add_argument("--qty", "-q", type=float, help="Quantity")
    p_inv_create.add_argument("--description", help="Description")
    p_inv_create.add_argument("--entity", "-e", help="Entity key")
    p_inv_create.add_argument("--item", action="append", help="Manual item: \"description\" amount")

    p_inv_unpaid = inv_sub.add_parser(
        "unpaid", help="Reopen a paid invoice (inverse of `invoice paid`)",
    )
    p_inv_unpaid.add_argument("invoice_number", help="Invoice number")

    p_inv_void = inv_sub.add_parser("void", help="Void an invoice")
    p_inv_void.add_argument("invoice_number", help="Invoice number")
    p_inv_void.add_argument("--force", action="store_true", help="Void even if paid")
    p_inv_void.add_argument("--delete-pdf", action="store_true", help="Delete PDF file")

    # --- Work commands (nested subparser) ---
    p_work = sub.add_parser("work", help="Work log management")
    work_sub = p_work.add_subparsers(dest="work_command")

    p_wl = work_sub.add_parser("list", help="List work entries")
    p_wl.add_argument("--client", "-c", help="Filter by client")
    p_wl.add_argument("--period", "-p", help="Filter by period (YYYY-MM)")
    p_wl.add_argument("--uninvoiced", action="store_true", help="Uninvoiced only")
    p_wl.add_argument("--invoiced", action="store_true", help="Invoiced only")

    p_wa = work_sub.add_parser("add", help="Add work entry")
    p_wa.add_argument("--date", "-d", dest="entry_date", required=True, help="Date (YYYY-MM-DD)")
    p_wa.add_argument("--client", "-c", required=True, help="Client key")
    p_wa.add_argument("--service", "-s", required=True, help="Service key")
    p_wa.add_argument("--qty", "-q", type=float, help="Quantity")
    p_wa.add_argument("--amount", type=float, help="Fixed amount")
    p_wa.add_argument("--discount", type=float, help="Discount")
    p_wa.add_argument("--description", help="Description")
    p_wa.add_argument("--entity", "-e", help="Entity override")

    p_wu = work_sub.add_parser("update", help="Update work entry")
    p_wu.add_argument("entry_id", type=int, help="Entry ID")
    p_wu.add_argument("--date", "-d", dest="entry_date", help="Date (YYYY-MM-DD)")
    p_wu.add_argument("--client", "-c", help="Client key")
    p_wu.add_argument("--service", "-s", help="Service key")
    p_wu.add_argument("--qty", "-q", type=float, help="Quantity")
    p_wu.add_argument("--amount", type=float, help="Fixed amount")
    p_wu.add_argument("--discount", type=float, help="Discount")
    p_wu.add_argument("--description", help="Description")
    p_wu.add_argument("--entity", "-e", help="Entity override")

    p_wr = work_sub.add_parser("remove", help="Remove work entry")
    p_wr.add_argument("entry_id", type=int, help="Entry ID")

    # --- Portfolio commands (nested subparser) ---
    p_pf = sub.add_parser("portfolio", help="Portfolio positions snapshots")
    pf_sub = p_pf.add_subparsers(dest="portfolio_command")

    p_pf_imp = pf_sub.add_parser("import", help="Import a positions CSV")
    p_pf_imp.add_argument("file", help="CSV file path")
    p_pf_imp.add_argument("--source", "-s", help="Import source name (auto-detected)")
    p_pf_imp.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_pf_imp.add_argument("--replace", type=int, help="Delete this snapshot id first")

    pf_sub.add_parser("snapshots", help="List imported snapshots")

    p_pf_sum = pf_sub.add_parser("summary", help="Current-state portfolio summary")
    p_pf_sum.add_argument("--snapshot", type=int, help="Snapshot id (default latest)")
    p_pf_sum.add_argument("--group", "-g", help="Filter by account group")

    p_pf_hist = pf_sub.add_parser("history", help="Value over time")
    p_pf_hist.add_argument(
        "--group-by", dest="group_by",
        choices=["total", "group", "account_type", "asset_class"],
        help="Stack the series by this dimension",
    )
    p_pf_hist.add_argument("--group", "-g", help="Filter by account group")

    p_pf_diff = pf_sub.add_parser("diff", help="Diff two snapshots")
    p_pf_diff.add_argument("older", type=int, help="Older snapshot id")
    p_pf_diff.add_argument("newer", type=int, help="Newer snapshot id")

    p_pf_sym = pf_sub.add_parser("symbol", help="One symbol's history")
    p_pf_sym.add_argument("symbol", help="Ticker symbol")

    p_pf_del = pf_sub.add_parser("delete-snapshot", help="Hard-delete a snapshot")
    p_pf_del.add_argument("snapshot_id", type=int, help="Snapshot id")
    p_pf_del.add_argument("--confirmed", action="store_true",
                          help="Required: this is irreversible")

    p_pf_acc = pf_sub.add_parser("accounts", help="Account registry (list/update)")
    p_pf_acc.add_argument("--set-group", nargs=2, metavar=("ID", "GROUP"),
                          help="Set an account's group label (an owner, a purpose — any grouping)")
    p_pf_acc.add_argument("--set-type", nargs=2, metavar=("ID", "TYPE"),
                          help="Set an account's type")
    p_pf_acc.add_argument("--exclude", type=int, help="Exclude account from summaries")
    p_pf_acc.add_argument("--include", type=int, help="Re-include account")

    pf_sub.add_parser("classifications", help="List symbol classifications")

    p_pf_cls = pf_sub.add_parser("classify", help="Set a symbol classification")
    p_pf_cls.add_argument("symbol", help="Ticker symbol")
    p_pf_cls.add_argument("--asset-class", dest="asset_class", required=True)
    p_pf_cls.add_argument("--sub-class", dest="sub_class", help="Sub asset class")
    p_pf_cls.add_argument("--geography", help="Geography label")

    p_pf_uncls = pf_sub.add_parser("unclassify", help="Remove a symbol classification")
    p_pf_uncls.add_argument("symbol", help="Ticker symbol")

    pf_sub.add_parser(
        "autoclass",
        help="Auto-classify unclassified symbols (ticker lookup + heuristics)",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "list": cmd_list,
        "check": cmd_check,
        "balances": cmd_balances,
        "query": cmd_query,
        "report": cmd_report,
        "lots": cmd_lots,
        "wash-sales": cmd_wash_sales,
        "backfill-ids": cmd_backfill_ids,
        "add-transaction": cmd_add_transaction,
        "edit-transaction": cmd_edit_transaction,
        "sync-monarch": cmd_sync_monarch,
        "debug-monarch": cmd_debug_monarch,
        "import-csv": cmd_import_csv,
        "run-scheduled": cmd_run_scheduled,
    }

    if args.command == "transaction-rules":
        rule_commands = {
            "list": cmd_transaction_rules_list,
            "set": cmd_transaction_rules_set,
            "test": cmd_transaction_rules_test,
        }
        fn = rule_commands.get(getattr(args, "transaction_rules_action", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["transaction-rules", "--help"])
    elif args.command == "monarch-category-map":
        map_commands = {
            "list": cmd_monarch_category_map_list,
            "set": cmd_monarch_category_map_set,
        }
        fn = map_commands.get(getattr(args, "category_map_action", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["monarch-category-map", "--help"])
    elif args.command == "invoice":
        invoice_commands = {
            "generate": cmd_invoice_generate,
            "list": cmd_invoice_list,
            "paid": cmd_invoice_paid,
            "create": cmd_invoice_create,
            "unpaid": cmd_invoice_unpaid,
            "void": cmd_invoice_void,
        }
        fn = invoice_commands.get(getattr(args, "invoice_command", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["invoice", "--help"])
    elif args.command == "work":
        work_commands = {
            "list": cmd_work_list,
            "add": cmd_work_add,
            "update": cmd_work_update,
            "remove": cmd_work_remove,
        }
        fn = work_commands.get(getattr(args, "work_command", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["work", "--help"])
    elif args.command == "portfolio":
        portfolio_commands = {
            "import": cmd_portfolio_import,
            "snapshots": cmd_portfolio_snapshots,
            "summary": cmd_portfolio_summary,
            "history": cmd_portfolio_history,
            "diff": cmd_portfolio_diff,
            "symbol": cmd_portfolio_symbol,
            "delete-snapshot": cmd_portfolio_delete_snapshot,
            "accounts": cmd_portfolio_accounts,
            "classifications": cmd_portfolio_classifications,
            "classify": cmd_portfolio_classify,
            "unclassify": cmd_portfolio_unclassify,
            "autoclass": cmd_portfolio_autoclass,
        }
        fn = portfolio_commands.get(getattr(args, "portfolio_command", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["portfolio", "--help"])
    else:
        fn = commands.get(args.command)
        if fn:
            fn(args)
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
