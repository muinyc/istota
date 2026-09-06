"""Tests for istota.cli_money — operator-facing top-level CLI."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from istota import cli_money
from istota.money import config_store
from istota.money.cli import UserContext


@pytest.fixture
def fake_ctx(tmp_path):
    """A UserContext rooted at tmp_path with an initialised DB."""
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


@pytest.fixture
def patched_loader(fake_ctx):
    """Patch resolve_for_user → fake_ctx so CLI commands skip module gating."""
    with patch.object(
        cli_money, "_load_user_ctx", return_value=fake_ctx,
    ):
        yield fake_ctx


def _run(argv: list[str], istota_config=None) -> tuple[int, str, str]:
    """Build the argparse subparser, dispatch, capture output."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cli_money.add_subparser(sub)
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_money.dispatch(args, istota_config) or 0
    return rc, out.getvalue(), err.getvalue()


def _argparse_refusal(argv: list[str]) -> str:
    """Argparse's own stderr for an argv it refuses.

    ``_run`` cannot answer this: argparse raises ``SystemExit`` out of
    ``parse_args``, before the redirect ``_run`` sets up has anything to
    return. Asserting only on the exit code would also be vacuous, since a
    subcommand that does not exist at all is refused with the same 2 — so the
    caller matches on the message, which names the flag.
    """
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cli_money.add_subparser(sub)
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2
    return err.getvalue()


def _json_objects(out: str):
    """Split a run of ``json.dumps(..., indent=2)`` objects back into dicts.

    ``rules list`` prints one object per row, the way ``monarch profile list``
    does, so a reader gets whole records rather than a table that has to drop
    columns.
    """
    decoder = json.JSONDecoder()
    text, index = out.strip(), 0
    while index < len(text):
        obj, end = decoder.raw_decode(text, index)
        yield obj
        index = end
        while index < len(text) and text[index] in " \t\r\n":
            index += 1


class TestClientMutations:
    def test_add_client_create_then_noop(self, patched_loader):
        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme Corp",
        ])
        assert rc == 0
        assert "STATE: created client key=acme" in out

        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme Corp",
        ])
        assert "STATE: noop" in out

    def test_update_client(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme"])
        rc, out, _ = _run([
            "money", "client", "update", "--user", "u1", "--key", "acme",
            "--terms", "NET 15",
        ])
        assert "STATE: updated" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.clients["acme"].terms == "NET 15"

    def test_remove_client(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme"])
        rc, out, _ = _run([
            "money", "client", "remove", "--user", "u1", "--key", "acme",
        ])
        assert "STATE: removed" in out
        rc, out, _ = _run([
            "money", "client", "remove", "--user", "u1", "--key", "acme",
        ])
        assert "STATE: noop" in out

    def test_separate_json(self, patched_loader):
        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme",
            "--separate-json", '["consulting", "training"]',
        ])
        assert "STATE: created" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.clients["acme"].separate == ["consulting", "training"]


class TestCompanyMutations:
    def test_company_lifecycle(self, patched_loader):
        rc, out, _ = _run([
            "money", "company", "add", "--user", "u1", "--key", "ochotona",
            "--name", "Ochotona",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "company", "remove", "--user", "u1", "--key", "ochotona",
        ])
        assert "STATE: removed" in out


class TestServiceMutations:
    def test_service_create(self, patched_loader):
        rc, out, _ = _run([
            "money", "service", "add", "--user", "u1", "--key", "consulting",
            "--display-name", "Consulting", "--rate", "150", "--type", "hours",
        ])
        assert "STATE: created" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.services["consulting"].rate == 150.0


class TestTax:
    def test_tax_set(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "set", "--user", "u1",
            "--tax-year", "2026", "--filing-status", "mfj",
            "--w2-income", "80000",
        ])
        assert "STATE: updated" in out
        loaded = config_store.load_tax(patched_loader.db_path)
        assert loaded.tax_year == 2026
        assert loaded.w2_income == 80000

    def test_tax_set_noop(self, patched_loader):
        # Initial set produces an "updated" state (it's writing for the first time
        # against the loaded defaults).
        _run([
            "money", "tax", "set", "--user", "u1",
            "--tax-year", "2026", "--w2-income", "80000",
        ])
        # Re-running with no field changes against the loaded snapshot is a noop.
        rc, out, _ = _run([
            "money", "tax", "set", "--user", "u1",
        ])
        assert "STATE: noop" in out

    def test_tax_rates_set(self, patched_loader):
        """`rates` carries the payroll scalars, which really are year-keyed."""
        rc, out, _ = _run([
            "money", "tax", "rates", "set", "--user", "u1", "--year", "2026",
            "--ss-wage-base", "184500",
            "--ss-rate", "0.124",
        ])
        assert "STATE: created" in out
        rates = config_store.list_tax_year_rates(patched_loader.db_path)
        assert rates[0]["tax_year"] == 2026
        assert rates[0]["ss_wage_base"] == 184500

    def test_tax_schedule_set_and_remove(self, patched_loader):
        """Brackets and deductions are keyed on year + jurisdiction + status."""
        rc, out, _ = _run([
            "money", "tax", "schedule", "set", "--user", "u1", "--year", "2026",
            "--jurisdiction", "CA", "--filing-status", "mfj",
            "--standard-deduction", "10726",
            "--brackets-json", "[[0, 0.01], [21428, 0.02]]",
        ])
        assert "STATE: created" in out
        rows = config_store.list_tax_schedules(patched_loader.db_path)
        assert rows[0]["jurisdiction"] == "CA"
        assert rows[0]["filing_status"] == "mfj"
        assert rows[0]["brackets"] == [[0, 0.01], [21428, 0.02]]

        rc, out, _ = _run([
            "money", "tax", "schedule", "remove", "--user", "u1",
            "--year", "2026", "--jurisdiction", "CA", "--filing-status", "mfj",
        ])
        assert "STATE: removed" in out
        assert config_store.list_tax_schedules(patched_loader.db_path) == []

    def test_tax_schedule_rejects_unknown_jurisdiction(self, patched_loader):
        rc, _, err = _run([
            "money", "tax", "schedule", "set", "--user", "u1", "--year", "2026",
            "--jurisdiction", "Narnia", "--filing-status", "mfj",
            "--standard-deduction", "1",
        ])
        assert rc == 2
        assert "unknown jurisdiction" in err

    def test_tax_set_state(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "set", "--user", "u1", "--state", "ny",
        ])
        assert "STATE: updated" in out
        assert config_store.load_tax(patched_loader.db_path).state == "NY"

    def test_tax_pattern_add_remove(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "pattern", "add", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "tax", "pattern", "add", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: noop" in out
        rc, out, _ = _run([
            "money", "tax", "pattern", "remove", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: removed" in out


class TestMonarch:
    def test_profile_lifecycle(self, patched_loader):
        rc, out, _ = _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "acme", "--ledger", "acme",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "monarch", "profile", "update", "--user", "u1",
            "--name", "acme", "--lookback-days", "60",
        ])
        assert "STATE: updated" in out
        rc, out, _ = _run([
            "money", "monarch", "profile", "remove", "--user", "u1",
            "--name", "acme",
        ])
        assert "STATE: removed" in out

    def test_account_map_set_unset(self, patched_loader):
        _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "acme", "--ledger", "acme",
        ])
        rc, out, _ = _run([
            "money", "monarch", "account-map", "set", "--user", "u1",
            "--profile", "acme",
            "--monarch-name", "Acme Visa",
            "--account", "Liabilities:Visa",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "monarch", "account-map", "unset", "--user", "u1",
            "--profile", "acme",
            "--monarch-name", "Acme Visa",
        ])
        assert "STATE: removed" in out

    def test_global_account_map(self, patched_loader):
        rc, out, _ = _run([
            "money", "monarch", "account-map", "set", "--user", "u1",
            "--global",
            "--monarch-name", "Bank", "--account", "Assets:Bank",
        ])
        assert "STATE: created" in out

    def test_tag_filter_add(self, patched_loader):
        _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "acme", "--ledger", "acme",
        ])
        rc, out, _ = _run([
            "money", "monarch", "tag-filter", "add", "--user", "u1",
            "--profile", "acme",
            "--kind", "include", "--tag", "Biz",
        ])
        assert "STATE: created" in out


class TestConfigImportExport:
    def test_export_import_round_trip(self, patched_loader, tmp_path):
        # Seed some data
        _run(["money", "client", "add", "--user", "u1", "--key", "acme",
              "--name", "Acme"])
        _run(["money", "company", "add", "--user", "u1", "--key", "ochotona",
              "--name", "Ochotona"])
        _run(["money", "service", "add", "--user", "u1", "--key", "consulting",
              "--display-name", "Consulting", "--rate", "150"])

        export_path = tmp_path / "exported.toml"
        rc, out, _ = _run([
            "money", "config", "export", "--user", "u1",
            "--section", "invoicing", "--file", str(export_path),
        ])
        assert rc == 0
        assert export_path.exists()
        text = export_path.read_text()
        assert "[clients.acme]" in text
        assert "[companies.ochotona]" in text
        assert "[services.consulting]" in text

    def test_import_dry_run_writes_nothing(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            'next_invoice_number = 5\n[clients.foo]\nname = "Foo"\n',
        )
        rc, out, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
            "--dry-run",
        ])
        assert rc == 0
        assert "STATE: created client key=foo" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" not in cfg.clients

    def test_import_actually_writes(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" in cfg.clients

    def test_show_toml(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme",
              "--name", "Acme"])
        rc, out, _ = _run([
            "money", "config", "show", "--user", "u1", "--section", "invoicing",
        ])
        assert rc == 0
        assert "[clients.acme]" in out
        assert 'name = "Acme"' in out

    def test_diff(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, out, _ = _run([
            "money", "config", "diff", "--user", "u1",
            "--file", str(toml_path),
        ])
        assert rc == 0
        assert "STATE: created client key=foo" in out


# =============================================================================
# Regression tests from mulder/scully review
# =============================================================================


class TestStrictMode:
    """Scully Bug 1: --strict actually rejects unknown TOML keys."""

    def test_strict_rejects_unknown_key(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            'next_invoice_number = 5\n'
            'unknown_top_key = "X"\n'
            '[clients.foo]\nname = "Foo"\n'
        )
        rc, out, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing", "--strict",
        ])
        assert rc == 2
        assert "unknown" in err.lower()

    def test_non_strict_warns_but_succeeds(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            '[clients.foo]\nname = "Foo"\nbogus_field = 1\n'
        )
        rc, out, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        assert "warning" in err.lower()


class TestMergeModePreservesScalars:
    """Scully Bug 2: merge-mode import doesn't clobber existing scalars."""

    def test_existing_currency_preserved(self, patched_loader, tmp_path):
        # Seed an existing setting via the API path.
        from istota.money import config_store
        cfg = config_store.load_invoicing(patched_loader.db_path)
        cfg.currency = "EUR"
        cfg.next_invoice_number = 999
        config_store.save_invoicing(patched_loader.db_path, cfg)

        # Import a TOML that doesn't mention currency or next_invoice_number.
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        loaded = config_store.load_invoicing(patched_loader.db_path)
        assert loaded.currency == "EUR"
        assert loaded.next_invoice_number == 999
        assert "foo" in loaded.clients

    def test_replace_mode_does_overwrite(self, patched_loader, tmp_path):
        from istota.money import config_store
        cfg = config_store.load_invoicing(patched_loader.db_path)
        cfg.currency = "EUR"
        config_store.save_invoicing(patched_loader.db_path, cfg)
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing", "--replace",
        ])
        loaded = config_store.load_invoicing(patched_loader.db_path)
        # --replace truncates collections; scalars not mentioned go back to defaults.
        assert loaded.currency == "USD"

    def test_existing_tax_w2_preserved(self, patched_loader, tmp_path):
        from istota.money import config_store
        existing = config_store.load_tax(patched_loader.db_path)
        existing.tax_year = 2026
        existing.w2_income = 80000
        existing.filing_status = "single"
        config_store.save_tax(patched_loader.db_path, existing)

        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[tax]\ntax_year = 2027\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "tax",
        ])
        assert rc == 0
        loaded = config_store.load_tax(patched_loader.db_path)
        assert loaded.tax_year == 2027
        # Untouched
        assert loaded.w2_income == 80000
        assert loaded.filing_status == "single"

    def test_existing_monarch_sync_preserved(self, patched_loader, tmp_path):
        from istota.money import config_store
        existing = config_store.load_monarch(patched_loader.db_path)
        existing.sync.lookback_days = 99
        config_store.save_monarch(patched_loader.db_path, existing)

        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            '[monarch.profiles.acme]\nledger = "acme"\n'
        )
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "monarch",
        ])
        assert rc == 0
        loaded = config_store.load_monarch(patched_loader.db_path)
        assert loaded.sync.lookback_days == 99
        assert any(p.name == "acme" for p in loaded.profiles)


class TestCombinedImport:
    """Mulder P1 #3: combined-form file with [invoicing][tax][monarch] wrappers."""

    def test_combined_imports_invoicing(self, patched_loader, tmp_path):
        from istota.money import config_store
        toml_path = tmp_path / "combined.toml"
        toml_path.write_text(
            '[invoicing]\n'
            '[invoicing.clients.foo]\nname = "Foo"\n'
            '[tax]\ntax_year = 2027\n'
            '[monarch.profiles.x]\nledger = "x"\n'
        )
        rc, out, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path),
        ])
        assert rc == 0
        # All three sections processed.
        assert "section=invoicing" in out
        assert "section=tax" in out
        assert "section=monarch" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" in cfg.clients
        tax = config_store.load_tax(patched_loader.db_path)
        assert tax.tax_year == 2027
        mon = config_store.load_monarch(patched_loader.db_path)
        assert any(p.name == "x" for p in mon.profiles)

    def test_combined_with_both_forms_errors(self, patched_loader, tmp_path):
        toml_path = tmp_path / "ambiguous.toml"
        toml_path.write_text(
            '[invoicing]\n'
            '[invoicing.clients.foo]\nname = "Foo"\n'
            '[clients.bar]\nname = "Bar"\n'  # bare clients alongside [invoicing]
        )
        rc, _, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 2
        assert "wrapper" in err.lower() or "bare" in err.lower()


class TestOperationalPassthrough:
    """`istota money <op>` forwards accounting operations to the money Click tree."""

    def test_pop_user_space_separated(self):
        user, rest = cli_money._pop_user(["generate", "-u", "alice", "--dry-run"])
        assert user == "alice"
        assert rest == ["generate", "--dry-run"]

    def test_pop_user_long_and_equals(self):
        assert cli_money._pop_user(["list", "--user", "bob"]) == ("bob", ["list"])
        assert cli_money._pop_user(["list", "--user=bob"]) == ("bob", ["list"])

    def test_pop_user_attached_short(self):
        assert cli_money._pop_user(["list", "-ubob"]) == ("bob", ["list"])

    def test_pop_user_absent(self):
        assert cli_money._pop_user(["list", "--ledger", "main"]) == (
            None, ["list", "--ledger", "main"],
        )

    def test_operational_requires_user(self):
        rc, _, err = _run(["money", "list"])
        assert rc == 2
        assert "--user/-u is required" in err

    def test_forwards_command_and_strips_user(self, patched_loader):
        """The subcommand name + remaining args reach the Click tree; -u is pulled out."""
        captured = {}

        def fake_invoke(istota_config, user_id, click_args):
            captured["user"] = user_id
            captured["args"] = click_args
            return 0

        with patch.object(cli_money, "_invoke_money_cli", side_effect=fake_invoke):
            rc, _, _ = _run([
                "money", "invoice", "generate", "-u", "alice", "--dry-run",
            ])
        assert rc == 0
        assert captured["user"] == "alice"
        assert captured["args"] == ["invoice", "generate", "--dry-run"]

    def test_end_to_end_work_list_empty(self, patched_loader):
        """A real forward through the Click tree returns JSON for an empty workspace."""
        import json

        rc, out, err = _run(["money", "work", "list", "-u", "u1"], istota_config=object())
        assert rc == 0, err
        payload = json.loads(out)
        assert payload["status"] == "ok"

    def test_dispatch_operational_options_first(self, patched_loader):
        """The main() peel path: command + options-first args, -u extracted."""
        captured = {}

        def fake_invoke(istota_config, user_id, click_args):
            captured["user"] = user_id
            captured["args"] = click_args
            return 0

        with patch.object(cli_money, "_invoke_money_cli", side_effect=fake_invoke):
            rc = cli_money.dispatch_operational(
                "list", ["-u", "alice", "--account", "Foo"], object(),
            )
        assert rc == 0
        assert captured["user"] == "alice"
        assert captured["args"] == ["list", "--account", "Foo"]

    def test_dispatch_operational_requires_user(self):
        rc = cli_money.dispatch_operational("balances", ["--account", "Foo"], object())
        assert rc == 2

    def test_is_operational(self):
        assert cli_money.is_operational("invoice")
        assert cli_money.is_operational("list")
        assert not cli_money.is_operational("client")
        assert not cli_money.is_operational("config")


class TestCliDeleteGuards:
    """The reference guards are enforced here too, not only in the web route.

    The skill body tells the agent a referenced service cannot be deleted, and
    the agent's reach is `istota money service remove` — so the guard has to
    live below both surfaces or the documented contract is false where it
    matters most.
    """

    def test_referenced_service_refused(self, patched_loader):
        from istota.money.work import add_work_entry

        _run([
            "money", "service", "add", "--user", "u1", "--key", "dev",
            "--display-name", "Dev", "--rate", "150",
        ])
        add_work_entry(patched_loader.data_dir, "2026-03-01", "acme", "dev", qty=2)

        rc, _, err = _run(["money", "service", "remove", "--user", "u1", "--key", "dev"])
        assert rc == 2
        assert "work entr" in err
        assert "dev" in config_store.load_invoicing(patched_loader.db_path).services

    def test_unreferenced_service_removed(self, patched_loader):
        _run([
            "money", "service", "add", "--user", "u1", "--key", "design",
            "--display-name", "Design", "--rate", "90",
        ])
        rc, out, _ = _run(["money", "service", "remove", "--user", "u1", "--key", "design"])
        assert rc == 0
        assert "STATE: removed" in out

    def test_referenced_entity_refused(self, patched_loader):
        _run(["money", "company", "add", "--user", "u1", "--key", "oldco", "--name", "Old"])
        _run(["money", "company", "add", "--user", "u1", "--key", "newco", "--name", "New"])
        _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme", "--entity", "oldco",
        ])

        rc, _, err = _run(["money", "company", "remove", "--user", "u1", "--key", "oldco"])
        assert rc == 2
        assert "acme" in err
        assert "oldco" in config_store.load_invoicing(patched_loader.db_path).companies

    def test_client_remove_is_still_unguarded(self, patched_loader):
        """The soft case — entries and invoices survive a missing client."""
        from istota.money.work import add_work_entry

        _run(["money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme"])
        add_work_entry(patched_loader.data_dir, "2026-03-01", "acme", "dev", qty=2)

        rc, out, _ = _run(["money", "client", "remove", "--user", "u1", "--key", "acme"])
        assert rc == 0
        assert "STATE: removed" in out


class TestCliClientKeyCase:
    """Work entries store the client lowercased, so a mixed-case config key
    matches none of them and the client's work is silently never billed."""

    def test_mixed_case_client_key_refused(self, patched_loader):
        rc, _, err = _run([
            "money", "client", "add", "--user", "u1", "--key", "Acme", "--name", "Acme",
        ])
        assert rc == 2
        assert "lowercase" in err
        assert "Acme" not in config_store.load_invoicing(patched_loader.db_path).clients

    def test_lowercase_key_accepted(self, patched_loader):
        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme",
        ])
        assert rc == 0
        assert "STATE: created" in out


class TestTransactionRules:
    """``istota money rules`` — the operator front end over `transaction_rules`.

    A third surface over the same accessors the six HTTP routes and the web
    section use, so what it has to agree with them about is validation and
    error text rather than payload shape.
    """

    def _add(self, extra: list[str] | None = None) -> tuple[int, str, str]:
        return _run([
            "money", "rules", "add", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--field", "category", "--match-value", "Software",
            "--action", "posting_account", "--target", "Expenses:Biz:Software",
            *(extra or []),
        ])

    def _rules(self, ctx) -> list[dict]:
        return config_store.list_transaction_rules(
            ctx.db_path, ledger="personal", source="monarch-api",
        )

    def test_add_then_list(self, patched_loader):
        rc, out, _ = self._add()
        assert rc == 0
        assert "STATE: created rule" in out

        rc, out, _ = _run([
            "money", "rules", "list", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
        ])
        assert rc == 0
        rows = list(_json_objects(out))
        assert [r["match_value"] for r in rows] == ["Software"]
        assert rows[0]["target"] == "Expenses:Biz:Software"

    def test_an_added_rule_is_the_operators_own(self, patched_loader):
        """`origin` is not a flag. A rule written here is a user rule.

        The store reserves `seed` for the shipped set and wedges every later
        map write if a caller claims it, so the safest surface is one with no
        way to say anything but `user` — which is also what the web card
        sends.
        """
        self._add()
        assert self._rules(patched_loader)[0]["origin"] == "user"

    def test_the_scope_must_be_chosen_rather_than_defaulted_into(
        self, patched_loader,
    ):
        """Both columns default to `''`, which the engine reads as "any", so
        an omitted ledger is a rule applying everywhere. The HTTP create
        refuses that; argparse refuses it here."""
        for missing, absent in (
            (["--source", "monarch-api"], "--ledger"),
            (["--ledger", "personal"], "--source"),
        ):
            err = _argparse_refusal([
                "money", "rules", "add", "--user", "u1", *missing,
                "--field", "category", "--match-value", "Software",
                "--action", "posting_account", "--target", "Expenses:X",
            ])
            assert "the following arguments are required" in err
            assert absent in err.split("the following arguments are required")[1]

    def test_the_any_scope_is_reachable_as_an_empty_string(self, patched_loader):
        rc, out, _ = _run([
            "money", "rules", "add", "--user", "u1",
            "--ledger", "", "--source", "",
            "--field", "tag", "--match-value", "Personal", "--action", "skip",
        ])
        assert rc == 0
        stored = config_store.list_transaction_rules(
            patched_loader.db_path, ledger="", source="",
        )
        assert [r["match_value"] for r in stored if r["origin"] == "user"] == [
            "Personal",
        ]

    def test_a_bad_account_is_refused_without_echoing_it(self, patched_loader):
        """The one thing every front end owes this feature.

        A validation message names the field and the constraint; the target is
        the user's own financial data and reaches a terminal, a log and — on
        the skill path — a model's context.
        """
        rc, _, err = self._add(["--target", "expenses:nope"])
        assert rc == 2
        assert "error:" in err
        assert "target" in err
        assert "expenses:nope" not in err
        assert self._rules(patched_loader) == []

    def test_an_over_long_match_value_is_refused_without_echoing_it(
        self, patched_loader,
    ):
        value = "x" * 400
        rc, _, err = _run([
            "money", "rules", "add", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--field", "category", "--match-value", value,
            "--action", "posting_account", "--target", "Expenses:X",
        ])
        assert rc == 2
        assert "match_value" in err
        assert value not in err

    def test_a_duplicate_names_the_existing_id_and_not_the_value(
        self, patched_loader,
    ):
        self._add()
        existing = self._rules(patched_loader)[0]["id"]
        rc, _, err = self._add(["--target", "Expenses:Other"])
        assert rc == 2
        assert f"id {existing}" in err
        assert "Software" not in err
        assert "Expenses:Other" not in err

    def test_a_skip_rule_takes_no_target(self, patched_loader):
        rc, _, err = _run([
            "money", "rules", "add", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--field", "tag", "--match-value", "Personal",
            "--action", "skip", "--target", "Expenses:X",
        ])
        assert rc == 2
        assert "skip" in err

    def test_update_merges_one_field_and_leaves_the_rest(self, patched_loader):
        self._add(["--note", "keep me"])
        rule_id = self._rules(patched_loader)[0]["id"]
        rc, out, _ = _run([
            "money", "rules", "update", "--user", "u1",
            "--id", str(rule_id), "--target", "Expenses:Biz:Tools",
        ])
        assert rc == 0
        assert f"STATE: updated rule id={rule_id}" in out
        row = self._rules(patched_loader)[0]
        assert row["target"] == "Expenses:Biz:Tools"
        assert row["match_value"] == "Software"
        assert row["note"] == "keep me"

    def test_update_can_switch_a_rule_off_and_back_on(self, patched_loader):
        """`--enabled-only` drops the switched-off rule and keeps the rest.

        Asserting an empty list would have been satisfied by a `list` that
        printed nothing for any reason at all, which is why a second, enabled
        rule is here: the property is that one row survives and the disabled
        one is not it.
        """
        self._add()
        self._add(["--match-value", "Hosting", "--target", "Expenses:Biz:Hosting"])
        rule_id = self._rules(patched_loader)[0]["id"]
        _run(["money", "rules", "update", "--user", "u1",
              "--id", str(rule_id), "--disable"])
        assert [r["enabled"] for r in self._rules(patched_loader)] == [False, True]

        rc, out, _ = _run([
            "money", "rules", "list", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api", "--enabled-only",
        ])
        assert rc == 0
        assert [r["match_value"] for r in _json_objects(out)] == ["Hosting"]

        rc, out, _ = _run([
            "money", "rules", "list", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
        ])
        assert rc == 0
        assert len(list(_json_objects(out))) == 2

        _run(["money", "rules", "update", "--user", "u1",
              "--id", str(rule_id), "--enable"])
        assert self._rules(patched_loader)[0]["enabled"] is True

    def test_update_keeps_a_falsy_value_the_argv_named(self, patched_loader):
        """`''` and `0` are values here, not "unset".

        `''` on either scope column means "any scope" and `0` is a legal
        priority, so a builder testing truthiness would drop both and silently
        leave the stored value standing. The create path cannot catch that —
        the store's own defaults are `''` and the same widening — so the
        assertion has to be on a merge.
        """
        self._add()
        rule_id = self._rules(patched_loader)[0]["id"]
        rc, _, err = _run([
            "money", "rules", "update", "--user", "u1", "--id", str(rule_id),
            "--ledger", "", "--priority", "0", "--note", "",
        ])
        assert rc == 0, err
        row = config_store.get_transaction_rule(patched_loader.db_path, rule_id)
        assert row["ledger"] == ""
        assert row["priority"] == 0
        assert row["source"] == "monarch-api"

    def test_a_lost_duplicate_race_is_not_a_traceback(self, patched_loader):
        """The store checks for a duplicate and then inserts, which is not
        atomic across connections, so the unique index can still refuse. That
        handler ships unexercised otherwise, and it answers without the id
        because looking one up costs a query on a path already losing a race.
        """
        import sqlite3

        with patch.object(
            config_store, "create_transaction_rule",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed"),
        ):
            rc, _, err = self._add()
        assert rc == 2
        assert "already exists" in err
        assert "id " not in err

    def test_a_preview_refuses_more_tags_than_the_http_preview_does(
        self, patched_loader,
    ):
        """The three `test` surfaces answer one input one way. Refused rather
        than cut, because dropping a tag changes which rules fire."""
        from istota.money.core import rules as rule_engine

        args = []
        for i in range(rule_engine.MAX_PREVIEW_TAGS + 1):
            args += ["--tag", f"t{i}"]
        rc, out, err = _run([
            "money", "rules", "test", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api", *args,
        ])
        assert rc == 2
        assert str(rule_engine.MAX_PREVIEW_TAGS) in err
        assert out.strip() == ""

    def test_update_of_a_rule_that_is_not_there_is_an_error(self, patched_loader):
        rc, _, err = _run([
            "money", "rules", "update", "--user", "u1",
            "--id", "4242", "--target", "Expenses:X",
        ])
        assert rc == 2
        assert "4242" in err

    def test_remove_then_remove_again(self, patched_loader):
        self._add()
        rule_id = self._rules(patched_loader)[0]["id"]
        rc, out, _ = _run([
            "money", "rules", "remove", "--user", "u1", "--id", str(rule_id),
        ])
        assert rc == 0
        assert f"STATE: removed rule id={rule_id}" in out
        rc, out, _ = _run([
            "money", "rules", "remove", "--user", "u1", "--id", str(rule_id),
        ])
        assert rc == 0
        assert "STATE: noop" in out

    def test_test_resolves_a_made_up_transaction(self, patched_loader):
        self._add()
        rule_id = self._rules(patched_loader)[0]["id"]
        rc, out, _ = _run([
            "money", "rules", "test", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "software",
        ])
        assert rc == 0
        body = json.loads(out)
        assert body["resolution"]["posting_account"] == "Expenses:Biz:Software"
        assert body["resolution"]["skip"] is False
        assert [h["rule_id"] for h in body["resolution"]["hits"]] == [rule_id]

    def test_test_reports_a_skip(self, patched_loader):
        _run([
            "money", "rules", "add", "--user", "u1",
            "--ledger", "personal", "--source", "", "--field", "tag",
            "--match-value", "Personal", "--action", "skip", "--priority", "50",
        ])
        rc, out, _ = _run([
            "money", "rules", "test", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "Software", "--tag", "Personal",
        ])
        assert rc == 0
        assert json.loads(out)["resolution"]["skip"] is True

    def test_test_needs_an_explicit_scope(self, patched_loader):
        """Both scope flags are required, one at a time so each is pinned.

        Sending neither and matching on the flag names would be satisfied by
        argparse's usage line, which names every optional flag too — so it
        could not tell "both required" from "either one required". Supplying
        one per case makes a non-required counterpart parse cleanly, and the
        refusal is then the discriminator.
        """
        for supplied, absent in (
            (["--source", "monarch-api"], "--ledger"),
            (["--ledger", "personal"], "--source"),
        ):
            err = _argparse_refusal([
                "money", "rules", "test", "--user", "u1",
                "--category", "X", *supplied,
            ])
            assert "the following arguments are required" in err
            assert absent in err.split("the following arguments are required")[1]

    def test_test_refuses_on_a_deployment_the_migration_has_not_reached(
        self, patched_loader, monkeypatch,
    ):
        """`load_rules_for_run` answering None means an import still resolves
        from the legacy maps, so a preview drawn from the table would describe
        behaviour this deployment does not have. The HTTP route answers 409;
        this one refuses rather than printing a fiction."""
        monkeypatch.setattr(config_store, "_rules_migrated", lambda conn: False)
        rc, out, err = _run([
            "money", "rules", "test", "--user", "u1",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "Software",
        ])
        assert rc == 2
        assert "migration" in err
        assert out.strip() == ""
