"""Tests for money.cli module.

The standalone config loader (``load_context`` / ``_parse_user_context`` /
``--config``) was removed: the money CLI is injection-only now. Callers (the
``istota money …`` operator CLI and the money skill) resolve a per-user
:class:`Context` and pass it via ``CliRunner.invoke(obj=...)``; config
(invoicing / monarch / tax) is read only from the per-user money DB through
:mod:`istota.money.config_store`.

These tests therefore build the Context directly and seed any invoicing /
monarch config into the DB.
"""

import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from istota.money import config_store
from istota.money.cli import Context, UserContext, cli, _resolve


@pytest.fixture
def runner():
    return CliRunner()


def _make_context(tmp_path, *, ledgers=None, db_path=None, secrets=None,
                  invoicing_config_path=None, monarch_config_path=None,
                  user="default"):
    """Build an injected Context for a tmp workspace, with the DB initialised."""
    dbp = db_path or (tmp_path / "data" / "money.db")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(dbp)
    uctx = UserContext(
        data_dir=tmp_path,
        ledgers=ledgers or [],
        db_path=dbp,
        invoicing_config_path=invoicing_config_path,
        monarch_config_path=monarch_config_path,
    )
    obj = Context()
    obj.users[user] = uctx
    obj.activate_user(user)
    obj.secrets = secrets
    return obj


def _invoke(runner, cli_args, *, tmp_path, ledgers=None, db_path=None,
            secrets=None, invoicing_config_path=None, monarch_config_path=None,
            user="default", obj=None):
    """Invoke a money command through an injected Context."""
    if obj is None:
        obj = _make_context(
            tmp_path, ledgers=ledgers, db_path=db_path, secrets=secrets,
            invoicing_config_path=invoicing_config_path,
            monarch_config_path=monarch_config_path, user=user,
        )
    return runner.invoke(cli, ["-u", user, *cli_args], obj=obj)


def _seed_invoicing(db_path, toml_text):
    """Hydrate the DB invoicing config from a TOML string (old-fixture shape)."""
    cfg = config_store.invoicing_config_from_toml_dict(tomllib.loads(toml_text))
    config_store.save_invoicing(db_path, cfg, replace_collections=True)


def _seed_monarch(db_path, toml_text):
    """Hydrate the DB monarch config from a TOML string (old-fixture shape)."""
    cfg = config_store.monarch_config_from_toml_dict(tomllib.loads(toml_text))
    config_store.save_monarch(db_path, cfg, replace_collections=True)


@pytest.fixture
def single_ledger(tmp_path):
    """A default ledger file + the ledgers list for a Context."""
    ledger = tmp_path / "main.beancount"
    ledger.write_text("")
    return ledger, [{"name": "default", "path": ledger}]


@pytest.fixture
def invoicing_ctx(tmp_path):
    """Context (obj) with a seeded invoicing config matching the old fixture.

    Mirrors the previous ``invoicing.toml``: company "Test Co", client "acme"
    (terms 30), services "dev" (hours, 150.0) and "hosting" (flat, 50.0).
    accounting_path "." / invoice_output "invoices" / next_invoice_number 1.
    """
    ledger = tmp_path / "main.beancount"
    ledger.write_text("")
    obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
    _seed_invoicing(
        obj.db_path,
        'accounting_path = "."\n'
        'invoice_output = "invoices"\n'
        'next_invoice_number = 1\n\n'
        '[company]\nname = "Test Co"\naddress = "123 Main"\n\n'
        '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
        '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
        'income_account = "Income:Dev"\n\n'
        '[services.hosting]\ndisplay_name = "Hosting"\nrate = 50.0\ntype = "flat"\n'
        'income_account = "Income:Hosting"\n',
    )
    return obj


class TestResolve:
    def test_relative(self, tmp_path):
        assert _resolve(tmp_path, "foo/bar.txt") == tmp_path / "foo/bar.txt"

    def test_absolute(self, tmp_path):
        assert _resolve(tmp_path, "/absolute/path") == Path("/absolute/path")


class TestWorkCommands:
    def test_work_add_and_list(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        result = _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["id"] == 1

        result = _invoke(runner, ["work", "list"], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["count"] == 1
        assert output["entries"][0]["client"] == "acme"
        assert output["entries"][0]["qty"] == 8

    def test_work_list_includes_uid(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "list"], tmp_path=tmp_path, obj=obj)
        entry = json.loads(result.output)["entries"][0]
        assert entry["uid"]
        assert len(entry["uid"]) == 32

    def test_work_backfill_ids(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        work_dir = Path(obj.data_dir) / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\ndate = 2026-03-01\nclient = \"acme\"\nservice = \"dev\"\n"
        )

        result = _invoke(runner, ["work", "backfill-ids"], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        assert json.loads(result.output)["stamped"] == 1

        # Idempotent.
        result = _invoke(runner, ["work", "backfill-ids"], tmp_path=tmp_path, obj=obj)
        assert json.loads(result.output)["stamped"] == 0

    def test_work_remove(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "remove", "1"], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        assert "Removed" in json.loads(result.output)["message"]

    def test_work_update(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "update", "1", "-q", "10"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0

        result = _invoke(runner, ["work", "list"], tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["entries"][0]["qty"] == 10

    def test_work_list_uninvoiced(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-02", "-c", "acme", "-s", "dev", "-q", "4"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["count"] == 2


class TestListCommand:
    def test_list_with_config(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        result = _invoke(runner, ["list"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1

    def test_list_no_config(self, runner):
        # No injected Context with users → the group refuses to run.
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 1


class TestCheckCommand:
    @patch("istota.money.core.ledger.run_bean_check")
    def test_check_success(self, mock_check, runner, tmp_path, single_ledger):
        ledger, ledgers = single_ledger
        ledger.write_text("content")
        mock_check.return_value = (True, [])
        result = _invoke(runner, ["check"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestBalancesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_balances(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = [{"account": "Assets:Bank", "sum(position)": "1000 USD"}]
        result = _invoke(runner, ["balances"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["account_count"] == 1


class TestQueryCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_query(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["query", "SELECT * LIMIT 1"],
            tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestReportCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_income_statement(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["report", "income-statement"],
            tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestWashSalesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_wash_sales(self, mock_query, runner, tmp_path, single_ledger, monkeypatch):
        # wash-sales is experimental — operator must enable money_wash_sales
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "money_wash_sales")
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["wash-sales"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestInvoiceCreate:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_service_creates_db_entries(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000001"
        assert output["total"] == 1200.0

        # Verify work entries exist in DB with invoice number assigned
        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["client"] == "acme"
        assert entry["service"] == "dev"
        assert entry["qty"] == 8
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_manual_item_creates_db_entries(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "--item", '"Custom work" 500',
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["total"] == 500.0

        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["service"] == "_manual"
        assert entry["amount"] == 500.0
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_mixed_service_and_manual(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "4",
            "--item", '"Travel expenses" 200',
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["total"] == 800.0  # 4 * 150 + 200

        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 2

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_visible_in_invoice_list(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "list"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["invoice_count"] == 1
        assert output["invoices"][0]["invoice_number"] == "INV-000001"
        assert output["invoices"][0]["total"] == 1200.0
        assert output["invoices"][0]["status"] == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_increments_invoice_number(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "hosting",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        # The counter is now DB-backed (persist_next_invoice_number writes to
        # the DB when invoicing data exists there), not the old TOML file.
        assert config_store.load_invoicing(invoicing_ctx.db_path).next_invoice_number == 2

    def test_unknown_client_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "nonexistent", "-s", "dev", "-q", "1",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_unknown_service_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "nonexistent", "-q", "1",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_no_items_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "No line items" in output["error"]


class TestInvoiceGenerateScheduleState:
    @staticmethod
    def _generate_previous_month(
        runner, tmp_path, invoicing_ctx, *, dry_run=False,
    ):
        from datetime import date, timedelta
        from istota.money.db import init_db

        config = config_store.load_invoicing(invoicing_ctx.db_path)
        config.clients["acme"].schedule = "monthly"
        config.clients["acme"].schedule_day = 1
        config_store.save_invoicing(invoicing_ctx.db_path, config)
        init_db(invoicing_ctx.db_path)

        this_month = date.today().replace(day=1)
        previous_month = (this_month - timedelta(days=1)).replace(day=1)
        for entry_date in (previous_month, this_month):
            _invoke(runner, [
                "work", "add", "-d", entry_date.isoformat(),
                "-c", "acme", "-s", "dev", "-q", "8",
            ], tmp_path=tmp_path, obj=invoicing_ctx)

        args = [
            "invoice", "generate", "--period", previous_month.strftime("%Y-%m"),
        ]
        if dry_run:
            args.append("--dry-run")
        return _invoke(runner, args, tmp_path=tmp_path, obj=invoicing_ctx)

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_manual_generation_prevents_a_scheduled_invoice_for_the_remainder(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        from istota.money.db import get_db, get_invoice_schedule_state

        result = self._generate_previous_month(runner, tmp_path, invoicing_ctx)

        assert result.exit_code == 0, result.output
        assert "client_key" not in json.loads(result.output)["invoices"][0]
        with get_db(invoicing_ctx.db_path) as conn:
            state = get_invoice_schedule_state(conn, "acme")
        assert state is not None
        assert state.last_generation_at is not None

        scheduled = _invoke(
            runner, ["run-scheduled"], tmp_path=tmp_path, obj=invoicing_ctx,
        )
        assert scheduled.exit_code == 0, scheduled.output
        assert json.loads(scheduled.output)["message"] == "No scheduled invoices due"

        remaining = _invoke(
            runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx,
        )
        assert json.loads(remaining.output)["count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_invoice_history_guards_against_missing_schedule_state(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        from istota.money.db import get_db

        result = self._generate_previous_month(runner, tmp_path, invoicing_ctx)
        assert result.exit_code == 0, result.output
        with get_db(invoicing_ctx.db_path) as conn:
            conn.execute(
                "DELETE FROM invoice_schedule_state WHERE client_key = ?", ("acme",),
            )

        scheduled = _invoke(
            runner, ["run-scheduled"], tmp_path=tmp_path, obj=invoicing_ctx,
        )

        assert scheduled.exit_code == 0, scheduled.output
        assert json.loads(scheduled.output)["message"] == "No scheduled invoices due"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_scheduled_result_keeps_the_client_key(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        from datetime import date
        from istota.money.db import init_db

        config = config_store.load_invoicing(invoicing_ctx.db_path)
        config.clients["acme"].schedule = "monthly"
        config.clients["acme"].schedule_day = 1
        config_store.save_invoicing(invoicing_ctx.db_path, config)
        init_db(invoicing_ctx.db_path)
        _invoke(runner, [
            "work", "add", "-d", date.today().isoformat(),
            "-c", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(
            runner, ["run-scheduled"], tmp_path=tmp_path, obj=invoicing_ctx,
        )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["invoice_count"] == 1
        assert output["invoices"][0]["client_key"] == "acme"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_dry_run_does_not_record_the_client_schedule(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        from istota.money.db import get_db, get_invoice_schedule_state

        result = self._generate_previous_month(
            runner, tmp_path, invoicing_ctx, dry_run=True,
        )

        assert result.exit_code == 0, result.output
        with get_db(invoicing_ctx.db_path) as conn:
            state = get_invoice_schedule_state(conn, "acme")
        assert state is None


class TestInvoicePaid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_ledger_posting_false_skips_post(self, mock_pdf, runner, tmp_path):
        """When client has ledger_posting = false, invoice paid skips ledger entry."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_invoicing(
            obj.db_path,
            'accounting_path = "."\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Test Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[clients.acme.invoicing]\nledger_posting = false\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
            'income_account = "Income:Dev"\n',
        )

        # Create an invoice
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=obj)

        # Record payment — should NOT write to ledger
        result = _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15",
        ], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["no_post"] is True

        # Ledger should remain empty
        assert ledger.read_text() == ""

        # Invoice should be marked paid
        result = _invoke(runner, ["invoice", "list", "--all"],
            tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["invoices"][0]["status"] == "paid"


class TestInvoiceUnpaid:
    """The inverse of `invoice paid`, and the way back from a wrong auto-match.

    `invoice void` is not that inverse — it clears the invoice number too,
    un-invoicing the work rather than reopening the invoice.
    """

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_unpaid_reopens_a_paid_invoice(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15", "--no-post",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "unpaid", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_cleared"] == 1

        listed = json.loads(_invoke(runner, ["invoice", "list"],
            tmp_path=tmp_path, obj=invoicing_ctx).output)
        assert listed["invoices"][0]["status"] == "outstanding"
        # The work stays invoiced — only the payment was undone.
        work = json.loads(_invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx).output)
        assert work["count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_unpaid_on_an_outstanding_invoice_is_an_error(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "unpaid", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not marked paid" in output["error"]

    def test_unpaid_unknown_invoice_is_an_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, ["invoice", "unpaid", "INV-999999"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not found" in output["error"]


class TestInvoiceVoid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_unpaid_invoice(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        # Create an invoice
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is False

        # Verify entry is now uninvoiced
        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_blocked(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        # Mark paid (skip ledger posting since no real ledger)
        _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15", "--no-post",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "paid" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_with_force(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15", "--no-post",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, [
            "invoice", "void", "INV-000001", "--force",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is True

    def test_void_nonexistent_invoice(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, ["invoice", "void", "INV-999999"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not found" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_then_reinvoice(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        # Create and void
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)

        # Entry should be uninvoiced and available for re-invoicing
        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1

        # Invoice number counter was already bumped to 2, so next invoice is INV-000002
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000002"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_with_delete_pdf(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        # --delete-pdf should not crash (was broken: 2-tuple vs 3-tuple unpack)
        result = _invoke(runner, [
            "invoice", "void", "INV-000001", "--delete-pdf",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1


class TestInvoiceListDate:
    """ISSUE-256: `invoice list` reports the invoice's date, not its first work."""

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_reports_the_issue_date_not_the_earliest_work(
        self, mock_pdf, runner, tmp_path, invoicing_ctx,
    ):
        from datetime import date as _date

        for when in ("2026-01-05", "2026-01-20"):
            _invoke(runner, [
                "work", "add", "--date", when, "--client", "acme",
                "--service", "dev", "--qty", "4",
            ], tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "list"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        invoice = json.loads(result.output)["invoices"][0]
        assert invoice["date"] == _date.today().isoformat()


class TestSyncMonarchProfiles:
    def test_sync_no_ledger_calls_sync_all_profiles(self, runner, tmp_path):
        """sync-monarch without --ledger calls sync_all_profiles."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.default]\nledger = "default"\n',
        )

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "ok", "message": "test"}
            result = _invoke(runner, ["sync-monarch"], tmp_path=tmp_path, obj=obj)
            assert result.exit_code == 0, result.output
            mock_sync.assert_called_once()

    def test_sync_with_ledger_and_profiles(self, runner, tmp_path):
        """sync-monarch --ledger syncs only matching profile."""
        biz_ledger = tmp_path / "biz.beancount"
        biz_ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "biz", "path": biz_ledger}])
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.business]\n'
            'ledger = "biz"\n\n'
            '[monarch.profiles.business.tags]\n'
            'include = ["business"]\n',
        )

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            with patch("istota.money.core.transactions.sync_monarch") as mock_sync:
                mock_sync.return_value = {"status": "ok", "transaction_count": 0}
                result = _invoke(runner, ["sync-monarch", "-l", "biz"],
                    tmp_path=tmp_path, obj=obj)
                assert result.exit_code == 0, result.output
                mock_sync.assert_called_once()
                call_kwargs = mock_sync.call_args
                assert call_kwargs[1]["profile"] == "business"


class TestSyncMonarchInvoiceMatching:
    """ISSUE-083: a synced credit settles the one open invoice it fits.

    These go through the real ``sync-monarch`` command with only the Monarch
    fetch stubbed, so they cover the wiring between the core sync's
    ``imported`` list, the matcher, and the work-entry store.
    """

    _MONARCH_TOML = (
        '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
        '[monarch.sync]\nlookback_days = 30\n\n'
        '[monarch.profiles.default]\nledger = "default"\n'
    )

    def _ctx(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_invoicing(
            obj.db_path,
            'accounting_path = "."\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Test Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            # Deliberately left on: with posting disabled, the "no double
            # booking" assertion below would hold no matter what the matcher
            # did, and would prove nothing.
            '[clients.acme.invoicing]\nledger_posting = true\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
            'income_account = "Income:Dev"\n',
        )
        _seed_monarch(obj.db_path, self._MONARCH_TOML)
        return obj

    def _credit(self, amount, payee="Acme Corp", days_after=0, tags=()):
        from datetime import date, timedelta
        return {
            "id": f"mon-{payee}-{amount}-{days_after}",
            "date": (date.today() + timedelta(days=days_after)).isoformat(),
            "merchant": {"name": payee},
            "category": {"name": "Consulting"},
            "account": {"displayName": "Checking"},
            "amount": amount, "notes": "",
            "tags": [{"name": t} for t in tags],
        }

    def _sync(self, runner, tmp_path, obj, txns, extra_args=()):
        with patch("istota.money.core.transactions.fetch_monarch_transactions") as fetch:
            fetch.return_value = txns
            result = _invoke(runner, ["sync-monarch", *extra_args],
                tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def _invoice_status(self, runner, tmp_path, obj, number="INV-000001"):
        result = _invoke(runner, ["invoice", "list", "--all"],
            tmp_path=tmp_path, obj=obj)
        invoices = json.loads(result.output)["invoices"]
        return next(i for i in invoices if i["invoice_number"] == number)["status"]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_matching_credit_marks_the_invoice_paid(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        matching = out["profiles"][0]["invoice_matching"]
        assert [m["invoice_number"] for m in matching["matched"]] == ["INV-000001"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_no_ledger_posting_on_the_auto_match(self, mock_pdf, runner, tmp_path):
        """The sync already booked the income; matching must not book it again.

        The client has ``ledger_posting = true``, so routing the auto-match
        through `invoice paid`'s posting path would add a second transaction
        here and fail this.
        """
        obj = self._ctx(tmp_path)
        ledger = tmp_path / "main.beancount"
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        # One transaction in the ledger: the synced credit. No income posting
        # from the payment recording on top of it. Count transaction headers
        # rather than the payee, which also appears in the monarch-id.
        assert ledger.read_text().count('* "') == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_non_matching_credit_leaves_the_invoice_open(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(999.00)])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_ambiguous_amount_is_flagged_not_guessed(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        for _ in range(2):
            _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
                tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        matching = out["profiles"][0]["invoice_matching"]
        assert "matched" not in matching
        assert matching["review"][0]["candidates"] == ["INV-000001", "INV-000002"]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"
        assert self._invoice_status(runner, tmp_path, obj, "INV-000002") == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_dry_run_does_not_mark_anything_paid(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)],
            extra_args=["--dry-run"])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_no_match_invoices_flag_disables_it(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)],
            extra_args=["--no-match-invoices"])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_tolerance_option_widens_the_match(self, mock_pdf, runner, tmp_path):
        """A wire fee shaves a few dollars off what lands in the account."""
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1185.00)],
            extra_args=["--tolerance", "15"])
        assert out["profiles"][0]["invoice_matching"]["matched"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_debit_of_the_same_size_never_matches(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(-1200.00)])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_second_sync_does_not_rematch_a_paid_invoice(self, mock_pdf, runner, tmp_path):
        """Dedup keeps the credit out of ``imported``, and the invoice is closed."""
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        credit = self._credit(1200.00)
        self._sync(runner, tmp_path, obj, [credit])
        out = self._sync(runner, tmp_path, obj, [credit])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    def test_matching_is_skipped_without_invoicing_config(self, runner, tmp_path):
        """Monarch configured, invoicing not — the sync must still succeed."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_monarch(obj.db_path, self._MONARCH_TOML)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        assert out["status"] == "ok"
        assert "invoice_matching" not in out["profiles"][0]

    def _add_work(self, runner, tmp_path, obj, when, qty="8"):
        _invoke(runner, [
            "work", "add", "--date", when, "--client", "acme",
            "--service", "dev", "--qty", qty,
        ], tmp_path=tmp_path, obj=obj)

    def _strip_issue_date(self, tmp_path):
        """Make an invoice look like one raised before issue dates were stored.

        Nothing can reconstruct the issue date of an existing invoice, so the
        latest-work fallback has to keep working indefinitely. This is the only
        way to build a record in that shape.
        """
        from istota.money.work import _save_entries, load_work_entries

        entries = load_work_entries(tmp_path)
        for entry in entries:
            entry.invoice_date = None
        _save_entries(tmp_path, entries)

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_legacy_invoice_falls_back_to_the_latest_work_billed(
        self, mock_pdf, runner, tmp_path,
    ):
        """No stored issue date: the bound is the latest work, not the earliest.

        Taking the earliest instead would let a credit that landed mid-period
        settle an invoice raised at the end of it.
        """
        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-20", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)
        self._strip_issue_date(tmp_path)

        # $1,200 total; the credit lands between the two work entries.
        credit = self._credit(1200.00)
        credit["date"] = "2026-01-10"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"
        matching = out["profiles"][0]["invoice_matching"]
        assert "matched" not in matching
        assert "issued after this payment" in matching["review"][0]["reason"]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_legacy_invoice_still_matches_a_credit_after_its_last_work(
        self, mock_pdf, runner, tmp_path,
    ):
        """The fallback must stay usable, not just safe.

        Under the stored issue date this credit would be rejected — the
        invoice was really raised today. With no date recorded, the loose
        bound is all there is and the match still has to land.
        """
        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-20", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)
        self._strip_issue_date(tmp_path)

        credit = self._credit(1200.00)
        credit["date"] = "2026-02-01"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert out["profiles"][0]["invoice_matching"]["matched"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_credit_before_the_invoice_was_issued_is_not_a_match(
        self, mock_pdf, runner, tmp_path,
    ):
        """ISSUE-256: the bound is the issue date, not the last work billed.

        January work invoiced months later. A credit that landed in February
        is in the gap between the last work and the issue date: the invoice
        did not exist yet, so it cannot be what that credit paid for. Before
        the stored issue date this was admitted, and being the only open
        invoice at that amount was enough to settle it.
        """
        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-20", qty="4")
        # Generation stamps today, which is well after the billed work.
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)

        credit = self._credit(1200.00)
        credit["date"] = "2026-02-10"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"
        # Not settled, but not silent either: the amount fits to the cent.
        matching = out["profiles"][0]["invoice_matching"]
        assert "matched" not in matching
        assert "issued after this payment" in matching["review"][0]["reason"]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_credit_on_the_issue_date_matches(self, mock_pdf, runner, tmp_path):
        """The bound is inclusive: a credit the day the invoice went out fits.

        The issue date is moved back to a day *before* the latest work billed,
        which is a shape the old latest-work bound could not produce. A credit
        on that day would have been rejected under the old rule, so this can
        only pass off the stored date.
        """
        from datetime import date as _date
        from istota.money.work import _save_entries, load_work_entries

        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-20", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)
        entries = load_work_entries(tmp_path)
        for entry in entries:
            entry.invoice_date = _date(2026, 1, 10)
        _save_entries(tmp_path, entries)

        credit = self._credit(1200.00)
        credit["date"] = "2026-01-10"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert out["profiles"][0]["invoice_matching"]["matched"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_an_entry_hand_added_to_an_issued_invoice_keeps_its_date(
        self, mock_pdf, runner, tmp_path,
    ):
        """`work update --invoice` must not drag the invoice forward to today.

        Stamping today would push the bound past a payment that really did
        settle the invoice — the one way this change could reject a real
        payment, which the bound it replaced never could.
        """
        from datetime import date as _date
        from istota.money.work import _save_entries, load_work_entries

        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)
        entries = load_work_entries(tmp_path)
        for entry in entries:
            entry.invoice_date = _date(2026, 1, 31)
        _save_entries(tmp_path, entries)

        # A forgotten entry, attached to the invoice that already went out.
        self._add_work(runner, tmp_path, obj, "2026-01-06", qty="4")
        uninvoiced = [e for e in load_work_entries(tmp_path) if not e.invoice]
        assert len(uninvoiced) == 1
        _invoke(runner, ["work", "update", str(uninvoiced[0].id),
            "--invoice", "INV-000001"], tmp_path=tmp_path, obj=obj)
        assert {e.invoice_date for e in load_work_entries(tmp_path)} == {_date(2026, 1, 31)}

        credit = self._credit(1200.00)
        credit["date"] = "2026-02-01"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert out["profiles"][0]["invoice_matching"]["matched"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_an_entry_hand_added_to_a_legacy_invoice_stays_undated(
        self, mock_pdf, runner, tmp_path,
    ):
        """There is no date to inherit, and a guess must not be recorded."""
        from istota.money.work import load_work_entries

        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)
        self._strip_issue_date(tmp_path)

        self._add_work(runner, tmp_path, obj, "2026-01-20", qty="4")
        uninvoiced = [e for e in load_work_entries(tmp_path) if not e.invoice]
        _invoke(runner, ["work", "update", str(uninvoiced[0].id),
            "--invoice", "INV-000001"], tmp_path=tmp_path, obj=obj)
        assert all(e.invoice_date is None for e in load_work_entries(tmp_path))

        # Still on the legacy fallback: the latest work billed.
        credit = self._credit(1200.00)
        credit["date"] = "2026-02-01"
        out = self._sync(runner, tmp_path, obj, [credit])
        assert out["profiles"][0]["invoice_matching"]["matched"]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_partly_paid_invoice_is_not_offered_at_its_gross_total(
        self, mock_pdf, runner, tmp_path,
    ):
        """Its total is no longer what the client owes, so it must not match."""
        from datetime import date as _date
        from istota.money.work import _save_entries, load_work_entries

        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-06", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)

        # Mark one of the two entries paid, leaving the invoice half-settled.
        entries = load_work_entries(tmp_path)
        entries[0].paid_date = _date(2026, 1, 31)
        _save_entries(tmp_path, entries)

        # Dated today, so the date filter admits it and the exclusion below is
        # the only thing that can reject it.
        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        assert "invoice_matching" not in out["profiles"][0]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_invoice_with_an_unknown_service_is_not_matchable(
        self, mock_pdf, runner, tmp_path,
    ):
        """`build_line_items` skips unknown services, understating the total.

        A $1,200 invoice whose $600 of work lost its service key would
        otherwise look like a $600 invoice and be settled by an unrelated
        $600 credit.
        """
        obj = self._ctx(tmp_path)
        self._add_work(runner, tmp_path, obj, "2026-01-05", qty="4")
        self._add_work(runner, tmp_path, obj, "2026-01-06", qty="4")
        _invoke(runner, ["invoice", "generate", "--period", "2026-01"],
            tmp_path=tmp_path, obj=obj)

        # Drop one entry's service out of the config's known set.
        from istota.money.work import _save_entries, load_work_entries
        entries = load_work_entries(tmp_path)
        entries[0].service = "retired-service"
        _save_entries(tmp_path, entries)

        # Dated today, so the date filter admits it and the understated total
        # below is the only thing that can reject it.
        out = self._sync(runner, tmp_path, obj, [self._credit(600.00)])
        assert "invoice_matching" not in out["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_a_race_that_settles_the_invoice_first_is_reported_not_claimed(
        self, mock_pdf, runner, tmp_path,
    ):
        """Open invoices are read without the work lock, so this gap is real."""
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.work.record_invoice_payment", return_value=0):
            out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])

        matching = out["profiles"][0]["invoice_matching"]
        assert "matched" not in matching
        assert "already settled or voided" in matching["review"][0]["reason"]

    def test_non_finite_tolerance_is_refused_before_anything_runs(
        self, runner, tmp_path,
    ):
        obj = self._ctx(tmp_path)
        for bad in ("nan", "inf", "-1"):
            result = _invoke(runner, ["sync-monarch", "--tolerance", bad],
                tmp_path=tmp_path, obj=obj)
            output = json.loads(result.output)
            assert output["status"] == "error", bad
            assert "tolerance" in output["error"], bad

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_run_scheduled_can_turn_matching_off(self, mock_pdf, runner, tmp_path):
        """Without this the only off switch also skips the ledger sync."""
        obj = self._ctx(tmp_path)
        obj.users["default"].monarch_config_path = tmp_path / "monarch.toml"
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as fetch:
            fetch.return_value = [self._credit(1200.00)]
            result = _invoke(runner, ["run-scheduled", "--no-match-invoices"],
                tmp_path=tmp_path, obj=obj)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        # The ledger sync still ran.
        assert out["monarch"]["profiles"][0]["transaction_count"] == 1
        assert "invoice_matching" not in out["monarch"]["profiles"][0]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_a_broken_work_file_does_not_fail_the_scheduled_run(
        self, mock_pdf, runner, tmp_path,
    ):
        """Matching runs before invoice generation; it must not abort the run."""
        obj = self._ctx(tmp_path)
        obj.users["default"].monarch_config_path = tmp_path / "monarch.toml"
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.work.load_work_entries",
                   side_effect=ValueError("malformed year file")), \
             patch("istota.money.core.transactions.fetch_monarch_transactions") as fetch:
            fetch.return_value = [self._credit(1200.00)]
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["monarch"]["profiles"][0]["transaction_count"] == 1
        assert "invoice_matching" not in out["monarch"]["profiles"][0]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_a_locked_work_store_does_not_fail_the_sync(self, mock_pdf, runner, tmp_path):
        """A ledger sync that succeeded must not report failure over this."""
        from istota.money.work import WorkStoreLocked

        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.work.record_invoice_payment",
                   side_effect=WorkStoreLocked("held")):
            out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])

        assert out["status"] == "ok"
        matching = out["profiles"][0]["invoice_matching"]
        assert "matched" not in matching
        assert "held" in matching["review"][0]["reason"]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_flat_result_shape_carries_the_matching(self, mock_pdf, runner, tmp_path):
        """`--ledger` naming a ledger no profile targets returns a flat result.

        `_sync_results` handles that shape, but every other test here seeds a
        matching profile and reads `out["profiles"][0]`.
        """
        other = tmp_path / "other.beancount"
        other.write_text("")
        obj = self._ctx(tmp_path)
        obj.users["default"].ledgers.append({"name": "other", "path": other})
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)],
            extra_args=["--ledger", "other"])
        assert "profiles" not in out
        assert [m["invoice_number"] for m in out["invoice_matching"]["matched"]] \
            == ["INV-000001"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_imported_is_not_part_of_the_output(self, mock_pdf, runner, tmp_path):
        """Plumbing for the matcher, not a per-transaction dump into a prompt."""
        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])
        assert "imported" not in out["profiles"][0]
        # Still reported, just as a count.
        assert out["profiles"][0]["transaction_count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_imported_is_stripped_when_matching_is_off(self, mock_pdf, runner, tmp_path):
        obj = self._ctx(tmp_path)
        out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)],
            extra_args=["--no-match-invoices"])
        assert "imported" not in out["profiles"][0]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_a_quarantined_work_file_does_not_fail_the_sync(
        self, mock_pdf, runner, tmp_path,
    ):
        """The read side gets the same best-effort guarantee as the write side."""
        from istota.money.work import WorkFileQuarantined

        obj = self._ctx(tmp_path)
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.work.get_invoice_numbers",
                   side_effect=WorkFileQuarantined("bad row")):
            out = self._sync(runner, tmp_path, obj, [self._credit(1200.00)])

        assert out["status"] == "ok"
        assert out["profiles"][0]["transaction_count"] == 1
        assert "invoice_matching" not in out["profiles"][0]

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_one_invoice_contested_across_two_profiles_is_not_settled(
        self, mock_pdf, runner, tmp_path,
    ):
        """Two profiles, each booking a different credit that fits one invoice.

        Tag filters route one credit to each profile. Matching per profile
        would let whichever ran first settle the invoice and leave the other
        silently unreported, with profile order deciding which.
        """
        biz = tmp_path / "biz.beancount"
        biz.write_text("")
        personal = tmp_path / "personal.beancount"
        personal.write_text("")
        obj = _make_context(tmp_path, ledgers=[
            {"name": "biz", "path": biz}, {"name": "personal", "path": personal},
        ])
        _seed_invoicing(
            obj.db_path,
            'accounting_path = "."\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Test Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[clients.acme.invoicing]\nledger_posting = false\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
            'income_account = "Income:Dev"\n',
        )
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.business]\nledger = "biz"\n\n'
            '[monarch.profiles.business.tags]\ninclude = ["business"]\n\n'
            '[monarch.profiles.personal]\nledger = "personal"\n\n'
            '[monarch.profiles.personal.tags]\ninclude = ["personal"]\n',
        )
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        out = self._sync(runner, tmp_path, obj, [
            self._credit(1200.00, payee="Acme Corp", tags=["business"]),
            self._credit(1200.00, payee="Northwind Ltd", tags=["personal"]),
        ])

        # One credit booked per profile, and both fit INV-000001.
        assert [p["transaction_count"] for p in out["profiles"]] == [1, 1]
        for profile in out["profiles"]:
            matching = profile["invoice_matching"]
            assert "matched" not in matching
            assert "2 payments fit INV-000001" in matching["review"][0]["reason"]
        assert self._invoice_status(runner, tmp_path, obj) == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_run_scheduled_matches_too(self, mock_pdf, runner, tmp_path):
        """The daily cron path is where this matters most."""
        obj = self._ctx(tmp_path)
        obj.users["default"].monarch_config_path = tmp_path / "monarch.toml"
        _invoke(runner, ["invoice", "create", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as fetch:
            fetch.return_value = [self._credit(1200.00)]
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        matching = out["monarch"]["profiles"][0]["invoice_matching"]
        assert [m["invoice_number"] for m in matching["matched"]] == ["INV-000001"]
        assert self._invoice_status(runner, tmp_path, obj) == "paid"


class TestRunScheduled:
    """End-to-end CliRunner coverage for `run-scheduled` — mirrors
    `tests/test_feeds_cli.py::TestPoll.test_run_scheduled_polls_due_feeds`
    so wiring regressions (TypeError from pass-decorator collisions, etc.)
    are caught at the same shape of layer.
    """

    _MONARCH_TOML = (
        '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
        '[monarch.sync]\nlookback_days = 30\n\n'
        '[monarch.profiles.default]\nledger = "default"\n'
    )

    def test_run_scheduled_no_monarch_no_invoicing(self, runner, tmp_path):
        """No monarch_config and no invoicing — succeeds with the
        no-invoicing message, doesn't blow up on wiring."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path,
            ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "No invoicing config" in out["message"]
        assert "monarch" not in out

    def test_run_scheduled_skip_monarch(self, runner, tmp_path):
        """--skip-monarch path — succeeds even with monarch_config set."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        # monarch_config_path set (so the monarch branch would normally fire),
        # but --skip-monarch suppresses it.
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)
        result = _invoke(runner, ["run-scheduled", "--skip-monarch"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "monarch" not in out

    def test_run_scheduled_rolls_up_monarch_error(self, runner, tmp_path):
        """ISSUE-069: a monarch sync failure must surface as outer
        envelope status='error' so the scheduler's JSON-error detector
        and any alerting layer fire, instead of nesting the failure
        invisibly under out['monarch']."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "error", "error": "auth failed"}
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        assert result.exit_code == 1, result.output
        out = json.loads(result.output)
        assert out["status"] == "error"
        assert "auth failed" in out["error"]
        assert out["monarch"]["status"] == "error"

    def test_run_scheduled_partial_error_on_per_profile_failure(self, runner, tmp_path):
        """When sync_all_profiles returns ok but one of its profiles
        failed, surface as partial_error so logs reflect the issue."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {
                "status": "ok",
                "profiles": [
                    {"name": "personal", "ledger": "main", "status": "ok"},
                    {"name": "business", "ledger": "biz", "status": "error", "error": "ledger not found"},
                ],
            }
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        # partial_error is not a hard failure — exit 0, but the envelope
        # records the per-profile breakage for logs/alerting.
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "partial_error"
        assert "business" in out["monarch_errors"]


class TestHelp:
    def test_main_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Money" in result.output

    def test_work_help(self, runner, tmp_path):
        # Descending into a subgroup runs the root cli() callback first, which
        # requires an injected Context with users.
        obj = _make_context(tmp_path)
        result = runner.invoke(cli, ["-u", "default", "work", "--help"], obj=obj)
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "update" in result.output

    def test_invoice_help(self, runner, tmp_path):
        obj = _make_context(tmp_path)
        result = runner.invoke(cli, ["-u", "default", "invoice", "--help"], obj=obj)
        assert result.exit_code == 0
        assert "generate" in result.output
        assert "list" in result.output
        assert "paid" in result.output
        assert "create" in result.output
        assert "void" in result.output


class TestInjectedContext:
    """Callers (the istota money skill) build a Context and inject via obj=."""

    def test_pre_built_context_skips_load_context(self, runner, tmp_path):
        """When obj has users, the cli() group does not try to load a config file."""
        from istota.money.cli import Context, UserContext, cli

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = Context()
        obj.users["alice"] = UserContext(
            data_dir=tmp_path,
            ledgers=[{"name": "main", "path": ledger}],
        )
        obj.activate_user("alice")

        # No -c flag, no MONEY_CONFIG env var — load_context would normally
        # find nothing, but the injected obj is preferred.
        result = runner.invoke(cli, ["-u", "alice", "list"], obj=obj)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1


class TestBackfillIds:
    def test_backfill_ids_command(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food\n\n"
            '2024-02-01 * "Acme" "Coffee"\n'
            "  Expenses:Food   5.00 USD\n"
            "  Assets:Bank:Checking\n"
        )
        result = _invoke(runner, ["backfill-ids"], tmp_path=tmp_path,
            ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["stamped"] == 1
        assert 'id: "' in ledger.read_text()


class TestEditTransactionCLI:
    def test_edit_transaction_command(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food:Coffee\n"
            "2024-01-01 open Expenses:Food:Restaurants\n\n"
            '2024-02-01 * "Acme" "Coffee"\n'
            '  id: "txn-1"\n'
            "  Expenses:Food:Coffee   5.00 USD\n"
            "  Assets:Bank:Checking\n"
        )
        result = _invoke(runner, [
            "edit-transaction", "--id", "txn-1",
            "--old-account", "Expenses:Food:Coffee", "--old-position", "5.00 USD",
            "--account", "Expenses:Food:Restaurants",
        ], tmp_path=tmp_path, ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "Expenses:Food:Restaurants" in ledger.read_text()

    def test_edit_transaction_not_found_exits_nonzero(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("2024-01-01 open Assets:Bank:Checking\n")
        result = _invoke(runner, [
            "edit-transaction", "--id", "nope",
            "--account", "Expenses:X",
        ], tmp_path=tmp_path, ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 1
        assert json.loads(result.output)["status"] == "error"


class TestSyncMonarchLoadsRules:
    """Stage 3's two CLI rule-loading entry points.

    `_sync_monarch_ledgers`' `--ledger` path either syncs the matching
    profiles or falls back to a flat sync for a ledger no profile claims —
    and that fallback is the only way a profile-less ledger is synced on a
    deployment that has profiles.
    """

    # A profile on some *other* ledger, so `has_monarch_data` is true and
    # the deployment is the one the profile-less fallback branch exists for.
    _TOML_WITH_OTHER_PROFILE = (
        '[monarch]\nsession_id = "s"\ncsrftoken = "c"\n\n'
        '[monarch.sync]\ndefault_account = "Assets:Bank:Checking"\n\n'
        '[monarch.profiles.other]\nledger = "unrelated"\n'
    )

    def _txn(self, category="Meals", account="Visa"):
        return {
            "id": "mon-1", "date": "2026-07-13",
            "merchant": {"name": "Hi Tops"},
            "category": {"name": category},
            "account": {"displayName": account},
            "amount": -35.0, "notes": "", "tags": [],
        }

    def _rule(self, db_path, ledger, target):
        config_store.create_transaction_rule(
            db_path, ledger=ledger, source="monarch-api", field="category",
            match_kind="iexact", match_value="Meals",
            action="posting_account", target=target, priority=10,
            enabled=True, origin="user", note="",
        )

    def _sync(self, runner, tmp_path, obj, args=()):
        with patch(
            "istota.money.core.transactions.fetch_monarch_transactions"
        ) as fetch:
            fetch.return_value = [self._txn()]
            return _invoke(
                runner, ["sync-monarch", "--no-match-invoices", *args],
                tmp_path=tmp_path, obj=obj,
            )

    def test_the_profile_less_ledger_fallback_loads_rules(self, runner, tmp_path):
        """A ledger no profile claims still gets the rules in its scope."""
        ledger = tmp_path / "personal.beancount"
        ledger.write_text("")
        obj = _make_context(
            tmp_path, ledgers=[{"name": "personal", "path": ledger}],
        )
        # A deployment that *has* profiles, none of which claims this
        # ledger: the only shape in which the fallback branch runs.
        _seed_monarch(obj.db_path, self._TOML_WITH_OTHER_PROFILE)
        self._rule(obj.db_path, "personal", "Expenses:Business:Meals")

        result = self._sync(runner, tmp_path, obj, ["--ledger", "personal"])

        assert result.exit_code == 0, result.output
        assert "Expenses:Business:Meals" in ledger.read_text()

    def test_a_differently_cased_ledger_argument_still_matches(
        self, runner, tmp_path,
    ):
        """The scope name comes from the config list, the rule from the store.

        The two spellings are allowed to differ, so the comparison folds case.
        An exact match here selects no ledger-scoped rule and is
        indistinguishable from the user having written none.
        """
        ledger = tmp_path / "personal.beancount"
        ledger.write_text("")
        obj = _make_context(
            tmp_path, ledgers=[{"name": "Personal", "path": ledger}],
        )
        _seed_monarch(obj.db_path, self._TOML_WITH_OTHER_PROFILE)
        # Stored lowercase, config says "Personal", argument says "personal".
        self._rule(obj.db_path, "personal", "Expenses:Business:Meals")

        result = self._sync(runner, tmp_path, obj, ["--ledger", "personal"])

        assert result.exit_code == 0, result.output
        assert "Expenses:Business:Meals" in ledger.read_text()

    def test_the_matching_profile_branch_loads_rules(self, runner, tmp_path):
        ledger = tmp_path / "business.beancount"
        ledger.write_text("")
        obj = _make_context(
            tmp_path, ledgers=[{"name": "business", "path": ledger}],
        )
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "s"\ncsrftoken = "c"\n\n'
            '[monarch.sync]\ndefault_account = "Assets:Bank:Checking"\n\n'
            '[monarch.profiles.biz]\nledger = "business"\n'
            'default_account = "Assets:Biz"\n',
        )
        self._rule(obj.db_path, "business", "Expenses:Business:Meals")

        result = self._sync(runner, tmp_path, obj, ["--ledger", "business"])

        assert result.exit_code == 0, result.output
        assert "Expenses:Business:Meals" in ledger.read_text()

    def test_the_all_profiles_branch_loads_rules(self, runner, tmp_path):
        ledger = tmp_path / "business.beancount"
        ledger.write_text("")
        obj = _make_context(
            tmp_path, ledgers=[{"name": "business", "path": ledger}],
        )
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "s"\ncsrftoken = "c"\n\n'
            '[monarch.sync]\ndefault_account = "Assets:Bank:Checking"\n\n'
            '[monarch.profiles.biz]\nledger = "business"\n'
            'default_account = "Assets:Biz"\n',
        )
        self._rule(obj.db_path, "business", "Expenses:Business:Meals")

        result = self._sync(runner, tmp_path, obj)

        assert result.exit_code == 0, result.output
        assert "Expenses:Business:Meals" in ledger.read_text()

    def test_import_csv_loads_rules_for_the_named_ledger(self, runner, tmp_path):
        ledger = tmp_path / "personal.beancount"
        ledger.write_text("")
        csv_path = tmp_path / "export.csv"
        csv_path.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags\n"
            "2026-07-13,Hi Tops,Meals,Visa,HI TOPS,,-35.00,\n"
        )
        obj = _make_context(
            tmp_path, ledgers=[{"name": "personal", "path": ledger}],
        )
        config_store.create_transaction_rule(
            obj.db_path, ledger="personal", source="", field="category",
            match_kind="iexact", match_value="Meals",
            action="posting_account", target="Expenses:Business:Meals",
            priority=10, enabled=True, origin="user", note="",
        )

        result = _invoke(
            runner,
            ["import-csv", str(csv_path), "--account", "Assets:Fallback",
             "--ledger", "personal"],
            tmp_path=tmp_path, obj=obj,
        )

        assert result.exit_code == 0, result.output
        assert "Expenses:Business:Meals" in ledger.read_text()
